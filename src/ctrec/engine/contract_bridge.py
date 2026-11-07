"""midterm 계약 계층(명제·질문 계약·결정적 평가기) -> 엔진 계약 변환 (0.2). 판정을 만들거나 바꾸지 않습니다.

재사용하는 기존 모듈 (다시 구현하지 않음)
- ctrec.evaluator.evaluate / EvalResult: 승인된 Condition IR + 계약 명제 사실 -> CriterionAssessment (method="contract")
- ctrec.proposition_store.PropFact / PropositionLedger: 계약 답변에서 나온 명제 사실 ("prop:<명제 키>")
- ctrec.contract_session.ContractSession: 위 둘을 실행 경로에 연결 (CTREC_SESSION=contract일 때만, 기본은 Session)
- ctrec.question_contract.ContractRegistry.deciding: 미확인 명제를 정할 수 있는 활성 계약

이 모듈이 하는 일
1. 평가기가 낸 판정(또는 ContractSession이 경계·병합까지 적용한 최종 판정)을 contracts.CriterionResult로 옮김.
   판정 주체는 decided_by="contract_evaluator" (LLM 아님). 기존 decision_state 기록은 method="contract"를 engine="llm"으로
   남기므로(decision_state._record), 출처 구분은 여기서 평가기 출처(provenance)를 직접 읽어서만 합니다.
2. 근거 사실마다 EvidenceRef(명제 키·출처·검증 상태·계약 ID/버전)를 보존. 미검증(자기 보고) 명제는 검증된 임상 근거로
   올리지 않음 — 진료 기록이 필요한 기준은 document_verified 사실로만 evidence_satisfied=True.
3. CohortState.from_decision_states(criterion_overrides=...)에 넣을 출처 보충표를 만듦. 판정 값이 기존 상태와 다르면
   cohort_state가 거부합니다(OverrideError).

미결정 (추측해서 구현하지 않음)
- 기준 성격 분류(adjudication rule / fact_llm / expert)는 M12의 RuleMeta.adjudication이 아직 없어 잠정 규칙
  `provisional_adjudication`(expert_boundary 분류 -> expert, structured_evaluable -> rule, 그 밖 -> fact_llm)을 씁니다.
  호출자가 adjudication을 직접 넘기면 그 값을 씁니다. expert 기준을 평가기가 확정했다면 변환하지 않고 BridgeError.
"""

from __future__ import annotations

from typing import Iterable

from .. import expert_boundary
from .contracts import ALIAS_PATTERN, CriterionResult, EvidenceRef, PairKey

CONTRACT_EVALUATOR = "contract_evaluator"
_CONFLICT_FLAGS = frozenset({"conflicting_contract_answers", "contract_vs_record_conflict"})


class BridgeError(ValueError):
    pass


def provisional_adjudication(rule, trial_id: str = "") -> str:
    """잠정 기준 성격 분류 (M12 RuleMeta.adjudication 확정 전). 판정 규칙이 아니라 '누가 확정할 수 있는가'의 분류."""
    if expert_boundary.clinician_reason(trial_id, rule):
        return "expert"
    return "rule" if getattr(rule, "structured_evaluable", False) else "fact_llm"


def evidence_ref(fact) -> EvidenceRef:
    """PropFact -> EvidenceRef. 명제 키는 propositions.Proposition.key(정규화 키) 그대로."""
    source = fact.source if fact.source in ("clinical_record", "self_report") else "unknown"
    if source == "clinical_record" and fact.verification_status != "document_verified":
        source = "self_report"  # proposition_store 원칙: 문서 검증 전에는 clinical_record가 아님
    return EvidenceRef(fact_id=fact.fact_id, field="prop:" + fact.proposition.key, source_type=source,
                       verification_status=fact.verification_status, contract_id=fact.contract_id,
                       contract_version=fact.contract_version,
                       document_reference=getattr(fact, "document_reference", None))


def _state(a) -> str:
    return "MET" if a.passes is True else "NOT_MET" if a.passes is False else "UNKNOWN"


def _gap(a) -> str:
    if a.passes is not None:
        return "none"
    if set(a.validation_flags) & _CONFLICT_FLAGS:
        return "conflict"
    if a.needs_verification:
        return "unverified"
    if a.underspecified:
        return "physician_review"
    return "missing"


