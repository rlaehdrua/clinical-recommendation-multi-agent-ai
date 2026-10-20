"""Phase 2 테스트 공용 합성 데이터 (실제 환자 정보 아님). pytest가 수집하지 않는 도우미 모듈."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import fact_changes, incremental
from ctrec.fact_store import FactStore
from ctrec.schemas import CriterionAssessment, ParsedTrial, RuleMeta
from ctrec.tools import rule_engine

from test_decisions import _parsed
from test_offline import _rule

P = "SYN-1"
REF = "2026-10-12"
REC = "clinical_record"
POLICY = incremental.EvaluationPolicy(reference_date=REF)

AGE = _rule(rule_id="I1", text="Age >= 18", field="age", operator=">=", value="18", unit="years")
ECOG = _rule(rule_id="I2", text="ECOG 0-1", category="performance_status", field="ecog", operator="<=", value="1")
HB = _rule(rule_id="I3", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:Hb", operator=">=", value="9",
           unit="g/dL")
EGFR = _rule(rule_id="I4", text="EGFR mutation positive", category="biomarker", structured_evaluable=False)
PDL1 = _rule(rule_id="I5", text="PD-L1 TPS >= 50%", category="biomarker", structured_evaluable=False)
DX = _rule(rule_id="I6", text="Histologically confirmed NSCLC", category="diagnosis", structured_evaluable=False)
OTHER = _rule(rule_id="E1", kind="exclusion", text="Any condition that would interfere with the study",
              category="other", structured_evaluable=False)
DX_QUOTE = "조직검사로 비소세포폐암 4기 확진"


def store(*facts, patient=P) -> FactStore:
    s = FactStore()
    for field, value, kw in facts:
        s.add(patient, field, value, **{"source_type": REC, **kw})
    return s


def snap(s: FactStore, patient=P):
    return fact_changes.take_snapshot(s, patient)


def trial(rules, cohorts=(), tid="T1", meta=None) -> ParsedTrial:
    p = _parsed([r.model_copy() for r in rules], cohorts).model_copy(update={"trial_id": tid})
    p.rule_meta = dict(meta or {})
    return p


def review_meta(rid: str) -> dict[str, RuleMeta]:
    return {rid: RuleMeta(review="원문과 구조화 결과가 다름")}


class FakeLLM:
    """결정적인 LLM 판정 대역. 사실 ID로 근거를 적어 근거 연결이 정확히 되게 함. 호출을 셈."""

    def __init__(self, quote_evidence: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.quote_evidence = quote_evidence

    def __call__(self, tid, rule, profile, view):
        self.calls.append((tid, rule.rule_id))
        holds, evidence = "unknown", "근거 없음"
        if rule.category == "biomarker":
            target = "egfr" if "EGFR" in rule.text else "pd-l1"
            e = view.fields.get("biomarker")
            hits = [r for r in (e.facts if e else []) if target in str(r.value).lower()]
            if hits:
                r = hits[-1]
                holds = "no" if "negative" in str(r.value).lower() else "yes"
                evidence = r.fact_id
        elif rule.category == "diagnosis":
            e = view.fields.get("primary_diagnosis")
            if e and e.state == "agreed":
                r = e.facts[0]
                holds = "yes" if "nsclc" in str(r.value).lower() else "no"
                evidence = (r.quote or r.fact_id) if self.quote_evidence else r.fact_id
        a = CriterionAssessment(
            rule_id=rule.rule_id, kind=rule.kind, criterion_text=rule.text, holds=holds,
            passes=rule_engine.passes(rule.kind, holds), confidence="high" if holds != "unknown" else "low",
            evidence=evidence, reasoning="fake", missing_info=None if holds != "unknown" else rule.text,
            method="llm", category=rule.category, underspecified=rule.underspecified, cohort=rule.cohort)
        return a, "fake_llm"


def base_facts():
    return [("age", 62, {"unit": "years"}), ("sex", "female", {}), ("ecog", 1, {}),
            ("lab:hemoglobin", 11.2, {"unit": "g/dL", "observed_at": "2026-09-01"}),
            ("primary_diagnosis", "NSCLC stage IV", {"source_reference": {"kind": "input_text", "quote": DX_QUOTE}})]
