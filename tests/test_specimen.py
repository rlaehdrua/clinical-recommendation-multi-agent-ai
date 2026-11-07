"""검체 종류: 검사명이 같아도 검체가 다르면 같은 검사 사실이 아님 (실제 Session, 합성 데이터, LLM·API 호출 없음)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import test_p0_session_grounding as tsg  # noqa: E402
from test_expert_boundary import _run  # noqa: E402
from test_offline import _rule  # noqa: E402
from test_p0_session_grounding import PID, RECORD, SELF_OK, confirmed, prof, run  # noqa: E402,F401

from ctrec import specimen as sp, value_grounding as vg  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.fact_store import FactStore  # noqa: E402
from ctrec.fact_view import FactEvidenceView  # noqa: E402
from ctrec.profile_facts import ingest_profile  # noqa: E402
from ctrec.schemas import LabValue, QAPair, RuleMeta  # noqa: E402

CR = _rule(rule_id="I3", text="Serum creatinine <= 1.5 mg/dL", category="lab", field="lab:creatinine", operator="<=", value="1.5", unit="mg/dL")
CR_PLAIN = CR.model_copy(update={"text": "Creatinine <= 1.5 mg/dL"})
ALB = _rule(rule_id="I3", text="Serum albumin >= 3 g/dL", category="lab", field="lab:albumin", operator=">=", value="3", unit="g/dL")
WBC = _rule(rule_id="I3", text="WBC >= 3 x10^9/L", category="lab", field="lab:white blood cell count", operator=">=", value="3", unit="x10^9/L")
UP = _rule(rule_id="I3", text="Urine protein <= 1 g/24h", category="lab", field="lab:urine protein", operator="<=", value="1", unit="g/24h")


def lab(name, value, unit, quote):
    return LabValue(name=name, value=value, unit=unit, date=None, source_quote=quote)


def cr(value, quote):
    return lab("creatinine", value, "mg/dL", quote)


def case(run, text, rule, item, meta=SELF_OK, **kw):
    return run(f"폐암 환자. {text}", [rule], prof(labs=[item]), meta=meta, **kw)


def held(o):
    assert o.a.passes is None and o.match.eligibility == "UNCERTAIN" and o.status != "CONFIRMED_ELIGIBLE"


@pytest.fixture
def contract_run(run, monkeypatch):
    monkeypatch.setattr(tsg, "Session", ContractSession)
    return run


OTHER = ["Urine creatinine 1.2 mg/dL (spot).", "Creatinine 1.2 mg/dL (spot urine for protein ratio); serum not drawn.", "creatinine (urine) 1.2 mg/dL",
         "Creatinine, urine: 1.2 mg/dL", "소변 크레아티닌 1.2 mg/dL", "요 크레아티닌 1.2 mg/dL", "Urinalysis: creatinine 1.2 mg/dL",
         "24h urine collection: creatinine 1.2 mg/dL", "CSF creatinine 1.2 mg/dL", "뇌척수액 크레아티닌 1.2 mg/dL", "Pleural fluid creatinine 1.2 mg/dL"]
UNSTATED = ["Creatinine 1.2 mg/dL.", "크레아티닌 1.2 mg/dL", "Cr 1.2 mg/dL today.", "Urinalysis normal. Creatinine 1.2 mg/dL.",
            "Spot sample sent this morning. Creatinine 1.2 mg/dL.", "Result released: creatinine 1.2 mg/dL."]
SERUM = ["Serum creatinine 1.2 mg/dL", "혈청 크레아티닌 1.2 mg/dL", "Plasma creatinine 1.2 mg/dL", "Creatinine 1.2 mg/dL (serum)", "CMP: creatinine 1.2 mg/dL",
         "Bloods drawn at 06:00. Creatinine 1.2 mg/dL.", "채혈 결과 크레아티닌 1.2 mg/dL", "혈액검사 크레아티닌 1.2 mg/dL",
         "CMP: creatinine 1.2 mg/dL. No pleural effusion on CT.", "Urine creatinine 140 mg/dL; serum creatinine 1.2 mg/dL."]


# ------------------------------------------------------------ 판정 경로
@pytest.mark.parametrize("text", OTHER)
@pytest.mark.parametrize("rule", [CR, CR_PLAIN], ids=["named", "plain"])
def test_other_specimen_does_not_decide_a_blood_criterion(run, text, rule):
    held(case(run, text, rule, cr(1.2, text)))


@pytest.mark.parametrize("text", OTHER)
def test_other_specimen_is_held_in_contract_session(contract_run, text):
    held(case(contract_run, text, CR, cr(1.2, text)))


@pytest.mark.parametrize("text", UNSTATED)
def test_unstated_specimen_is_held_not_assumed_serum(run, text):
    o = case(run, text, CR, cr(1.2, text))
    held(o)
    assert o.status == "INFO_MISSING"


@pytest.mark.parametrize("text", SERUM)
def test_blood_value_confirms(run, text):
    confirmed(case(run, text, CR, cr(1.2, text)))


@pytest.mark.parametrize("text", SERUM[:4])
def test_blood_value_confirms_in_contract_session(contract_run, text):
    confirmed(case(contract_run, text, CR, cr(1.2, text)))


def test_serum_value_rules_out_and_urine_value_does_not(run):
    text = "Serum creatinine 3.4 mg/dL"
    assert case(run, text, CR, cr(3.4, text)).match.eligibility == "INELIGIBLE"
    text = "Urine creatinine 3.4 mg/dL"
    held(case(run, text, CR, cr(3.4, text)))


def test_unstated_high_value_does_not_rule_out(run):
    text = "Creatinine 3.4 mg/dL"
    held(case(run, text, CR, cr(3.4, text)))


def test_csf_albumin_and_serum_albumin(run):
    text = "CSF albumin 30 mg/dL, lumbar puncture note; serum albumin unknown."
    held(case(run, text, ALB.model_copy(update={"unit": "mg/dL", "value": "20"}), lab("albumin", 30, "mg/dL", text)))
    text = "혈청 알부민 3.5 g/dL"
    confirmed(case(run, text, ALB, lab("albumin", 3.5, "g/dL", text)))


def test_blood_only_lab_needs_no_label_but_a_named_fluid_still_blocks(run):
    text = "WBC 5.0 x10^9/L"
    confirmed(case(run, text, WBC, lab("wbc", 5.0, "x10^9/L", text)))
    text = "CSF WBC 5.0 x10^9/L"
    held(case(run, text, WBC, lab("wbc", 5.0, "x10^9/L", text)))
    text = "CSF analysis: glucose 60 mg/dL, WBC 5.0 x10^9/L"
    held(case(run, text, WBC, lab("wbc", 5.0, "x10^9/L", text)))


def test_urine_criterion_takes_the_urine_value_only(run):
    text = "Urine protein 0.5 g/24h"
    confirmed(case(run, text, UP, lab("urine protein", 0.5, "g/24h", text)))
    text = "Serum protein 0.5 g/24h"
    held(case(run, text, UP, lab("urine protein", 0.5, "g/24h", text)))


# ------------------------------------------------------------ 문서 필수 기준
RECORD_I3 = {"I3": RECORD["I3"]}


@pytest.mark.parametrize("text", ["Urine creatinine 1.2 mg/dL", "Creatinine 1.2 mg/dL"])
def test_document_required_is_not_lifted_by_attestation(run, text):
    held(case(run, text, CR, cr(1.2, text), meta=RECORD_I3, input_source="clinical_record", documents=True))


def test_document_required_keeps_needing_a_document_for_serum(run):
    text = "Serum creatinine 1.2 mg/dL"
    held(case(run, text, CR, cr(1.2, text), meta=RECORD_I3, input_source="clinical_record"))
    confirmed(case(run, text, CR, cr(1.2, text), meta=RECORD_I3, input_source="clinical_record", documents=True))


# ------------------------------------------------------------ Fact Store와 사실 연결
def store(text, *labs):
    s = FactStore()
    ingest_profile(s, prof(labs=list(labs)), raw_text=text, input_source="unknown", session_id="s1")
    return s


def test_urine_value_is_kept_as_its_own_fact_and_never_conflicts_with_serum():
    text = "Urine creatinine 140 mg/dL (spot); serum creatinine 1.3 mg/dL."
    s = store(text, cr(140, text), cr(1.3, text))
    [urine], [serum] = s.facts(PID, "lab:urine creatinine"), s.facts(PID, "lab:creatinine")
    assert (urine.value, urine.specimen, serum.value, serum.specimen) == (140, "urine", 1.3, "blood")
    assert s.conflicts(PID) == []


def test_unclear_fact_is_recorded_but_not_used_by_the_fact_view():
    text = "Creatinine 1.2 mg/dL"
    s = store(text, cr(1.2, text))
    assert s.facts(PID, "lab:creatinine")[0].specimen == sp.UNCLEAR
    assert FactEvidenceView(s, PID).to_profile().labs == []


def test_fact_view_for_wp6_uses_only_matching_specimens():
    s = FactStore()
    s.add(PID, "lab:creatinine", 1.2, unit="mg/dL", specimen="urine")
    s.add(PID, "lab:albumin", 3.5, unit="g/dL", specimen="blood")
    s.add(PID, "lab:hemoglobin", 12.0, unit="g/dL")  # 구조화 입력: 검체 없음은 확인하지 않은 값
    s.add(PID, "lab:urine protein", 0.5, unit="g/24h")
    assert {x.name for x in FactEvidenceView(s, PID).to_profile().labs} == {"albumin", "hemoglobin", "urine protein"}


def test_specimen_survives_save_and_load(tmp_path):
    s = FactStore(tmp_path)
    s.add(PID, "lab:creatinine", 1.2, unit="mg/dL", specimen="blood")
    s.save()
    assert FactStore.load(tmp_path).facts(PID, "lab:creatinine")[0].specimen == "blood"


# ------------------------------------------------------------ 값 근거 층
def test_llm_quote_of_another_specimen_is_not_a_measured_basis():
    rule = CR.model_copy(update={"field": "lab:creatinine", "structured_evaluable": False})
    assert vg._quote_not_measured("Urine creatinine 1.2 mg/dL", rule, "Urine creatinine 1.2 mg/dL")
    assert vg._quote_not_measured("Creatinine 1.2 mg/dL", rule, "Creatinine 1.2 mg/dL")
    assert not vg._quote_not_measured("Serum creatinine 1.2 mg/dL", rule, "Serum creatinine 1.2 mg/dL")


def test_llm_quote_on_a_blood_only_lab_stays_as_it_was():
    rule = WBC.model_copy(update={"structured_evaluable": False})
    assert not vg._quote_not_measured("WBC 5.0 x10^9/L", rule, "WBC 5.0 x10^9/L")


@pytest.mark.parametrize("question, grounded", [("혈청 크레아티닌 수치를 알려주세요", True), ("크레아티닌 수치를 알려주세요", False), ("소변 크레아티닌 수치를 알려주세요", False)])
def test_bare_answer_takes_its_specimen_from_the_question(question, grounded):
    source = "폐암 환자." + chr(10) * 2 + f"[추가 확인 정보 - 라운드 1]{chr(10)}Q: {question}{chr(10)}A: 1.2"
    qa = [QAPair(round=1, question_id="Q1", question=question, answer="1.2", purpose="eligibility", target=["T1:I3"], session_id="S")]
    result = vg.ground_lab("creatinine", 1.2, "1.2", source, key="T1:I3", qa=qa, unit="mg/dL", rule_text=CR.text)
    assert (result is None) is grounded


@pytest.mark.parametrize("text, rule_text, wanted", [
    ("creatinine", None, "blood"), ("urine protein", None, "urine"), ("creatinine", "Serum creatinine <= 1.5", "blood"),
    ("creatinine", "Urine creatinine <= 1.5", "urine"), ("glucose", "CSF glucose >= 40", "csf"),
    ("creatinine", "Creatinine <= 1.5 mg/dL or urine protein <= 1 g", "blood"), ("albumin", "Albumin (serum) >= 3", "blood")])
def test_required_specimen_reads_the_label_on_this_lab(text, rule_text, wanted):
    assert sp.required(text, rule_text) == wanted


@pytest.mark.parametrize("key, value, wanted, result", [
    ("creatinine", "blood", "blood", "match"), ("creatinine", "urine", "blood", "mismatch"), ("creatinine", None, "blood", "unclear"),
    ("creatinine", "unclear", "blood", "unclear"), ("hemoglobin", None, "blood", "match"), ("hemoglobin", "csf", "blood", "mismatch"),
    ("urine protein", None, "urine", "unclear"), ("urine protein", "urine", "urine", "match")])
def test_verdict_table(key, value, wanted, result):
    assert sp.verdict(key, value, wanted) == result


# ------------------------------------------------------------ J의 잔여 8건, 과잉 보류 5건: 실제 경로
LEAKS = [("lab:creatinine", 3.4, "mg/dL", "Same ward, bed 4 (not our patient): creatinine 3.4 mg/dL, nephrology consulted."),
         ("lab:absolute neutrophil count", 0.6, "x10^9/L", "Tumour board minutes: the case discussed before ours had ANC 0.6 x10^9/L; ours not yet available."),
         ("age", 58, None, "보호자(아내) 나이 58세. 환자 기록은 별도 확인 필요."),
         ("lab:creatinine", 140, "mg/dL", "Urine creatinine 140 mg/dL (spot urine for protein ratio); serum not drawn."),
         ("lab:albumin", 30, "mg/dL", "CSF albumin 30 mg/dL, lumbar puncture note; serum albumin unknown."),
         ("lab:white blood cell count", 12.0, "x10^9/L", "WBC 12.0 x10^9/L given verbally by phone; written report not yet received, unverified."),
         ("lab:total bilirubin", 5.1, "mg/dL", "지방혈증 검체, 총빌리루빈 5.1 mg/dL 간섭으로 보고 불가, 재채혈 예정."),
         ("ecog", 0, None, "No functional decline reported. Form legend: ECOG 0 = fully active; physician has not scored the patient yet.")]
RECOVERED = [("lab:inr", 1.0, None, "Repeat INR was not needed since the first draw was fine: INR 1.0."),
             ("lab:white blood cell count", 7.2, "x10^9/L", "Roommate's WBC is high; ours (the patient) is 7.2 x10^9/L."),
             ("ecog", 2, None, "Tumour board: our patient ECOG 2 per oncology; the neighbouring case (ECOG 0) is irrelevant here."),
             ("lab:creatinine", 1.3, "mg/dL", "Urine creatinine 140 mg/dL (spot); serum creatinine 1.3 mg/dL."),
             ("lab:white blood cell count", 5.9, "x10^9/L", "WBC after smear review confirmed 5.9 x10^9/L.")]


def _case(run, field, value, unit, text):
    if field == "age":
        return run(f"폐암 환자. {text}", [tsg.AGE], prof(age=value))
    if field == "ecog":
        return run(f"폐암 환자. {text}", [tsg.ECOG.model_copy(update={"text": "ECOG <= 2", "value": "2"})], prof(ecog=value), meta={"I2": RuleMeta(required_evidence="self_report_ok")})
    name = field[4:]
    rule = _rule(rule_id="I3", text=f"{name} >= 0", category="lab", field=field, operator=">=", value="0", unit=unit)
    return case(run, text, rule, lab(name, value, unit, text))


@pytest.mark.parametrize("field, value, unit, text", LEAKS, ids=[x[3][:24] for x in LEAKS])
def test_residual_leak_is_held_in_session(run, field, value, unit, text):
    held(_case(run, field, value, unit, text))


@pytest.mark.parametrize("field, value, unit, text", LEAKS, ids=[x[3][:24] for x in LEAKS])
def test_residual_leak_is_held_in_contract_session(contract_run, field, value, unit, text):
    held(_case(contract_run, field, value, unit, text))


@pytest.mark.parametrize("field, value, unit, text", RECOVERED, ids=[x[3][:24] for x in RECOVERED])
def test_over_hold_is_recovered_in_session(run, field, value, unit, text):
    confirmed(_case(run, field, value, unit, text))


@pytest.mark.parametrize("field, value, unit, text", RECOVERED, ids=[x[3][:24] for x in RECOVERED])
def test_over_hold_is_recovered_in_contract_session(contract_run, field, value, unit, text):
    confirmed(_case(contract_run, field, value, unit, text))


def hb_grounded(text):
    return vg.ground_lab("hemoglobin", 12.0, text, f"폐암 환자. {text}", unit="g/dL", known={"12"})


@pytest.mark.parametrize("text", ["Donor Hb 9 g/dL; our patient: Hb 12 g/dL", "Hb 12 g/dL, verified", "Hb 12 g/dL; procalcitonin unverified", "Hb 12 g/dL. CRP 보고 불가",
                                  "Transfusion not needed, Hb 12 g/dL", "Neighbour's Hb is low; ours is 12 g/dL", "Hb after smear review confirmed 12 g/dL", "Same ward, our patient: Hb 12 g/dL"])
def test_control_stays_grounded(text):
    assert hb_grounded(text) is None


@pytest.mark.parametrize("text", ["Bed 4 (not our patient): Hb 12 g/dL", "The case discussed before ours had Hb 12 g/dL", "another case Hb 12 g/dL", "Hb 12 g/dL, unverified", "Hb 12 g/dL 보고 불가",
                                  "Transfusion needed at Hb 12 g/dL", "Hb on the ward was looked at by two separate doctors who then later confirmed 12 g/dL"])
def test_control_stays_held(text):
    assert hb_grounded(text) is not None


@pytest.mark.parametrize("text, grounded", [("ECOG 1 = ambulatory, light work only", True), ("Form legend: ECOG 1 = ambulatory", False), ("Scale key: ECOG 1 = ambulatory", False), ("Scale reviewed. ECOG 1.", True)])
def test_ecog_definition_is_not_an_assessment(text, grounded):
    assert (vg.ground("ecog", 1, f"폐암 환자. {text}") is None) is grounded


@pytest.mark.parametrize("text, grounded", [("보호자(아내) 동석. 환자 나이 62세", True), ("보호자(아내) 나이 62세", False), ("아내(보호자) 62세", False)])
def test_age_after_a_parenthesis_keeps_its_owner(text, grounded):
    assert (vg.ground("age", 62, f"폐암 환자. {text}", reference_date="2026-11-07") is None) is grounded


@pytest.mark.parametrize("text", ["Creatinine 1.2 mg/dL and urine protein negative on CMP.", "CMP: creatinine 1.2 mg/dL, urine output normal.", "CBC done. Creatinine 1.2 mg/dL, blood pressure stable."])
def test_a_specimen_word_about_something_else_does_not_move_the_value(text):
    assert vg._value_specimen(text, text.index("1.2"), text.index("1.2") + 3, "creatinine") in ("blood", None)
    assert vg.lab_specimen("lab:creatinine", 1.2, text, text) != "urine"


def test_blood_pressure_alone_is_not_blood_context():
    text = "Creatinine 1.2 mg/dL, blood pressure stable, white blood cell differential normal."
    assert vg.lab_specimen("lab:creatinine", 1.2, text, text) == sp.UNCLEAR


def test_derived_values_have_no_specimen_to_compare():
    text = "24h urine CrCl 60 mL/min"
    assert vg.ground_lab("creatinine clearance", 60, text, f"폐암 환자. {text}", unit="mL/min") is None
    text = "LVEF 55 % pericardial effusion noted"
    assert vg.ground_lab("lvef", 55, text, f"폐암 환자. {text}", unit="%") is None


LLM_LEAKS = [x[3] for x in LEAKS if x[0].startswith("lab:")]


@pytest.mark.parametrize("text", LLM_LEAKS)
def test_residual_leak_in_an_llm_quote_is_held(monkeypatch, text):
    rule = _rule(rule_id="I3", text="Adequate serum creatinine, albumin, ANC, WBC and bilirubin", category="lab", structured_evaluable=False)
    x = _run(monkeypatch, "T1", [rule], {"I3": RuleMeta(required_evidence="self_report_ok")}, {"I3": ("yes", text)}, f"폐암 환자. {text}")
    assert x["match"].eligibility == "UNCERTAIN" and x["pair"].status.value == "INFO_MISSING"
