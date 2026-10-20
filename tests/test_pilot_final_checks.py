"""실제 LLM 파일럿 실행 직전 최종 점검 (Phase 5-4 마감): 모델 고정, 계획 해시·승인 조건, 유료 실행 차단 매트릭스.

유료·네트워크 호출 없음. 별도 프로세스 실행은 ANTHROPIC_BASE_URL을 로컬 수신 소켓으로 돌려, 요청이 하나라도 나가면
연결 수로 드러나게 함 (실제 키 값은 쓰지 않음).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_llm_pilot as rlp  # noqa: E402

from ctrec import config, llm, llm_pilot as lp  # noqa: E402
from ctrec.llm_pilot_fake import FakeSDKClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "run_llm_pilot.py"
KEY = "sk-ant-test-final-check-0000"
RESCUE, SHARED, BIOMARKER = lp.PILOT_CASE_POOL[0], lp.PILOT_CASE_POOL[1], lp.PILOT_CASE_POOL[2]
FIRST_BATCH = [RESCUE, BIOMARKER, SHARED]
PRICE_ARGS = ["--price-source", "test: official pricing page", "--price-confirmed"]
LIMIT_ARGS = ["--max-cost-usd", "3", "--max-calls", "72", "--run-deadline-s", "1800"]


class Sink:
    """로컬 수신 소켓: ANTHROPIC_BASE_URL을 여기로 돌리면 SDK 요청이 연결 수로 드러남 (외부로 나가지 않음)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.connections = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def _loop(self):
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
                self.connections += 1
                c.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        self._t.join(2)
        self.sock.close()


@pytest.fixture
def sink():
    s = Sink()
    yield s
    s.close()


@pytest.fixture
def no_sdk_client(monkeypatch):
    import anthropic
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: made.append(1) or pytest.fail("API 클라이언트 생성"))
    monkeypatch.setattr(llm, "_client", None)
    return made


# ---------------------------------------------------------------- 모델 고정


def test_model_pinned_everywhere():
    assert lp.PILOT_MODEL == "claude-sonnet-5" == config.MODEL
    src = (ROOT / "src/ctrec/config.py").read_text(encoding="utf-8")
    assert 'MODEL = os.getenv("CTREC_MODEL", "claude-sonnet-5")' in src
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert re.search(r"key: CTREC_MODEL\s+value: claude-sonnet-5\s", render)
    assert "CTREC_MODEL=claude-sonnet-5" in (ROOT / ".env.example").read_text(encoding="utf-8")
    assert lp.documented_price().model == "claude-sonnet-5" and lp.ASSUMED_PRICE.model == "claude-sonnet-5"


def test_request_and_response_model_checks():
    from ctrec import llm_pilot_exec as lpx
    import prepare_llm_pilot as plp
    from bench_incremental import load_frozen
    from decision_scenarios import load_trials
    parsed = load_frozen()
    trials = {t: v for t, v in load_trials().items() if t in parsed}
    case = [p for p in plp.load_pairs(parsed, "representative")[1] if p[1].case_id == BIOMARKER]
    plan = {"experiment": "A_pipeline", "case_ids": [BIOMARKER], "model": config.MODEL, "rounds": 1, "question_budget": 3}
    r = lpx.simulate(plan, case, trials)  # 실행 경로 전체에서 응답 모델 검증을 통과 (불일치면 중단)
    assert r["model"] == "claude-sonnet-5" and r["status"] == "succeeded" and r["stop_reason"] is None
    fake = FakeSDKClient()
    m = lp.PilotMeter(lp.PilotLimits(max_cost_usd=10, max_calls=10, max_input_tokens=10 ** 9,
                                     max_output_tokens=10 ** 9), lp.ASSUMED_PRICE)
    with config.overrides(USE_FALLBACKS=False), lp.metered(m, fake):
        from ctrec.agents import matcher
        from ctrec.schemas import MatcherOutput
        llm.structured(MatcherOutput, system=matcher.SYSTEM, user="<rules_to_assess>[]</rules_to_assess>")
    assert [c["model"] for c in fake.calls] == ["claude-sonnet-5"]  # SDK로 보낸 모델 ID
    # 응답 모델 검증: 같은 이름(또는 날짜 스냅샷)만, 후속·다른 모델은 거부
    assert lp.same_model("claude-sonnet-5", "claude-sonnet-5")
    for other in ("claude-sonnet-5-5", "claude-sonnet-4-6", "claude-opus-5-5", None, ""):
        assert not lp.same_model("claude-sonnet-5", other)


def test_gate_rejects_non_pinned_model_even_if_env_changes(monkeypatch):
    monkeypatch.setenv("CTREC_ALLOW_PAID_EVAL", "1")
    monkeypatch.setenv("CTREC_FALLBACKS", "0")
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5-5")  # CTREC_MODEL을 바꾼 환경
    price = lp.PriceTable(model="claude-sonnet-5-5", input=2, output=10, cache_write=2.5, cache_read=0.2,
                          confirmed=True, source="test")
    plan, approval = _plan_and_approval(price=price, model="claude-sonnet-5-5")
    problems = lp.approval_problems(plan, approval)
    assert problems == ["파일럿 고정 모델(claude-sonnet-5)이 아님"]
    with pytest.raises(lp.PaidRunNotApproved):
        lp.authorize(plan, approval)


