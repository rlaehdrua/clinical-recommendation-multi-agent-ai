"""실제 LLM 파일럿 실행 경로 (Phase 5-1): 승인 게이트·실제 SDK 연결 차단·Fake SDK 장애 재현·실험 A/B 실행. API 호출 없음.

Fake SDK(llm_pilot_fake.FakeSDKClient)는 실제 SDK와 같은 messages.create 인터페이스로 응답하고 네트워크를 쓰지 않습니다.
모든 테스트에서 anthropic 클라이언트가 만들어지면 실패합니다.
"""

from __future__ import annotations

import contextvars
import json
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prepare_llm_pilot as plp  # noqa: E402
import run_llm_pilot as rlp  # noqa: E402
from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, llm, llm_pilot as lp, llm_pilot_exec as lpx, question_eval as qe, store_patch  # noqa: E402
from ctrec.llm_pilot_fake import FAKE_PII, FakeSDKClient  # noqa: E402
from ctrec.schemas import MatcherOutput  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}
PAIRS = {gt.case_id: (c, gt) for c, gt in plp.load_pairs(PARSED, "representative_egfr")[1]}
RESCUE, SHARED, BIOMARKER, COHORT, CONFIRMED, EGFR = lp.PILOT_CASE_POOL


@pytest.fixture(autouse=True)
def no_sdk_client(monkeypatch):
    """실제 anthropic 클라이언트가 만들어지면 실패 (네트워크·유료 호출 없음 보장)."""
    import anthropic
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: made.append(1) or pytest.fail("API 클라이언트 생성"))
    monkeypatch.setattr(llm, "_client", None)
    return made


def _limits(**kw):
    base = dict(max_cost_usd=100.0, max_calls=1000, max_input_tokens=10 ** 9, max_output_tokens=10 ** 9,
                max_retries_per_call=2, max_total_retries=50, max_consecutive_failures=50)
    base.update(kw)
    return lp.PilotLimits(**base)


PRICE = lp.PriceTable(model=config.MODEL, input=3, output=15, cache_write=3.75, cache_read=0.3)


def _plan(exp="A_pipeline", ids=(BIOMARKER, COHORT)):
    return {"experiment": exp, "case_ids": list(ids), "model": config.MODEL, "price": asdict(PRICE),
            "rounds": lp.EXPERIMENTS[exp]["rounds"], "question_budget": 3, "limits": asdict(_limits()),
            "save_patient_data": False, "redact_outputs": True}


def _run(exp="A_pipeline", ids=(BIOMARKER, COHORT), fake=None, limits=None, **kw):
    fake = fake or FakeSDKClient()
    r = lpx.execute(_plan(exp, ids), PRICE, limits or _limits(), fake, [PAIRS[i] for i in ids], TRIALS,
                    sleep=lambda s: None, **kw)
    return r, fake


def _stages(fake):
    return [c["stage"] for c in fake.calls]


# ---------------------------------------------------------------- 승인 게이트·실제 SDK 차단

def test_switch_off_and_no_real_client_path(no_sdk_client):
    assert lp.PAID_EXECUTION_ENABLED is False
    plan = _plan()
    forged = lp.ExecutionTicket(lp.plan_hash(plan), plan["experiment"], lp._TICKET_KEY)  # 내부 키로 위조해도
    assert not lp.ticket_valid(forged, plan, None)
    with pytest.raises(lp.PaidRunNotApproved):
        lp.sdk_client(forged, plan, None)
    with pytest.raises(lp.PaidRunNotApproved):
        lp.run_pilot(plan, None, make_client=lambda *a: pytest.fail("클라이언트 생성 시도"))
    assert no_sdk_client == []


