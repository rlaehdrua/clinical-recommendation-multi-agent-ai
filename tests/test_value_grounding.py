"""구조화 값의 원문 근거 판별. 정상 표현을 과하게 막지 않는지 고정합니다 (합성 문장, LLM 호출 없음).

처음 입력 48개 표현은 이전 설계 v3가 8개를 과하게 막았던 표본입니다. 아래에서 막는 것은 승인된 정책의 보류뿐입니다.
"""

from __future__ import annotations

import pytest

from ctrec import value_grounding as vg
from ctrec.schemas import QAPair

ACCEPTED = [("age", 62, t) for t in [
    "62세 여성", "만 62세", "62살", "62 yrs", "62 years old", "62-year-old woman", "62 y/o", "62yo", "62YO", "age 62",
    "Age: 62", "나이 62", "연령 62", "sixty-two years old", "62F", "62/F", "62 F", "육십이세"]]
ACCEPTED += [("sex", "female", t) for t in ["62세 여성", "여자", "female", "a woman", "62F", "62/F", "성별: 여", "여아"]]
ACCEPTED += [("sex", "male", t) for t in ["남성", "남자", "male", "a man", "62M", "성별: 남"]]
ACCEPTED += [("ecog", 1, t) for t in ["ECOG 1", "ECOG PS 1", "ECOG:1", "ECOG performance status of 1", "PS 1", "수행 상태 1",
                                      "활동도 1", "ECOG one"]]
POLICY_HELD = [("sex", "female", t) for t in ["F", "여", "she has NSCLC", "그녀는"]]
POLICY_HELD += [("sex", "male", t) for t in ["M", "남", "he has NSCLC"]]
POLICY_HELD += [("ecog", 1, "ECOG 0-1")]


@pytest.mark.parametrize("field,value,text", ACCEPTED)
def test_stated_values_in_the_record_are_accepted(field, value, text):
    assert vg.ground(field, value, text) is None


@pytest.mark.parametrize("field,value,text", POLICY_HELD)
def test_policy_holds_in_the_record(field, value, text):
    assert vg.ground(field, value, text) == "unlocated"


def test_the_sample_is_the_48_expressions():
    assert len(ACCEPTED) + len(POLICY_HELD) == 48


def answer(field, value, text, question="확인 질문", key="T1:I1", target="T1:I1"):
    src = f"기록.\n\n[추가 확인 정보 - 라운드 1]\nQ: {question}\nA: {text}"
    qa = [QAPair(round=1, question_id="Q1", question=question, answer=text, purpose="eligibility", target=[target], session_id="S")]
    return vg.ground(field, value, src, key=key, qa=qa)


@pytest.mark.parametrize("field,value,text", [
    ("age", 62, "62세입니다"), ("age", 62, "저는 62세예요"), ("age", 62, "62살이요"), ("age", 62, "62"), ("age", 62, "62요"),
    ("age", 62, "예순둘이에요"), ("age", 62, "62세인데 혈색소는 몰라요"), ("age", 62, "확실히 62세입니다"), ("age", 62, "주민등록상 62세입니다"),
    ("sex", "female", "여자요"), ("sex", "female", "여성입니다"), ("ecog", 1, "ECOG 1이에요"), ("ecog", 1, "1"), ("ecog", 1, "1입니다"),
    ("ecog", 1, "활동도는 1이요")])
def test_clear_answers_are_accepted(field, value, text):
    assert answer(field, value, text) is None


@pytest.mark.parametrize("field,value,text", [
    ("age", 62, "62세쯤이요"), ("age", 62, "아마 62세"), ("age", 62, "62세일 거예요"), ("age", 62, "62세요?"), ("age", 62, "글쎄요, 62세"),
    ("age", 62, "모르겠어요 62세인가"), ("sex", "female", "여자인 것 같아요"), ("ecog", 1, "아마 ECOG 1")])
def test_hedged_answers_are_held(field, value, text):
    assert answer(field, value, text) == "hedged"


@pytest.mark.parametrize("text", ["모름", "", "   ", "확인 불가", "답변 거부"])
def test_non_answers_are_unlocated(text):
    assert answer("age", 62, text) == "unlocated"


def test_bare_number_needs_a_question_about_it():
    assert answer("age", 62, "62", question="체중이 얼마인가요?", target="T1:I9") == "unlocated"
    assert answer("age", 62, "62", question="나이가 어떻게 되시나요?", target="T1:I9") is None


