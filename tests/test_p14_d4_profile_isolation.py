"""P14 D4 후속: 의사 참고용 답에서 다시 추출한 프로필 필드가 판정을 바꾸던 우회 경로. 오프라인, 유료 API 없음.

D4(7076b89)는 참고용 답을 '인용'한 근거를 막았지만, 기본 Session.clarify는 참고용 문답까지 원문에 붙인 뒤 프로필을 다시
추출함. 그 답에 적힌 나이·성별·ECOG·검사 수치가 프로필 필드가 되면 규칙 엔진 판정(인용 없이 "환자 나이 44 years")과
LLM의 필드 인용("age: 44")은 D4 검사를 거치지 않고, 나이·성별은 자기 보고로 판정해도 되는 기준(self_report_ok)이라
확정 판정이 됨. 수정: 프로필 추출 입력에서 이 세션의 판정용이 아닌 문답만 뺌(Session._profiler_text). 원문은 그대로.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config, pipeline
from ctrec import evidence_directness as ed
from ctrec.agents import matcher
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import ClarifyingQuestion, LLMAssessment, MatcherOutput, PatientProfile, QAPair, QuestionSet, \
    Recommendation

from test_decisions import TRIAL, _parsed
from test_offline import _rule

RAW = "여성. 조짐을 동반한 편두통으로 내원. 나이 기록 없음."  # 같은 환자 기록: 나이를 모름
QUESTION = "복용 중인 약과 과거 병력을 알려주세요."
ANSWER = "특별히 먹는 약은 없고 저는 44세입니다."
RULES = [
    _rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18", unit="years"),
    _rule(rule_id="I2", text="Adults aged 18 to 65", category="demographic", structured_evaluable=False),
]


class _Tracer:
    def log(self, agent, event, **data):
        pass


class _Answerer:
    def answer(self, questions, patient_id):
        return {q.question_id: ANSWER for q in questions}


def _fake_profile(patient_id, raw_text, reference_date=None):
    """입력 어디에든(확인 답변 포함) 적힌 나이를 프로필에 넣음 - 실제 프로필 추출 에이전트의 동작과 같은 방향."""
    m = re.search(r"(\d+)세", raw_text)
    age = int(m.group(1)) if m else None
    return PatientProfile(patient_id=patient_id, age=age, age_value=age, age_unit="years" if m else None, sex="female",
                          primary_diagnosis="migraine with aura", disease_stage=None, diagnoses=[], biomarkers=[],
                          labs=[], medications=[], treatment_history=[], ecog=None, other_facts=[],
                          missing_or_ambiguous=[] if m else ["나이"])


def _fake_matcher_llm(model, system, user, **k):
    """I2: 프로필에 나이가 있으면 그 필드를 인용해 '해당', 없으면 판정 불가."""
    patient = json.loads(user.split("<patient>")[1].split("</patient>")[0])
    rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
    out = []
    for r in rules:
        if patient["age"] is not None:
            out.append(LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="yes", evidence_type="explicit",
                                     confidence="high", evidence=f"age: {patient['age']}", reasoning="mock",
                                     missing_info=None))
        else:
            out.append(LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent",
                                     confidence="low", evidence="근거 없음", reasoning="mock", missing_info="나이"))
    return MatcherOutput(assessments=out)


@pytest.fixture()
def stubs(monkeypatch):
    def setup(purpose: str | None):
        def gen(profile, matches, qa_log, n, acquisition_plan=None):
            if purpose is None or qa_log:  # 질문 없음(기준선) / 한 라운드만
                return QuestionSet(questions=[])
            return QuestionSet(questions=[ClarifyingQuestion(
                question_id="Q1", question=QUESTION, answer_type="free_text", target=["T1:I1"], purpose=purpose,
                why="I1")])

        monkeypatch.setattr(pipeline.question_generator, "generate_questions", gen)
        monkeypatch.setattr(pipeline.recommender, "recommend", lambda profile, matches, trials, qa_log=None:
                            Recommendation(patient_id=profile.patient_id, ranked=[], excluded=[], overall_comment="m"))
        monkeypatch.setattr(pipeline.trial_store, "get", lambda trial: _parsed([r.model_copy() for r in RULES]))
        monkeypatch.setattr(matcher.llm, "structured", _fake_matcher_llm)
        monkeypatch.setattr(config, "WRITE_REPORT", False)
        monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _fake_profile)
    return setup


def _run(stubs, purpose: str | None) -> Session:
    stubs(purpose)
    s = Session(case=PatientCase("P-iso", RAW, input_source="clinical_record"), trials={"T1": dict(TRIAL)},
                answerer=_Answerer(), tracer=_Tracer())
    s.run_fixed()
    return s


def _judged(s: Session) -> dict:
    return {a.rule_id: (a.holds, a.passes) for a in s.matches["T1"].assessments}


# ------------------------------------------------------------ 같은 환자 기록에 참고용 답만 추가: 판정이 바뀌지 않음
def test_reference_answer_alone_does_not_confirm_age(stubs):
    base = _run(stubs, None)
    assert base.qa_log == [] and _judged(base) == {"I1": ("unknown", None), "I2": ("unknown", None)}

    s = _run(stubs, "physician_reference")
    assert len(s.qa_log) == 1 and s.qa_log[0].answer == ANSWER and ANSWER in s.case.raw_text  # 원문·문답 기록은 그대로
    assert s.profile.age is None  # 참고용 답의 나이는 프로필로 다시 추출되지 않음
    assert _judged(s) == _judged(base)  # 규칙 엔진(I1)·LLM 필드 인용(I2) 모두 확정되지 않음
    assert s.matches["T1"].eligibility == base.matches["T1"].eligibility


def test_eligibility_answer_still_sets_profile_and_judgment(stubs):
    s = _run(stubs, "eligibility")
    assert s.profile.age == 44
    i1 = next(a for a in s.matches["T1"].assessments if a.rule_id == "I1")
    assert (i1.method, i1.holds, i1.passes, i1.evidence_source) == ("rule_engine", "yes", True, "self_report")
    assert _judged(s)["I2"] == ("yes", True)


# ------------------------------------------------------------ 프로필 추출 입력 (Session._profiler_text)
def _session(raw: str, *pairs: QAPair) -> Session:
    s = Session(case=PatientCase("P", raw), trials={}, answerer=_Answerer(), tracer=_Tracer(), session_id="S1")
    s.qa_log.extend(pairs)
    return s


def _pair(q, a, purpose="eligibility", round_=1, sid="S1", **kw):
    p = {} if purpose is None else {"purpose": purpose}
    return QAPair(round=round_, question_id=q, question=q, answer=a, target=[], session_id=sid, **p, **kw)


def _block(round_, *pairs):
    return f"\n\n[추가 확인 정보 - 라운드 {round_}]\n" + "\n".join(f"Q: {p.question}\nA: {p.answer}" for p in pairs)


def test_profiler_text_unchanged_without_reference_answers():
    e = _pair("q1", "a1")
    s = _session(RAW + _block(1, e), e)
    assert s._profiler_text() is s.case.raw_text


def test_profiler_text_drops_reference_and_purpose_unknown_pairs_only():
    e, r, u = _pair("q1", "예"), _pair("q2", "44세", "physician_reference"), _pair("q3", "ECOG 1", None)
    r2 = _pair("q4", "Hb 9", "physician_reference", round_=2)
    raw = RAW + _block(1, r, e, u) + _block(2, r2)
    s = _session(raw, r, e, u, r2)
    assert ed.declared_purpose(u) == "unknown"
    assert s._profiler_text() == RAW + _block(1, e)  # 판정용만 남고, 비게 된 라운드 2 머리말은 빠짐
    assert s.case.raw_text == raw


def test_profiler_text_keeps_record_text_identical_to_reference_answer():
    """처음 입력에 같은 문장이 있으면 그것은 기록이므로 남김 (답변 블록 안에서만 뺌)."""
    r = _pair("q", "44세", "physician_reference")
    raw = "Q: q\nA: 44세 (외래 기록)" + _block(1, r)
    assert _session(raw, r)._profiler_text() == "Q: q\nA: 44세 (외래 기록)"


def test_profiler_text_ignores_other_session_records():
    other = _pair("q", "44세", "physician_reference", sid="S-other")
    raw = RAW + _block(1, other)
    assert _session(raw, other)._profiler_text() == raw  # 이 세션이 만든 문답이 아니면 건드리지 않음


# ------------------------------------------------------------ 상충 감지: 참고용 답은 판정을 확정하지 않고 보류로만 바꿈
def test_reference_answer_conflict_only_withholds_never_confirms():
    crit = "Pregnant or breastfeeding"
    rec = "현재 임신 12주."
    r = _pair("임신·수유 여부를 포함해 과거 병력을 알려주세요.", "임신하지 않았습니다", "physician_reference")
    assert ed.check(crit, "현재 임신 12주", rec, holds="yes", qa=[], session_id="S1").ok
    c = ed.check(crit, "현재 임신 12주", rec + _block(1, r), holds="yes", qa=[r], session_id="S1")
    assert not c.ok and c.directness == "conflicting"  # 기록 확인 필요(보류). 반대 방향 확정 근거는 되지 않음
    c = ed.check(crit, r.answer, rec + _block(1, r), holds="no", qa=[r], session_id="S1")
    assert not c.ok and c.flags == ["physician_reference_answer"]