def test_execute_rejects_any_non_fake_client():
    class LooksLikeSDK:  # anthropic 모듈의 클라이언트로 보이는 객체
        max_retries = 0
        messages = SimpleNamespace(create=lambda **k: pytest.fail("요청"))
    LooksLikeSDK.__module__ = "anthropic._client"

    class Plain:
        max_retries = 0
        messages = SimpleNamespace(create=lambda **k: pytest.fail("요청"))
    for inner in (LooksLikeSDK(), Plain()):
        with pytest.raises(lp.PaidRunNotApproved):
            lpx.execute(_plan(), PRICE, _limits(), inner, [PAIRS[BIOMARKER], PAIRS[COHORT]], TRIALS)


def test_cli_with_every_flag_and_key_still_refused(monkeypatch, capsys, no_sdk_client):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-123")
    monkeypatch.setenv("CTREC_ALLOW_PAID_EVAL", "1")
    monkeypatch.setenv("CTREC_FALLBACKS", "0")
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    price = ["--price-input", "2", "--price-output", "10", "--price-cache-write", "2.5", "--price-cache-read", "0.2",
             "--price-source", "test", "--price-confirmed"]
    first = rlp.main(["--experiment", "A_pipeline", *price])
    phrase = first["plan"]["confirmation_phrase"]
    out = rlp.main(["--experiment", "A_pipeline", *price, "--approved-by", "test", "--confirm", phrase, "--execute"])
    assert out["gate_problems"] == []  # 12개 조건을 모두 갖춰도
    assert "PAID_EXECUTION_ENABLED" in out["refused"] and "result" not in out
    assert no_sdk_client == [] and "sk-ant-test" not in capsys.readouterr().out
    assert not (ROOT / "outputs").exists()


@pytest.mark.parametrize("env", [
    {},  # API 키가 있어도 기본 OFF
    {"CTREC_ALLOW_PAID_EVAL": "1", "CTREC_FALLBACKS": "0"},  # 승인 플래그만으로는 실행 불가
])
def test_default_off_and_flag_alone(monkeypatch, env, no_sdk_client):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-123")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    out = rlp.main(["--experiment", "B_question_plan", "--execute"])
    assert out["gate_problems"] == ["명시적 사용자 승인 없음"] and "refused" in out and "result" not in out
    assert no_sdk_client == []


@pytest.mark.parametrize("change, expect", [
    (lambda p: {**p, "case_ids": ["NOT-IN-POOL"]}, "승인된 합성 사례"),
    (lambda p: {**p, "experiment": "C"}, "실험 A_pipeline 또는 B_question_plan"),
    (lambda p: {**p, "price": {**p["price"], "output": 1.0}}, "승인 단가와 계획 단가"),
    (lambda p: {**p, "question_budget": 5}, "계획 해시 불일치"),
])
def test_plan_changes_after_approval_block(monkeypatch, change, expect):
    monkeypatch.setenv("CTREC_ALLOW_PAID_EVAL", "1")
    monkeypatch.setenv("CTREC_FALLBACKS", "0")
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    price = replace(PRICE, confirmed=True, source="test")
    plan = {**_plan(), "price": asdict(price)}
    h = lp.plan_hash(plan)
    approval = lp.PilotApproval("user", "A_pipeline", h, price, _limits(), f"RUN A_pipeline {h[:12]}")
    assert lp.approval_problems(plan, approval) == []
    problems = lp.approval_problems(change(plan), approval)
    assert any(expect in p for p in problems), problems


def test_fallbacks_env_must_be_explicit_zero(monkeypatch):
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    monkeypatch.delenv("CTREC_FALLBACKS", raising=False)
    assert any("CTREC_FALLBACKS=0" in p for p in lp.approval_problems(_plan(), None) + lp.approval_problems(
        _plan(), lp.PilotApproval("u", "A_pipeline", "", PRICE, _limits(), "")))


def test_model_price_mismatch_blocks_before_request(monkeypatch):
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    fake = FakeSDKClient()
    m = lp.PilotMeter(_limits(), replace(PRICE, model="claude-other"))
    with lp.metered(m, fake), pytest.raises(lp.PilotStop, match="model_price_mismatch"):
        llm.structured(MatcherOutput, system="s", user="u")
    assert fake.calls == []


