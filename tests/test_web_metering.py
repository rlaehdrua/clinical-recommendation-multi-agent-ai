"""웹 실행별 기록 전용 LLM 계측 (Phase 5-3). 실제 웹 앱 + 실제 llm.create 경로, LLM은 SDK 모양 대역. 네트워크 없음.

기록 전용: 호출을 막거나 바꾸지 않고(요청 인자·SSE·취소 동작이 기록 off와 같음), 비용 상한을 보장한다고 표시하지 않음.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path

import anthropic
import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402

from ctrec import call_guard, config, llm, store_patch, web_metering as wm  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.schemas import MatcherOutput  # noqa: E402
from ctrec.web import app as web  # noqa: E402

REAL_ANTHROPIC = anthropic.Anthropic
KEY = "sk-ant-test-metering-key-0000"
KNOWN_EVENTS = {"status", "trace", "questions", "answers", "result", "end", "error", "decision_diagnostic"}


@pytest.fixture(autouse=True)
def no_real_client(monkeypatch):
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: made.append(1) or pytest.fail("API 클라이언트 생성"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    before = call_guard.counters()  # 다른 테스트의 웹 실행(답변 대기 중인 데몬 스레드)이 남아 있을 수 있음
    yield
    after = call_guard.counters()
    assert made == [] and after["exclusive"] == before["exclusive"] == 0
    for run in list(web.RUNS.values()):  # 이 테스트가 시작한 실행이 끝났으면 기록 등록도 풀림
        if run.done:
            assert run.metering is not None


def _run(c, answer=h.SYN_ANSWER, text=h.SYN_TEXT):
    r = c.post("/api/runs", json={"custom_text": text, "trial_ids": h.TRIAL_IDS, "screen": False})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    run = web.RUNS[rid]
    q = h.wait_for(run, "questions")
    assert c.post(f"/api/runs/{rid}/answers",
                  json={"answers": {x["question_id"]: answer for x in q["questions"]}}).status_code == 200
    h.wait_for(run, "end", 30)
    return rid, run, h.sse_events(c.get(f"/api/runs/{rid}/events").text)


def _metering(c, rid):
    r = c.get(f"/api/runs/{rid}/metering")
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------- 8: 실행별 분리, 상태 FULLY


def test_runs_metered_separately_and_fully(monkeypatch, tmp_path):
    sdk = h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    c1, c2 = h.browser(client=("198.51.100.21", 1)), h.browser(client=("198.51.100.22", 2))
    out = {}
    ts = [threading.Thread(target=lambda c=c, k=k: out.__setitem__(k, _run(c))) for k, c in (("a", c1), ("b", c2))]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    ma, mb = _metering(c1, out["a"][0]), _metering(c2, out["b"][0])
    for m in (ma, mb):
        assert m["status"] == wm.FULLY_METERED and m["in_progress"] is False and m["usage_confirmed"] is True
        assert {"orchestrator", "patient_profiler", "matcher", "recommender"} <= set(m["by_stage"])
        assert m["input_tokens"] == 100 * m["calls"] and m["output_tokens"] == 20 * m["calls"]
        assert m["cache_read_tokens"] == 7 * m["calls"]
        assert m["response_models"] == {config.MODEL: m["calls"]} and m["retries"] == wm.UNKNOWN  # SDK가 아니라 안 보임
        assert m["elapsed_s"] >= 0 and m["unattributed_calls_during_run"] == 0
    assert ma["calls"] + mb["calls"] == len(sdk.requests) and ma["run_id"] != mb["run_id"]


# ---------------------------------------------------------------- 9·14: 기존 동작 유지 (요청 인자·SSE·결과·취소)


def _strip(events):
    return [(e["type"], e.get("state")) for e in events if e["type"] != "trace"]


def _norm(obj) -> str:
    """실행마다 달라지는 값(웹 환자 ID, 대역의 도구 호출 ID)만 같게."""
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return re.sub(r"tu_[0-9a-f]{8}", "tu_X", re.sub(r"WEB-[0-9A-F]{6}", "WEB-X", blob))


def test_record_only_does_not_change_requests_events_or_result(monkeypatch, tmp_path):
    results = {}
    for mode in ("off", "record"):
        sdk = h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
        monkeypatch.setenv("CTREC_WEB_METERING", mode)
        rid, run, events = _run(h.browser())
        result = next(e for e in events if e["type"] == "result")["result"]
        results[mode] = (sdk.requests, _strip(events),
                         _norm({k: result[k] for k in ("matches", "qa_log", "recommendation")}))
        assert {e["type"] for e in events} <= KNOWN_EVENTS  # 새 SSE 이벤트 없음
    req_off, req_rec = results["off"][0], results["record"][0]
    assert len(req_off) == len(req_rec)
    for a, b in zip(req_off, req_rec):  # SDK로 보낸 인자 (모델·대체 모델·max_tokens·프롬프트·메시지) 동일
        assert _norm(a) == _norm(b)
    assert any("fallbacks" in r for r in req_rec) == config.USE_FALLBACKS  # 웹 대체 모델 정책 그대로
    assert results["off"][1] == results["record"][1] and results["off"][2] == results["record"][2]


def test_cancel_behavior_unchanged_and_metering_recorded(monkeypatch, tmp_path):
    h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    c = h.browser()
    rid = c.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}).json()["run_id"]
    run = web.RUNS[rid]
    h.wait_for(run, "questions")
    assert c.post(f"/api/runs/{rid}/cancel").json() == {"ok": True, "state": "cancelling"}
    h.wait_for(run, "end", 30)
    events = h.sse_events(c.get(f"/api/runs/{rid}/events").text)
    assert events[-1]["type"] == "end" and events[-1]["state"] == "cancelled"
    assert not any(e["type"] == "result" for e in events)
    m = _metering(c, rid)
    assert m["in_progress"] is False and m["status"] == wm.FULLY_METERED and m["calls"] >= 1
    assert c.get(f"/api/runs/{rid}").json() == {"run_id": rid, "state": "cancelled", "done": True,
                                                "waiting_for_answers": False}  # 기존 상태 응답 모양 그대로


# ---------------------------------------------------------------- 10·11: USAGE_UNKNOWN, UNMETERED, PARTIALLY


def test_usage_missing_is_usage_unknown(monkeypatch, tmp_path):
    h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter(usage=False))
    c = h.browser()
    rid, _, _ = _run(c)
    m = _metering(c, rid)
    assert m["status"] == wm.USAGE_UNKNOWN and m["status_reason"] == "usage_missing"
    assert m["usage_missing"] == m["calls"] and m["usage_confirmed"] is False


def test_metering_off_is_unmetered(monkeypatch, tmp_path):
    h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    monkeypatch.setenv("CTREC_WEB_METERING", "off")
    c = h.browser()
    rid, _, _ = _run(c)
    m = _metering(c, rid)
    assert m["status"] == wm.UNMETERED and m["status_reason"] == "metering_off" and m["calls"] == 0


def test_calls_bypassing_recorder_are_unmetered(monkeypatch, tmp_path):
    """LLM 함수 자체를 바꿔 끼운 경우(기록기를 거치지 않음): 호출 0으로 '완전 계측'처럼 보이면 안 됨."""
    h.install(monkeypatch, tmp_path)  # 기존 웹 테스트 대역: llm.create/structured를 직접 교체
    c = h.browser()
    rid, _, _ = _run(c)
    m = _metering(c, rid)
    assert m["status"] == wm.UNMETERED and m["status_reason"] == "llm_used_but_no_recorded_calls"


def test_contextless_thread_call_makes_run_partially_metered(monkeypatch, tmp_path):
    fired = []

    def on_call(kw):  # 실행 도중 컨텍스트를 넘기지 않은 스레드에서 LLM 호출 (기록기 밖, 기본 클라이언트로 감)
        if not fired:
            fired.append(1)
            t = threading.Thread(target=lambda: llm.structured(
                MatcherOutput, system=matcher.SYSTEM, user="<rules_to_assess>[]</rules_to_assess>"))
            t.start()
            t.join()
    h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter(on_call=on_call))
    c = h.browser()
    rid, run, _ = _run(c)
    m = _metering(c, rid)
    assert m["status"] == wm.PARTIALLY_METERED and m["unattributed_calls_during_run"] >= 1
    assert any(e["type"] == "result" for e in run.events)  # 기록 전용: 막지 않음


def test_meter_unit_states_and_real_sdk_retries(monkeypatch):
    """단위: 기록기 밖 호출만 있으면 UNMETERED, 실제 SDK(가짜 HTTP)로 보내면 재시도 수가 보임."""
    monkeypatch.setattr(anthropic._base_client.SyncAPIClient, "_calculate_retry_timeout", lambda *a, **k: 0)
    sent = []

    def handler(request):
        sent.append(1)
        if len(sent) == 1:
            return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}})
        body = json.loads(request.content)
        return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": body["model"],
                                          "content": [{"type": "text", "text": "{\"assessments\": []}"}],
                                          "stop_reason": "end_turn", "stop_sequence": None,
                                          "usage": {"input_tokens": 9, "output_tokens": 3}})
    sdk = REAL_ANTHROPIC(api_key="sk-test", base_url="http://mock.invalid", max_retries=2,
                         http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(llm, "_client", sdk)
    call = lambda: llm.structured(MatcherOutput, system=matcher.SYSTEM,  # noqa: E731
                                  user="<rules_to_assess>[]</rules_to_assess>")
    with config.overrides(USE_FALLBACKS=False):
        meter = wm.WebRunMeter("unit-1")
        with meter.recording():
            call()  # 기록기를 거침: SDK 자체 재시도 1회 (기존 동작 그대로)
        s = meter.summary(llm_expected=True)
        assert s["status"] == wm.FULLY_METERED and s["retries"] == 1 and len(sent) == 2
        assert s["input_tokens"] == 9 and s["response_models"] == {config.MODEL: 1}

        meter2 = wm.WebRunMeter("unit-2")
        with meter2.recording():
            t = threading.Thread(target=call)  # 기록기 밖 (컨텍스트 없음) 호출만
            t.start()
            t.join()
        s2 = meter2.summary(llm_expected=False)
        assert s2["status"] == wm.UNMETERED and s2["status_reason"] == "calls_outside_recorder" and s2["calls"] == 0


# ---------------------------------------------------------------- 12·13: 누출 없음, 비용 상한 표시 없음


def test_metering_has_no_patient_text_or_key_and_never_claims_cost_cap(monkeypatch, tmp_path):
    h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    c = h.browser()
    rid, _, _ = _run(c)
    m = _metering(c, rid)
    blob = json.dumps(m, ensure_ascii=False)
    assert KEY not in blob and h.SYN_TEXT not in blob and h.SYN_ANSWER not in blob
    assert "비소세포폐암" not in blob and "ECOG" not in blob  # 환자 원문 조각
    assert m["cost_usd"] == wm.UNKNOWN and m["cost_estimable"] is False and m["cost_cap_enforced"] is False
    assert m["mode"] == "record_only" and "비용 상한을 보장하지 않습니다" in m["note"]
    # 다른 브라우저는 볼 수 없음 (기존 실행 소유권과 같음)
    assert h.browser(client=("198.51.100.99", 9)).get(f"/api/runs/{rid}/metering").status_code == 404


def test_paid_pilot_guard_blocks_web_in_same_process(monkeypatch, tmp_path):
    """유료 파일럿과 같은 프로세스의 웹 실행은 fail-closed로 막힘 (기록 전용이 파일럿 보호를 약하게 하지 않음)."""
    from ctrec import llm_pilot as lp
    from ctrec.llm_pilot_fake import FakeSDKClient
    sdk = h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    m = lp.PilotMeter(lp.PilotLimits(max_cost_usd=1, max_calls=1, max_input_tokens=1, max_output_tokens=1),
                      lp.ASSUMED_PRICE)
    with lp.metered(m, FakeSDKClient(), exclusive=True):
        c = h.browser()
        rid = c.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS,
                                        "screen": False}).json()["run_id"]
        h.wait_for(web.RUNS[rid], "end", 30)
    run = web.RUNS[rid]
    assert run.state != "completed" and sdk.requests == []  # 기본 클라이언트에 한 번도 닿지 않음
    assert call_guard.counters()["block_reasons"].get("unregistered_client", 0) >= 1


# ---------------------------------------------------------------- 15


def test_frozen_store_intact():
    assert store_patch.verify_frozen() == []
