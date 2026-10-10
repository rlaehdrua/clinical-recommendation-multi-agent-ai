"""추론·매칭 에이전트: 규칙별 충족 여부를 판정하고 시험 단위 적격성을 결정.

1) 규칙 엔진(tools/rule_engine.py)으로 기계적으로 판정 가능한 규칙을 먼저 처리
2) 나머지 규칙은 Claude가 환자 정보를 근거로 추론 (근거 인용 필수)
3) 시험 단위 적격성은 아래 결정 규칙으로 계산 (LLM이 아닌 코드가 최종 결정)
   - 선정 기준 불충족 또는 제외 기준 해당이 하나라도 있으면 INELIGIBLE
   - 그렇지 않고 임상 기준 중 판정 불가(unknown)가 있으면 UNCERTAIN
     (동의·방문 가능 여부 같은 절차·행정 기준, 원문에 구체 기준이 없는 underspecified 기준의
      unknown은 적격성을 막지 않고 각각 '절차 확인 필요' / '의사 확인 필요'로 남김)
   - 모두 통과하면 ELIGIBLE
"""

from __future__ import annotations

import json
from datetime import date

from .. import config, llm
from ..schemas import CriterionAssessment, LLMAssessment, MatcherOutput, ParsedTrial, PatientProfile, TrialMatch
from ..tools import rule_engine
from .criteria_parser import rules_json

SYSTEM = """당신은 임상시험 적격성을 판정하는 '추론·매칭 에이전트'입니다.
구조화된 환자 정보와 임상시험 규칙 목록을 받아, 각 규칙에 기술된 조건이 이 환자에게 성립하는지(holds)를 판정합니다.

판정 원칙:
- holds="yes": 환자 정보에 조건이 성립한다는 명시적 근거가 있음
- holds="no": 환자 정보에 조건이 성립하지 않는다는 명시적 근거가 있음
- holds="unknown": 근거가 없거나 불충분함. 이때 missing_info에 판정에 필요한 정보를 구체적으로 적습니다.
- 제외 기준 규칙은 '제외 사유가 되는 상태'를 서술합니다. 환자가 그 상태에 해당하면 holds="yes"입니다.
- evidence_type을 정직하게 표시합니다. 기록에 "언급이 없다"는 것은 absent입니다(inferred가 아님). absent이면 holds는 "unknown"입니다.
- 정보가 없다는 사실만으로 "no"라고 판단하지 않습니다. 예: 뇌전이 언급이 없음 -> "unknown" (단, 영상검사에서 뇌전이 없음이 명시되면 "no").
- yes/no 확정은 환자 기록에 명시된 근거(explicit)가 있을 때만 합니다. 나이·성별 등으로부터의 추정이나 "언급이 없으니 아닐 것"은
  evidence_type="inferred" 또는 "absent"로 표시하고 holds="unknown"으로 두며, 추정 내용은 reasoning에, 확인할 사항은 missing_info에 적습니다.
  (예: 62세 여성의 임신 여부 -> 폐경 여부가 명시되지 않았으면 unknown, missing_info="폐경 여부 또는 임신·수유 여부")
- 날짜 조건(예: "28일 이내 검사")은 기준일과 검사일을 비교해 판단합니다. 검사일이 없으면 unknown입니다.
- 단위가 다르면 표준적인 환산을 적용하고 reasoning에 환산식을 적습니다.
- 'consent_or_logistics' 규칙(동의 가능, 방문 가능 등)은 반대 근거가 없으면 unknown으로 두고 missing_info에 확인 필요 사항을 적습니다.
- evidence에는 환자 정보의 source_quote를 그대로 인용합니다.
- underspecified=true 규칙(원문에 약물 목록·기준값이 없는 기준)은, 환자가 명백히 해당할 때만 "yes"로 판정하고 그 외에는 "unknown"으로 둡니다. missing_info에는 의사가 전체 프로토콜과 대조할 때 필요한 환자 정보(예: 현재 복용 약물 전체 목록)를 적습니다.
- 조건부 기준(예: "Men who can father a child: must use contraception", "Women of childbearing potential must have a negative pregnancy test")에서
  환자가 그 대상 집단에 속하지 않으면(예: 여성 환자에게 남성 대상 조건, 폐경이 명시된 환자에게 가임 여성 조건) applicable=false로 표시합니다.
  이 경우 기준은 '해당 없음'으로 통과 처리되므로 holds 값은 무시됩니다. 대상에 속하는지 불확실하면 applicable=true로 두고 판정합니다.
- cohort가 지정된 규칙은 특정 참여자 군 전용입니다. 환자가 그 군에 속하는지와 상관없이 규칙에 적힌 조건이 성립하는지만 판정하세요(어느 군으로 판정할지는 코드가 결정합니다).
- 입력으로 받은 모든 rule_id에 대해 정확히 하나씩 평가를 반환합니다.
"""