def test_fallback_request_and_sdk_retries_refused(monkeypatch):
    fake = FakeSDKClient()
    m = lp.PilotMeter(_limits(), PRICE)
    monkeypatch.setattr(config, "USE_FALLBACKS", True)  # llm.create가 beta fallbacks로 보내려 해도
    with lp.metered(m, fake), pytest.raises(lp.PilotStop, match="fallback_requested"):
        llm.structured(MatcherOutput, system="s", user="u")
    assert fake.calls == []

    class Retrying(FakeSDKClient):
        max_retries = 4
    with pytest.raises(lp.PilotStop, match="sdk_retries_enabled"):
        lp.MeteredClient(Retrying(), m)


# ---------------------------------------------------------------- 실험 A·B 실행 경로

def test_experiment_a_runs_real_stages_through_meter():
    r, fake = _run("A_pipeline", (SHARED, CONFIRMED))
    assert r["status"] == "succeeded" and r["client"] == "fake_sdk" and r["paid"] is False
    assert set(_stages(fake)) == {"patient_profiler", "matcher", "recommender"}  # 질문 생성 LLM·가상 환자 LLM 없음
    assert all(c["timeout"] == _limits().call_timeout_s and c["fallbacks"] is False for c in fake.calls)
    for c in r["cases"]:
        u = c["usage"]
        assert c["status"] == "succeeded" and u["calls"] == sum(s["calls"] for s in u["by_stage"].values())
        assert u["by_stage"]["patient_profiler"]["calls"] == 2  # 처음 + 답변 반영 재추출
        assert u["input_tokens"] > 0 and u["output_tokens"] > 0 and u["cost_actual_usd"] > 0
        m = c["metrics"]
        assert m["facts"]["promoted_to_document_verified"] == 0 and m["facts"]["answer_facts_not_self_report"] == 0
        assert m["extraction"]["checked"] >= 1 and m["matcher"]["not_evaluated"] == 0
    assert r["totals"]["by_stage"]["matcher"]["thinking_blocks"] > 0  # 사고 블록 수 기록 (토큰은 출력에 포함)
    assert r["frozen_store_intact"] and r["patient_data_saved"] is False


def test_experiment_a_same_results_as_offline_path():
    """Fake SDK 경로의 판정·사실 지표가 기존 오프라인 대역 경로와 같음 (Matcher·추천·Decision Loop 정책 불변)."""
    r, _ = _run("A_pipeline", (SHARED, CONFIRMED, EGFR))
    with qe.OfflineEnvironment(3):
        offline = [lp.run_experiment_a(*PAIRS[i], TRIALS) for i in (SHARED, CONFIRMED, EGFR)]
    for row, off in zip(r["cases"], offline):
        m = row["metrics"]
        for k in ("facts", "egfr", "matcher", "questions", "safety", "extraction"):
            assert m[k] == off[k], k


def test_experiment_a_egfr_case_counts():
    r, _ = _run("A_pipeline", (EGFR,))
    e = r["cases"][0]["metrics"]["egfr"]
    assert e.get("kidney_egfr_facts", 0) >= 1 and e.get("kidney_egfr_linked_to_EGFR", 0) == 0
    assert r["summary"]["kidney_egfr_linked_to_EGFR"] == 0


def test_experiment_b_same_state_and_no_answers():
    ids = (RESCUE, SHARED, BIOMARKER)
    r, fake = _run("B_question_plan", ids)
    assert r["status"] == "succeeded" and r["summary"]["valid"] == 3 and r["summary"]["invalid"] == 0
    assert _stages(fake).count("question_generator") == 3  # 기존 LLM 질문 생성기를 사례마다 1회
    assert "recommender" not in _stages(fake)
    for c in r["cases"]:
        m = c["metrics"]
        assert m["same_state"] and m["same_generator_inputs"] and m["comparison_valid"]
        assert set(m["plans"]) == {"decision_loop", "llm_generator"}
        assert all(p["metrics"]["n_questions"] <= 3 for p in m["plans"].values())
        assert c["usage"]["by_stage"]["patient_profiler"]["calls"] == 1  # 답변 반영·재추출 없음


