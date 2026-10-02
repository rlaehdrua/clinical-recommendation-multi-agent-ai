"""추천 에이전트: 매칭 결과를 종합해 환자별 임상시험 우선순위 제시.

추천 대상 = 적격성(ELIGIBLE/UNCERTAIN) + 현재 참여 가능한 모집 상태.
모집이 끝난 시험은 적격이어도 추천하지 않고 excluded에 사유를 남기며,
추천할 시험이 하나도 없으면 그 이유(no_recommendation_reason)를 코드가 명시합니다.
"""

from __future__ import annotations

import json

from .. import config, llm
from ..schemas import (
    ExcludedTrial, PatientProfile, QAPair, RankedTrial, Recommendation, RecommendationDraft, TrialMatch,
)
from .matcher import needs_physician_review

# 참여 신청이 가능한 모집 상태. UNKNOWN(직접 입력한 시험 등)은 막지 않고 확인 필요로 표시
OPEN_STATUSES = {"RECRUITING", "NOT_YET_RECRUITING", "UNKNOWN"}

STATUS_KO = {
    "RECRUITING": "모집 중",
    "NOT_YET_RECRUITING": "모집 예정",
    "ENROLLING_BY_INVITATION": "초청 대상자만 등록",
    "ACTIVE_NOT_RECRUITING": "진행 중(모집 종료)",
    "COMPLETED": "완료",
    "TERMINATED": "조기 종료",
    "WITHDRAWN": "철회",
    "SUSPENDED": "일시 중단",
    "UNKNOWN": "모집 상태 미확인",
}

SYSTEM = """당신은 환자에게 가장 적절한 임상시험을 추천하는 '추천 에이전트'입니다.
적격성 판정 결과와 시험 정보를 종합해 우선순위를 정합니다.
입력으로 받는 시험은 모두 현재 참여 가능한(모집 중·모집 예정) 시험입니다. 모집이 끝난 시험은 코드가 미리 걸러 냅니다.

우선순위 원칙(위에서부터 중요):
1. 적격성: ELIGIBLE > UNCERTAIN. INELIGIBLE 시험은 ranked에 넣지 않고 excluded에 사유와 함께 넣습니다.
2. UNCERTAIN 시험끼리는 판정 불가 항목이 적고, 남은 항목이 충족될 가능성이 높은 시험을 앞에 둡니다.
3. 환자의 주 진단·병기·바이오마커·치료 이력과 시험 대상군(target population)의 적합도.
4. 모집 상태(RECRUITING > NOT_YET_RECRUITING), 시험 단계 등 실무적 요소.
pre_score는 코드로 계산한 참고 점수이며 위 원칙에 따라 조정할 수 있습니다.

작성 원칙:
- rationale, key_matches, key_concerns는 반드시 판정 결과에 있는 근거(규칙 ID와 환자 정보)에 기반합니다. 입력에 없는 효능·안전성 주장을 하지 않습니다.
- next_steps에는 참여 전 확인해야 할 사항(미확인 기준, 담당의 상담 등)을 적습니다.
- physician_review는 코드가 채우므로 빈 목록으로 둡니다. 모집 상태가 UNKNOWN이면 "모집 상태 확인"을 포함합니다.
- 최종 추천은 rank=1인 시험입니다. 원칙적으로 가장 적합한 시험 하나만 1순위로 정합니다.
  위 원칙으로 적합성이 실질적으로 구분되지 않을 때만 같은 rank를 부여하고(공동 순위), selection_reason에 구분되지 않는 이유를 적습니다.
- selection_reason에는 1순위를 고른 결정적 이유와 차순위 후보보다 나은 점을 근거(규칙 ID·환자 정보)와 함께 적습니다.
- 모든 trial_id는 입력에 있는 값만 사용하고 rank는 1부터 시작하는 정수입니다.
- 한국어로 작성합니다.
"""


def status_of(trial: dict) -> str:
    return (trial.get("overall_status") or "UNKNOWN").upper()


def is_open(trial: dict) -> bool:
    return status_of(trial) in OPEN_STATUSES


def status_ko(trial: dict) -> str:
    s = status_of(trial)
    return f"{STATUS_KO.get(s, s)}({s})"


def pre_score(match: TrialMatch, trial: dict) -> float:
    total = max(1, len(match.assessments))
    score = {"ELIGIBLE": 100.0, "UNCERTAIN": 50.0, "INELIGIBLE": 0.0}[match.eligibility]
    score += 30.0 * match.n_pass / total
    score -= 5.0 * sum(a.passes is None and a.category != "consent_or_logistics" for a in match.assessments)
    if status_of(trial) == "RECRUITING":
        score += 5.0
    return round(score, 1)


