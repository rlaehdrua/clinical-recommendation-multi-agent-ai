"""A(P0-A): 서술형 환자 정보 상충 탐지. 합성 문장만 씀 (held-out 6 문장을 복사하지 않음), LLM 호출 0건.

- 상위어·하위어: 상위 개념의 '없음'("No history of hepatitis")과 하위 개념의 '있음'("chronic HBV")은 상충(보류).
  형제 개념(B형 vs C형 간염, 신장 vs 간 이식, 혈우병 vs von Willebrand)과 조혈모세포 이식은 상충이 아님.
- 절 앞 기록 머리말("HPI:", "Template ROS:")은 대상 낱말 비교에서 뗌.
- 환자 본인 이식의 공여 형태("deceased-donor renal transplant")는 다른 사람의 진술이 아님. 가족·공여자 본인 진술은 그대로 제외.
- 인용된 '없음'과 같은 질환의 추측형 '있음'("의심", "추정", "possible")은 상충(보류). 같은 문장에서 배제됐거나
  '없음' 쪽 추측("unlikely", "가능성 낮음")이면 상충이 아님.
- 상충은 보류(기록 확인)로만 이어짐: 확정 적격·확정 부적격으로 바꾸지 않음.
"""

from __future__ import annotations

import pytest

from ctrec import config, evidence_directness as ed, llm
from ctrec.agents import matcher
from ctrec.schemas import LLMAssessment, MatcherOutput, ParsedTrial, PatientProfile, Rule

HEP = "Active hepatitis B or C infection"
TX = "Prior allogeneic organ transplant"
BLEED = "Known bleeding diathesis or coagulopathy"
AUTO = "Active autoimmune disease requiring systemic treatment"
ILD = "History of interstitial lung disease"
CNS = "Untreated brain metastases"

# (기준, 원문, 인용) - 인용은 '없음' 쪽
CONFLICTS = {
    # 머리말
    "label_hpi": (HEP, "Problem list: HBV, chronic, on tenofovir. HPI: No history of hepatitis.", "HPI: No history of hepatitis."),
    # 본인 이식의 공여 형태
    "own_living_donor": (TX, "Denies any organ transplant. s/p living-donor liver transplant 2019, on tacrolimus.",
                         "Denies any organ transplant."),
    "own_deceased_donor_ko": (TX, "장기 이식 받은 적 없음. 2017년 뇌사자 기증자 신장 이식 받음.", "장기 이식 받은 적 없음"),
    # 상위어 '없음' vs 하위어 '있음'
    "hepatitis_vs_hcv": (HEP, "Patient denies hepatitis. Labs 2026: HCV RNA detectable, untreated.", "Patient denies hepatitis."),
    "bleeding_vs_vwd": (BLEED, "No known bleeding disorder. Hematology clinic: type 1 von Willebrand disease.",
                        "No known bleeding disorder."),
    "bleeding_vs_hemophilia_ko": (BLEED, "출혈성 질환 없음. 혈액내과: 경증 혈우병 B 진단.", "출혈성 질환 없음"),
    "autoimmune_vs_crohn": (AUTO, "Denies autoimmune conditions. GI note: Crohn disease on infliximab.",
                            "Denies autoimmune conditions."),
    "autoimmune_vs_ms_ko": (AUTO, "자가면역 질환 없음. 신경과: 다발성 경화증으로 인터페론 치료 중.", "자가면역 질환 없음"),
    "organ_vs_heart": (TX, "No prior organ transplantation. Heart transplant 2012, cardiology follow-up.",
                       "No prior organ transplantation."),
    # 추측형 '있음'
    "possible_ild": (ILD, "Denies interstitial lung disease. Chest CT 2026: findings suggestive of possible early ILD.",
                     "Denies interstitial lung disease."),
    "suspected_brain_met": (CNS, "No brain metastases. Brain MRI: small lesion, suspected brain metastasis.", "No brain metastases."),
    "suspected_fibrosis_ko": (ILD, "간질성 폐질환 없음. 흉부 CT: 폐섬유증 의심.", "간질성 폐질환 없음"),
    "cannot_be_excluded": (ILD, "No history of ILD. ILD cannot be excluded on CT.", "No history of ILD."),
    "other_disease_ruled_out": (ILD, "No history of ILD. Possible ILD on CXR, HBsAg negative.", "No history of ILD."),
}