# ---------------------------------------------------------------- 계획 해시·승인 조건


def _limits(**kw):
    base = dict(max_cost_usd=3.0, max_calls=72, max_input_tokens=981000, max_output_tokens=241000,
                max_retries_per_call=2, max_total_retries=5, max_consecutive_failures=3, call_timeout_s=180.0,
                run_deadline_s=1800.0)
    base.update(kw)
    return lp.PilotLimits(**base)


CONFIRMED = lp.PriceTable(model="claude-sonnet-5", input=2.0, output=10.0, cache_write=2.5, cache_read=0.2,
                          confirmed=True, source="test: official pricing page")


def _plan_and_approval(price=CONFIRMED, model="claude-sonnet-5", limits=None):
    plan = {"experiment": "A_pipeline", "case_ids": list(FIRST_BATCH), "model": model, "price": asdict(price),
            "rounds": 1, "question_budget": 3, "limits": asdict(limits or _limits()), "save_patient_data": False,
            "redact_outputs": True}
    h = lp.plan_hash(plan)
    return plan, lp.PilotApproval("user", "A_pipeline", h, price, limits or _limits(), f"RUN A_pipeline {h[:12]}")


CHANGES = {
    "model": lambda p: {**p, "model": "claude-sonnet-5-5"},
    "case_ids": lambda p: {**p, "case_ids": [RESCUE, BIOMARKER]},
    "case_order": lambda p: {**p, "case_ids": [BIOMARKER, RESCUE, SHARED]},
    "experiment": lambda p: {**p, "experiment": "B_question_plan"},
    "price_input": lambda p: {**p, "price": {**p["price"], "input": 3.0}},
    "price_output": lambda p: {**p, "price": {**p["price"], "output": 15.0}},
    "price_cache_write": lambda p: {**p, "price": {**p["price"], "cache_write": 4.0}},
    "price_cache_read": lambda p: {**p, "price": {**p["price"], "cache_read": 0.3}},
    "price_source": lambda p: {**p, "price": {**p["price"], "source": "other"}},
    "max_cost": lambda p: {**p, "limits": {**p["limits"], "max_cost_usd": 4.0}},
    "max_calls": lambda p: {**p, "limits": {**p["limits"], "max_calls": 80}},
    "input_tokens": lambda p: {**p, "limits": {**p["limits"], "max_input_tokens": 2_000_000}},
    "output_tokens": lambda p: {**p, "limits": {**p["limits"], "max_output_tokens": 300_000}},
    "run_deadline": lambda p: {**p, "limits": {**p["limits"], "run_deadline_s": 3600.0}},
    "call_timeout": lambda p: {**p, "limits": {**p["limits"], "call_timeout_s": 600.0}},
    "retries_per_call": lambda p: {**p, "limits": {**p["limits"], "max_retries_per_call": 3}},
    "total_retries": lambda p: {**p, "limits": {**p["limits"], "max_total_retries": 9}},
    "consecutive_failures": lambda p: {**p, "limits": {**p["limits"], "max_consecutive_failures": 9}},
    "max_tokens_per_call": lambda p: {**p, "limits": {**p["limits"], "max_tokens_per_call": 64000}},
    "rounds": lambda p: {**p, "rounds": 2},
    "question_budget": lambda p: {**p, "question_budget": 5},
    "save_patient_data": lambda p: {**p, "save_patient_data": True},
}


@pytest.mark.parametrize("name", sorted(CHANGES))
def test_any_plan_change_invalidates_previous_approval(monkeypatch, name):
    monkeypatch.setenv("CTREC_ALLOW_PAID_EVAL", "1")
    monkeypatch.setenv("CTREC_FALLBACKS", "0")
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    plan, approval = _plan_and_approval()
    assert lp.approval_problems(plan, approval) == []
    changed = CHANGES[name](plan)
    assert lp.plan_hash(changed) != lp.plan_hash(plan)
    problems = lp.approval_problems(changed, approval)  # 이전 승인·확인 문구 재사용
    assert any("계획 해시 불일치" in p for p in problems) and any("확인 문구" in p for p in problems)


@pytest.mark.parametrize("field, value", [
    ("run_deadline_s", 7200.0), ("call_timeout_s", 900.0), ("max_consecutive_failures", 50),
    ("max_tokens_per_call", 64000), ("chars_per_token", 4.0), ("max_cost_usd", 1.0),
])
def test_approval_limits_must_equal_planned_limits(monkeypatch, field, value):
    """실행은 승인의 상한으로 돌므로, 승인 상한이 계획(해시) 상한과 하나라도 다르면 거절 (느슨한 시간 제한 등)."""
    monkeypatch.setenv("CTREC_ALLOW_PAID_EVAL", "1")
    monkeypatch.setenv("CTREC_FALLBACKS", "0")
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    plan, approval = _plan_and_approval()
    looser = replace(approval, limits=replace(approval.limits, **{field: value}))
    assert any(f"상한 {field}가 계획과 다름" in p for p in lp.approval_problems(plan, looser))


