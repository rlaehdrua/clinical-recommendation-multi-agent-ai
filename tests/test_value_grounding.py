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


@pytest.mark.parametrize("text", ["글쎄요", "글쎄", "모름. 기억 안 남", "모르겠어요, 기억이 안 나요", "글쎄요, 잘 기억이 안 나네요", "잘 기억이 안 나요"])
def test_compound_non_answers_are_recognized(text):
    from ctrec.question_contract import non_answer
    assert non_answer(text)


@pytest.mark.parametrize("text", ["글쎄요, 62세", "모르겠어요 62세인가", "BCG 치료를 거부했습니다", "62세입니다", "손목 수술은 했는데 종류는 몰라요"])
def test_answers_with_content_are_not_non_answers(text):
    from ctrec.question_contract import non_answer
    assert not non_answer(text)


LINE = "Synthetic 45-year-old female. ECOG 0. hemoglobin 12 g/dL"


@pytest.mark.parametrize("quote", [LINE, "ECOG 0. hemoglobin 12 g/dL", "hemoglobin 12 g/dL", "ECOG 0, hemoglobin 12 g/dL"])
def test_quote_across_sentences_finds_the_lab_value(quote):
    text = LINE if quote != "ECOG 0, hemoglobin 12 g/dL" else quote
    assert vg.ground_lab("hemoglobin", 12.0, quote, text) is None


@pytest.mark.parametrize("value,quote,text", [
    (12.0, "Platelets 12. Hb 9 g/dL", "Platelets 12. Hb 9 g/dL"),  # 인접한 다른 검사값
    (9.0, "Hb 12 g/dL, Platelets 9", "Hb 12 g/dL, Platelets 9"),
    (200.0, "Hb 12 g/dL, Platelets 200", "Hb 12 g/dL, Platelets 200"),
    (5.0, "Hb low. ANC 5 g/dL", "Hb low. ANC 5 g/dL"),
    (4.1, "Hb low, potassium 4.1", "Hb low, potassium 4.1"),
    (9.0, "Hb 12 g/dL (2026-09-01)", "Hb 12 g/dL (2026-09-01)"),  # 날짜의 숫자
    (13.0, "Hb 12 g/dL (normal 13-17)", "Hb 12 g/dL (normal 13-17)"),  # 참고 범위
    (10.0, "Hb 12 x10^9/L", "Hb 12 x10^9/L"),  # 단위의 숫자
    (12.0, "Hb는 측정함", "Hb는 측정함. 체중 12 kg"),  # 인용에 값이 없음
    (12.0, "체중 12 kg입니다", "체중 12 kg입니다"),  # 값은 있지만 검사명이 다름
    (12.0, "Hb는 측정했고 다른 메모", "Hb는 측정했고 다른 메모 12"),  # 값이 인용 밖
])
def test_neighbouring_numbers_are_not_the_lab_value(value, quote, text):
    assert vg.ground_lab("hemoglobin", value, quote, text) == "unlocated"


@pytest.mark.parametrize("value,quote,text", [
    (12.0, "Hb: 12 g/dL", "Hb: 12 g/dL"), (12.0, "hemoglobin was 12 g/dL", "hemoglobin was 12 g/dL"),
    (12.0, "12 g/dL", "Hb 12 g/dL"), (12.0, "혈색소 수치는 12 g/dL", "혈색소 수치는 12 g/dL"), (12.0, "12 g/dL hemoglobin", "12 g/dL hemoglobin"),
    (12.0, "Hb 12 g/dL, Platelets 200", "Hb 12 g/dL, Platelets 200"), (12.0, "Hb on 2026-09-01 was 12", "Hb on 2026-09-01 was 12"),
    (12.0, "Hb 9/1 12 g/dL", "Hb 9/1 12 g/dL"), (12.0, "Hb 12 (normal 13-17)", "Hb 12 (normal 13-17)")])
def test_linked_lab_values_are_accepted(value, quote, text):
    assert vg.ground_lab("hemoglobin", value, quote, text) is None


def test_comma_number_stays_one_value():
    assert vg.ground_lab("platelets", 150000, "Platelets 150,000 /uL", "Platelets 150,000 /uL") is None
    assert vg.ground_lab("platelets", 150, "Platelets 150,000 /uL", "Platelets 150,000 /uL") == "unlocated"


