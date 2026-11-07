"""판정 근거의 종류 구분과 진료 기록 필요 기준의 공통 게이트 (Session·시뮬레이터·fact_compare가 matcher.match_trial로 공유).

네 가지를 섞지 않습니다.
- patient_report    : 환자·보호자가 말한 값. 진료 기록이 필요한 기준의 확정 근거가 아님
- declared_record   : 입력 쪽이 clinical_record라고 밝혔을 뿐인 값. 문서를 본 사람도 서버도 없음
- document_verified : 현재 문서 권한(document_authority)이 인정한 증명이 이력에 있는 값
- clinical_record 선언, document_verified 상태 문자열, 문서 참조 문자열은 위 셋째가 아닙니다.
- clinician         : 임상의 판단이 필요한 기준. 사실의 출처로 해결되지 않음 (자동 확정 없음)
값의 출처를 알 수 없으면 unknown입니다.

게이트 (enforce): 진료 기록이 필요한 기준의 통과(passes=True)는 서버 문서 확인이 있어야 확정합니다.
불충족(passes=False)은 건드리지 않습니다: 선언된 기록의 불충족은 잠정 부적격이고, 확정 부적격은 decision_state가 따로 문서 확인을
요구합니다. 운영에는 문서 권한이 없으므로 지금 이 기준들은 모두 보류됩니다. 이 모듈은 어떤 값도 검증됨으로 바꾸지 않습니다.
"""

from __future__ import annotations

from typing import Literal

from . import config
from .agents import matcher
from .fact_store import FactStore, PatientFact, document_backed
from .fact_view import FactEvidenceView, gate, supporting_facts
from .schemas import CriterionAssessment, ParsedTrial, Rule, TrialMatch

EvidenceBasis = Literal["patient_report", "declared_record", "document_verified", "clinician", "unknown"]
HELD_FLAG = matcher.DECLARED_RECORD_HELD


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
        "reasoning": f"[UNVERIFIED: 진료 기록으로 선언됐으나 서버 문서 확인이 없어 확정하지 않음] {a.reasoning}"})


def view_check(view: FactEvidenceView, parsed: ParsedTrial, reference_date: str | None):
    """이 사실 보기로 document_check를 만듦. fact_view.gate와 같은 판단 (근거 연결·상충·표적·기간 포함, 문서 확인 필요)."""
    ref = reference_date or config.reference_date()

    def check(a: CriterionAssessment, rule: Rule) -> bool:
        return gate(a, rule, matcher._meta(parsed, rule), view, ref)[1] is None
    return check


def document_check(store: FactStore, patient_id: str, parsed: ParsedTrial, reference_date: str | None):
    return view_check(FactEvidenceView(store, patient_id), parsed, reference_date)


def enforce(a: CriterionAssessment, rule: Rule, check=None) -> CriterionAssessment:
    """matcher.match_trial의 공통 게이트. check가 없으면 문서 확인이 없는 것으로 봄 (호출자가 빠뜨려도 확정되지 않음)."""
    if a.passes is not True or a.required_evidence != "clinical_record":
        return a
    return a if check is not None and check(a, rule) else hold(a)


__all__ = ["EvidenceBasis", "HELD_FLAG", "criterion_basis", "document_backed", "document_check", "enforce", "fact_basis", "facts_for", "hold",
           "unverified_record_passes", "view_check"]
