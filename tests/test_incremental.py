"""부분 재평가(IncrementalEvaluator)와 전체·부분 동등성 (Phase 2, 체크포인트 3·4). 합성 데이터, API 호출 없음."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import incremental
from ctrec.agents import matcher
from ctrec.incremental import EvaluationPolicy, compare_states, evaluate_full, update
from ctrec.tools import rule_engine

from incremental_fixtures import (AGE, DX, DX_QUOTE, ECOG, EGFR, HB, OTHER, P, PDL1, POLICY, REF, FakeLLM, base_facts,
                                  snap, store, trial)

RULES = [AGE, ECOG, HB, EGFR, PDL1, DX]


@pytest.fixture(autouse=True)
def no_paid_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("유료 LLM이 호출되면 안 됨")
    monkeypatch.setattr(matcher.llm, "structured", boom)


def _run(change, rules=RULES, facts=None, assessor=None, cohorts=(), policy=POLICY, new_policy=None, **kw):
    s = store(*(facts or base_facts()))
    before = snap(s)
    parsed = {"T1": trial(rules, cohorts)}
    st0 = evaluate_full(before, parsed, policy, assessor)
    change(s)
    after = snap(s)
    r = update(st0, before, after, parsed, new_policy or policy, assessor, **kw)
    full = evaluate_full(after, parsed, new_policy or policy, assessor)
    return st0, r, full


def _crit(state, rid):
    return state.trials["T1"].criteria[rid]


def _add(field, value, **kw):
    return lambda s: s.add(P, field, value, **{"source_type": "clinical_record", **kw})


# ------------------------------------------------------------ 영향받은 기준만 재평가, 나머지 재사용
def test_only_affected_criteria_reevaluated_and_rest_reused():
    llm = FakeLLM()
    st0, r, full = _run(_add("lab:hemoglobin", 8.0, unit="g/dL", observed_at="2026-10-05"), assessor=llm)
    statuses = {rid: c.status for rid, c in r.state.trials["T1"].criteria.items()}
    # 나이·ECOG(규칙 엔진, 의존 항목 검증됨)는 재사용. LLM 기준(I4-I6)은 입력 항목을 검증할 수 없어 모든 변경에 다시 판정
    assert statuses == {"I1": "reused", "I2": "reused", "I3": "evaluated", "I4": "evaluated", "I5": "evaluated",
                        "I6": "evaluated"}
    assert r.stats["affected_criteria"] == 4 and r.stats["reused"] == 2 and r.stats["reevaluated"] == 4
    assert compare_states(r.state, full)["status"] == "equivalent"
    assert len(llm.calls) == 9  # LLM 대역 호출: 초기 전체 3 + 부분 재평가 3 + 비교용 전체 3


def test_reused_keeps_provenance_and_reevaluated_links_new_version():
    st0, r, _ = _run(_add("ecog", 2))
    reused, old = _crit(r.state, "I1"), _crit(st0, "I1")
    assert reused.provenance == old.provenance  # 원래 계산 버전·근거 그대로
    assert reused.provenance.evaluated_version == st0.snapshot_version
    assert reused.validated_version == r.state.snapshot_version != st0.snapshot_version
    new = _crit(r.state, "I2")
    assert new.status == "evaluated" and new.provenance.evaluated_version == r.state.snapshot_version
    assert len(new.provenance.fact_ids) == 2  # 상충하는 ECOG 사실 둘 다 근거로 연결


def test_ecog_value_change_between_snapshots():
    """ECOG 1 -> 2: ECOG 기준만 다시 판정, 나이는 재사용, 시험 판정은 다시 집계."""
    before_store, after_store = store(*base_facts()), store(*[f for f in base_facts() if f[0] != "ecog"],
                                                            ("ecog", 2, {}))
    parsed = {"T1": trial([AGE, ECOG, HB])}
    st0 = evaluate_full(snap(before_store), parsed, POLICY)
    r = update(st0, snap(before_store), snap(after_store), parsed, POLICY)
    assert _crit(r.state, "I2").state == "NOT_MET" and _crit(r.state, "I1").status == "reused"
    assert (st0.trials["T1"].eligibility, r.state.trials["T1"].eligibility) == ("ELIGIBLE", "INELIGIBLE")
    assert compare_states(r.state, evaluate_full(snap(after_store), parsed, POLICY))["status"] == "equivalent"


# ------------------------------------------------------------ AND/OR, 코호트 재집계
def test_cohort_reaggregation_matches_full():
    a_only = ECOG.model_copy(update={"rule_id": "I8", "cohort": "A"})
    b_only = HB.model_copy(update={"rule_id": "I9", "cohort": "B"})
    st0, r, full = _run(_add("ecog", 3), rules=[AGE, a_only, b_only], cohorts=["A", "B"])
    t0, t1 = st0.trials["T1"], r.state.trials["T1"]
    assert (t0.matched_cohort, t0.eligibility) == ("A", "ELIGIBLE")
    assert t1.cohort_results == {"A": "UNCERTAIN", "B": "ELIGIBLE"} and t1.matched_cohort == "B"
    assert compare_states(r.state, full)["status"] == "equivalent"


# ------------------------------------------------------------ 동등성 (여러 변경)
SCENARIOS = {
    "duplicate": _add("ecog", 1),
    "value_conflict": _add("ecog", 2),
    "lab_new_date": _add("lab:hemoglobin", 10.0, unit="g/dL", observed_at="2026-10-01"),
    "egfr_positive": _add("biomarker", "EGFR exon 19 deletion positive"),
    "pdl1_self_report": _add("biomarker", "PD-L1 TPS 60%", source_type="self_report"),
    "unknown_target": _add("biomarker", "driver mutation detected"),
    "diagnosis_conflict": _add("primary_diagnosis", "small cell lung cancer"),
    "age_source": _add("age", 62, unit="years", source_type="self_report"),
    "multi": lambda s: (s.add(P, "ecog", 0, source_type="self_report"),
                        s.add(P, "biomarker", "EGFR negative", source_type="clinical_record")),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_full_vs_incremental_equivalent(name):
    _, r, full = _run(SCENARIOS[name], assessor=FakeLLM())
    eq = compare_states(r.state, full)
    assert eq["status"] == "equivalent", eq
    assert r.state.trials["T1"].model_dump(exclude={"criteria"}) == full.trials["T1"].model_dump(exclude={"criteria"})


def test_duplicate_reuses_everything():
    _, r, _ = _run(SCENARIOS["duplicate"], assessor=FakeLLM())
    assert r.changes.changed == {} and r.stats["reused"] == len(RULES) and r.stats["reevaluated"] == 0
    assert r.stats["reaggregated_trials"] == 0


def test_strict_document_policy_equivalent():
    pol = EvaluationPolicy(reference_date=REF, require_document_verification=True)
    _, r, full = _run(SCENARIOS["egfr_positive"], assessor=FakeLLM(), policy=pol)
    assert compare_states(r.state, full)["status"] == "equivalent"


# ------------------------------------------------------------ LLM 기준: 조용히 빼지 않음, 판정을 지어내지 않음
def test_llm_criteria_without_assessor_are_incomplete_not_equivalent():
    _, r, full = _run(SCENARIOS["egfr_positive"])
    egfr = _crit(r.state, "I4")
    assert (egfr.status, egfr.reason, egfr.assessment) == ("needs_full_evaluation", "llm_required", None)
    assert r.state.trials["T1"].status == "needs_full_evaluation" and r.state.trials["T1"].eligibility is None
    eq = compare_states(r.state, full)
    assert eq["status"] == "incomplete_comparison"
    assert {c["rule_id"] for c in eq["trials"][0]["criteria"]} == {"I4", "I5", "I6"}


# ------------------------------------------------------------ 불확실한 의존성
def test_uncertain_dependency_reevaluated_on_any_change():
    llm = FakeLLM()
    _, r, full = _run(_add("medication", "metformin", source_type="self_report"), rules=[AGE, OTHER], assessor=llm)
    assert r.impact.fallback_required and r.impact.fallback_criteria == ["T1:E1"]
    assert _crit(r.state, "E1").status == "evaluated" and _crit(r.state, "I1").status == "reused"
    assert compare_states(r.state, full)["status"] == "equivalent"


def test_cross_field_evidence_ambiguity_detected():
    """LLM 판정의 인용이 다른 항목의 새 사실과도 이어지면(모호) 근거 게이트가 판정 불가로 둠."""
    llm = FakeLLM(quote_evidence=True)
    _, r, full = _run(_add("medication", "pembrolizumab", source_reference={"kind": "input_text", "quote": DX_QUOTE}),
                      rules=[AGE, DX], assessor=llm)
    dx = _crit(r.state, "I6")
    assert dx.status == "evaluated" and dx.gate_reason == "ambiguous_evidence"
    assert compare_states(r.state, full)["status"] == "equivalent"


# ------------------------------------------------------------ LLM 기준: 검증되지 않은 재사용 차단
def test_llm_result_not_reused_without_assessor():
    """ECOG만 바뀌어도 LLM 기준의 이전 판정을 최신 판정으로 쓰지 않음 -> 공급자가 없으면 명시적 미완료."""
    s = store(*base_facts())
    before = snap(s)
    parsed = {"T1": trial([AGE, DX])}
    st0 = evaluate_full(before, parsed, POLICY, FakeLLM())
    assert _crit(st0, "I6").state == "MET" and st0.trials["T1"].eligibility == "ELIGIBLE"
    s.add(P, "ecog", 2, source_type="clinical_record")
    r = update(st0, before, snap(s), parsed, POLICY)  # LLM 공급자 없음 (유료 API 없이)
    dx = _crit(r.state, "I6")
    assert (dx.status, dx.reason, dx.assessment, dx.raw) == ("needs_full_evaluation", "llm_required", None, None)
    t = r.state.trials["T1"]
    assert (t.status, t.eligibility) == ("needs_full_evaluation", None)
    assert compare_states(r.state, evaluate_full(snap(s), parsed, POLICY))["status"] == "incomplete_comparison"


def test_structural_guard_blocks_llm_reuse_even_if_graph_says_unaffected(monkeypatch):
    """의존 판단이 틀려(검증되지 않은 좁은 매핑) '영향 없음'이 나와도, 동등성 비교 없이 update만 써도 재사용하지 않음."""
    from ctrec.dependency_graph import CriterionDependency

    real = incremental.resolve

    def narrow(tid, rule, meta, prev=None, prev_view=None):
        d = real(tid, rule, meta, prev, prev_view)
        if d.evaluator == "llm":  # 예전의 유형별 매핑처럼 일부 항목만 의존한다고 가정
            return CriterionDependency(**{**d.model_dump(), "wildcard": False, "fields": ["primary_diagnosis"]})
        return d
    monkeypatch.setattr(incremental, "resolve", narrow)
    s = store(*base_facts())
    before = snap(s)
    parsed = {"T1": trial([AGE, DX])}
    st0 = evaluate_full(before, parsed, POLICY, FakeLLM())
    s.add(P, "ecog", 2, source_type="clinical_record")
    r = update(st0, before, snap(s), parsed, POLICY)
    assert "T1:I6" in r.impact.unaffected_criteria  # 그래프는 영향 없음으로 판단
    assert r.stats["reuse_blocked"] == 1 and _crit(r.state, "I6").status == "needs_full_evaluation"
    assert _crit(r.state, "I1").status == "reused"


def test_llm_result_reused_only_when_fact_content_identical():
    s = store(*base_facts())
    before = snap(s)
    parsed = {"T1": trial([AGE, DX])}
    st0 = evaluate_full(before, parsed, POLICY, FakeLLM())
    s.add(P, "ecog", 1, source_type="clinical_record")  # 중복 입력: 판정 입력이 그대로
    r = update(st0, before, snap(s), parsed, POLICY)
    assert r.changes.changed == {} and _crit(r.state, "I6").status == "reused"
    assert r.stats["reuse_blocked"] == 0


def test_comparison_reports_llm_scope():
    _, r, full = _run(SCENARIOS["egfr_positive"], assessor=FakeLLM())
    eq = compare_states(r.state, full)
    assert eq["status"] == "equivalent"
    scope = eq["llm_judgments"]
    assert (scope["scope"], scope["sources"], scope["compared"]) == ("assessor_outputs_only", ["fake_llm"], 3)
    _, r, full = _run(SCENARIOS["egfr_positive"])
    scope = compare_states(r.state, full)["llm_judgments"]
    assert scope["scope"] == "none" and scope["unavailable"] == 6


def test_baseline_assessor_supplies_only_fresh_trials():
    from test_offline import _profile
    from test_session_fact_shadow import _fake_matcher_llm
    import ctrec.agents.matcher as m

    m_ = {}
    for tid in ("T1", "T2"):
        p = trial([DX], tid=tid)
        orig = m.llm.structured
        m.llm.structured = _fake_matcher_llm
        try:
            m_[tid] = m.match_trial(_profile(patient_id=P), p, {"brief_summary": ""}, reference_date=REF)
        finally:
            m.llm.structured = orig
    assess = incremental.baseline_assessor(m_, fresh={"T1"})
    assert assess("T1", DX, None, None)[1] == "baseline_matcher"
    assert assess("T2", DX, None, None) is None  # 이번에 다시 매칭되지 않은 시험의 이전 LLM 판정은 공급하지 않음


def test_mapping_false_negative_is_reported_as_mismatch():
    """매핑이 놓친 영향(가상)은 동등성 비교에서 불일치로 드러나야 함. (Hb 두 값의 날짜가 달라 전체 평가는 판정 불가)"""
    _, r, full = _run(_add("lab:hemoglobin", 8.0, unit="g/dL", observed_at="2026-10-05"), rules=[AGE, HB])
    stale = _crit(r.state, "I3").model_copy(deep=True)
    tampered = r.state.model_copy(deep=True)
    tampered.trials["T1"].criteria["I3"] = _crit(evaluate_full(snap(store(*base_facts())), {"T1": trial([AGE, HB])},
                                                               POLICY), "I3")
    tampered.trials["T1"].criteria["I3"].provenance = stale.provenance  # 근거는 그대로, 판정만 이전 값
    eq = compare_states(tampered, full)
    assert eq["status"] == "mismatch" and "state:MET->UNKNOWN" in eq["trials"][0]["criteria"][0]["diffs"]


# ------------------------------------------------------------ 실패 시 이전 결과 차단, UNKNOWN 보존
def test_failed_reevaluation_blocks_stale_result(monkeypatch):
    s = store(*base_facts())
    before = snap(s)
    parsed = {"T1": trial([AGE, ECOG])}
    st0 = evaluate_full(before, parsed, POLICY)
    assert st0.trials["T1"].eligibility == "ELIGIBLE"
    s.add(P, "ecog", 0, source_type="clinical_record")
    real = rule_engine.evaluate

    def flaky(rule, profile):
        if rule.field == "ecog":
            raise RuntimeError("민감할 수 있는 메시지 ECOG 0")
        return real(rule, profile)
    monkeypatch.setattr(incremental.rule_engine, "evaluate", flaky)
    r = update(st0, before, snap(s), parsed, POLICY)
    ecog = _crit(r.state, "I2")
    assert (ecog.status, ecog.reason, ecog.assessment) == ("needs_full_evaluation", "evaluation_error:RuntimeError", None)
    t = r.state.trials["T1"]
    assert (t.status, t.eligibility, t.match_status) == ("needs_full_evaluation", None, None)  # 이전 ELIGIBLE을 쓰지 않음
    assert "ECOG 0" not in str(r.summary())


def test_unknown_not_turned_into_mismatch():
    _, r, full = _run(SCENARIOS["value_conflict"], rules=[AGE, ECOG, HB])
    ecog = _crit(r.state, "I2")
    assert ecog.state == "UNKNOWN" and ecog.gate_reason == "conflict"
    t = r.state.trials["T1"]
    assert (t.eligibility, t.match_status) == ("UNCERTAIN", "INSUFFICIENT_INFORMATION")  # CRITERIA_MISMATCH 아님
    assert compare_states(r.state, full)["status"] == "equivalent"


# ------------------------------------------------------------ 전체 재평가 fallback
def test_policy_change_falls_back_to_full_evaluation():
    pol = EvaluationPolicy(reference_date="2026-12-31")
    _, r, full = _run(SCENARIOS["duplicate"], rules=[AGE, ECOG], new_policy=pol)
    assert r.fallback == {"required": True, "performed": True, "trials": {"T1": "policy_changed"}, "reason": None,
                          "unresolved_trials": []}
    assert compare_states(r.state, full)["status"] == "equivalent"


def test_fallback_not_performed_is_recorded():
    pol = EvaluationPolicy(reference_date="2026-12-31")
    _, r, full = _run(SCENARIOS["duplicate"], rules=[AGE, ECOG], new_policy=pol, full_fallback=False)
    assert r.fallback["performed"] is False and r.fallback["reason"] == "full_fallback_disabled"
    assert {c.reason for c in r.state.trials["T1"].criteria.values()} == {"fallback_not_performed"}
    assert compare_states(r.state, full)["status"] == "fallback_required"


def test_rules_changed_and_stale_state_fall_back():
    s = store(*base_facts())
    before = snap(s)
    st0 = evaluate_full(before, {"T1": trial([AGE, ECOG])}, POLICY)
    r = update(st0, before, before, {"T1": trial([AGE, ECOG.model_copy(update={"value": "2"})])}, POLICY)
    assert r.fallback["trials"] == {"T1": "rules_changed"}
    s.add(P, "ecog", 2, source_type="clinical_record")
    r = update(st0, snap(s), snap(s), {"T1": trial([AGE, ECOG])}, POLICY)  # 결과와 다른 스냅샷을 이전 것으로 줌
    assert r.fallback["trials"] == {"T1": "state_version_mismatch"}


def test_not_evaluated_trial_preserved():
    s = store(*base_facts())
    before = snap(s)
    parsed = {"T1": trial([AGE]), "T2": None}
    st0 = evaluate_full(before, parsed, POLICY)
    s.add(P, "age", 63, unit="years", source_type="clinical_record")
    r = update(st0, before, snap(s), parsed, POLICY)
    assert st0.trials["T2"].status == r.state.trials["T2"].status == "not_evaluated"
    assert r.state.trials["T2"].eligibility is None and r.state.trials["T2"].criteria == {}
    assert compare_states(r.state, evaluate_full(snap(s), parsed, POLICY))["status"] == "equivalent"


def test_new_trial_evaluated_fully():
    s = store(*base_facts())
    before = snap(s)
    st0 = evaluate_full(before, {"T1": trial([AGE])}, POLICY)
    r = update(st0, before, before, {"T1": trial([AGE]), "T2": trial([ECOG], tid="T2")}, POLICY)
    assert r.fallback["trials"] == {"T2": "trial_added"} and r.state.trials["T2"].status == "decided"


def test_review_rule_kept_as_review():
    meta = {"I2": incremental.matcher._meta(trial([ECOG]), ECOG).model_copy(update={"review": "검토 필요"})}
    s = store(*base_facts())
    before = snap(s)
    parsed = {"T1": trial([AGE, ECOG], meta=meta)}
    st0 = evaluate_full(before, parsed, POLICY)
    assert _crit(st0, "I2").assessment.underspecified and _crit(st0, "I2").state == "UNKNOWN"
    s.add(P, "age", 70, unit="years", source_type="clinical_record")
    s1 = snap(s)
    r = update(st0, before, s1, parsed, POLICY)
    assert _crit(r.state, "I2").status == "reused"
    s.add(P, "ecog", 3, source_type="clinical_record")  # 필드가 상충하면 검토 기준도 '상충 정보 확인'으로
    r2 = update(r.state, s1, snap(s), parsed, POLICY)
    assert (_crit(r2.state, "I2").status, _crit(r2.state, "I2").gate_reason) == ("evaluated", "conflict")
    assert compare_states(r2.state, evaluate_full(snap(s), parsed, POLICY))["status"] == "equivalent"


def test_different_patient_rejected():
    s = store(*base_facts())
    st0 = evaluate_full(snap(s), {"T1": trial([AGE])}, POLICY)
    other = snap(store(*base_facts(), patient="SYN-2"), "SYN-2")
    with pytest.raises(ValueError):
        update(st0, snap(s), other, {"T1": trial([AGE])}, POLICY)


# ------------------------------------------------------------ 실제 고정 기준표 (읽기만) + 측정 스크립트
def test_frozen_store_scenarios_equivalent_and_counts_consistent():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import bench_incremental
    from ctrec import store_patch

    rows = bench_incremental.run(repeat=1)
    assert rows and all(r["trials"] == 26 and r["total_criteria"] == 471 for r in rows)
    for r in rows:
        assert r["equivalence"] == "equivalent", r
        assert r["reused"] + r["reevaluated"] == r["total_criteria"]
        assert r["llm_stub_calls_incremental"] <= r["llm_stub_calls_full"]
    dup = next(r for r in rows if r["scenario"] == "duplicate_reentry")
    assert (dup["changed_fields"], dup["reevaluated"], dup["llm_stub_calls_incremental"]) == (0, 0, 0)
    assert store_patch.verify_frozen() == []