NOT_CONFLICTS = {
    # 정상 경로 (self_report_ok 그대로)
    "plain_negative": (ILD, "No history of ILD. ECOG 1. Hypertension on amlodipine.", "No history of ILD."),
    "label_consistent": (HEP, "HPI: No hepatitis. ROS: negative.", "HPI: No hepatitis."),
    "serology_consistent": (HEP, "HBsAg negative, anti-HCV negative. No history of hepatitis.", "No history of hepatitis."),
    # 다른 사람
    "family_label": (HEP, "PMH: denies liver disease. Family history: mother with hepatitis B.", "PMH: denies liver disease"),
    "brother_is_donor": (TX, "No history of organ transplant. Her brother is the kidney donor for a relative.",
                         "No history of organ transplant."),
    "brother_received": (TX, "No history of organ transplant. Brother received a living-donor kidney transplant in 2015.",
                         "No history of organ transplant."),
    "patient_was_donor": (TX, "No history of organ transplant. Patient was a living kidney donor in 2010.",
                          "No history of organ transplant."),
    "father_psoriasis": (AUTO, "No autoimmune disease. Father has psoriasis.", "No autoimmune disease."),
    "donor_none": (TX, "No history of organ transplant. Donor: none.", "No history of organ transplant."),
    # 형제 개념·다른 개념
    "hbv_vs_hcv": ("Active hepatitis B infection", "No HBV infection. Treated HCV 2019, SVR achieved.", "No HBV infection."),
    "auto_sct": (TX, "No history of organ transplant. Autologous stem cell transplant 2019 for myeloma.",
                 "No history of organ transplant."),
    "allo_hsct": (TX, "No history of organ transplant. Allogeneic HSCT 2018 for AML.", "No history of organ transplant."),
    "kidney_vs_liver": (TX, "No kidney transplant history. Liver transplant 2010.", "No kidney transplant history."),
    "hemophilia_vs_vwd": (BLEED, "No hemophilia. Von Willebrand disease type 1.", "No hemophilia."),
    "pneumonia_not_ild": (ILD, "No history of ILD. Possible pneumonia on CXR.", "No history of ILD."),
    # 선별 계획
    "screen_planned": (BLEED, "No bleeding disorder. Screening for hemophilia carrier status planned.", "No bleeding disorder."),
    # 같은 문장에서 배제되었거나 '없음' 쪽 추측
    "possible_then_negative": (ILD, "No history of ILD. Possible ILD on CXR, HRCT negative.", "No history of ILD."),
    "possible_then_ruled_out": (ILD, "No history of ILD. Possible ILD on CXR, ruled out on HRCT.", "No history of ILD."),
    "unlikely": (ILD, "No history of ILD. ILD unlikely per pulmonology.", "No history of ILD."),
    "low_probability_ko": (ILD, "간질성 폐질환 없음. 흉부 CT상 간질성 폐질환 가능성 낮음.", "간질성 폐질환 없음"),
    "not_suspected": (ILD, "No history of ILD. ILD not suspected on CT.", "No history of ILD."),
}


@pytest.mark.parametrize("key", list(CONFLICTS))
def test_conflicting_narrative_is_held(key):
    crit, text, quote = CONFLICTS[key]
    c = ed.check(crit, quote, text, holds="no")
    assert c.directness == "conflicting" and "conflicting_evidence" in c.flags, (key, c)


@pytest.mark.parametrize("key", list(NOT_CONFLICTS))
def test_consistent_or_distractor_narrative_is_not_conflicting(key):
    crit, text, quote = NOT_CONFLICTS[key]
    assert ed.check(crit, quote, text, holds="no").directness != "conflicting", key