def criterion_result_from_assessment(alias: str, trial_id: str, assessment, provenance: dict,
                                     facts: Iterable, *, rule=None, adjudication: str | None = None
                                     ) -> CriterionResult:
    """평가기 판정(method="contract") 하나 -> CriterionResult. assessment는 최종 판정(병합·경계 적용 후)을 넘김.

    provenance: evaluator.EvalResult.provenance (ContractSession.contract_provenance[key]).
    facts: 그 세션의 PropFact 목록 (정정된 사실 포함, include_superseded=True 권장).
    """
    import re
    if not re.match(ALIAS_PATTERN, alias):
        raise BridgeError("환자 별칭 형식이 아님")
    if getattr(assessment, "method", None) != "contract":
        raise BridgeError("평가기(method=contract) 판정이 아님")
    key = f"{trial_id}:{assessment.rule_id}"
    if provenance.get("criterion_id") != key:
        raise BridgeError("평가기 출처의 기준 ID가 다름")
    by_id = {f.fact_id: f for f in facts}
    used = list(provenance.get("fact_ids", ()))
    missing = [i for i in used if i not in by_id]
    if missing:
        raise BridgeError(f"평가기 출처의 사실 {len(missing)}개를 장부에서 찾을 수 없음")
    refs = tuple(evidence_ref(by_id[i]) for i in used)
    state = _state(assessment)
    adj = adjudication or (provisional_adjudication(rule, trial_id) if rule is not None else "rule")
    if adj == "expert" and state != "UNKNOWN":
        raise BridgeError("전문가 판단 기준을 평가기가 확정함 - 변환하지 않음 (임상의만 확정)")
    req = assessment.required_evidence if assessment.required_evidence in ("self_report_ok", "clinical_record") \
        else "clinical_record"
    satisfied = state != "UNKNOWN" and (req == "self_report_ok" or bool(refs) and all(r.verified for r in refs))
    digest = str(provenance.get("ir_digest") or "")
    try:
        return CriterionResult(
            pair=PairKey(patient_alias=alias, trial_id=trial_id), rule_id=assessment.rule_id, state=state,
            adjudication=adj, decided_by=CONTRACT_EVALUATOR if state != "UNKNOWN" else "none",
            required_evidence=req, evidence_satisfied=satisfied, evidence_fact_ids=tuple(used), evidence_refs=refs,
            inputs_used=tuple("prop:" + p for p in provenance.get("propositions", ())),
            gap=_gap(assessment),
            evaluator_version=f"{CONTRACT_EVALUATOR}:ir{provenance.get('ir_version')}:{digest[:12]}",
            criterion_version=digest or None)
    except ValueError as e:  # 계약 검증 실패 (권한·근거 규칙) -> 조용히 넘기지 않음
        raise BridgeError(f"엔진 계약 검증 실패: {e}") from e


def criterion_overrides_from_session(alias: str, session) -> dict[str, CriterionResult]:
    """ContractSession -> {"<trial>:<rule>": CriterionResult} (평가기가 판정에 관여한 기준만).

    판정에 쓰인 최종 assessment(session.matches)와 평가기 출처(session.contract_provenance), 장부 사실을 읽기만 합니다.
    ContractSession이 아니거나 평가기 판정이 없으면 빈 dict (기존 출처 그대로).
    """
    prov = getattr(session, "contract_provenance", None)
    ledger = getattr(session, "ledger", None)
    if not prov or ledger is None:
        return {}
    facts = ledger.facts(session.case.patient_id, session.session_id, include_superseded=True)
    out = {}
    for tid, m in sorted(session.matches.items()):
        parsed = session.parsed.get(tid)
        rules = {r.rule_id: r for r in parsed.rules} if parsed else {}
        for a in list(m.assessments) + list(m.other_cohort_assessments):
            key = f"{tid}:{a.rule_id}"
            if a.method != "contract" or key not in prov:
                continue
            out[key] = criterion_result_from_assessment(alias, tid, a, prov[key], facts, rule=rules.get(a.rule_id))
    return out


def contract_candidates(target_criteria: Iterable[str], provenance: dict, contracts) -> list[str]:
    """행동의 대상 기준에 남은 미확인 명제(leaf status missing)를 정할 수 있는 활성 질문 계약 ID (정렬, 중복 없음).

    ContractSession._contract_plan과 같은 기준(ContractRegistry.deciding)을 씁니다. 판정·질문 문장을 만들지 않음.
    """
    from ..propositions import Proposition
    ids = set()
    for key in target_criteria:
        p = provenance.get(key)
        if not p or p.get("outcome") != "undecided":
            continue
        for leaf in p.get("leaves", ()):
            if leaf.get("status") != "missing":
                continue
            for c in contracts.deciding(Proposition.parse(leaf["proposition"])):
                ids.add(c.contract_id)
    return sorted(ids)


__all__ = ["CONTRACT_EVALUATOR", "BridgeError", "provisional_adjudication", "evidence_ref",
           "criterion_result_from_assessment", "criterion_overrides_from_session", "contract_candidates"]
