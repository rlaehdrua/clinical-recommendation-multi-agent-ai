"""웹 실행별·공용 LLM 비용 통제 (Phase 6-3, enforce 모드).

모드 (CTREC_WEB_METERING): off / record(기본, 기록만) / enforce(호출 전 예약·상한 검사). Phase 5 파일럿 실행 게이트와 무관.

enforce 모드:
- 실행마다 BudgetClient를 그 실행의 컨텍스트에 걸고 call_guard에 배타(fail-closed)로 등록 -> 등록된 예산 클라이언트를
  거치지 않는 LLM 호출(컨텍스트 없는 스레드·기본 클라이언트·직접 SDK·SDK 자동 재시도·미들웨어)은 막힘.
- 호출 직전에 보수적 최악값(입력 추정 x 입력·캐시 쓰기 중 비싼 단가 + max_tokens x 출력 단가)을 실행별 한도와
  공용 원장(SQLite)에 예약하고, 응답 뒤 실제 usage로 정산.
- 결과가 불명확한 요청(응답 없는 실패, usage 누락, 다른 모델·대체 모델 응답, 중단)은 UNKNOWN으로 남기고 예약액을
  계속 점유 (운영자가 정산할 때까지). 0으로 보지 않음.
- 비용 '상한'은 이 서버가 보내는 요청을 예약액 기준으로 막는 것이며, 외부 청구액의 절대 상한을 보장하지 않음.

원장에는 실행 ID·예약 ID·모델·토큰·금액·상태·시각·사유 코드만 저장 (환자 원문·문답·보고서·프롬프트·키·쿠키 없음).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import call_guard, config, llm
from ..llm_pilot import (ALLOWED_OPTIONS, REQUEST_OVERHEAD_TOKENS, is_retryable, possibly_billed,  # noqa: F401
                         request_problem, request_tokens_bound, response_problem)  # 파일럿과 같은 요청·응답 검사

# 차단 이유 코드 -> 사용자에게 보이는 종료 종류
EXCEEDED = "budget_exceeded"  # 한도에 도달해 새 호출을 막음
USAGE_UNKNOWN = "usage_unknown"  # 사용량을 확인할 수 없어 안전을 위해 멈춤
BUDGET_ERROR = "budget_error"  # 원장·설정 등 비용 관리 기능 오류
CATEGORY = {
    "run_cost_cap": EXCEEDED, "run_call_cap": EXCEEDED, "run_input_token_cap": EXCEEDED,
    "run_output_token_cap": EXCEEDED, "run_time_cap": EXCEEDED,
    "shared_cost_cap": EXCEEDED, "shared_call_cap": EXCEEDED, "shared_input_token_cap": EXCEEDED,
    "shared_output_token_cap": EXCEEDED,
    "usage_missing": USAGE_UNKNOWN, "response_lost": USAGE_UNKNOWN, "model_mismatch": USAGE_UNKNOWN,
    "fallback_detected": USAGE_UNKNOWN, "service_tier_mismatch": USAGE_UNKNOWN, "cache_ttl_mismatch": USAGE_UNKNOWN,
    "inference_geo_mismatch": USAGE_UNKNOWN,
    "interrupted": USAGE_UNKNOWN,
    "fallback_requested": BUDGET_ERROR, "model_price_mismatch": BUDGET_ERROR, "sdk_retries_enabled": BUDGET_ERROR,
    "request_option_unsupported": BUDGET_ERROR, "server_tool_requested": BUDGET_ERROR, "estimate_exceeded": BUDGET_ERROR,
    "ledger_unavailable": BUDGET_ERROR, "ledger_inconsistent": BUDGET_ERROR, "ledger_missing": BUDGET_ERROR,
    "ledger_in_use": BUDGET_ERROR,
}


class BudgetStop(llm.RunCancelled):
    """예산 통제로 더 이상 LLM을 부르지 않음 (args[0]: 이유 코드). 기존 취소 경로(RunCancelled)를 그대로 탐."""

    @property
    def code(self) -> str:
        return self.args[0] if self.args else "ledger_unavailable"

    @property
    def category(self) -> str:
        return CATEGORY.get(self.code, BUDGET_ERROR)


class BudgetConfigError(RuntimeError):
    """enforce에 필요한 설정이 없거나 잘못됨 (problems: 운영자용 코드 목록, 사용자에게 보이지 않음)."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class LedgerError(RuntimeError):
    """원장을 쓸 수 없음 (args[0]: ledger_unavailable / ledger_inconsistent). enforce에서는 호출을 막음."""


