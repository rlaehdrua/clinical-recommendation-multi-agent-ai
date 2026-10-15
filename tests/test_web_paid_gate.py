"""웹 실제 LLM(유료) 호출 허가 게이트 (Phase 8). 네트워크 없음: 실제 SDK 객체는 가짜 HTTP(MockTransport)로만.

기본은 꺼짐: API 키만 있어도 실행·스레드·SDK 클라이언트를 만들지 않고 503. 허가하려면 CTREC_WEB_PAID_ENABLED=1 +
enforce + 비용 설정·원장 + 비밀번호·HTTPS 설정 + API 키가 모두 필요. 테스트 대역은 하네스가 명시할 때만.
"""

from __future__ import annotations

import base64
import contextvars
import json
import logging
import sys
from pathlib import Path

import anthropic
import anthropic._client
import httpx2
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402

from ctrec import config, llm, llm_pilot as lp  # noqa: E402
from ctrec.web import app as web, budget, paid_gate  # noqa: E402

REAL_SDK = anthropic._client.Anthropic
KEY = "sk-ant-test-paid-gate-key-0000"
PASSWORD = "correct-horse-battery"
ENFORCE = {
    "CTREC_WEB_RUN_MAX_USD": "10", "CTREC_WEB_RUN_MAX_CALLS": "50", "CTREC_WEB_RUN_MAX_INPUT_TOKENS": "5000000",
    "CTREC_WEB_RUN_MAX_OUTPUT_TOKENS": "2000000", "CTREC_WEB_SHARED_MAX_USD": "100", "CTREC_WEB_SHARED_MAX_CALLS": "500",
    "CTREC_WEB_SHARED_MAX_INPUT_TOKENS": "50000000", "CTREC_WEB_SHARED_MAX_OUTPUT_TOKENS": "20000000",
    "CTREC_WEB_PRICE_INPUT": "2", "CTREC_WEB_PRICE_OUTPUT": "10", "CTREC_WEB_PRICE_CACHE_WRITE": "2.5",
    "CTREC_WEB_PRICE_CACHE_READ": "0.2", "CTREC_WEB_PRICE_CONFIRMED": "1", "CTREC_WEB_PRICE_SOURCE": "test",
    "CTREC_WEB_PRICE_MODEL": config.MODEL, "CTREC_WEB_BUDGET_LEDGER_PERSISTENT": "1",
    "CTREC_WEB_BUDGET_SINGLE_INSTANCE": "1", "CTREC_WEB_BUDGET_PERIOD": "day", "CTREC_WEB_METERING": "enforce",
    "CTREC_WEB_PAID_ENABLED": "1", "ANTHROPIC_API_KEY": KEY}
BODY = {"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}


@pytest.fixture(autouse=True)
def real_backend(monkeypatch, tmp_path):
    """하네스 없이 '실제' 백엔드 상태 (TEST_LLM_BACKEND=None). 실제 SDK 클라이언트 생성은 기록하고 실패시킴."""
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: made.append(1) or (_ for _ in ()).throw(
        RuntimeError("실제 SDK 클라이언트 생성 시도")))
    monkeypatch.setattr(web, "TEST_LLM_BACKEND", None)
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web, "PASSWORD", "")
    for k in list(ENFORCE) + ["CTREC_PASSWORD", "CTREC_ALLOW_INSECURE_HTTP", "CTREC_WEB_BUDGET_LEDGER", "CTREC_REQUIRE_HTTPS",
                              "CTREC_COOKIE_SECURE", "CTREC_WEB_SAVE_OUTPUTS"]:
        monkeypatch.delenv(k, raising=False)
    web.RUNS.clear()
    web.CLIENT_STARTS.clear()
    budget.reset_ledgers()
    yield made
    budget.reset_ledgers()
    assert llm._client is None or not isinstance(llm._client, REAL_SDK) or getattr(llm._client, "_mock", False)


def _env(monkeypatch, tmp_path, init_ledger=True, **over):
    env = {**ENFORCE, "CTREC_WEB_BUDGET_LEDGER": str(tmp_path / "ledger.db"), **over}
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    if init_ledger:
        budget.Ledger(str(tmp_path / "ledger.db"), budget.Limits(1, 1, 1, 1), "day").open(create=True)
    monkeypatch.setattr(web, "PASSWORD", PASSWORD)
    return env


