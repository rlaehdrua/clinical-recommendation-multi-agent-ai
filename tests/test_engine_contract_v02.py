"""공통 계약 0.2 후보 (PROVISIONAL): 쌍 상태 안전 경계, 남은 작업 보존, 확정 역전 추적, 질문 목적 보호,
하위 호환, 정보원 카탈로그 해시, 명제 근거 보존, midterm 계약 계층(ContractSession·평가기) 연결.

합성 데이터와 오프라인 대역만 씁니다(유료 API·실제 LLM 없음)."""

from __future__ import annotations

import ast
import itertools
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine.audit import make_record, parse_records, reversal_issues  # noqa: E402
from ctrec.engine.bundles import DEFAULT_SOURCES, SourceCatalog  # noqa: E402
from ctrec.engine.cohort_state import CohortState, OverrideError, required_gaps  # noqa: E402
from ctrec.engine.contract_bridge import (BridgeError, criterion_overrides_from_session,  # noqa: E402
                                          criterion_result_from_assessment, evidence_ref, provisional_adjudication)
from ctrec.engine.experiment import (ExperimentSpecError, catalog_claims, require_catalog_hash,  # noqa: E402
                                     with_catalog_hash)
from ctrec.engine.policy import LegacyAdapterPolicy, plan_and_charge  # noqa: E402
from ctrec.engine.question_bridge import (RenderError, Wording, attach_question_contract, check_rendered,  # noqa: E402
                                          qa_pair, render_action)
from ctrec.eval_hygiene import AliasMap  # noqa: E402
from ctrec.proposition_store import PropFact  # noqa: E402
from ctrec.propositions import prop  # noqa: E402
from ctrec.schemas import CriterionAssessment  # noqa: E402

from test_contract_p5_session import patched  # noqa: E402,F401  (ContractSession 오프라인 대역 fixture)

A1 = "PA-" + "a" * 16
GAPS = ["missing", "unverified", "physician_review", "conflict", "stale"]
ENGINE_DIR = Path(__file__).resolve().parents[1] / "src" / "ctrec" / "engine"


def _action(**kw):
    base = dict(action_id="ACT-1", action_type="ASK_PATIENT", patient_alias=A1, source_id="patient_interview",
                target_trial_ids=("T1",), target_criteria=("T1:I4",), reason_code="missing_required_info", tier=2,
                fingerprint="fp1", policy_version="p0")
    return C.Action(**{**base, **kw})


def _pair(tid="T1"):
    return C.PairKey(patient_alias=A1, trial_id=tid)


def _rec(step, before, after, *, facts=(), reason=None, run="RUN-1"):
    act = _action(action_id=f"ACT-{step}")
    out = C.ActionOutcome(action_id=f"ACT-{step}", status="succeeded", new_fact_ids=tuple(facts))
    return C.DecisionRecord(run_id=run, step=step, action=act, outcome=out, facts_added=tuple(facts),
                            pairs=(C.PairTransition(pair=_pair(), before=before, after=after, reason_code=reason),),
                            policy_version="p0")


# ------------------------------------------------------------ 5.1 쌍 상태 안전 경계
@pytest.mark.parametrize("gap,expected", [
    ("missing", "INFO_MISSING"), ("stale", "INFO_MISSING"), ("unverified", "VERIFICATION_PENDING"),
    ("conflict", "VERIFICATION_PENDING"), ("physician_review", "EXPERT_REVIEW")])
def test_eligible_with_each_required_gap_is_not_confirmed(gap, expected):
    assert C.derive_pair_status("ELIGIBLE", {gap}) == C.PairStatus(expected)
    assert C.pending_kinds("ELIGIBLE", {gap}) == {gap}


