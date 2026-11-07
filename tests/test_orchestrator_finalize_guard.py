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
