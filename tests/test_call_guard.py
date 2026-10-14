"""파일럿 fail-closed (Phase 5-3): 계측 컨텍스트가 없거나 우회하는 LLM 호출은 어느 스레드에서든 차단. 네트워크 없음.

실제 anthropic SDK 객체는 MockTransport(가짜 HTTP)로만 만들어 SDK 내부 요청 훅까지 확인합니다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import anthropic
import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prepare_llm_pilot as plp  # noqa: E402
from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import call_guard, config, llm, llm_pilot as lp, llm_pilot_exec as lpx  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.llm_pilot_fake import FakeSDKClient  # noqa: E402
from ctrec.schemas import MatcherOutput  # noqa: E402

REAL_ANTHROPIC, REAL_ASYNC = anthropic.Anthropic, anthropic.AsyncAnthropic
PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}
PAIRS = {gt.case_id: (c, gt) for c, gt in plp.load_pairs(PARSED, "representative_egfr")[1]}
RESCUE, SHARED, BIOMARKER, COHORT, CONFIRMED, EGFR = lp.PILOT_CASE_POOL
KEY = "sk-ant-test-guard-key-0000"
MSG = {"id": "msg_1", "type": "message", "role": "assistant", "model": None, "content": [{"type": "text", "text": "{\"assessments\": []}"}],
       "stop_reason": "end_turn", "stop_sequence": None,
       "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """실제 클라이언트 생성 차단 (MockTransport로 만든 SDK 객체만 사용), 끝나면 보호 장치가 모두 풀렸는지 확인."""
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: made.append(1) or pytest.fail("API 클라이언트 생성"))
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setattr(anthropic._base_client.SyncAPIClient, "_calculate_retry_timeout", lambda *a, **k: 0)
    before = call_guard.counters()  # 다른 테스트의 웹 실행이 관측 모드로 남아 있을 수 있음 (배타는 없어야 함)
    assert before["exclusive"] == 0
    yield
    c = call_guard.counters()
    assert made == [] and c["exclusive"] == 0 and c["observers"] == before["observers"]
    if before["observers"] == 0:
        assert not c["sdk_hooks_installed"] and llm._gate is None
    assert llm._client_var.get() is None


def mock_sdk(max_retries=0, fail_first=0, async_=False):
    """MockTransport로 응답하는 실제 SDK 객체 (네트워크 없음). 보낸 HTTP 요청 수는 .sent."""
    sent = []

    def handler(request):
        sent.append(request.url.path)
        if len(sent) <= fail_first:
            return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}})
        body = json.loads(request.content)
        return httpx2.Response(200, json={**MSG, "model": body["model"]})
    if async_:
        c = REAL_ASYNC(api_key="sk-test", base_url="http://mock.invalid", max_retries=max_retries,
                       http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
    else:
        c = REAL_ANTHROPIC(api_key="sk-test", base_url="http://mock.invalid", max_retries=max_retries,
                           http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    c.sent = sent
    return c


def _limits(**kw):
    base = dict(max_cost_usd=100.0, max_calls=1000, max_input_tokens=10 ** 9, max_output_tokens=10 ** 9,
                max_retries_per_call=2, max_total_retries=50, max_consecutive_failures=50)
    base.update(kw)
    return lp.PilotLimits(**base)


PRICE = lp.PriceTable(model=config.MODEL, input=3, output=15, cache_write=3.75, cache_read=0.3)


def _execute(exp="A_pipeline", ids=(COHORT,), fake=None, limits=None, **kw):
    fake = fake or FakeSDKClient()
    plan = {"experiment": exp, "case_ids": list(ids), "model": config.MODEL, "price": asdict(PRICE),
            "rounds": lp.EXPERIMENTS[exp]["rounds"], "question_budget": 3, "limits": asdict(_limits()),
            "save_patient_data": False, "redact_outputs": True}
    r = lpx.execute(plan, PRICE, limits or _limits(), fake, [PAIRS[i] for i in ids], TRIALS, sleep=lambda s: None, **kw)
    return r, fake


def _structured():
    return llm.structured(MatcherOutput, system=matcher.SYSTEM, user="<rules_to_assess>[]</rules_to_assess>")


def _bypass_during_run(attempt):
    """파일럿 첫 요청 도중 attempt()로 우회 호출을 시도하고 결과(예외 종류)를 기록."""
    seen = {}

    def on_call(i, kw):
        if i == 0:
            try:
                attempt()
                seen["result"] = "called"
            except BaseException as e:  # noqa: BLE001
                seen["result"] = type(e).__name__
                seen["args"] = getattr(e, "args", ())
    return seen, FakeSDKClient(on_call=on_call)


# ---------------------------------------------------------------- 1·2·3: 컨텍스트 누락·새 스레드·run_in_executor


def _bare_thread():
    out = {}

    def target():
        try:
            _structured()
            out["r"] = "called"
        except BaseException as e:  # noqa: BLE001
            out["r"] = type(e).__name__
    t = threading.Thread(target=target)  # 컨텍스트를 넘기지 않음
    t.start()
    t.join()
    if out["r"] != "called":
        raise call_guard.UnmeteredCallBlocked("unmetered_call_blocked", "thread:" + out["r"])


def _bare_pool():
    with ThreadPoolExecutor(2) as pool:
        pool.submit(_structured).result()


def _run_in_executor():
    async def main():
        return await asyncio.get_running_loop().run_in_executor(None, _structured)
    asyncio.run(main())


@pytest.mark.parametrize("attempt", [_bare_thread, _bare_pool, _run_in_executor, lambda: llm.client()],
                         ids=["new_thread", "thread_pool", "run_in_executor", "default_client"])
def test_bypass_without_context_is_blocked_and_run_aborts(attempt):
    default = FakeSDKClient()
    llm._client = default  # 기본 클라이언트 자리에 대역 (도달하면 우회 성공)
    seen, fake = _bypass_during_run(attempt)
    blocked0 = call_guard.counters()["blocked"]
    r, _ = _execute("A_pipeline", (COHORT, BIOMARKER), fake)
    assert seen["result"] == "UnmeteredCallBlocked" and default.calls == []
    assert call_guard.counters()["blocked"] > blocked0
    assert r["stop_reason"] == "unmetered_call_blocked" and r["status"] == "aborted"
    assert r["cases"][1]["status"] == "skipped"  # 믿을 수 없는 실행은 다음 사례로 넘어가지 않음


def test_unregistered_context_client_is_blocked():
    rogue = FakeSDKClient()

    def attempt():
        with llm.use_client(rogue):  # 계측기를 거치지 않는 다른 클라이언트를 컨텍스트에 직접 건 경우
            _structured()
    seen, fake = _bypass_during_run(attempt)
    _execute("A_pipeline", (COHORT,), fake)
    assert seen["result"] == "UnmeteredCallBlocked" and seen["args"][1] == "unregistered_client" and rogue.calls == []


# ---------------------------------------------------------------- 4·5: 정상 경로 (Matcher 병렬·async)


def test_matcher_parallel_threads_metered_under_guard(monkeypatch):
    monkeypatch.setattr(config, "MAX_WORKERS", 4)
    r, fake = _execute("A_pipeline", (SHARED,))
    assert r["status"] == "succeeded" and r["process_guard"] is True
    assert r["totals"]["by_stage"]["matcher"]["calls"] >= 2 and r["totals"]["calls"] == len(fake.calls)


def test_async_tasks_with_run_context_are_metered():
    out = {}

    def attempt():
        async def main():  # asyncio.run은 현재(실행) 컨텍스트를 task에 복사 -> 계측 클라이언트로 정상 호출
            return await asyncio.gather(*(asyncio.to_thread(_structured) for _ in range(2)), asyncio.sleep(0))
        asyncio.run(main())
        out["ok"] = True
    seen, fake = _bypass_during_run(attempt)
    r, _ = _execute("A_pipeline", (COHORT,), fake)
    assert seen["result"] == "called" and out["ok"]
    assert r["status"] == "succeeded" and r["totals"]["calls"] == len(fake.calls)
    assert r["totals"]["by_stage"]["matcher"]["calls"] >= 2  # 사례의 Matcher + async에서 부른 2회


def test_async_pilots_isolated_under_guard():
    async def main():
        return await asyncio.gather(asyncio.to_thread(_execute, "A_pipeline", (COHORT,)),
                                    asyncio.to_thread(_execute, "B_question_plan", (BIOMARKER,)))
    (ra, fa), (rb, fb) = asyncio.run(main())
    assert ra["status"] == rb["status"] == "succeeded"
    assert ra["totals"]["calls"] == len(fa.calls) and rb["totals"]["calls"] == len(fb.calls)


# ---------------------------------------------------------------- 6·7: 중첩 예산, 종료 후 복원


def test_nested_guarded_pilots_keep_separate_budgets():
    inner = {}

    def on_call(i, kw):
        if i == 0:
            inner["r"], inner["fake"] = _execute("A_pipeline", (COHORT,), limits=_limits(max_cost_usd=0.01))
    outer_fake = FakeSDKClient(on_call=on_call)
    r, _ = _execute("A_pipeline", (BIOMARKER,), outer_fake)
    assert inner["r"]["stop_reason"] == "cost_cap" and inner["fake"].calls == []
    assert r["status"] == "succeeded" and r["totals"]["calls"] == len(outer_fake.calls)


def test_state_restored_after_run_and_after_exception(monkeypatch):
    _execute("A_pipeline", (COHORT,))
    default = FakeSDKClient()
    llm._client = default
    _structured()  # 파일럿이 끝나면 기본 클라이언트로 정상 호출 (일반 CLI·웹 동작)
    c = call_guard.counters()
    assert len(default.calls) == 1 and c["exclusive"] == 0
    assert c["observers"] or not c["sdk_hooks_installed"]  # 관측 중인 다른 실행이 없으면 훅도 걷힘

    def boom(*a, **k):
        raise RuntimeError("실험 코드 예외")
    monkeypatch.setattr(lp, "run_experiment_a", boom)
    with pytest.raises(RuntimeError):
        _execute("A_pipeline", (COHORT,))
    c = call_guard.counters()
    assert c["exclusive"] == 0 and (c["observers"] or llm._gate is None)
    _structured()
    assert len(default.calls) == 2


# ---------------------------------------------------------------- SDK 수준: 직접 사용·재시도·미들웨어·async


def _guarded(fn, inner=None):
    """배타 실행 하나를 열고 그 안에서 fn 실행 (계측 대리 객체는 inner를 감쌈)."""
    m = lp.PilotMeter(_limits(), PRICE)
    with lp.metered(m, inner or FakeSDKClient(), exclusive=True):
        return fn(), m


def test_real_sdk_through_metered_client_passes_once():
    sdk = mock_sdk()
    with config.overrides(USE_FALLBACKS=False):  # 파일럿 실행 설정과 같게 (execute는 자동으로 끔)
        out, m = _guarded(_structured, sdk)
    assert out.assessments == [] and sdk.sent == ["/v1/messages"] and m.report()["calls"] == 1
    assert m.report()["input_tokens"] == 10  # 실제 SDK 응답의 usage로 정산


def test_raw_sdk_use_in_run_context_is_blocked():
    sdk = mock_sdk()
    with pytest.raises(call_guard.UnmeteredCallBlocked) as e:
        _guarded(lambda: sdk.messages.create(model=config.MODEL, max_tokens=10,
                                             messages=[{"role": "user", "content": "x"}]))
    assert e.value.args[1] == "sdk_without_metered_mark" and sdk.sent == []


def test_sdk_auto_retry_and_middleware_blocked():
    retrying = mock_sdk(max_retries=2)
    with pytest.raises(lp.PilotStop, match="sdk_retries_enabled"):  # 계측기가 재시도 켜진 클라이언트를 거부
        _guarded(_structured, retrying)

    def sneak(sdk):  # 계측 표시는 있지만 SDK 자체 재시도가 켜진 요청
        with call_guard.mark("metered", sdk):
            return sdk.messages.create(model=config.MODEL, max_tokens=10, messages=[{"role": "user", "content": "x"}])
    with pytest.raises(call_guard.UnmeteredCallBlocked) as e:
        _guarded(lambda: sneak(retrying))
    assert e.value.args[1] == "sdk_auto_retry" and retrying.sent == []
    mw = mock_sdk()
    mw._middleware_chain = lambda req: pytest.fail("미들웨어 실행")  # 클라이언트 측 대체 모델 미들웨어 흉내
    with pytest.raises(call_guard.UnmeteredCallBlocked) as e:
        _guarded(lambda: sneak(mw))
    assert e.value.args[1] == "sdk_middleware" and mw.sent == []


def test_server_fallback_request_refused(monkeypatch):
    monkeypatch.setattr(config, "USE_FALLBACKS", True)
    sdk = mock_sdk()
    with pytest.raises(lp.PilotStop, match="fallback_requested"):
        _guarded(_structured, sdk)
    assert sdk.sent == []


def test_async_sdk_blocked_during_guard():
    sdk = mock_sdk(async_=True)

    async def call():
        return await sdk.messages.create(model=config.MODEL, max_tokens=10, messages=[{"role": "user", "content": "x"}])
    with pytest.raises(call_guard.UnmeteredCallBlocked) as e:
        _guarded(lambda: asyncio.run(call()))
    assert e.value.args[1] == "async_sdk" and sdk.sent == []


def test_real_sdk_works_normally_without_guard():
    """보호 장치가 없을 때 SDK 동작(자동 재시도 포함)은 그대로."""
    sdk = mock_sdk(max_retries=2, fail_first=1)
    r = sdk.messages.create(model=config.MODEL, max_tokens=10, messages=[{"role": "user", "content": "x"}])
    assert r.usage.input_tokens == 10 and len(sdk.sent) == 2


# ---------------------------------------------------------------- 유료 실행 경로


def test_paid_path_keeps_guard_and_switch_off():
    assert lp.PAID_EXECUTION_ENABLED is False
    with pytest.raises(lp.PaidRunNotApproved):
        lp.run_pilot({"experiment": "A_pipeline"}, None, make_client=lambda *a: pytest.fail("클라이언트"))
    src = (Path(lpx.__file__)).read_text(encoding="utf-8")
    assert 'kind == "sdk" and not process_guard' in src  # 실제 실행은 보호를 끌 수 없음
    r, _ = _execute("A_pipeline", (COHORT,))
    assert r["process_guard"] is True  # 기본값
