"""추천 에이전트: 매칭 결과를 추천 그룹과 표시 순서로 정리 (아키텍처 §49–50).

추천 대상 = 적격성(ELIGIBLE/UNCERTAIN) + 현재 참여 가능한 모집 상태 + 실제 시험(가상 데모 시험은 데모 모드에서만).
모집이 끝난 시험은 적격이어도 추천하지 않고 excluded에 사유를 남기며,
추천할 시험이 하나도 없으면 그 이유(no_recommendation_reason)를 코드가 명시합니다.

Eligibility와 순서를 분리합니다. 시험 간 임상적 우열을 주장하지 않으므로 LLM은 순위를 정하지 않고 시험별 근거만 씁니다.
표시 순서는 코드가 설명 가능한 기준으로 정합니다: 추천 그룹(외부 상태) -> 모집 상태 -> 남은 미확인 항목 수 -> 후보 순서(추림 관련도).
"""

from __future__ import annotations

import json

from .. import config, llm
from ..schemas import (
    ExcludedTrial, PatientProfile, QAPair, RankedTrial, Recommendation, RecommendationDraft, TrialMatch,
)
from ..status import GROUP_ORDER, LABEL_KO, label
from .matcher import is_blocking_unknown, needs_physician_review, needs_record_check

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

SYSTEM = """당신은 임상시험 추천 결과를 시험별로 정리하는 '추천 에이전트'입니다.
입력으로 받는 시험은 모두 현재 참여 가능한(모집 중·모집 예정) 시험입니다. 모집이 끝난 시험은 코드가 미리 걸러 냅니다.
판정(match_status)과 화면 표시 순서는 코드가 정합니다. 당신은 시험마다 판정 근거를 정리합니다.

match_status (공개 프로토콜 기준 사전 비교 결과이며, 실제 참여 여부는 연구기관 스크리닝과 연구자 판단으로 정해짐):
- CRITERIA_MATCH: 공개 기준 충족 / POTENTIAL_MATCH: 기록 확인 시 충족 가능 / NEEDS_REVIEW: 의료진 검토 필요
- INSUFFICIENT_INFORMATION: 추가 정보 필요 / CRITERIA_MISMATCH: 공개 기준 불충족

작성 원칙:
- 시험 사이의 임상적 우열(어느 시험이 더 낫다·더 적합하다·1순위다)을 판단하거나 주장하지 않습니다. 시험마다 독립적으로 씁니다.
- ranked에는 CRITERIA_MISMATCH가 아닌 시험을 하나씩 모두 넣고 rank는 모두 1로 둡니다(표시 순서는 코드가 정함).
  CRITERIA_MISMATCH 시험은 ranked에 넣지 않고 excluded에 사유와 함께 넣습니다.
- rationale, key_matches, key_concerns는 반드시 판정 결과에 있는 근거(규칙 ID와 환자 정보)에 기반합니다. 입력에 없는 효능·안전성 주장을 하지 않습니다.
- '적격', 'ELIGIBLE', '참여 가능'처럼 등록이 확정된 것으로 읽히는 표현 대신 match_status의 한국어 표현을 씁니다.
- next_steps에는 참여 전 확인해야 할 사항(미확인 기준, 문서 확인, 담당의 상담 등)을 적습니다. 모집 상태가 UNKNOWN이면 "모집 상태 확인"을 포함합니다.
- match_status, physician_review, verification_needed는 코드가 채우므로 비워 두고, selection_reason은 빈 문자열로 둡니다.
- overall_comment에는 시험 간 비교 없이 전체 결과를 요약합니다.
- 모든 trial_id는 입력에 있는 값만 사용합니다.
- 한국어로 작성합니다.
"""


def is_candidate(m: TrialMatch) -> bool:
    """추천 대상이 될 수 있는 판정인가 (INELIGIBLE·NOT_EVALUATED 제외)."""
    return m.eligibility in ("ELIGIBLE", "UNCERTAIN")


def status_of(trial: dict) -> str:
    return (trial.get("overall_status") or "UNKNOWN").upper()


def is_open(trial: dict) -> bool:
    return status_of(trial) in OPEN_STATUSES


def status_ko(trial: dict) -> str:
    s = status_of(trial)
    return f"{STATUS_KO.get(s, s)}({s})"


def is_demo(trial: dict) -> bool:
    """가상 데모 시험 (예: DEMO-LUNG-001). 실제 시험이 아니므로 데모 모드에서만 추천 (아키텍처 §67)."""
    return bool(trial.get("synthetic"))


def is_recommendable(m: TrialMatch, trial: dict) -> bool:
    return is_candidate(m) and is_open(trial) and (config.DEMO_MODE or not is_demo(trial))


_RECRUIT_ORDER = {"RECRUITING": 0, "NOT_YET_RECRUITING": 1}


