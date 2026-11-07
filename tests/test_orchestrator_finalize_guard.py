"""오케스트레이터 마무리 경계 (감사 V5 F-01, F-02). 실제 LLM 없이 대본 응답으로 루프를 돌림."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from ctrec import llm, pipeline
from ctrec import question_eval as qe
from ctrec.agents.answerers import NoAnswerer
from ctrec.orchestrator import Orchestrator
from ctrec.sim import demo
from ctrec.trace import Tracer

TEXT = "Synthetic NSCLC patient. 64-year-old male with stage IV non-small cell lung cancer. ECOG 2."


def make_session(text: str = TEXT, answerer=None) -> pipeline.Session:
    st = demo.trials()
    case = pipeline.PatientCase(patient_id="SYN-F", raw_text=text, input_source="clinical_record")
    s = pipeline.Session(case=case, trials={t.parsed.trial_id: dict(t.trial) for t in st},
                         answerer=answerer or NoAnswerer(), tracer=Tracer(None, verbose=False),
                         reference_date=demo.REFERENCE_DATE)
    for t in st:
        s.parsed[t.parsed.trial_id] = t.parsed
    return s


def turn(*calls):
    """한 응답에 도구 호출 여러 개. calls: (이름, 인자) 또는 이름."""
    blocks = []
    for c in calls:
        name, args = (c, {}) if isinstance(c, str) else c
        blocks.append(SimpleNamespace(type="tool_use", name=name, input=args, id=f"tu_{uuid.uuid4().hex[:8]}"))
    return SimpleNamespace(content=blocks, stop_reason="tool_use")


class Scripted:
    def __init__(self, script):
        self.script = list(script)
        self.results: list[list[dict]] = []

    def __call__(self, *, system, messages, tools=None, **k):
        if messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
            self.results.append(messages[-1]["content"])
        if self.script:
            return self.script.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn")


def run(script, **kw) -> tuple[pipeline.Session, Scripted]:
    with qe.OfflineEnvironment(3):
        s = make_session(**kw)
        fake = Scripted(script)
        llm.create = fake  # OfflineEnvironment가 끝날 때 원래 값으로 되돌림
        Orchestrator(s).run()
    return s, fake


# ------------------------------------------------------------------ F-02
def test_early_finalize_keeps_unevaluated_candidates_as_not_evaluated():
    s, _ = run([turn("profile_patient"), turn(("evaluate_trials", {"trial_ids": ["T-A"]})),
                turn("finalize_recommendation")])
    assert set(s.candidate_ids()) <= set(s.matches), "후보가 결과에서 조용히 빠지면 안 됨"
    assert s.matches["T-A"].eligibility == "INELIGIBLE"  # 평가한 판정은 그대로
    for tid in ("T-B", "T-C"):
        assert s.matches[tid].eligibility == "NOT_EVALUATED"  # 판정을 지어내지 않음
        assert s.matches[tid].assessments == []
    reason = s.recommendation.no_recommendation_reason or ""
    assert "모두 선정/제외 기준에 맞지 않았습니다" not in reason or "판정하지 못했으므로(부적격 아님)" in reason
    assert "T-B" in reason and "T-C" in reason


# ------------------------------------------------------------------ F-01
class AgeAnswerer:
    def answer(self, questions, patient_id):
        return {q.question_id: "15-year-old" for q in questions}


def test_tool_calls_after_finalize_in_the_same_turn_are_not_executed(monkeypatch):
    from ctrec.agents import question_generator

    seen: dict = {}
    real_finalize = pipeline.Session.finalize

    def spy(self):
        real_finalize(self)
        seen["at_finalize"] = {t: m.eligibility for t, m in self.matches.items()}

    monkeypatch.setattr(pipeline.Session, "finalize", spy)
    with question_generator.use_generator(qe.baseline_generator):
        s, _ = run([turn("profile_patient"), turn(("evaluate_trials", {"trial_ids": []})),
                    turn("finalize_recommendation", "ask_clarifying_questions",
                         ("evaluate_trials", {"trial_ids": []}))],
                   text="Synthetic NSCLC patient, male, stage IV non-small cell lung cancer. ECOG 1.",
                   answerer=AgeAnswerer())
    assert s.qa_log == [] and s.rounds == 0, "마무리 뒤 호출은 실행되지 않아야 함"
    # 추천이 계산된 판정과 최종 판정이 같음 (추천만 옛 판정으로 남지 않음)
    assert {t: m.eligibility for t, m in s.matches.items()} == seen["at_finalize"]


def test_post_finalize_calls_get_error_tool_results():
    events: list[dict] = []
    with qe.OfflineEnvironment(3):
        s = make_session()
        s.tracer = Tracer(None, verbose=False, listener=events.append)
        fake = Scripted([turn("profile_patient"), turn(("evaluate_trials", {"trial_ids": []})),
                         turn("finalize_recommendation", "ask_clarifying_questions")])
        convo: list = []

        def create(**k):
            convo[:] = [k["messages"]]  # 루프가 계속 덧붙이는 대화 목록 자체를 잡아 둠
            return fake(**k)

        llm.create = create
        Orchestrator(s).run()
    final = [m for m in convo[0] if m["role"] == "user" and isinstance(m["content"], list)][-1]["content"]
    assert len(final) == 2 and final[0].get("is_error") is not True  # 마무리 결과는 정상
    assert final[1]["is_error"] is True and "finalize_recommendation" in final[1]["content"]
    assert [e["tool"] for e in events if e["event"] == "tool_rejected"] == ["ask_clarifying_questions"]
    assert not [e for e in events if e["event"] == "tool_call" and e["tool"] == "ask_clarifying_questions"]
