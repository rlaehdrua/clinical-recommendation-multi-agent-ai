"""A-1: '배제하지 못함' 표현은 배제 진술이 아니라 추측형 '있음'. 합성 문장만 씀, LLM 호출 0건.

- "ILD not ruled out", "Cannot rule out ILD", "could not be excluded", "배제할 수 없음", "배제 못함"이 인용된
  '없음'("No history of ILD")과 함께 있으면 상충(보류). 확정 적격으로 남지 않음.
- A-4: "solid organ malignancy"는 장기 이식이 아니고, "5 마이크론 결절"의 '크론'은 크론병이 아님 (과보류 해소).
  "solid organ transplant"와 "크론병"은 그대로 같은 질환으로 봄.
- "r/o X", "?X"는 임상 결정(CL-A1) 전까지 바꾸지 않으므로 여기서 고정하지 않음.
"""

from __future__ import annotations

import pytest

from ctrec import config, evidence_directness as ed, llm
from ctrec.agents import matcher
from ctrec.schemas import LLMAssessment, MatcherOutput, ParsedTrial, PatientProfile, Rule

ILD = "History of interstitial lung disease"
TX = "Prior allogeneic organ transplant"
AUTO = "Active autoimmune disease requiring systemic treatment"

NOT_RULED_OUT = {
    "not_ruled_out": (ILD, "No history of ILD. ILD not ruled out on CT.", "No history of ILD."),
    "cannot_rule_out": (ILD, "No history of ILD. Cannot rule out ILD on CT.", "No history of ILD."),
    "cant_rule_out": (ILD, "No history of ILD. Can't rule out ILD.", "No history of ILD."),
    "could_not_be_excluded": (ILD, "No history of ILD. ILD could not be excluded on CT.", "No history of ILD."),
    "ko_cannot_exclude": (ILD, "간질성 폐질환 병력 없음. 흉부 CT상 간질성 폐질환 배제할 수 없음.", "간질성 폐질환 병력 없음"),
    "ko_not_excluded": (ILD, "간질성 폐질환 병력 없음. 흉부 CT상 간질성 폐질환 배제 못함.", "간질성 폐질환 병력 없음"),
}

NOT_CONFLICTS = {
    "solid_organ_malignancy": (TX, "No history of organ transplant. History of solid organ malignancy (breast cancer).",
                               "No history of organ transplant."),
    "micron_nodule_ko": (AUTO, "자가면역 질환 없음. 흉부 CT: 5 마이크론 결절.", "자가면역 질환 없음"),
    "micron_nodule_en": (AUTO, "No autoimmune disease. CT: 5 마이크론 결절.", "No autoimmune disease."),
    # 배제된 진술은 그대로 '없음'
    "ruled_out_still_negative": (ILD, "No history of ILD. ILD ruled out on HRCT.", "No history of ILD."),
    "ko_ruled_out_still_negative": (ILD, "간질성 폐질환 병력 없음. 흉부 CT상 간질성 폐질환 배제됨.", "간질성 폐질환 병력 없음"),
}

STILL_CONFLICTS = {
    "solid_organ_transplant": (TX, "No history of organ transplant. s/p solid organ transplant 2015.",
                               "No history of organ transplant."),
    "crohn_ko": (AUTO, "자가면역 질환 없음. 크론병으로 치료 중.", "자가면역 질환 없음"),
    "crohn_ko_spaced": (AUTO, "자가면역 질환 없음. 위장관: 크론 병 진단.", "자가면역 질환 없음"),
}


@pytest.mark.parametrize("key", list(NOT_RULED_OUT) + list(STILL_CONFLICTS))
def test_not_ruled_out_is_conflicting(key):
    crit, text, quote = {**NOT_RULED_OUT, **STILL_CONFLICTS}[key]
    c = ed.check(crit, quote, text, holds="no")
    assert c.directness == "conflicting" and "conflicting_evidence" in c.flags, (key, c)


@pytest.mark.parametrize("key", list(NOT_CONFLICTS))
def test_over_broad_patterns_do_not_conflict(key):
    crit, text, quote = NOT_CONFLICTS[key]
    assert ed.check(crit, quote, text, holds="no").directness != "conflicting", key


def test_condition_groups_boundaries():
    assert "inflammatory_bowel_disease" not in ed._conditions("5 마이크론 결절")
    assert "inflammatory_bowel_disease" in ed._conditions("크론병")
    assert "organ_transplant" not in ed._conditions("solid organ malignancy")
    assert "organ_transplant" in ed._conditions("solid organ transplant recipient")


def test_conflict_polarity_not_ruled_out_is_uncertain():
    for clause in ("ild not ruled out on ct", "ild could not be excluded", "간질성 폐질환 배제할 수 없음", "배제 못함"):
        assert ed._conflict_polarity(clause) == "uncertain", clause
    for clause in ("ild ruled out on hrct", "뇌전이 배제됨"):
        assert ed._conflict_polarity(clause) == "negative", clause


def _run(monkeypatch, crit, text, quote):
    def _no_llm(*a, **k):
        raise AssertionError("real LLM call attempted")
    monkeypatch.setattr(llm, "structured", _no_llm)
    monkeypatch.setattr(config, "REFERENCE_DATE", "2026-11-07")
    prof = PatientProfile(patient_id="SYN-A1", age=60, sex="male", primary_diagnosis=None, disease_stage=None, diagnoses=[],
                          biomarkers=[], labs=[], medications=[], treatment_history=[], ecog=None, other_facts=[],
                          missing_or_ambiguous=[])
    rule = Rule(rule_id="E1", kind="exclusion", text=crit, category="comorbidity", field=None, operator=None, value=None,
                unit=None, time_window_days=None, structured_evaluable=False, underspecified=False, cohort=None)
    pt = ParsedTrial(trial_id="SYN-A1-TRIAL", title="t", target_population="t", intervention_summary="t", key_conditions=[],
                     cohorts=[], rules=[rule], parsing_notes="")
    fake = MatcherOutput(assessments=[LLMAssessment(rule_id="E1", applicable=True, holds="no", evidence_type="explicit",
                                                    confidence="high", evidence=quote, reasoning="synthetic",
                                                    missing_info=None)])
    monkeypatch.setattr(matcher, "_llm_assess", lambda *a, **k: fake)
    return matcher.match_trial(prof, pt, {"brief_summary": ""}, source_text=text, input_source="self_report")


@pytest.mark.parametrize("key", list(NOT_RULED_OUT))
def test_match_trial_not_ruled_out_is_never_confirmed(monkeypatch, key):
    m = _run(monkeypatch, *NOT_RULED_OUT[key])
    a = m.assessments[0]
    assert m.eligibility not in ("ELIGIBLE", "INELIGIBLE"), key
    assert a.passes is None and a.evidence_directness == "conflicting" and a.needs_verification is True