def no_recommendation_reason(matches: list[TrialMatch], trials: dict[str, dict]) -> str:
    """추천할 시험이 없을 때 그 이유를 상황별로 명시."""
    if not matches:
        return "평가할 후보 임상시험이 없어 추천할 수 있는 임상시험이 없습니다."
    closed = [m for m in matches if not is_open(trials[m.trial_id])]
    closed_fit = [m for m in closed if m.eligibility != "INELIGIBLE"]

    def fit_desc(ms: list[TrialMatch], with_status: bool = False) -> str:
        label = {"ELIGIBLE": "적격", "UNCERTAIN": "판정 보류"}
        return ", ".join(
            f"{m.trial_id} {label[m.eligibility]}" + (f"·{status_ko(trials[m.trial_id])}" if with_status else "")
            for m in ms
        )
    closed_desc = ", ".join(f"{m.trial_id} {status_ko(trials[m.trial_id])}" for m in closed)

    if len(closed) == len(matches):
        if closed_fit:
            return (f"현재 진행 중인 적절한 임상시험이 없습니다. 기준을 충족하거나 판정이 보류된 시험({fit_desc(closed_fit)})이 "
                    f"있었으나 후보 시험 {len(matches)}개가 모두 모집이 종료되었습니다: {closed_desc}.")
        return (f"현재 진행 중인 적절한 임상시험이 없습니다. 후보 시험 {len(matches)}개가 모두 모집이 종료되었고 "
                f"({closed_desc}), 선정/제외 기준에도 맞지 않았습니다.")
    if closed_fit:
        return (f"현재 진행 중인 적절한 임상시험이 없습니다. 모집 중인 시험은 모두 선정/제외 기준에 맞지 않았고, "
                f"기준을 충족하거나 판정이 보류된 시험({fit_desc(closed_fit, with_status=True)})은 모집이 종료되었습니다.")
    return "현재 진행 중인 적절한 임상시험이 없습니다. 모집 중인 후보 시험이 모두 선정/제외 기준에 맞지 않았습니다."


def _closed_reason(m: TrialMatch, trial: dict) -> str:
    fit = {"ELIGIBLE": "적격 기준은 충족", "UNCERTAIN": "적격 여부 일부 미확인",
           "INELIGIBLE": "선정/제외 기준 불충족"}[m.eligibility]
    return f"모집 종료된 시험으로 현재 참여 불가 - {status_ko(trial)}. ({fit}: {m.summary})"


def physician_notes(match: TrialMatch, profile: PatientProfile | None, qa_log: list[QAPair]) -> list[str]:
    """원문에 구체 기준이 없는 항목별로 의사가 참고할 내용을 정리 (코드가 작성, LLM 미사용)."""
    notes = []
    meds = "; ".join(f.description for f in profile.medications) if profile and profile.medications else None
    for a in match.assessments:
        if not needs_physician_review(a):
            continue
        key = f"{match.trial_id}:{a.rule_id}"
        related = [qa for qa in qa_log if key in qa.target]
        if not related:  # target이 규칙 단위로 연결되지 않았으면 같은 시험의 참고용 문답을 사용
            related = [qa for qa in qa_log if qa.purpose == "physician_reference"
                       and any(t.split(":")[0] == match.trial_id for t in qa.target)]
        note = (f"{a.rule_id} {'제외' if a.kind == 'exclusion' else '선정'} 기준 \"{a.criterion_text}\": "
                f"공개된 프로토콜 원문에 구체 목록·기준이 없어 시스템이 판정하지 않았습니다.")
        if related:
            note += " 환자 응답: " + " / ".join(f"Q. {qa.question} A. {qa.answer}" for qa in related) + "."
        if a.category == "medication" and meds:
            note += f" 기록상 복용 약물: {meds}."
        if not related and not (a.category == "medication" and meds):
            note += " 관련 환자 정보가 수집되지 않았습니다."
        note += " → 참여 결정 전 담당 의사가 전체 프로토콜(금지 약물 목록 등)과 대조해 확인하세요."
        notes.append(note)
    return notes


def recommend(profile: PatientProfile, matches: list[TrialMatch], trials: dict[str, dict],
              qa_log: list[QAPair] | None = None) -> Recommendation:
    open_fit = [m for m in matches if m.eligibility != "INELIGIBLE" and is_open(trials[m.trial_id])]

    if not open_fit:
        # 추천할 시험이 없으면 LLM을 부르지 않고 코드가 결과와 이유를 작성
        reason = no_recommendation_reason(matches, trials)
        return _validate(
            RecommendationDraft(patient_id=profile.patient_id, ranked=[], excluded=[], overall_comment=reason),
            profile.patient_id, matches, trials, profile, qa_log or [],
        )

    items = []
    for m in open_fit + [m for m in matches if m.eligibility == "INELIGIBLE" and is_open(trials[m.trial_id])]:
        t = trials[m.trial_id]
        items.append({
            "trial_id": m.trial_id,
            "title": t.get("title"),
            "overall_status": status_of(t),
            "phases": t.get("phases"),
            "conditions": t.get("conditions"),
            "interventions": [i.get("name") for i in t.get("interventions", [])],
            "eligibility": m.eligibility,
            "matched_cohort": m.matched_cohort,
            "pre_score": pre_score(m, t),
            "summary": m.summary,
            "assessments": [
                {"rule_id": a.rule_id, "kind": a.kind, "criterion": a.criterion_text,
                 "passes": a.passes, "evidence": a.evidence}
                for a in m.assessments
            ],
        })
    user = f"""<patient>
{profile.model_dump_json(indent=1)}
</patient>

<trial_results>
{json.dumps(items, ensure_ascii=False, indent=1)}
</trial_results>

patient_id={profile.patient_id} 환자에 대한 임상시험 추천 순위를 작성하세요."""
    draft = llm.structured(
        RecommendationDraft,
        system=SYSTEM,
        user=user,
        effort=config.EFFORT["recommender"],
    )
    return _validate(draft, profile.patient_id, matches, trials, profile, qa_log or [])