def _client(password=PASSWORD):
    c = h.browser()
    if password:
        c.headers["Authorization"] = "Basic " + base64.b64encode(f"u:{password}".encode()).decode()
    return c


def _start(c):
    return c.post("/api/runs", json=BODY)


# ---------------------------------------------------------------- 1. 기본 꺼짐: API 키만으로는 호출 불가


def test_api_key_alone_cannot_start_analysis(monkeypatch, real_backend, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    caplog.set_level(logging.WARNING, logger="ctrec.web")
    c = _client(password=None)
    assert c.get("/api/config").json()["analysis"] == {"enabled": False, "backend": "real"}
    r = _start(c)
    assert r.status_code == 503 and "실제 AI 분석이 꺼져" in r.json()["detail"] and "외부로 보내지 않았습니다" in r.json()["detail"]
    assert web.RUNS == {} and real_backend == [] and llm._client is None  # 실행·클라이언트 없음
    assert "web_paid_disabled" in caplog.text and KEY not in caplog.text and KEY not in r.text


@pytest.mark.parametrize("over, code", [
    ({"CTREC_WEB_METERING": "record"}, "metering_not_enforce"),
    ({"CTREC_WEB_METERING": None}, "metering_not_enforce"),
    ({"CTREC_WEB_PRICE_CONFIRMED": "0"}, "unconfirmed:CTREC_WEB_PRICE"),
    ({"CTREC_WEB_PRICE_MODEL": "claude-other"}, "price_model_mismatch:CTREC_WEB_PRICE_MODEL"),
    ({"CTREC_WEB_RUN_MAX_USD": None}, "missing:CTREC_WEB_RUN_MAX_USD"),
    ({"CTREC_WEB_SHARED_MAX_CALLS": None}, "missing:CTREC_WEB_SHARED_MAX_CALLS"),
    ({"CTREC_WEB_BUDGET_SINGLE_INSTANCE": None}, "unconfirmed:CTREC_WEB_BUDGET_SINGLE_INSTANCE"),
    ({"CTREC_ALLOW_INSECURE_HTTP": "1"}, "insecure_http_allowed"),
    ({"ANTHROPIC_API_KEY": None}, "api_key_missing"),
])
def test_each_missing_condition_refuses(monkeypatch, tmp_path, real_backend, caplog, over, code):
    _env(monkeypatch, tmp_path, **over)
    caplog.set_level(logging.WARNING, logger="ctrec.web")
    assert code in paid_gate.problems(password=PASSWORD)
    r = _start(_client())
    assert r.status_code == 503 and "서버 설정" in r.json()["detail"] and code not in r.text  # 세부 사유는 로그에만
    assert code in caplog.text and KEY not in caplog.text and PASSWORD not in caplog.text
    assert web.RUNS == {} and real_backend == []


def test_fallback_and_ledger_and_password_conditions(monkeypatch, tmp_path, real_backend):
    _env(monkeypatch, tmp_path, init_ledger=False)
    assert "ledger_missing" in paid_gate.problems(password=PASSWORD)  # enforce + 허가 + 원장 없음
    budget.Ledger(str(tmp_path / "ledger.db"), budget.Limits(1, 1, 1, 1), "day").open(create=True)
    budget.reset_ledgers()
    assert paid_gate.problems(password=PASSWORD) == []
    assert "password_missing_or_short" in paid_gate.problems(password="short")
    monkeypatch.setattr(config, "USE_FALLBACKS", True)
    assert "fallbacks_enabled:CTREC_FALLBACKS" in paid_gate.problems(password=PASSWORD)


def test_enforce_ready_but_switch_off_is_blocked(monkeypatch, tmp_path, real_backend):
    _env(monkeypatch, tmp_path, CTREC_WEB_PAID_ENABLED="0")
    assert paid_gate.problems(password=PASSWORD) == ["web_paid_disabled"]
    r = _start(_client())
    assert r.status_code == 503 and "꺼져" in r.json()["detail"] and web.RUNS == {} and real_backend == []


def test_record_mode_with_switch_on_is_refused(monkeypatch, tmp_path, real_backend):
    _env(monkeypatch, tmp_path, CTREC_WEB_METERING="record")
    assert _start(_client()).status_code == 503 and real_backend == []


# ---------------------------------------------------------------- 2. 실행 중 다시 확인 (클라이언트 생성 전)


def test_execute_rechecks_before_any_client(monkeypatch, tmp_path, real_backend):
    """시작 뒤 설정이 바뀐 경우: 실행 스레드가 클라이언트·예산 준비 전에 멈추고, 완료·추천으로 보이지 않음."""
    run = web.Run(run_id="r-recheck", owner_digest="x", client_digest="y")
    case = web.PatientCase(patient_id="P", raw_text=h.SYN_TEXT)
    trials = {t: web._load_trials()[t] for t in h.TRIAL_IDS}
    # 서버와 같이 복사한 컨텍스트에서 실행 (실행별 취소 확인 설정이 테스트 프로세스에 남지 않게)
    contextvars.copy_context().run(web._execute, run, web.RunRequest(**BODY), case, trials)
    kinds = [e["type"] for e in run.events]
    assert run.state == "analysis_disabled" and "result" not in kinds and kinds[-1] == "end"
    assert run.events[-1]["state"] == "analysis_disabled" and real_backend == [] and llm._client is None
    assert any("외부로 보내지 않았습니다" in (e.get("message") or "") for e in run.events)


# ---------------------------------------------------------------- 3. 모든 조건 충족: enforce 예산 경로로만 호출


def _mock_sdk(status=400):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content).get("model"))
        return httpx2.Response(status, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}})
    sdk = REAL_SDK(api_key=KEY, base_url="http://mock.invalid", max_retries=4,
                   http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    sdk._mock = True
    return sdk, sent


def test_all_conditions_met_calls_go_through_budget_only(monkeypatch, tmp_path, real_backend):
    _env(monkeypatch, tmp_path)
    sdk, sent = _mock_sdk()
    monkeypatch.setattr(llm, "_client", sdk)  # 실제 SDK 객체 (가짜 HTTP)
    c = _client()
    assert c.get("/api/config").json()["analysis"] == {"enabled": True, "backend": "real"}
    r = _start(c)
    assert r.status_code == 200
    run = web.RUNS[r.json()["run_id"]]
    h.wait_for(run, "end", 30)
    assert sent and set(sent) == {config.MODEL}  # 가짜 HTTP까지만 도달, SDK 자동 재시도 없음(400은 재시도 안 함)
    rows = budget.Ledger(str(tmp_path / "ledger.db"), budget.Limits(1, 1, 1, 1), "day").rows()
    assert len(rows) == len(sent) and {r_["status"] for r_ in rows} == {"released"}  # 모든 요청이 예약·해제됨
    assert run.state != "completed" and "result" not in [e["type"] for e in run.events]
    m = c.get(f"/api/runs/{run.run_id}/metering").json()
    assert m["budget_mode"] == "enforce" and m["cap_enforced"] is True


def test_client_creation_failure_before_any_request(monkeypatch, tmp_path, real_backend):
    """허가 조건은 충족했지만 SDK 클라이언트를 만들 수 없음 -> 요청 0, 완료로 보이지 않음."""
    _env(monkeypatch, tmp_path)
    r = _start(_client())
    run = web.RUNS[r.json()["run_id"]]
    h.wait_for(run, "end", 30)
    assert real_backend == [1] and run.state == budget.BUDGET_ERROR and "result" not in [e["type"] for e in run.events]
    assert budget.Ledger(str(tmp_path / "ledger.db"), budget.Limits(1, 1, 1, 1), "day").rows() == []


# ---------------------------------------------------------------- 4. 대역·파일럿 경로는 그대로


def test_test_double_requires_explicit_harness_and_is_labelled(monkeypatch, tmp_path, real_backend):
    for v in ("fake", "1", "true"):
        monkeypatch.setenv("CTREC_TEST_LLM_BACKEND", v)  # 환경 변수로는 대역을 켤 수 없음
        assert web._llm_backend() == "real"
    h.install(monkeypatch, tmp_path)
    c = h.browser()
    assert c.get("/api/config").json()["analysis"] == {"enabled": True, "backend": "test_double"}
    assert _start(c).status_code == 200


def test_pilot_gate_unchanged():
    assert lp.PAID_EXECUTION_ENABLED is False and len(lp.PILOT_CASE_POOL) == 6
    with pytest.raises(lp.PaidRunNotApproved):
        lp.authorize({"experiment": "A_pipeline"}, None)