def test_eligible_confirmed_only_without_any_gap():
    for r in range(len(GAPS) + 1):
        for combo in itertools.combinations(GAPS, r):
            for ex in (False, True):
                s = C.derive_pair_status("ELIGIBLE", set(combo), None, ex)
                assert (s == C.PairStatus.CONFIRMED_ELIGIBLE) == (not combo), (combo, ex, s)
                if combo and "physician_review" in combo and len(combo) == 1:
                    assert s == C.PairStatus.EXPERT_REVIEW  # 임상의 판단 대기는 소진과 무관
    assert C.derive_pair_status("ELIGIBLE", {"none"}) == C.PairStatus.CONFIRMED_ELIGIBLE


@pytest.mark.parametrize("mismatch,expected", [
    (None, "VERIFICATION_PENDING"), ("none", "VERIFICATION_PENDING"), ("provisional", "VERIFICATION_PENDING"),
    ("confirmed", "CONFIRMED_INELIGIBLE")])
def test_ineligible_confirmed_only_with_verified_mismatch(mismatch, expected):
    assert C.derive_pair_status("INELIGIBLE", set(), mismatch) == C.PairStatus(expected)
    kinds = C.pending_kinds("INELIGIBLE", {"missing"}, mismatch)
    assert kinds == (frozenset() if mismatch == "confirmed" else {"exclusion_unverified", "missing"})


def test_multiple_reasons_preserved_with_priority_status():
    g = {"missing", "unverified", "physician_review"}
    assert C.derive_pair_status("UNCERTAIN", g) == C.PairStatus.INFO_MISSING
    assert C.pending_kinds("UNCERTAIN", g) == g
    assert C.pending_kinds("UNCERTAIN", {"missing"}, exhausted=True) == {"missing", "sources_exhausted"}
    assert C.derive_pair_status("UNCERTAIN", {"missing"}, exhausted=True) == C.PairStatus.BLOCKED_FAILURE
    assert C.pending_kinds("ELIGIBLE", set()) == frozenset()
    with pytest.raises(ValueError):
        C.derive_pair_status("UNCERTAIN", {"guess"})


def test_closed_or_branch_gap_does_not_reopen_pair():
    crit = lambda req, gap, conflict=False: SimpleNamespace(requirement=req, gap=gap, conflict_on_decided=conflict)  # noqa
    t = SimpleNamespace(criteria=[crit("required", "none"), crit("alternative_cohort", "missing"),
                                  crit("waivable", "missing")])
    assert required_gaps(t) == frozenset()
    assert C.derive_pair_status("ELIGIBLE", required_gaps(t)) == C.PairStatus.CONFIRMED_ELIGIBLE
    t2 = SimpleNamespace(criteria=[crit("required", "none", conflict=True)])  # 확정 판정 근거 상충
    assert required_gaps(t2) == {"conflict"}
    assert C.derive_pair_status("ELIGIBLE", required_gaps(t2)) == C.PairStatus.VERIFICATION_PENDING


