"""P0-B: 근사값·택일 값·전해 들은 값은 확정된 직접 사실이 아님 (합성 데이터, LLM 호출 없음).

기록 구간(처음 입력)에서도 답변 구간과 같이 근사("약", "쯤", "정도", "내외", "전후", "roughly", "approx.", "~", "≈")와
값 바로 앞뒤의 택일("12 or 12.5", "67 또는 68세", "ECOG 1 or 2")을 보류합니다.
보류는 판정 근거 확인(ground, ground_lab)에서만 일어납니다. 저장된 사실(원문 인용·출처)은 바뀌지 않습니다.
표현은 임상 검수를 받지 않았습니다. 기대값은 보류 상태(HEDGED 또는 UNLOCATED)를 정확히 고정합니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_p0_session_grounding import AGE, ECOG, HB, HB_HIGH, Q_HB, RECORD, SELF_OK, confirmed, hb, held, prof, run  # noqa: E402,F401

from ctrec import value_grounding as vg  # noqa: E402
from ctrec.schemas import RuleMeta  # noqa: E402

ECOG_SELF_OK = {"I2": RuleMeta(required_evidence="self_report_ok")}
H, U = vg.HEDGED, vg.UNLOCATED


def lab(field, value, text, unit=None):
    return vg.ground_lab(field, value, text, text, unit=unit)


# ----------------------------------------------------------------------------- 근사값
@pytest.mark.parametrize("field,value,text,status", [
    ("hemoglobin", 12, "혈색소 약 12", H),
    ("hemoglobin", 12, "혈색소 12쯤", H),
    ("hemoglobin", 12, "혈색소 12 정도", H),
    ("hemoglobin", 12, "혈색소 12 g/dL 내외", H),
    ("hemoglobin", 12, "혈색소 12 전후", H),
    ("hemoglobin", 12, "혈색소 12 가량", H),
    ("hemoglobin", 12, "Hb ~12 g/dL", H),
    ("hemoglobin", 12, "Hb ≈12 g/dL", H),
    ("hemoglobin", 12, "Hb about 12 g/dL by POC today.", H),
    ("hemoglobin", 12, "Hgb 12 or so", H),
    ("hemoglobin", 12, "Hb 12ish", H),
    ("hemoglobin", 12, "Hb roughly 12 g/dL", H),
    ("hemoglobin", 12, "Hb approx 12 g/dL", H),
    ("hemoglobin", 12, "Hb approx. 12 g/dL", U),  # "approx."의 마침표가 문장을 끊어 수치가 검사명에 연결되지 않음
])
def test_approximate_lab_value_is_held(field, value, text, status):
    assert lab(field, value, text) == status, text


@pytest.mark.parametrize("field,value,text", [
    ("age", 67, "Age around 67."),
    ("age", 67, "약 67세"),
    ("age", 67, "67세쯤"),
    ("age", 67, "67세 전후"),
    ("age", 67, "67세 내외"),
    ("age", 64, "Roughly 64 years old."),
    ("age", 64, "Approx. 64 years old."),
    ("ecog", 1, "ECOG 약 1"),
    ("ecog", 1, "ECOG 1 정도"),
    ("ecog", 1, "ECOG ~1"),
])
def test_approximate_age_or_ecog_in_the_record_is_held(field, value, text):
    assert vg.ground(field, value, text) == H, text


# ----------------------------------------------------------------------------- 택일 값
@pytest.mark.parametrize("field,value,text,status", [
    ("hemoglobin", 12, "Hb 12 or 12.5 g/dL", H),
    ("hemoglobin", 12.5, "Hb 12 or 12.5 g/dL", U),  # 뒤쪽 값은 검사명에 연결되지 않음
    ("hemoglobin", 12, "Hb 12 g/dL or 12.5 g/dL", H),
    ("hemoglobin", 12, "혈색소 12 또는 13", H),
    ("hemoglobin", 12, "혈색소 12~13", U),  # 범위는 기존대로 값이 아님
    ("hemoglobin", 10, "Hb 10 or 11 g/dL", H),
    ("hemoglobin", 11, "Hb 10 or 11 g/dL", U),
])
def test_alternative_lab_value_is_held(field, value, text, status):
    assert lab(field, value, text) == status, text


@pytest.mark.parametrize("field,value,text,status", [
    ("age", 67, "Self-reported age 67 or 68; no ID available.", H),
    ("age", 68, "67 or 68 years old", H),
    ("age", 67, "나이 67~68세", H),
    ("age", 67, "Age 67-68.", H),
    ("age", 67, "67 또는 68세", U),
    ("ecog", 1, "ECOG 1 or 2", H),
])
def test_alternative_age_or_ecog_in_the_record_is_held(field, value, text, status):
    assert vg.ground(field, value, text) == status, text


def test_alternative_age_does_not_confirm_eligible(run):
    held(run("폐암 환자. Self-reported age 67 or 68.", [AGE], prof(age=67)))


# ----------------------------------------------------------------------------- 전해 들은 값 (비의료인·화자 없음)
@pytest.mark.parametrize("field,value,text,status", [
    ("hemoglobin", 12, "혈색소 12라고 들었어요", H),
    ("hemoglobin", 12, "혈색소 12래요", H),
    ("hemoglobin", 12, "혈색소가 12이래요", H),
    ("hemoglobin", 12, "혈색소 12라더라고요", H),
    ("hemoglobin", 12, "혈색소 12 된대요", H),
    ("hemoglobin", 12, "혈색소 12였다고 했어요", H),
    ("hemoglobin", 12, "혈색소 12였다고 하더라고요", H),
    ("hemoglobin", 12, "보호자 말로는, 혈색소 12", H),  # 도입부가 앞 절
    ("hemoglobin", 12, "엄마가 그러는데 혈색소 12", H),
    ("platelets", 120, "다른 병원에서 혈소판 120이라고 들었다고 함", H),
    ("platelets", 120, "환자 말로는 다른 병원에서 혈소판 120", H),
    ("platelets", 120, "보호자 말로는 혈소판 120", U),  # 기존: 다른 사람의 값으로 봄
    ("platelets", 120, "본인 기억으로 혈소판 120", H),
    ("hemoglobin", 12, "Per family, Hb 12 g/dL.", H),
    ("hemoglobin", 12, "My wife said my Hb was 12.", U),
    ("hemoglobin", 12, "Apparently Hb 12 g/dL.", H),
    ("hemoglobin", 12, "Hb reportedly 12 g/dL.", H),
    ("hemoglobin", 12, "Patient states Hb 12 g/dL.", H),
    ("hemoglobin", 12, "Pt states her Hb was 12.", H),
    ("hemoglobin", 10, "Patient says Hb was 10 at the outside clinic.", H),
    ("hemoglobin", 12, "Per patient, Hb 12.", H),
    ("hemoglobin", 12, "Patient reports fatigue and patient states Hb 12 g/dL.", H),  # 뒤쪽 동사의 대상이 값
    ("hemoglobin", 12, "I was told my Hb is 12.", H),
    ("hemoglobin", 10, "Pt thinks her hemoglobin was 'around 10'", H),
    ("platelets", 140, "My oncologist mentioned, I believe, that my platelets were 140.", H),  # 추측 표지가 "that" 절의 앞 절
    ("absolute neutrophil count", 1.5, "ANC was something like 1.5, not sure.", H),
])
def test_relayed_lab_value_is_held(field, value, text, status):
    assert lab(field, value, text) == status, text


@pytest.mark.parametrize("text", [
    "Patient reports fatigue and Hb 12.0 g/dL today.",  # 전하는 동사의 대상은 증상 (리뷰 B-3)
    "환자 피곤하다고 하며 혈색소 12.0 g/dL.",
    "환자가 피곤하다고 함. 혈색소 12.0 g/dL.",
    "혈색소 12.0 g/dL이고 피곤하다고 함.",
    "Patient says she feels well; Hb 12.0 g/dL today.",
    "Hgb 12.0 g/dL today; patient says she feels tired.",
    "Wife says he is tired, but Hb 12.0 g/dL today.",
    "Family at bedside; Hb 12.0 g/dL today.",
    "Results reported: Hb 12.0 g/dL.",
    "Hb 12.0 g/dL as reported by the central lab.",
])
def test_reporting_verb_on_something_else_does_not_hold_the_lab(text):
    assert lab("hemoglobin", 12, text, "g/dL") is None, text


@pytest.mark.parametrize("text", [
    "The nurse called; Hb 12.0 g/dL.", "의사 선생님이 혈색소 12래요", "Dr. Kim said Hb 12.0 g/dL.", "Her GP told her the Hb was 10.",
])
def test_clinician_relayed_lab_is_left_as_before(text):
    """의료진이 전한 값은 임상 결정 CL-B1 전까지 이 변경으로 바꾸지 않음 (지금 동작: 채택). 결정되면 이 고정값을 갱신."""
    value = 10 if "was 10" in text else 12
    assert lab("hemoglobin", value, text) is None, text


@pytest.mark.parametrize("field,value,text,status", [
    ("age", 72, "보호자(아들)가 환자 나이를 72세라고 전함.", H),
    ("age", 72, "Son says she is 72 years old.", H),
    ("age", 68, "나이는 68인가 69인가, 정확히 기억이 안 남.", H),
])
def test_age_relayed_by_family_or_unsure_is_held(field, value, text, status):
    assert vg.ground(field, value, text) == status, text


@pytest.mark.parametrize("text", ["Patient says she is 67 years old.", "67세래요",
                                  "Wife says she is 55, but the patient himself is 67 years old per registration."])
def test_age_said_by_the_patient_is_still_a_self_report(text):
    assert vg.ground("age", 67, text) is None, text


@pytest.mark.parametrize("text", ["Pt thinks her hemoglobin was 'around 10' at the other hospital.", "혈색소 10이라고 들었어요."])
def test_relayed_lab_does_not_confirm_eligible_on_self_report_rule(run, text):
    o = run(f"폐암 환자. {text}", [HB], prof(labs=[hb(10.0, text)]), meta=SELF_OK)
    held(o)
    assert o.a.evidence_directness == "hedged" and text in o.a.evidence
    assert [x.source_quote for x in o.session.profile.labs] == [text]  # 원문 인용은 사실에 그대로 남음


@pytest.mark.parametrize("text", ["Pt thinks her hemoglobin was 'around 10' at the other hospital.", "혈색소 10이라고 들었어요."])
def test_relayed_lab_does_not_rule_out_on_a_record_rule(run, text):
    """진료 기록 선언 입력이어도 전해 들은 값으로 부적격을 내지 않음."""
    o = run(f"폐암 환자. {text}", [HB_HIGH], prof(labs=[hb(10.0, text)]), meta=RECORD, input_source="clinical_record")
    assert o.match.eligibility != "INELIGIBLE"


def test_relayed_lab_in_an_answer_is_held(run):
    o = run("폐암 환자.", [HB], prof(), prof(labs=[hb(12.0, "혈색소 12래요")]), "혈색소 12래요", meta=SELF_OK, question=Q_HB, target="T1:I3")
    held(o)
    assert o.a.evidence_directness == "hedged"


def test_plain_lab_in_an_answer_still_confirms(run):
    confirmed(run("폐암 환자.", [HB], prof(), prof(labs=[hb()]), "Hb 12 g/dL", meta=SELF_OK, question=Q_HB, target="T1:I3"))


# ----------------------------------------------------------------------------- 정상 값 (대조)
@pytest.mark.parametrize("field,value,text,unit", [
    ("hemoglobin", 12.1, "Hb 12.1 g/dL (2026-10-01)", "g/dL"),
    ("hemoglobin", 10.5, "Hb 10.5 g/dL - 2026-11-05 CBC.", "g/dL"),
    ("hemoglobin", 12, "Hb 12.0 g/dL (ref 12.0-16.0)", "g/dL"),  # 참고 범위는 택일이 아님
    ("platelets", 120, "Platelets 120 (ref 150-400) today.", None),
    ("hemoglobin", 12, "Hb 12.0 g/dL ~ 2026-11-06", "g/dL"),  # 값 뒤 날짜
    ("hemoglobin", 12, "Hb 12.0 g/dL, approximately stable.", "g/dL"),  # 근사 표지는 다른 절
    ("hemoglobin", 12, "수술 전후 수혈 없음, 혈색소 12 g/dL.", "g/dL"),  # "전후"가 값 뒤가 아님
    ("hemoglobin", 12, "Lab reported Hb 12.0 g/dL.", "g/dL"),
    ("hemoglobin", 12, "Hgb 12 g/dL today, English-speaking patient.", "g/dL"),  # "-ish"가 아님
    ("hemoglobin", 12, "혈색소 12 g/dL이라고 결과지에 기재됨.", "g/dL"),
    ("hemoglobin", 12, "혈색소 12.0 g/dL, 혈소판 210.", "g/dL"),
])
def test_plain_measured_lab_value_is_still_adopted(field, value, text, unit):
    assert lab(field, value, text, unit) is None, text


@pytest.mark.parametrize("field,value,text", [
    ("age", 67, "나이 67세"),
    ("age", 67, "67-year-old woman."),
    ("age", 67, "Patient says she is 67 years old."),  # 본인 진술: 자기 보고 허용 근거
    ("age", 67, "67세 남성, 5년 전 진단."),
    ("ecog", 1, "ECOG 1"),
    ("ecog", 1, "ECOG 1, says she walks daily."),
    ("sex", "female", "67-year-old woman, around 60kg."),
])
def test_plain_age_ecog_sex_is_still_adopted(field, value, text):
    assert vg.ground(field, value, text) is None, text


def test_a_definite_mention_elsewhere_still_grounds_the_value():
    assert vg.ground("age", 67, "Age around 67 per intake. ID checked: 67 years old.") is None


# ----------------------------------------------------------------------------- 판정까지 (Session, 오프라인)
def test_approximate_age_does_not_confirm_eligible(run):
    held(run("폐암 환자. Age around 67, not sure.", [AGE], prof(age=67)))


def test_plain_age_still_confirms(run):
    confirmed(run("폐암 환자. 나이 67세.", [AGE], prof(age=67)))


def test_approximate_ecog_does_not_confirm(run):
    held(run("폐암 환자. ECOG 1 정도.", [ECOG], prof(ecog=1), meta=ECOG_SELF_OK))


def test_plain_ecog_still_confirms(run):
    confirmed(run("폐암 환자. ECOG 1.", [ECOG], prof(ecog=1), meta=ECOG_SELF_OK))


@pytest.mark.parametrize("text", ["혈색소 10 g/dL 내외.", "Hb roughly 10 g/dL."])
def test_approximate_lab_does_not_confirm_and_keeps_its_source(run, text):
    """보류된 값도 원문 인용은 프로필 사실에 그대로 남고, 판정의 근거 문자열에도 그 인용이 남음."""
    o = run(f"폐암 환자. {text}", [HB], prof(labs=[hb(10.0, text)]), meta=SELF_OK)
    held(o)
    assert o.a.evidence_directness == "hedged"
    assert [x.source_quote for x in o.session.profile.labs] == [text]
    assert text in o.a.evidence


def test_plain_lab_still_rules_out(run):
    text = "Hb 12.1 g/dL (2026-10-01)"
    o = run(f"폐암 환자. {text}", [HB_HIGH], prof(labs=[hb(12.1, text)]), meta=SELF_OK)
    assert o.match.eligibility == "INELIGIBLE"


def test_plain_lab_still_confirms_on_a_self_report_rule(run):
    text = "Hb 12.1 g/dL (2026-10-01)"
    confirmed(run(f"폐암 환자. {text}", [HB], prof(labs=[hb(12.1, text)]), meta=SELF_OK))
