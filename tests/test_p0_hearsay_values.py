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

from test_p0_session_grounding import AGE, ECOG, HB, HB_HIGH, RECORD, SELF_OK, confirmed, hb, held, prof, run  # noqa: E402,F401

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
