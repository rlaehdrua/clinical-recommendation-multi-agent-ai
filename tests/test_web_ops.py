"""웹 운영 준비 (Phase 6-4, 6-5): 배포 사전 점검, 원장 장애·재시작·백업·복구, 탭 복귀. 네트워크·유료 호출 없음.

원장 장애는 임시 원장과 Fake SDK로만 흉내 냅니다. 다른 프로세스는 subprocess로 실제로 띄워 OS 잠금을 확인합니다.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import anthropic
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT / "scripts"))

import check_web_deployment as chk  # noqa: E402
import web_budget_ledger as cli  # noqa: E402
import web_harness as h  # noqa: E402

from ctrec import config, llm  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.llm_pilot_fake import FakeSDKClient  # noqa: E402
from ctrec.schemas import MatcherOutput  # noqa: E402
from ctrec.web import app as web, budget  # noqa: E402

PRICE = budget.Price(config.MODEL, 2.0, 10.0, 2.5, 0.2, "test")
BIG = budget.Limits(1000.0, 10_000, 10**9, 10**9)
ENV_ENFORCE = {
    "CTREC_WEB_RUN_MAX_USD": "10", "CTREC_WEB_RUN_MAX_CALLS": "50", "CTREC_WEB_RUN_MAX_INPUT_TOKENS": "5000000",
    "CTREC_WEB_RUN_MAX_OUTPUT_TOKENS": "2000000", "CTREC_WEB_SHARED_MAX_USD": "100", "CTREC_WEB_SHARED_MAX_CALLS": "500",
    "CTREC_WEB_SHARED_MAX_INPUT_TOKENS": "50000000", "CTREC_WEB_SHARED_MAX_OUTPUT_TOKENS": "20000000",
    "CTREC_WEB_PRICE_INPUT": "2", "CTREC_WEB_PRICE_OUTPUT": "10", "CTREC_WEB_PRICE_CACHE_WRITE": "2.5",
    "CTREC_WEB_PRICE_CACHE_READ": "0.2", "CTREC_WEB_PRICE_CONFIRMED": "1", "CTREC_WEB_PRICE_SOURCE": "test",
    "CTREC_WEB_PRICE_MODEL": config.MODEL, "CTREC_WEB_BUDGET_LEDGER_PERSISTENT": "1",
    "CTREC_WEB_BUDGET_SINGLE_INSTANCE": "1", "CTREC_WEB_BUDGET_PERIOD": "day"}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: pytest.fail("실제 API 클라이언트 생성"))
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    budget.reset_ledgers()
    yield
    budget.reset_ledgers()


def _cfg(tmp_path, shared=BIG, price=PRICE, name="ledger.db"):
    return budget.BudgetConfig(run=BIG, shared=shared, period="day", price=price, ledger_path=str(tmp_path / name),
                               run_max_seconds=3600)


def _init(path):
    return budget.Ledger(str(path), BIG, "day").open(create=True)


def _call():
    return llm.structured(MatcherOutput, system=matcher.SYSTEM, user="<rules_to_assess>[]</rules_to_assess>")


def _run(cfg, inner, led, n=1):
    with budget.run_context("run-1", cfg, led, inner) as b:
        for _ in range(n):
            _call()
    return b


def _child(code: str, *args: str) -> subprocess.Popen:
    """다른 프로세스: 원장을 열고 'ready'를 출력한 뒤 표준 입력을 기다림 (실행 중인 서버 흉내)."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    env.pop("ANTHROPIC_API_KEY", None)
    p = subprocess.Popen([sys.executable, "-c", textwrap.dedent(code), *args], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    line = p.stdout.readline().strip()
    assert line == "ready", (line, p.stderr.read() if p.poll() is not None else "")
    return p


HOLD = """
import sys
from ctrec.web import budget
led = budget.Ledger(sys.argv[1], budget.Limits(1000, 10000, 10**9, 10**9), "day").open()
print("ready", flush=True)
sys.stdin.read()
"""


# ================================================================ 원장 장애 시나리오 (6-5)


def test_missing_or_uninitialized_ledger_refused(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(budget.LedgerError, match="ledger_missing"):
        budget.get_ledger(cfg)  # 서버는 원장을 새로 만들지 않음 (디스크가 빠져 빈 원장이 생기는 것 방지)
    assert not os.path.exists(cfg.ledger_path)
    Path(cfg.ledger_path).write_bytes(b"")  # 빈 파일 (예: 미리 만든 자리)
    with pytest.raises(budget.LedgerError, match="ledger_missing"):
        budget.Ledger(cfg.ledger_path, BIG, "day").open()
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as c:
        c.execute("CREATE TABLE x (a)")
    with pytest.raises(budget.LedgerError, match="ledger_inconsistent"):
        budget.Ledger(str(other), BIG, "day").open()


def test_ledger_deleted_while_running_blocks_without_recreating(tmp_path):
    cfg = _cfg(tmp_path)
    led = _init(cfg.ledger_path)
    budget.release_process_lock(cfg.ledger_path)
    os.remove(cfg.ledger_path)
    fake = FakeSDKClient()
    with pytest.raises(budget.BudgetStop, match="ledger_unavailable"):
        _run(cfg, fake, led)
    assert fake.calls == [] and not os.path.exists(cfg.ledger_path)


def test_second_process_refused_while_first_holds_ledger(tmp_path):
    path = tmp_path / "ledger.db"
    _init(path)
    budget.release_process_lock(str(path))  # 이 테스트 프로세스는 잠금을 놓고, 자식이 "서버"가 됨
    p = _child(HOLD, str(path))
    try:
        with pytest.raises(budget.LedgerError, match="ledger_in_use"):
            budget.Ledger(str(path), BIG, "day").open()
    finally:
        p.communicate("", timeout=30)
    budget.Ledger(str(path), BIG, "day").open()  # 자식이 끝나면 열 수 있음


def test_crash_after_reserve_recovers_as_unknown_in_new_process(tmp_path):
    path = tmp_path / "ledger.db"
    _init(path)
    budget.release_process_lock(str(path))
    crash = """
    import os, sys
    from ctrec.web import budget
    led = budget.Ledger(sys.argv[1], budget.Limits(1000, 10000, 10**9, 10**9), "day").open()
    led.reserve("run-crash", "m", 0.75, 100, 10)
    print("ready", flush=True)
    sys.stdin.readline()
    os._exit(9)  # 정산 없이 비정상 종료 (OS가 잠금을 풂)
    """
    p = _child(crash, str(path))
    p.communicate("\n", timeout=30)
    assert p.returncode == 9
    led = budget.Ledger(str(path), BIG, "day").open()  # 재시작: 잠금 획득 + 복구
    rows = led.rows()
    assert led.recovered == 1 and [(r["status"], r["reason"]) for r in rows] == [("unknown", "orphaned_on_restart")]
    assert led.used()["cost_usd"] == pytest.approx(0.75)  # UNKNOWN은 계속 점유
    budget.reset_ledgers()
    again = budget.Ledger(str(path), BIG, "day").open()  # 다시 재시작해도 UNKNOWN 유지 (자동으로 풀지 않음)
    assert again.recovered == 0 and again.rows("unknown")[0]["id"] == rows[0]["id"]


class _FailingConn:
    """특정 SQL에서 sqlite 오류를 내는 연결 (디스크 가득 참·읽기 전용 디렉터리 흉내)."""

    def __init__(self, conn, when, message):
        self._c, self._when, self._msg = conn, when, message

    def execute(self, sql, *a):
        if self._when(sql):
            raise sqlite3.OperationalError(self._msg)
        return self._c.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._c, name)


@pytest.mark.parametrize("message", ["database or disk is full", "attempt to write a readonly database"])
def test_write_failure_on_reserve_sends_nothing(tmp_path, monkeypatch, message):
    cfg = _cfg(tmp_path)
    led = _init(cfg.ledger_path)
    orig = budget.Ledger._conn
    monkeypatch.setattr(budget.Ledger, "_conn", lambda self, create=False: _FailingConn(
        orig(self, create), lambda sql: sql.startswith("INSERT"), message))
    fake = FakeSDKClient()
    with pytest.raises(budget.BudgetStop, match="ledger_unavailable") as e:
        _run(cfg, fake, led)
    assert e.value.category == budget.BUDGET_ERROR and fake.calls == []


def test_settlement_failure_stops_run_and_keeps_reservation_held(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    led = _init(cfg.ledger_path)
    orig = budget.Ledger._conn
    monkeypatch.setattr(budget.Ledger, "_conn", lambda self, create=False: _FailingConn(
        orig(self, create), lambda sql: sql.startswith("UPDATE reservations SET status = ?") and "settled" not in sql,
        "database or disk is full"))
    fake = FakeSDKClient()
    with budget.run_context("run-1", cfg, led, fake) as b:
        _call()  # 응답은 받았지만 정산 기록 실패
        with pytest.raises(budget.BudgetStop, match="ledger_unavailable"):
            _call()  # 다음 요청은 보내지 않음
    assert len(fake.calls) == 1 and b.stopped == "ledger_unavailable"
    monkeypatch.setattr(budget.Ledger, "_conn", orig)
    row = led.rows()[0]
    assert row["status"] == "reserved" and led.used()["cost_usd"] == pytest.approx(row["est_cost"])  # 예약액 점유 유지


def test_settings_changed_after_restart(tmp_path):
    cfg = _cfg(tmp_path)
    led = _init(cfg.ledger_path)
    _run(cfg, FakeSDKClient(), led, n=2)
    old = [dict(r) for r in led.rows()]
    budget.reset_ledgers()
    # 공용 호출 한도를 이미 쓴 양보다 낮추고 재시작 -> 새 호출 차단
    low = _cfg(tmp_path, shared=budget.Limits(1000.0, 2, 10**9, 10**9))
    fake = FakeSDKClient()
    with pytest.raises(budget.BudgetStop, match="shared_call_cap"):
        _run(low, fake, budget.Ledger(low.ledger_path, low.shared, "day").open())
    assert fake.calls == []
    # 단가 변경 -> 새 정산만 새 단가, 이미 정산된 행은 그대로
    pricier = _cfg(tmp_path, price=budget.Price(config.MODEL, 4.0, 20.0, 5.0, 0.4, "test2"))
    led2 = budget.Ledger(pricier.ledger_path, pricier.shared, "day").open()
    _run(pricier, FakeSDKClient(), led2)
    rows = led2.rows()
    assert [dict(r) for r in rows[:2]] == old and rows[2]["actual_cost"] > old[0]["actual_cost"]


def test_model_without_confirmed_price_refused(tmp_path):
    env = {**ENV_ENFORCE, "CTREC_WEB_BUDGET_LEDGER": str(tmp_path / "l.db")}
    with config.overrides(MODEL="claude-other-model"):
        with pytest.raises(budget.BudgetConfigError, match="price_model_mismatch"):
            budget.load_config(env)
    with config.overrides(USE_FALLBACKS=True), pytest.raises(budget.BudgetConfigError, match="fallbacks_enabled"):
        budget.load_config(env)
    with pytest.raises(budget.BudgetConfigError, match="CTREC_WEB_PRICE_INPUT"):
        budget.load_config({**env, "CTREC_WEB_PRICE_INPUT": "two"})


def test_regional_inference_response_is_unknown(tmp_path):
    cfg = _cfg(tmp_path)
    led = _init(cfg.ledger_path)
    fake = FakeSDKClient()
    real = fake.messages.create

    def create(**kw):
        r = real(**kw)
        r.usage.inference_geo = "us"
        return r
    fake.messages.create = create
    with pytest.raises(budget.BudgetStop, match="inference_geo_mismatch"):
        _run(cfg, fake, led)
    assert [r["status"] for r in led.rows()] == ["unknown"]


# ================================================================ 운영 도구: init·백업·복구·UNKNOWN (6-5)


def _cli(monkeypatch, capsys, path, *argv):
    monkeypatch.setenv("CTREC_WEB_BUDGET_LEDGER", str(path))
    capsys.readouterr()
    try:
        code = cli.main(list(argv))
    except SystemExit as e:
        return (e.code if isinstance(e.code, int) else 1), str(e.code)
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_cli_init_refuses_to_overwrite(tmp_path, monkeypatch, capsys):
    path = tmp_path / "led.db"
    code, out = _cli(monkeypatch, capsys, path, "init")
    assert code == 0 and out["created"] and out["check"]["rows"] == 0
    code, msg = _cli(monkeypatch, capsys, path, "init")
    assert code == 1 and "덮어쓰지" in msg


def test_backup_verify_restore_cycle(tmp_path, monkeypatch, capsys):
    path = tmp_path / "led.db"
    led = _init(path)
    settled = led.reserve("run-a", "m", 0.2, 10, 10)
    led.settle(settled, 0.1, 5, 5, 0, 0)
    unk = led.reserve("run-b", "m", 0.4, 10, 10)
    led.mark_unknown(unk, "response_lost")
    inflight = led.reserve("run-c", "m", 0.3, 10, 10)  # 백업 시점에 진행 중
    bak = tmp_path / "bak" / "led-1.db"
    bak.parent.mkdir()
    code, out = _cli(monkeypatch, capsys, path, "backup", str(bak))
    assert code == 0 and out["check"]["rows"] == 3 and Path(str(bak) + ".sha256").is_file()
    code, _ = _cli(monkeypatch, capsys, path, "backup", str(bak))
    assert code == 1  # 기존 백업을 덮어쓰지 않음
    code, out = _cli(monkeypatch, capsys, path, "verify", str(bak))
    assert code == 0 and out["checksum"] == "ok"
    led.reserve("run-d", "m", 0.5, 10, 10)  # 백업 이후 기록
    code, msg = _cli(monkeypatch, capsys, path, "restore", str(bak))
    assert code == 1 and "--confirm" in msg
    budget.reset_ledgers()
    budget.release_process_lock(str(path))  # 서버(이 프로세스)를 멈춘 상태
    code, out = _cli(monkeypatch, capsys, path, "restore", str(bak), "--confirm")
    assert code == 0 and out["reserved_to_unknown"] == 1 and "백업 시점 이후" in out["warning"]
    kept = Path(out["previous_ledger_kept_as"])
    assert kept.is_file() and budget.inspect_ledger(str(kept))["rows"] == 4  # 기존 원장은 지우지 않고 보관
    restored = budget.Ledger(str(path), BIG, "day").open()
    st = {r["run_id"]: (r["status"], r["reason"]) for r in restored.rows()}
    assert st == {"run-a": ("settled", None), "run-b": ("unknown", "response_lost"),
                  "run-c": ("unknown", "restored_from_backup")}
    assert inflight in {r["id"] for r in restored.rows("unknown")}


def test_restore_rejects_bad_backups_and_running_server(tmp_path, monkeypatch, capsys):
    path = tmp_path / "led.db"
    _init(path)
    bak = tmp_path / "b.db"
    _cli(monkeypatch, capsys, path, "backup", str(bak))
    budget.reset_ledgers()
    budget.release_process_lock(str(path))
    no_sum = tmp_path / "nosum.db"
    no_sum.write_bytes(bak.read_bytes())
    code, msg = _cli(monkeypatch, capsys, path, "restore", str(no_sum), "--confirm")
    assert code == 1 and "체크섬 파일" in msg
    tampered = tmp_path / "t.db"
    data = bytearray(bak.read_bytes())
    data[120:130] = bytes(b ^ 0xFF for b in data[120:130])  # 내용 변조
    tampered.write_bytes(bytes(data))
    Path(str(tampered) + ".sha256").write_text(Path(str(bak) + ".sha256").read_text())
    code, msg = _cli(monkeypatch, capsys, path, "restore", str(tampered), "--confirm")
    assert code == 1 and "체크섬" in msg
    garbage = tmp_path / "g.db"
    garbage.write_bytes(b"not sqlite" * 50)
    Path(str(garbage) + ".sha256").write_text(cli._sha256(str(garbage)) + "  g.db\n")
    code, msg = _cli(monkeypatch, capsys, path, "restore", str(garbage), "--confirm")
    assert code == 1 and "올바른 원장이 아닙니다" in msg
    p = _child(HOLD, str(path))  # 서버 실행 중
    try:
        code, msg = _cli(monkeypatch, capsys, path, "restore", str(bak), "--confirm")
        assert code == 1 and "서버를 멈춘" in msg
    finally:
        p.communicate("", timeout=30)
    assert budget.inspect_ledger(str(path))["rows"] == 0 and not list(tmp_path.glob("led.db.before-restore-*"))


def test_backup_refuses_corrupt_source(tmp_path, monkeypatch, capsys):
    path = tmp_path / "led.db"
    path.write_bytes(b"corrupt" * 100)
    code, msg = _cli(monkeypatch, capsys, path, "backup", str(tmp_path / "x.db"))
    assert code == 1 and not (tmp_path / "x.db").exists()


def test_unknown_listing_has_reason_time_amount_and_run(tmp_path, monkeypatch, capsys):
    path = tmp_path / "led.db"
    led = _init(path)
    rid = led.reserve("run-z", "m", 0.25, 10, 10)
    led.mark_unknown(rid, "response_lost")
    code, rows = _cli(monkeypatch, capsys, path, "unknown")
    assert code == 0 and rows[0]["run_id"] == "run-z" and rows[0]["reason"] == "response_lost"
    assert rows[0]["est_cost"] == 0.25 and rows[0]["created_at_utc"].endswith("+00:00") and rows[0]["age_hours"] >= 0
    code, s = _cli(monkeypatch, capsys, path, "summary")
    assert s["unknown_held_usd"] == 0.25 and s["used"]["cost_usd"] == pytest.approx(0.25)
    code, out = _cli(monkeypatch, capsys, path, "resolve", rid, "--cost", "0.05", "--input", "10", "--output", "3")
    assert out == {"resolved": True}
    code, out = _cli(monkeypatch, capsys, path, "resolve", rid, "--not-billed")
    assert code == 1 and out == {"resolved": False}  # 중복 정산 불가
    code, s = _cli(monkeypatch, capsys, path, "summary")
    assert s["used"]["cost_usd"] == pytest.approx(0.05) and s["rows_by_status"] == {"settled": 1}


# ================================================================ 배포 사전 점검 (6-4)


def _check(*argv, environ=None):
    r, meta = chk.run(list(argv), environ=environ)
    return {(i["area"], i["status"]) for i in r.items}, r


def _yaml(tmp_path, text, name="render.yaml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


def test_repo_render_yaml_record_has_no_blockers():
    got, r = _check()
    assert not [i for i in r.items if i["status"] == chk.BLOCKED], r.items
    assert {("workers", "PASS"), ("fallbacks", "PASS"), ("budget_mode", "PASS"), ("instances", "PASS"),
            ("health_check", "PASS"), ("secrets_in_repo", "PASS")} <= got
    assert ("auth", "UNVERIFIED") in got and ("model_access", "UNVERIFIED") in got  # 대시보드 값·실제 호출은 확인 안 함


def test_previous_render_yaml_problems_detected(tmp_path):
    old = subprocess.run(["git", "show", "c90e009:render.yaml"], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8").stdout
    got, _ = _check("--render-yaml", _yaml(tmp_path, old))
    assert ("workers", "BLOCKED") in got and ("fallbacks", "WARNING") in got  # WEB_CONCURRENCY 워커·대체 모델
    got, _ = _check("--render-yaml", _yaml(tmp_path, old), "--target", "enforce")
    assert {("fallbacks", "BLOCKED"), ("plan", "BLOCKED"), ("disk", "BLOCKED")} <= got


def test_enforce_example_blocked_until_price_confirmed_and_ledger_ready(tmp_path):
    yaml = str(ROOT / "deploy" / "render.enforce.yaml")
    env_file = str(ROOT / "deploy" / "web-enforce.env.example")
    got, r = _check("--render-yaml", yaml, "--env-file", env_file)
    assert ("enforce_config", "BLOCKED") in got and ("disk", "PASS") in got and ("disk_persistence", "UNVERIFIED") in got
    assert ("ledger_dir", "UNVERIFIED") in got  # 이 컴퓨터에 /var/data가 없음 -> 있다고 추정하지 않음
    led = tmp_path / "data" / "web-ledger.db"
    led.parent.mkdir()
    filled = tmp_path / "filled.env"
    filled.write_text(Path(env_file).read_text(encoding="utf-8").replace("CTREC_WEB_PRICE_CONFIRMED=0", "CTREC_WEB_PRICE_CONFIRMED=1")
                      .replace("CTREC_WEB_PRICE_SOURCE=", "CTREC_WEB_PRICE_SOURCE=pricing-page-2026-10-15")
                      + f"\nCTREC_WEB_BUDGET_LEDGER={led}\nCTREC_PASSWORD=correct-horse-battery\n", encoding="utf-8")
    got, r = _check("--render-yaml", yaml, "--env-file", str(filled), "--process-env", environ={})
    assert ("enforce_config", "PASS") in got and ("ledger_file", "BLOCKED") in got  # 아직 init 안 함
    assert ("disk", "BLOCKED") in got  # 임시 경로는 마운트 경로 밖
    _init(led)
    led_obj = budget.Ledger(str(led), BIG, "day")
    led_obj.mark_unknown(led_obj.reserve("r", "m", 0.1, 1, 1), "response_lost")
    got, r = _check("--render-yaml", yaml, "--env-file", str(filled), "--process-env", environ={})
    assert {("ledger_file", "PASS"), ("ledger_dir", "PASS"), ("ledger_unknown", "WARNING"), ("auth", "PASS")} <= got
    assert all(i["status"] == "PASS" for i in r.items if i["area"] == "price")


def test_deploy_check_blocks_unsafe_settings_and_hides_secrets(tmp_path, capsys):
    leaked = "sk-ant-" + "api03-THIS-IS-A-LEAKED-VALUE-0000000000"  # 실행 때 조립 (저장소 키 검사에 걸리지 않게)
    yaml = _yaml(tmp_path, """
        services:
          - type: web
            name: x
            plan: free
            numInstances: 2
            startCommand: uvicorn ctrec.web.app:app --host 0.0.0.0 --port $PORT --workers 3 --log-level debug
            healthCheckPath: /
            envVars:
              - key: ANTHROPIC_API_KEY
                value: LEAKED_KEY
              - key: CTREC_WEB_SAVE_OUTPUTS
                value: "1"
              - key: CTREC_WEB_METERING
                value: enforce
              - key: CTREC_FALLBACKS
                value: "1"
              - key: CTREC_WEB_PRICE_INPUT
                value: "1.5"
        """.replace("LEAKED_KEY", leaked))
    env = tmp_path / "e.env"
    env.write_text("CTREC_PASSWORD=\n", encoding="utf-8")
    got, r = _check("--render-yaml", yaml, "--env-file", str(env))
    assert {("instances", "BLOCKED"), ("workers", "BLOCKED"), ("proxy", "BLOCKED"), ("log_level", "WARNING"),
            ("health_check", "WARNING"), ("secrets_in_blueprint", "BLOCKED"), ("outputs", "WARNING"),
            ("fallbacks", "BLOCKED"), ("plan", "BLOCKED"), ("ctrec_require_https", "BLOCKED"), ("price", "BLOCKED"),
            ("auth", "WARNING")} <= got
    assert chk.main(["--render-yaml", yaml, "--env-file", str(env), "--json"]) == 2
    printed = capsys.readouterr().out
    assert "LEAKED" not in printed and "sk-ant-api03" not in printed
    rec = _yaml(tmp_path, """
        services:
          - type: web
            numInstances: 1
            startCommand: uvicorn ctrec.web.app:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips 10.0.0.1
            envVars:
              - key: CTREC_WEB_METERING
                value: bogus
        """, "rec.yaml")
    env.write_text("ANTHROPIC_API_KEY=sk-ant-test-x\nCTREC_PASSWORD=\n", encoding="utf-8")
    got, _ = _check("--render-yaml", rec, "--env-file", str(env), "--target", "record")
    assert {("budget_mode", "BLOCKED"), ("auth", "BLOCKED"), ("proxy", "PASS")} <= got  # 비밀번호 없는 유료 호출 가능 구성


# ================================================================ 탭을 닫았다 다시 연 경우 (6-6)


def test_active_run_offered_only_to_same_browser(monkeypatch, tmp_path):
    h.install(monkeypatch, tmp_path)
    me, other = h.browser(), h.browser(client=("198.51.100.21", 50001))
    assert me.get("/api/runs/active").json() == {"run_id": None}
    r = me.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False})
    rid = r.json()["run_id"]
    run = web.RUNS[rid]
    q = h.wait_for(run, "questions")
    assert me.get("/api/runs/active").json() == {"run_id": rid}
    assert other.get("/api/runs/active").json() == {"run_id": None}
    assert other.get(f"/api/runs/{rid}").status_code == 404  # ID를 알아도 소유권 쿠키 없이 접근 불가
    me.post(f"/api/runs/{rid}/answers", json={"answers": {x["question_id"]: "모름" for x in q["questions"]}})
    h.wait_for(run, "end", 30)
    assert me.get("/api/runs/active").json() == {"run_id": None}  # 끝난 실행은 알려 주지 않음


def test_enforce_without_initialized_ledger_refuses_to_start(monkeypatch, tmp_path):
    sdk = h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    for k, v in {**ENV_ENFORCE, "CTREC_WEB_METERING": "enforce",
                 "CTREC_WEB_BUDGET_LEDGER": str(tmp_path / "missing" / "l.db")}.items():
        monkeypatch.setenv(k, v)
    web.RUNS.clear()
    r = h.browser().post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False})
    assert r.status_code == 503 and "비용 관리 설정" in r.json()["detail"] and "l.db" not in r.text
    assert sdk.requests == [] and web.RUNS == {} and not (tmp_path / "missing").exists()