def _validate(draft: RecommendationDraft, patient_id: str, matches: list[TrialMatch],
              trials: dict[str, dict], profile: PatientProfile | None = None,
              qa_log: list[QAPair] | None = None) -> Recommendation:
    """LLM 출력이 코드의 적격성 판정·모집 상태와 어긋나지 않도록 교정."""
    by_id = {m.trial_id: m for m in matches}
    recommendable = {m.trial_id for m in matches
                     if m.eligibility != "INELIGIBLE" and is_open(trials[m.trial_id])}

    ranked, seen = [], set()
    for r in sorted(draft.ranked, key=lambda x: x.rank):
        if r.trial_id not in recommendable or r.trial_id in seen:
            continue
        r.eligibility = by_id[r.trial_id].eligibility  # 판정은 매칭 에이전트 결과를 따름
        ranked.append(r)
        seen.add(r.trial_id)

    # LLM이 빠뜨린 추천 가능 시험은 pre_score 순으로 뒤에 추가 (LLM 순위보다 뒤)
    missing = [by_id[t] for t in recommendable if t not in seen]
    tail_rank = max((r.rank for r in ranked), default=0) + 1
    for i, m in enumerate(sorted(missing, key=lambda m: -pre_score(m, trials[m.trial_id]))):
        ranked.append(RankedTrial(
            rank=tail_rank + i, trial_id=m.trial_id, eligibility=m.eligibility, rationale=m.summary,
            key_matches=[], key_concerns=[], next_steps=["담당 의료진과 적격성 재확인"],
        ))
    _assign_ranks(ranked)
    for r in ranked:
        r.physician_review = physician_notes(by_id[r.trial_id], profile, qa_log or [])
        if r.physician_review and not any("의사" in s for s in r.next_steps):
            r.next_steps.append("원문에 구체 기준이 없는 항목은 담당 의사가 전체 프로토콜과 대조해 참여 여부 판단")

    # 제외 목록: 모집 종료 시험은 코드가 사유를 작성, 부적격 시험은 LLM 사유가 없으면 요약으로 채움
    llm_reasons = {e.trial_id: e.reason for e in draft.excluded}
    excluded = []
    for m in matches:
        if m.trial_id in seen:
            continue
        t = trials[m.trial_id]
        if not is_open(t):
            excluded.append(ExcludedTrial(trial_id=m.trial_id, reason=_closed_reason(m, t)))
        elif m.eligibility == "INELIGIBLE":
            excluded.append(ExcludedTrial(trial_id=m.trial_id, reason=llm_reasons.get(m.trial_id) or m.summary))

    reason = None if ranked else no_recommendation_reason(matches, trials)
    return Recommendation(
        patient_id=patient_id,
        ranked=ranked,
        recommended=[r for r in ranked if r.rank == 1],
        excluded=excluded,
        selection_reason=draft.selection_reason if ranked else "",
        overall_comment=reason or draft.overall_comment,
        no_recommendation_reason=reason,
    )


_TIER = {"ELIGIBLE": 0, "UNCERTAIN": 1}


def _assign_ranks(ranked: list[RankedTrial]) -> None:
    """순위 정규화: 적격성 우선(ELIGIBLE > UNCERTAIN), 그다음 LLM 순위.

    LLM이 같은 순위를 준 시험은 적격성 등급도 같을 때만 공동 순위로 인정합니다.
    (예: ELIGIBLE과 UNCERTAIN을 공동 1순위로 두면 ELIGIBLE만 1순위)
    """
    ranked.sort(key=lambda r: (_TIER.get(r.eligibility, 2), r.rank))
    prev_key, rank = None, 0
    for i, r in enumerate(ranked, start=1):
        key = (_TIER.get(r.eligibility, 2), r.rank)
        if key != prev_key:
            rank = i  # 공동 순위 다음은 건너뛴 순위 (1, 1, 3)
        prev_key = key
        r.rank = rank