LOGISTICS = "consent_or_logistics"


def is_blocking_unknown(a: CriterionAssessment) -> bool:
    """판정 보류(UNCERTAIN)를 일으키는 미확인 항목인가."""
    return a.passes is None and a.category != LOGISTICS and not a.underspecified


def needs_physician_review(a: CriterionAssessment) -> bool:
    return a.passes is None and a.underspecified


def decide(assessments: list[CriterionAssessment]) -> str:
    if any(a.passes is False for a in assessments):
        return "INELIGIBLE"
    if any(is_blocking_unknown(a) for a in assessments):
        return "UNCERTAIN"
    return "ELIGIBLE"


_RANK = {"ELIGIBLE": 2, "UNCERTAIN": 1, "INELIGIBLE": 0}


def _select_cohort(assessments: list[CriterionAssessment], cohorts: list[str]):
    """코호트가 나뉜 시험: 공통 기준 + 각 코호트 기준으로 따로 판정해 가장 유리한 코호트를 선택.

    반환: (판정에 적용한 기준 목록, 선택된 코호트 또는 None, 코호트별 적격성)
    """
    known = [c for c in cohorts if any(a.cohort == c for a in assessments)]
    if not known:
        # 코호트 구분이 없거나 규칙에 연결되지 않음 -> 모든 기준 적용
        return assessments, None, {}
    results = {}
    for c in known:
        subset = [a for a in assessments if a.cohort in (None, c)]
        results[c] = (subset, decide(subset))
    # 적격성 우선, 같으면 통과 기준이 많은 코호트
    best = max(known, key=lambda c: (_RANK[results[c][1]], sum(a.passes is True for a in results[c][0])))
    return results[best][0], best, {c: e for c, (_, e) in results.items()}


def _enforce_evidence(a: LLMAssessment) -> LLMAssessment:
    """근거 없는 확정 판정을 코드로 차단 (모델이 바뀌어도 같은 기준 유지).

    - absent(기록에 관련 정보 없음)인데 yes/no로 판정 -> unknown
    - inferred(추론)로 yes/no를 판정 -> unknown (확신도와 무관)
      예: "62세이니 폐경 후일 것", "기록에 없으니 받지 않았을 것"
      모델이 스스로 매긴 확신도는 믿을 수 없으므로(추론에도 high를 매김), 확정 판정은 명시적 근거가 있을 때만 인정합니다.
    이렇게 되돌린 항목은 확인 질문 대상이 되고, 모델의 추정은 reasoning에 남습니다.
    """
    if a.holds == "unknown" or not a.applicable:
        return a
    reason = None
    if a.evidence_type == "absent":
        reason = "기록에 관련 정보가 없어 확정할 수 없음"
    elif a.evidence_type == "inferred":
        reason = "명시적 근거가 없는 추론이라 확정할 수 없음"
    if reason is None:
        return a
    return a.model_copy(update={
        "holds": "unknown",
        "reasoning": f"[코드 검증: {reason} - 모델 판정 '{a.holds}' 보류] {a.reasoning}",
        "missing_info": a.missing_info or f"확인 필요: {a.reasoning[:120]}",
    })


def _llm_assess(profile: PatientProfile, parsed: ParsedTrial, trial: dict, rules: list) -> MatcherOutput:
    pending_parsed = parsed.model_copy(update={"rules": rules})
    user = f"""기준일(오늘): {date.today().isoformat()}

<patient>
{profile.model_dump_json(indent=1)}
</patient>

<trial id="{parsed.trial_id}">
title: {parsed.title}
target_population: {parsed.target_population}
intervention: {parsed.intervention_summary}
parsing_notes: {parsed.parsing_notes}
brief_summary: {trial.get('brief_summary', '')[:3000]}
</trial>

<rules_to_assess>
{rules_json(pending_parsed)}
</rules_to_assess>

각 규칙의 holds를 판정하세요."""
    return llm.structured(
        MatcherOutput,
        system=SYSTEM,
        user=user,
        effort=config.EFFORT["matcher"],
    )


