"""의존 그래프와 변경 영향 분석 (Phase 2, 체크포인트 1·2). 합성 데이터, API 호출 없음."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import incremental
from ctrec.agents import matcher
from ctrec.dependency_graph import DependencyGraph, analyze_impact, cohort_groups, resolve
from ctrec.fact_changes import detect_changes
from ctrec.fact_view import FactEvidenceView

from incremental_fixtures import (AGE, DX, ECOG, EGFR, HB, OTHER, P, PDL1, POLICY, FakeLLM, base_facts, review_meta,
                                  snap, store, trial)
from test_offline import _rule


def _deps(rules, facts=None, assessor=None, cohorts=(), meta=None):
    s = store(*(facts or base_facts()))
    sn = snap(s)
    p = trial(rules, cohorts, meta=meta)
    st = incremental.evaluate_full(sn, {"T1": p}, POLICY, assessor)
    view = FactEvidenceView(s, P)
    deps = {("T1", r.rule_id): resolve("T1", r, matcher._meta(p, r), st.trials["T1"].criteria[r.rule_id].raw, view)
            for r in p.rules}
    return s, sn, p, deps


def _impact(rules, change, facts=None, assessor=None, cohorts=(), meta=None):
    s, before, p, deps = _deps(rules, facts, assessor, cohorts, meta)
    change(s)
    after = snap(s)
    graph = DependencyGraph.build({"T1": p}, deps, after)
    return analyze_impact(detect_changes(before, after), graph, before, after), deps, graph


def _affected(report):
    return sorted(a.rule_id for a in report.affected_criteria)


# ------------------------------------------------------------ 단일 필드 기준
def test_structured_fields_map_to_their_criteria():
    _, _, _, deps = _deps([AGE, ECOG, HB])
    assert {k[1]: (d.fields, d.resolution) for k, d in deps.items()} == {
        "I1": (["age"], "explicit_field"), "I2": (["ecog"], "explicit_field"),
        "I3": (["lab:hemoglobin"], "explicit_field")}  # lab:Hb -> 규칙 엔진 동의어 표로 표준 검사명


def test_age_ecog_hb_changes_hit_only_their_criterion():
    for field, value, kw, rid in [("age", 17, {"unit": "years"}, "I1"), ("ecog", 2, {}, "I2"),
                                  ("lab:hemoglobin", 8.1, {"unit": "g/dL", "observed_at": "2026-10-01"}, "I3")]:
        r, _, _ = _impact([AGE, ECOG, HB], lambda s: s.add(P, field, value, source_type="clinical_record", **kw))
        assert _affected(r) == [rid] and len(r.unaffected_criteria) == 2
        assert r.affected_criteria[0].reason == "dependency_changed" and not r.fallback_required


# ------------------------------------------------------------ 바이오마커
def test_egfr_and_pdl1_rule_engine_dependencies_separate():
    """규칙 엔진 기준은 의존 항목이 검증되므로 표적별로 좁게 잡음."""
    egfr_engine = _rule(rule_id="I12", text="EGFR mutation status", category="biomarker", field="lab:egfr",
                        operator=">=", value="1")
    r, deps, _ = _impact([egfr_engine, ECOG], lambda s: s.add(P, "biomarker", "PD-L1 TPS 60%", source_type="clinical_record"),
                         facts=base_facts() + [("lab:egfr", 1, {})])
    d = deps[("T1", "I12")]
    assert d.resolution == "explicit_field" and d.biomarker_targets == ["EGFR"]
    assert _affected(r) == [] and r.skipped_by_target == ["T1:I12"]  # PD-L1 사실은 EGFR 규칙 엔진 기준과 무관
    r, _, _ = _impact([egfr_engine], lambda s: s.add(P, "biomarker", "EGFR negative", source_type="clinical_record"),
                      facts=base_facts() + [("lab:egfr", 1, {})])
    assert _affected(r) == ["I12"]  # 같은 표적의 상반된 결과는 영향


def test_llm_biomarker_targets_kept_only_as_candidates():
    """LLM 기준은 프로필 전체를 받으므로 표적 구분은 후보 정보로만 남기고 다른 표적 변경에도 영향받음."""
    r, deps, _ = _impact([EGFR, PDL1], lambda s: s.add(P, "biomarker", "PD-L1 TPS 60%", source_type="clinical_record"),
                         assessor=FakeLLM())
    assert deps[("T1", "I4")].biomarker_targets == ["EGFR"] and deps[("T1", "I5")].biomarker_targets == ["PD-L1"]
    assert _affected(r) == ["I4", "I5"] and r.skipped_by_target == []
    assert {a.reason for a in r.affected_criteria} == {"llm_inputs_unverified"}


def test_same_target_conflict_and_unknown_target_are_kept():
    facts = base_facts() + [("biomarker", "EGFR exon 19 deletion positive", {})]
    for value in ("EGFR negative", "driver mutation detected"):
        r, _, _ = _impact([EGFR, PDL1], lambda s: s.add(P, "biomarker", value, source_type="clinical_record"),
                          facts=facts, assessor=FakeLLM())
        assert _affected(r) == ["I4", "I5"]


# ------------------------------------------------------------ 복수 필드, LLM 기준의 미검증 입력
def test_multi_field_dependency():
    """여러 항목에 의존하는 기준: 후보 매핑의 항목은 물론, 후보에 없는 항목(ECOG)이 바뀌어도 영향받음."""
    rule = _rule(rule_id="I7", text="Women of childbearing potential must use contraception", category="reproductive",
                 structured_evaluable=False)
    _, _, _, deps = _deps([rule], assessor=FakeLLM())
    assert {"age", "sex", "medication"} <= set(deps[("T1", "I7")].candidate_fields)
    for field, value in [("age", 45), ("sex", "male"), ("ecog", 2)]:
        r, _, _ = _impact([rule], lambda s: s.add(P, field, value, source_type="clinical_record"))
        assert _affected(r) == ["I7"]


def test_llm_only_criterion_affected_by_ecog_without_referencing_it():
    _, _, _, deps = _deps([DX], assessor=FakeLLM())
    assert "ecog" not in deps[("T1", "I6")].candidate_fields  # 기준·후보 어디에도 ECOG가 없음
    r, _, _ = _impact([DX, AGE], lambda s: s.add(P, "ecog", 2, source_type="clinical_record"), assessor=FakeLLM())
    assert _affected(r) == ["I6"] and r.affected_criteria[0].reason == "llm_inputs_unverified"


def test_field_absent_from_previous_llm_evidence_still_affects():
    """이전 근거가 primary_diagnosis 사실만 가리켜도, 근거에 없던 항목(검사·약물) 변경에 영향받음."""
    _, _, _, deps = _deps([DX], assessor=FakeLLM())
    assert deps[("T1", "I6")].wildcard
    for change in (lambda s: s.add(P, "lab:platelets", 90, unit="10^9/L", source_type="clinical_record"),
                   lambda s: s.add(P, "medication", "warfarin", source_type="self_report")):
        r, _, _ = _impact([DX], change, assessor=FakeLLM())
        assert _affected(r) == ["I6"]


def test_and_or_cohort_groups():
    a_only = ECOG.model_copy(update={"rule_id": "I8", "cohort": "A"})
    b_only = HB.model_copy(update={"rule_id": "I9", "cohort": "B"})
    rules = [AGE, a_only, b_only]
    assert cohort_groups(trial(rules, ["A", "B"])) == {"A": ["I1", "I8"], "B": ["I1", "I9"]}
    r, _, g = _impact(rules, lambda s: s.add(P, "ecog", 3, source_type="clinical_record"), cohorts=["A", "B"])
    assert _affected(r) == ["I8"] and r.affected_groups == ["group:T1:A"]  # B 묶음은 다시 계산할 필요 없음
    assert g.nodes["decision:T1"]["combine"] == "OR(best cohort)"
    assert ("group:T1:A", "decision:T1") in g.edges and ("criterion:T1:I1", "group:T1:B") in g.edges
    r, _, _ = _impact(rules, lambda s: s.add(P, "age", 16, source_type="clinical_record"), cohorts=["A", "B"])
    assert r.affected_groups == ["group:T1:A", "group:T1:B"]  # 공통 기준은 모든 코호트에


# ------------------------------------------------------------ 불확실한 의존성, LLM 기준, 검토, NOT_EVALUATED
def test_unknown_dependency_falls_back_to_all_fields():
    hb_text_only = _rule(rule_id="I10", text="Hemoglobin >= 9 g/dL", category="other", structured_evaluable=False)
    _, _, _, deps = _deps([OTHER, hb_text_only])
    d = deps[("T1", "I10")]
    assert d.wildcard and d.resolution == "fallback_all_fields"  # 문장에 Hemoglobin이 있어도 추측하지 않음
    assert d.candidate_fields == []
    r, _, _ = _impact([OTHER, hb_text_only], lambda s: s.add(P, "medication", "aspirin", source_type="self_report"))
    assert _affected(r) == ["E1", "I10"] and r.fallback_required and r.fallback_criteria == ["T1:E1", "T1:I10"]


def test_llm_only_criterion_dependencies():
    _, _, _, deps = _deps([DX], assessor=FakeLLM())
    d = deps[("T1", "I6")]
    assert (d.evaluator, d.resolution, d.wildcard) == ("llm", "llm_all_fields", True)
    assert {"primary_diagnosis", "diagnosis", "disease_stage", "other_fact"} <= set(d.candidate_fields)
    assert "llm_inputs_unverified" in d.notes


def test_engine_rule_without_engine_result_is_llm_all_fields():
    windowed = HB.model_copy(update={"rule_id": "I11", "time_window_days": 28})  # 기간 조건 검사 -> LLM 위임
    _, _, _, deps = _deps([windowed])
    d = deps[("T1", "I11")]
    assert d.resolution == "llm_all_fields" and d.wildcard and d.fields == ["lab:hemoglobin"]
    assert {"lab:hemoglobin", "lab:*"} <= set(d.candidate_fields)


def test_review_rule_depends_on_its_field_only():
    _, _, _, deps = _deps([ECOG], meta=review_meta("I2"))
    d = deps[("T1", "I2")]
    assert (d.resolution, d.fields, d.wildcard) == ("static_review", ["ecog"], False)


def test_not_evaluated_trial_in_graph():
    _, sn, p, deps = _deps([AGE])
    g = DependencyGraph.build({"T1": p, "T2": None}, deps, sn)
    assert g.not_evaluated == ["T2"] and g.nodes["decision:T2"]["not_evaluated"]
    assert not any(e[1] == "decision:T2" and e[0].startswith("group:") for e in g.edges)
    summary = g.summary()
    assert summary["nodes"]["fact"] == len(sn.facts) and summary["not_evaluated_trials"] == ["T2"]
