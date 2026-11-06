"""값이 적혀 있어도 환자의 현재 값이 아니면 근거가 아님: 다른 사람, 기준값, 조건, 철회 (합성 데이터, LLM 호출 없음)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_p0_session_grounding import (AGE, ECOG, HB, Q_HB, SELF_OK, SEX, TOO_OLD, confirmed, hb, held, prof, run)  # noqa: E402,F401

from ctrec import value_grounding as vg  # noqa: E402

U = "unlocated"


def lab(text, value=12.0):
    return vg.ground_lab("hemoglobin", value, text, text, unit="g/dL")


@pytest.mark.parametrize("text", [
    "딸의 Hb 12", "아들 Hb 12", "동생 Hb 12", "형 Hb 12", "엄마 Hb 12", "가족 Hb 12", "옆 환자 Hb 12", "daughter Hb 12", "son Hb 12",
    "mom's Hb 12", "family member Hb 12", "another patient Hb 12"])
def test_another_persons_lab_is_not_evidence(text):
    assert lab(text) == U


@pytest.mark.parametrize("text", [
    "denies Hb 12", "Hb 12 was ruled out", "Hb 12 is incorrect", "Hb 12는 틀린 값", "Hb 12는 오기", "Hb 12 취소", "Hb 12에서 9로 정정", "Hb 12 corrected to 9"])
def test_denied_or_corrected_lab_is_not_evidence(text):
    assert lab(text) == U


@pytest.mark.parametrize("text", [
    "Hb 12로 맞추기로 함", "Hb 12 희망", "Hb 12 필요", "Hb 12 required", "Hb 12 needed", "Hb 12 이어야 함", "plan to reach Hb 12", "when Hb 12, give dose",
    "Hb 12가 되면 투약", "Hb 12 시 투약", "Hb 12 도달 시", "만약 Hb 12", "Hb 12로 가정", "assume Hb 12", "hypothetical Hb 12", "예: Hb 12", "example Hb 12",
    "estimated Hb 12", "가상의 Hb 12"])
def test_goal_or_supposed_lab_is_not_evidence(text):
    assert lab(text) == U


@pytest.mark.parametrize("text", [
    "Hb lower limit 12 g/dL", "평균 Hb 12", "population mean Hb 12", "시험 기준 Hb 12", "criteria: Hb 12", "eligibility Hb 12", "trial requires Hb 12"])
def test_limit_or_population_figure_is_not_evidence(text):
    assert lab(text) == U


@pytest.mark.parametrize("text", [
    "Hb 12 when admitted", "내원 시 Hb 12 g/dL", "가족력상 어머니 유방암, Hb 12 g/dL", "어머니 유방암. Hb 12 g/dL", "Hb 12 g/dL (정상 12-16)", "Hb 12 g/dL (L)",
    "오늘 Hb 12", "Hb 12 g/dL, 항암 일정 취소"])
def test_real_lab_next_to_those_words_is_kept(text):
    assert lab(text) is None


def test_estimated_gfr_is_still_a_measurement():
    text = "estimated GFR 60 mL/min"
    assert vg.ground_lab("gfr", 60.0, text, text) is None


@pytest.mark.parametrize("field,value,text", [
    ("age", 62, "아버지 62세, 폐암 환자"), ("age", 62, "남편 62세"), ("age", 62, "보호자 나이 62세"), ("age", 62, "father is 62 years old"), ("age", 62, "mother aged 62"),
    ("age", 62, "딸 62세"), ("age", 62, "배우자는 62세"), ("ecog", 1, "아버지 ECOG 1"), ("sex", "male", "아버지는 남성"), ("sex", "female", "아내는 여성")])
def test_another_persons_value_placed_before_it_is_not_evidence(field, value, text):
    assert vg.ground(field, value, text) == U


@pytest.mark.parametrize("field,value,text", [
    ("sex", "female", "여성 간호사"), ("sex", "female", "female nurse"), ("sex", "male", "male physician"), ("sex", "female", "여성 의사가 진찰"), ("sex", "male", "남자 친구와 내원"),
    ("sex", "male", "남자친구와 내원"), ("sex", "female", "여자친구"), ("sex", "female", "여성 호르몬 치료"), ("sex", "male", "남성 호르몬")])
def test_a_sex_word_about_someone_else_is_not_evidence(field, value, text):
    assert vg.ground(field, value, text) == U


@pytest.mark.parametrize("field,value,text", [
    ("age", 18, "18세 이상 환자"), ("age", 62, "at least 62 years"), ("age", 62, "age >= 62 years"), ("age", 62, "만 62세 미만"), ("age", 62, "62세가 되면 재평가"), ("age", 62, "나이 62세 아님"),
    ("age", 3, "3 years ago 수술"), ("age", 20, "20 years of smoking"), ("age", 5, "5-year survival"),
    ("ecog", 1, "ECOG 1 이하"), ("ecog", 1, "ECOG <= 1"), ("ecog", 1, "target ECOG 1"), ("ecog", 1, "ECOG 1 목표"), ("ecog", 1, "ECOG 1이면 투약"), ("ecog", 1, "if ECOG 1"),
    ("ecog", 1, "ECOG 1 required"), ("ecog", 1, "정상 ECOG 1")])
def test_limit_duration_or_condition_is_not_a_stated_value(field, value, text):
    assert vg.ground(field, value, text) == U


def test_a_past_value_gives_way_to_the_current_one():
    assert vg.ground("age", 40, "40세에 폐암 진단, 현재 62세") == U
    assert vg.ground("age", 62, "40세에 폐암 진단, 현재 62세") is None
    assert vg.ground("age", 62, "62세에 진단") is None  # 다른 값이 없으면 지울 근거도 없음
    assert vg.ground("ecog", 1, "과거 ECOG 1, 현재 ECOG 2") == U
    assert vg.ground("ecog", 2, "과거 ECOG 1, 현재 ECOG 2") is None


def test_two_current_ecog_values_in_the_record_are_a_conflict():
    assert vg.ground("ecog", 1, "ECOG 1. ECOG 2.") == "conflict"


@pytest.mark.parametrize("field,value,text", [
    ("age", 62, "62세 남성, 아들 35세"), ("age", 62, "62세 여성, 첫 출산 28세"), ("age", 62, "남편과 내원한 62세 여성"), ("age", 62, "보호자 동반 62세 남성"), ("age", 62, "62세 가족력 없음"),
    ("age", 62, "62-year-old man"), ("age", 62, "62 year old"), ("age", 62, "62 years old"), ("age", 62, "나이: 62"), ("age", 62, "62세 의사"),
    ("sex", "female", "A형 간염력 있는 여성"), ("sex", "male", "남성 유방암"), ("sex", "female", "62세 여성 환자, 남편 동행"), ("sex", "male", "남성, 흡연자"),
    ("ecog", 1, "ECOG 1"), ("ecog", 1, "ECOG PS 1, 통증 없음"), ("ecog", 0, "수행 상태 0")])
def test_ordinary_statements_stay_evidence(field, value, text):
    assert vg.ground(field, value, text) is None


# ------------------------------------------------------------ 실제 Session
def test_age_of_a_relative_does_not_confirm_or_rule_out(run):
    held(run("아버지 62세. 폐암 환자.", [AGE], prof(age=62)), "INFO_MISSING")
    held(run("아버지 62세. 폐암 환자.", [TOO_OLD], prof(age=62)), "INFO_MISSING")


def test_threshold_in_the_record_does_not_become_the_patient_age(run):
    held(run("18세 이상 환자 대상 시험 문의.", [AGE], prof(age=18)), "INFO_MISSING")


def test_ecog_limit_does_not_become_the_patient_value(run):
    held(run("ECOG 1 이하가 목표임.", [ECOG], prof(ecog=1)))


def test_sex_of_a_clinician_does_not_confirm(run):
    held(run("여성 간호사가 면담함.", [SEX], prof(sex="female")), "INFO_MISSING")


def test_answer_that_takes_the_value_back_does_not_confirm(run):
    o = run("폐암 환자.", [HB], prof(), prof(labs=[hb(12.0, "Hb 12")]), "Hb 12라고 했는데 틀렸어요", meta=SELF_OK, question=Q_HB, target="T1:I3")
    held(o, "INFO_MISSING")


def test_real_statements_still_confirm(run):
    confirmed(run("62세 남성, 아들과 내원. 폐암 환자.", [AGE], prof(age=62)))
    confirmed(run("혈색소는 12 g/dL입니다.", [HB], prof(labs=[hb(12.0, "혈색소는 12 g/dL입니다")]), meta=SELF_OK))
    o = run("폐암 환자.", [HB], prof(), prof(labs=[hb(12.0, "Hb 12 g/dL입니다")]), "Hb 12 g/dL입니다", meta=SELF_OK, question=Q_HB, target="T1:I3")
    confirmed(o)
