"""API 호출 없이 실행되는 단위 테스트: 규칙 엔진, 적격성 결정 규칙, 스키마 변환."""

from ctrec.agents.matcher import decide
from ctrec.llm import strict_schema
from ctrec.schemas import CriterionAssessment, LabValue, ParsedTrial, PatientProfile, Rule
from ctrec.tools import rule_engine


def _profile(**kw) -> PatientProfile:
    base = dict(
        patient_id="T", age=62, sex="female", primary_diagnosis="NSCLC", disease_stage="IV",
        diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[], ecog=1,
        other_facts=[], missing_or_ambiguous=[],
    )
    base.update(kw)
    return PatientProfile(**base)


def _rule(**kw) -> Rule:
    base = dict(rule_id="I1", kind="inclusion", text="t", category="demographic", field=None,
                operator=None, value=None, unit=None, time_window_days=None, structured_evaluable=True,
                underspecified=False, cohort=None)
    base.update(kw)
    return Rule(**base)


def test_age_rule():
    r = rule_engine.evaluate(_rule(field="age", operator=">=", value="19"), _profile())
    assert r.holds == "yes" and r.passes is True


def test_exclusion_semantics():
    r = rule_engine.evaluate(_rule(kind="exclusion", field="ecog", operator=">=", value="2"), _profile(ecog=1))
    assert r.holds == "no" and r.passes is True


def test_lab_alias_and_unit_mismatch():
    labs = [LabValue(name="Hb", value=11.2, unit="g/dL", date="2026-09-10", source_quote="Hb 11.2 g/dL")]
    ok = rule_engine.evaluate(_rule(field="lab:hemoglobin", operator=">=", value="9.0", unit="g/dL"), _profile(labs=labs))
    assert ok.passes is True
    mismatch = rule_engine.evaluate(_rule(field="lab:hemoglobin", operator=">=", value="90", unit="g/L"), _profile(labs=labs))
    assert mismatch is None  # 단위가 다르면 LLM에 위임


def test_unknown_defers_to_llm():
    assert rule_engine.evaluate(_rule(field="ecog", operator="<=", value="1"), _profile(ecog=None)) is None


def _a(passes, category="other", underspecified=False):
    return CriterionAssessment(rule_id="I1", kind="inclusion", criterion_text="t",
                               holds="unknown" if passes is None else "yes", passes=passes,
                               confidence="high", evidence="", reasoning="", missing_info=None, method="llm",
                               category=category, underspecified=underspecified)


def test_decide():
    assert decide([_a(True), _a(True)]) == "ELIGIBLE"
    assert decide([_a(True), _a(None)]) == "UNCERTAIN"
    assert decide([_a(None), _a(False)]) == "INELIGIBLE"
    # 절차·행정 '선정' 기준(동의 등)의 판정 불가는 적격성을 막지 않음
    assert decide([_a(True), _a(None, "consent_or_logistics")]) == "ELIGIBLE"
    # 원문에 구체 기준이 없는 항목(의사 확인 필요)의 판정 불가는 ELIGIBLE을 막음 (파서 분류만으로 통과 처리 금지)
    assert decide([_a(True), _a(None, "medication", underspecified=True)]) == "UNCERTAIN"


def test_strict_schema_keeps_field_named_title():
    schema = strict_schema(ParsedTrial)
    assert "title" in schema["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    rule_def = schema["$defs"]["Rule"]
    assert rule_def["additionalProperties"] is False


def test_absent_evidence_cannot_decide():
    from ctrec.agents.matcher import _enforce_evidence
    from ctrec.schemas import LLMAssessment
    base = dict(rule_id="E4", applicable=True, confidence="medium", evidence="근거 없음", reasoning="62세라 폐경 후일 가능성", missing_info=None)
    assert _enforce_evidence(LLMAssessment(holds="no", evidence_type="absent", **base)).holds == "unknown"
    assert _enforce_evidence(LLMAssessment(holds="no", evidence_type="inferred", **{**base, "confidence": "low"})).holds == "unknown"
    assert _enforce_evidence(LLMAssessment(holds="no", evidence_type="inferred", **base)).holds == "unknown"
    # 모델이 추론에 high를 매겨도 확정 불가 (실제 관찰된 사례)
    assert _enforce_evidence(LLMAssessment(holds="no", evidence_type="inferred", **{**base, "confidence": "high"})).holds == "unknown"
    assert _enforce_evidence(LLMAssessment(holds="no", evidence_type="explicit", **base)).holds == "no"


def test_cohort_specific_criteria():
    """코호트별 기준: 대조군 전용 기준 불충족이 IM군 판정을 막지 않아야 함 (S009 사례)."""
    from ctrec.agents.matcher import _select_cohort
    common = _a(True)
    im = _a(True).model_copy(update={"rule_id": "I2", "cohort": "IM group"})
    ctrl = _a(False).model_copy(update={"rule_id": "I3", "cohort": "control group"})
    chosen, cohort, results = _select_cohort([common, im, ctrl], ["IM group", "control group"])
    assert cohort == "IM group" and results == {"IM group": "ELIGIBLE", "control group": "INELIGIBLE"}
    assert ctrl not in chosen and decide(chosen) == "ELIGIBLE"
    # 코호트 없는 시험은 그대로
    assert _select_cohort([common, ctrl.model_copy(update={"cohort": None})], [])[1] is None


def test_not_applicable_conditional_rule_passes():
    """여성 환자에게 '남성 대상 피임' 선정 기준은 해당 없음 -> 통과 (S008 사례)."""
    from ctrec.agents import matcher
    from ctrec.schemas import LLMAssessment, MatcherOutput, ParsedTrial
    rule = _rule(rule_id="I9", kind="inclusion", text="Men who can father a child: must use birth control",
                 category="reproductive", structured_evaluable=False)
    parsed = ParsedTrial(trial_id="T", title="t", target_population="", intervention_summary="", key_conditions=[],
                         cohorts=[], rules=[rule], parsing_notes="")
    fake = MatcherOutput(assessments=[LLMAssessment(rule_id="I9", applicable=False, holds="no", evidence_type="explicit",
                                                    confidence="high", evidence="60세 여성", reasoning="남성 대상 조건", missing_info=None)])
    orig = matcher.llm.structured
    matcher.llm.structured = lambda *a, **k: fake
    try:
        m = matcher.match_trial(_profile(), parsed, {"brief_summary": ""})
    finally:
        matcher.llm.structured = orig
    assert m.eligibility == "ELIGIBLE" and m.assessments[0].passes is True and m.assessments[0].applicable is False