def test_experiment_b_state_divergence_invalidates_comparison():
    case, gt = PAIRS[BIOMARKER]
    holder = {}

    def mutating(profile, matches, qa_log, n, acquisition_plan=None):  # 생성기가 원래 Session 상태를 바꾸는 경우
        s = holder["s"]
        s.fact_store.add(s.case.patient_id, "biomarker", "changed", source_type="self_report")
        return qe.baseline_generator(profile, matches, qa_log, n, acquisition_plan)

    from ctrec import pipeline
    real_match = pipeline.Session.match

    def grab(self, *a, **k):
        holder["s"] = self
        return real_match(self, *a, **k)
    with qe.OfflineEnvironment(3):
        pipeline.Session.match = grab
        try:
            r = lp.compare_plans(case, gt, TRIALS, 3, llm_generate=mutating)
        finally:
            pipeline.Session.match = real_match
    assert r["status"] == "state_diverged" and r["comparison_valid"] is False
    s = lp.summarize_b([r, {**r, "comparison_valid": True, "status": "ok"}])
    assert s["invalid"] == 1 and s["valid"] == 1


def test_simulated_calls_match_offline_measurement():
    ids = (SHARED, CONFIRMED)
    measured = lp.measure_offline([PAIRS[i] for i in ids], TRIALS)["per_experiment"]
    for exp in lp.EXPERIMENTS:
        r = lpx.simulate(_plan(exp, ids), [PAIRS[i] for i in ids], TRIALS)
        assert r["totals"]["by_stage"] and {k: v["calls"] for k, v in r["totals"]["by_stage"].items()} \
            == measured[exp]["calls"]


def test_config_restored_after_run(monkeypatch):
    before = {k: getattr(config, k) for k in ("WRITE_REPORT", "QUESTION_POLICY", "MAX_QUESTIONS_PER_ROUND",
                                             "USE_FALLBACKS", "LAZY_COMPILE")}
    real_client = llm.client
    _run("A_pipeline", (COHORT,))
    assert {k: getattr(config, k) for k in before} == before and llm.client is real_client


# ---------------------------------------------------------------- Fake SDK 장애 재현

def _at(code, stage=None, first=1, start=0):
    """start번째 호출부터 first번 (stage가 주어지면 그 단계만) 장애."""
    seen = [0]

    def f(i, st):
        if i < start or (stage and st != stage) or seen[0] >= first:
            return None
        seen[0] += 1
        return code
    return f


def test_429_retried_not_billed():
    r, fake = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=_at("429")))
    t = r["totals"]
    assert r["status"] == "succeeded" and t["retries"] == 1 and t["failures"] == {"RateLimitError": 1}
    assert t["possibly_billed_failures"] == 0 and t["cost_estimated_usd"] == 0
    assert len(fake.calls) == t["calls"]


def test_5xx_retried_and_conservatively_billed():
    r, _ = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=_at("5xx")))
    t = r["totals"]
    assert r["status"] == "succeeded" and t["retries"] == 1 and t["possibly_billed_failures"] == 1
    assert t["cost_estimated_usd"] > 0 and t["cost_conservative_usd"] == round(t["cost_usd"] + t["cost_estimated_usd"], 4)


def test_bad_request_not_retried():
    r, fake = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=_at("400", "patient_profiler")))
    c = r["cases"][0]
    assert c["status"] == "failed" and c["error_type"] == "BadRequestError" and r["totals"]["retries"] == 0
    assert len(fake.calls) == 1  # 프로파일러 실패 뒤 더 호출하지 않음


