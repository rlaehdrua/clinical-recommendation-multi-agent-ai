"""P0: 원문에 근거가 없는 환자값으로 적격·부적격을 확정하지 않음 (실제 Session, 합성 데이터, LLM·API 호출 없음).

프로필러는 LLM 자리를 스크립트로 고정합니다: 원문에 없는 값을 만들어 내는 최악의 경우를 그대로 재현합니다.
모든 사례에서 판정, PairStatus, 행동을 함께 봅니다. 실제 LLM에서의 발생 빈도는 검증하지 않았습니다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_decisions import TRIAL, _parsed  # noqa: E402
from test_offline import _rule  # noqa: E402

from ctrec import config, pipeline  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.decision_state import SessionView, build_state  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.schemas import (ClarifyingQuestion, LabValue, LLMAssessment, MatcherOutput, PatientProfile, QuestionSet,  # noqa: E402
                           RuleMeta)

PID = "PA-0123456789abcdef"
AGE = _rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18", unit="years")
TOO_OLD = _rule(rule_id="I1", text="Age 80 years or older", field="age", operator=">=", value="80", unit="years")
SEX = _rule(rule_id="I1", text="Female participants", field="sex", operator="==", value="female")
ECOG = _rule(rule_id="I2", text="ECOG 0-1", category="performance_status", field="ecog", operator="<=", value="1")
HB = _rule(rule_id="I3", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin", operator=">=", value="9",
           unit="g/dL")
HB_HIGH = HB.model_copy(update={"text": "Hemoglobin >= 15 g/dL", "value": "15"})
OPEN = _rule(rule_id="I9", text="Adequate renal function", category="comorbidity", structured_evaluable=False)  # 질문 라운드가 돌게 함
JUDGE = _rule(rule_id="I5", text="Adequate organ function in the investigator's judgment", category="comorbidity",
              structured_evaluable=False, underspecified=True)
SELF_OK = {"I3": RuleMeta(required_evidence="self_report_ok")}
RECORD = {"I2": RuleMeta(required_evidence="clinical_record"), "I3": RuleMeta(required_evidence="clinical_record")}
Q_AGE, Q_SEX, Q_OTHER, Q_HB = "나이가 어떻게 되시나요?", "성별이 어떻게 되시나요?", "임신 중이신가요?", "혈색소 수치를 알려주세요"
HELD = {"INFO_MISSING", "VERIFICATION_PENDING", "EXPERT_REVIEW"}


class Tracer:
    def log(self, *a, **k):
        pass


def prof(age=None, sex="unknown", ecog=None, labs=()):
    return PatientProfile(patient_id=PID, age=age, age_value=age, age_unit="years" if age is not None else None, sex=sex,
                          primary_diagnosis=None, disease_stage=None, diagnoses=[], biomarkers=[], labs=list(labs),
                          medications=[], treatment_history=[], ecog=ecog, other_facts=[], missing_or_ambiguous=[])


def hb(value=12.0, quote="Hb 12 g/dL"):
    return LabValue(name="hemoglobin", value=value, unit="g/dL", date=None, source_quote=quote)


class Outcome:
    def __init__(self, s: Session, co: CohortState):
        self.session = s
        self.match = s.matches["T1"]
        self.pair = co.pair(PID, "T1")
        self.status = self.pair.status.value
        self.actions = {a.action_type for a in LegacyAdapterPolicy().propose(co) if a.action_type != "FINALIZE"}
        self.a = self.match.assessments[0]


@pytest.fixture
def run(monkeypatch):
    def go(initial, rules, before, after=None, answer=None, meta=None, input_source="unknown", question="확인 질문",
           target="T1:I1", budget=None):
        p = _parsed([r.model_copy() for r in rules])
        p.rule_meta = dict(meta or {})

        def stub(model, *, system=None, user=None, **k):
            assert model is MatcherOutput
            rs = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
            return MatcherOutput(assessments=[LLMAssessment(
                rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent", confidence="low",
                evidence="근거 없음", reasoning="stub", missing_info="확인 필요") for r in rs])

        def questions(*a, **k):
            return QuestionSet(questions=[ClarifyingQuestion(question_id="Q1", question=question, answer_type="free_text",
                                                             target=[target], purpose="eligibility", why="x")])

        monkeypatch.setattr(matcher.llm, "structured", stub)
        monkeypatch.setattr(config, "WRITE_REPORT", False)
        monkeypatch.setattr(pipeline.trial_store, "get", lambda t: p)
        monkeypatch.setattr(pipeline.patient_profiler, "profile_patient",
                            lambda pid, raw, reference_date=None: after if after is not None and "[추가 확인 정보" in raw else before)
        monkeypatch.setattr(pipeline.question_generator, "generate_questions", questions)

        class Ans:
            def answer(self, qs, pid):
                return {q.question_id: answer for q in qs}

        s = Session(case=PatientCase(PID, initial, input_source=input_source), trials={"T1": dict(TRIAL)},
                    answerer=Ans(), tracer=Tracer(), **({} if budget is None else {"question_budget": budget}))
        s.profile_patient()
        s.match()
        if answer is not None:
            s.clarify()
        v = SessionView.from_session(s)
        v.matched_versions = {t: v.snapshot.version for t in s.matches}
        return Outcome(s, CohortState.from_decision_states({PID: build_state(v)}))
    return go


def held(o: Outcome, *allowed: str) -> None:
    """확정 적격도 확정 부적격도 아닌 보류."""
    assert o.match.eligibility == "UNCERTAIN"
    assert o.status in (allowed or HELD)
    assert o.a.passes is None


def confirmed(o: Outcome) -> None:
    assert (o.match.eligibility, o.status) == ("ELIGIBLE", "CONFIRMED_ELIGIBLE")


# ------------------------------------------------------------ 1, 2: 원문에 없는 값
def test_01_invented_age_does_not_confirm(run):
    held(run("폐암 환자.", [AGE], prof(age=60)), "INFO_MISSING")


def test_01b_invented_age_does_not_rule_out(run):
    held(run("폐암 환자.", [TOO_OLD], prof(age=60)), "INFO_MISSING")


def test_02_invented_lab_does_not_confirm(run):
    held(run("폐암 환자.", [HB], prof(labs=[hb()]), meta=SELF_OK), "INFO_MISSING")


def test_02b_invented_lab_does_not_rule_out(run):
    held(run("폐암 환자.", [HB_HIGH], prof(labs=[hb()]), meta=SELF_OK), "INFO_MISSING")


def test_02c_real_quote_with_a_different_value_is_not_grounded(run):
    """인용 문자열이 원문에 있다는 것만으로 값이 맞다고 보지 않음."""
    held(run("혈색소 수치는 정상 범위로 기록됨.", [HB], prof(labs=[hb(12.0, "혈색소 수치는 정상 범위로 기록됨")]), meta=SELF_OK), "INFO_MISSING")


def test_02d_quote_about_another_lab_is_not_grounded(run):
    held(run("Platelets 12 g/dL 입니다.", [HB], prof(labs=[hb(12.0, "Platelets 12 g/dL")]), meta=SELF_OK), "INFO_MISSING")


# ------------------------------------------------------------ 3~5: 답하지 않은 답
@pytest.mark.parametrize("answer", ["모름", "잘 모르겠어요", "글쎄요", "글쎄요, 잘 기억이 안 나네요", "모름. 기억 안 남"])
def test_03_04_non_answers_do_not_ground_a_value(run, answer):
    o = run("폐암 환자.", [AGE], prof(), prof(age=60), answer, question=Q_AGE)
    held(o, "INFO_MISSING")


@pytest.mark.parametrize("answer", ["", "   "])
def test_05_empty_answer_does_not_ground_a_value(run, answer):
    held(run("폐암 환자.", [AGE], prof(), prof(age=60), answer, question=Q_AGE), "INFO_MISSING")


# ------------------------------------------------------------ 6, 7: 정상 답변·수사 표현
@pytest.mark.parametrize("answer", ["62세", "62세입니다", "저는 62세예요", "62살이요", "62", "62요", "확실히 62세입니다"])
def test_06_clear_age_answer_is_accepted(run, answer):
    confirmed(run("폐암 환자.", [AGE], prof(), prof(age=62), answer, question=Q_AGE))


@pytest.mark.parametrize("initial", ["sixty-two years old with NSCLC", "Sixty two year old patient", "육십이세 환자", "예순두 살 환자",
                                     "1964년생 환자"])
def test_07_number_words_in_record_are_accepted(run, initial):
    confirmed(run(initial, [AGE], prof(age=62)))


def test_07b_number_word_answer_is_accepted(run):
    confirmed(run("폐암 환자.", [AGE], prof(), prof(age=62), "예순둘이에요", question=Q_AGE))


def test_07c_number_word_must_match_the_value(run):
    held(run("sixty-two years old with NSCLC", [AGE], prof(age=70)), "INFO_MISSING")


def test_07d_ecog_word_is_accepted(run):
    confirmed(run("ECOG one.", [ECOG], prof(ecog=1), meta={"I2": RuleMeta(required_evidence="self_report_ok")}))


# ------------------------------------------------------------ 8~10: 성별
@pytest.mark.parametrize("answer,sex", [("F", "female"), ("여", "female"), ("여요", "female"), ("M", "male"), ("남", "male")])
def test_08_bare_sex_answer_to_a_sex_question(run, answer, sex):
    o = run("폐암 환자.", [SEX], prof(), prof(sex=sex), answer, question=Q_SEX)
    if sex == "female":
        confirmed(o)
    else:
        assert o.match.eligibility == "INELIGIBLE"


@pytest.mark.parametrize("answer", ["F", "여", "M", "남"])
def test_09_bare_sex_token_on_another_question_is_not_sex(run, answer):
    sex = "female" if answer in ("F", "여") else "male"
    held(run("폐암 환자.", [SEX], prof(), prof(sex=sex), answer, question=Q_OTHER), "INFO_MISSING")


@pytest.mark.parametrize("text", ["she has NSCLC", "he has NSCLC", "그녀는 폐암 환자", "F", "여"])
def test_10_pronoun_or_bare_token_in_record_is_not_sex(run, text):
    sex = "male" if text.startswith("he") else "female"
    held(run(text, [SEX], prof(sex=sex)), "INFO_MISSING")


@pytest.mark.parametrize("text", ["62세 여성", "여자", "female", "a woman", "62F", "62/F", "성별: 여", "sex: F"])
def test_10b_stated_sex_is_accepted(run, text):
    confirmed(run(text, [SEX], prof(sex="female")))


# ------------------------------------------------------------ 11, 12: 혼합, 충돌
def test_11_mixed_answer_keeps_the_certain_fact_only(run):
    rules = [AGE, HB]
    o = run("폐암 환자.", rules, prof(), prof(age=62, labs=[hb(12.0, "혈색소는 12쯤인 것 같아요")]),
            "62세인데 혈색소는 12쯤인 것 같아요", meta=SELF_OK, question="나이와 혈색소를 알려주세요")
    by = {a.rule_id: a for a in o.match.assessments}
    assert by["I1"].passes is True
    assert by["I3"].passes is None and o.match.eligibility == "UNCERTAIN"


@pytest.mark.parametrize("initial,profile_age,answer", [("62세 환자.", 70, "70세입니다"), ("62세 환자.", 62, "70세입니다")])
def test_12_conflicting_age_is_held(run, initial, profile_age, answer):
    held(run(initial, [AGE, OPEN], prof(age=62), prof(age=profile_age), answer, question=Q_AGE, target="T1:I9"), "INFO_MISSING")


def test_12b_conflicting_sex_is_held(run):
    held(run("62세 여성.", [SEX, OPEN], prof(sex="female"), prof(sex="male"), "남자입니다", question=Q_SEX, target="T1:I9"), "INFO_MISSING")


def test_12c_conflicting_lab_is_held(run):
    held(run("Hb 12 g/dL.", [HB, OPEN], prof(labs=[hb()]), prof(labs=[hb(8.0, "Hb 8 g/dL")]), "Hb 8 g/dL 입니다", meta=SELF_OK,
             question=Q_HB, target="T1:I9"), "INFO_MISSING")


def test_12d_another_age_in_the_record_is_not_a_conflict(run):
    """진단 시 나이처럼 기록 안의 다른 나이는 답변과 충돌이 아님."""
    confirmed(run("58세에 진단. 현재 62세.", [AGE], prof(age=62)))


# ------------------------------------------------------------ 13~17: 근거 수준
def test_13_real_self_report_on_self_report_ok_rule(run):
    o = run("폐암 환자.", [HB], prof(), prof(labs=[hb()]), "Hb 12 g/dL", meta=SELF_OK, question=Q_HB, target="T1:I3")
    confirmed(o)


def test_14_unlocated_value_on_self_report_ok_rule_is_held(run):
    held(run("폐암 환자.", [HB], prof(), prof(labs=[hb()]), "흡연은 안 해요", meta=SELF_OK, target="T1:I3"), "INFO_MISSING")


def test_15_self_report_on_record_rule_requests_a_document(run):
    o = run("폐암 환자.", [HB], prof(), prof(labs=[hb()]), "Hb 12 g/dL", meta=RECORD, question=Q_HB, target="T1:I3")
    held(o, "VERIFICATION_PENDING")
    assert o.a.needs_verification and o.a.self_reported_holds == "yes" and "REQUEST_DOCUMENT" in o.actions


def test_16_undeclared_document_on_record_rule_requests_a_document(run):
    o = run("Hb 12 g/dL.", [HB], prof(labs=[hb()]), meta=RECORD, input_source="unknown")
    held(o, "VERIFICATION_PENDING")
    assert "REQUEST_DOCUMENT" in o.actions


def test_16b_declared_record_without_the_value_keeps_the_document_path(run):
    """근거 없는 값이어도 기록 필요 기준은 INFO_MISSING으로 바뀌지 않고 문서 요청 경로를 유지."""
    o = run("폐암 환자.", [HB], prof(labs=[hb()]), meta=RECORD, input_source="clinical_record")
    held(o, "VERIFICATION_PENDING")
    assert "REQUEST_DOCUMENT" in o.actions and o.a.self_reported_holds is None


def test_17_declared_record_with_the_value_is_confirmed(run):
    """현행 정책 기록: 선언된 진료 기록의 값은 문서 검증 전에도 확정됨. docs/dev/night_safety_audit_2026_11_06.md 참고."""
    confirmed(run("Hb 12 g/dL.", [HB], prof(labs=[hb()]), meta=RECORD, input_source="clinical_record"))


def test_17b_document_rule_on_ineligible_value_still_rules_out(run):
    o = run("Hb 12 g/dL.", [HB_HIGH], prof(labs=[hb()]), meta=RECORD, input_source="clinical_record")
    assert o.match.eligibility == "INELIGIBLE" and o.status == "VERIFICATION_PENDING"


# ------------------------------------------------------------ 18~20
def test_18_clinician_only_rule_stays_with_the_clinician(run):
    o = run("폐암 환자. 62세.", [JUDGE], prof(age=62))
    assert (o.match.eligibility, o.match.match_status, o.status) == ("UNCERTAIN", "NEEDS_REVIEW", "EXPERT_REVIEW")
    assert "CLINICIAN_REVIEW" in o.actions


def test_19_exhausted_question_budget_does_not_confirm(run):
    o = run("폐암 환자.", [AGE], prof(age=60), prof(age=60), "62세", question=Q_AGE, budget=0)
    held(o, "INFO_MISSING")
    assert o.session.qa_log == []


def test_20_unknown_answer_leaves_the_item_open_and_records_the_try(run):
    o = run("폐암 환자.", [HB], prof(), prof(labs=[hb(12.0, "모르겠어요")]), "모르겠어요", meta=RECORD, question=Q_HB, target="T1:I3")
    held(o, "VERIFICATION_PENDING", "INFO_MISSING")
    assert len(o.session.qa_log) == 1 and o.actions


# ------------------------------------------------------------ 불확실한 답
@pytest.mark.parametrize("answer", ["62세쯤이요", "아마 62세", "62세일 거예요", "62세요?", "글쎄요, 62세", "모르겠어요 62세인가"])
def test_hedged_age_answer_is_held(run, answer):
    held(run("폐암 환자.", [AGE], prof(), prof(age=62), answer, question=Q_AGE), "INFO_MISSING")


def test_ecog_range_is_not_a_single_value(run):
    held(run("ECOG 0-1.", [ECOG], prof(ecog=1), meta={"I2": RuleMeta(required_evidence="self_report_ok")}), "INFO_MISSING")


def test_days_ago_is_not_an_age(run):
    held(run("62일 전 수술 후 회복 중.", [AGE], prof(age=62)), "INFO_MISSING")


def test_sex_all_rule_needs_no_sex_value(run):
    rule = SEX.model_copy(update={"text": "Sex: All", "value": "all"})
    confirmed(run("62세 폐암 환자.", [rule, AGE.model_copy(update={"rule_id": "I2"})], prof(age=62)))