def match_trial(profile: PatientProfile, parsed: ParsedTrial, trial: dict) -> TrialMatch:
    by_id = {r.rule_id: r for r in parsed.rules}
    results: dict[str, CriterionAssessment] = {}

    for rule in parsed.rules:
        res = rule_engine.evaluate(rule, profile)
        if res is not None:
            results[rule.rule_id] = res

    # LLM 판정: 응답에서 빠진 rule_id만 골라 다시 요청 (전체 재평가 없이 누락분만)
    pending = [r for r in parsed.rules if r.rule_id not in results]
    retried: list[str] = []
    for attempt in range(1 + config.MATCHER_MISSING_RETRIES):
        if not pending:
            break
        if attempt:
            retried.extend(r.rule_id for r in pending)
        asked = {r.rule_id for r in pending}
        for a in _llm_assess(profile, parsed, trial, pending).assessments:
            rule = by_id.get(a.rule_id)
            # 화이트리스트: 이번에 요청하지 않은 rule_id(지어낸 ID 포함)나 중복 응답은 버림
            if rule is None or a.rule_id not in asked or a.rule_id in results:
                continue
            a = _enforce_evidence(a)
            results[a.rule_id] = CriterionAssessment(
                rule_id=a.rule_id,
                kind=rule.kind,
                criterion_text=rule.text,
                holds=a.holds,
                # 조건부 기준의 대상이 아니면 '해당 없음'으로 통과
                passes=True if not a.applicable else rule_engine.passes(rule.kind, a.holds),
                applicable=a.applicable,
                confidence=a.confidence,
                evidence=a.evidence,
                reasoning=a.reasoning,
                missing_info=a.missing_info,
                method="llm",
                category=rule.category,
                underspecified=rule.underspecified,
                cohort=rule.cohort,
            )
        pending = [r for r in pending if r.rule_id not in results]

    # LLM이 누락한 규칙은 unknown으로 채움
    for rule in parsed.rules:
        if rule.rule_id not in results:
            results[rule.rule_id] = CriterionAssessment(
                rule_id=rule.rule_id, kind=rule.kind, criterion_text=rule.text, holds="unknown",
                passes=None, confidence="low", evidence="근거 없음",
                reasoning=f"평가 누락 (모델이 재요청 {config.MATCHER_MISSING_RETRIES}회 후에도 판정을 반환하지 않음)",
                missing_info=rule.text, method="llm", category=rule.category,
                underspecified=rule.underspecified,
                cohort=rule.cohort,
            )

    all_assessments = [results[r.rule_id] for r in parsed.rules]
    assessments, matched_cohort, cohort_results = _select_cohort(all_assessments, parsed.cohorts)
    other = [a for a in all_assessments if a not in assessments]
    eligibility = decide(assessments)
    n_pass = sum(a.passes is True for a in assessments)
    n_fail = sum(a.passes is False for a in assessments)
    n_unknown = sum(a.passes is None for a in assessments)
    failed = [f"{a.rule_id}({a.criterion_text[:60]})" for a in assessments if a.passes is False]
    n_blocking = sum(is_blocking_unknown(a) for a in assessments)
    n_physician = sum(needs_physician_review(a) for a in assessments)
    n_logistics = n_unknown - n_blocking - n_physician
    summary = f"{eligibility}: 통과 {n_pass} / 불충족 {n_fail} / 판정불가 {n_blocking}"
    if n_physician:
        summary += f" (+의사 확인 필요 {n_physician})"
    if n_logistics:
        summary += f" (+절차 기준 확인 필요 {n_logistics})"
    if matched_cohort:
        summary += f" [코호트: {matched_cohort}]"
    if failed:
        summary += " | 불충족: " + "; ".join(failed[:3])

    return TrialMatch(
        patient_id=profile.patient_id,
        trial_id=parsed.trial_id,
        eligibility=eligibility,  # type: ignore[arg-type]
        assessments=assessments,
        n_pass=n_pass,
        n_fail=n_fail,
        n_unknown=n_unknown,
        summary=summary,
        matched_cohort=matched_cohort,
        cohort_results=cohort_results,
        other_cohort_assessments=other,
        retried_rule_ids=retried,
    )


def compact(match: TrialMatch) -> dict:
    """오케스트레이터/추천 에이전트에 넘길 요약 형태."""
    return {
        "trial_id": match.trial_id,
        "eligibility": match.eligibility,
        "summary": match.summary,
        "unknown": [
            {"rule_id": a.rule_id, "criterion": a.criterion_text, "missing_info": a.missing_info}
            for a in match.assessments if is_blocking_unknown(a)
        ],
        "physician_review": [
            {"rule_id": a.rule_id, "criterion": a.criterion_text, "info_needed": a.missing_info}
            for a in match.assessments if needs_physician_review(a)
        ],
        "logistics_to_confirm": [
            a.criterion_text for a in match.assessments
            if a.passes is None and a.category == LOGISTICS and not a.underspecified
        ],
        "failed": [
            {"rule_id": a.rule_id, "criterion": a.criterion_text, "evidence": a.evidence}
            for a in match.assessments if a.passes is False
        ],
    }


def compact_json(matches: list[TrialMatch]) -> str:
    return json.dumps([compact(m) for m in matches], ensure_ascii=False, indent=1)
