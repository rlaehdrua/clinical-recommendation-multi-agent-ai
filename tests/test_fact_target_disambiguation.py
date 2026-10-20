"""바이오마커 표적과 검사 약어 구분 (Phase 4-4): EGFR(유전자) vs eGFR(신장 기능), ALK vs Alk phos. 합성 데이터."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ctrec import fact_changes  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.dependency_graph import DependencyGraph, analyze_impact, resolve  # noqa: E402
from ctrec.decision_state import info_key  # noqa: E402
from ctrec.fact_store import FactStore  # noqa: E402
from ctrec.fact_view import FactEvidenceView, gate, non_biomarker_mentions, relevant_biomarker_facts, targets_in  # noqa: E402
from ctrec.schemas import CriterionAssessment, RuleMeta  # noqa: E402
from ctrec.tools import rule_engine  # noqa: E402

from test_offline import _rule  # noqa: E402


@pytest.mark.parametrize("text, targets, labs", [
    ("Normal renal function: eGFR≥90 ml/min/1.73m².", set(), {"lab:egfr"}),  # 신장 검사 (고정 기준표 NCT07087314:I5)
    ("eGFR 45 mL/min/1.73m²", set(), {"lab:egfr"}),
    ("EGFR ≥ 60 mL/min/1.73m2", set(), {"lab:egfr"}),  # 대문자여도 신장 문맥이면 유전자가 아님
    ("Bedside Schwartz eGFR was about 130 mL/min/1.73m2.", set(), {"lab:egfr"}),
    ("eGFR test done", set(), set()),  # 신장 문맥이 없는 eGFR: 어느 쪽으로도 단정하지 않음
    ("EGFR mutation positive", {"EGFR"}, set()),
    ("Prior treatment with an EGFR inhibitor (osimertinib)", {"EGFR"}, set()),  # 약물 이력도 유전자 표적
    ("EGFR testing performed", {"EGFR"}, set()),
    ("egfr exon 19 deletion", set(), set()),  # 대소문자가 섞이면 자동 연결하지 않음 (표적 미상)
    ("Egfr L858R", set(), set()),
    ("EGFR exon 19 deletion; eGFR 45 mL/min/1.73m²", {"EGFR"}, {"lab:egfr"}),  # 한 문장의 두 용어를 절 단위로 구분
    ("eGFR 45 mL/min/1.73m², EGFR wild-type", {"EGFR"}, {"lab:egfr"}),
    ("Alk phos 120 U/L", set(), {"lab:alkaline phosphatase"}),
    ("alkaline phosphatase 2x ULN", set(), {"lab:alkaline phosphatase"}),
    ("ALK rearrangement positive", {"ALK"}, set()),
    ("EGFR wild-type, ALK negative", {"EGFR", "ALK"}, set()),
    ("PD-L1 TPS 50%", {"PD-L1"}, set()), ("HER2 positive", {"HER2"}, set()), ("ERBB2 amplification", {"HER2"}, set()),
    ("KRAS G12C", {"KRAS"}, set()), ("신장 기능 eGFR 80", set(), {"lab:egfr"}),
])
def test_target_and_lab_mentions(text, targets, labs):
    assert targets_in(text) == targets
    assert non_biomarker_mentions(text) == labs


def test_structured_key_takes_precedence():
    kidney = _rule(rule_id="I5", text="Normal renal function: eGFR≥90 ml/min/1.73m².", category="lab",
                   field="lab:eGFR", operator=">=", value="90", unit="mL/min/1.73m2")
    assert info_key(kidney, "T")[0] == "lab:egfr"
    free = _rule(rule_id="I6", text="EGFR ≥ 60 mL/min/1.73m2", category="lab", structured_evaluable=False)
    assert info_key(free, "T")[0] == "criterion:T:I6"  # 바이오마커 EGFR 질문과 묶이지 않음
    gene = _rule(rule_id="I7", text="EGFR mutation positive", category="biomarker", structured_evaluable=False)
    assert info_key(gene, "T")[0] == "biomarker:EGFR"


EGFR_RULE = _rule(rule_id="I1", text="EGFR mutation positive", category="biomarker", structured_evaluable=False)


def _view(*facts):
    s = FactStore()
    for field, value, src in facts:
        s.add("P", field, value, source_type=src)
    return s, FactEvidenceView(s, "P")


def _met():
    return CriterionAssessment(rule_id="I1", kind="inclusion", criterion_text=EGFR_RULE.text, holds="yes", passes=True,
                               confidence="high", evidence="근거", reasoning="r", missing_info=None, method="llm",
                               category="biomarker")


def test_misfiled_kidney_fact_not_a_conflicting_egfr_result():
    """바이오마커 항목에 잘못 들어간 신장 검사 문장은 EGFR 결과와 '상반된 결과'로 보지 않음 (예전에는 검토로 보냄)."""
    _, view = _view(("biomarker", "EGFR exon 19 deletion positive", "clinical_record"),
                    ("biomarker", "eGFR 45 mL/min/1.73m2", "self_report"))
    _, rel = relevant_biomarker_facts(EGFR_RULE, view)
    assert [r.value for r in rel] == ["EGFR exon 19 deletion positive"]
    _, why = gate(_met(), EGFR_RULE, RuleMeta(), view, "2026-10-13")
    assert why != "biomarker_unresolved"
    kidney = next(r for r in view.fields["biomarker"].facts if "eGFR" in str(r.value))
    assert kidney.tier == "self_report"  # 표시만 붙을 뿐 검증 상태·출처는 그대로


def test_unknown_target_still_conservative():
    _, view = _view(("biomarker", "EGFR exon 19 deletion positive", "clinical_record"),
                    ("biomarker", "egfr negative", "self_report"))  # 대소문자가 섞인 표기: 표적 미상 -> 관련 있는 것으로
    _, rel = relevant_biomarker_facts(EGFR_RULE, view)
    assert len(rel) == 2
    _, why = gate(_met(), EGFR_RULE, RuleMeta(), view, "2026-10-13")
    assert why == "biomarker_unresolved"  # 상반된 결과일 수 있어 판정 불가 (보수적 처리 유지)


def test_dependency_impact_ignores_kidney_but_keeps_unknown():
    from ctrec.schemas import ParsedTrial
    p = ParsedTrial(trial_id="T", title="t", target_population="", intervention_summary="", key_conditions=[],
                    cohorts=[], rules=[EGFR_RULE.model_copy(update={"structured_evaluable": True, "field": "lab:egfr_gene",
                                                                    "operator": "=="})], parsing_notes="")
    s, _ = _view(("biomarker", "EGFR exon 19 deletion positive", "clinical_record"))
    before = fact_changes.take_snapshot(s, "P")
    prev = CriterionAssessment(rule_id="I1", kind="inclusion", criterion_text="t", holds="yes", passes=True,
                               confidence="high", evidence="e", reasoning="r", missing_info=None, method="rule_engine",
                               category="biomarker")
    deps = {("T", "I1"): resolve("T", p.rules[0], RuleMeta(), prev)}
    for text, affected in (("eGFR 50 mL/min/1.73m2", False), ("egfr negative", True), ("EGFR negative", True)):
        s2 = FactStore()
        for f in before.facts:
            s2._facts.setdefault("P", []).append(f.model_copy(deep=True))
        s2.add("P", "biomarker", text, source_type="self_report")
        after = fact_changes.take_snapshot(s2, "P")
        r = analyze_impact(fact_changes.detect_changes(before, after), DependencyGraph.build({"T": p}, deps, after),
                           before, after)
        assert bool(r.affected_criteria) is affected, text


def test_matcher_judgment_path_does_not_use_target_inference():
    """잘못된 표적 연결은 진단·계획 경로(fact_view·dependency_graph·decision_state)에만 영향, 기존 Matcher 판정에는 없음."""
    for mod in (matcher, rule_engine):
        src = inspect.getsource(mod)
        assert "targets_in" not in src and "fact_view" not in src


def test_frozen_store_rule_change_is_only_the_kidney_rule():
    import re
    from bench_incremental import load_frozen
    from ctrec.fact_view import BIOMARKER_TARGETS
    old = {t: re.compile(r"(?<![a-z0-9])(?:" + "|".join(p) + r")(?![a-z0-9])", re.I) for t, p in BIOMARKER_TARGETS.items()}
    changed = [(t, r.rule_id) for t, p in load_frozen().items() for r in p.rules
               if {k for k, rx in old.items() if rx.search(r.text)} != targets_in(r.text)]
    assert changed == [("NCT07087314", "I5")]