def test_unit_must_agree_when_both_are_stated():
    assert vg.ground_lab("hemoglobin", 12.0, "Hb 12 g/L", "Hb 12 g/L", unit="g/dL") == "unlocated"
    assert vg.ground_lab("hemoglobin", 12.0, "Hb 12 g/dL", "Hb 12 g/dL", unit="g/dl") is None
    assert vg.ground_lab("hemoglobin", 12.0, "Hb 12", "Hb 12", unit="g/dL") is None  # 원문에 단위가 없으면 비교할 수 없음
    assert vg.ground_lab("platelets", 150.0, "Plt 150 x10^3/uL", "Plt 150 x10^3/uL", unit="x10^9/L") is None  # 같은 단위의 다른 표기


TIMES = "Hb 10 g/dL (2026-01-01). Hb 12 g/dL (2026-03-01)."


def test_several_time_points_are_each_located_when_the_profile_holds_them():
    assert vg.ground_lab("hemoglobin", 12.0, "Hb 12 g/dL", TIMES, known={"10", "12"}) is None
    assert vg.ground_lab("hemoglobin", 10.0, "Hb 10 g/dL", TIMES, known={"10", "12"}) is None


@pytest.mark.parametrize("value,kept", [(12.0, {"12"}), (10.0, {"10"})])
def test_a_record_value_missing_from_the_profile_is_a_conflict(value, kept):
    """프로필이 여러 시점 값 중 하나만 옮기면 어느 값이 현재인지 판정할 수 없음."""
    assert vg.ground_lab("hemoglobin", value, f"Hb {value:g} g/dL", TIMES, known=kept) == "conflict"


def test_unlisted_lab_keeps_working_without_a_known_name():
    assert vg.ground_lab("potassium", 4.1, "칼륨 4.1 mmol/L", "칼륨 4.1 mmol/L") is None
    assert vg.ground_lab("potassium", 12.0, "Hb 12 g/dL", "Hb 12 g/dL") == "unlocated"  # 다른 검사의 수치


def test_unknown_answer_does_not_ground_a_lab():
    src = "기록.\n\n[추가 확인 정보 - 라운드 1]\nQ: 혈색소 수치는요?\nA: 잘 모르겠어요"
    qa = [QAPair(round=1, question_id="Q1", question="혈색소 수치는요?", answer="잘 모르겠어요", purpose="eligibility", target=["T1:I3"], session_id="S")]
    assert vg.ground_lab("hemoglobin", 12.0, "잘 모르겠어요", src, key="T1:I3", qa=qa) == "unlocated"


def test_hedged_lab_answer_is_held_only_for_the_hedged_value():
    src = "기록.\n\n[추가 확인 정보 - 라운드 1]\nQ: 수치는요?\nA: 혈소판은 200이고 혈색소는 12쯤인 것 같아요"
    assert vg.ground_lab("hemoglobin", 12.0, "혈색소는 12쯤인 것 같아요", src) == "hedged"
    assert vg.ground_lab("platelets", 200.0, "혈소판은 200이고", src) == "hedged"  # '이고'는 절 경계가 아니라 함께 보류됨 (안전한 쪽)
    mixed = "기록.\n\n[추가 확인 정보 - 라운드 1]\nQ: 수치는요?\nA: 혈소판은 200인데 혈색소는 12쯤인 것 같아요"
    assert vg.ground_lab("platelets", 200.0, "혈소판은 200인데", mixed) is None
    assert vg.ground_lab("hemoglobin", 12.0, "혈색소는 12쯤인 것 같아요", mixed) == "hedged"


def test_short_quote_is_found_and_still_needs_the_link():
    assert vg.ground_lab("hemoglobin", 12.0, "Hb 12", "ECOG 1. Hb 12") is None
    assert vg.ground_lab("hemoglobin", 12.0, "12", "ECOG 1. Hb 12") is None
    assert vg.ground_lab("hemoglobin", 12.0, "12", "ECOG 1. 체중 12") == "unlocated"
    assert vg.ground_lab("hemoglobin", 12.0, "Hb 12", "ECOG 1. Hb 13") == "unlocated"
