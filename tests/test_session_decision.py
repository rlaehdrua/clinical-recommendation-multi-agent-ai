"""Decision Loop의 Session·웹 연결 (Phase 3, D). 기본 off, 켜도 판정·질문·추천·웹 흐름은 그대로. API 호출 없음."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import decision_loop, store_patch
from ctrec.agents import matcher

import test_session_fact_shadow as harness
from test_session_fact_shadow import _outputs, _run, calls  # noqa: F401 - calls는 pytest fixture


def _reset(calls):
    calls["questions"].clear()
    calls["recommend"].clear()


# ------------------------------------------------------------ Session
def test_off_by_default(calls):
    s = _run()
    assert s.decision_loop == "off" and s.decision_diagnostics == [] and s._decision_tracker is None
    assert "decision_diagnostic" not in s.result()
    assert not any(r["agent"] == "decision" for r in s.tracer.records)


def test_diagnostic_keeps_results_and_llm_calls(calls, monkeypatch):
    n = {"calls": 0}

    def counted(*a, **k):
        n["calls"] += 1
        return harness._fake_matcher_llm(*a, **k)
    monkeypatch.setattr(matcher.llm, "structured", counted)
    off = _outputs(_run(), calls)
    off_calls = n["calls"]
    _reset(calls)
    n["calls"] = 0
    s = _run(decision_loop="diagnostic")
    assert _outputs(s, calls) == off and n["calls"] == off_calls  # 판정·질문·추천 입력 동일, LLM 추가 호출 없음
    first, second = s.decision_diagnostics
    assert (first["round"], first["status"]) == (0, "ok") and (second["round"], second["status"]) == (1, "ok")
    asks = [a for a in first["actions"] if a["action_type"] == "ASK_USER"]
    assert asks and asks[0]["target_criterion_ids"] == ["T1:I2"]  # EGFR 정보 부족을 질문 후보로
    assert all(a["status"] == "proposed_not_dispatched" for a in asks)  # 진단: 질문을 끼워 넣지 않음
    # 답변 뒤: 자기 보고 EGFR -> 기존 Matcher가 UNVERIFIED -> 문서 요청 (다시 묻지 않음)
    kinds = {(a["action_type"], tuple(a["target_criterion_ids"])) for a in second["actions"]}
    assert ("REQUEST_DOCUMENT", ("T1:I2",)) in kinds and not any(k[0] == "ASK_USER" and k[1] == ("T1:I2",)
                                                                  for k in kinds)
    assert s.result()["decision_diagnostic"] == s.decision_diagnostics


def test_diagnostic_works_with_other_diagnostics(calls):
    off = _outputs(_run(), calls)
    _reset(calls)
    s = _run(decision_loop="diagnostic", dependency_diagnostic=True, fact_compare="deterministic")
    assert _outputs(s, calls) == off
    assert s.decision_diagnostics[-1]["status"] == "ok" and s.dependency_diagnostics[-1]["status"] == "ok"


def test_failure_isolated_and_invalid_mode(calls, monkeypatch):
    off = _outputs(_run(), calls)
    _reset(calls)

    def boom(*a, **k):
        raise ValueError("민감할 수 있는 메시지 11.2")
    monkeypatch.setattr(decision_loop.DecisionTracker, "observe", boom)
    s = _run(decision_loop="diagnostic")
    assert _outputs(s, calls) == off and s.errors == []
    assert s.decision_diagnostics[0] == {"round": 0, "status": "error", "error_type": "ValueError"}
    assert {e["status"] for e in _run(decision_loop="always").decision_diagnostics} == {"error"}
    assert {e["status"] for e in _run(decision_loop="diagnostic", fact_shadow=False).decision_diagnostics} == {"skipped"}


def test_no_patient_values_no_files_frozen_intact(calls, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _run(decision_loop="diagnostic")
    assert list(tmp_path.iterdir()) == []
    text = json.dumps(s.decision_diagnostics, ensure_ascii=False)
    for value in ("EGFR 양성", "11.2", "female", "62세", "비소세포폐암"):
        assert value not in text
    assert store_patch.verify_frozen() == []


def test_sessions_isolated(calls):
    a = _run(pid="A1", decision_loop="diagnostic")
    b = _run(pid="B2", decision_loop="diagnostic")
    assert a._decision_tracker is not b._decision_tracker
    snaps_a = a._decision_tracker.matched_snapshots.values()
    assert all(f.patient_id == "A1" for snap in snaps_a for f in snap.facts)


# ------------------------------------------------------------ 웹
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
import web_harness as h  # noqa: E402
from ctrec.web import app as web  # noqa: E402

BODY = {"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}


def _web_run(c, body):
    rid = c.post("/api/runs", json=body).json()["run_id"]
    run = web.RUNS[rid]
    h.wait_for(run, "questions")
    c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": h.SYN_ANSWER}})
    h.wait_for(run, "end", 30)
    return rid, h.sse_events(c.get(f"/api/runs/{rid}/events").text)


def _shape(events):
    """비교용: 진단 이벤트를 뺀 이벤트 종류와 결과 판정 (시각·ID·환자 ID 제외)."""
    kinds = [e["type"] for e in events if e["type"] not in ("decision_diagnostic",)]
    res = next(e for e in events if e["type"] == "result")["result"]
    return kinds, {t: (m["eligibility"], m["match_status"], [(a["rule_id"], a["passes"]) for a in m["assessments"]])
                   for t, m in res["matches"].items()}, res["recommendation"]["ranked"]


def test_web_default_has_no_diagnostic_events_and_same_flow(monkeypatch, tmp_path):
    h.install(monkeypatch, tmp_path)
    _, off = _web_run(h.browser(), BODY)
    assert not any(e["type"] == "decision_diagnostic" for e in off)
    res = next(e for e in off if e["type"] == "result")["result"]
    assert "decision_diagnostic" not in res
    h.install(monkeypatch, tmp_path)
    _, on = _web_run(h.browser(), {**BODY, "decision_diagnostic": True})
    diag = [e for e in on if e["type"] == "decision_diagnostic"]
    assert len(diag) == 2 and {d["diagnostic"]["status"] for d in diag} == {"ok"}  # 첫 매칭 + 답변 뒤 재매칭
    assert _shape(on) == _shape(off)  # 진단 이벤트 외 흐름·판정·추천 동일
    assert not any(e["type"] == "trace" and e["record"].get("agent") == "decision" for e in on)
    assert not (tmp_path / "outputs").exists()  # 저장 기본 off 유지


def test_web_cancel_with_diagnostic_and_access_control(monkeypatch, tmp_path):
    h.install(monkeypatch, tmp_path)
    owner, stranger = h.browser(), h.browser()
    rid = owner.post("/api/runs", json={**BODY, "decision_diagnostic": True}).json()["run_id"]
    run = web.RUNS[rid]
    h.wait_for(run, "questions")
    assert stranger.get(f"/api/runs/{rid}/events").status_code == 404  # 소유권 통제 유지
    assert owner.post(f"/api/runs/{rid}/cancel").status_code == 200
    h.wait_for(run, "end", 10)
    events = h.sse_events(owner.get(f"/api/runs/{rid}/events").text)
    assert events[-1]["state"] == "cancelled" and not any(e["type"] == "result" for e in events)
    assert [e["type"] for e in events].count("decision_diagnostic") == 1  # 취소 뒤 추가 진단 없음