def test_eligible_without_any_required_criterion_is_not_confirmed():
    """기존 Matcher는 기준 목록이 비면 ELIGIBLE을 돌려줌 -> 평가한 필수 기준이 0개면 확정 근거가 없으므로 NOT_EVALUATED."""
    assert C.derive_pair_status("ELIGIBLE", set(), required_criteria=0) == C.PairStatus.NOT_EVALUATED
    assert C.pending_kinds("ELIGIBLE", set(), required_criteria=0) == frozenset()
    assert C.derive_pair_status("ELIGIBLE", set(), required_criteria=1) == C.PairStatus.CONFIRMED_ELIGIBLE
    assert C.derive_pair_status("ELIGIBLE", set()) == C.PairStatus.CONFIRMED_ELIGIBLE  # 개수를 모르는 이전 호출
    for elig, expect in (("INELIGIBLE", "VERIFICATION_PENDING"), ("UNCERTAIN", "INFO_MISSING")):
        assert C.derive_pair_status(elig, set(), required_criteria=0) == C.PairStatus(expect)  # 확정이 아닌 값은 그대로

    from ctrec.decision_state import CriterionState, DecisionState, TrialState
    crit = lambda tid, rid, req: CriterionState(trial_id=tid, rule_id=rid, kind="inclusion", category="clinical",  # noqa
                                                cohort=None, requirement=req, status="MET", info_key="age")
    st = DecisionState(session_id="S", patient_id="P", fact_version="v1", trials={
        "T0": TrialState(trial_id="T0", eligibility="ELIGIBLE", match_status=None, open=True),  # 기준 0개
        "T1": TrialState(trial_id="T1", eligibility="ELIGIBLE", match_status=None, open=True,
                         criteria=[crit("T1", "I1", "alternative_cohort"), crit("T1", "I2", "waivable")]),
        "T2": TrialState(trial_id="T2", eligibility="ELIGIBLE", match_status=None, open=True,
                         criteria=[crit("T2", "I1", "required")])})
    co = CohortState.from_decision_states({A1: st})
    status = {p.pair.trial_id: p.status for p in co.pairs()}
    assert status == {"T0": C.PairStatus.NOT_EVALUATED, "T1": C.PairStatus.NOT_EVALUATED,
                      "T2": C.PairStatus.CONFIRMED_ELIGIBLE}
    assert all(p.eligibility == "ELIGIBLE" for p in co.pairs())  # 판정 값은 그대로 복사만 함


# ------------------------------------------------------------ 5.5 확정 역전
def test_direct_confirmed_reversal_forbidden_for_every_authority():
    for a, b in (("CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE"), ("CONFIRMED_INELIGIBLE", "CONFIRMED_ELIGIBLE")):
        for auth in ("adjudication", "policy"):
            with pytest.raises(C.TransitionError):
                C.check_transition(a, b, authority=auth)


def test_record_cannot_chain_reversal_inside_one_record():
    act = _action()
    out = C.ActionOutcome(action_id="ACT-1", status="succeeded", new_fact_ids=("F-1",))
    with pytest.raises(ValidationError):
        C.DecisionRecord(run_id="R", step=0, action=act, outcome=out, policy_version="p0", pairs=(
            C.PairTransition(pair=_pair(), before="CONFIRMED_ELIGIBLE", after="VERIFICATION_PENDING"),
            C.PairTransition(pair=_pair(), before="VERIFICATION_PENDING", after="CONFIRMED_INELIGIBLE")))


def test_reversal_requires_reason_and_new_evidence():
    good = [_rec(0, "INFO_MISSING", "CONFIRMED_ELIGIBLE", facts=("F-1",)),
            _rec(1, "CONFIRMED_ELIGIBLE", "VERIFICATION_PENDING", facts=("F-2",),
                 reason=C.REASON_CONTRADICTING_EVIDENCE),
            _rec(2, "VERIFICATION_PENDING", "CONFIRMED_INELIGIBLE", facts=("F-3",),
                 reason=C.REASON_EVIDENCE_REVERIFIED)]
    assert reversal_issues(good) == []
    bad = [good[0], _rec(1, "CONFIRMED_ELIGIBLE", "VERIFICATION_PENDING", facts=("F-2",)),
           _rec(2, "VERIFICATION_PENDING", "CONFIRMED_INELIGIBLE")]
    assert reversal_issues(bad) == ["unexplained_confirmed_downgrade:1", "reversal_without_new_evidence:2"]
    loaded = parse_records((i, r.model_dump(mode="json")) for i, r in enumerate(bad, 1))
    assert "reversal_without_new_evidence:2" in loaded.issues and len(loaded.records) == 3


