"""WP8 실행기 (web/engine_runner.py): 실제 Session.clarify의 확인 질문을 정책 엔진이 고름 (오프라인 대역, API 호출 없음).

E2E: 별칭 masked 사례 -> 실제 Session.run_fixed (프로필·매칭은 오프라인 대역) -> 정책 엔진이 질문 선택 -> "모름" ->
같은 항목은 문서 정보원으로, 다른 항목 질문은 계속 -> 문서도 못 얻으면 그 항목만 보류.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import question_eval as qe  # noqa: E402
from ctrec.agents import question_generator  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.eval_hygiene import AliasMap, load_cases_hygienic  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.web import engine_runner as er  # noqa: E402

PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}
CASE = "M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1"
WEB = Path(__file__).resolve().parents[1] / "src" / "ctrec"


class _Tracer:
    def __init__(self):
        self.recs: list[tuple[str, str, dict]] = []

    def log(self, agent, event, **kw):
        self.recs.append((agent, event, kw))


class _Answerer:
    def __init__(self, fn):
        self.fn = fn
        self.asked: list = []

    def answer(self, questions, patient_id):
        self.asked.append(list(questions))
        return {q.question_id: self.fn(q) for q in questions}


def _case():
    amap = AliasMap(salt="engine-runner")
    amap.alias(CASE)
    hcs, _, _ = load_cases_hygienic(PARSED, only=[CASE], alias_map=amap)
    return hcs[0]


def _run(answer=lambda q: "모름", rounds=9, monkeypatch=None):
    hc = _case()
    c = hc.case
    if monkeypatch is not None:  # 엔진 모드에서는 기존 LLM 질문 생성 경로를 타지 않아야 함
        monkeypatch.setattr(question_generator, "compact_json",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM question generator used")))
    with qe.OfflineEnvironment(3):
        s = Session(case=PatientCase(patient_id=c.patient_id, raw_text=c.raw_text, input_source=c.input_source,
                                     candidate_trials=list(c.candidate_trials or [])),
                    trials={t: TRIALS[t] for t in hc.gt.candidate_trials}, answerer=_Answerer(answer),
                    tracer=_Tracer(), max_rounds=rounds)
        with er.activate(s) as runner:
            s.run_fixed()
    return s, runner


@pytest.fixture(scope="module")
def no_answer_run():
    return _run()


def test_engine_picks_the_questions_and_keeps_asking_after_no_answer(no_answer_run, monkeypatch):
    s, r = _run(monkeypatch=monkeypatch, rounds=2)  # LLM 질문 생성기를 부르면 실패
    assert s.rounds == 2 and len(r.asked) == 6
    assert all(a.action.action_type == "ASK_PATIENT" for a in r.asked)
    s, r = no_answer_run
    first, second = r.plans[0], r.plans[1]
    assert {a["action_type"] for a in first["asked"]} == {"ASK_PATIENT"}
    assert all(o["error_code"] == C.NO_ANSWER for o in second["outcomes"])  # "모름" = 항목 단위 실패
    assert second["asked"] and {a["source_id"] for a in second["asked"]} == {"patient_interview"}  # 다른 항목 계속
    asked_first = {tuple(a["targets"]) for a in first["asked"]}
    assert not asked_first & {tuple(a["targets"]) for a in second["asked"]}  # 같은 항목을 다시 묻지 않음


def test_no_answer_item_moves_to_a_document_source_then_is_held(no_answer_run):
    s, r = no_answer_run
    docs = [a for p in r.plans for a in p["asked"] if a["action_type"] == "REQUEST_DOCUMENT"
            and a["reason_code"] == "alternative_source_after_no_answer"]
    assert docs, "모름 뒤 대체 문서 요청이 실행되지 않음"
    first_ask = {tuple(a["targets"]) for a in r.plans[0]["asked"]}
    assert first_ask & {tuple(a["targets"]) for a in docs}  # 처음 "모름"이 나온 항목이 문서 요청으로 이어짐
    seen = [(a.action.source_id, a.action.info_key) for a in r.asked]
    assert len(seen) == len(set(seen))  # 같은 (정보원, 항목)을 두 번 요청하지 않음
    held = [a for a in r.policy.propose(r.cohort()) if a.reason_code == "item_sources_exhausted"]
    assert held and all(a.action_type == "DEFER" for a in held)  # 문서에서도 못 얻은 항목은 그 항목만 보류


def test_pair_is_not_blocked_while_other_items_remain(no_answer_run):
    s, r = no_answer_run
    co = r.cohort()
    remaining = [a for a in r.policy.propose(co) if a.action_type in ("ASK_PATIENT", "REQUEST_DOCUMENT")]
    statuses = {p.status for p in co.pairs()}
    assert remaining and C.PairStatus.BLOCKED_FAILURE not in statuses
    for p in r.plans:
        assert "BLOCKED_FAILURE" not in p["pairs"] or not p["asked"]


def test_answered_question_is_not_an_item_failure():
    s, r = _run(answer=lambda q: "네", rounds=2)
    outs = r.plans[1]["outcomes"]
    assert outs and all(o["status"] == "succeeded" and o["error_code"] is None for o in outs)
    assert r.ops[r.alias].failed_items == frozenset()


def test_questions_keep_action_targets_and_purpose(no_answer_run):
    s, r = no_answer_run
    for a, q in ((a, q) for a in r.asked for qs in s.answerer.asked for q in qs if q.question_id == a.question_id):
        assert list(q.target) == list(a.action.target_criteria) and q.purpose == a.action.purpose
    docs = [q for qs in s.answerer.asked for q in qs if q.question.startswith("[기록 확인 요청")]
    assert docs and all("모름" in q.question for q in docs)


def test_trace_has_codes_only_no_alias_or_patient_id(no_answer_run):
    s, r = no_answer_run
    blob = json.dumps([rec for rec in s.tracer.recs if rec[0] == "engine"], ensure_ascii=False, default=str)
    assert r.alias not in blob and CASE not in blob and s.case.patient_id not in blob
    assert any(rec[1] == "plan" for rec in s.tracer.recs if rec[0] == "engine")


def test_safety_unfunded_stops_questions_for_that_round(monkeypatch):
    real = er.LegacyAdapterPolicy.select

    def unfunded(self, *a, **k):
        return real(self, *a, **k).model_copy(update={"safety_unfunded": ("conflict-check",)})
    monkeypatch.setattr(er.LegacyAdapterPolicy, "select", unfunded)
    s, r = _run(rounds=2)
    assert r.asked == [] and s.qa_log == []
    assert any(rec[:2] == ("engine", "safety_unfunded") for rec in s.tracer.recs)


def test_default_mode_is_legacy_and_changes_nothing(monkeypatch):
    monkeypatch.delenv(er.ENV_POLICY_MODE, raising=False)
    assert er.policy_mode() == "legacy"
    with er.for_mode(object()):
        assert question_generator._override_var.get() is None
    monkeypatch.setenv(er.ENV_POLICY_MODE, "engine")
    assert er.policy_mode() == "engine"
    monkeypatch.setenv(er.ENV_POLICY_MODE, "unknown")
    assert er.policy_mode() == "legacy"


def test_entry_points_use_the_same_runner():
    for path in (WEB / "web" / "app.py", WEB / "cli.py"):
        assert "engine_runner.for_mode(session)" in path.read_text(encoding="utf-8"), path
    assert "engine_runner" not in (WEB / "pipeline.py").read_text(encoding="utf-8")  # 보호 파일은 그대로
