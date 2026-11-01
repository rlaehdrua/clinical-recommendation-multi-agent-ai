"""WP0 공통 계약 (PROVISIONAL): 직렬화 왕복, 필수값, 음수 비용 차단, 상태 전이, 판정 권한 분리."""

from __future__ import annotations

import itertools

import pytest
from pydantic import ValidationError

from ctrec.engine import contracts as C

A1 = "PA-" + "a" * 16
A2 = "PA-" + "b" * 16


def _action(**kw):
    base = dict(action_id="ACT-1", action_type="REQUEST_DOCUMENT", patient_alias=A1, source_id="lab_report",
                target_trial_ids=("T1",), target_criteria=("T1:I4",), reason_code="unverified_self_report", tier=3,
                fingerprint="fp1", policy_version="p0",
                expected_cost=C.EffortCost(coordinator_minutes=10, patient_wait_minutes=1440))
    return C.Action(**{**base, **kw})


def _outcome(**kw):
    base = dict(action_id="ACT-1", status="succeeded", new_fact_ids=("F-9",),
                cost_charged=C.EffortCost(coordinator_minutes=10), charge_keys=(f"{A1}|lab_report",))
    return C.ActionOutcome(**{**base, **kw})


def _record(**kw):
    pair = C.PairKey(patient_alias=A1, trial_id="T1")
    base = dict(run_id="RUN-1", step=0, action=_action(), outcome=_outcome(), facts_added=("F-9",),
                criteria=(C.CriterionChange(criterion_key="T1:I4", before="UNKNOWN", after="MET", reevaluated=True,
                                            reason_code="fact_dependency_changed", evaluator_version="rule/1"),),
                pairs=(C.PairTransition(pair=pair, before="VERIFICATION_PENDING", after="CONFIRMED_ELIGIBLE"),),
                policy_version="p0")
    return C.DecisionRecord(**{**base, **kw})


def test_contract_marked_provisional():
    assert C.CONTRACT_STATUS == "PROVISIONAL"
    assert C.DECISION_RECORD_SCHEMA.endswith("/v2")


@pytest.mark.parametrize("model", [
    lambda: C.PairKey(patient_alias=A1, trial_id="NCT0001"),
    lambda: C.CriterionResult(pair=C.PairKey(patient_alias=A1, trial_id="T1"), rule_id="I1", state="MET",
                              adjudication="rule", decided_by="rule_engine", inputs_used=("age",),
                              evaluator_version="rule/1", evidence_fact_ids=("F1",)),
    _action, _outcome, _record,
    lambda: C.InformationSource(source_id="lab_report", kind="document", provides_fields=("lab:*",),
                                request_cost=C.EffortCost(coordinator_minutes=8), can_verify=True,
                                alternatives=("patient_interview",)),
    lambda: C.EffortBudget(coordinator_minutes=120, per_patient_floor_minutes=5),
    lambda: C.ExperimentSpec(experiment_id="EXP-1", policy_id="greedy", policy_version="0.1",
                             policy_config={"w": 1}, seed=7, budget=C.EffortBudget(coordinator_minutes=60),
                             data_version=C.DataVersion(frozen_store_hash="abc", cohort_hash="def")),
])
def test_roundtrip_json(model):
    m = model()
    again = type(m).model_validate_json(m.model_dump_json())
    assert again == m
    assert type(m).model_validate(m.model_dump(mode="json")) == m


def test_alias_must_be_opaque():
    for bad in ["M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1", "V-DEMO-LUNG-001-I9-single", "PA-xyz", ""]:
        with pytest.raises(ValidationError):
            C.PairKey(patient_alias=bad, trial_id="T1")


def test_required_fields():
    with pytest.raises(ValidationError):
        C.Action(action_id="A", action_type="ASK_PATIENT", patient_alias=A1)  # reason/tier/fingerprint/version 누락
    with pytest.raises(ValidationError):
        C.ActionOutcome(status="succeeded")
    with pytest.raises(ValidationError):
        C.DecisionRecord(run_id="R", step=0, policy_version="p")


@pytest.mark.parametrize("field", ["coordinator_minutes", "clinician_minutes", "llm_cost_usd", "llm_calls",
                                   "patient_wait_minutes"])
def test_negative_costs_rejected(field):
    with pytest.raises(ValidationError):
        C.EffortCost(**{field: -1})
    with pytest.raises(ValidationError):
        C.InformationSource(source_id="x", kind="document", per_item_minutes=-0.5)
    with pytest.raises(ValidationError):
        C.EffortBudget(coordinator_minutes=-1)


