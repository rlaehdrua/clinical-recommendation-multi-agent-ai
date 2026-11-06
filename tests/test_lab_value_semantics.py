"""수치의 역할: 환자의 실제 측정값만 근거입니다 (합성 문장, LLM 호출 없음).

부정, 목표, 가정, 비교식의 기준값, 참고 범위, 다른 사람의 값은 숫자가 검사명에 붙어 있어도 측정값이 아닙니다.
표현 전체를 해석하지는 않습니다. 역할을 가르지 못하면 보류하는 것이 안전한 쪽입니다.
"""

from __future__ import annotations

import pytest

from ctrec import value_grounding as vg
from ctrec.schemas import QAPair

HB, PLT = "hemoglobin", "platelets"
U, C = "unlocated", "conflict"


def lab(value, text, name=HB, quote=None, **kw):
    return vg.ground_lab(name, value, quote or text, text, **kw)


@pytest.mark.parametrize("text", ["Hb 12 아님", "Hb가 12라는 뜻은 아님", "Hb 12였던 적 없음", "Hb 12가 아닌 것으로 확인됨", "Hb is not 12", "Hb was never 12 g/dL"])
def test_negated_value_is_not_a_measurement(text):
    assert lab(12.0, text) == U


@pytest.mark.parametrize("value,text", [
    (10.0, "target Hb 10"), (12.0, "goal hemoglobin 12"), (10.0, "Hb 10 이상을 목표로 함"), (8.0, "수혈 목표 수치 Hb 8"), (8.0, "transfuse if Hb 8"),
    (10.0, "maintain Hb 10 g/dL"), (9.0, "Hb 유지 목표 9"), (10.0, "threshold Hb 10")])
def test_target_value_is_not_a_measurement(value, text):
    assert lab(value, text) == U


@pytest.mark.parametrize("value,text", [(8.0, "if Hb < 8, transfuse"), (7.0, "Hb가 7이라면"), (10.0, "Hb 10일 경우"), (12.0, "unless Hb reaches 12"), (8.0, "should Hb fall to 8"), (9.0, "Hb 9이면 재검")])
def test_conditional_value_is_not_a_measurement(value, text):
    assert lab(value, text) == U


@pytest.mark.parametrize("value,text", [(9.0, "Hb > 9"), (9.0, "Hb >= 9 g/dL"), (9.0, "Hb ≥ 9"), (9.0, "Hb 9 이상"), (9.0, "Hb 9 미만"), (9.0, "Hb 9 g/dL or higher")])
def test_comparison_threshold_is_not_a_measurement(value, text):
    assert lab(value, text) == U


@pytest.mark.parametrize("value,text", [
    (12.0, "reference range 12-16 g/dL"), (16.0, "reference range 12–16 g/dL"), (12.0, "Hb 정상 범위 12~16"), (16.0, "Hb 정상 범위 12~16"), (12.0, "Hb reference 12 g/dL"),
    (12.0, "normal Hb 12 g/dL"), (9.0, "Plt 150 x10^9/L"), (10.0, "Plt 150 x10^9/L"), (13.0, "Hb 12 g/dL (normal 13-17)")])
def test_reference_range_and_unit_numbers_are_not_a_measurement(value, text):
    assert lab(value, text) == U


def test_measurement_next_to_a_reference_is_kept():
    assert lab(12.0, "Hb 12 g/dL (normal 13-17)") is None
    assert lab(12.0, "Hb 12 g/dL, reference range 13-17") is None
    assert lab(12.0, "Hb 12 g/dL로 정상") is None


@pytest.mark.parametrize("value,name,text,want", [
    (12.0, HB, "Hb 12, Platelets 200", None), (200.0, HB, "Hb 12, Platelets 200", U), (200.0, PLT, "Hb 12, Platelets 200", None),
    (12.0, HB, "Platelets 12. Hb 9", U), (9.0, HB, "Platelets 12. Hb 9", None),
    (12.0, HB, "Hb 12 아님. Platelets 12", U), (12.0, PLT, "Hb 12 아님. Platelets 12", None),
    (4.1, HB, "potassium 4.1, Hb 12", U), (12.0, HB, "potassium 4.1, Hb 12", None), (1.2, HB, "creatinine 1.2, Hb 12", U), (12.0, HB, "creatinine 1.2, Hb 12", None)])
def test_several_labs_keep_their_own_values(value, name, text, want):
    assert lab(value, text, name) == want


PAST_NOW = "과거 Hb 12, 현재 Hb 9"


@pytest.mark.parametrize("text", [PAST_NOW, "이전 Hb 12 g/dL. 최근 Hb 9 g/dL", "previous Hb 12, current Hb 9", "Hb 9 (최근), 지난달 Hb 12"])
def test_stated_current_value_wins_over_the_past_one(text):
    assert lab(9.0, text, known={"9"}) is None
    assert lab(12.0, text, known={"12"}) in (U, C)  # 과거 값만 옮긴 프로필


