"""P5 ContractSession 통합: 프로필 격리(Profiler 우회 차단), 계약 질문 공유, 경계, 기록-계약 상충, 기존 판정 호환.

LLM(프로필·Matcher·질문 생성·추천)은 모두 결정적 대역이며 API를 부르지 않습니다. 사이드카·잠긴 기준표는 tmp_path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config, pipeline
from ctrec.agents import matcher
from ctrec.contract_session import ContractSession
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import (ClarifyingQuestion, LLMAssessment, MatcherOutput, ParsedTrial, PatientProfile,
                           QuestionSet, Recommendation, RuleMeta)

from contract_fixtures import (MATCHER_GUARDED, PREG_IR, PROXY, QC_LACT, QC_PREG, QC_SURGERY, REF, RULES, SURG_IR, frozen_dir,
                               ir_dict, rule, sidecar)

AGE = rule("I1", "Age 18 years or older", kind="inclusion", category="demographic", field="age", operator=">=",
           value="18", unit="years", structured_evaluable=True)
T1_RULES = [RULES["SURG"], RULES["PREG"]]
T2_RULES = [RULES["SURG"].model_copy()]
TRIAL = {"title": "t", "eligibility_criteria": "x", "overall_status": "RECRUITING", "minimum_age": "18 Years",
         "locations": [{"country": "United States"}]}


class _Tracer:
    def __init__(self):
        self.records = []

    def log(self, agent, event, **data):
        self.records.append({"agent": agent, "event": event, **data})


def _parsed(tid, rules):
    p = ParsedTrial(trial_id=tid, title="t", target_population="", intervention_summary="", key_conditions=[],
                    cohorts=[], rules=[r.model_copy() for r in rules], parsing_notes="")
    p.rule_meta = {r.rule_id: RuleMeta(required_evidence="self_report_ok") for r in rules}
    return p


class _Answerer:
    def __init__(self, by_question: dict[str, str], default="모름"):
        self.by_question, self.default, self.seen = by_question, default, []

    def answer(self, questions, patient_id):
        self.seen += questions
        return {q.question_id: self.by_question.get(q.question, self.default) for q in questions}


def _profile_from(text_marker_age: str | None):
    def profile(patient_id, raw_text, reference_date=None):
        age = 45 if text_marker_age and text_marker_age in raw_text else None
        return PatientProfile(patient_id=patient_id, age=age, sex="female", primary_diagnosis=None,
                              disease_stage=None, diagnoses=[], biomarkers=[], labs=[], medications=[],
                              treatment_history=[], ecog=None, other_facts=[], missing_or_ambiguous=[])
    return profile


def _matcher(confirm: dict | None = None):
    """confirm: {rule_id: (holds, quote)} -> 그 규칙만 확정, 나머지 unknown."""
    confirm = confirm or {}

    def fake(model, system, user, **k):
        rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
        out = []
        for r in rules:
            if r["rule_id"] in confirm:
                h, q = confirm[r["rule_id"]]
                out.append(LLMAssessment(rule_id=r["rule_id"], applicable=True, holds=h, evidence_type="explicit",
                                         confidence="high", evidence=q, reasoning="mock", missing_info=None))
            else:
                out.append(LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="unknown",
                                         evidence_type="absent", confidence="low", evidence="근거 없음",
                                         reasoning="mock", missing_info="x"))
        return MatcherOutput(assessments=out)
    return fake


@pytest.fixture()
def patched(monkeypatch):
    state = {"llm_questions": []}

    def gen(profile, matches, qa_log, n, acquisition_plan=None):
        qs = state["llm_questions"][:n]
        return QuestionSet(questions=[ClarifyingQuestion(**q) for q in qs])

    monkeypatch.setattr(pipeline.question_generator, "generate_questions", gen)
    monkeypatch.setattr(pipeline.recommender, "recommend", lambda profile, matches, trials, qa_log=None:
                        Recommendation(patient_id=profile.patient_id, ranked=[], excluded=[], overall_comment="m"))
    monkeypatch.setattr(config, "WRITE_REPORT", False)
    monkeypatch.setattr(config, "QUESTION_POLICY", "batch")
    monkeypatch.setattr(config, "MAX_QUESTIONS_PER_ROUND", 5)
    monkeypatch.setattr(matcher.llm, "structured", _matcher())
    return state


def _trials(monkeypatch, spec: dict[str, list]):
    parsed = {tid: _parsed(tid, rules) for tid, rules in spec.items()}
    monkeypatch.setattr(pipeline.trial_store, "get", lambda trial: parsed[trial["trial_id"]].model_copy(deep=True))
    return {tid: {**TRIAL, "trial_id": tid} for tid in spec}


def _contract_env(tmp_path, approve=True):
    d = frozen_dir(tmp_path, T1_RULES, "T1")
    frozen_dir(tmp_path, T2_RULES, "T2")
    sidecar(tmp_path, d, [ir_dict(RULES["SURG"], d, SURG_IR, trial_id="T1", anchor_policy=PROXY),
                          ir_dict(RULES["PREG"], d, PREG_IR, trial_id="T1")],
            approve_ir=approve, approve_contracts=approve, trial_id="T1")
    sidecar(tmp_path, d, [ir_dict(RULES["SURG"], d, SURG_IR, trial_id="T2", anchor_policy=PROXY)],
            approve_ir=approve, approve_contracts=approve, trial_id="T2")
    return {"contract_root": tmp_path / "contracts", "contract_parsed_dir": d}


def _session(cls, trials, answerer, raw="45세 여성.", **kw):
    s = cls(case=PatientCase("P1", raw, input_source="clinical_record"), trials=trials, answerer=answerer,
            tracer=_Tracer(), **kw)
    s.reference_date = REF
    return s


# ------------------------------------------------------------ 프로필 격리 (P0 C5)
def test_profile_reextraction_bypass_blocked(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from("45세입니다"))
    trials = _trials(monkeypatch, {"T1": [AGE]})
    patched["llm_questions"] = [dict(question_id="Q1", question="나이가 어떻게 되시나요?", answer_type="number",
                                     target=["T1:I1"], purpose="eligibility", why="I1")]
    ans = {"나이가 어떻게 되시나요?": "45세입니다"}

    base = _session(Session, trials, _Answerer(ans), raw="여성 환자.")
    base.run_fixed()
    b = base.matches["T1"].assessments[0]
    # 기존 Session: 답변으로 확정 (패치 03이 적용된 환경이면 기존 Session도 경계로 UNVERIFIED)
    assert (b.method, b.passes, b.evidence_source) == ("rule_engine", None if MATCHER_GUARDED else True, "self_report")

    s = _session(ContractSession, trials, _Answerer(ans), raw="여성 환자.", contract_root=tmp_path / "none")
    s.run_fixed()
    a = s.matches["T1"].assessments[0]
    assert a.passes is None and s.profile.age is None
    assert any(r["event"] == "skipped" for r in s.tracer.records if r["agent"] == "patient_profiler")
    assert "45세입니다" in s.case.raw_text  # 답변 원문은 기록으로 남음


# ------------------------------------------------------------ 계약 질문·공유·결정적 판정
def test_contract_questions_shared_across_trials_and_decide(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": T1_RULES, "T2": T2_RULES})
    ans = _Answerer({QC_SURGERY["question"]: "수술을 받은 적 없음", QC_PREG["question"]: "아니요",
                     QC_LACT["question"]: "아니요"})
    s = _session(ContractSession, trials, ans, **_contract_env(tmp_path))
    s.run_fixed()
    asked = [q for q in ans.seen if q.question_id.startswith("C")]
    assert sorted(q.question for q in asked) == sorted([QC_SURGERY["question"], QC_PREG["question"],
                                                        QC_LACT["question"]])
    surg_q = next(q for q in asked if q.question == QC_SURGERY["question"])
    assert sorted(surg_q.target) == ["T1:E1", "T2:E1"]  # 한 번 묻고 두 시험이 공유
    assert s.matches["T1"].eligibility == "ELIGIBLE" and s.matches["T2"].eligibility == "ELIGIBLE"
    a = next(x for x in s.matches["T1"].assessments if x.rule_id == "E1")
    assert a.method == "contract" and a.passes is True
    res = s.result()
    prov = res["contract"]["provenance"]["T1:E1"]
    assert prov["outcome"] == "decided" and prov["contracts"] == ["QC-SURGERY-LAST@v1"] and prov["fact_ids"]
    assert {q["contract_id"] for q in res["contract"]["questions"]} >= {"QC-SURGERY-LAST"}
    assert all("contract_id" not in q and "session_id" not in q for q in res["qa_log"])  # 기존 형식 유지
    assert res["contract"]["ir_status"]["T1"] == {"E1": "active", "E3": "active"}


def test_unapproved_ir_and_contracts_ask_nothing_and_decide_nothing(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": T1_RULES})
    ans = _Answerer({})
    s = _session(ContractSession, trials, ans, **_contract_env(tmp_path, approve=False))
    s.run_fixed()
    assert not [q for q in ans.seen if q.question_id.startswith("C")]
    assert all(a.method != "contract" for a in s.matches["T1"].assessments)
    assert s.result()["contract"]["ir_status"]["T1"] == {"E1": "unreviewed", "E3": "unreviewed"}


def test_off_contract_answer_via_profiler_cannot_confirm_in_session(monkeypatch, patched, tmp_path):
    """반례 A를 실제 Session 흐름으로 (P0 B 경로): LLM 질문(수술 부작용)의 답이 Profiler 재추출로 프로필 사실이 되고
    Matcher가 그 인용으로 확정. ContractSession은 프로필을 다시 추출하지 않아 이 경로가 없음."""
    from ctrec.schemas import Fact
    q = "최근 4주 이내 수술 부작용이 있었나요?"
    ans_text = "아니요, 부작용은 전혀 없었어요"

    def profiler(patient_id, raw_text, reference_date=None):
        p = _profile_from(None)(patient_id, raw_text)
        if ans_text in raw_text:
            p.other_facts = [Fact(description="최근 4주 수술 문제 없음", date=None, source_quote=ans_text)]
        return p
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", profiler)
    trials = _trials(monkeypatch, {"T1": [RULES["SURG"]]})
    patched["llm_questions"] = [dict(question_id="Q1", question=q, answer_type="yes_no", target=["T1:E1"],
                                     purpose="eligibility", why="E1")]

    def matcher_llm(model, system, user, **k):
        return _matcher({"E1": ("no", ans_text)} if ans_text in user else {})(model, system, user)
    monkeypatch.setattr(matcher.llm, "structured", matcher_llm)

    base = _session(Session, trials, _Answerer({q: ans_text}))
    base.run_fixed()
    assert base.matches["T1"].eligibility == ("UNCERTAIN" if MATCHER_GUARDED else "ELIGIBLE")  # 기존 흐름: 잘못된 확정

    s = _session(ContractSession, trials, _Answerer({q: ans_text}), contract_root=tmp_path / "none")
    s.run_fixed()
    a = s.matches["T1"].assessments[0]
    assert a.passes is None and s.matches["T1"].eligibility == "UNCERTAIN"
    assert not s.profile.other_facts


def test_off_contract_answer_seen_directly_by_matcher_is_guarded(monkeypatch, patched, tmp_path):
    """Matcher가 답변을 직접 보는 경우(qalink의 문답 블록처럼)에도 경계가 확정을 되돌림 (방어 심층)."""
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [RULES["SURG"]]})
    q = "최근 4주 이내 수술 부작용이 있었나요?"
    ans_text = "아니요, 부작용은 전혀 없었어요"
    patched["llm_questions"] = [dict(question_id="Q1", question=q, answer_type="yes_no", target=["T1:E1"],
                                     purpose="eligibility", why="E1")]
    answerer = _Answerer({q: ans_text})
    monkeypatch.setattr(matcher.llm, "structured", lambda model, system, user, **k: _matcher(
        {"E1": ("no", ans_text)} if answerer.seen else {})(model, system, user))
    s = _session(ContractSession, trials, answerer, contract_root=tmp_path / "none")
    s.run_fixed()
    a = s.matches["T1"].assessments[0]
    assert a.passes is None and "qa_uncontracted" in a.validation_flags and a.self_reported_holds == "no"
    assert s.matches["T1"].match_status == "POTENTIAL_MATCH"
    assert s.missing_facts["T1:E1"].status == "document_requested"  # 같은 비계약 질문을 반복하지 않음
    # 경계를 적용한 곳: ContractSession (패치 03이 적용된 환경이면 matcher가 먼저 적용해 ContractSession은 바꿀 것이 없음)
    assert any(r["agent"] == "contract" for r in s.tracer.records) is not MATCHER_GUARDED


def test_record_contradicting_contract_answer_is_conflict(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": T1_RULES})
    raw = "45세 여성. 현재 임신 16주."
    monkeypatch.setattr(matcher.llm, "structured", _matcher({"E3": ("yes", "현재 임신 16주")}))
    ans = _Answerer({QC_PREG["question"]: "아니요", QC_LACT["question"]: "아니요",
                     QC_SURGERY["question"]: "수술을 받은 적 없음"})
    s = _session(ContractSession, trials, ans, raw=raw, **_contract_env(tmp_path))
    s.profile_patient()
    s.match()
    assert next(a for a in s.matches["T1"].assessments if a.rule_id == "E3").passes is False  # 기록 판정 유지
    # 기록상 INELIGIBLE이라 질문하지 않지만, 계약 사실이 생기면 상충으로 남기는지 직접 확인
    c = s._contracts.raw("QC-PREGNANT-NOW")
    from ctrec.question_contract import map_answer, render
    for cid in ("QC-PREGNANT-NOW", "QC-LACTATING-NOW"):
        c = s._contracts.raw(cid)
        s.ledger.record("P1", s.session_id, c, map_answer(c, render(c, "C1", []), "아니요"), round_no=1,
                        question_id="C1", as_of=REF)
    s.match()
    a = next(a for a in s.matches["T1"].assessments if a.rule_id == "E3")
    assert a.passes is None and a.needs_verification and "contract_vs_record_conflict" in a.validation_flags


def test_contract_session_matches_base_when_no_answers(monkeypatch, patched, tmp_path):
    """활성 IR 없음·답변 없음: 판정이 기존 Session과 같음 (정상 판정을 일괄 UNKNOWN으로 바꾸지 않음)."""
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from("45세"))
    trials = _trials(monkeypatch, {"T1": [AGE, RULES["PREG"]]})
    monkeypatch.setattr(matcher.llm, "structured", _matcher({"E3": ("no", "임신·수유 중 아님")}))
    raw = "45세 여성. 임신·수유 중 아님."
    base = _session(Session, trials, _Answerer({}), raw=raw)
    base.profile_patient()
    base.match()
    s = _session(ContractSession, trials, _Answerer({}), raw=raw, contract_root=tmp_path / "none")
    s.profile_patient()
    s.match()
    assert s.matches["T1"].model_dump() == base.matches["T1"].model_dump()
    assert s.matches["T1"].eligibility == "ELIGIBLE"


def test_free_text_contract_answer_is_not_fact_and_not_confirmed(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [RULES["SURG"]]})
    frozen = _contract_env(tmp_path)
    ans = _Answerer({QC_SURGERY["question"]: "한 달 전쯤 맹장 수술 했어요"})
    s = _session(ContractSession, trials, ans, **frozen)
    s.run_fixed()
    assert s.ledger.facts("P1", s.session_id) == [] and s.ledger.candidates
    assert s.matches["T1"].assessments[0].passes is None