def test_timeout_retried_billed_and_timeout_passed():
    r, fake = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=_at("timeout", first=2)), _limits(call_timeout_s=33.0))
    t = r["totals"]
    assert t["failures"] == {"APITimeoutError": 2} and t["retries"] == 2 and t["possibly_billed_failures"] == 2
    assert r["status"] == "succeeded" and all(c["timeout"] == 33.0 for c in fake.calls)


def test_retry_cap_per_call():
    r, fake = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=_at("5xx", "patient_profiler", first=10)))
    assert len(fake.calls) == 3 and r["cases"][0]["status"] == "failed"  # 1 + 재시도 2


def test_truncated_response_counted_and_case_failed():
    r, _ = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=_at("truncated", "matcher")))
    c = r["cases"][0]
    assert c["status"] == "failed" and "TruncatedError" in c["metrics"]["errors"]
    assert c["usage"]["by_stage"]["matcher"]["stop_max_tokens"] == 1 and c["usage"]["cost_actual_usd"] > 0


def test_refusal_counted_and_case_failed():
    r, _ = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=_at("refusal", "matcher")))
    c = r["cases"][0]
    assert c["status"] == "failed" and "RefusalError" in c["metrics"]["errors"]
    assert c["usage"]["by_stage"]["matcher"]["stop_refusal"] == 1


def test_missing_usage_stops_next_request():
    r, fake = _run("A_pipeline", (COHORT, BIOMARKER), FakeSDKClient(faults=_at("no_usage")))
    t = r["totals"]
    assert r["status"] == "aborted" and r["stop_reason"] == "usage_missing" and len(fake.calls) == 1
    assert t["usage_missing"] == 1 and t["cost_estimated_usd"] > 0
    assert [c["status"] for c in r["cases"]] == ["aborted", "skipped"]


@pytest.mark.parametrize("fault, code", [("other_model", "model_mismatch"), ("fallback_iteration", "fallback_detected")])
def test_response_from_other_model_not_used(fault, code):
    r, fake = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=_at(fault)))
    assert r["stop_reason"] == code and r["cases"][0]["status"] == "aborted" and len(fake.calls) == 1


def test_cancel_mid_run():
    flag = threading.Event()
    fake = FakeSDKClient(on_call=lambda i, kw: flag.set() if i == 1 else None)
    r, _ = _run("A_pipeline", (SHARED, COHORT), fake, cancel=flag.is_set)
    assert r["status"] == "aborted" and r["stop_reason"] == "cancelled" and len(fake.calls) == 2
    assert [c["status"] for c in r["cases"]] == ["aborted", "skipped"]


def test_run_deadline():
    t = [0.0]
    fake = FakeSDKClient(on_call=lambda i, kw: t.__setitem__(0, 100.0))
    r, _ = _run("A_pipeline", (COHORT,), fake, _limits(run_deadline_s=50), clock=lambda: t[0])
    assert r["stop_reason"] == "timeout" and len(fake.calls) == 1


def test_budget_too_small_no_request():
    r, fake = _run("A_pipeline", (COHORT,), limits=_limits(max_cost_usd=0.01))
    assert r["stop_reason"] == "cost_cap" and fake.calls == [] and r["totals"]["cost_conservative_usd"] == 0


def test_retry_blocked_by_budget():
    """재시도 중 예산 초과: 응답 없는 실패(5xx)의 보수적 과금 뒤 재시도 예약이 상한을 넘으면 보내지 않음."""
    cap = PRICE.cost(0, 16000) * 1.9  # 최악값(입력 + 출력 16000) 1회는 들어가고 2회는 넘음
    r, fake = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=_at("5xx", first=10)),
                   _limits(max_cost_usd=cap, chars_per_token=1.0))
    assert r["stop_reason"] == "cost_cap" and len(fake.calls) == 1 and r["totals"]["retries"] == 0
    assert 0 < r["totals"]["cost_conservative_usd"] <= cap


