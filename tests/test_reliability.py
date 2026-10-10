"""대상팀 분석에서 도입한 평가 지표·질문 예산·누락 재요청·파서 무결성 검사 (API 호출 없음)."""

from ctrec import config
from ctrec.agents import matcher
from ctrec.agents.criteria_parser import check_integrity
from ctrec.metrics import eligibility_metrics
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import LLMAssessment, MatcherOutput, ParsedTrial, PatientProfile, QAPair, Rule


def _rule(**kw) -> Rule:
    base = dict(rule_id="I1", kind="inclusion", text="t", category="lab", field=None, operator=None, value=None,
                unit=None, time_window_days=None, structured_evaluable=True, underspecified=False, cohort=None)
    base.update(kw)
    return Rule(**base)


def _parsed(rules) -> ParsedTrial:
    return ParsedTrial(trial_id="T", title="t", target_population="", intervention_summary="", key_conditions=[],
                       cohorts=[], rules=rules, parsing_notes="")


def _profile() -> PatientProfile:
    return PatientProfile(patient_id="P", age=60, sex="female", primary_diagnosis=None, disease_stage=None,
                          diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[],
                          ecog=None, other_facts=[], missing_or_ambiguous=[])


# ------------------------------------------------------------------ 지표
def test_rescue_cleanup_and_safety_metrics():
    gold = {("p", "a"): "ELIGIBLE", ("p", "b"): "ELIGIBLE", ("p", "c"): "INELIGIBLE", ("p", "d"): "INELIGIBLE"}
    text_only = {("p", "a"): "UNCERTAIN", ("p", "b"): "UNCERTAIN", ("p", "c"): "UNCERTAIN", ("p", "d"): "INELIGIBLE"}
    pred = {("p", "a"): "ELIGIBLE", ("p", "b"): "INELIGIBLE", ("p", "c"): "UNCERTAIN", ("p", "d"): "ELIGIBLE"}
    m = eligibility_metrics(gold, pred, text_only)
    assert m["rescue"] == 0.5 and m["rescue_n"] == "1/2"      # a 회복, b 놓침
    assert m["cleanup"] == 0.0 and m["cleanup_n"] == "0/1"    # c는 여전히 보류
    assert m["false_removal"] == 0.5                          # b: 적격을 부적격으로
    assert m["premature_match"] == 0.5                        # d: 부적격을 적격으로


def test_metric_without_cases_is_none():
    m = eligibility_metrics({("p", "a"): "ELIGIBLE"}, {("p", "a"): "ELIGIBLE"}, {("p", "a"): "ELIGIBLE"})
    assert m["cleanup"] is None and m["rescue"] is None and m["premature_match"] is None


# ------------------------------------------------------------------ 질문 예산
def test_question_budget_limits_total_questions(monkeypatch):
    monkeypatch.setattr(config, "MAX_QUESTIONS_PER_ROUND", 5)
    s = Session(case=PatientCase("P", ""), trials={}, answerer=None, tracer=None, question_budget=3)
    assert s.remaining_questions() == 3
    s.qa_log = [QAPair(round=1, question_id="Q1", question="q", answer="a")] * 2
    assert s.remaining_questions() == 1
    s.qa_log *= 2
    assert s.remaining_questions() == 0 and not s.needs_clarification()
    assert s.clarify()["status"] == "skipped"
    # 예산 미지정이면 라운드당 한도만 적용
    assert Session(case=PatientCase("P", ""), trials={}, answerer=None, tracer=None).remaining_questions() == 5


# ------------------------------------------------------------------ 누락 규칙만 재요청
def _assess(rid):
    return LLMAssessment(rule_id=rid, applicable=True, holds="yes", evidence_type="explicit", confidence="high",
                         evidence="e", reasoning="r", missing_info=None)


def test_matcher_retries_only_missing_rule_ids(monkeypatch):
    rules = [_rule(rule_id=r, structured_evaluable=False) for r in ("I1", "I2", "I3")]
    asked = []

    def fake(_cls, *, user, **_):
        ids = [r for r in ("I1", "I2", "I3") if f'"rule_id": "{r}"' in user]
        asked.append(ids)
        if len(asked) == 1:  # 첫 응답: I2 누락 + 존재하지 않는 ID 생성
            return MatcherOutput(assessments=[_assess("I1"), _assess("I3"), _assess("I99")])
        return MatcherOutput(assessments=[_assess(i) for i in ids] + [_assess("I1")])

    monkeypatch.setattr(matcher.llm, "structured", fake)
    monkeypatch.setattr(config, "MATCHER_MISSING_RETRIES", 1)
    m = matcher.match_trial(_profile(), _parsed(rules), {"brief_summary": ""})
    assert asked == [["I1", "I2", "I3"], ["I2"]]
    assert m.retried_rule_ids == ["I2"] and m.eligibility == "ELIGIBLE"
    assert [a.rule_id for a in m.assessments] == ["I1", "I2", "I3"]


def test_matcher_marks_unrecovered_rule_unknown(monkeypatch):
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[_assess("I1")]))
    monkeypatch.setattr(config, "MATCHER_MISSING_RETRIES", 1)
    rules = [_rule(rule_id=r, structured_evaluable=False) for r in ("I1", "I2")]
    m = matcher.match_trial(_profile(), _parsed(rules), {"brief_summary": ""})
    assert m.eligibility == "UNCERTAIN" and "평가 누락" in m.assessments[1].reasoning


# ------------------------------------------------------------------ 파서 무결성 검사
def test_integrity_demotes_changed_number():
    ok = _rule(rule_id="I1", text="ANC >= 1.5 x 10^9/L", field="lab:anc", operator=">=", value="1.5")
    bad = _rule(rule_id="I2", text="ANC >= 1.5 x 10^9/L", field="lab:anc", operator=">=", value="1.0")
    p = _parsed([ok, bad])
    issues = check_integrity(p, {})
    assert len(issues) == 1 and issues[0].startswith("I2")
    assert ok.field == "lab:anc" and bad.field is None and not bad.structured_evaluable
    assert "[코드 검증]" in p.parsing_notes


def test_integrity_demotes_reversed_operator():
    r = _rule(text="Hemoglobin < 9.0 g/dL", field="lab:hemoglobin", operator=">=", value="9.0")
    assert check_integrity(_parsed([r]), {}) and r.field is None


def test_integrity_accepts_ranges_commas_and_structured_age():
    rules = [
        _rule(rule_id="I1", text="Adults aged 18 to <65 years", field="age", operator=">=", value="18"),
        _rule(rule_id="I2", text="Adults aged 18 to <65 years", field="age", operator="<", value="65"),
        _rule(rule_id="I3", text="Platelets ≥ 100,000/microliter", field="lab:platelets", operator=">=", value="100000"),
        _rule(rule_id="I4", text="structured_eligibility: minimum_age", field="age", operator=">=", value="18"),
        _rule(rule_id="I5", text="ECOG performance status 0-1", field="ecog", operator="<=", value="1"),
    ]
    assert check_integrity(_parsed(rules), {"minimum_age": "18 Years"}) == []


def test_integrity_rejects_unit_converted_value():
    r = _rule(text="structured_eligibility: minimum_age=2 Weeks", field="age", operator=">=", value="0.038")
    assert check_integrity(_parsed([r]), {"minimum_age": "2 Weeks"}) and r.field is None