def test_past_value_alone_is_still_the_only_measurement():
    assert lab(12.0, "이전 검사 Hb 12 g/dL", known={"12"}) is None


def test_undated_different_values_are_a_conflict():
    assert lab(12.0, "Hb 12. Hb 9", known={"12"}) == C
    assert lab(9.0, "Hb 12. Hb 9", known={"9"}) == C


def test_partial_copy_of_dated_values_is_a_conflict():
    text = "Hb 12 (2026-01-01). Hb 9 (2026-03-01)."
    assert lab(9.0, text, quote="Hb 9", known={"9"}) == C
    assert lab(9.0, text, quote="Hb 9", known={"9", "12"}) is None


@pytest.mark.parametrize("value,text,quote", [
    (12.0, "Hb 12 g/dL", None), (12.0, "hemoglobin is 12 g/dL", None), (12.0, "혈색소 12 g/dL", None), (12.0, "혈색소 수치는 12 g/dL입니다", None),
    (12.0, "2026-03-01 Hb 12 g/dL", None), (12.0, "Hb 12 g/dL (2026-03-01 검사)", None), (12.0, "CBC 결과. 수치는 다음과 같음. Hb 12 g/dL", None),
    (12.0, "ECOG 1. Hb 12", "Hb 12"), (12.0, "Hb | 12 | g/dL", None), (12.0, "검사 | 결과\nHb | 12", "Hb | 12"), (12.0, "Hb\t12\tg/dL", None), (12.0, "Hb: 12", "Hb: 12"),
    (12.0, "수혈 없이 Hb 12", None)])
def test_real_measurements_are_accepted(value, text, quote):
    assert lab(value, text, quote=quote) is None


@pytest.mark.parametrize("name,value,text", [("potassium", 4.1, "potassium 4.1 mmol/L"), ("potassium", 4.1, "칼륨 4.1 mmol/L"), ("sodium", 140.0, "Na 나트륨 140 mmol/L"),
                                             ("calcium", 9.2, "calcium 9.2 mg/dL"), ("albumin", 3.5, "albumin 3.5 g/dL"), ("tsh", 0.05, "TSH 0.05 mIU/L"),
                                             ("ferritin", 80.0, "ferritin 80 ng/mL")])
def test_unlisted_labs_link_by_their_own_name(name, value, text):
    assert lab(value, text, name) is None


@pytest.mark.parametrize("name,value,text", [("potassium", 4.1, "체중 4.1 kg"), ("ferritin", 80.0, "체중 80 kg"), ("tsh", 12.0, "Hb 12 g/dL"), ("potassium", 4.1, "target potassium 4.1"),
                                             ("ferritin", 80.0, "ferritin 정상, 체중 80 kg")])
def test_unlisted_labs_without_a_link_are_held(name, value, text):
    assert lab(value, text, name) == U


@pytest.mark.parametrize("field,value,text", [("age", 62, "62세 아님"), ("age", 62, "not 62 years old"), ("sex", "female", "여성 아님"), ("sex", "male", "not male"), ("ecog", 1, "ECOG 1 아님")])
def test_negated_structured_values_are_not_grounded(field, value, text):
    assert vg.ground(field, value, text) == U


def test_partner_sex_is_not_the_patient_sex():
    assert vg.ground("sex", "female", "He has a female partner of childbearing potential") == U


def test_a_value_about_someone_else_is_not_the_patient():
    assert lab(12.0, "아버지 Hb 12 g/dL") == U
    assert lab(12.0, "donor Hb 12 g/dL") == U
    assert vg.ground("age", 62, "62세 아버지와 내원") == U
    assert vg.ground("sex", "male", "남성 보호자와 내원") == U
    assert vg.ground("sex", "female", "여성. 남성 보호자와 내원") is None


def answer(lab_name, value, text, question="혈색소 수치는요?"):
    src = f"기록.\n\n[추가 확인 정보 - 라운드 1]\nQ: {question}\nA: {text}"
    qa = [QAPair(round=1, question_id="Q1", question=question, answer=text, purpose="eligibility", target=["T1:I3"], session_id="S")]
    return vg.ground_lab(lab_name, value, text, src, key="T1:I3", qa=qa)


@pytest.mark.parametrize("text", ["12 아니에요", "Hb 12 아닙니다", "12라면 괜찮대요", "목표가 12예요"])
def test_answers_follow_the_same_roles(text):
    assert answer(HB, 12.0, text) in (U, "hedged")


def test_clear_answer_is_kept():
    assert answer(HB, 12.0, "Hb 12 g/dL입니다") is None
    assert answer(HB, 12.0, "12요") is None
