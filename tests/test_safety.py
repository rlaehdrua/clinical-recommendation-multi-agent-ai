"""0단계 안전 패치 회귀 테스트 (API 호출 없음).

- '해당 없음'(applicable=false)도 명시적 근거가 있어야 인정
- 평가 실패 시험은 NOT_EVALUATED로 남고, 부적격·추천 어느 쪽으로도 처리되지 않음
- 코호트 이름이 cohorts 목록과 어긋나도 서로 다른 코호트 기준이 한꺼번에 적용되지 않음
- 기준일 고정, 날짜를 확정할 수 없는 여러 검사값은 규칙 엔진이 판정하지 않음
"""

import pytest

from ctrec import config
from ctrec.agents import matcher, recommender
from ctrec.agents.answerers import NoAnswerer
from ctrec.agents.matcher import _enforce_evidence, _select_cohort
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import LabValue, LLMAssessment, MatcherOutput, ParsedTrial
from ctrec.tools import rule_engine
from ctrec.trace import Tracer
from test_offline import _a, _profile, _rule
from test_recommender import _match


# ---------------------------------------------------------------- N/A 우회 차단

def _na(**kw):
    base = dict(rule_id="I9", applicable=False, holds="no", evidence_type="explicit", confidence="high",
                evidence="60세 여성", reasoning="남성 대상 조건", missing_info=None)
    base.update(kw)
    return LLMAssessment(**base)


@pytest.mark.parametrize("kw", [
    {"evidence_type": "inferred"},
    {"evidence_type": "absent"},
    {"evidence": "근거 없음"},
    {"evidence": "  "},
])
def test_not_applicable_requires_explicit_evidence(kw):
    out = _enforce_evidence(_na(**kw))
    assert out.applicable is True and out.holds == "unknown" and out.missing_info


def test_not_applicable_with_explicit_evidence_kept():
    out = _enforce_evidence(_na())
    assert out.applicable is False


def test_unsupported_not_applicable_makes_trial_uncertain(monkeypatch):
    """'이전 전신치료를 받은 환자는...' + 치료력 미상 -> N/A 금지, UNCERTAIN (설계 §10)."""
    rule = _rule(rule_id="I9", text="For patients who previously received systemic therapy: washout 4 weeks",
                 category="treatment_history", structured_evaluable=False)
    parsed = ParsedTrial(trial_id="T", title="t", target_population="", intervention_summary="", key_conditions=[],
                         cohorts=[], rules=[rule], parsing_notes="")
    fake = MatcherOutput(assessments=[_na(evidence_type="absent", evidence="근거 없음", reasoning="치료력 언급 없음")])
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: fake)
    m = matcher.match_trial(_profile(), parsed, {"brief_summary": ""}, reference_date="2026-10-05")
    assert m.eligibility == "UNCERTAIN" and m.assessments[0].passes is None


# ---------------------------------------------------------------- 평가 실패 = NOT_EVALUATED

def _session(trials):
    return Session(case=PatientCase(patient_id="P", raw_text="x"), trials=trials,
                   answerer=NoAnswerer(), tracer=Tracer(None, verbose=False), reference_date="2026-10-05")


def test_failed_trial_recorded_as_not_evaluated(monkeypatch):
    from ctrec.agents import criteria_parser
    ok_rule = _rule(field="age", operator=">=", value="18")
    ok = ParsedTrial(trial_id="OK", title="t", target_population="", intervention_summary="", key_conditions=[],
                     cohorts=[], rules=[ok_rule], parsing_notes="")

    def parse(trial, **k):
        if trial["trial_id"] == "BAD_PARSE":
            raise RuntimeError("parser boom")
        return ok.model_copy(update={"trial_id": trial["trial_id"]})

    real_match = matcher.match_trial

    def match(profile, parsed, trial, **k):
        if parsed.trial_id == "BAD_MATCH":
            raise RuntimeError("matcher boom")
        return real_match(profile, parsed, trial, **k)

    monkeypatch.setattr(criteria_parser, "parse_trial", parse)
    monkeypatch.setattr(matcher, "match_trial", match)
    trials = {t: {"trial_id": t, "overall_status": "RECRUITING"} for t in ("OK", "BAD_PARSE", "BAD_MATCH")}
    s = _session(trials)
    s.profile = _profile()
    s.match()

    assert set(s.matches) == {"OK", "BAD_PARSE", "BAD_MATCH"}
    assert s.matches["OK"].eligibility == "ELIGIBLE"
    for tid, stage in (("BAD_PARSE", "파싱"), ("BAD_MATCH", "판정")):
        m = s.matches[tid]
        assert m.eligibility == "NOT_EVALUATED" and stage in m.evaluation_error
    # 확인 질문·추천 후보에서 제외
    assert [m.trial_id for m in s.open_candidates()] == ["OK"]
    assert len(s.errors) == 2


