"""한 라운드의 질문 ID는 서로 달라야 함 (감사 V5 F-08). 답변이 question_id로 모이므로 겹치면 한 답이 두 질문에 붙음."""

from __future__ import annotations

from ctrec import llm, pipeline
from ctrec import question_eval as qe
from ctrec.agents import question_generator
from ctrec.agents.answerers import NoAnswerer
from ctrec.schemas import ClarifyingQuestion, QuestionSet
from ctrec.sim import demo
from ctrec.trace import Tracer

TEXT_NO_AGE = "Synthetic NSCLC patient, male, stage IV non-small cell lung cancer. ECOG 1."


def _questions() -> list[ClarifyingQuestion]:
    return [ClarifyingQuestion(question_id="Q1", question="최근 혈액검사의 혈색소(hemoglobin) 수치를 알려주세요.",
                               answer_type="number", unit="g/dL", target=["T-B:I3"], purpose="eligibility", why="x"),
            ClarifyingQuestion(question_id="Q1", question="환자분의 나이는 몇 세인가요?", answer_type="number",
                               unit="세", target=["T-A:I1", "T-B:I1", "T-C:I1"], purpose="eligibility", why="x")]


class KeyedAnswerer:
    """웹 화면처럼 {question_id: 답}으로 돌려줌. Q1 카드에만 답함."""

    def __init__(self):
        self.asked: list[str] = []

    def answer(self, questions, patient_id):
        self.asked = [q.question_id for q in questions]
        return {q.question_id: ("15" if q.question_id == "Q1" else "모름") for q in questions}


def _session(answerer) -> pipeline.Session:
    st = demo.trials()
    case = pipeline.PatientCase(patient_id="SYN-Q", raw_text=TEXT_NO_AGE, input_source="clinical_record")
    s = pipeline.Session(case=case, trials={t.parsed.trial_id: dict(t.trial) for t in st}, answerer=answerer,
                         tracer=Tracer(None, verbose=False), reference_date=demo.REFERENCE_DATE)
    for t in st:
        s.parsed[t.parsed.trial_id] = t.parsed
    return s


def test_llm_duplicate_question_ids_are_made_unique():
    with qe.OfflineEnvironment(5):
        s = _session(NoAnswerer())
        s.profile_patient()
        s.match()
        llm.structured = lambda model_cls, **k: QuestionSet(questions=_questions())  # OfflineEnvironment가 되돌림
        qs = question_generator.generate_questions(s.profile, list(s.matches.values()), [], 5)
    ids = [q.question_id for q in qs.questions]
    assert len(ids) == 2 and len(set(ids)) == 2, ids
    assert ids[0] == "Q1"  # 처음 것은 그대로


def test_one_answer_is_not_recorded_for_two_questions():
    def gen(profile, matches, qa_log, n, acquisition_plan=None):
        return QuestionSet(questions=_questions())

    ans = KeyedAnswerer()
    with qe.OfflineEnvironment(5), question_generator.use_generator(gen):
        s = _session(ans)
        s.profile_patient()
        s.match()
        s.clarify()
    assert len(set(ans.asked)) == len(ans.asked) == 2
    ids = [p.question_id for p in s.qa_log]
    assert len(set(ids)) == len(ids)
    answered = [p for p in s.qa_log if p.answer == "15"]
    assert len(answered) == 1 and "혈색소" in answered[0].question
