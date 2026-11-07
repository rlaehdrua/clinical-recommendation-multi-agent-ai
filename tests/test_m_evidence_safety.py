"""M: 환자값 근거 안전성 P0 최종 (2026-11-07). 합성 값만, LLM 호출 0건.

- M-1 원문 서술 상충: 같은 질환의 '있음'·'없음' 진술이 함께 있으면 한쪽 인용으로 확정하지 않음. 다른 사람·배제·선별 문구,
  "No A, B or C" 목록 부정은 상충이 아님 (과잉 보류 대조군).
- M-2 검사 기간 조건: 규칙 엔진이 검사일로 계산. 일 단위 날짜가 없거나 기간 밖이면 판정하지 않고 LLM에도 넘기지 않음.
- L 잔여(held-out 5의 17건): 과거 값, 다른 사람, 체액 문맥, 무효, 복사 각주. 같은 세트의 정상 값은 계속 채택.
문서 권한(document_authority)·임상의 판단 경계는 바꾸지 않습니다: 기록 필요 기준은 문서 확인 대조군에서만 확정.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctrec import config, evidence_directness as ed, llm, value_grounding as vg
from ctrec.agents import matcher
from ctrec.agents.answerers import NoAnswerer
from ctrec.contract_session import ContractSession
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import (LabValue, LLMAssessment, MatcherOutput, PatientProfile, QuestionSet, RecommendationDraft,
                           Rule)
from ctrec.tools import ctgov, rule_engine
from ctrec.trace import Tracer
from ctrec import trial_store

ROOT = Path(__file__).resolve().parents[1]
ILD = "Participants must not have history of ILD/pneumonitis requiring treatment with steroids (≥ Grade 2), or current or suspected ILD/pneumonitis"
NO_KO = "스테로이드 치료가 필요했던 간질성 폐렴 병력은 없습니다"
YES_KO = "2025년에 스테로이드 치료가 필요했던 간질성 폐렴 병력이 있습니다"


# ----------------------------------------------------------------------------- M-1 서술 상충
CONFLICTS = [
    (f"합성 환자 62세 남성. {NO_KO}. 이전 병원 기록: {YES_KO}.", NO_KO),  # N 감사 E (조사만 다른 같은 서술)
    ("No history of ILD. Outside records: steroid-treated pneumonitis in 2024.", "No history of ILD"),  # 동의어 묶음
    ("간질성 폐질환 병력 없음 (환자 진술). 문제목록: 간질성 폐질환, 스테로이드 투여 중.", "간질성 폐질환 병력 없음"),  # 문제 목록
    ("Denies brain metastases. MRI 2026-10-02: two enhancing brain lesions consistent with metastases.", "Denies brain metastases"),
]
NOT_CONFLICTS = [
    (f"합성 환자 62세 남성. {NO_KO}.", NO_KO),
    ("ILD ruled out on HRCT 2026-10. No history of pneumonitis.", "No history of pneumonitis"),  # 배제는 '없음'
    ("No history of ILD. Father had pulmonary fibrosis.", "No history of ILD"),  # 가족
    ("No history of pneumonitis. Treated for community-acquired pneumonia in 2023.", "No history of pneumonitis"),  # 다른 질환
    ("No history of ILD. Will screen for ILD with baseline HRCT.", "No history of ILD"),  # 선별 계획
    ("No ILD, pneumonitis, or pulmonary fibrosis.", "No ILD"),  # 목록 부정
    ("간질성 폐질환 없음. 어머니 간질성 폐질환으로 치료 중.", "간질성 폐질환 없음"),  # 가족 (한국어)
]


@pytest.mark.parametrize("src,quote", CONFLICTS)
def test_m1_contradicting_history_is_conflicting(src, quote):
    assert ed.check(ILD if "ILD" in src or "간질성" in src else "Symptomatic or untreated brain metastases",
                    quote, src, holds="no").directness == "conflicting"


@pytest.mark.parametrize("src,quote", NOT_CONFLICTS)
def test_m1_consistent_or_distractor_history_is_not_conflicting(src, quote):
    assert ed.check(ILD, quote, src, holds="no").directness != "conflicting"


def _ild_rule(**kw) -> Rule:
    base = dict(rule_id="E2", kind="exclusion", text=ILD, category="comorbidity", field=None, operator=None, value=None,
                unit=None, time_window_days=None, structured_evaluable=False, underspecified=False, cohort=None)
    base.update(kw)
    return Rule(**base)


# ----------------------------------------------------------------------------- M-2 기간 조건
def _hb_rule(window=28) -> Rule:
    return Rule(rule_id="I9", kind="inclusion", text=f"Adequate organ function within {window} days before enrollment: "
                "hemoglobin >= 9.0 g/dL", category="lab", field="lab:hemoglobin", operator=">=", value="9.0", unit="g/dL",
                time_window_days=window, structured_evaluable=True, underspecified=False, cohort=None)


def _hb_profile(value=12.1, date="2026-10-20") -> PatientProfile:
    return PatientProfile(patient_id="T", age=62, sex="male", primary_diagnosis="NSCLC", disease_stage="IV", diagnoses=[],
                          biomarkers=[], labs=[LabValue(name="hemoglobin", value=value, unit="g/dL", date=date,
                                                        source_quote=f"Hb {value:g} g/dL")],
                          medications=[], treatment_history=[], ecog=1, other_facts=[], missing_or_ambiguous=[])


@pytest.mark.parametrize("lab_date,window,expect", [
    ("2026-10-20", 28, ("inside", 18)), ("2026-10-10", 28, ("inside", 28)), ("2026-10-09", 28, ("outside", 29)),
    ("2026-11-07", 7, ("inside", 0)), ("2026-10-31", 7, ("inside", 7)), ("2026-10-30", 7, ("outside", 8)),
    ("2026-11-08", 28, ("undetermined", -1)), ("2026-10", 28, ("undetermined", None)), ("2026", 28, ("undetermined", None)),
    (None, 28, ("undetermined", None)), ("Oct 20", 28, ("undetermined", None))])
def test_m2_window_status(lab_date, window, expect):
    assert rule_engine.window_status(lab_date, window, "2026-11-07") == expect


@pytest.mark.parametrize("value,holds,passes", [(12.1, "yes", True), (8.5, "no", False)])
def test_m2_inside_window_is_decided_by_value(value, holds, passes):
    a = rule_engine.evaluate(_hb_rule(), _hb_profile(value), "2026-11-07")
    assert (a.holds, a.passes, a.method) == (holds, passes, "rule_engine")
    assert "18일 전 검사" in a.reasoning


@pytest.mark.parametrize("date,flag", [("2026-09-01", "lab_window_outside"), ("2026-10", "lab_window_undetermined"),
                                       (None, "lab_window_undetermined"), ("2026-12-01", "lab_window_undetermined")])
@pytest.mark.parametrize("value", [12.1, 8.5])  # 기간을 확인하지 못하면 값이 통과든 미달이든 판정하지 않음
def test_m2_outside_or_undated_is_held_without_guessing(date, flag, value):
    a = rule_engine.evaluate(_hb_rule(), _hb_profile(value, date), "2026-11-07")
    assert a is not None and a.holds == "unknown" and a.passes is None  # None이면 LLM이 날짜를 추정하게 됨
    assert a.validation_flags == [flag] and "28일" in a.missing_info


def test_m2_default_reference_date_is_config(monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_DATE", "2026-11-07")
    assert rule_engine.evaluate(_hb_rule(), _hb_profile(12.1, "2026-10-20")).passes is True
    monkeypatch.setattr(config, "REFERENCE_DATE", "2026-12-31")
    assert rule_engine.evaluate(_hb_rule(), _hb_profile(12.1, "2026-10-20")).passes is None


def _demo():
    trial = ctgov.load_local(ROOT / "data" / "trials" / "DEMO-LUNG-001.json")
    return trial, trial_store.load(trial)


@pytest.mark.parametrize("date,passes", [("2026-10-20", True), ("2026-09-01", None), (None, None)])
def test_m2_matcher_uses_the_session_reference_date(monkeypatch, date, passes):
    """Matcher가 기준일을 규칙 엔진에 넘김. 문서 확인 대조군(document_check=True)에서 기간 안의 값만 확정."""
    trial, parsed = _demo()
    parsed.rules = [r for r in parsed.rules if r.rule_id == "I9"]
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM 호출 금지")))
    src = f"{date or ''} Hb 12.1 g/dL".strip()
    m = matcher.match_trial(_hb_profile(12.1, date), parsed, trial, reference_date="2026-11-07", source_text=src,
                            input_source="clinical_record", document_check=lambda a, r: True)
    assert m.assessments[0].passes is passes and m.assessments[0].method == "rule_engine"


# ----------------------------------------------------------------------------- Session / ContractSession (M-1 끝까지)
class _FakeLLM:
    def __init__(self, quote: str):
        self.quote = quote

    def structured(self, model_cls, *, system, user, **k):
        if model_cls is PatientProfile:
            return PatientProfile(patient_id="x", age=62, sex="male", primary_diagnosis="NSCLC", disease_stage="IV",
                                  diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[], ecog=None,
                                  other_facts=[], missing_or_ambiguous=[])
        if model_cls is MatcherOutput:
            rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
            return MatcherOutput(assessments=[LLMAssessment(
                rule_id=r["rule_id"], applicable=True, holds="no", evidence_type="explicit", confidence="high",
                evidence=self.quote, reasoning="synthetic", missing_info=None) for r in rules])
        if model_cls is QuestionSet:
            return QuestionSet(questions=[])
        if model_cls is RecommendationDraft:
            return RecommendationDraft(patient_id=user.split("patient_id=")[1].split(" ")[0], ranked=[], excluded=[],
                                       overall_comment="synthetic")
        raise AssertionError(model_cls.__name__)


@pytest.mark.parametrize("cls", [Session, ContractSession])
@pytest.mark.parametrize("text,eligible", [(f"합성 환자 62세 남성. {NO_KO}. 이전 병원 기록: {YES_KO}.", False),
                                           (f"합성 환자 62세 남성. {NO_KO}.", True)])
def test_m1_session_end_to_end(monkeypatch, cls, text, eligible):
    trial, parsed = _demo()
    keep = {"I1", "I2", "E3"}  # 나이·성별(구조화) + 간질성 폐질환 병력(자기 보고 가능)
    parsed.rules = [r for r in parsed.rules if r.rule_id in keep]
    parsed.rule_meta = {k: v for k, v in parsed.rule_meta.items() if k in keep}
    from ctrec import pipeline
    monkeypatch.setattr(pipeline.trial_store, "get", lambda t: parsed.model_copy(deep=True))
    monkeypatch.setattr(llm, "structured", _FakeLLM(NO_KO).structured)
    monkeypatch.setattr(config, "WRITE_REPORT", False)
    monkeypatch.setattr(config, "REFERENCE_DATE", "2026-11-07")
    s = cls(case=PatientCase(patient_id="SYN-M-1", raw_text=text), trials={trial["trial_id"]: trial},
            answerer=NoAnswerer(), tracer=Tracer(None, verbose=False), max_rounds=0)
    s.run_fixed()
    m = s.matches[trial["trial_id"]]
    e3 = next(a for a in m.assessments if a.rule_id == "E3")
    if eligible:
        assert m.eligibility == "ELIGIBLE" and e3.passes is True  # 상충이 없으면 과잉 보류하지 않음
    else:
        assert m.eligibility != "ELIGIBLE" and e3.passes is None and e3.evidence_directness == "conflicting"


# ----------------------------------------------------------------------------- L 잔여 (held-out 5, 이제 회귀 세트)
SET5 = json.loads((ROOT / "docs" / "dev" / "specimen_holdout_5.json").read_text(encoding="utf-8"))


def _judge(it):
    if it["field"] in ("age", "sex", "ecog"):
        return vg.ground(it["field"], it["value"], it["text"], unit=it.get("unit"))
    return vg.ground_lab(it["field"], it["value"], it["text"], it["text"], unit=it.get("unit"))


@pytest.fixture(autouse=True)
def _reference_date(monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_DATE", "2026-11-07")


@pytest.mark.parametrize("it", SET5["negative"], ids=lambda it: it["category"])
def test_l_residual_negative_is_held(it):
    assert _judge(it) is not None, it["text"]


# 고친 뒤에도 보류되는 정상 값 (정상 70건 중 9건). 검체 표시를 못 읽는 표현·다른 검사의 무효 표현·이름 쌍 표기.
# 개수가 늘면 과잉 보류 회귀, 줄면 이 목록을 갱신
KNOWN_OVERHOLD = {
    "오늘 아침 공복혈당 89 mg/dL, 인슐린 조정 없음.",
    "Venous blood draw 08:15 (L antecubital)\nCreatinine 0.76 mg/dL\nEstimated GFR 98",
    "S-Alb 3.3 g/dL (blood chemistry, 11/05), edema a little better.",
    "Handover. Bed 5: ANC 0.6, neutropenic fever, on pip-tazo. Bed 6 (this patient): ANC 2.4 on this morning's CBC, afebrile.",
    "Tumor board case 2 had bilirubin 5.0 and was deferred. Ours: total bilirubin 0.8 mg/dL on this morning's labs, can proceed.",
    "CBC today: Hgb 13.0, plt 210. Ferritin cancelled, sample insufficient.",
    "TSH pending. ANC 3.1 on this morning's differential, cleared for chemo.",
    "A/G ratio 1.3; serum albumin 3.9 g/dL on this morning's chemistry.",
    "AST/ALT 27/18 today, no change.",
}


def test_l_set5_positive_overhold_does_not_grow():
    held = {it["text"] for it in SET5["positive"] if _judge(it) is not None}
    assert held <= KNOWN_OVERHOLD, sorted(held - KNOWN_OVERHOLD)
    assert len(held) <= 9


@pytest.mark.parametrize("text,field,value,unit,accepted", [
    # 과거 값: 현재 값이 함께 있으면 현재 값은 채택
    ("Back in 2019 Hb was 9.1 g/dL. Today Hb 12.4 g/dL.", "hemoglobin", 12.4, "g/dL", True),
    ("Back in 2019 Hb was 9.1 g/dL. Today Hb 12.4 g/dL.", "hemoglobin", 9.1, "g/dL", False),
    ("Hb 12.4 g/dL (2026-11-05).", "hemoglobin", 12.4, "g/dL", True),  # 일 단위 날짜는 연도 규칙을 쓰지 않음
    ("Hb 11.0 g/dL in 2025.", "hemoglobin", 11.0, "g/dL", False),
    # 다른 사람: 같은 문장에서 환자로 돌아오면 채택
    ("Roommate's Hgb 7.1; this patient Hgb 12.0 today.", "hemoglobin", 12.0, None, True),
    ("Roommate in 12A, Hgb 7.1, needs transfusion.", "hemoglobin", 7.1, None, False),
    ("Her husband is the donor; his serum creatinine was 0.9 mg/dL.", "creatinine", 0.9, "mg/dL", False),
    ("기증자 Hb 13 g/dL, 환자 Hb 12 g/dL.", "hemoglobin", 12, "g/dL", True),
    # 체액 문맥
    ("LP done today. Tube 3 WBC 240/uL.", "white blood cell count", 240, "/uL", False),
    ("No ascites. WBC 6.2 x10^3/uL today.", "white blood cell count", 6.2, "x10^3/uL", True),
    ("Chem panel this AM: creatinine 0.79 mg/dL. Urine sent separately.", "creatinine", 0.79, "mg/dL", True),
    # 무효·복사
    ("Verbal report, not verified in the LIS: creatinine 2.3 mg/dL.", "creatinine", 2.3, "mg/dL", False),
    ("Labs: Hgb 10.2*\n*copied from the previous note", "hemoglobin", 10.2, None, False),
    ("Labs: Hgb 10.2* (*abnormal, repeat tomorrow)", "hemoglobin", 10.2, None, True),
])
def test_value_context_pairs(text, field, value, unit, accepted):
    assert (vg.ground_lab(field, value, text, text, unit=unit) is None) is accepted


@pytest.mark.parametrize("text,value,accepted", [
    ("At diagnosis in January 2022 he was ECOG 0. No current assessment recorded.", 0, False),  # 과거 값만
    ("ECOG 0 at diagnosis in 2022; ECOG 1 today.", 0, False),  # 현재 값과 다른 과거 값은 근거 아님
    ("ECOG 0 at diagnosis in 2022; ECOG 1 today.", 1, True),
    ("He cares for his mother. Her ECOG was 2 at her last visit.", 2, False),  # 앞 문장의 다른 사람을 가리키는 소유격
    ("ECOG 1 today.", 1, True),
])
def test_ecog_past_and_possessive(text, value, accepted):
    assert (vg.ground("ecog", value, text) is None) is accepted
