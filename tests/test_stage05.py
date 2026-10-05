"""0.5단계 회귀 테스트 (API 호출 없음). 실제 실행 결과에서 발견된 사례를 재현합니다.

- 나이 단위: 영아(S007)를 '0세'로 주(week) 기준과 비교하던 문제
- 인용 근거 검증: "medications: []" 같은 필드 덤프를 근거로 확정하던 문제(S009)
- underspecified / 제외 절차 기준의 미확인이 ELIGIBLE을 막지 않던 문제(S005)
- lab 단위 한쪽 누락 시 숫자만 비교하던 문제
"""

import pytest

from ctrec.agents import matcher
from ctrec.agents.answerers import NoAnswerer
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import LabValue, LLMAssessment, MatcherOutput, ParsedTrial
from ctrec.tools import rule_engine
from ctrec.trace import Tracer
from test_offline import _a, _profile, _rule


# ---------------------------------------------------------------- 나이 단위

def _age(op, value, unit):
    return _rule(field="age", operator=op, value=value, unit=unit)


@pytest.mark.parametrize("op,value,unit", [
    ("<", "12", "weeks"),      # NCT00195949 I1 "less than 12 weeks"
    (">", "12", "weeks"),      # NCT00195949 E1 "greater than 12 weeks"
    (">=", "0.038", "years"),  # NCT02415049 I3 (파서가 2주를 년으로 환산)
    ("<=", "0.192", None),     # NCT02415049 I4 (10주, 단위 없음 = 년)
])
def test_infant_age_zero_years_is_not_decided(op, value, unit):
    """S007: 나이가 '0세'뿐이면 주 단위 기준은 확정할 수 없음 (예전: 0 > 12 -> False로 통과)."""
    assert rule_engine.evaluate(_age(op, value, unit), _profile(age=0)) is None


@pytest.mark.parametrize("op,value,unit,expected", [
    ("<", "12", "weeks", "no"),   # 3개월 = 91~122일 > 84일
    (">", "12", "weeks", "yes"),
    (">=", "2", "weeks", "yes"),
    ("<=", "10", "weeks", "no"),
    ("<", "1", "years", "yes"),
])
def test_infant_age_in_months(op, value, unit, expected):
    p = _profile(age=0, age_value=3, age_unit="months")
    assert rule_engine.evaluate(_age(op, value, unit), p).holds == expected


def test_infant_age_boundary_inside_interval_is_not_decided():
    # 2개월 = 61~91일, 기준 12주 = 84일 -> 구간 안에서 갈림
    p = _profile(age=0, age_value=2, age_unit="months")
    assert rule_engine.evaluate(_age("<", "12", "weeks"), p) is None


@pytest.mark.parametrize("op,value,unit,expected", [
    (">=", "18", "Years", "yes"),
    ("<=", "62", None, "yes"),  # 같은 단위의 정수 기준은 관례대로 만 나이 비교 (62세는 '62세 이하')
    ("<", "62", "years", "no"),
    (">=", "780", "months", "no"),  # 62세 = 744~756개월
])
def test_adult_age(op, value, unit, expected):
    assert rule_engine.evaluate(_age(op, value, unit), _profile(age=62)).holds == expected


def test_unknown_age_unit_defers():
    assert rule_engine.evaluate(_age(">=", "18", "fortnights"), _profile()) is None


# ---------------------------------------------------------------- 인용 근거 검증

SOURCE = "62-year-old woman with NSCLC. Hepatitis B/C negative (2023). Currently taking metformin 500 mg."


def _trial(rule):
    return ParsedTrial(trial_id="T", title="t", target_population="", intervention_summary="", key_conditions=[],
                       cohorts=[], rules=[rule], parsing_notes="")


def _run(monkeypatch, assessment, source=SOURCE, **rule_kw):
    rule = _rule(rule_id="E5", kind="exclusion", text="Active hepatitis B or C", category="comorbidity",
                 structured_evaluable=False, **rule_kw)
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[assessment]))
    return matcher.match_trial(_profile(), _trial(rule), {"brief_summary": ""},
                               reference_date="2026-10-05", source_text=source)


def _la(**kw):
    base = dict(rule_id="E5", applicable=True, holds="no", evidence_type="explicit", confidence="high",
                evidence="", reasoning="r", missing_info=None)
    base.update(kw)
    return LLMAssessment(**base)


@pytest.mark.parametrize("evidence", [
    "medications: []",                # S009: 필드 덤프
    "age 62, sex female",             # SYN-DEMO-A: 프로필 요약
    "B형/C형 간염 음성",                # 원문은 영어인데 번역 인용
    "Hepatitis negative per records",  # 지어낸 인용
])
def test_unverifiable_quote_is_not_decisive(monkeypatch, evidence):
    m = _run(monkeypatch, _la(evidence=evidence))
    a = m.assessments[0]
    assert a.holds == "unknown" and a.passes is None and "원문에서 확인되지 않음" in a.reasoning
    assert m.eligibility == "UNCERTAIN"


@pytest.mark.parametrize("evidence", [
    '"Hepatitis B/C negative (2023)"',
    "hepatitis b/c   NEGATIVE / 62세 여성",  # 한 조각이라도 원문에 있으면 인정 (공백·대소문자 무시)
])
def test_verbatim_quote_is_accepted(monkeypatch, evidence):
    m = _run(monkeypatch, _la(evidence=evidence))
    assert m.assessments[0].holds == "no" and m.eligibility == "ELIGIBLE"