# ---------------------------------------------------------------- 설정 (서버 환경 변수만, 요청 본문으로 바꿀 수 없음)


@dataclass(frozen=True)
class Limits:
    max_cost_usd: float
    max_calls: int
    max_input_tokens: int
    max_output_tokens: int


@dataclass(frozen=True)
class Price:
    model: str
    input: float
    output: float
    cache_write: float
    cache_read: float
    source: str

    def cost(self, inp: int, out: int, cache_write: int = 0, cache_read: int = 0) -> float:
        return (inp * self.input + out * self.output + cache_write * self.cache_write + cache_read * self.cache_read) / 1e6

    def worst(self, est_in: int, max_out: int) -> float:
        """예약용 최악값: 입력은 입력·캐시 쓰기 중 비싼 단가, 출력은 max_tokens 전부 (사고 토큰 포함)."""
        return (est_in * max(self.input, self.cache_write) + max_out * self.output) / 1e6


@dataclass(frozen=True)
class BudgetConfig:
    run: Limits
    shared: Limits
    period: str  # day / month / total
    price: Price
    ledger_path: str
    run_max_seconds: float
    max_retries: int = 1  # 일시 오류만, 시도마다 따로 예약
    call_timeout_s: float = 300.0


_REQUIRED = {
    "run": ("CTREC_WEB_RUN_MAX_USD", "CTREC_WEB_RUN_MAX_CALLS", "CTREC_WEB_RUN_MAX_INPUT_TOKENS",
            "CTREC_WEB_RUN_MAX_OUTPUT_TOKENS"),
    "shared": ("CTREC_WEB_SHARED_MAX_USD", "CTREC_WEB_SHARED_MAX_CALLS", "CTREC_WEB_SHARED_MAX_INPUT_TOKENS",
               "CTREC_WEB_SHARED_MAX_OUTPUT_TOKENS"),
    "price": ("CTREC_WEB_PRICE_INPUT", "CTREC_WEB_PRICE_OUTPUT", "CTREC_WEB_PRICE_CACHE_WRITE", "CTREC_WEB_PRICE_CACHE_READ"),
}


def _num(env, name, cast, problems):
    raw = (env.get(name) or "").strip()
    if not raw:
        problems.append(f"missing:{name}")
        return None
    try:
        v = cast(raw)
    except ValueError:
        problems.append(f"invalid:{name}")
        return None
    if not v > 0:
        problems.append(f"not_positive:{name}")
        return None
    return v


