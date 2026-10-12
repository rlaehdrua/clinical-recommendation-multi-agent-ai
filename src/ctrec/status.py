"""외부 표현(아키텍처 §49): 내부 판정을 최종 등록 확정처럼 보이지 않는 외부 상태로 바꿉니다.

내부 판정(TrialMatch.eligibility: ELIGIBLE / UNCERTAIN / INELIGIBLE / NOT_EVALUATED)은 평가·지표에 그대로 쓰고,
사용자에게 보이는 곳(추천, 보고서, 웹 화면)에는 아래 외부 상태를 씁니다.

- CRITERIA_MATCH          : 공개된 기준을 모두 충족 (ELIGIBLE)
- POTENTIAL_MATCH         : 남은 항목이 '답변상 충족, 진료 기록 확인 필요(UNVERIFIED)'뿐
- NEEDS_REVIEW            : 남은 항목이 원문에 구체 기준이 없어 의료진이 확인할 항목뿐
- INSUFFICIENT_INFORMATION: 아직 확인하지 못한 정보가 있음 (질문으로 확인할 항목, 답변상 불충족인 UNVERIFIED 포함)
- CRITERIA_MISMATCH       : 공개된 기준에 맞지 않음 (INELIGIBLE)
- NOT_EVALUATED           : 평가 실패 (부적격 아님)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schemas import CriterionAssessment, TrialMatch

LABEL_KO = {
    "CRITERIA_MATCH": "공개 기준 충족",
    "POTENTIAL_MATCH": "기록 확인 시 충족 가능",
    "NEEDS_REVIEW": "의료진 검토 필요",
    "INSUFFICIENT_INFORMATION": "추가 정보 필요",
    "CRITERIA_MISMATCH": "공개 기준 불충족",
    "NOT_EVALUATED": "평가 실패(부적격 아님)",
}

# 추천 그룹 표시 순서 (아키텍처 §50). 같은 그룹 안의 순서는 시험 간 임상적 우열을 뜻하지 않음
GROUP_ORDER = ["CRITERIA_MATCH", "POTENTIAL_MATCH", "NEEDS_REVIEW", "INSUFFICIENT_INFORMATION"]

MATCH_NOTE = ("이 결과는 공개된 임상시험 프로토콜 기준으로 미리 비교한 것입니다. "
              "실제 참여 가능 여부는 연구기관의 스크리닝과 연구자 판단으로 정해집니다.")


def _self_report_passes(a: CriterionAssessment) -> bool:
    """UNVERIFIED 항목의 답변이 기준 통과를 가리키는가."""
    from .tools.rule_engine import passes
    return (not a.self_reported_applicable) or passes(a.kind, a.self_reported_holds or "unknown") is True


def external_status(m: TrialMatch) -> str:
    from .agents.matcher import is_askable_unknown, is_blocking_unknown, needs_physician_review

    if m.eligibility == "NOT_EVALUATED":
        return "NOT_EVALUATED"
    if m.eligibility == "INELIGIBLE":
        return "CRITERIA_MISMATCH"
    if m.eligibility == "ELIGIBLE":
        return "CRITERIA_MATCH"
    blocking = [a for a in m.assessments if is_blocking_unknown(a)]
    unverified = [a for a in blocking if a.needs_verification]
    if any(is_askable_unknown(a) for a in blocking) or any(not _self_report_passes(a) for a in unverified):
        return "INSUFFICIENT_INFORMATION"
    if any(needs_physician_review(a) for a in blocking):
        return "NEEDS_REVIEW"
    if unverified:
        return "POTENTIAL_MATCH"
    return "INSUFFICIENT_INFORMATION"


def label(status: str | None) -> str:
    return LABEL_KO.get(status or "", status or "")