def unresolved_burden(m: TrialMatch) -> int:
    """남은 미확인 항목 수 (등록 시 확인하는 절차 기준 제외)."""
    return sum(is_blocking_unknown(a) for a in m.assessments)


def display_key(m: TrialMatch, trial: dict, candidate_index: int) -> tuple:
    """표시 순서 (시험 간 임상적 우열이 아니라 설명 가능한 기준)."""
    return (GROUP_ORDER.index(m.match_status) if m.match_status in GROUP_ORDER else len(GROUP_ORDER),
            _RECRUIT_ORDER.get(status_of(trial), 2), unresolved_burden(m), candidate_index, m.trial_id)


DISPLAY_BASIS = ("표시 순서 기준: 추천 그룹(공개 기준 충족 → 기록 확인 시 충족 가능 → 의료진 검토 필요 → 추가 정보 필요) → "
                 "모집 상태(모집 중 → 모집 예정) → 남은 미확인 항목 수 → 후보 추림 순서. "
                 "같은 그룹 안의 순서는 시험 간 임상적 우열을 뜻하지 않습니다.")


def no_recommendation_reason(matches: list[TrialMatch], trials: dict[str, dict]) -> str:
    """추천할 시험이 없을 때 그 이유를 상황별로 명시."""
    if not matches:
        return "평가할 후보 임상시험이 없어 추천할 수 있는 임상시험이 없습니다."
    demo = [m for m in matches if is_demo(trials[m.trial_id]) and not config.DEMO_MODE]
    if demo:
        # 가상 데모 시험은 실제 추천 대상이 아니므로 빼고 이유를 판단한 뒤, 뺐다는 사실을 덧붙임
        real = [m for m in matches if m not in demo]
        note = (f" 가상 데모 시험 {len(demo)}개({', '.join(m.trial_id for m in demo)})는 실제 시험이 아니라 "
                f"데모 모드에서만 추천에 표시합니다.")
        if not real:
            return "후보가 가상 데모 시험뿐이라 추천할 수 있는 실제 임상시험이 없습니다." + note
        return no_recommendation_reason(real, trials) + note
    failed = [m for m in matches if m.eligibility == "NOT_EVALUATED"]
    if failed:
        # 평가 실패 시험이 있으면 '기준에 맞지 않음'으로 단정하지 않고 실패 사실을 명시
        others = [m for m in matches if m.eligibility != "NOT_EVALUATED"]
        note = (f" 평가에 실패한 시험 {len(failed)}개({', '.join(m.trial_id for m in failed)})는 "
                f"적격 여부를 판정하지 못했으므로(부적격 아님) 재평가 또는 담당자 확인이 필요합니다.")
        if not others:
            return "모든 후보 임상시험의 평가에 실패해 추천할 수 있는 임상시험이 없습니다." + note
        return no_recommendation_reason(others, trials) + note
    closed = [m for m in matches if not is_open(trials[m.trial_id])]
    closed_fit = [m for m in closed if is_candidate(m)]

    def fit_desc(ms: list[TrialMatch], with_status: bool = False) -> str:
        return ", ".join(
            f"{m.trial_id} {label(m.match_status)}" + (f"·{status_ko(trials[m.trial_id])}" if with_status else "")
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
    return f"모집 종료된 시험으로 현재 참여 불가 - {status_ko(trial)}. ({label(m.match_status)}: {m.summary})"


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


def verification_notes(match: TrialMatch) -> list[str]:
    """UNVERIFIED: 진료 기록이 필요한 기준인데 근거가 환자 답변뿐이라 판정하지 않은 항목 (D2, 코드가 작성)."""
    def said(a) -> str:
        if not a.self_reported_applicable:
            return "해당 없음"
        return {"yes": "해당", "no": "해당하지 않음"}.get(a.self_reported_holds or "", "불명")
    return [f"{a.rule_id} \"{a.criterion_text[:100]}\": UNVERIFIED - 환자 답변상 '{said(a)}'({a.evidence[:80]}). "
            f"진료 기록이 필요한 기준이라 판정하지 않음 → 검사 결과지·보고서 등 문서로 확인 필요"
            for a in match.assessments if needs_record_check(a)]


def recommend(profile: PatientProfile, matches: list[TrialMatch], trials: dict[str, dict],
              qa_log: list[QAPair] | None = None) -> Recommendation:
    """matches 순서 = 후보 순서(추림 관련도). 표시 순서의 마지막 기준으로 씁니다."""
    open_fit = [m for m in matches if is_recommendable(m, trials[m.trial_id])]

    if not open_fit:
        # 추천할 시험이 없으면 LLM을 부르지 않고 코드가 결과와 이유를 작성
        reason = no_recommendation_reason(matches, trials)
        return _validate(
            RecommendationDraft(patient_id=profile.patient_id, ranked=[], excluded=[], overall_comment=reason),
            profile.patient_id, matches, trials, profile, qa_log or [],
        )

    items = []
    ineligible_open = [m for m in matches if m.eligibility == "INELIGIBLE" and is_open(trials[m.trial_id])
                       and (config.DEMO_MODE or not is_demo(trials[m.trial_id]))]
    for m in open_fit + ineligible_open:
        t = trials[m.trial_id]
        items.append({
            "trial_id": m.trial_id,
            "title": t.get("title"),
            "overall_status": status_of(t),
            "phases": t.get("phases"),
            "conditions": t.get("conditions"),
            "interventions": [i.get("name") for i in t.get("interventions", [])],
            "match_status": m.match_status,
            "matched_cohort": m.matched_cohort,
            "summary": m.summary,
            "assessments": [
                {"rule_id": a.rule_id, "kind": a.kind, "criterion": a.criterion_text,
                 "passes": a.passes, "evidence": a.evidence,
                 **({"unverified_self_report": a.self_reported_holds} if a.needs_verification else {})}
                for a in m.assessments
            ],
        })
    user = f"""<patient>
{profile.model_dump_json(indent=1)}
</patient>

<trial_results>
{json.dumps(items, ensure_ascii=False, indent=1)}
</trial_results>

patient_id={profile.patient_id} 환자에 대해 시험별 근거를 정리하세요."""
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
    """LLM 출력이 코드의 판정·모집 상태와 어긋나지 않도록 교정하고, 표시 순서를 코드가 정함.

    LLM이 매긴 rank와 순서는 쓰지 않습니다(시험 간 우열 주장 제거). LLM 문장은 시험별 근거로만 씁니다.
    """
    by_id = {m.trial_id: m for m in matches}
    index = {m.trial_id: i for i, m in enumerate(matches)}
    order = sorted((m for m in matches if is_recommendable(m, trials[m.trial_id])),
                   key=lambda m: display_key(m, trials[m.trial_id], index[m.trial_id]))
    texts: dict[str, RankedTrial] = {}
    for r in draft.ranked:
        texts.setdefault(r.trial_id, r)

    ranked = []
    for pos, m in enumerate(order, start=1):
        r = texts.get(m.trial_id) or RankedTrial(
            rank=pos, trial_id=m.trial_id, eligibility=m.eligibility, rationale=m.summary,
            key_matches=[], key_concerns=[], next_steps=["담당 의료진과 기준 충족 여부 재확인"],
        )
        r.rank, r.eligibility, r.match_status = pos, m.eligibility, m.match_status  # 판정은 매칭 결과를 따름
        r.physician_review = physician_notes(m, profile, qa_log or [])
        if r.physician_review and not any("의사" in s for s in r.next_steps):
            r.next_steps.append("원문에 구체 기준이 없는 항목은 담당 의사가 전체 프로토콜과 대조해 참여 여부 판단")
        r.verification_needed = verification_notes(m)
        if r.verification_needed:
            r.next_steps.append("UNVERIFIED 항목은 검사 결과지·보고서 등 진료 기록(문서)으로 확인")
        ranked.append(r)

    # 제외 목록: 모집 종료·데모 시험은 코드가 사유를 작성, 부적격 시험은 LLM 사유가 없으면 요약으로 채움
    seen = {r.trial_id for r in ranked}
    llm_reasons = {e.trial_id: e.reason for e in draft.excluded}
    excluded = []
    for m in matches:
        if m.trial_id in seen:
            continue
        t = trials[m.trial_id]
        if m.eligibility == "NOT_EVALUATED":
            excluded.append(ExcludedTrial(trial_id=m.trial_id, reason=(
                f"평가 실패로 적격 여부를 판정하지 못함(부적격 아님) - 재평가 또는 담당자 확인 필요. "
                f"원인: {(m.evaluation_error or '')[:200]}")))
        elif not is_open(t):
            excluded.append(ExcludedTrial(trial_id=m.trial_id, reason=_closed_reason(m, t)))
        elif is_demo(t) and not config.DEMO_MODE:
            excluded.append(ExcludedTrial(trial_id=m.trial_id, reason=(
                f"가상 데모 시험(실제 시험 아님) - 데모 모드에서만 추천에 표시. ({label(m.match_status)}: {m.summary})")))
        elif m.eligibility == "INELIGIBLE":
            excluded.append(ExcludedTrial(trial_id=m.trial_id, reason=llm_reasons.get(m.trial_id) or m.summary))

    reason = None if ranked else no_recommendation_reason(matches, trials)
    top_group = ranked[0].match_status if ranked else None
    return Recommendation(
        patient_id=patient_id,
        ranked=ranked,
        recommended=[r for r in ranked if r.match_status == top_group],
        excluded=excluded,
        selection_reason=(f"추천 그룹: {LABEL_KO.get(top_group or '', '')}. " + DISPLAY_BASIS) if ranked else "",
        overall_comment=reason or draft.overall_comment,
        no_recommendation_reason=reason,
    )