def load_config(env=None, default_run_seconds: float = 3600.0) -> BudgetConfig:
    """enforce 설정 검사. 하나라도 빠지거나 잘못되면 BudgetConfigError (조용히 record로 내려가지 않음)."""
    env = os.environ if env is None else env
    p: list[str] = []
    lim = {}
    for group, names in _REQUIRED.items():
        casts = (float, int, int, int) if group != "price" else (float, float, float, float)
        lim[group] = [_num(env, n, c, p) for n, c in zip(names, casts)]
    ledger = (env.get("CTREC_WEB_BUDGET_LEDGER") or "").strip()
    if not ledger:
        p.append("missing:CTREC_WEB_BUDGET_LEDGER")
    if env.get("CTREC_WEB_BUDGET_LEDGER_PERSISTENT") != "1":
        p.append("unconfirmed:CTREC_WEB_BUDGET_LEDGER_PERSISTENT")  # 운영자가 영속 저장소임을 확인해야 함
    if env.get("CTREC_WEB_BUDGET_SINGLE_INSTANCE") != "1":
        p.append("unconfirmed:CTREC_WEB_BUDGET_SINGLE_INSTANCE")  # 이 원장을 쓰는 서버 프로세스가 하나뿐임을 확인
    if env.get("CTREC_WEB_PRICE_CONFIRMED") != "1" or not (env.get("CTREC_WEB_PRICE_SOURCE") or "").strip():
        p.append("unconfirmed:CTREC_WEB_PRICE")  # 공식 가격표 확인 기록 필요
    if (env.get("CTREC_WEB_PRICE_MODEL") or "").strip() != config.MODEL:
        p.append("price_model_mismatch:CTREC_WEB_PRICE_MODEL")  # 단가를 확인한 모델과 실제 호출 모델이 같아야 함
    period = (env.get("CTREC_WEB_BUDGET_PERIOD") or "day").strip()
    if period not in ("day", "month", "total"):
        p.append("invalid:CTREC_WEB_BUDGET_PERIOD")
    if config.USE_FALLBACKS:
        p.append("fallbacks_enabled:CTREC_FALLBACKS")  # 대체 모델 단가를 알 수 없음 -> 0 필요
    run_seconds = default_run_seconds
    if (env.get("CTREC_WEB_RUN_MAX_SECONDS") or "").strip():
        run_seconds = _num(env, "CTREC_WEB_RUN_MAX_SECONDS", float, p) or default_run_seconds
    retries = (env.get("CTREC_WEB_BUDGET_MAX_RETRIES") or "1").strip()
    if retries not in ("0", "1", "2", "3"):
        p.append("invalid:CTREC_WEB_BUDGET_MAX_RETRIES")
    if p:
        raise BudgetConfigError(p)
    r, s, pr = lim["run"], lim["shared"], lim["price"]
    return BudgetConfig(run=Limits(*r), shared=Limits(*s), period=period,
                        price=Price(config.MODEL, *pr, source=env["CTREC_WEB_PRICE_SOURCE"].strip()),
                        ledger_path=ledger, run_max_seconds=min(run_seconds, default_run_seconds),
                        max_retries=int(retries))


# ---------------------------------------------------------------- 공용 원장 (SQLite, 단일 인스턴스)

SCHEMA_VERSION = "1"
STATUSES = ("reserved", "settled", "unknown", "released")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reservations (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, window TEXT NOT NULL, model TEXT NOT NULL, boot_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('reserved','settled','unknown','released')),
  est_cost REAL NOT NULL CHECK (est_cost >= 0), est_input INTEGER NOT NULL CHECK (est_input >= 0),
  est_output INTEGER NOT NULL CHECK (est_output >= 0),
  actual_cost REAL CHECK (actual_cost IS NULL OR actual_cost >= 0), actual_input INTEGER, actual_output INTEGER,
  cache_write INTEGER, cache_read INTEGER, reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS reservations_window ON reservations(window, status);
