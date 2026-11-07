"""실제 Session: 측정값이 아닌 수치는 판정·추천·PairStatus·행동을 바꾸지 않음 (합성 데이터, LLM 호출 없음).

프로필러 자리의 스크립트가 원문의 수치를 환자 검사값으로 옮긴 최악의 경우를 재현합니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_p0_session_grounding import (HB, HB_HIGH, JUDGE, PID, Q_HB, RECORD, SELF_OK, confirmed, hb, held, prof, run)  # noqa: E402,F401
from test_offline import _rule  # noqa: E402

from ctrec.agents import matcher  # noqa: E402
from ctrec.schemas import LabValue, RecommendationDraft, RuleMeta  # noqa: E402

NOT_MEASURED = [
    ("Hb 12 아님", 12.0), ("Hb가 12라는 뜻은 아님", 12.0), ("Hb 12였던 적 없음", 12.0), ("Hb 12가 아닌 것으로 확인됨", 12.0),
    ("target Hb 12", 12.0), ("goal hemoglobin 12", 12.0), ("Hb 12 이상을 목표로 함", 12.0), ("수혈 목표 수치 Hb 12", 12.0), ("transfuse if Hb 12", 12.0),
    ("if Hb < 12, transfuse", 12.0), ("Hb가 12라면", 12.0), ("Hb 12일 경우", 12.0), ("unless Hb reaches 12", 12.0),
    ("reference range 12-16 g/dL", 12.0), ("Hb 정상 범위 12~16", 12.0), ("Hb reference 12 g/dL", 12.0), ("Hb >= 12 g/dL", 12.0), ("아버지 Hb 12 g/dL", 12.0),
]


def ranked(o, monkeypatch):
    """추천까지 실행. 추천 문장 LLM만 빈 초안으로 대체하고, 순서와 판정은 코드가 정합니다."""
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: RecommendationDraft(patient_id=PID, ranked=[], excluded=[], overall_comment=""))
    o.session.finalize()
    return o.session.recommendation


def lab_profile(text, value):
    return prof(labs=[hb(value, text)])


@pytest.mark.parametrize("text,value", NOT_MEASURED)
def test_not_measured_value_does_not_confirm(run, monkeypatch, text, value):
    o = run(text, [HB], lab_profile(text, value), meta=SELF_OK)
    held(o, "INFO_MISSING")
    assert o.match.match_status != "CRITERIA_MATCH" and "structured_" in " ".join(o.a.validation_flags)
    assert "ASK_PATIENT" in o.actions and ranked(o, monkeypatch).ranked[0].match_status == "INSUFFICIENT_INFORMATION"


@pytest.mark.parametrize("text,value", NOT_MEASURED)
def test_not_measured_value_does_not_rule_out(run, text, value):
    o = run(text, [HB_HIGH], lab_profile(text, value), meta=SELF_OK)
    held(o, "INFO_MISSING")
    assert o.match.match_status != "CRITERIA_MISMATCH"


@pytest.mark.parametrize("text,value", NOT_MEASURED[:6])
def test_record_rule_keeps_the_document_path(run, text, value):
    o = run(text, [HB], lab_profile(text, value), meta=RECORD, input_source="clinical_record")
    held(o, "VERIFICATION_PENDING")
    assert "REQUEST_DOCUMENT" in o.actions and o.a.self_reported_holds is None


@pytest.mark.parametrize("text", ["Hb 12 g/dL", "hemoglobin is 12 g/dL", "혈색소 12 g/dL", "Hb | 12 | g/dL", "Hb 12", "2026-03-01 Hb 12 g/dL", "CBC 결과. 수치는 다음과 같음. Hb 12 g/dL"])
def test_real_measurement_still_confirms(run, monkeypatch, text):
    o = run(text, [HB], lab_profile(text, 12.0), meta=RECORD, input_source="clinical_record")
    confirmed(o)
    assert ranked(o, monkeypatch).ranked[0].match_status == "CRITERIA_MATCH" and not o.actions


def test_real_measurement_still_rules_out(run, monkeypatch):
    o = run("Hb 12 g/dL", [HB_HIGH], lab_profile("Hb 12 g/dL", 12.0), meta=RECORD, input_source="clinical_record")
    assert (o.match.eligibility, o.status) == ("INELIGIBLE", "VERIFICATION_PENDING") and ranked(o, monkeypatch).ranked == []


def test_undeclared_document_does_not_rule_out(run):
    """선언이 없으면 값이 원문에 있어도 기록 필요 기준은 어느 쪽으로도 확정하지 않음."""
    o = run("Hb 12 g/dL", [HB_HIGH], lab_profile("Hb 12 g/dL", 12.0), meta=RECORD, input_source="unknown")
    held(o, "VERIFICATION_PENDING")
    assert "REQUEST_DOCUMENT" in o.actions


def test_current_value_is_used_over_the_stated_past(run):
    text = "과거 Hb 8, 현재 Hb 12"
    confirmed(run(text, [HB], prof(labs=[hb(12.0, "현재 Hb 12")]), meta=SELF_OK))
    held(run(text, [HB], prof(labs=[hb(8.0, "과거 Hb 8")]), meta=SELF_OK), "INFO_MISSING")


def test_undated_conflict_is_held(run):
    text = "Hb 8. Hb 12."
    held(run(text, [HB], prof(labs=[hb(12.0, "Hb 12")]), meta=SELF_OK), "INFO_MISSING")


def test_invented_unlisted_lab_is_held(run):
    rule = _rule(rule_id="I3", text="Ferritin >= 30 ng/mL", category="lab", field="lab:ferritin", operator=">=", value="30", unit="ng/mL")
    lab = LabValue(name="ferritin", value=80.0, unit="ng/mL", date=None, source_quote="체중 80 kg")
    held(run("체중 80 kg", [rule], prof(labs=[lab]), meta=SELF_OK), "INFO_MISSING")
    good = LabValue(name="ferritin", value=80.0, unit="ng/mL", date=None, source_quote="ferritin 80 ng/mL")
    confirmed(run("ferritin 80 ng/mL", [rule], prof(labs=[good]), meta=SELF_OK))


def test_vague_answer_with_a_number_stays_held(run):
    held(run("폐암 환자.", [HB], prof(), prof(labs=[hb(12.0, "대충 12쯤")]), "대충 12쯤", meta=SELF_OK, question=Q_HB, target="T1:I3"), "INFO_MISSING")


def test_clinician_rule_is_unchanged_by_a_lab_value(run):
    o = run("Hb 12 g/dL", [JUDGE], lab_profile("Hb 12 g/dL", 12.0))
    assert (o.match.match_status, o.status) == ("NEEDS_REVIEW", "EXPERT_REVIEW") and "CLINICIAN_REVIEW" in o.actions


def test_self_report_rule_meta_is_respected(run):
    meta = {"I3": RuleMeta(required_evidence="self_report_ok")}
    o = run("폐암 환자.", [HB], prof(), prof(labs=[hb()]), "Hb 12 g/dL", meta=meta, question=Q_HB, target="T1:I3")
    confirmed(o)