# ---------------------------------------------------------------- 유료 실행 차단 매트릭스 (같은 프로세스·CLI 직접)


MATRIX = {
    "no_key": ({}, False),
    "key": ({"ANTHROPIC_API_KEY": KEY}, False),
    "allow_flag": ({"ANTHROPIC_API_KEY": KEY, "CTREC_ALLOW_PAID_EVAL": "1"}, False),
    "fallbacks_off": ({"ANTHROPIC_API_KEY": KEY, "CTREC_FALLBACKS": "0"}, False),
    "all_12_conditions": ({"ANTHROPIC_API_KEY": KEY, "CTREC_ALLOW_PAID_EVAL": "1", "CTREC_FALLBACKS": "0"}, True),
}


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_paid_block_matrix_direct_cli(monkeypatch, capsys, no_sdk_client, sink, name):
    env, approve = MATRIX[name]
    for k in ("ANTHROPIC_API_KEY", "CTREC_ALLOW_PAID_EVAL", "CTREC_FALLBACKS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", sink.url)
    monkeypatch.setattr(config, "USE_FALLBACKS", env.get("CTREC_FALLBACKS", "1") == "1")
    args = ["--experiment", "A_pipeline", "--cases", *FIRST_BATCH, *PRICE_ARGS, *LIMIT_ARGS]
    extra = []
    if approve:  # 계획 해시와 확인 문구가 일치하는 승인
        phrase = rlp.main(args)["plan"]["confirmation_phrase"]
        extra = ["--approved-by", "test", "--confirm", phrase]
    out = rlp.main(args + extra + ["--execute"])
    assert "refused" in out and "result" not in out and out["readiness"]["ready_to_execute"] is False
    if approve:
        assert out["gate_problems"] == [] and out["readiness"]["reasons"] == ["PAID_EXECUTION_ENABLED = False"]
    assert no_sdk_client == [] and sink.connections == 0
    assert KEY not in capsys.readouterr().out


# ---------------------------------------------------------------- 별도 OS 프로세스


def _proc_env(sink_url, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CTREC_", "ANTHROPIC_"))}
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8", ANTHROPIC_API_KEY=KEY,
               ANTHROPIC_BASE_URL=sink_url, **extra)
    return env


def _run(args, env):
    p = subprocess.run([sys.executable, str(CLI), *args], cwd=ROOT, env=env, capture_output=True, timeout=300)
    out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    assert KEY not in out and KEY not in err
    return p.returncode, json.loads(out)


def test_separate_process_full_approval_zero_requests(sink):
    env = _proc_env(sink.url, CTREC_ALLOW_PAID_EVAL="1", CTREC_FALLBACKS="0")
    args = ["--experiment", "A_pipeline", "--cases", *FIRST_BATCH, *PRICE_ARGS, *LIMIT_ARGS]
    _, plan_out = _run(args, env)
    plan = plan_out["plan"]
    assert plan["model"] == "claude-sonnet-5" and plan["case_ids"] == FIRST_BATCH
    assert (plan["limits"]["max_cost_usd"], plan["limits"]["max_calls"], plan["limits"]["run_deadline_s"],
            plan["limits"]["call_timeout_s"]) == (3.0, 72, 1800.0, 180.0)
    rc, out = _run(args + ["--approved-by", "test", "--confirm", plan["confirmation_phrase"], "--execute"], env)
    assert rc == 0 and out["gate_problems"] == [] and "PAID_EXECUTION_ENABLED" in out["refused"]
    assert out["plan"]["plan_hash"] == plan["plan_hash"]  # 같은 인자 -> 같은 해시 (재현 가능)
    assert sink.connections == 0  # 실제 SDK 요청 0회


def test_separate_process_fake_run_zero_requests(sink):
    rc, out = _run(["--experiment", "A_pipeline", "--cases", *FIRST_BATCH, *LIMIT_ARGS, "--fake-sdk"],
                   _proc_env(sink.url))
    r = out["fake_run"]["result"]
    assert rc == 0 and r["status"] == "succeeded" and r["paid"] is False and r["process_guard"] is True
    assert r["totals"]["calls"] == out["fake_run"]["fake_requests"] and sink.connections == 0


def test_switch_constant_unchanged():
    src = (ROOT / "src/ctrec/llm_pilot.py").read_text(encoding="utf-8")
    assert re.search(r"^PAID_EXECUTION_ENABLED = False$", src, re.M) and lp.PAID_EXECUTION_ENABLED is False


def test_every_pool_case_is_synthetic():
    """승인 후보 사례 6개(대표 5 + eGFR) 모두 합성 데이터 (실제 환자 데이터 미사용)."""
    import prepare_llm_pilot as plp
    found = {}
    for path in plp.PATHS.values():
        for r in json.loads(path.read_text(encoding="utf-8")):
            if r["patient_id"] in lp.PILOT_CASE_POOL:
                found[r["patient_id"]] = r.get("synthetic")
    assert set(found) == set(lp.PILOT_CASE_POOL) and all(v is True for v in found.values())