def test_wait_time_is_not_active_time():
    total = C.EffortCost(coordinator_minutes=3, patient_wait_minutes=1440) + C.EffortCost(coordinator_minutes=5,
                                                                                          patient_wait_minutes=60)
    assert total.coordinator_minutes == 8 and total.patient_wait_minutes == 1440  # 대기는 더하지 않음(동시 대기)


def test_budget_floor_consistency():
    with pytest.raises(ValidationError):
        C.EffortBudget(coordinator_minutes=10, per_patient_floor_minutes=20)
    with pytest.raises(ValidationError):
        C.EffortBudget(coordinator_minutes=100, per_patient_floor_minutes=20, per_patient_cap_minutes=10)


def test_models_are_frozen():
    a = _action()
    with pytest.raises(ValidationError):
        a.tier = 0  # type: ignore[misc]
    assert isinstance(a.target_criteria, tuple)  # 시퀀스도 변경 불가


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        _action(success_probability=0.8)  # 근거 없는 확률 필드는 계약에 없음


def test_expert_criterion_cannot_be_auto_decided():
    pair = C.PairKey(patient_alias=A1, trial_id="T1")
    for who in ("rule_engine", "llm"):
        with pytest.raises(ValidationError):
            C.CriterionResult(pair=pair, rule_id="E1", state="NOT_MET", adjudication="expert", decided_by=who,
                              evaluator_version="v")
    ok = C.CriterionResult(pair=pair, rule_id="E1", state="NOT_MET", adjudication="expert", decided_by="clinician",
                           evaluator_version="v")
    assert ok.criterion_key == "T1:E1"
    with pytest.raises(ValidationError):  # 판정 주체 없는 확정
        C.CriterionResult(pair=pair, rule_id="I1", state="MET", adjudication="rule", evaluator_version="v")
    with pytest.raises(ValidationError):  # 규칙 기준을 LLM이 확정
        C.CriterionResult(pair=pair, rule_id="I1", state="MET", adjudication="rule", decided_by="llm",
                          evaluator_version="v")


def test_failed_outcome_cannot_add_facts():
    for status in ("failed", "no_response", "not_executed"):
        with pytest.raises(ValidationError):
            _outcome(status=status, new_fact_ids=("F-1",), error_code="timeout")
    with pytest.raises(ValidationError):
        _outcome(status="failed", new_fact_ids=())  # error_code 필요
    with pytest.raises(ValidationError):
        _outcome(status="partial", new_fact_ids=())
    with pytest.raises(ValidationError):
        _outcome(status="not_executed", new_fact_ids=(), cost_charged=C.EffortCost(coordinator_minutes=1))
    failed = _outcome(status="no_response", new_fact_ids=(), error_code="patient_no_response")
    assert failed.cost_charged.coordinator_minutes == 10  # 실패해도 들인 시간은 청구


def test_action_criteria_must_match_trials():
    with pytest.raises(ValidationError):
        _action(target_criteria=("T2:I1",))
    with pytest.raises(ValidationError):
        _action(target_criteria=("no-colon",))
    with pytest.raises(ValidationError):
        _action(reason_code="Free text with 환자 원문")


def test_all_transitions_against_table():
    for a, b in itertools.product(C.PairStatus, C.PairStatus):
        allowed = b in C.ALLOWED_TRANSITIONS[a]
        if allowed:
            C.check_transition(a, b, authority="adjudication")
        else:
            with pytest.raises(C.TransitionError):
                C.check_transition(a, b, authority="adjudication")
    # 확정 -> 반대 확정은 바로 가지 않음
    with pytest.raises(C.TransitionError):
        C.check_transition("CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE", authority="adjudication")


def test_policy_cannot_change_clinical_status():
    for a, b in itertools.product(C.PairStatus, C.PairStatus):
        if a == b:
            continue
        if (a, b) in C.POLICY_TRANSITIONS:
            C.check_transition(a, b, authority="policy")
        else:
            with pytest.raises(C.TransitionError):
                C.check_transition(a, b, authority="policy")
    assert all(b == C.PairStatus.BLOCKED_FAILURE for _, b in C.POLICY_TRANSITIONS)