@pytest.mark.parametrize("text,value", [("F", "female"), ("여", "female"), ("여요", "female"), ("M", "male"), ("남", "male")])
def test_bare_sex_needs_a_sex_question(text, value):
    assert answer("sex", value, text, question="성별이 어떻게 되시나요?", target="T1:I9") is None
    assert answer("sex", value, text, question="임신 중이신가요?") == "unlocated"


def test_physician_reference_answer_is_not_evidence():
    src = "기록.\n\n[추가 확인 정보 - 라운드 1]\nQ: 참고로 나이는요?\nA: 62세"
    qa = [QAPair(round=1, question_id="Q1", question="참고로 나이는요?", answer="62세", purpose="physician_reference",
                 target=["T1:I1"], session_id="S")]
    assert vg.ground("age", 62, src, key="T1:I1", qa=qa) == "unlocated"


@pytest.mark.parametrize("text,value,expected", [
    ("1964년생 환자", 62, None), ("1964년생 환자", 61, None), ("1964년생 환자", 60, "unlocated"), ("born in 1964", 62, None),
    ("DOB 1964-03-01", 62, None), ("생년월일 1964.03.01", 62, None), ("1964년 진단", 62, "unlocated"),
    ("sixty-two years old", 63, "unlocated"), ("육십이세", 63, "unlocated"), ("예순세 살", 63, None), ("쉰다섯 살", 55, None),
    ("seventy years old", 70, None), ("twelve years old", 12, None)])
def test_numeral_and_birth_year_forms(text, value, expected):
    assert vg.ground("age", value, text, reference_date="2026-11-06") == expected


def test_unit_must_match():
    assert vg.ground("age", 3, "3개월 아기", unit="months") is None
    assert vg.ground("age", 3, "3세 아동", unit="months") == "unlocated"
    assert vg.ground("age", 62, "62일 전 수술") == "unlocated"


@pytest.mark.parametrize("lab,value,quote,text,expected", [
    ("hemoglobin", 12.0, "Hb 12 g/dL", "Hb 12 g/dL", None),
    ("hemoglobin", 12.0, "Hb 12.0 g/dL", "Hb 12.0 g/dL", None),
    ("hemoglobin", 12.5, "혈색소 12.5 g/dL", "혈색소 12.5 g/dL 입니다", None),
    ("platelets", 150000, "혈소판 150,000 /uL", "혈소판 150,000 /uL", None),
    ("hemoglobin", 12.0, "Hb 12 g/dL", "Platelets 200", "unlocated"),
    ("hemoglobin", 13.0, "Hb 12 g/dL", "Hb 12 g/dL", "unlocated"),
    ("hemoglobin", 12.0, "Platelets 12 g/dL", "Platelets 12 g/dL", "unlocated"),
    ("hemoglobin", 12.0, "수치는 정상 범위", "수치는 정상 범위", "unlocated"),
    ("hemoglobin", 12.0, None, "Hb 12 g/dL", "unlocated"),
    ("hba1c", 7.0, "HbA1c 7 %", "HbA1c 7 %", None),
    ("hemoglobin", 7.0, "HbA1c 7 %", "HbA1c 7 %", "unlocated")])
def test_lab_quote_needs_the_same_lab_and_value(lab, value, quote, text, expected):
    assert vg.ground_lab(lab, value, quote, text) == expected


def test_lab_bare_answer_needs_a_question_about_it():
    def bare(question, target):
        src = f"기록.\n\n[추가 확인 정보 - 라운드 1]\nQ: {question}\nA: 12"
        qa = [QAPair(round=1, question_id="Q1", question=question, answer="12", purpose="eligibility", target=[target], session_id="S")]
        return vg.ground_lab("hemoglobin", 12.0, "12", src, key="T1:I3", qa=qa)
    assert bare("혈색소 수치는요?", "T1:I9") is None
    assert bare("확인 질문", "T1:I3") is None
    assert bare("체중이 얼마인가요?", "T1:I9") == "unlocated"


def test_conflict_is_reported_for_answers_only():
    two = "62세.\n\n[추가 확인 정보 - 라운드 1]\nQ: 나이?\nA: 70세입니다"
    assert vg.ground("age", 62, two) == "conflict"
    assert vg.ground("age", 62, "58세에 진단. 현재 62세.") is None
    both = "기록.\n\n[추가 확인 정보 - 라운드 1]\nQ: 나이?\nA: 62세\n\n[추가 확인 정보 - 라운드 2]\nQ: 다시 나이?\nA: 63세"
    assert vg.ground("age", 62, both) == "conflict"


def test_nothing_to_check():
    assert vg.ground("age", None, "x") is None
    assert vg.ground("age", 62, None) is None
    assert vg.ground_lab("hemoglobin", 12.0, "q", None) is None