def test_not_evaluated_not_ranked_and_excluded_with_reason():
    trials = {"A": {"overall_status": "RECRUITING"}, "F": {"overall_status": "RECRUITING"}}
    f = _match("F", "NOT_EVALUATED").model_copy(update={"evaluation_error": "기준 파싱 실패: boom"})
    draft = recommender.RecommendationDraft(patient_id="P", ranked=[], excluded=[], overall_comment="")
    rec = recommender._validate(draft, "P", [_match("A", "ELIGIBLE"), f], trials)
    assert [r.trial_id for r in rec.ranked] == ["A"]
    (ex,) = rec.excluded
    assert ex.trial_id == "F" and "부적격 아님" in ex.reason


def test_no_recommendation_reason_does_not_call_failure_ineligible(monkeypatch):
    monkeypatch.setattr(recommender.llm, "structured", lambda *a, **k: pytest.fail("LLM 호출 불필요"))
    trials = {"F": {"overall_status": "RECRUITING"}, "B": {"overall_status": "RECRUITING"}}
    rec = recommender.recommend(_profile(), [_match("F", "NOT_EVALUATED"), _match("B", "INELIGIBLE")], trials)
    assert rec.ranked == [] and "F" in rec.no_recommendation_reason and "부적격 아님" in rec.no_recommendation_reason
    rec = recommender.recommend(_profile(), [_match("F", "NOT_EVALUATED")], trials)
    assert "평가에 실패" in rec.no_recommendation_reason


# ---------------------------------------------------------------- 코호트

def test_cohort_label_mismatch_does_not_merge_cohorts():
    """파서가 cohorts 목록과 다른 이름을 규칙에 적어도 코호트별로 따로 판정."""
    common = _a(True)
    a = _a(True).model_copy(update={"rule_id": "I2", "cohort": "Cohort A"})
    b = _a(False).model_copy(update={"rule_id": "I3", "cohort": "Cohort B"})
    chosen, cohort, results = _select_cohort([common, a, b], ["Part 1", "Part 2"])
    assert cohort == "Cohort A" and results == {"Cohort A": "ELIGIBLE", "Cohort B": "INELIGIBLE"}
    assert b not in chosen


def test_cohort_tie_break_is_deterministic():
    """같은 적격성이면 판정 불가가 적은 코호트, 그다음 선언 순서."""
    a_unknown = _a(None).model_copy(update={"rule_id": "I2", "cohort": "A"})
    b_unknown = _a(None).model_copy(update={"rule_id": "I3", "cohort": "B"})
    b_known = _a(True).model_copy(update={"rule_id": "I4", "cohort": "B"})
    assert _select_cohort([a_unknown, b_known], ["A", "B"])[1] == "B"
    assert _select_cohort([a_unknown, b_unknown], ["A", "B"])[1] == "A"
    assert _select_cohort([b_unknown, a_unknown], ["B", "A"])[1] == "B"


# ---------------------------------------------------------------- 기준일 / 검사 날짜

def test_reference_date_fixed_per_session(monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_DATE", "2026-01-15")
    assert config.reference_date() == "2026-01-15"
    s = Session(case=PatientCase(patient_id="P", raw_text="x"), trials={}, answerer=NoAnswerer(),
                tracer=Tracer(None, verbose=False))
    assert s.reference_date == "2026-01-15"


def test_reference_date_passed_to_matcher(monkeypatch):
    seen = {}

    def fake(model_cls, *, system, user, **k):
        seen["user"] = user
        return MatcherOutput(assessments=[])

    monkeypatch.setattr(matcher.llm, "structured", fake)
    parsed = ParsedTrial(trial_id="T", title="t", target_population="", intervention_summary="", key_conditions=[],
                         cohorts=[], rules=[_rule(structured_evaluable=False)], parsing_notes="")
    matcher.match_trial(_profile(), parsed, {"brief_summary": ""}, reference_date="2025-12-31")
    assert "기준일: 2025-12-31" in seen["user"]


def _lab(value, date):
    return LabValue(name="hemoglobin", value=value, unit="g/dL", date=date, source_quote="q")


def _hb_rule():
    return _rule(field="lab:hemoglobin", operator=">=", value="9", unit="g/dL", category="lab")


def test_lab_latest_by_parsed_date():
    p = _profile(labs=[_lab(8.0, "2026-09-01"), _lab(10.0, "2026-10-01"), _lab(7.0, "2026-08-15")])
    r = rule_engine.evaluate(_hb_rule(), p)
    assert r.holds == "yes" and "10.0" in r.evidence


@pytest.mark.parametrize("dates", [("Aug 2026", "2026-09-01"), (None, "2026-09-01"), ("2026-09-01", "2026-09-01")])
def test_lab_ambiguous_latest_defers_to_llm(dates):
    p = _profile(labs=[_lab(8.0, dates[0]), _lab(10.0, dates[1])])
    assert rule_engine.evaluate(_hb_rule(), p) is None


def test_lab_same_value_without_dates_still_evaluated():
    p = _profile(labs=[_lab(10.0, None), _lab(10.0, "Aug 2026")])
    assert rule_engine.evaluate(_hb_rule(), p).holds == "yes"
