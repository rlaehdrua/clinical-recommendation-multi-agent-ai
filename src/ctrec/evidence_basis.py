"""판정 근거의 종류 구분과 미검증 기록 판별. 어떤 실행 경로에도 연결되어 있지 않습니다 (팀 정책 결정 대기).

네 가지를 섞지 않습니다.
- patient_report    : 환자·보호자가 말한 값. 진료 기록이 필요한 기준의 확정 근거가 아님
- declared_record   : 입력 쪽이 clinical_record라고 밝혔을 뿐인 값. 문서를 본 사람도 서버도 없음
- document_verified : 문서 확인 이력이 있고 그 이력에 문서 참조가 있는 값
- clinician         : 임상의 판단이 필요한 기준. 사실의 출처로 해결되지 않음 (자동 확정 없음)
값의 출처를 알 수 없으면 unknown입니다.

문서 참조는 지금 자유 문자열이라 문서가 실제로 있는지 보여 주지 못합니다. document_backed는 "이력과 참조가 있다"까지만
뜻하며 서버 검증이 아닙니다. 이 모듈은 어떤 값도 검증됨으로 바꾸지 않습니다.
"""

from __future__ import annotations

from typing import Literal

from .agents import matcher
from .fact_store import FactStore, PatientFact, document_backed
from .fact_view import FactEvidenceView, supporting_facts
from .schemas import CriterionAssessment, ParsedTrial, TrialMatch

EvidenceBasis = Literal["patient_report", "declared_record", "document_verified", "clinician", "unknown"]
HELD_FLAG = "declared_record_unverified"


def fact_basis(f: PatientFact) -> EvidenceBasis:
    if document_backed(f):
        return "document_verified"
    if f.source_type == "clinical_record":
        return "declared_record"
    return "patient_report" if f.source_type == "self_report" else "unknown"


def criterion_basis(a: CriterionAssessment, facts: list[PatientFact]) -> EvidenceBasis:
    """기준 판정 하나의 근거 종류. 사실 중 가장 강한 것을 씁니다 (fact_view.gate와 같은 기준)."""
    if matcher.needs_physician_review(a):
        return "clinician"
    found = {fact_basis(f) for f in facts}
    return next((b for b in ("document_verified", "declared_record", "patient_report") if b in found), "unknown")


def facts_for(a: CriterionAssessment, rule, store: FactStore, view: FactEvidenceView) -> list[PatientFact]:
    refs, _, _ = supporting_facts(a, rule, view)
    return [store.get(r.fact_id) for r in refs]


def unverified_record_passes(match: TrialMatch, parsed: ParsedTrial, store: FactStore, patient_id: str) -> list[str]:
    """문서 확인 없이 통과로 판정된 진료 기록 필요 기준의 ID (적용 경로 기준). 판정을 바꾸지 않음."""
    view = FactEvidenceView(store, patient_id)
    rules = {r.rule_id: r for r in parsed.rules}
    return [a.rule_id for a in match.assessments
            if a.passes is True and a.required_evidence == "clinical_record" and a.rule_id in rules
            and criterion_basis(a, facts_for(a, rules[a.rule_id], store, view)) != "document_verified"]


def hold(a: CriterionAssessment) -> CriterionAssessment:
    """통과 판정을 UNVERIFIED로 되돌린 사본. 기록이 가리킨 값은 보존하고 문서 확인을 요청하는 기존 경로로 보냄."""
    return a.model_copy(update={
        "holds": "unknown", "passes": None, "applicable": True, "confidence": "low", "needs_verification": True,
        "self_reported_holds": a.holds, "self_reported_applicable": a.applicable, "validation_flags": [*a.validation_flags, HELD_FLAG],
        "missing_info": "진료 기록(검사 결과지, 병리·유전자 보고서 등)으로 확인 필요",
        "reasoning": f"[UNVERIFIED: 진료 기록으로 선언됐으나 문서 확인 이력이 없어 확정하지 않음] {a.reasoning}"})


def mark(a: CriterionAssessment) -> CriterionAssessment:
    """판정은 그대로 두고 문서 확인이 없다는 표시만 남긴 사본."""
    return a.model_copy(update={"validation_flags": [*a.validation_flags, HELD_FLAG]})


def replace_unverified(match: TrialMatch, parsed: ParsedTrial, store: FactStore, patient_id: str, change=hold) -> TrialMatch:
    """적용 경로의 미검증 통과 기준에 change를 적용해 시험 판정을 다시 계산. 승인된 호출부가 생기기 전에는 쓰이지 않음."""
    from .qa_guard import recompose
    ids = set(unverified_record_passes(match, parsed, store, patient_id))
    return recompose(match, parsed, {a.rule_id: change(a) for a in match.assessments if a.rule_id in ids})


__all__ = ["EvidenceBasis", "HELD_FLAG", "criterion_basis", "document_backed", "fact_basis", "facts_for", "hold", "mark", "replace_unverified",
           "unverified_record_passes"]
