"""임상의 판단 기준의 안전 경계: 임상의 판단 없이는 이 기준을 확정하지 않습니다 (읽기·판정 계층 공용, LLM 호출 없음).

불변식 (계약 CriterionResult._authority의 "전문가 판단 기준은 임상의만 확정"을 판정 계층에서 지킴)
- 이 기준의 MET/NOT_MET은 LLM·판정기(reviewed 슬롯 연결 포함)·계약 평가기·규칙 엔진 어느 쪽 값으로도 남기지 않습니다.
- 문서 확인은 임상의 판단이 아닙니다: 근거가 문서 검증돼 있어도 같습니다.
- 다른 기준의 판정은 건드리지 않습니다: 독립적인 불충족이 있으면 쌍은 그대로 부적격입니다 (matcher.decide).

분류 신호 (둘뿐이며 완전하지 않음)
1. Rule.underspecified: 파서·검증기가 "원문에 구체 기준이 없음"으로 확인한 기준.
2. EXPERT_RULES: 위 플래그가 없지만 슬롯 후보표(adjudication_candidate=expert)가 임상의 판단으로 분류한 저장 규칙.
   기준 ID로 묶으므로 재파싱으로 ID가 바뀌면 다시 맞춰야 합니다 (tests/test_expert_boundary.py가 후보표와 대조).
목록 밖의 새 시험에서 플래그 없이 들어온 임상 판단 기준은 막지 못합니다. 원문 표현으로 추정하는 휴리스틱은 쓰지 않습니다.

임상의 판단 완료를 나타내는 입력은 없습니다. CriterionResult.decided_by="clinician"은 타입에만 있고 만드는 코드가 없으므로,
이 경계에는 "임상의가 확인함"으로 통과시키는 인자를 두지 않습니다. 그런 계약이 생길 때 여기에 추가합니다.
"""

from __future__ import annotations

from typing import Iterable

from .schemas import CriterionAssessment, ParsedTrial, Rule, TrialMatch

FLAG = "clinician_judgment_required"
REASON_UNDERSPECIFIED = "underspecified"
REASON_REGISTERED = "registered"

# "<시험>:<기준>" -> 슬롯 후보표의 슬롯 (data/nsclc/cohort/slot_candidates.csv, underspecified=False인 저장 규칙만).
# 임상 검수로 바뀌면 여기만 고칩니다.
EXPERT_RULES: dict[str, str] = {
    "NCT07098338:I5": "judge.life_expectancy",
    "NCT07027514:I7": "judge.life_expectancy",
    "NCT06928389:I5": "judge.life_expectancy",
    "NCT07098338:E10": "cm.cardiac",
    "NCT07680790:I2": "dx.disease_setting",
}


def clinician_reason(trial_id: str, rule: Rule) -> str | None:
    """이 기준이 임상의 판단을 요구하면 사유, 아니면 None."""
    if getattr(rule, "underspecified", False):
        return REASON_UNDERSPECIFIED
    return REASON_REGISTERED if f"{trial_id}:{getattr(rule, 'rule_id', '')}" in EXPERT_RULES else None


def enforce(trial_id: str, rule: Rule, a: CriterionAssessment) -> CriterionAssessment:
    """임상의 판단 기준의 판정을 미확정으로 되돌림. 대상이 아니면 같은 객체를 돌려줌 (여러 번 적용해도 같음).

    underspecified=True로 표시하므로 기존 경로가 그대로 의사 확인으로 처리합니다
    (matcher.needs_physician_review -> gap physician_review -> EXPERT_REVIEW, 추천의 의사 확인 메모).
    판정기가 낸 값은 llm_judgment와 reasoning에 감사용으로 남습니다.
    """
    if clinician_reason(trial_id, rule) is None:
        return a
    if a.passes is None and a.underspecified and not a.needs_verification:
        return a
    update: dict = {"underspecified": True, "needs_verification": False, "self_reported_holds": None,
                    "self_reported_applicable": True}  # 기록 확인으로 끝나는 기준이 아님
    if FLAG not in a.validation_flags:
        update["validation_flags"] = [*a.validation_flags, FLAG]
    if a.passes is not None or a.holds != "unknown" or not a.applicable:
        said = "해당 없음" if not a.applicable else a.holds
        update.update(holds="unknown", passes=None, applicable=True, confidence="low", evidence_source="none",
                      evidence_directness="none",
                      reasoning=f"[코드 검증: 임상의 판단 기준 - '{said}' 판정 보류] {a.reasoning}")
    if not a.missing_info or a.needs_verification:
        update["missing_info"] = "담당 의사가 판단해야 하는 기준"
    return a.model_copy(update=update)


def enforce_all(parsed: ParsedTrial, assessments: Iterable[CriterionAssessment]) -> list[CriterionAssessment]:
    """시험 하나의 판정 목록에 경계를 적용 (matcher.match_trial이 판정을 집계하기 직전에 부름)."""
    rules = {r.rule_id: r for r in parsed.rules}
    return [enforce(parsed.trial_id, rules[a.rule_id], a) if a.rule_id in rules else a for a in assessments]


def violations(match: TrialMatch, parsed: ParsedTrial) -> list[str]:
    """임상의 판단 기준을 확정한 채로 남은 판정의 기준 ID (빈 목록 = 불변식 성립). 다른 코호트 기준도 포함."""
    rules = {r.rule_id: r for r in parsed.rules}
    return [a.rule_id for a in match.assessments + match.other_cohort_assessments
            if a.rule_id in rules and a.passes is not None and clinician_reason(parsed.trial_id, rules[a.rule_id])]


__all__ = ["EXPERT_RULES", "FLAG", "clinician_reason", "enforce", "enforce_all", "violations"]