@pytest.mark.parametrize("elig,gaps,mismatch,exhausted,expected", [
    ("NOT_EVALUATED", {"missing"}, None, False, "NOT_EVALUATED"),
    ("ELIGIBLE", set(), None, False, "CONFIRMED_ELIGIBLE"),
    ("ELIGIBLE", {"unverified"}, None, False, "VERIFICATION_PENDING"),
    ("ELIGIBLE", {"conflict"}, None, False, "VERIFICATION_PENDING"),
    ("INELIGIBLE", set(), "confirmed", False, "CONFIRMED_INELIGIBLE"),
    ("INELIGIBLE", set(), "provisional", False, "VERIFICATION_PENDING"),
    ("UNCERTAIN", {"missing", "unverified", "physician_review"}, None, False, "INFO_MISSING"),
    ("UNCERTAIN", {"stale"}, None, False, "INFO_MISSING"),
    ("UNCERTAIN", {"unverified", "physician_review"}, None, False, "VERIFICATION_PENDING"),
    ("UNCERTAIN", {"conflict"}, None, False, "VERIFICATION_PENDING"),
    ("UNCERTAIN", {"physician_review"}, None, False, "EXPERT_REVIEW"),
    ("UNCERTAIN", {"missing"}, None, True, "BLOCKED_FAILURE"),
    ("UNCERTAIN", set(), None, False, "INFO_MISSING"),
])
def test_derive_pair_status_table(elig, gaps, mismatch, exhausted, expected):
    assert C.derive_pair_status(elig, gaps, mismatch, exhausted) == C.PairStatus(expected)


def test_derive_pair_status_total_over_gap_combinations():
    gaps = ["missing", "unverified", "physician_review", "conflict", "stale"]
    for elig in ("ELIGIBLE", "INELIGIBLE", "UNCERTAIN", "NOT_EVALUATED"):
        for r in range(len(gaps) + 1):
            for combo in itertools.combinations(gaps, r):
                for mm in (None, "none", "provisional", "confirmed"):
                    s = C.derive_pair_status(elig, set(combo), mm)
                    assert isinstance(s, C.PairStatus)
                    if elig == "UNCERTAIN":
                        assert s not in C.CONFIRMED  # 보류는 어떤 gap 조합으로도 확정이 되지 않음
    with pytest.raises(ValueError):
        C.derive_pair_status("MAYBE")


def test_record_links_and_validation():
    r = _record()
    assert r.outcome.action_id == r.action.action_id
    with pytest.raises(ValidationError):
        _record(outcome=_outcome(action_id="ACT-OTHER"))
    with pytest.raises(ValidationError):
        _record(facts_added=("F-NOT-IN-OUTCOME",))
    with pytest.raises(ValidationError):
        _record(outcome=None)  # 결과 없이 사실·재평가
    with pytest.raises(ValidationError):  # 재사용 기준의 판정 변화
        _record(criteria=(C.CriterionChange(criterion_key="T1:I4", before="UNKNOWN", after="MET", reevaluated=False,
                                            reason_code="reused"),))
    with pytest.raises(ValidationError):  # 다른 환자 쌍
        _record(pairs=(C.PairTransition(pair=C.PairKey(patient_alias=A2, trial_id="T1"),
                                        before="INFO_MISSING", after="INFO_MISSING"),))
    with pytest.raises(ValidationError):  # 정책 권한으로 판정 전이
        _record(pairs=(C.PairTransition(pair=C.PairKey(patient_alias=A1, trial_id="T1"), before="INFO_MISSING",
                                        after="CONFIRMED_ELIGIBLE", authority="policy"),))


def test_experiment_spec_hash_stable():
    def spec(eid, abl):
        return C.ExperimentSpec(experiment_id=eid, policy_id="greedy", policy_version="0.1", policy_config={"b": 2, "a": 1},
                                seed=3, budget=C.EffortBudget(coordinator_minutes=60), ablations=abl,
                                data_version=C.DataVersion(frozen_store_hash="h1", cohort_hash="h2"))
    assert spec("E1", ("x", "y")).spec_hash() == spec("E2", ("y", "x")).spec_hash()
    assert spec("E1", ()).spec_hash() != spec("E1", ("x",)).spec_hash()
    assert spec("E1", ()).config_hash == C.canonical_hash({"a": 1, "b": 2})


def test_legacy_action_mapping_complete():
    from typing import get_args
    from ctrec.decision_state import ActionType as Legacy
    assert set(C.LEGACY_ACTION_TYPES) == set(get_args(Legacy))
    assert set(C.LEGACY_ACTION_TYPES.values()) <= set(get_args(C.ActionType))


def test_policy_engine_protocol_runtime_check():
    class P:
        policy_id, version = "x", "1"

        def propose(self, state):
            return []

        def select(self, candidates, budget, ledger=None):
            return C.Plan(policy_id="x", policy_version="1")
    assert isinstance(P(), C.PolicyEngine)
