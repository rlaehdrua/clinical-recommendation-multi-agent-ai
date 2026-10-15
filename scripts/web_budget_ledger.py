"""웹 비용 원장 운영 도구 (Phase 6-3, 6-5). 원장 파일만 읽고 쓰며 LLM API를 부르지 않습니다.

    PYTHONPATH=src python scripts/web_budget_ledger.py init                  # 새 원장 만들기 (이미 있으면 거절)
    PYTHONPATH=src python scripts/web_budget_ledger.py summary
    PYTHONPATH=src python scripts/web_budget_ledger.py unknown
    PYTHONPATH=src python scripts/web_budget_ledger.py resolve <예약 ID> --cost 0.0123 [--input N --output N]
    PYTHONPATH=src python scripts/web_budget_ledger.py resolve <예약 ID> --not-billed
    PYTHONPATH=src python scripts/web_budget_ledger.py backup <백업 파일>     # 서버 실행 중에도 가능 (일관된 사본)
    PYTHONPATH=src python scripts/web_budget_ledger.py verify <원장 또는 백업 파일>
    PYTHONPATH=src python scripts/web_budget_ledger.py restore <백업 파일> --confirm   # 서버를 멈춘 뒤에만

설정은 웹 서버와 같은 환경 변수(CTREC_WEB_BUDGET_LEDGER, CTREC_WEB_SHARED_*, CTREC_WEB_BUDGET_PERIOD)를 읽습니다.
출력에는 메타데이터(실행·예약 ID, 모델, 토큰, 금액, 상태, 시각, 사유 코드)만 있습니다.
UNKNOWN 정산은 Anthropic Console 사용량·청구 내역으로 실제 값을 확인한 뒤에만 하세요.
행을 지우는 명령은 없습니다. 복구는 기존 원장을 지우지 않고 옆에 보관합니다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ctrec.web.budget import (Ledger, LedgerError, Limits, inspect_ledger, process_lock, release_process_lock,
                              window_key)


def _path() -> str:
    path = os.getenv("CTREC_WEB_BUDGET_LEDGER", "").strip()
    if not path:
        raise SystemExit("CTREC_WEB_BUDGET_LEDGER가 설정되지 않았습니다.")
    return path


def _ledger() -> Ledger:
    path = _path()
    if not os.path.exists(path):
        raise SystemExit("CTREC_WEB_BUDGET_LEDGER 파일이 없습니다. 처음이면 init으로 만드세요.")
    g = lambda n, c: c(os.getenv(n) or 0)  # noqa: E731
    lim = Limits(g("CTREC_WEB_SHARED_MAX_USD", float), g("CTREC_WEB_SHARED_MAX_CALLS", int),
                 g("CTREC_WEB_SHARED_MAX_INPUT_TOKENS", int), g("CTREC_WEB_SHARED_MAX_OUTPUT_TOKENS", int))
    # 조회·정산은 open()(잠금·재시작 복구)을 하지 않음 -> 실행 중인 서버의 진행 중 예약을 건드리지 않음
    return Ledger(path, lim, os.getenv("CTREC_WEB_BUDGET_PERIOD", "day"))


def _iso(ts) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds") if ts else None


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_db(src: str, dst: str) -> None:
    """SQLite 온라인 백업 API: 쓰는 중인 원장도 트랜잭션 단위로 일관된 사본을 만듦 (파일 복사와 다름)."""
    s = sqlite3.connect(Path(os.path.abspath(src)).as_uri() + "?mode=ro", uri=True, timeout=10)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def cmd_init(args) -> dict:
    path = _path()
    if os.path.exists(path):
        raise SystemExit("원장이 이미 있습니다. 덮어쓰지 않습니다.")
    Ledger(path, Limits(1, 1, 1, 1), "day").open(create=True)
    os.chmod(path, 0o600)  # 서버 실행 계정만 읽고 쓰게 (POSIX; Windows에서는 읽기 전용 여부만 바뀜)
    return {"created": True, "check": inspect_ledger(path)}


def cmd_summary(args) -> dict:
    led = _ledger()
    win = window_key(led.period)
    rows = led.rows()
    by: dict = {}
    for row in rows:
        by[row["status"]] = by.get(row["status"], 0) + 1
    unknown = [r for r in rows if r["status"] == "unknown"]
    return {"window": win, "used": led.used(win), "rows_by_status": by, "limits": vars(led.limits),
            "unknown_held_usd": round(sum(r["est_cost"] for r in unknown), 6),
            "oldest_unknown_at": _iso(min((r["created_at"] for r in unknown), default=None))}


def cmd_unknown(args) -> list:
    now = time.time()
    return [{**{k: row[k] for k in ("id", "run_id", "window", "model", "est_cost", "est_input", "est_output", "reason")},
             "created_at_utc": _iso(row["created_at"]), "updated_at_utc": _iso(row["updated_at"]),
             "age_hours": round((now - row["created_at"]) / 3600, 1)}
            for row in _ledger().rows("unknown")]


def cmd_resolve(args):
    if not args.not_billed and args.cost is None:
        raise SystemExit("--cost 또는 --not-billed가 필요합니다.")
    ok = _ledger().resolve_unknown(args.reservation_id, cost=args.cost, inp=args.input, out=args.output,
                                   not_billed=args.not_billed)
    return {"resolved": ok}, (0 if ok else 1)


def cmd_backup(args) -> dict:
    src, dst = _path(), args.dest
    try:
        inspect_ledger(src)  # 손상된 원장은 백업하지 않음 (좋은 백업으로 착각할 위험)
    except LedgerError as e:
        raise SystemExit(f"원장이 올바르지 않아 백업하지 않습니다: {e.args[0]}") from None
    if os.path.exists(dst):
        raise SystemExit("백업 파일이 이미 있습니다. 덮어쓰지 않습니다.")
    tmp = dst + ".partial"
    if os.path.exists(tmp):
        os.remove(tmp)
    _copy_db(src, tmp)
    check = inspect_ledger(tmp)
    os.replace(tmp, dst)
    digest = _sha256(dst)
    Path(dst + ".sha256").write_text(f"{digest}  {os.path.basename(dst)}\n", encoding="ascii")
    return {"backup": dst, "sha256": digest, "check": check, "last_updated_at_utc": _iso(check["last_updated_at"])}


def verify_backup(path: str) -> dict:
    """백업 검증: 체크섬 파일과 일치 + SQLite 무결성 + 스키마. 하나라도 어긋나면 SystemExit."""
    side = Path(path + ".sha256")
    if not side.is_file():
        raise SystemExit("체크섬 파일(<백업>.sha256)이 없어 백업을 확인할 수 없습니다.")
    if side.read_text(encoding="ascii").split()[0] != _sha256(path):
        raise SystemExit("체크섬이 맞지 않습니다. 손상되었거나 다른 파일입니다.")
    try:
        return inspect_ledger(path)
    except LedgerError as e:
        raise SystemExit(f"백업이 올바른 원장이 아닙니다: {e.args[0]}") from None


def cmd_verify(args) -> dict:
    if os.path.exists(args.file + ".sha256"):
        return {"file": args.file, "check": verify_backup(args.file), "checksum": "ok"}
    try:
        return {"file": args.file, "check": inspect_ledger(args.file), "checksum": "none"}
    except LedgerError as e:
        raise SystemExit(f"올바른 원장이 아닙니다: {e.args[0]}") from None


def cmd_restore(args) -> dict:
    if not args.confirm:
        raise SystemExit("복구는 --confirm을 붙여 명시적으로 실행해야 합니다. 서버를 먼저 멈추세요.")
    path = _path()
    check = verify_backup(args.backup)
    try:
        process_lock(path)  # 서버(또는 다른 도구)가 원장을 쓰는 중이면 실패
    except LedgerError as e:
        raise SystemExit(f"원장을 사용하는 프로세스가 있습니다({e.args[0]}). 서버를 멈춘 뒤 다시 실행하세요.") from None
    try:
        kept = None
        if os.path.exists(path):  # 기존 원장은 지우지 않고 보관
            kept = f"{path}.before-restore-{time.strftime('%Y%m%d-%H%M%S')}"
            os.replace(path, kept)
        tmp = path + ".restoring"
        _copy_db(args.backup, tmp)
        os.replace(tmp, path)
        # 백업 시점에 진행 중이던 예약은 결과를 알 수 없음 -> UNKNOWN (예약액 점유 유지)
        c = sqlite3.connect(path, isolation_level=None)
        try:
            c.execute("BEGIN IMMEDIATE")
            n = c.execute("UPDATE reservations SET status = 'unknown', reason = 'restored_from_backup', updated_at = ? "
                          "WHERE status = 'reserved'", (time.time(),)).rowcount
            c.execute("COMMIT")
        finally:
            c.close()
        after = inspect_ledger(path)
    finally:
        release_process_lock(path)
    return {"restored_from": args.backup, "previous_ledger_kept_as": kept, "reserved_to_unknown": n, "check": after,
            "backup_last_updated_at_utc": _iso(check["last_updated_at"]),
            "warning": "백업 시점 이후의 예약·정산은 이 원장에 없습니다. 그동안의 사용액은 Console 청구 내역으로 확인하세요."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("summary")
    sub.add_parser("unknown")
    r = sub.add_parser("resolve")
    r.add_argument("reservation_id")
    r.add_argument("--cost", type=float)
    r.add_argument("--input", type=int, default=0)
    r.add_argument("--output", type=int, default=0)
    r.add_argument("--not-billed", action="store_true")
    sub.add_parser("backup").add_argument("dest")
    sub.add_parser("verify").add_argument("file")
    rs = sub.add_parser("restore")
    rs.add_argument("backup")
    rs.add_argument("--confirm", action="store_true")
    args = ap.parse_args(argv)
    out = {"init": cmd_init, "summary": cmd_summary, "unknown": cmd_unknown, "resolve": cmd_resolve,
           "backup": cmd_backup, "verify": cmd_verify, "restore": cmd_restore}[args.cmd](args)
    code = 0
    if isinstance(out, tuple):
        out, code = out
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