def test_make_record_carries_pair_reason_and_pending():
    out = C.ActionOutcome(action_id="ACT-1", status="succeeded", new_fact_ids=("F-1",))
    r = make_record(run_id="R", step=0, action=_action(), outcome=out, pair_before={"T1": "CONFIRMED_ELIGIBLE"},
                    pair_after={"T1": "VERIFICATION_PENDING"}, pair_reasons={"T1": C.REASON_CONTRADICTING_EVIDENCE},
                    pair_pending={"T1": {"conflict"}}, policy_version="p0")
    t = r.pairs[0]
    assert (t.reason_code, t.pending_kinds) == ("contradicting_evidence", ("conflict",))
    with pytest.raises(ValidationError):  # 확정 상태에 남은 작업
        C.PairTransition(pair=_pair(), before="INFO_MISSING", after="CONFIRMED_ELIGIBLE", pending_kinds=("missing",))


# ------------------------------------------------------------ 5.4 질문 목적
def test_policy_sets_physician_reference_only_for_underspecified_targets():
    pr = LegacyAdapterPolicy.purpose_of
    gaps = {"T1:E1": "physician_review", "T1:I2": "missing"}
    assert pr("CLINICIAN_REVIEW", ("T1:E1",), gaps) == "physician_reference"
    assert pr("ASK_PATIENT", ("T1:E1", "T1:I2"), gaps) == "eligibility"
    assert pr("REQUEST_DOCUMENT", ("T1:E1",), gaps) == "eligibility"
    assert pr("ASK_PATIENT", (), gaps) == "eligibility"


def test_action_purpose_and_contract_validation():
    with pytest.raises(ValidationError):
        _action(action_type="REQUEST_DOCUMENT", purpose="physician_reference")
    with pytest.raises(ValidationError):
        _action(purpose="physician_reference", question_contract_id="QC-SURGERY-LAST")
    with pytest.raises(ValidationError):
        _action(action_type="REQUEST_DOCUMENT", question_contract_id="QC-SURGERY-LAST")
    with pytest.raises(ValidationError):
        _action(question_contract_id="not-a-contract")
    a = _action(purpose="physician_reference")
    with pytest.raises(ValidationError):  # frozen: 렌더러가 목적을 바꿀 수 없음
        a.purpose = "eligibility"  # type: ignore[misc]


def test_physician_reference_answer_never_becomes_eligibility_evidence():
    from ctrec.evidence_directness import eligibility_answers
    a = _action(purpose="physician_reference", target_criteria=("T1:E1",))
    q = render_action(a, "Q1", wording=lambda act: Wording(question="합성 질문", answer_type="free_text"))
    assert q.purpose == "physician_reference"
    p = qa_pair(a, q, round_no=1, answer="합성 답")
    assert p.purpose == "physician_reference" and p.contract_id is None
    assert eligibility_answers("T1:E1", [p]) == []
    e = _action(target_criteria=("T1:E1",))
    pe = qa_pair(e, render_action(e, "Q2", wording=lambda act: Wording(question="합성 질문 2")), round_no=1,
                 answer="예")
    assert eligibility_answers("T1:E1", [pe]) == [pe]


def test_renderer_cannot_change_purpose_or_targets():
    a = _action(purpose="physician_reference")
    q = render_action(a, "Q1", wording=lambda act: Wording(question="합성"))
    with pytest.raises(RenderError):
        check_rendered(a, q.model_copy(update={"purpose": "eligibility"}))
    with pytest.raises(RenderError):
        check_rendered(a, q.model_copy(update={"target": ["T1:I9"]}))
    with pytest.raises(RenderError):
        qa_pair(a, q.model_copy(update={"purpose": "eligibility"}), round_no=1, answer="x")
    with pytest.raises(RenderError):
        render_action(_action(action_type="REQUEST_DOCUMENT", source_id="lab_report"), "Q", wording=lambda x: None)
    with pytest.raises(RenderError):
        render_action(a, "Q")  # 문구 생성기 없음


class _Contracts:
    """ContractRegistry 대역: status/raw만."""

    def __init__(self, contract, status="active"):
        self.c, self.s = contract, status

    def status(self, cid):
        return (self.s, []) if cid == self.c.contract_id else ("invalid", [])

    def raw(self, cid):
        return self.c if cid == self.c.contract_id else None


