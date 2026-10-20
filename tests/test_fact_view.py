"""사실 -> Matcher 입력 계층과 사실 기반 근거 게이트 (Phase 1-3B/1-4, 체크포인트 1·2). 합성 데이터만 사용."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.fact_store import FactStore
from ctrec.fact_view import FactEvidenceView, gate, tier
from ctrec.schemas import CriterionAssessment, RuleMeta

from test_offline import _rule

P = "P001"
REF = "2026-10-12"
REC = {"kind": "input_text", "ref": "raw_text:initial"}


def _store(*facts) -> FactStore:
    s = FactStore()
    for field, value, kw in facts:
        s.add(P, field, value, **kw)
    return s


def _a(rule, holds="yes", method="rule_engine", evidence="e") -> CriterionAssessment:
    from ctrec.tools.rule_engine import passes
    return CriterionAssessment(rule_id=rule.rule_id, kind=rule.kind, criterion_text=rule.text, holds=holds,
                               passes=passes(rule.kind, holds), confidence="high", evidence=evidence, reasoning="r",
                               missing_info=None, method=method, category=rule.category, cohort=rule.cohort)


ECOG = _rule(rule_id="I1", text="ECOG 0-1", category="performance_status", field="ecog", operator="<=", value="1")
AGE = _rule(rule_id="I2", text="Age >= 18", field="age", operator=">=", value="18", unit="years")
HB = _rule(rule_id="I3", text="Hb >= 9 g/dL within 28 days", category="lab", field="lab:hemoglobin", operator=">=",
           value="9", unit="g/dL", time_window_days=28)
EGFR = _rule(rule_id="I4", text="EGFR mutation positive", category="biomarker", structured_evaluable=False)
RECORD = RuleMeta(required_evidence="clinical_record")
SELF_OK = RuleMeta(required_evidence="self_report_ok")


# ------------------------------------------------------------ 체크포인트 1: 입력 계층
def test_view_preserves_provenance_and_builds_profile():
    s = _store(("age", 62, {"unit": "years", "source_type": "clinical_record"}),
               ("ecog", 1, {"source_type": "clinical_record", "source_reference": {**REC, "quote": "ECOG 1"}}),
               ("ecog", 1, {"source_type": "self_report", "source_reference": {"kind": "answer", "quote": "1이에요"}}),
               ("lab:hemoglobin", 11.2, {"unit": "g/dL", "observed_at": "2026-10-01",
                                         "source_type": "clinical_record",
                                         "source_reference": {**REC, "quote": "Hb 11.2"}}),
               ("diagnosis", "hypertension", {}), ("diagnosis", "type 2 diabetes", {}))
    v = FactEvidenceView(s, P)
    assert v.fields["ecog"].state == "agreed" and len(v.fields["ecog"].facts) == 2  # 같은 값의 다른 근거 보존
    assert [r.tier for r in v.fields["ecog"].facts] == ["declared_record", "self_report"]
    p = v.to_profile()
    assert (p.age, p.age_value, p.age_unit, p.ecog, p.sex) == (62, 62.0, "years", 1, "unknown")  # 성별은 만들지 않음
    assert [(l.name, l.value, l.unit, l.date) for l in p.labs] == [("hemoglobin", 11.2, "g/dL", "2026-10-01")]
    assert [d.description for d in p.diagnoses] == ["hypertension", "type 2 diabetes"]  # 공존하는 항목 모두
    assert p.biomarkers == [] and p.primary_diagnosis is None and p.missing_or_ambiguous == []
    assert s.facts(P)[0].value == 62  # 저장소는 그대로


def test_conflicting_scalar_left_empty_not_chosen():
    s = _store(("ecog", 1, {}), ("ecog", 2, {}),
               ("lab:hemoglobin", 10.0, {"unit": "g/dL", "observed_at": "2026-06-01"}),
               ("lab:hemoglobin", 12.0, {"unit": "g/dL", "observed_at": "2026-09-01"}))
    v = FactEvidenceView(s, P)
    assert v.excluded() == {"ecog": "contradictory", "lab:hemoglobin": "time_dependent"}
    p = v.to_profile()
    assert p.ecog is None and p.labs == []  # 최신 값을 고르지 않음
    assert sorted(p.missing_or_ambiguous) == ["ecog: 사실 값이 맞지 않음(contradictory)",
                                             "lab:hemoglobin: 사실 값이 맞지 않음(time_dependent)"]


def test_tier_never_promotes_declaration():
    s = _store(("ecog", 1, {"source_type": "clinical_record"}))
    f = s.facts(P)[0]
    assert tier(f) == "declared_record" and f.verification_status == "unknown"
    s.set_verification(f.fact_id, "document_verified", reason="외래 기록 확인", by="연구간호사", document_reference="doc-1")
    assert tier(f) == "verified"


# ------------------------------------------------------------ 체크포인트 2: 출처·검증·충돌 게이트
def _gate(store, rule, a, meta, **kw):
    return gate(a, rule, meta, FactEvidenceView(store, P), REF, **kw)


def test_self_report_only_for_record_criterion_is_unverified():
    s = _store(("ecog", 1, {"source_type": "self_report", "source_reference": {"kind": "answer", "quote": "1"}}))
    g, why = _gate(s, ECOG, _a(ECOG), RECORD)
    assert why == "self_report_only" and g.passes is None and g.needs_verification and g.self_reported_holds == "yes"


def test_self_report_allowed_when_protocol_allows():
    s = _store(("age", 62, {"unit": "years", "source_type": "self_report"}))
    g, why = _gate(s, AGE, _a(AGE), SELF_OK)
    assert why is None and g.passes is True and g.evidence_source == "self_report"


def test_declared_record_is_not_document_verified():
    s = _store(("ecog", 1, {"source_type": "clinical_record"}))
    g, why = _gate(s, ECOG, _a(ECOG), RECORD)
    assert why is None and g.passes is True  # 기존 기준선과 같이 선언된 기록은 잠정 인정
    g, why = _gate(s, ECOG, _a(ECOG), RECORD, require_document_verification=True)
    assert why == "declared_unverified" and g.passes is None and g.self_reported_holds is None


def test_document_verified_fact_passes_strict_mode():
    s = _store(("ecog", 1, {"source_type": "self_report"}))
    s.set_verification(s.facts(P)[0].fact_id, "document_verified", reason="확인", by="b", document_reference="doc")
    g, why = _gate(s, ECOG, _a(ECOG), RECORD, require_document_verification=True)
    assert why is None and g.passes is True and g.evidence_source == "record"


def test_unknown_source_for_record_criterion_is_unverified():
    s = _store(("ecog", 1, {}))  # 인용 없는 단일 값 (어댑터 기본: unknown)
    g, why = _gate(s, ECOG, _a(ECOG), RECORD)
    assert why == "unknown_source" and g.needs_verification and g.self_reported_holds is None


def test_unverified_info_not_turned_into_mismatch():
    """미검증 근거는 판정 불가로 남지 불일치(NOT_MET)가 되지 않음."""
    s = _store(("ecog", 3, {"source_type": "self_report"}))
    g, why = _gate(s, ECOG, _a(ECOG, holds="no"), RECORD)
    assert g.passes is None and why == "self_report_only"


def test_observation_window_not_met():
    old = _store(("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-06-01", "source_type": "clinical_record"}))
    g, why = _gate(old, HB, _a(HB), RECORD)
    assert why == "observation_window" and g.passes is None and not g.needs_verification
    undated = _store(("lab:hemoglobin", 11.0, {"unit": "g/dL", "source_type": "clinical_record"}))
    assert _gate(undated, HB, _a(HB), RECORD)[1] == "observation_window"
    month = _store(("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-09", "source_type": "clinical_record"}))
    assert _gate(month, HB, _a(HB), RECORD)[1] == "observation_window"  # 9월 1일부터면 28일을 넘을 수 있음
    recent = _store(("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-10-01", "source_type": "clinical_record"}))
    assert _gate(recent, HB, _a(HB), RECORD) [1] is None


def test_ecog_conflict_blocks_decision():
    s = _store(("ecog", 1, {"source_type": "clinical_record"}), ("ecog", 2, {"source_type": "clinical_record"}))
    g, why = _gate(s, ECOG, _a(ECOG), RECORD)
    assert why == "conflict" and g.passes is None
    g, why = _gate(s, ECOG, _a(ECOG, method="llm", evidence="ecog: 1"), RECORD)  # LLM이 한쪽 값을 골라도 막음
    assert why == "conflict" and g.passes is None


def test_time_dependent_values_block_decision():
    s = _store(("ecog", 1, {"observed_at": "2026-01-01", "source_type": "clinical_record"}),
               ("ecog", 3, {"observed_at": "2026-09-01", "source_type": "clinical_record"}))
    assert _gate(s, ECOG, _a(ECOG), RECORD)[1] == "conflict"


def test_biomarker_possible_opposite_results_needs_review():
    s = _store(("biomarker", "EGFR positive", {"source_type": "clinical_record",
                                               "source_reference": {**REC, "quote": "EGFR positive"}}),
               ("biomarker", "EGFR negative", {"source_type": "clinical_record",
                                               "source_reference": {**REC, "quote": "EGFR negative"}}))
    g, why = _gate(s, EGFR, _a(EGFR, method="llm", evidence="EGFR positive"), RECORD)
    assert why == "biomarker_unresolved" and g.passes is None and g.underspecified  # 의사 확인, 일치로 묶지 않음


def test_coexisting_diagnoses_and_medications_do_not_block():
    dx = _rule(rule_id="I5", text="Hypertension", category="comorbidity", structured_evaluable=False)
    s = _store(("diagnosis", "hypertension", {"source_type": "clinical_record",
                                              "source_reference": {**REC, "quote": "고혈압 진단"}}),
               ("diagnosis", "type 2 diabetes", {"source_type": "clinical_record"}),
               ("medication", "amlodipine", {"source_type": "self_report"}),
               ("medication", "metformin", {"source_type": "self_report"}))
    g, why = _gate(s, dx, _a(dx, method="llm", evidence="고혈압 진단"), SELF_OK)
    assert why is None and g.passes is True and g.evidence_source == "record"


def test_llm_evidence_not_linked_to_any_fact_is_blocked():
    s = _store(("ecog", 1, {"source_type": "clinical_record"}))
    g, why = _gate(s, EGFR, _a(EGFR, method="llm", evidence="환자는 EGFR 변이 양성으로 보임"), SELF_OK)
    assert why == "ungrounded" and g.passes is None


def test_undecided_assessment_left_as_is():
    s = _store(("ecog", 1, {}))
    a = _a(ECOG, holds="unknown")
    g, why = _gate(s, ECOG, a, RECORD)
    assert why is None and g.passes is None and not g.needs_verification


# ------------------------------------------------------------ 바이오마커 표적 관련성
from ctrec.fact_view import link_evidence, non_biomarker_mentions, targets_in  # noqa: E402

PDL1 = _rule(rule_id="I6", text="PD-L1 TPS >= 50%", category="biomarker", structured_evaluable=False)
DRIVER = _rule(rule_id="I7", text="Actionable driver mutation present", category="biomarker", structured_evaluable=False)
EGFR_Q = "EGFR exon 19 deletion 양성"
PDL1_Q = "PD-L1 TPS 60% 확인됨"


def _bm(value, quote=None, **kw):
    return ("biomarker", value, {"source_type": "clinical_record",
                                 "source_reference": {**REC, "quote": quote or value}, **kw})


def test_target_table_is_explicit_and_word_bounded():
    assert targets_in("EGFR exon 19") == {"EGFR"} and targets_in("PD-L1 TPS 60%") == {"PD-L1"}
    assert targets_in("ERBB2 amplification") == {"HER2"}
    # Phase 4-4: 신장 기능 eGFR은 유전자 EGFR이 아님 (이전에는 대소문자 무시로 EGFR로 잘못 잡았음)
    assert targets_in("eGFR 85 mL/min") == set() and non_biomarker_mentions("eGFR 85 mL/min") == {"lab:egfr"}
    assert targets_in("criteria met, retained") == set()  # MET은 목록에 없음, 'ret'는 단어 경계로만


def test_different_targets_do_not_force_review():
    """EGFR과 PD-L1이 함께 있어도 각 기준은 자기 표적의 사실만 봄."""
    s = _store(_bm("EGFR exon 19 deletion positive", EGFR_Q), _bm("PD-L1 TPS 60%", PDL1_Q))
    g, why = _gate(s, EGFR, _a(EGFR, method="llm", evidence=EGFR_Q), RECORD)
    assert why is None and g.passes is True and not g.underspecified
    g, why = _gate(s, PDL1, _a(PDL1, method="llm", evidence=PDL1_Q), RECORD)
    assert why is None and g.passes is True
    for f in s.facts(P):
        assert f.verification_status == "unknown"  # 표적을 구분해도 검증된 것은 아님


def test_same_target_opposite_results_stay_under_review():
    s = _store(_bm("EGFR positive", "EGFR 양성 결과지"), _bm("EGFR negative", "EGFR 음성이라고 들음"),
               _bm("PD-L1 TPS 60%", PDL1_Q))
    g, why = _gate(s, EGFR, _a(EGFR, method="llm", evidence="EGFR 양성 결과지"), RECORD)
    assert why == "biomarker_unresolved" and g.passes is None and g.underspecified
    g, why = _gate(s, EGFR, _a(EGFR, holds="no", method="llm", evidence="EGFR 음성이라고 들음"), RECORD)
    assert why == "biomarker_unresolved" and g.passes is None  # NOT_MET도 확정하지 않음
    g, why = _gate(s, PDL1, _a(PDL1, method="llm", evidence=PDL1_Q), RECORD)
    assert why is None and g.passes is True  # 다른 표적의 상반 결과로 PD-L1 기준까지 막지 않음


def test_unknown_target_handled_conservatively():
    s = _store(_bm("EGFR exon 19 deletion positive", EGFR_Q), _bm("PD-L1 TPS 60%", PDL1_Q))
    g, why = _gate(s, DRIVER, _a(DRIVER, method="llm", evidence=EGFR_Q), RECORD)
    assert why == "biomarker_unresolved"  # 기준의 표적을 모르면 모든 바이오마커 사실을 관련으로 봄
    t = _store(_bm("EGFR exon 19 deletion positive", EGFR_Q), _bm("driver mutation negative", "드라이버 변이 음성 소견"))
    g, why = _gate(t, EGFR, _a(EGFR, method="llm", evidence=EGFR_Q), RECORD)
    assert why == "biomarker_unresolved"  # 표적을 모르는 사실은 관련 없다고 단정하지 않음


def test_other_target_fact_cannot_support_criterion():
    s = _store(_bm("PD-L1 TPS 60%", PDL1_Q))
    g, why = _gate(s, EGFR, _a(EGFR, method="llm", evidence=PDL1_Q), RECORD)
    assert why == "ungrounded" and g.passes is None


# ------------------------------------------------------------ 근거 연결
def test_short_phrase_in_several_facts_is_ambiguous():
    dx = _rule(rule_id="I8", text="Hypertension", category="comorbidity", structured_evaluable=False)
    s = _store(("diagnosis", "hypertension", {"source_reference": {**REC, "quote": "있음"}}),
               ("medication", "aspirin", {"source_reference": {**REC, "quote": "있음"}}))
    assert link_evidence("있음", FactEvidenceView(s, P)).status == "ambiguous"
    g, why = _gate(s, dx, _a(dx, method="llm", evidence="있음"), SELF_OK)
    assert why == "ambiguous_evidence" and g.passes is None


def test_partial_coincidental_overlap_does_not_link():
    """'EGFR exon' 같은 짧은 공통 조각만으로는 잇지 않음 (MET뿐 아니라 NOT_MET도)."""
    s = _store(_bm("EGFR exon 19 deletion positive", EGFR_Q))
    assert link_evidence("EGFR exon 21 L858R 음성", FactEvidenceView(s, P)).status == "none"
    for holds in ("yes", "no"):
        g, why = _gate(s, EGFR, _a(EGFR, holds=holds, method="llm", evidence="EGFR exon 21 L858R 음성"), RECORD)
        assert why == "ungrounded" and g.passes is None


def test_sufficient_context_containment_links():
    s = _store(_bm("EGFR exon 19 deletion positive", EGFR_Q))
    v = FactEvidenceView(s, P)
    assert link_evidence("병리 보고서: EGFR exon 19 deletion 양성 (2026-09)", v).status == "linked"  # 인용 전체 포함
    assert link_evidence("EGFR exon 19 deletion", v).status == "linked"  # 인용의 일부지만 12자 이상
    assert link_evidence("exon 19", v).status == "none"  # 너무 짧음


def test_structured_fact_id_links_exactly():
    s = _store(_bm("EGFR exon 19 deletion positive", EGFR_Q), ("diagnosis", "hypertension", {}))
    fid = s.facts(P, "biomarker")[0].fact_id
    link = link_evidence(f"근거 사실 {fid}", FactEvidenceView(s, P))
    assert (link.status, link.fact_ids) == ("linked", [fid])
    g, why = _gate(s, EGFR, _a(EGFR, method="llm", evidence=f"근거 사실 {fid}"), RECORD)
    assert why is None and g.passes is True
    assert link_evidence("근거 사실 F-000000000000", FactEvidenceView(s, P)).status == "invalid_ref"
    g, why = _gate(s, EGFR, _a(EGFR, method="llm", evidence="근거 사실 F-000000000000"), RECORD)
    assert why == "ungrounded"


def test_same_quote_on_several_facts_is_ambiguous_unless_identical():
    q = "고혈압과 당뇨로 약을 복용하고 있음"
    s = _store(("diagnosis", "hypertension", {"source_reference": {**REC, "quote": q}}),
               ("medication", "metformin", {"source_reference": {**REC, "quote": q}}))
    assert link_evidence(q, FactEvidenceView(s, P)).status == "ambiguous"
    t = _store(("diagnosis", "hypertension", {"source_reference": {**REC, "quote": q}, "session_id": "S1"}),
               ("diagnosis", "hypertension", {"source_reference": {**REC, "quote": q}, "session_id": "S2"}))
    link = link_evidence(q, FactEvidenceView(t, P))
    assert link.status == "linked" and len(link.fact_ids) == 2  # 항목·값·근거 수준이 같은 같은 사실의 반복


def test_field_reference_must_match_value():
    s = _store(("ecog", 1, {}))
    assert link_evidence("ecog: 2", FactEvidenceView(s, P)).status == "none"
    assert link_evidence("ecog: 1", FactEvidenceView(s, P)).status == "linked"
