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

해제 (release_match, 호출자가 명시적으로 부름 - 지금은 follow-up 서비스만. matcher 게이트의 의미는 그대로): 판정기가 "근거가 환자 답변뿐"이라 보류한 진료 기록 필요 기준(matcher._apply_evidence_policy,
답변이 가리킨 값이 self_reported_holds에 남은 경우만)은, 답변 값대로 되돌린 판정이 위와 같은 문서 확인(check = fact_view.gate:
근거 연결·상충·표적·기간·문서 확인 범위)을 통과할 때만 그 판정으로 확정합니다. 출처 불명(unknown)·구조화 값 근거 문제·
상충으로 보류된 기준은 해제하지 않습니다. 현재 권한이 인정한 증명이 없으면(운영 기본값) 아무것도 해제되지 않습니다.
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
RELEASED_FLAG = "self_report_document_verified"  # 자기 보고 보류를 서버 문서 확인으로 해제한 기준


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


def release(a: CriterionAssessment, rule: Rule, check=None) -> CriterionAssessment:
    """자기 보고라 보류된 판정 -> 답변 값의 판정이 문서 확인(check)을 통과하면 그 판정. 아니면 그대로 (같은 객체)."""
    if (check is None or a.passes is not None or not a.needs_verification or a.required_evidence != "clinical_record"
            or a.evidence_source != "self_report" or a.self_reported_holds not in ("yes", "no")):
        return a
    from .tools import rule_engine
    holds, applicable = a.self_reported_holds, a.self_reported_applicable
    cand = a.model_copy(update={
        "holds": holds, "applicable": applicable, "passes": True if not applicable else rule_engine.passes(a.kind, holds),
        "needs_verification": False, "self_reported_holds": None, "self_reported_applicable": True,
        "evidence_source": "record", "missing_info": None, "validation_flags": [*a.validation_flags, RELEASED_FLAG],
        "reasoning": f"[문서 확인: 답변 값이 서버가 확인한 문서의 사실과 이어짐 - 판정 확정] {a.reasoning}"})
    return cand if check(cand, rule) else a


def release_match(match: TrialMatch, parsed: ParsedTrial, check) -> TrialMatch:
    """시험 판정 하나에 release를 적용하고 판정을 다시 집계 (matcher.match_trial과 같은 집계). 바뀐 것이 없으면 같은 객체."""
    if match.eligibility == "NOT_EVALUATED" or check is None:
        return match
    rules = {r.rule_id: r for r in parsed.rules}
    before = match.assessments + match.other_cohort_assessments
    after = [release(a, rules[a.rule_id], check) if a.rule_id in rules else a for a in before]
    released = sum(x is not y for x, y in zip(after, before))
    if not released:
        return match
    return reaggregate(match, parsed, after, f"문서 확인으로 자기 보고 보류 해제 {released}")


def reaggregate(match: TrialMatch, parsed: ParsedTrial, after: list[CriterionAssessment], note: str) -> TrialMatch:
    """바뀐 기준 판정 목록으로 시험 판정을 다시 집계 (matcher.match_trial과 같은 코호트 선택·판정 규칙)."""
    assessments, cohort, cohort_results = matcher._select_cohort(after, parsed.cohorts)
    other = [a for a in after if a not in assessments]
    no_evidence = matcher.no_evidence_code(assessments)
    eligibility = "NOT_EVALUATED" if no_evidence else matcher.decide(assessments)
    n_pass = sum(a.passes is True for a in assessments)
    n_fail = sum(a.passes is False for a in assessments)
    n_unknown = sum(a.passes is None for a in assessments)
    return match.model_copy(update={
        "eligibility": eligibility, "assessments": assessments, "other_cohort_assessments": other,
        "matched_cohort": cohort, "cohort_results": cohort_results, "n_pass": n_pass, "n_fail": n_fail,
        "n_unknown": n_unknown, "evaluation_error": no_evidence,
        "summary": f"{eligibility}: 통과 {n_pass} / 불충족 {n_fail} / 판정불가 {n_unknown} ({note})"})


def enforce(a: CriterionAssessment, rule: Rule, check=None) -> CriterionAssessment:
    """matcher.match_trial의 공통 게이트. check가 없으면 문서 확인이 없는 것으로 봄 (호출자가 빠뜨려도 확정되지 않음)."""
    if a.passes is not True or a.required_evidence != "clinical_record":
        return a
    return a if check is not None and check(a, rule) else hold(a)


__all__ = ["EvidenceBasis", "HELD_FLAG", "RELEASED_FLAG", "criterion_basis", "document_backed", "document_check", "enforce", "fact_basis", "facts_for", "hold",
           "reaggregate", "release", "release_match", "unverified_record_passes", "view_check"]
