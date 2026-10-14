"""파일럿 전용 프로세스 검증 (Phase 5-4): 실제 CLI를 별도 프로세스로 띄워 Fake SDK로 실행. 유료·네트워크 호출 없음.

자식 프로세스에는 가짜 API 키와 닿지 않는 로컬 주소(ANTHROPIC_BASE_URL=http://127.0.0.1:9)를 줌 - 혹시 실제 SDK가
요청을 보내도 로컬에서 바로 실패하고 외부로 나가지 않음.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import prepare_llm_pilot as plp  # noqa: E402
import web_harness as h  # noqa: E402
from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import call_guard, config, llm, llm_pilot as lp, llm_pilot_exec as lpx, store_patch  # noqa: E402
from ctrec import web_metering as wm  # noqa: E402
from ctrec.llm_pilot_fake import INTERRUPT, FakeSDKClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "run_llm_pilot.py"
KEY = "sk-ant-test-process-key-0000"
RESCUE, SHARED, BIOMARKER, COHORT, CONFIRMED, EGFR = lp.PILOT_CASE_POOL
FIRST_BATCH = [RESCUE, BIOMARKER, SHARED]
PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}
PAIRS = {gt.case_id: (c, gt) for c, gt in plp.load_pairs(PARSED, "representative_egfr")[1]}


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CTREC_", "ANTHROPIC_"))}
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8", ANTHROPIC_API_KEY=KEY,
               ANTHROPIC_BASE_URL="http://127.0.0.1:9")
    env.update(extra)
    return env


def _cli(*args, env=None, timeout=240):
    p = subprocess.run([sys.executable, str(CLI), *args], cwd=ROOT, env=env or _env(), capture_output=True,
                       timeout=timeout)
    out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    assert KEY not in out and KEY not in err  # API 키 비노출
    return p.returncode, (json.loads(out) if out.strip().startswith("{") else None), err


def _no_patient_text(obj, ids):
    lp.assert_redacted(obj, lp.forbidden_texts([PAIRS[i] for i in ids]))


# ---------------------------------------------------------------- 전용 프로세스 E2E


@pytest.mark.parametrize("exp", ["A_pipeline", "B_question_plan"])
def test_dedicated_process_fake_e2e(exp):
    code, out, _ = _cli("--experiment", exp, "--cases", *FIRST_BATCH, "--fake-sdk")
    assert code == 0
    run = out["fake_run"]
    r = run["result"]
    assert r["status"] == "succeeded" and r["client"] == "fake_sdk" and r["paid"] is False
    assert r["process_guard"] is True and r["frozen_store_intact"] is True
    assert r["totals"]["calls"] == run["fake_requests"]  # 모든 요청이 계측기를 거침
    assert 0 < r["totals"]["thinking_tokens"] <= r["totals"]["output_tokens"]  # 공식 사고 토큰 필드 기록
    proc = run["process"]
    assert proc["web_app_loaded"] is False and proc["pid"] != os.getpid()  # 웹과 다른 프로세스, 웹 코드 없음
    assert proc["guard_after"]["exclusive"] == 0 and proc["guard_after"]["sdk_hooks_installed"] is False
    assert proc["client_context_after"] is True
    assert out["readiness"]["ready_to_execute"] is False and out["plan"]["confirmation_phrase"] is None
    _no_patient_text(out, FIRST_BATCH)
    assert not (ROOT / "outputs").exists()


@pytest.mark.parametrize("args, code", [
    (["--max-cost-usd", "0.05"], "cost_cap"),
    (["--max-calls", "2"], "call_cap"),
    (["--fake-fault", "no_usage"], "usage_missing"),
    (["--fake-fault", "other_model"], "model_mismatch"),
    (["--fake-fault", "fallback_iteration"], "fallback_detected"),
    (["--fake-fault", "priority_tier"], "service_tier_mismatch"),
    (["--run-deadline-s", "0.001", "--fake-delay", "0.05"], "timeout"),
])
def test_caps_and_stops_hold_in_separate_process(args, code):
    rc, out, _ = _cli("--experiment", "A_pipeline", "--cases", BIOMARKER, SHARED, "--fake-sdk", *args)
    assert rc == 0
    r = out["fake_run"]["result"]
    assert r["status"] == "aborted" and r["stop_reason"] == code
    assert r["cases"][-1]["status"] in ("aborted", "skipped")
    t = r["totals"]
    assert t["calls"] <= t["limits"]["max_calls"] and t["cost_conservative_usd"] <= t["limits"]["max_cost_usd"]
    assert out["fake_run"]["process"]["guard_after"]["exclusive"] == 0


def test_full_approval_in_separate_process_still_refused():
    price = ["--price-source", "test: official pricing page", "--price-confirmed"]
    env = _env(CTREC_ALLOW_PAID_EVAL="1", CTREC_FALLBACKS="0")
    _, plan_out, _ = _cli("--experiment", "A_pipeline", "--cases", *FIRST_BATCH, *price, env=env)
    phrase = plan_out["plan"]["confirmation_phrase"]
    assert phrase and phrase.startswith("RUN A_pipeline ")  # 단가 확인 뒤에만 확인 문구
    rc, out, _ = _cli("--experiment", "A_pipeline", "--cases", *FIRST_BATCH, *price, "--approved-by", "test",
                      "--confirm", phrase, "--execute", env=env)
    assert rc == 0 and out["gate_problems"] == [] and "PAID_EXECUTION_ENABLED" in out["refused"]
    assert out["readiness"]["ready_to_execute"] is False
    assert out["readiness"]["reasons"] == ["PAID_EXECUTION_ENABLED = False"]
    assert "result" not in out


def test_unconfirmed_documented_price_is_not_final():
    doc = lp.documented_price("claude-sonnet-5")
    assert doc is not None and doc.confirmed is False and (doc.input, doc.output) == (2.0, 10.0)
    assert (doc.cache_write, doc.cache_read) == (2.50, 0.20)
    rc, out, _ = _cli("--experiment", "B_question_plan", "--cases", RESCUE)
    assert out["plan"]["price"]["confirmed"] is False and out["plan"]["confirmation_phrase"] is None
    assert any("단가" in r for r in out["readiness"]["reasons"])
    assert set(out["readiness"]["unknown_items"]) >= {"failed_request_billing", "sonnet5_refusal_classifier"}


# ---------------------------------------------------------------- 중단·강제 종료


def test_interrupt_restores_and_saves_nothing(tmp_path):
    target = tmp_path / "result.json"
    rc, out, _ = _cli("--experiment", "A_pipeline", "--cases", BIOMARKER, "--fake-sdk", "--fake-fault", INTERRUPT,
                      "--fake-fault-at", "1", "--save-results", str(target))
    assert rc == 130 and out["interrupted"] is True
    assert out["process"]["guard_after"]["exclusive"] == 0 and out["process"]["client_context_after"] is True
    assert not target.exists() and list(tmp_path.iterdir()) == []


def test_forced_kill_leaves_no_partial_result(tmp_path):
    target = tmp_path / "result.json"
    p = subprocess.Popen([sys.executable, str(CLI), "--experiment", "A_pipeline", "--cases", *FIRST_BATCH,
                          "--fake-sdk", "--fake-delay", "1.0", "--save-results", str(target)],
                         cwd=ROOT, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(8)  # 데이터 적재 뒤 요청 도중 (요청마다 1초, 16회 이상)
    assert p.poll() is None
    p.kill()  # 강제 종료 (정리 코드가 돌 기회 없음)
    p.communicate(timeout=30)
    assert p.returncode != 0 and not target.exists() and list(tmp_path.iterdir()) == []


def test_saved_result_written_atomically_and_redacted(tmp_path):
    target = tmp_path / "result.json"
    rc, out, _ = _cli("--experiment", "B_question_plan", "--cases", RESCUE, "--fake-sdk", "--save-results", str(target))
    assert rc == 0 and [x.name for x in tmp_path.iterdir()] == ["result.json"]
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["run_id"] == out["fake_run"]["result"]["run_id"]
    _no_patient_text(saved, [RESCUE])


def test_in_process_interrupt_restores_guard_and_context():
    plan = {"experiment": "A_pipeline", "case_ids": [COHORT], "model": config.MODEL,
            "price": asdict(lp.ASSUMED_PRICE), "rounds": 1, "question_budget": 3,
            "limits": {}, "save_patient_data": False, "redact_outputs": True}
    lim = lp.PilotLimits(max_cost_usd=100, max_calls=100, max_input_tokens=10 ** 9, max_output_tokens=10 ** 9)
    with pytest.raises(KeyboardInterrupt):
        lpx.execute(plan, lp.ASSUMED_PRICE, lim, FakeSDKClient(faults=lambda i, s: INTERRUPT), [PAIRS[COHORT]],
                    TRIALS, sleep=lambda s: None)
    c = call_guard.counters()
    assert c["exclusive"] == 0 and llm._client_var.get() is None and config.active_overrides() == {}


# ---------------------------------------------------------------- 웹과 분리


def test_pilot_process_does_not_affect_concurrent_web(monkeypatch, tmp_path):
    from ctrec.web import app as web
    pilot = subprocess.Popen([sys.executable, str(CLI), "--experiment", "A_pipeline", "--cases", *FIRST_BATCH,
                              "--fake-sdk", "--fake-delay", "0.2"],
                             cwd=ROOT, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        sdk = h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
        exclusive_seen = []
        stop = threading.Event()

        def sample():  # 웹 프로세스(이 프로세스)에서는 파일럿 보호가 한 번도 켜지지 않음
            while not stop.is_set():
                exclusive_seen.append(call_guard.counters()["exclusive"])
                time.sleep(0.05)
        t = threading.Thread(target=sample)
        t.start()
        c = h.browser()
        rid = c.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS,
                                        "screen": False}).json()["run_id"]
        assert pilot.poll() is None  # 웹 실행을 시작할 때 파일럿 프로세스가 돌고 있음 (겹침)
        run = web.RUNS[rid]
        q = h.wait_for(run, "questions")
        c.post(f"/api/runs/{rid}/answers", json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}})
        h.wait_for(run, "end", 30)
        stop.set()
        t.join()
        assert run.state == "completed" and set(exclusive_seen) == {0}
        assert c.get(f"/api/runs/{rid}/metering").json()["status"] == wm.FULLY_METERED and sdk.requests
    finally:
        out, _ = pilot.communicate(timeout=240)
    result = json.loads(out.decode("utf-8"))["fake_run"]["result"]
    assert result["status"] == "succeeded" and result["process_guard"] is True


def test_frozen_store_intact():
    assert store_patch.verify_frozen() == []


def test_switch_still_off():
    assert lp.PAID_EXECUTION_ENABLED is False
    assert replace(lp.documented_price(), confirmed=True).confirmed  # 확인은 사용자 행위 (코드 기본값은 False)
