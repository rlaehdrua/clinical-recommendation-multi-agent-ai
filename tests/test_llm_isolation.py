"""실행별 LLM 격리 (Phase 5-2): 클라이언트·설정·질문 생성기 교체가 실행 컨텍스트에만 적용되는지. API 호출 없음.

전역 llm.client·config 값을 바꾸지 않고 contextvars로 실행마다 따로 적용합니다. contextvars는 스레드에 자동으로
넘어가지 않으므로(copy_context 필요), 넘기지 않은 스레드·run_in_executor는 기본 클라이언트로 간다는 것도 확인합니다.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare_llm_pilot as plp  # noqa: E402
import web_harness as h  # noqa: E402
from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, llm, llm_pilot as lp, llm_pilot_exec as lpx, question_eval as qe  # noqa: E402
from ctrec.agents import question_generator  # noqa: E402
from ctrec.llm_pilot_fake import FakeSDKClient  # noqa: E402
from ctrec.schemas import MatcherOutput, PatientProfile, QuestionSet, RecommendationDraft  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}
PAIRS = {gt.case_id: (c, gt) for c, gt in plp.load_pairs(PARSED, "representative_egfr")[1]}
RESCUE, SHARED, BIOMARKER, COHORT, CONFIRMED, EGFR = lp.PILOT_CASE_POOL
MODEL_B = "claude-test-model-b"
KEY = "sk-ant-test-isolation-key-0000"


@pytest.fixture(autouse=True)
def guard_real_sdk(monkeypatch):
    """실제 anthropic 클라이언트 생성 0회. 기본 클라이언트가 필요한 테스트는 llm._client에 대역을 직접 넣음."""
    import anthropic
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: made.append(1) or pytest.fail("API 클라이언트 생성"))
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    yield made
    assert made == []


def _limits(**kw):
    base = dict(max_cost_usd=100.0, max_calls=1000, max_input_tokens=10 ** 9, max_output_tokens=10 ** 9,
                max_retries_per_call=2, max_total_retries=50, max_consecutive_failures=50)
    base.update(kw)
    return lp.PilotLimits(**base)


def _price(model=None):
    return lp.PriceTable(model=model or config.MODEL, input=3, output=15, cache_write=3.75, cache_read=0.3)


def _plan(exp="A_pipeline", ids=(BIOMARKER, COHORT), model=None):
    return {"experiment": exp, "case_ids": list(ids), "model": model or config.MODEL,
            "price": asdict(_price(model)), "rounds": lp.EXPERIMENTS[exp]["rounds"], "question_budget": 3,
            "limits": asdict(_limits()), "save_patient_data": False, "redact_outputs": True}


def _execute(exp="A_pipeline", ids=(BIOMARKER, COHORT), fake=None, limits=None, model=None, **kw):
    fake = fake or FakeSDKClient()
    r = lpx.execute(_plan(exp, ids, model), _price(model), limits or _limits(), fake, [PAIRS[i] for i in ids],
                    TRIALS, sleep=lambda s: None, **kw)
    return r, fake


def _parallel(*jobs):
    """작업마다 새 스레드 (컨텍스트를 넘기지 않음 - 각 실행이 스스로 컨텍스트를 설정해야 함)."""
    out, errors = [None] * len(jobs), []

    def run(i, fn):
        try:
            out[i] = fn()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(jobs)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert not errors, errors
    return out


def _rendezvous(n=2):
    """각 실행의 첫 요청에서 서로를 기다림 -> 실제로 겹쳐서 실행됨을 보장."""
    b = threading.Barrier(n, timeout=10)
    seen = set()

    def make(name):
        def on_call(i, kw):
            if name not in seen:
                seen.add(name)
                b.wait()
        return on_call
    return make


def _global_state():
    return llm.client, llm._client, llm._client_var.get(), config.MODEL, config.USE_FALLBACKS, \
        config.WRITE_REPORT, config.QUESTION_POLICY, config.MAX_QUESTIONS_PER_ROUND, config.active_overrides(), \
        question_generator._override_var.get()


# ---------------------------------------------------------------- 1·2·3·4·13: 동시 실행, 상한·모델 독립, 정확한 집계

def test_pilots_a_and_b_concurrently_with_independent_caps_models_and_accounting():
    before = _global_state()
    meet = _rendezvous(3)
    fa, fb = FakeSDKClient(on_call=meet("A")), FakeSDKClient(on_call=meet("B"))
    fc = FakeSDKClient(on_call=meet("C"))
    (ra, _), (rb, _), (rc, _) = _parallel(
        lambda: _execute("A_pipeline", (SHARED, CONFIRMED), fa),
        lambda: _execute("B_question_plan", (RESCUE, SHARED, BIOMARKER), fb, model=MODEL_B),
        # 같은 시점에 예산이 작은 실행: 이 실행만 비용 상한에 걸림
        lambda: _execute("A_pipeline", (COHORT, BIOMARKER), fc, _limits(max_cost_usd=0.6)))
    assert ra["status"] == "succeeded" and rb["status"] == "succeeded"
    assert rc["stop_reason"] == "cost_cap" and rc["case_status"].get("succeeded", 0) < 2
    # 실행마다 자기 클라이언트로만 호출, 단계도 실험별로 다름
    assert {c["stage"] for c in fa.calls} == {"patient_profiler", "matcher", "recommender"}
    assert {c["stage"] for c in fb.calls} == {"patient_profiler", "matcher", "question_generator"}
    # 모델은 실행별 (B만 MODEL_B), 전역 config.MODEL은 그대로
    assert {c["model"] for c in fa.calls} == {config.MODEL} and {c["model"] for c in fb.calls} == {MODEL_B}
    assert {c["model"] for c in fc.calls} == {config.MODEL}
    # 집계: 실행별 호출 수 = 그 실행의 Fake 요청 수, 사례별 합 = 실행 합계
    for r, f in ((ra, fa), (rb, fb), (rc, fc)):
        t = r["totals"]
        assert t["calls"] == len(f.calls)
        assert sum(c["usage"]["calls"] for c in r["cases"] if "usage" in c) == t["calls"]
        # 사례별 금액(cost_actual_usd)과 실행 합계(cost_usd)는 각각 소수 넷째 자리로 반올림해 보고함(llm_pilot.Ledger).
        # 반올림한 사례별 금액의 합은 반올림한 합계와 최대 (사례 수 + 1) x 0.00005까지 다를 수 있음 -> 그 범위로 비교
        # (이전: 셋째 자리로 다시 반올림해 정확히 같다고 비교 -> 합계가 x.xxx5 근처면 계산이 맞아도 실패)
        case_costs = [c["usage"]["cost_actual_usd"] for c in r["cases"] if "usage" in c]
        assert abs(sum(case_costs) - t["cost_usd"]) <= 0.00005 * (len(case_costs) + 1) + 1e-12
        assert sum(c["usage"]["input_tokens"] for c in r["cases"] if "usage" in c) == t["input_tokens"]
    assert rc["totals"]["cost_conservative_usd"] <= 0.6 and len({ra["run_id"], rb["run_id"], rc["run_id"]}) == 3
    assert _global_state() == before


# ---------------------------------------------------------------- 5·6: 한 실행만 취소·시간 초과

def test_only_one_run_cancelled_or_timed_out():
    meet = _rendezvous(3)
    cancel = threading.Event()
    t = [0.0]
    f_cancel = FakeSDKClient(on_call=lambda i, kw: (meet("x")(i, kw), cancel.set()))
    f_time = FakeSDKClient(on_call=lambda i, kw: (meet("y")(i, kw), t.__setitem__(0, 999.0)))
    f_ok = FakeSDKClient(on_call=meet("z"))
    (r_cancel, _), (r_time, _), (r_ok, _) = _parallel(
        lambda: _execute("A_pipeline", (SHARED, COHORT), f_cancel, cancel=cancel.is_set),
        lambda: _execute("A_pipeline", (SHARED, COHORT), f_time, _limits(run_deadline_s=100), clock=lambda: t[0]),
        lambda: _execute("A_pipeline", (SHARED, COHORT), f_ok))
    assert r_cancel["stop_reason"] == "cancelled" and r_time["stop_reason"] == "timeout"
    assert r_ok["status"] == "succeeded" and r_ok["stop_reason"] is None
    assert len(f_cancel.calls) <= 2 and len(f_time.calls) <= 2  # 멈춘 뒤 더 보내지 않음


# ---------------------------------------------------------------- 7: 중첩 실행

def test_nested_run_has_its_own_meter_and_restores_outer():
    inner_result = {}

    def on_call(i, kw):
        if i == 0:  # 바깥 실행의 첫 요청 도중 다른 실행을 통째로 실행
            inner_result["r"], inner_result["fake"] = _execute("B_question_plan", (COHORT,), model=MODEL_B)
    outer_fake = FakeSDKClient(on_call=on_call)
    r, _ = _execute("A_pipeline", (BIOMARKER,), outer_fake)
    inner = inner_result["r"]
    assert r["status"] == "succeeded" and inner["status"] == "succeeded"
    assert r["totals"]["calls"] == len(outer_fake.calls) and inner["totals"]["calls"] == len(inner_result["fake"].calls)
    assert {c["model"] for c in outer_fake.calls} == {config.MODEL}  # 안쪽 실행이 끝난 뒤 바깥 설정 복구
    assert {c["model"] for c in inner_result["fake"].calls} == {MODEL_B}


def test_nested_context_helpers_restore_in_order():
    a, b = object(), object()
    with llm.use_client(a), config.overrides(MODEL="m1", WRITE_REPORT=False):
        with llm.use_client(b), config.overrides(MODEL="m2"):
            assert llm.current_client() is b and config.MODEL == "m2" and config.WRITE_REPORT is False
        assert llm.current_client() is a and config.MODEL == "m1"
    assert llm._client_var.get() is None and config.active_overrides() == {}
    with pytest.raises(KeyError):
        with config.overrides(PARSED_DIR="x"):  # 실행별로 바꿀 수 없는 설정
            pass


# ---------------------------------------------------------------- 8: 예외 후 복원

def test_state_restored_after_exception(monkeypatch):
    before = _global_state()
    with pytest.raises(RuntimeError):
        with lp.metered(lp.PilotMeter(_limits(), _price()), FakeSDKClient()), config.overrides(MODEL=MODEL_B), \
                question_generator.use_generator(lambda *a: None):
            raise RuntimeError("boom")
    assert _global_state() == before

    def boom(*a, **k):
        raise RuntimeError("실험 코드 예외")
    monkeypatch.setattr(lp, "run_experiment_a", boom)
    with pytest.raises(RuntimeError):
        _execute("A_pipeline", (COHORT,))
    assert _global_state() == before


# ---------------------------------------------------------------- 9: async task 분리

def test_async_tasks_isolated_and_executor_needs_explicit_context():
    a, b = FakeSDKClient(), FakeSDKClient()

    async def task(client, model, seen):
        with llm.use_client(client), config.overrides(MODEL=model):
            for _ in range(3):
                await asyncio.sleep(0)  # 다른 task와 번갈아 실행
                seen.append((llm.current_client() is client, config.MODEL == model))

    async def main():
        s1, s2 = [], []
        await asyncio.gather(task(a, "m-a", s1), task(b, "m-b", s2))
        loop = asyncio.get_running_loop()
        with llm.use_client(a):
            # run_in_executor는 컨텍스트를 넘기지 않음 -> 기본 클라이언트 쪽(None)을 봄
            raw = await loop.run_in_executor(None, llm._client_var.get)
            # asyncio.to_thread는 copy_context로 넘김
            copied = await asyncio.to_thread(llm._client_var.get)
        return s1, s2, raw, copied
    s1, s2, raw, copied = asyncio.run(main())
    assert s1 == s2 == [(True, True)] * 3
    assert raw is None and copied is a
    assert llm._client_var.get() is None


def test_async_pilots_via_to_thread_are_isolated():
    async def main():
        return await asyncio.gather(
            asyncio.to_thread(_execute, "A_pipeline", (COHORT,)),
            asyncio.to_thread(_execute, "B_question_plan", (BIOMARKER,), None, None, MODEL_B))
    (ra, fa), (rb, fb) = asyncio.run(main())
    assert ra["status"] == rb["status"] == "succeeded"
    assert ra["totals"]["calls"] == len(fa.calls) and rb["totals"]["calls"] == len(fb.calls)
    assert {c["model"] for c in fb.calls} == {MODEL_B}


# ---------------------------------------------------------------- 10: ThreadPoolExecutor

def test_thread_pool_requires_copy_context():
    a = FakeSDKClient()
    with llm.use_client(a), config.overrides(MODEL=MODEL_B), ThreadPoolExecutor(2) as pool:
        bare = pool.submit(lambda: (llm._client_var.get(), config.MODEL)).result()
        copied = pool.submit(contextvars.copy_context().run, lambda: (llm._client_var.get(), config.MODEL)).result()
    assert bare == (None, config.MODEL) and copied == (a, MODEL_B)


def test_session_parallel_matcher_threads_use_run_client(monkeypatch):
    monkeypatch.setattr(config, "MAX_WORKERS", 4)
    r, fake = _execute("A_pipeline", (SHARED,))  # 시험 2개의 Matcher를 Session._parallel 스레드에서 동시 실행
    assert r["totals"]["by_stage"]["matcher"]["calls"] >= 2 and r["totals"]["calls"] == len(fake.calls)


def test_every_thread_spawn_in_src_copies_context():
    """LLM을 부를 수 있는 하위 스레드는 copy_context로 실행 컨텍스트를 넘겨야 함 (아니면 기본 클라이언트로 감).

    예외: cli.py의 환자별 병렬 처리는 작업 스레드 안에서 새 빈 컨텍스트를 만들어 환자별 사용량을 따로 셈.
    CLI는 실행별 클라이언트를 쓰지 않으므로 기본 클라이언트가 의도된 동작."""
    allowed = {("cli.py", "pool.submit(work, c)")}
    offenders = []
    for p in (ROOT / "src/ctrec").rglob("*.py"):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.submit\(|threading\.Thread\(|run_in_executor\(", line) \
                    and "copy_context" not in line and "ctx.run" not in line \
                    and not any(p.name == f and frag in line for f, frag in allowed):
                offenders.append(f"{p.name}:{n}")
    assert offenders == []


# ---------------------------------------------------------------- 11: 일반 웹 요청과 파일럿 동시 실행

class WebDefaultSDK:
    """웹 하네스의 합성 LLM을 실제 SDK 모양(messages.create)으로 감싼 '기본 클라이언트'. 실제 llm.create 경로를 탐."""
    max_retries = 0
    _CLS = {"patient_profiler": PatientProfile, "matcher": MatcherOutput, "question_generator": QuestionSet,
            "recommender": RecommendationDraft}

    def __init__(self, on_call=None):
        self.fake = h.FakeLLM()
        self.models, self.fallbacks, self.stages = [], [], []
        self._on_call = on_call
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        if self._on_call:
            self._on_call()
        self.models.append(kw.get("model"))
        self.fallbacks.append("fallbacks" in kw)
        system = kw["system"]
        text_system = system[0]["text"] if isinstance(system, list) else system
        if kw.get("tools"):
            self.stages.append("orchestrator")
            r = self.fake.create(system=system, messages=kw["messages"], tools=kw["tools"])
            r.model, r.usage = kw.get("model"), None
            return r
        stage = lp.stage_of(system)
        self.stages.append(stage)
        user = kw["messages"][0]["content"]
        if (kw.get("output_config") or {}).get("format"):
            text = self.fake.structured(self._CLS[stage], system=text_system, user=user).model_dump_json()
        else:
            text = self.fake.plain(system=text_system, user=user)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=None, model=kw.get("model"))


def test_web_run_and_pilot_concurrently(monkeypatch, tmp_path):
    """Fake 파일럿을 프로세스 보호 없이(process_guard=False, Fake 전용) 돌릴 때 웹과 공존. 실제 유료 파일럿은 보호를
    끌 수 없으므로 웹과 같은 프로세스에서 돌면 웹 호출이 막힘 (test_call_guard.py)."""
    from ctrec.web import app as web
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web, "ANSWER_TIMEOUT_S", 20)
    web.RUNS.clear()
    web.CLIENT_STARTS.clear()
    for name in ("CTREC_WEB_SAVE_OUTPUTS", "CTREC_REQUIRE_HTTPS", "CTREC_ALLOW_INSECURE_HTTP", "CTREC_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)
    default_fallbacks = config.USE_FALLBACKS
    meet = threading.Barrier(2, timeout=10)
    met = {"web": False, "pilot": False}

    def once(name):
        def f(*a):
            if not met[name]:
                met[name] = True
                meet.wait()
        return f
    web_sdk = WebDefaultSDK(on_call=once("web"))
    monkeypatch.setattr(llm, "_client", web_sdk)  # 일반 웹 실행이 쓰는 기본 클라이언트
    monkeypatch.setattr(web, "TEST_LLM_BACKEND", "fake")  # 명시적 테스트 대역 (Phase 8 실제 호출 허가 게이트 대신)
    pilot_fake = FakeSDKClient(on_call=lambda i, kw: once("pilot")())
    holder = {}
    pilot_thread = threading.Thread(target=lambda: holder.setdefault(
        "r", _execute("A_pipeline", (SHARED, COHORT), pilot_fake, model=MODEL_B, process_guard=False)))
    pilot_thread.start()

    browser = h.browser()
    r = browser.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False})
    assert r.status_code == 200
    run = web.RUNS[r.json()["run_id"]]
    q = h.wait_for(run, "questions")
    assert browser.post(f"/api/runs/{run.run_id}/answers",
                        json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}}).status_code == 200
    h.wait_for(run, "end", 30)
    pilot_thread.join(60)
    pilot = holder["r"][0]
    assert any(e["type"] == "result" for e in run.events)  # 웹 실행 정상 완료
    assert pilot["status"] == "succeeded"
    # 웹은 기본 클라이언트·기본 모델·기본 대체 모델 정책 그대로, 파일럿 설정을 상속하지 않음
    assert web_sdk.models and set(web_sdk.models) == {config.MODEL}
    assert set(web_sdk.fallbacks) == {default_fallbacks}
    assert "orchestrator" in web_sdk.stages and len(web_sdk.stages) == len(web_sdk.fake.calls)
    # 파일럿 호출은 모두 파일럿 Fake로, 파일럿 계측에는 파일럿 호출만
    assert {c["model"] for c in pilot_fake.calls} == {MODEL_B} and not any(c["fallbacks"] for c in pilot_fake.calls)
    assert pilot["totals"]["calls"] == len(pilot_fake.calls)
    assert "orchestrator" not in {c["stage"] for c in pilot_fake.calls}
    assert llm._client is web_sdk and config.active_overrides() == {}


# ---------------------------------------------------------------- 12·14: 전역 불변, 누출 없음

def test_global_client_and_settings_unchanged_during_and_after_runs():
    before = _global_state()
    seen = []

    def on_call(i, kw):  # 실행 도중 다른(컨텍스트 없는) 스레드에서 본 전역 상태
        t = threading.Thread(target=lambda: seen.append(_global_state()))
        t.start()
        t.join()
    _execute("A_pipeline", (COHORT,), FakeSDKClient(on_call=on_call))
    _execute("B_question_plan", (COHORT,), FakeSDKClient(on_call=on_call), model=MODEL_B)
    assert seen and all(s == before for s in seen) and _global_state() == before


def test_concurrent_results_have_no_key_or_patient_text():
    (ra, _), (rb, _) = _parallel(lambda: _execute("A_pipeline", (EGFR, COHORT), FakeSDKClient(faults=lambda i, s: "pii")),
                                 lambda: _execute("B_question_plan", (RESCUE,), model=MODEL_B))
    blob = json.dumps([ra, rb], ensure_ascii=False)
    assert KEY not in blob and "010-1234-5678" not in blob
    lp.assert_redacted([ra, rb], lp.forbidden_texts([PAIRS[i] for i in (EGFR, COHORT, RESCUE)]))


# ---------------------------------------------------------------- 공용 상한 (실행별 상한과 별도 계층)

def test_shared_cost_cap_stops_all_runs_even_within_per_run_caps():
    one_call = _price().cost(0, 16000)  # 호출 1회 예약의 하한 (출력 max_tokens)
    cap = one_call * 4
    shared = lp.SharedBudget(max_cost_usd=cap, max_concurrent_runs=4)
    meet = _rendezvous(2)
    (ra, fa), (rb, fb) = _parallel(
        lambda: _execute("A_pipeline", (SHARED, COHORT), FakeSDKClient(on_call=meet("a")), shared=shared),
        lambda: _execute("A_pipeline", (SHARED, COHORT), FakeSDKClient(on_call=meet("b")), shared=shared))
    # 두 실행 모두 실행별 상한(100$)보다 훨씬 적게 썼지만, 공용 상한 때문에 적어도 한 실행은 멈춤
    stops = {ra["stop_reason"], rb["stop_reason"]}
    assert "shared_cost_cap" in stops and stops <= {"shared_cost_cap", None}
    assert ra["totals"]["cost_conservative_usd"] < 100 and rb["totals"]["cost_conservative_usd"] < 100
    rep = shared.report()
    assert rep["reserved_usd"] == 0 and rep["active_runs"] == 0
    assert rep["committed_usd"] == pytest.approx(ra["totals"]["cost_conservative_usd"]
                                                 + rb["totals"]["cost_conservative_usd"], abs=1e-3)
    assert rep["committed_usd"] <= cap


def test_shared_concurrency_cap_and_per_run_cap_layers_are_separate():
    shared = lp.SharedBudget(max_cost_usd=1000, max_concurrent_runs=1)
    hold, entered = threading.Event(), threading.Event()

    def on_call(i, kw):
        entered.set()
        hold.wait(10)
    t = threading.Thread(target=lambda: _execute("A_pipeline", (COHORT,), FakeSDKClient(on_call=on_call), shared=shared))
    t.start()
    entered.wait(10)
    with pytest.raises(lp.PilotStop, match="shared_concurrency_cap"):
        _execute("A_pipeline", (COHORT,), shared=shared)
    hold.set()
    t.join(30)
    # 실행별 상한에 걸린 실행은 공용 상한을 쓰지 않은 만큼 다른 실행이 계속 쓸 수 있음
    r_small, _ = _execute("A_pipeline", (COHORT,), limits=_limits(max_cost_usd=0.01), shared=shared)
    r_ok, _ = _execute("A_pipeline", (COHORT,), shared=shared)
    assert r_small["stop_reason"] == "cost_cap" and r_ok["status"] == "succeeded"
    assert shared.report()["active_runs"] == 0 and shared.report()["reserved_usd"] == 0


# ---------------------------------------------------------------- 기존 기본 동작 불변

def test_default_paths_unchanged_without_context():
    """컨텍스트 설정이 없으면 기본 클라이언트·기본 설정·기본 질문 생성기 (기존 CLI·웹과 같음)."""
    sentinel = FakeSDKClient()
    llm._client = sentinel
    assert llm.current_client() is sentinel and config.active_overrides() == {}
    assert config.MODEL == config.__dict__["MODEL"]
    # 질문 생성기: 다른 스레드의 교체는 이 스레드에 보이지 않음
    seen = []
    ready, done = threading.Event(), threading.Event()

    def other():
        with question_generator.use_generator(lambda *a: "override"):
            ready.set()
            done.wait(5)
    t = threading.Thread(target=other)
    t.start()
    ready.wait(5)
    seen.append(question_generator._override_var.get())
    done.set()
    t.join()
    assert seen == [None]


def test_run_method_generator_swap_is_context_scoped():
    """오프라인 평가(run_method)의 질문 생성기 교체가 모듈 전역을 바꾸지 않음."""
    real = question_generator.generate_questions
    with qe.OfflineEnvironment(3):
        qe.run_method("candidate", *PAIRS[COHORT], TRIALS, 1, 3)
        assert question_generator.generate_questions is real and question_generator._override_var.get() is None


def test_pilot_switch_still_off():
    assert lp.PAID_EXECUTION_ENABLED is False
    plan = _plan()
    with pytest.raises(lp.PaidRunNotApproved):
        lp.run_pilot(plan, None, make_client=lambda *a: pytest.fail("클라이언트 생성"))
    assert replace(_price(), confirmed=True).confirmed  # 단가 확인 절차는 이번 단계에서 하지 않음 (게이트 그대로)
