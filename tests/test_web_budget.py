"""웹 LLM 비용 통제 (Phase 6-3): 실행별·공용 예산, 원장 원자성·재시작 복구, UNKNOWN 처리, 호출 경로 통합. 네트워크 없음.

실제 SDK 객체는 MockTransport(가짜 HTTP)로만 만들고, 그 밖에는 Fake SDK·웹 하네스 대역을 씁니다.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import anthropic
import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402

from ctrec import call_guard, config, llm, store_patch, web_metering  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.llm_pilot_fake import FakeSDKClient  # noqa: E402
from ctrec.schemas import MatcherOutput  # noqa: E402
from ctrec.web import app as web, budget  # noqa: E402

REAL_ANTHROPIC = anthropic.Anthropic
RULES = "<rules_to_assess>[]</rules_to_assess>"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: made.append(1) or pytest.fail("실제 API 클라이언트 생성"))
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    monkeypatch.setattr(anthropic._base_client.SyncAPIClient, "_calculate_retry_timeout", lambda *a, **k: 0)
    budget.reset_ledgers()
    before = call_guard.counters()
    yield
    budget.reset_ledgers()
    assert made == [] and call_guard.counters()["exclusive"] == before["exclusive"] == 0


PRICE = budget.Price(config.MODEL, 2.0, 10.0, 2.5, 0.2, "test")
BIG = budget.Limits(1000.0, 10_000, 10**9, 10**9)


def _cfg(tmp_path, run=BIG, shared=BIG, **kw):
    return budget.BudgetConfig(run=run, shared=shared, period="day", price=PRICE, ledger_path=str(tmp_path / "ledger.db"),
                               run_max_seconds=3600, **kw)


def _ledger(cfg, boot=None):
    return budget.Ledger(cfg.ledger_path, cfg.shared, cfg.period, boot_id=boot).open(create=True)


def _call():
    return llm.structured(MatcherOutput, system=matcher.SYSTEM, user=RULES)


def _run(cfg, inner, run_id="run-1", ledger=None, clock=None, fn=_call, n=1):
    led = ledger or _ledger(cfg)
    out = []
    kw = {"clock": clock} if clock else {}
    with budget.run_context(run_id, cfg, led, inner, **kw) as b:
        for _ in range(n):
            out.append(fn())
    return b, led, out


# ---------------------------------------------------------------- 설정 (enforce 필수 구성)


ENV = {"CTREC_WEB_RUN_MAX_USD": "10", "CTREC_WEB_RUN_MAX_CALLS": "50", "CTREC_WEB_RUN_MAX_INPUT_TOKENS": "5000000",
       "CTREC_WEB_RUN_MAX_OUTPUT_TOKENS": "2000000", "CTREC_WEB_SHARED_MAX_USD": "100", "CTREC_WEB_SHARED_MAX_CALLS": "500",
       "CTREC_WEB_SHARED_MAX_INPUT_TOKENS": "50000000", "CTREC_WEB_SHARED_MAX_OUTPUT_TOKENS": "20000000",
       "CTREC_WEB_PRICE_INPUT": "2", "CTREC_WEB_PRICE_OUTPUT": "10", "CTREC_WEB_PRICE_CACHE_WRITE": "2.5",
       "CTREC_WEB_PRICE_CACHE_READ": "0.2", "CTREC_WEB_PRICE_CONFIRMED": "1", "CTREC_WEB_PRICE_SOURCE": "test",
       "CTREC_WEB_PRICE_MODEL": config.MODEL, "CTREC_WEB_BUDGET_LEDGER_PERSISTENT": "1", "CTREC_WEB_BUDGET_SINGLE_INSTANCE": "1", "CTREC_WEB_BUDGET_PERIOD": "day"}


def test_config_complete_and_each_missing_item_refused(tmp_path):
    env = {**ENV, "CTREC_WEB_BUDGET_LEDGER": str(tmp_path / "l.db")}
    cfg = budget.load_config(env)
    assert cfg.run.max_calls == 50 and cfg.price.model == config.MODEL and cfg.period == "day"
    for k in env:
        with pytest.raises(budget.BudgetConfigError) as e:
            budget.load_config({x: v for x, v in env.items() if x != k} if k != "CTREC_WEB_BUDGET_PERIOD"
                               else {**env, k: "week"})
        assert e.value.problems
    with pytest.raises(budget.BudgetConfigError, match="not_positive"):
        budget.load_config({**env, "CTREC_WEB_RUN_MAX_USD": "0"})
    for bad in ("x", "-1", "9"):
        with pytest.raises(budget.BudgetConfigError, match="CTREC_WEB_BUDGET_MAX_RETRIES"):
            budget.load_config({**env, "CTREC_WEB_BUDGET_MAX_RETRIES": bad})
    assert budget.load_config({**env, "CTREC_WEB_BUDGET_MAX_RETRIES": "0"}).max_retries == 0


def test_fallbacks_on_refuses_enforce(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USE_FALLBACKS", True)
    with pytest.raises(budget.BudgetConfigError, match="fallbacks_enabled"):
        budget.load_config({**ENV, "CTREC_WEB_BUDGET_LEDGER": str(tmp_path / "l.db")})


def test_modes_and_pilot_gate_independent(monkeypatch):
    from ctrec import llm_pilot
    for raw, want in (("", "record"), ("record", "record"), ("off", "off"), ("enforce", "enforce"), ("typo", "enforce")):
        monkeypatch.setenv("CTREC_WEB_METERING", raw) if raw else monkeypatch.delenv("CTREC_WEB_METERING", raising=False)
        assert web_metering.mode() == want  # 알 수 없는 값은 enforce로 -> 설정 검사에서 거절 (조용히 기록 전용 아님)
    assert llm_pilot.PAID_EXECUTION_ENABLED is False


# ---------------------------------------------------------------- 실행별 예산: 예약·정산·차단


def test_reserve_and_settle_with_actual_usage(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeSDKClient()
    b, led, out = _run(cfg, fake)
    s = b.summary()
    assert out[0].assessments == [] and len(fake.calls) == 1
    assert s["calls_used"] == 1 and s["reserved_cost_usd"] == 0 and s["unknown_cost_usd"] == 0
    rows = led.rows()
    assert len(rows) == 1 and rows[0]["status"] == "settled"
    tok = s["token_usage"]
    assert rows[0]["actual_cost"] == pytest.approx(PRICE.cost(tok["input"], tok["output"], tok["cache_write"], tok["cache_read"]))
    assert s["confirmed_cost_usd"] == pytest.approx(rows[0]["actual_cost"], abs=1e-6)
    assert rows[0]["est_cost"] >= rows[0]["actual_cost"]  # 예약은 보수적 최악값
    assert tok["thinking"] > 0  # 공식 thinking_tokens 기록


@pytest.mark.parametrize("run, code", [
    (budget.Limits(0.01, 100, 10**9, 10**9), "run_cost_cap"),
    (budget.Limits(100, 100, 10, 10**9), "run_input_token_cap"),
    (budget.Limits(100, 100, 10**9, 100), "run_output_token_cap"),
])
def test_run_caps_block_before_request(tmp_path, run, code):
    fake = FakeSDKClient()
    with pytest.raises(budget.BudgetStop) as e:
        _run(_cfg(tmp_path, run=run), fake)
    assert e.value.args[0] == code and e.value.category == budget.EXCEEDED and fake.calls == []


def test_run_call_cap_and_time_cap(tmp_path):
    fake = FakeSDKClient()
    with pytest.raises(budget.BudgetStop, match="run_call_cap"):
        _run(_cfg(tmp_path, run=budget.Limits(100, 2, 10**9, 10**9)), fake, n=3)
    assert len(fake.calls) == 2
    t = [0.0]
    fake2 = FakeSDKClient(on_call=lambda i, kw: t.__setitem__(0, 10_000.0))
    sub = tmp_path / "time"
    sub.mkdir()
    cfg = replace(_cfg(sub), run_max_seconds=60)
    with pytest.raises(budget.BudgetStop, match="run_time_cap"):
        _run(cfg, fake2, clock=lambda: t[0], n=2)
    assert len(fake2.calls) == 1


def test_shared_cap_across_runs_and_period(tmp_path):
    cfg = _cfg(tmp_path, shared=budget.Limits(1000.0, 3, 10**9, 10**9))
    led = _ledger(cfg)
    _run(cfg, FakeSDKClient(), "run-a", led, n=1)
    fake = FakeSDKClient()
    with pytest.raises(budget.BudgetStop, match="shared_call_cap"):  # 실행별 한도(10,000회)와 별개로 공용 한도
        _run(cfg, fake, "run-b", led, n=5)
    assert len(fake.calls) == 2 and led.used()["calls"] == 3 and len({r["run_id"] for r in led.rows()}) == 2
    assert led.used("day:1999-01-01")["calls"] == 0  # 다른 기간은 따로
    cost_led = budget.Ledger(str(tmp_path / "cost.db"), budget.Limits(1.0, 100, 10**9, 10**9), "day").open(create=True)
    cost_led.reserve("r", PRICE.model, 0.6, 1, 1)
    with pytest.raises(budget.BudgetStop, match="shared_cost_cap"):
        cost_led.reserve("r", PRICE.model, 0.6, 1, 1)


def test_concurrent_reservations_are_atomic(tmp_path):
    cost = 0.5
    cfg = _cfg(tmp_path, shared=budget.Limits(cost * 7, 10_000, 10**9, 10**9))
    leds = [_ledger(cfg, boot="same-boot") for _ in range(4)]  # 연결이 다른 원장 객체 4개 (같은 파일)
    ok, blocked = [], []

    def work(i):
        try:
            ok.append(leds[i % 4].reserve(f"run-{i % 3}", PRICE.model, cost, 10, 10))
        except budget.BudgetStop:
            blocked.append(i)
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(work, range(40)))
    assert len(ok) == 7 and len(blocked) == 33 and len(set(ok)) == 7  # 정확히 한도만큼, 예약 ID 중복 없음
    assert leds[0].used()["cost_usd"] == pytest.approx(cost * 7)


def test_duplicate_settlement_prevented(tmp_path):
    cfg = _cfg(tmp_path)
    led = _ledger(cfg)
    rid = led.reserve("run-1", PRICE.model, 1.0, 10, 10)
    assert led.settle(rid, 0.2, 5, 5, 0, 0) is True
    assert led.settle(rid, 0.2, 5, 5, 0, 0) is False and led.mark_unknown(rid, "x") is False
    assert led.release(rid, "x") is False and led.used()["cost_usd"] == pytest.approx(0.2)
    b = budget.RunBudget("run-2", cfg, led)
    res = b.reserve(10, 10)
    b.settle(res, h.SDKAdapter()._usage())
    b.settle(res, h.SDKAdapter()._usage())  # 같은 예약을 두 번 정산 -> 원장이 거부 -> 이후 호출 차단
    assert b.stopped == "ledger_inconsistent"
    with pytest.raises(budget.BudgetStop):
        b.reserve(10, 10)


# ---------------------------------------------------------------- 불확실한 결과 -> UNKNOWN (점유 유지)


def test_usage_missing_is_unknown_and_stops(tmp_path):
    fake = FakeSDKClient(faults=lambda i, s: "no_usage" if i == 0 else None)
    cfg = _cfg(tmp_path)
    led = _ledger(cfg)
    with budget.run_context("run-1", cfg, led, fake) as b:
        _call()  # 응답은 쓰되 사용량 미확인
        with pytest.raises(budget.BudgetStop, match="usage_missing"):
            _call()
    rows = led.rows()
    assert [r["status"] for r in rows] == ["unknown"] and rows[0]["reason"] == "usage_missing"
    s = b.summary()
    assert s["unknown_cost_usd"] == pytest.approx(rows[0]["est_cost"], abs=1e-5) and s["confirmed_cost_usd"] == 0
    assert s["budget_status"] == budget.USAGE_UNKNOWN and led.used()["cost_usd"] == pytest.approx(rows[0]["est_cost"])
    assert len(fake.calls) == 1


def test_timeout_unknown_retry_reserved_separately(tmp_path):
    fake = FakeSDKClient(faults=lambda i, s: "timeout" if i == 0 else None)
    cfg = _cfg(tmp_path)
    b, led, _ = _run(cfg, fake)
    assert [r["status"] for r in led.rows()] == ["unknown", "settled"] and led.rows()[0]["reason"] == "response_lost"
    assert b.summary()["calls_used"] == 2 and b.summary()["unknown_requests"] == 1
    assert all(c["timeout"] == cfg.call_timeout_s for c in fake.calls)


def test_rate_limit_released_not_held(tmp_path):
    fake = FakeSDKClient(faults=lambda i, s: "429" if i == 0 else None)
    b, led, _ = _run(_cfg(tmp_path), fake)
    assert [r["status"] for r in led.rows()] == ["released", "settled"] and b.summary()["calls_used"] == 1


def test_cancel_during_call_keeps_ledger_consistent(tmp_path):
    flag = threading.Event()
    fake = FakeSDKClient(on_call=lambda i, kw: flag.set())
    cfg = _cfg(tmp_path)
    led = _ledger(cfg)
    with budget.run_context("run-1", cfg, led, fake):
        llm.set_guard(lambda: "cancelled" if flag.is_set() else None)
        try:
            _call()  # 보낸 요청은 끝까지 진행 -> 정산
            with pytest.raises(llm.RunCancelled):
                _call()  # 다음 요청 전에 취소 확인
        finally:
            llm.set_guard(None)
    assert [r["status"] for r in led.rows()] == ["settled"] and len(fake.calls) == 1


def test_interrupted_request_unknown_and_restart_recovery(tmp_path):
    cfg = _cfg(tmp_path)
    led = _ledger(cfg, boot="boot-1")
    fake = FakeSDKClient(faults=lambda i, s: "interrupt")
    with pytest.raises(KeyboardInterrupt), budget.run_context("run-1", cfg, led, fake):
        _call()
    assert led.rows()[0]["status"] == "unknown" and led.rows()[0]["reason"] == "interrupted"
    # 프로세스가 요청 도중 죽은 경우: 'reserved'로 남은 예약 -> 다음 기동 때 UNKNOWN
    rid = led.reserve("run-2", PRICE.model, 0.7, 10, 10)
    led2 = _ledger(cfg, boot="boot-2")
    row = next(r for r in led2.rows() if r["id"] == rid)
    assert led2.recovered == 1 and row["status"] == "unknown" and row["reason"] == "orphaned_on_restart"
    assert led2.used()["cost_usd"] >= 0.7  # 0으로 보지 않음
    assert led2.resolve_unknown(rid, cost=0.1, inp=5, out=5) is True  # 운영자 정산
    assert led2.resolve_unknown(rid, cost=0.1) is False  # 중복 정산 불가


def test_corrupt_or_inconsistent_ledger_blocks(tmp_path):
    p = tmp_path / "bad.db"
    p.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(budget.LedgerError):
        budget.Ledger(str(p), BIG, "day").open()
    cfg = _cfg(tmp_path)
    led = _ledger(cfg)
    with sqlite3.connect(cfg.ledger_path) as c:
        c.execute("INSERT INTO reservations (id, run_id, window, model, boot_id, status, est_cost, est_input, est_output, "
                  "created_at, updated_at) VALUES ('x','r','day:x','m','b','settled',1,1,1,0,0)")
    with pytest.raises(budget.LedgerError, match="ledger_inconsistent"):
        _ledger(cfg)
    with sqlite3.connect(cfg.ledger_path) as c:
        c.execute("UPDATE meta SET value = '99' WHERE key = 'schema'")
    with pytest.raises(budget.LedgerError, match="ledger_inconsistent"):
        led.open()


# ---------------------------------------------------------------- 응답 이상·캐시·SDK 재시도


@pytest.mark.parametrize("fault, code", [("other_model", "model_mismatch"), ("fallback_iteration", "fallback_detected"),
                                         ("priority_tier", "service_tier_mismatch")])
def test_unexpected_response_unknown_and_not_used(tmp_path, fault, code):
    fake = FakeSDKClient(faults=lambda i, s: fault)
    with pytest.raises(budget.BudgetStop, match=code) as e:
        _run(_cfg(tmp_path), fake)
    assert e.value.category == budget.USAGE_UNKNOWN


def test_cache_usage_priced(tmp_path):
    b, led, _ = _run(_cfg(tmp_path), FakeSDKClient(cache_read_ratio=1.0))
    tok = b.summary()["token_usage"]
    assert tok["cache_read"] > 0
    assert led.rows()[0]["actual_cost"] == pytest.approx(PRICE.cost(tok["input"], tok["output"], 0, tok["cache_read"]))


def _mock_sdk(max_retries, fail_first=0):
    sent = []

    def handler(request):
        sent.append(1)
        if len(sent) <= fail_first:
            return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}})
        body = json.loads(request.content)
        return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": body["model"],
                                          "content": [{"type": "text", "text": "{\"assessments\": []}"}],
                                          "stop_reason": "end_turn", "stop_sequence": None,
                                          "usage": {"input_tokens": 9, "output_tokens": 3,
                                                    "service_tier": "standard"}})
    c = REAL_ANTHROPIC(api_key="sk-test", base_url="http://mock.invalid", max_retries=max_retries,
                       http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    c.sent = sent
    return c


def test_sdk_internal_retries_disabled_and_counted(tmp_path, monkeypatch):
    sdk = _mock_sdk(max_retries=2, fail_first=1)
    monkeypatch.setattr(llm, "_client", sdk)
    inner = budget.make_inner()  # 기본 클라이언트의 재시도 0 사본
    assert inner is not sdk and inner.max_retries == 0
    b, led, out = _run(_cfg(tmp_path), inner)
    assert out[0].assessments == [] and len(sdk.sent) == 2  # SDK가 몰래 재시도하지 않고 래퍼가 예약 후 재시도
    assert [r["status"] for r in led.rows()] == ["unknown", "settled"]  # 529(응답 없음) -> 점유, 재시도 -> 정산
    with pytest.raises(budget.BudgetStop, match="sdk_retries_enabled"):
        budget.BudgetClient(sdk, b)


# ---------------------------------------------------------------- 우회 차단 (fail-closed)


def test_contextless_thread_and_bypass_attempts_blocked(tmp_path, monkeypatch):
    default = FakeSDKClient()
    monkeypatch.setattr(llm, "_client", default)  # 일반 경로의 기본 클라이언트 (닿으면 우회 성공)
    cfg = _cfg(tmp_path)
    led = _ledger(cfg)
    rogue = FakeSDKClient()
    raw = _mock_sdk(max_retries=0)
    results = {}
    with budget.run_context("run-1", cfg, led, FakeSDKClient()):
        def bare():
            try:
                _call()
                results["thread"] = "called"
            except call_guard.UnmeteredCallBlocked:
                results["thread"] = "blocked"
        t = threading.Thread(target=bare)
        t.start()
        t.join()
        with ThreadPoolExecutor(1) as pool:
            results["pool"] = pool.submit(lambda: _bypass(_call)).result()
        results["executor"] = asyncio.run(_in_executor())
        results["default"] = _bypass(llm.client)
        with llm.use_client(rogue):
            results["rogue"] = _bypass(_call)
        results["raw_sdk"] = _bypass(lambda: raw.messages.create(model=config.MODEL, max_tokens=5,
                                                                  messages=[{"role": "user", "content": "x"}]))
        with config.overrides(USE_FALLBACKS=True):
            with pytest.raises(budget.BudgetStop, match="fallback_requested"):
                _call()
    assert results == {"thread": "blocked", "pool": "UnmeteredCallBlocked", "executor": "UnmeteredCallBlocked",
                       "default": "UnmeteredCallBlocked", "rogue": "UnmeteredCallBlocked", "raw_sdk": "UnmeteredCallBlocked"}
    assert default.calls == [] and rogue.calls == [] and raw.sent == []


def _bypass(fn):
    try:
        fn()
        return "called"
    except call_guard.UnmeteredCallBlocked as e:
        return type(e).__name__


async def _in_executor():
    return await asyncio.get_running_loop().run_in_executor(None, lambda: _bypass(_call))


def test_matcher_parallel_threads_metered(tmp_path):
    cfg = _cfg(tmp_path)
    led = _ledger(cfg)
    fake = FakeSDKClient()
    import contextvars
    with budget.run_context("run-1", cfg, led, fake) as b, ThreadPoolExecutor(4) as pool:
        futs = [pool.submit(contextvars.copy_context().run, _call) for _ in range(4)]  # Session._parallel과 같은 방식
        [f.result() for f in futs]
    assert b.summary()["calls_used"] == 4 and len(fake.calls) == 4 and len(led.rows()) == 4


# ---------------------------------------------------------------- 웹 통합 (enforce 모드)


@pytest.fixture()
def enforce(monkeypatch, tmp_path):
    def setup(**over):
        env = {**ENV, "CTREC_WEB_METERING": "enforce", "CTREC_WEB_BUDGET_LEDGER": str(tmp_path / "web-ledger.db"), **over}
        budget.Ledger(str(tmp_path / "web-ledger.db"), BIG, "day").open(create=True)  # 운영자 init에 해당
        sdk = h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        return sdk, env
    return setup


def _go(c, answer=True):
    r = c.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False})
    if r.status_code != 200:
        return r, None
    run = web.RUNS[r.json()["run_id"]]
    try:  # 질문 없이 끝나는 실행(예산 차단 등)도 있음
        q = h.wait_for(run, "questions", 10)
        if answer:
            c.post(f"/api/runs/{run.run_id}/answers", json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}})
    except AssertionError:
        pass
    h.wait_for(run, "end", 30)
    return r, run


def test_record_mode_unchanged(monkeypatch, tmp_path):
    h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    monkeypatch.delenv("CTREC_WEB_METERING", raising=False)
    c = h.browser()
    _, run = _go(c)
    m = c.get(f"/api/runs/{run.run_id}/metering").json()
    assert run.state == "completed" and m["mode"] == "record_only" and m["budget_mode"] == "record"
    assert m["cap_enforced"] is False and m["cost_cap_enforced"] is False and m["status"] == web_metering.FULLY_METERED


@pytest.mark.parametrize("missing", ["CTREC_WEB_BUDGET_LEDGER", "CTREC_WEB_BUDGET_LEDGER_PERSISTENT",
                                     "CTREC_WEB_BUDGET_SINGLE_INSTANCE", "CTREC_WEB_PRICE_CONFIRMED", "CTREC_WEB_RUN_MAX_USD",
                                     "CTREC_WEB_SHARED_MAX_CALLS"])
def test_enforce_misconfigured_refuses_to_start(enforce, missing):
    sdk, _ = enforce(**{missing: None})
    web.RUNS.clear()
    r = h.browser().post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False})
    assert r.status_code == 503 and "비용 관리 설정" in r.json()["detail"] and missing not in r.text
    assert web.RUNS == {} and sdk.requests == []


def test_enforce_full_run_metered_and_owner_only(enforce, tmp_path):
    sdk, env = enforce()
    c = h.browser()
    _, run = _go(c)
    assert run.state == "completed"
    m = c.get(f"/api/runs/{run.run_id}/metering").json()
    assert m["budget_mode"] == "enforce" and m["cap_enforced"] is True and m["cost_cap_enforced"] is True
    assert m["budget_status"] == "ok" and m["calls_used"] == len(sdk.requests) and m["unknown_cost_usd"] == 0
    assert m["confirmed_cost_usd"] > 0 and m["calls_remaining"] == 50 - m["calls_used"]
    assert not any(k.startswith("shared") or k == "limits" for k in m)  # 공용 예산·설정값은 보이지 않음
    assert h.browser(client=("198.51.100.99", 9)).get(f"/api/runs/{run.run_id}/metering").status_code == 404
    # 원장에는 최소 메타데이터만 (환자 원문·답변·키 없음)
    with sqlite3.connect(env["CTREC_WEB_BUDGET_LEDGER"]) as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(reservations)")]
        dump = json.dumps(con.execute("SELECT * FROM reservations").fetchall() + con.execute("SELECT * FROM meta").fetchall(),
                          ensure_ascii=False)
    assert set(cols) == {"id", "run_id", "window", "model", "boot_id", "status", "est_cost", "est_input", "est_output",
                         "actual_cost", "actual_input", "actual_output", "cache_write", "cache_read", "reason",
                         "created_at", "updated_at"}
    assert h.SYN_TEXT not in dump and h.SYN_ANSWER not in dump and "sk-" not in dump


def test_enforce_budget_exceeded_ends_without_result(enforce):
    sdk, _ = enforce(CTREC_WEB_RUN_MAX_CALLS="2")
    c = h.browser()
    _, run = _go(c)
    events = h.sse_events(c.get(f"/api/runs/{run.run_id}/events").text)
    assert run.state == budget.EXCEEDED and (events[-1]["type"], events[-1]["state"]) == ("end", budget.EXCEEDED)
    assert not any(e["type"] == "result" for e in events)  # 중단된 분석을 완료·추천으로 내보내지 않음
    assert any(e["type"] == "status" and "비용 한도" in e.get("message", "") for e in events)
    m = c.get(f"/api/runs/{run.run_id}/metering").json()
    assert m["budget_status"] == budget.EXCEEDED and m["last_block_reason"] == "run_call_cap" and len(sdk.requests) == 2


def test_enforce_ledger_failure_at_start_and_shared_exhaustion(enforce, tmp_path):
    _, env = enforce(CTREC_WEB_SHARED_MAX_CALLS="3")
    c = h.browser()
    _, run = _go(c)
    assert run.state == budget.EXCEEDED  # 공용 한도로 멈춤
    m = c.get(f"/api/runs/{run.run_id}/metering").json()
    assert m["last_block_reason"] == "shared_call_cap"
    budget.reset_ledgers()
    Path(env["CTREC_WEB_BUDGET_LEDGER"]).write_bytes(b"corrupt" * 1000)  # 원장 손상 -> 시작 거절
    assert h.browser(client=("198.51.100.55", 5)).post("/api/runs", json={
        "custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}).status_code == 503


def test_frozen_store_intact():
    assert store_patch.verify_frozen() == []


def test_operator_cli_lists_and_resolves_unknown(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import web_budget_ledger as cli
    cfg = _cfg(tmp_path)
    led = _ledger(cfg, boot="b1")
    rid = led.reserve("run-1", PRICE.model, 0.5, 10, 10)
    led.mark_unknown(rid, "response_lost")
    monkeypatch.setenv("CTREC_WEB_BUDGET_LEDGER", cfg.ledger_path)
    assert cli.main(["unknown"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in listed] == [rid] and set(listed[0]) >= {"run_id", "est_cost", "reason"}
    assert cli.main(["resolve", rid, "--not-billed"]) == 0 and json.loads(capsys.readouterr().out) == {"resolved": True}
    assert cli.main(["resolve", rid, "--cost", "0.1"]) == 1  # 이미 정산됨 -> 중복 정산 불가
    capsys.readouterr()
    assert cli.main(["summary"]) == 0 and json.loads(capsys.readouterr().out)["rows_by_status"] == {"released": 1}


# ---------------------------------------------------------------- 커밋 전 안전성 검토 (입력 예약·옵션·원장 잠금)


class _Usage:
    """사용량을 마음대로 정하는 SDK 대역 (Fake SDK 응답을 받아 usage만 바꿈)."""

    def __init__(self, **usage):
        self.fake, self.usage, self.requests = FakeSDKClient(), usage, []
        self.max_retries = 0
        self.messages = type("M", (), {"create": staticmethod(self._create)})()

    def _create(self, **kw):
        self.requests.append(kw)
        r = self.fake.messages.create(**kw)
        for k, v in self.usage.items():
            setattr(r.usage, k, v)
        return r


@pytest.mark.parametrize("text", ["한국어 임상시험 선정 기준 " * 300, "English inclusion criteria " * 300,
                                  "≥≤±µ①→※🙂\\\"\n\t" * 300, json.dumps({"a": [{"b": "c"}] * 500})])
def test_input_reservation_counts_utf8_bytes_of_whole_request(text):
    kw = {"model": PRICE.model, "max_tokens": 10, "system": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}],
          "messages": [{"role": "user", "content": text}], "tools": [{"name": "t", "input_schema": {"type": "object"}}],
          "output_config": {"effort": "high"}}
    est = budget.request_tokens_bound(kw)
    assert est >= 2 * len(text.encode("utf-8")) + budget.REQUEST_OVERHEAD_TOKENS  # 시스템+메시지 모두 바이트 수로
    assert budget.request_tokens_bound({**kw, "tool_choice": {"type": "any"}}) > est  # 모든 요청 내용 포함


def test_actual_input_above_reservation_settles_and_stops(tmp_path):
    inner = _Usage(input_tokens=10**7)
    with pytest.raises(budget.BudgetStop, match="estimate_exceeded") as e:
        _run(_cfg(tmp_path), inner, n=2)
    assert e.value.category == budget.BUDGET_ERROR and len(inner.requests) == 1  # 다음 호출은 보내지 않음
    row = _ledger(_cfg(tmp_path)).rows()[0]
    assert row["status"] == "settled" and row["actual_input"] == 10**7  # 실제값으로 공용 원장에 반영


@pytest.mark.parametrize("usage, code", [({"service_tier": None}, "service_tier_mismatch"),
                                         ({"cache_creation": type("C", (), {"ephemeral_1h_input_tokens": 5})()},
                                          "cache_ttl_mismatch")])
def test_unconfirmed_tier_or_cache_ttl_is_unknown(tmp_path, usage, code):
    with pytest.raises(budget.BudgetStop, match=code):
        _run(_cfg(tmp_path), _Usage(**usage))
    assert [r["status"] for r in _ledger(_cfg(tmp_path)).rows()] == ["unknown"]


@pytest.mark.parametrize("extra, code", [({"service_tier": "priority"}, "request_option_unsupported"),
                                         ({"speed": "fast"}, "request_option_unsupported"),
                                         ({"stream": True}, "request_option_unsupported"),
                                         ({"extra_body": {"x": 1}}, "request_option_unsupported"),
                                         ({"betas": ["x"]}, "fallback_requested"),
                                         ({"tools": [{"type": "web_search_20260209", "name": "web_search"}]},
                                          "server_tool_requested")])
def test_unsupported_request_options_blocked_before_send(tmp_path, extra, code):
    inner = _Usage()

    def call():
        return llm.current_client().messages.create(model=PRICE.model, max_tokens=10, system="s",
                                                    messages=[{"role": "user", "content": "x"}], **extra)
    with pytest.raises(budget.BudgetStop, match=code):
        _run(_cfg(tmp_path), inner, fn=call)
    assert inner.requests == [] and _ledger(_cfg(tmp_path)).rows() == []


def test_ledger_lock_timeout_blocks_without_sending(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    led = _ledger(cfg)
    holder = sqlite3.connect(cfg.ledger_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # 다른 연결이 쓰기 잠금을 쥔 상태
    orig = budget.Ledger._conn

    def short(self):
        c = orig(self)
        c.execute("PRAGMA busy_timeout = 50")
        return c
    monkeypatch.setattr(budget.Ledger, "_conn", short)
    inner = FakeSDKClient()
    try:
        with pytest.raises(budget.BudgetStop, match="ledger_unavailable"):
            _run(cfg, inner, ledger=led)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert inner.calls == []
    assert led.rows() == []


def test_second_process_open_turns_inflight_into_unknown(tmp_path):
    """같은 원장을 두 번째 프로세스가 열면 첫 프로세스의 진행 중 예약은 UNKNOWN이 됨 (보수적으로 점유 유지).
    첫 프로세스의 정산은 실패해 그 실행이 멈춤 -> 여러 프로세스(워커) 운영은 지원하지 않음."""
    cfg = _cfg(tmp_path)
    a = _ledger(cfg, boot="proc-a")
    rid = a.reserve("run-1", PRICE.model, 0.5, 100, 10)
    b = _ledger(cfg, boot="proc-b")
    assert b.recovered == 1 and b.rows("unknown")[0]["id"] == rid
    assert a.settle(rid, 0.01, 10, 1, 0, 0) is False  # 중복 정산 불가 -> RunBudget은 ledger_inconsistent로 멈춤
    assert b.used()["cost_usd"] == pytest.approx(0.5)