# 알려진 과잉 보류 (임상 검수 CL-A1 전까지 보수적으로 보류로 고정). 판정을 확정하지 않으므로 안전 방향
KNOWN_OVER_HOLD = {
    "topical_psoriasis": (AUTO, "Denies autoimmune disease. Mild psoriasis, topical steroid only.", "Denies autoimmune disease."),
    "suspected_then_workup": (ILD, "No history of ILD. Suspected ILD; will evaluate for ILD with HRCT.", "No history of ILD."),
}


@pytest.mark.parametrize("key", list(KNOWN_OVER_HOLD))
def test_known_over_hold_stays_held_not_confirmed(key):
    crit, text, quote = KNOWN_OVER_HOLD[key]
    assert ed.check(crit, quote, text, holds="no").directness == "conflicting", key


# ----------------------------------------------------------------------------- 보조 함수
def test_parent_group_ignores_child_mention():
    assert "hepatitis_any" not in ed._conditions("no hepatitis b")
    assert ed._conditions("no hepatitis b") == {"hepatitis_b"}
    assert "hepatitis_any" in ed._conditions("autoimmune hepatitis and hepatitis b")


def test_hsct_is_not_organ_transplant():
    for t in ("autologous stem cell transplant", "allogeneic hsct", "bone marrow transplant", "조혈모세포 이식"):
        assert not ed._conditions(t) & {"organ_transplant", "kidney_transplant", "liver_transplant", "heart_transplant",
                                        "lung_transplant"}, t


def test_section_label_strip_keeps_body():
    assert ed._strip_label("template ros: negative for bleeding disorders") == "negative for bleeding disorders"
    assert ed._strip_label("hpi:") == "hpi:"  # 떼고 남는 것이 없으면 그대로
    assert ed._strip_label("no history of ild") == "no history of ild"


# ----------------------------------------------------------------------------- match_trial 끝까지
def _run(monkeypatch, crit, text, quote):
    def _no_llm(*a, **k):
        raise AssertionError("real LLM call attempted")
    monkeypatch.setattr(llm, "structured", _no_llm)
    monkeypatch.setattr(config, "REFERENCE_DATE", "2026-11-07")
    prof = PatientProfile(patient_id="SYN-A", age=60, sex="male", primary_diagnosis=None, disease_stage=None, diagnoses=[],
                          biomarkers=[], labs=[], medications=[], treatment_history=[], ecog=None, other_facts=[],
                          missing_or_ambiguous=[])
    rule = Rule(rule_id="E1", kind="exclusion", text=crit, category="comorbidity", field=None, operator=None, value=None,
                unit=None, time_window_days=None, structured_evaluable=False, underspecified=False, cohort=None)
    pt = ParsedTrial(trial_id="SYN-A-TRIAL", title="t", target_population="t", intervention_summary="t", key_conditions=[],
                     cohorts=[], rules=[rule], parsing_notes="")
    fake = MatcherOutput(assessments=[LLMAssessment(rule_id="E1", applicable=True, holds="no", evidence_type="explicit",
                                                    confidence="high", evidence=quote, reasoning="synthetic",
                                                    missing_info=None)])
    monkeypatch.setattr(matcher, "_llm_assess", lambda *a, **k: fake)
    return matcher.match_trial(prof, pt, {"brief_summary": ""}, source_text=text, input_source="self_report")


@pytest.mark.parametrize("key", ["label_hpi", "own_living_donor", "bleeding_vs_vwd", "autoimmune_vs_crohn", "possible_ild"])
def test_match_trial_conflict_is_unverified_not_confirmed(monkeypatch, key):
    m = _run(monkeypatch, *CONFLICTS[key])
    a = m.assessments[0]
    assert m.eligibility not in ("ELIGIBLE", "INELIGIBLE"), key
    assert a.passes is None and a.evidence_directness == "conflicting" and a.needs_verification is True


@pytest.mark.parametrize("key", ["plain_negative", "auto_sct", "father_psoriasis", "possible_then_negative"])
def test_match_trial_non_conflict_keeps_self_report_path(monkeypatch, key):
    m = _run(monkeypatch, *NOT_CONFLICTS[key])
    a = m.assessments[0]
    assert m.eligibility == "ELIGIBLE" and a.passes is True and a.needs_verification is False, key