def _qc():
    from contract_fixtures import QC_SURGERY
    from ctrec.question_contract import QuestionContract
    return QuestionContract.from_dict(QC_SURGERY)


def test_reviewed_question_contract_used_verbatim_and_only_when_active():
    c = _qc()
    reg = _Contracts(c)
    a = attach_question_contract(_action(), c.contract_id, reg)
    q = render_action(a, "C1", contracts=reg, wording=lambda act: Wording(question="LLM 문구"))
    assert q.question == c.question and q.purpose == "eligibility" and q.why.startswith("contract:")
    p = qa_pair(a, q, round_no=1, answer=c.labels[0])
    assert p.contract_id == c.contract_id
    with pytest.raises(RenderError):
        check_rendered(a, q.model_copy(update={"question": "바뀐 문장"}), contracts=reg)
    with pytest.raises(RenderError):
        render_action(a, "C1", contracts=_Contracts(c, status="unreviewed"))
    with pytest.raises(RenderError):
        attach_question_contract(_action(), c.contract_id, _Contracts(c, status="unreviewed"))
    with pytest.raises(ValidationError):
        attach_question_contract(_action(purpose="physician_reference"), c.contract_id, reg)


# ------------------------------------------------------------ 직렬화·하위 호환
def test_action_roundtrip_with_v02_fields():
    a = _action(purpose="eligibility", info_key="biomarker:EGFR+PD-L1", question_contract_id="QC-SURGERY-LAST")
    assert C.Action.model_validate_json(a.model_dump_json()) == a


def test_v01_payloads_still_load():
    old_action = {"action_id": "ACT-1", "action_type": "REQUEST_DOCUMENT", "patient_alias": A1,
                  "source_id": "lab_report", "target_trial_ids": ["T1"], "target_criteria": ["T1:I4"],
                  "reason_code": "unverified_self_report", "tier": 3, "fingerprint": "fp1", "policy_version": "p0"}
    a = C.Action.model_validate(old_action)
    assert (a.purpose, a.info_key, a.question_contract_id) == ("eligibility", None, None)
    rec = {"schema_version": C.DECISION_RECORD_SCHEMA, "contract_version": "dcvc.engine.contracts/0.1",
           "run_id": "R", "step": 0, "action": old_action, "policy_version": "p0",
           "pairs": [{"pair": {"patient_alias": A1, "trial_id": "T1"}, "before": "INFO_MISSING",
                      "after": "INFO_MISSING"}]}
    r = C.DecisionRecord.model_validate(rec)
    assert r.pairs[0].reason_code is None and r.pairs[0].pending_kinds == ()
    with pytest.raises(ValidationError):
        C.DecisionRecord.model_validate({**rec, "contract_version": "dcvc.engine.contracts/9.9"})
    crit = {"pair": {"patient_alias": A1, "trial_id": "T1"}, "rule_id": "I1", "state": "MET", "adjudication": "rule",
            "decided_by": "rule_engine", "evaluator_version": "rule/1"}
    assert C.CriterionResult.model_validate(crit).evidence_refs == ()
    assert C.InformationSource.model_validate({"source_id": "x_doc", "kind": "document"}).cost_basis == "assumed"
    assert C.CONTRACT_VERSION.endswith("/0.2") and C.CONTRACT_STATUS == "PROVISIONAL"


# ------------------------------------------------------------ 5.2·5.3 비용·정보원
def test_wait_max_is_parallel_only_and_axes_fixed():
    a, b = C.EffortCost(coordinator_minutes=5, patient_wait_minutes=1440), C.EffortCost(patient_wait_minutes=2880)
    assert (a + b).patient_wait_minutes == 2880 and (a + b).coordinator_minutes == 5
    assert set(C.EffortCost.model_fields) == {"coordinator_minutes", "clinician_minutes", "llm_calls", "llm_cost_usd",
                                              "patient_wait_minutes"}
    assert "병렬" in C.EffortCost.__doc__