def test_concurrent_matcher_calls_respect_call_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_WORKERS", 8)
    barrier_hits = []
    fake = FakeSDKClient(on_call=lambda i, kw: barrier_hits.append(i))
    r, _ = _run("A_pipeline", (SHARED,), fake, _limits(max_calls=3))  # 프로파일러 1 + 동시 Matcher 2 이후 막힘
    assert len(fake.calls) == 3 and r["totals"]["calls"] == 3 and r["stop_reason"] == "call_cap"


def test_concurrent_reservations_respect_cost_cap():
    fake = FakeSDKClient()
    per = PRICE.cost(1000, 16000)
    m = lp.PilotMeter(_limits(max_cost_usd=per * 3.5, chars_per_token=1e9), PRICE)
    errors = []

    def work():
        try:
            llm.structured(MatcherOutput, system="s", user="<rules_to_assess>[]</rules_to_assess>")
        except lp.PilotStop:
            errors.append(1)
    with lp.metered(m, fake):  # 실행 컨텍스트는 스레드에 자동으로 넘어가지 않으므로 copy_context로 전달
        ts = [threading.Thread(target=contextvars.copy_context().run, args=(work,)) for _ in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert len(fake.calls) <= 3 and m.report()["cost_conservative_usd"] <= per * 3.5


def test_cache_usage_recorded_separately():
    r, _ = _run("A_pipeline", (COHORT,), FakeSDKClient(cache_read_ratio=1.0))
    t = r["totals"]
    assert t["cache_read_tokens"] > 0 and t["cache_write_tokens"] == 0
    assert all("cache_read" in s for s in t["by_stage"].values())


# ---------------------------------------------------------------- 비식별화·저장

def test_pii_in_llm_output_never_reaches_result(tmp_path):
    r, _ = _run("A_pipeline", (COHORT,), FakeSDKClient(faults=lambda i, st: "pii"), save_to=tmp_path / "r.json")
    blob = json.dumps(r, ensure_ascii=False)
    assert "010-1234-5678" not in blob and "@example.com" not in blob
    saved = (tmp_path / "r.json").read_text(encoding="utf-8")
    lp.assert_redacted(json.loads(saved), lp.forbidden_texts([PAIRS[COHORT]]))
    for leak in (FAKE_PII, "sk-ant-abcdefgh12345", "900101-1234567"):
        with pytest.raises(ValueError):
            lp.assert_redacted({"x": leak}, [])


def test_result_format_fields_and_no_saving_by_default():
    r, _ = _run("B_question_plan", (COHORT,))
    for k in ("run_id", "plan_hash", "experiment", "model", "status", "cases", "totals", "summary", "elapsed_s"):
        assert k in r
    c = r["cases"][0]
    for k in ("case_id", "status", "usage", "elapsed_s", "metrics", "error_type"):
        assert k in c
    for k in ("by_stage", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
              "cost_actual_usd", "cost_estimated_usd", "retries", "failures"):
        assert k in c["usage"]
    assert r["plan_hash"] == lp.plan_hash(_plan("B_question_plan", (COHORT,)))
    assert r["cost_kind"].startswith("estimate") and not (ROOT / "outputs").exists()


def test_frozen_store_and_policy_files_unchanged():
    assert store_patch.verify_frozen() == []
    # 판정·추천·Decision Loop·웹·프로파일러는 바뀌지 않음 (llm.py·question_generator.py·config.py의 실행별 격리
    # 장치는 기본 동작이 같다는 것을 동작 테스트로 확인 - test_llm_isolation.py)
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", "src/ctrec/pipeline.py",
                           "src/ctrec/agents/matcher.py", "src/ctrec/agents/patient_profiler.py",
                           "src/ctrec/agents/recommender.py", "src/ctrec/decision_loop.py",
                           "src/ctrec/decision_policy.py", "src/ctrec/decision_state.py",
                           "src/ctrec/question_priority.py", "src/ctrec/web", "data"],
                          cwd=ROOT, capture_output=True, text=True)
    assert diff.returncode == 0 and diff.stdout.strip() == ""