"""
# 예산 점유: 정산된 것은 실제값, 예약 중·UNKNOWN은 예약한 최악값, 해제(과금 없음)는 0
_USED_SQL = """SELECT COUNT(*),
  COALESCE(SUM(CASE status WHEN 'settled' THEN actual_cost ELSE est_cost END), 0),
  COALESCE(SUM(CASE status WHEN 'settled' THEN actual_input + cache_write + cache_read ELSE est_input END), 0),
  COALESCE(SUM(CASE status WHEN 'settled' THEN actual_output ELSE est_output END), 0)
  FROM reservations WHERE window = ? AND status IN ('reserved', 'settled', 'unknown')"""


def window_key(period: str, now: float | None = None) -> str:
    d = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return {"day": d.strftime("day:%Y-%m-%d"), "month": d.strftime("month:%Y-%m")}.get(period, "total")


_PROC_LOCKS: dict[str, object] = {}
_PROC_LOCKS_GUARD = threading.Lock()


def _try_lock(fh) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def process_lock(path: str) -> None:
    """원장 옆 잠금 파일(<원장>.lock)을 이 프로세스가 끝날 때까지 배타적으로 쥠. OS 잠금이라 프로세스가 비정상 종료해도 풀림.
    같은 프로세스 안에서는 다시 열어도 됨. 다른 프로세스가 쥐고 있으면 LedgerError(ledger_in_use)."""
    key = os.path.normcase(os.path.abspath(path))
    with _PROC_LOCKS_GUARD:
        if key in _PROC_LOCKS:
            return
        try:
            fh = open(key + ".lock", "a+b")  # noqa: SIM115 - 프로세스 수명 동안 유지
        except OSError as e:
            raise LedgerError("ledger_unavailable") from e
        if not _try_lock(fh):
            fh.close()
            raise LedgerError("ledger_in_use")
        _PROC_LOCKS[key] = fh


def release_process_lock(path: str) -> None:
    """운영 도구(복구 명령)용: 이 프로세스가 쥔 잠금을 놓음."""
    key = os.path.normcase(os.path.abspath(path))
    with _PROC_LOCKS_GUARD:
        fh = _PROC_LOCKS.pop(key, None)
    if fh is not None:
        fh.close()  # 닫으면 OS 잠금도 풀림


def inspect_ledger(path: str, full: bool = True) -> dict:
    """원장 파일을 읽기 전용으로 검사 (잠금·복구·쓰기 없음). 운영 도구·배포 점검용.
    무결성 검사, 스키마 버전, 정산 행의 실제값 누락, 상태별 행 수, 마지막 갱신 시각. 문제가 있으면 LedgerError."""
    if not os.path.isfile(path):
        raise LedgerError("ledger_missing")
    try:
        c = sqlite3.connect(Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as e:
        raise LedgerError("ledger_unavailable") from e
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"meta", "reservations"} <= tables:
            raise LedgerError("ledger_inconsistent" if tables else "ledger_missing")
        if c.execute("PRAGMA integrity_check" if full else "PRAGMA quick_check").fetchone()[0] != "ok":
            raise LedgerError("ledger_inconsistent")
        row = c.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            raise LedgerError("ledger_inconsistent")
        if c.execute("SELECT COUNT(*) FROM reservations WHERE status = 'settled' AND (actual_cost IS NULL "
                     "OR actual_input IS NULL OR actual_output IS NULL)").fetchone()[0]:
            raise LedgerError("ledger_inconsistent")
        by = dict(c.execute("SELECT status, COUNT(*) FROM reservations GROUP BY status").fetchall())
        last = c.execute("SELECT MAX(updated_at) FROM reservations").fetchone()[0]
        return {"rows": sum(by.values()), "rows_by_status": by, "last_updated_at": last}
    except sqlite3.Error as e:
        raise LedgerError("ledger_inconsistent") from e
    finally:
        c.close()


class Ledger:
    """공용 예산 원장. 예약·정산은 SQLite 트랜잭션(BEGIN IMMEDIATE) 하나로 원자적으로 처리 (같은 호스트의 여러
    스레드·연결 사이에서). 여러 서버 인스턴스(다른 호스트)의 전역 한도는 보장하지 않음."""

    def __init__(self, path: str, limits: Limits, period: str, boot_id: str | None = None,
                 clock: Callable[[], float] = time.time):
        self.path, self.limits, self.period, self.clock = path, limits, period, clock
        self.boot_id = boot_id or uuid.uuid4().hex
        self.recovered = 0

    def _conn(self, create: bool = False) -> sqlite3.Connection:
        # mode=rw: 원장 파일이 없으면 새로 만들지 않고 실패 (영속 디스크가 빠진 경로에 빈 원장이 생겨 사용액이 0이 되는 것 방지)
        uri = Path(os.path.abspath(self.path)).as_uri() + ("?mode=rwc" if create else "?mode=rw")
        c = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
        c.execute("PRAGMA busy_timeout = 10000")
        return c

    @contextlib.contextmanager
    def _tx(self):
        try:
            c = self._conn()
        except sqlite3.Error as e:
            raise LedgerError("ledger_unavailable") from e
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except (sqlite3.Error, LedgerError) as e:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ROLLBACK")
            if isinstance(e, LedgerError):
                raise
            raise LedgerError("ledger_unavailable") from e
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def open(self, create: bool = False) -> "Ledger":
        """프로세스 잠금 + 스키마 확인 + 무결성 확인 + 이전 프로세스가 남긴 예약을 UNKNOWN으로 (재시작 복구).

        create=False(서버): 원장 파일이 없으면 ledger_missing (운영자가 scripts/web_budget_ledger.py init으로 만듦).
        같은 원장을 다른 프로세스가 열고 있으면 ledger_in_use (여러 워커·서버가 서로의 진행 중 예약을 복구하지 않도록)."""
        if not create and not os.path.exists(self.path):
            raise LedgerError("ledger_missing")
        process_lock(self.path)
        if create:
            try:
                c0 = self._conn(create=True)
                try:
                    c0.executescript(_SCHEMA)  # 없으면 만들기 (있으면 그대로)
                finally:
                    c0.close()
            except sqlite3.Error as e:
                raise LedgerError("ledger_unavailable") from e
        with self._tx() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"meta", "reservations"} <= tables:
                # 빈 파일·다른 DB: 서버는 새 원장으로 시작하지 않음 (사용액이 0으로 돌아가는 것 방지)
                raise LedgerError("ledger_inconsistent" if tables else "ledger_missing")
            row = c.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if row is None and create:
                c.execute("INSERT INTO meta(key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))
            elif row is None or row[0] != SCHEMA_VERSION:
                raise LedgerError("ledger_inconsistent")
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LedgerError("ledger_inconsistent")
            bad = c.execute("SELECT COUNT(*) FROM reservations WHERE status = 'settled' AND (actual_cost IS NULL "
                            "OR actual_input IS NULL OR actual_output IS NULL)").fetchone()[0]
            if bad:
                raise LedgerError("ledger_inconsistent")
            now = self.clock()
            cur = c.execute("UPDATE reservations SET status = 'unknown', reason = 'orphaned_on_restart', updated_at = ? "
                            "WHERE status = 'reserved' AND boot_id != ?", (now, self.boot_id))
            self.recovered = cur.rowcount
        return self

    def used(self, window: str | None = None) -> dict:
        with self._tx() as c:
            n, cost, inp, out = c.execute(_USED_SQL, (window or window_key(self.period, self.clock()),)).fetchone()
        return {"calls": n, "cost_usd": cost, "input_tokens": inp, "output_tokens": out}

    def reserve(self, run_id: str, model: str, est_cost: float, est_input: int, est_output: int) -> str:
        """한도 안이면 예약하고 예약 ID를 돌려줌. 넘으면 BudgetStop(shared_*_cap). 확인과 기록이 한 트랜잭션."""
        rid = uuid.uuid4().hex
        now = self.clock()
        win = window_key(self.period, now)
        with self._tx() as c:
            n, cost, inp, out = c.execute(_USED_SQL, (win,)).fetchone()
            lim = self.limits
            code = ("shared_call_cap" if n + 1 > lim.max_calls else
                    "shared_input_token_cap" if inp + est_input > lim.max_input_tokens else
                    "shared_output_token_cap" if out + est_output > lim.max_output_tokens else
                    "shared_cost_cap" if cost + est_cost > lim.max_cost_usd else None)
            if code:
                raise BudgetStop(code)
            c.execute("INSERT INTO reservations (id, run_id, window, model, boot_id, status, est_cost, est_input, est_output, "
                      "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?)",
                      (rid, run_id, win, model, self.boot_id, est_cost, est_input, est_output, now, now))
        return rid

    def _transition(self, rid: str, to: str, sets: str, args: tuple, from_status: str = "reserved") -> bool:
        with self._tx() as c:
            cur = c.execute(f"UPDATE reservations SET status = ?, {sets}, updated_at = ? WHERE id = ? AND status = ?",
                            (to, *args, self.clock(), rid, from_status))
            return cur.rowcount == 1  # 0이면 이미 처리된 예약 (중복 정산 방지)

    def settle(self, rid: str, cost: float, inp: int, out: int, cache_write: int, cache_read: int) -> bool:
        return self._transition(rid, "settled", "actual_cost = ?, actual_input = ?, actual_output = ?, cache_write = ?, "
                                "cache_read = ?, reason = NULL", (cost, inp, out, cache_write, cache_read))

    def mark_unknown(self, rid: str, reason: str) -> bool:
        return self._transition(rid, "unknown", "reason = ?", (reason,))

    def release(self, rid: str, reason: str) -> bool:
        """과금되지 않은 것이 확실한 요청 (보내기 전 차단, 429·4xx 거절)만."""
        return self._transition(rid, "released", "reason = ?", (reason,))

    def resolve_unknown(self, rid: str, *, cost: float | None = None, inp: int = 0, out: int = 0,
                        not_billed: bool = False) -> bool:
        """운영자 정산: 청구 내역으로 확인한 실제값으로 settled, 또는 과금 안 됨이 확인되면 released."""
        if not_billed:
            return self._transition(rid, "released", "reason = ?", ("operator_not_billed",), from_status="unknown")
        if cost is None or cost < 0:
            raise ValueError("cost 필요")
        return self._transition(rid, "settled", "actual_cost = ?, actual_input = ?, actual_output = ?, cache_write = 0, "
                                "cache_read = 0, reason = ?", (cost, inp, out, "operator_resolved"), from_status="unknown")

    def rows(self, status: str | None = None) -> list[dict]:
        with self._tx() as c:
            c.row_factory = sqlite3.Row
            q = "SELECT * FROM reservations" + (" WHERE status = ?" if status else "") + " ORDER BY created_at"
            return [dict(r) for r in c.execute(q, (status,) if status else ())]


_LEDGERS: dict[str, Ledger] = {}
_LEDGERS_LOCK = threading.Lock()


def get_ledger(cfg: BudgetConfig) -> Ledger:
    """프로세스당 원장 하나 (처음 열 때 재시작 복구). 실패하면 LedgerError."""
    with _LEDGERS_LOCK:
        led = _LEDGERS.get(cfg.ledger_path)
        if led is None:
            led = Ledger(cfg.ledger_path, cfg.shared, cfg.period).open()
            _LEDGERS[cfg.ledger_path] = led
        return led


def reset_ledgers() -> None:  # 테스트용 (프로세스 재시작 흉내)
    with _LEDGERS_LOCK:
        _LEDGERS.clear()


# ---------------------------------------------------------------- 실행별 예산


@dataclass
class _Res:
    id: str
    cost: float
    inp: int
    out: int


@dataclass
class RunBudget:
    """웹 실행 하나의 예산 (메모리) + 공용 원장. 스레드 안전 (Matcher 병렬 호출)."""
    run_id: str
    cfg: BudgetConfig
    ledger: Ledger
    clock: Callable[[], float] = time.monotonic
    started: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    stopped: str | None = None
    last_block_reason: str | None = None
    calls: int = 0
    confirmed_cost: float = 0.0
    unknown_cost: float = 0.0
    reserved_cost: float = 0.0
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "thinking": 0})
    held_input: int = 0  # 예약 중·UNKNOWN 입력 토큰 (최악값)
    held_output: int = 0
    unknown_count: int = 0
    blocked0: int = 0

    def __post_init__(self):
        self.started = self.clock()
        self.blocked0 = call_guard.counters()["blocked"]

    def _stop(self, code: str) -> BudgetStop:
        self.stopped = self.stopped or code
        self.last_block_reason = code
        return BudgetStop(code)

    def check(self) -> None:
        with self.lock:
            if self.stopped:
                raise BudgetStop(self.stopped)

    def reserve(self, est_in: int, max_out: int) -> _Res:
        r, p = self.cfg.run, self.cfg.price
        cost = p.worst(est_in, max_out)
        with self.lock:
            if self.stopped:
                raise BudgetStop(self.stopped)
            used_in = self.tokens["input"] + self.tokens["cache_write"] + self.tokens["cache_read"] + self.held_input
            code = ("run_time_cap" if self.clock() - self.started > self.cfg.run_max_seconds else
                    "run_call_cap" if self.calls + 1 > r.max_calls else
                    "run_input_token_cap" if used_in + est_in > r.max_input_tokens else
                    "run_output_token_cap" if self.tokens["output"] + self.held_output + max_out > r.max_output_tokens else
                    "run_cost_cap" if self.confirmed_cost + self.unknown_cost + self.reserved_cost + cost > r.max_cost_usd
                    else None)
            if code:
                raise self._stop(code)
            # 실행 한도 안: 먼저 실행 몫을 잡아 두고 공용 원장 예약 (실패하면 되돌림)
            self.calls += 1
            self.reserved_cost += cost
            self.held_input += est_in
            self.held_output += max_out
        try:
            rid = self.ledger.reserve(self.run_id, p.model, cost, est_in, max_out)
        except (BudgetStop, LedgerError) as e:
            with self.lock:
                self.calls -= 1
                self.reserved_cost -= cost
                self.held_input -= est_in
                self.held_output -= max_out
                code = e.args[0] if e.args else "ledger_unavailable"
                raise self._stop(code) from None
        return _Res(rid, cost, est_in, max_out)

    def _unhold(self, res: _Res) -> None:
        self.reserved_cost -= res.cost
        self.held_input -= res.inp
        self.held_output -= res.out

    def release(self, res: _Res, reason: str) -> None:
        """과금되지 않은 요청 (429·4xx): 예약 해제. 호출 수에서도 뺌."""
        with self.lock:
            self._unhold(res)
            self.calls -= 1
        self._ledger_call(self.ledger.release, res.id, reason)

    def unknown(self, res: _Res, reason: str, stop: bool = False) -> None:
        """결과 불명확: 예약액을 UNKNOWN으로 계속 점유 (0으로 보지 않음)."""
        with self.lock:
            self.reserved_cost -= res.cost
            self.unknown_cost += res.cost
            self.unknown_count += 1
            if stop:
                self._stop(reason)
        self._ledger_call(self.ledger.mark_unknown, res.id, reason)

    def settle(self, res: _Res, usage) -> bool:
        g = lambda k: int(getattr(usage, k, 0) or 0)  # noqa: E731
        inp, out, cw, cr = g("input_tokens"), g("output_tokens"), g("cache_creation_input_tokens"), g("cache_read_input_tokens")
        cost = self.cfg.price.cost(inp, out, cw, cr)
        think = int(getattr(getattr(usage, "output_tokens_details", None), "thinking_tokens", 0) or 0)
        with self.lock:
            self._unhold(res)
            self.confirmed_cost += cost
            for k, v in (("input", inp), ("output", out), ("cache_write", cw), ("cache_read", cr), ("thinking", think)):
                self.tokens[k] += v
        self._ledger_call(self.ledger.settle, res.id, cost, inp, out, cw, cr)
        return inp + cw + cr > res.inp or out > res.out  # 예약 추정이 실제보다 작았음 (실제값으로 정산은 함)

    def _ledger_call(self, fn, *args) -> None:
        try:
            ok = fn(*args)
        except LedgerError as e:
            with self.lock:
                self._stop(e.args[0] if e.args else "ledger_unavailable")
            return
        if not ok:  # 예약 상태가 예상과 다름 (이미 정산·해제됨) -> 원장 불일치로 보고 이후 호출 차단
            with self.lock:
                self._stop("ledger_inconsistent")

    def summary(self) -> dict:
        r = self.cfg.run
        with self.lock:
            confirmed, unknown, reserved = self.confirmed_cost, self.unknown_cost, self.reserved_cost
            status = ("ok" if not self.stopped else CATEGORY.get(self.stopped, BUDGET_ERROR))
            return {
                "budget_mode": "enforce", "budget_status": status, "cap_enforced": True,
                "estimated_cost_usd": round(confirmed + unknown + reserved, 6),
                "confirmed_cost_usd": round(confirmed, 6), "reserved_cost_usd": round(reserved, 6),
                "unknown_cost_usd": round(unknown, 6), "unknown_requests": self.unknown_count,
                "calls_used": self.calls, "calls_remaining": max(0, r.max_calls - self.calls),
                "token_usage": dict(self.tokens), "tokens_held": {"input": self.held_input, "output": self.held_output},
                "last_block_reason": self.last_block_reason,
                "blocked_unmetered_calls": call_guard.counters()["blocked"] - self.blocked0,
                "price_source": "operator_confirmed",
                "cost_note": "이 서버가 보내는 요청을 예약액 기준으로 막는 상한입니다. 외부 청구액의 절대 상한은 아닙니다.",
            }


# ---------------------------------------------------------------- 예산 통제 클라이언트


class _Endpoint:
    def __init__(self, owner: "BudgetClient", create):
        self._owner, self._create = owner, create

    def create(self, **kw):
        return self._owner._call(self._create, kw)


_FATAL_RESPONSE = ("model_mismatch", "fallback_detected", "service_tier_mismatch", "cache_ttl_mismatch",
                   "inference_geo_mismatch", "estimate_exceeded")


class BudgetClient:
    """llm.create가 쓰는 클라이언트 자리 (실행 컨텍스트). 모든 요청(재시도 포함)이 예약 -> 전송 -> 정산을 거침."""

    def __init__(self, inner, budget: RunBudget, meter=None, sleep: Callable[[float], None] = time.sleep):
        if getattr(inner, "max_retries", 0) != 0:  # SDK 자동 재시도는 예약 밖에서 일어나므로 허용하지 않음
            raise BudgetStop("sdk_retries_enabled")
        self._inner, self._budget, self._meter, self._sleep = inner, budget, meter, sleep
        self.messages = _Endpoint(self, inner.messages.create)
        beta = getattr(inner, "beta", None)
        self.beta = type("Beta", (), {"messages": _Endpoint(self, beta.messages.create)})() if beta else None

    def _call(self, inner_create, kw):
        from .. import web_metering
        b, cfg = self._budget, self._budget.cfg
        b.check()
        code = request_problem(kw)  # 대체 모델·허용 밖 옵션·서버 도구는 보내지 않음 (파일럿과 같은 검사)
        if code:
            with b.lock:
                raise b._stop(code)
        model = kw.get("model")
        if model != cfg.price.model:
            with b.lock:
                raise b._stop("model_price_mismatch")
        stage = web_metering.stage_of(kw.get("system"))
        est_in = request_tokens_bound(kw)
        max_out = int(kw.get("max_tokens") or 16000)
        kw = {**kw, "timeout": cfg.call_timeout_s}
        for attempt in range(1 + cfg.max_retries):
            res = b.reserve(est_in, max_out)
            t0 = time.monotonic()
            try:
                with call_guard.mark("metered", self._inner) as mk:
                    resp = inner_create(**kw)
            except Exception as e:  # noqa: BLE001 - 종류만 다룸
                if possibly_billed(e):
                    b.unknown(res, "response_lost")  # 응답 없음: 과금됐을 수 있음 -> 점유 유지
                else:
                    b.release(res, type(e).__name__)  # 429·4xx: 처리 전 거절
                if self._meter is not None:
                    self._meter.failed(stage, e, mk, time.monotonic() - t0)
                if is_retryable(e) and attempt < cfg.max_retries:
                    self._sleep(min(2 ** attempt, 4))
                    continue
                raise
            except BaseException:  # 요청 도중 중단 (KeyboardInterrupt 등): 결과 불명확
                b.unknown(res, "interrupted", stop=True)
                raise
            # 사용량 누락·다른 모델·대체 모델·등급·1시간 캐시·지역 추론: 정산하지 않고 UNKNOWN으로 점유, 다음 요청부터 막음
            code = response_problem(resp, model)
            if code:
                b.unknown(res, code, stop=True)
            elif b.settle(res, resp.usage):
                with b.lock:
                    b._stop("estimate_exceeded")
            if self._meter is not None:
                self._meter.succeeded(stage, kw, resp, mk, time.monotonic() - t0, self._inner)
            if b.stopped in _FATAL_RESPONSE:
                raise BudgetStop(b.stopped)
            return resp
        raise BudgetStop("response_lost")  # 도달하지 않음


def make_inner():
    """웹 기본 SDK 클라이언트의 재시도 0 사본 (자격 증명·설정은 같음). 실행마다 따로 만들어 서로 공유하지 않음.

    llm.client()는 다른 enforce 실행의 배타 관문에 막히므로 기본 클라이언트 객체를 직접 씀 (없으면 생성)."""
    base = llm._client
    if base is None:
        import anthropic
        base = llm._client = anthropic.Anthropic(max_retries=4)  # llm.client()와 같은 기본값 (일반 경로용)
    return base.with_options(max_retries=0) if hasattr(base, "with_options") else base


@contextlib.contextmanager
def run_context(run_id: str, cfg: BudgetConfig, ledger: Ledger, inner, meter=None, clock=time.monotonic):
    """이 실행(과 copy_context로 넘긴 하위 스레드)의 LLM 호출을 BudgetClient로. 실행 동안 프로세스 전체 fail-closed."""
    budget = RunBudget(run_id, cfg, ledger, clock=clock)
    proxy = BudgetClient(inner, budget, meter)
    with call_guard.register(proxy, "metered", exclusive=True), llm.use_client(proxy):
        yield budget