def test_cost_basis_requires_reference_and_defaults_assumed():
    assert all(s.cost_basis == "assumed" for s in DEFAULT_SOURCES)
    with pytest.raises(ValidationError):
        C.InformationSource(source_id="lab_report", kind="document", cost_basis="literature")
    C.InformationSource(source_id="lab_report", kind="document", cost_basis="literature", cost_reference="REF-001")


def test_experiment_requires_catalog_hash_and_limits_claims():
    cat = SourceCatalog()
    spec = C.ExperimentSpec(experiment_id="E1", policy_id="greedy", policy_version="0.2", seed=1,
                            budget=C.EffortBudget(coordinator_minutes=60),
                            data_version=C.DataVersion(frozen_store_hash="h1", cohort_hash="h2"))
    with pytest.raises(ExperimentSpecError, match="missing"):
        require_catalog_hash(spec, cat)
    spec2 = with_catalog_hash(spec, cat)
    assert require_catalog_hash(spec2, cat) == cat.digest() and spec2.spec_hash() != spec.spec_hash()
    changed = SourceCatalog(tuple(s.model_copy(update={"per_item_minutes": 9.0}) if s.source_id == "lab_report"
                                  else s for s in DEFAULT_SOURCES))
    with pytest.raises(ExperimentSpecError, match="mismatch"):
        require_catalog_hash(spec2, changed)
    claims = catalog_claims(cat)
    assert claims["absolute_time_claims_allowed"] is False and "lab_report" in claims["assumed_cost_sources"]
    assert claims["allowed_claims"] == "relative_policy_comparison_only"


# ------------------------------------------------------------ 6. midterm 계약 연결
def _fact(fid="F-aaaaaaaaaaaa", status="unverified", source="self_report"):
    return PropFact(fact_id=fid, proposition=prop("surgery", "ever"), value=False, as_of="2026-10-01",
                    session_id="S1", contract_id="QC-SURGERY-LAST", contract_version=1, contract_digest12="0" * 12,
                    round=1, question_id="C1", answer_label="합성", source=source, verification_status=status,
                    recorded_at="2026-10-01T00:00:00")


def _assessment(passes=True, required="self_report_ok", **kw):
    base = dict(rule_id="E1", kind="exclusion", criterion_text="합성 기준", holds="no" if passes else "unknown",
                passes=passes, confidence="high", evidence="", reasoning="", missing_info=None, method="contract",
                required_evidence=required, evidence_directness="contract")
    return CriterionAssessment(**{**base, **kw})


PROV = {"criterion_id": "T1:E1", "ir_version": 1, "ir_digest": "d" * 64, "propositions": ["surgery/event/ever"],
        "fact_ids": ["F-aaaaaaaaaaaa"], "outcome": "decided", "leaves": []}


def test_evidence_ref_preserves_proposition_key_and_status():
    r = evidence_ref(_fact())
    assert (r.field, r.proposition_key, r.verification_status, r.source_type) == (
        "prop:surgery/event/ever", "surgery/event/ever", "unverified", "self_report")
    assert (r.contract_id, r.contract_version, r.verified) == ("QC-SURGERY-LAST", 1, False)
    v = evidence_ref(_fact(status="document_verified", source="clinical_record"))
    assert v.verified and v.source_type == "clinical_record"
    fake = evidence_ref(_fact(status="unverified", source="clinical_record"))  # 검증 전 clinical_record 표기
    assert fake.source_type == "self_report" and not fake.verified