def test_not_applicable_needs_quote_in_source(monkeypatch):
    m = _run(monkeypatch, _la(applicable=False, evidence="male patient"))
    assert m.assessments[0].applicable is True and m.assessments[0].passes is None


def test_quote_check_uses_clarification_answers(monkeypatch):
    """확인 질문 답변은 raw_text에 추가되므로 인용 근거로 쓸 수 있음."""
    source = SOURCE + "\n\n[추가 확인 정보 - 라운드 1]\nQ: 간염 치료 중인가요?\nA: 간염 치료 받은 적 없어요"
    m = _run(monkeypatch, _la(evidence="간염 치료 받은 적 없어요"), source=source)
    assert m.assessments[0].holds == "no"


def test_too_short_fragment_not_enough():
    assert not matcher.evidence_in_source("no", "no history of hepatitis")
    assert matcher.evidence_in_source("no history of hepatitis", "Patient has NO history  of hepatitis.")


# ---------------------------------------------------------------- 미확인 항목이 ELIGIBLE을 막는 범위

def test_underspecified_unknown_blocks_eligible_but_is_not_asked():
    phys = _a(None, "comorbidity", underspecified=True)
    assert matcher.decide([_a(True), phys]) == "UNCERTAIN"
    assert matcher.needs_physician_review(phys) and not matcher.is_askable_unknown(phys)


def test_exclusion_logistics_unknown_blocks_inclusion_logistics_does_not():
    incl = _a(None, "consent_or_logistics")
    excl = incl.model_copy(update={"kind": "exclusion", "rule_id": "E1"})
    assert matcher.decide([_a(True), incl]) == "ELIGIBLE"
    assert matcher.decide([_a(True), excl]) == "UNCERTAIN" and matcher.is_askable_unknown(excl)


def test_only_physician_items_left_does_not_trigger_questions(monkeypatch):
    """의사 확인 항목만 남은 UNCERTAIN 시험으로 확인 질문 라운드를 돌리지 않음 (비용 낭비 방지)."""
    from ctrec.schemas import TrialMatch
    s = Session(case=PatientCase(patient_id="P", raw_text="x"), trials={"T": {"overall_status": "RECRUITING"}},
                answerer=NoAnswerer(), tracer=Tracer(None, verbose=False), reference_date="2026-10-05")
    phys = _a(None, "medication", underspecified=True)
    s.matches["T"] = TrialMatch(patient_id="P", trial_id="T", eligibility="UNCERTAIN", assessments=[_a(True), phys],
                                n_pass=1, n_fail=0, n_unknown=1, summary="")
    assert s.uncertain() == [] and s.open_candidates()  # 질문 대상은 아니지만 추천 후보로는 남음
    assert s.pending_physician_review()  # 의사 참고용 질문은 한 번 수집


# ---------------------------------------------------------------- lab 단위

def _hb(value, unit):
    return _profile(labs=[LabValue(name="hemoglobin", value=value, unit=unit, date="2026-09-01", source_quote="q")])


def test_lab_unit_missing_on_one_side_defers():
    rule = _rule(field="lab:hemoglobin", operator=">=", value="9", unit="g/dL", category="lab")
    assert rule_engine.evaluate(rule, _hb(100, None)) is None  # 100 g/L일 수도 있음
    rule_no_unit = rule.model_copy(update={"unit": None})
    assert rule_engine.evaluate(rule_no_unit, _hb(10, "g/dL")) is None
    assert rule_engine.evaluate(rule_no_unit, _hb(10, None)).holds == "yes"  # 양쪽 다 없으면 비교


def test_lab_equivalent_unit_spellings():
    rule = _rule(field="lab:platelets", operator=">=", value="100", unit="x10^9/L", category="lab")
    p = _profile(labs=[LabValue(name="platelets", value=150, unit="10³/µL", date="2026-09-01", source_quote="q")])
    assert rule_engine.evaluate(rule, p).holds == "yes"
    p2 = _profile(labs=[LabValue(name="plt", value=150, unit="/mm3", date="2026-09-01", source_quote="q")])
    assert rule_engine.evaluate(rule.model_copy(update={"unit": "cells/µL"}), p2).holds == "yes"


def test_profile_field_reference_is_accepted_but_empty_list_is_not(monkeypatch):
    """S001·S006: 남성 환자의 임신 제외 기준 'sex: male' 근거는 인정, S009 'medications: []'는 불인정."""
    m = _run(monkeypatch, _la(evidence='"sex": "female"'))  # _profile()은 62세 여성
    assert m.assessments[0].holds == "no"
    m = _run(monkeypatch, _la(evidence="age: 62, ecog: 1"))
    assert m.assessments[0].holds == "no"
    m = _run(monkeypatch, _la(evidence='"sex": "male"'))  # 프로필과 다른 값
    assert m.assessments[0].holds == "unknown"
    m = _run(monkeypatch, _la(evidence="sex: female, medications: []"))  # 덤프 섞임 -> 원문 인용도 아님
    assert m.assessments[0].holds == "no"  # sex 필드 참조가 프로필과 일치하므로 인정 (medications 키는 참조 대상 아님)


def test_sex_all_passes_even_if_patient_sex_unknown():
    """S007: 성별 미상 영아도 '모든 성별' 기준은 충족."""
    r = rule_engine.evaluate(_rule(field="sex", operator="==", value="all"), _profile(sex="unknown"))
    assert r.holds == "yes"
    assert rule_engine.evaluate(_rule(field="sex", operator="==", value="female"), _profile(sex="unknown")) is None