def test_contract_evaluator_result_converted_with_provenance():
    rule = SimpleNamespace(underspecified=False, structured_evaluable=False)
    cr = criterion_result_from_assessment(A1, "T1", _assessment(), PROV, [_fact()], rule=rule)
    assert (cr.state, cr.decided_by, cr.adjudication, cr.gap) == ("MET", "contract_evaluator", "fact_llm", "none")
    assert cr.evidence_satisfied and cr.inputs_used == ("prop:surgery/event/ever",)
    assert cr.evidence_refs[0].proposition_key == "surgery/event/ever"
    assert cr.evaluator_version.startswith("contract_evaluator:ir1:") and cr.criterion_version == "d" * 64
    assert C.CriterionResult.model_validate_json(cr.model_dump_json()) == cr


def test_unverified_proposition_not_promoted_to_clinical_evidence():
    with pytest.raises(ValidationError):  # 계약 차원 차단
        C.CriterionResult(pair=_pair(), rule_id="E1", state="MET", adjudication="rule",
                          decided_by="contract_evaluator", required_evidence="clinical_record", evidence_satisfied=True,
                          evidence_fact_ids=("F-aaaaaaaaaaaa",), evidence_refs=(evidence_ref(_fact()),),
                          evaluator_version="x")
    # 평가기가 '진료 기록 필요 + 자기 보고뿐'이면 UNVERIFIED(판정 불가)로 낸 것을 그대로 옮김
    a = _assessment(passes=None, required="clinical_record", needs_verification=True, holds="unknown",
                    validation_flags=["contract_self_report_needs_record"])
    cr = criterion_result_from_assessment(A1, "T1", a, {**PROV, "outcome": "unverified"}, [_fact()])
    assert (cr.state, cr.gap, cr.evidence_satisfied, cr.decided_by) == ("UNKNOWN", "unverified", False, "none")
    ok = criterion_result_from_assessment(A1, "T1", _assessment(required="clinical_record"), PROV,
                                          [_fact(status="document_verified", source="clinical_record")])
    assert ok.evidence_satisfied
    no = criterion_result_from_assessment(A1, "T1", _assessment(required="clinical_record"), PROV, [_fact()])
    assert not no.evidence_satisfied  # 판정은 옮기되 근거 요구 충족으로 올리지 않음


def test_bridge_refuses_unsafe_or_inconsistent_inputs():
    with pytest.raises(BridgeError):  # 전문가 판단 기준을 평가기가 확정
        criterion_result_from_assessment(A1, "T1", _assessment(), PROV, [_fact()],
                                         rule=SimpleNamespace(underspecified=True))
    with pytest.raises(BridgeError):  # 출처의 사실이 장부에 없음
        criterion_result_from_assessment(A1, "T1", _assessment(), PROV, [])
    with pytest.raises(BridgeError):  # 평가기 판정이 아님
        criterion_result_from_assessment(A1, "T1", _assessment(method="llm"), PROV, [_fact()])
    with pytest.raises(BridgeError):  # 원래 환자 ID
        criterion_result_from_assessment("P1", "T1", _assessment(), PROV, [_fact()])
    with pytest.raises(BridgeError):
        criterion_result_from_assessment(A1, "T2", _assessment(), PROV, [_fact()])
    conflict = _assessment(passes=None, holds="unknown", needs_verification=True,
                           validation_flags=["contract_vs_record_conflict"])
    assert criterion_result_from_assessment(A1, "T1", conflict, PROV, [_fact()]).gap == "conflict"
    assert provisional_adjudication(SimpleNamespace(underspecified=False, structured_evaluable=True)) == "rule"


def test_contract_session_end_to_end_overrides_keep_judgement(monkeypatch, patched, tmp_path):
    """ContractSession(오프라인 대역) -> 평가기 판정 -> 엔진 CriterionResult(contract_evaluator) -> CohortState."""
    from contract_fixtures import QC_LACT, QC_PREG, QC_SURGERY, RULES
    from test_contract_p5_session import T1_RULES, _Answerer, _contract_env, _profile_from, _session, _trials

    from ctrec import pipeline
    from ctrec.contract_session import ContractSession
    from ctrec.decision_state import SessionView, build_state
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": T1_RULES, "T2": [RULES["SURG"]]})
    ans = _Answerer({QC_SURGERY["question"]: "수술을 받은 적 없음", QC_PREG["question"]: "아니요",
                     QC_LACT["question"]: "아니요"})
    s = _session(ContractSession, trials, ans, **_contract_env(tmp_path))
    s.run_fixed()
    alias = AliasMap(salt="v02-tests").alias(s.case.patient_id)
    ov = criterion_overrides_from_session(alias, s)
    assert "T1:E1" in ov and "T2:E1" in ov
    e1 = ov["T1:E1"]
    assert (e1.decided_by, e1.state) == ("contract_evaluator", "MET")
    assert {r.proposition_key for r in e1.evidence_refs} == {"surgery/event/ever"}
    assert all(r.contract_id == "QC-SURGERY-LAST" and not r.verified for r in e1.evidence_refs)
    st = build_state(SessionView.from_session(s))
    legacy = CohortState.from_decision_states({alias: st})
    co = CohortState.from_decision_states({alias: st}, criterion_overrides={alias: ov})
    assert [p.status for p in co.pairs()] == [p.status for p in legacy.pairs()]  # 출처 보충은 상태를 바꾸지 않음
    crit = {c.criterion_key: c for p in co.pairs() for c in p.criteria}
    assert crit["T1:E1"].decided_by == "contract_evaluator"
    legacy_crit = {c.criterion_key: c for p in legacy.pairs() for c in p.criteria}
    assert legacy_crit["T1:E1"].decided_by == "llm"  # 기존 기록의 출처 표기 한계 (decision_state._record)
    flipped = ov["T1:E1"].model_copy(update={"state": "NOT_MET"})
    with pytest.raises(OverrideError):
        CohortState.from_decision_states({alias: st}, criterion_overrides={alias: {"T1:E1": flipped}})
    assert criterion_overrides_from_session(alias, SimpleNamespace(contract_provenance={}, ledger=None)) == {}


# ------------------------------------------------------------ 정책은 판정을 바꾸지 못함
def test_policy_cycle_never_changes_criteria_or_verdicts(monkeypatch):
    from test_engine_policy import _budget, _cohort
    import decision_fixtures as d
    from ctrec.agents import matcher
    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)
    co = _cohort()
    snap = [(p.pair.key, p.eligibility, p.criteria) for p in co.pairs()]
    pol = LegacyAdapterPolicy()
    plan, _ = plan_and_charge(pol, co, _budget(120))
    for a in plan.selected:
        out = C.ActionOutcome(action_id=a.action_id, status="failed", error_code="no_document")
        co2, trans = pol.apply_outcome(co, a, out)
        assert all(t.authority == "policy" and t.after == C.PairStatus.BLOCKED_FAILURE for t in trans)
        assert [(p.pair.key, p.eligibility, p.criteria) for p in co2.pairs()] == snap
    assert [(p.pair.key, p.eligibility, p.criteria) for p in co.pairs()] == snap
    assert any(a.purpose == "physician_reference" for a in pol.propose(co))  # 모호 기준 환자(PD)
    for p in co.pairs():
        assert p.pending_kinds == C.pending_kinds(p.eligibility, p.gaps, p.mismatch)


@pytest.mark.parametrize("name", ["contract_bridge.py", "question_bridge.py", "experiment.py"])
def test_bridges_do_not_reach_llm_or_execution_paths(name):
    tree = ast.parse((ENGINE_DIR / name).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
        elif isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
    parts = {p for m in mods for p in m.split(".")}
    assert not parts & {"llm", "pipeline", "web", "orchestrator", "contract_session", "question_generator",
                        "question_eval", "eval_hygiene", "agents"}, parts
    assert json.dumps(sorted(parts))  # 감사용 출력
