"""웹 비용 원장 운영 도구 (Phase 6-3). 원장 파일만 읽고 쓰며 LLM API를 부르지 않습니다.

    PYTHONPATH=src python scripts/web_budget_ledger.py summary
    PYTHONPATH=src python scripts/web_budget_ledger.py unknown
    PYTHONPATH=src python scripts/web_budget_ledger.py resolve <예약 ID> --cost 0.0123 [--input N --output N]
    PYTHONPATH=src python scripts/web_budget_ledger.py resolve <예약 ID> --not-billed

설정은 웹 서버와 같은 환경 변수(CTREC_WEB_BUDGET_LEDGER, CTREC_WEB_SHARED_*, CTREC_WEB_BUDGET_PERIOD)를 읽습니다.
출력에는 메타데이터(실행·예약 ID, 모델, 토큰, 금액, 상태, 시각, 사유 코드)만 있습니다.
UNKNOWN 정산은 Anthropic Console 사용량·청구 내역으로 실제 값을 확인한 뒤에만 하세요.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ctrec.web.budget import Ledger, Limits, window_key


def _ledger() -> Ledger:
    path = os.getenv("CTREC_WEB_BUDGET_LEDGER", "").strip()
    if not path or not os.path.exists(path):
        raise SystemExit("CTREC_WEB_BUDGET_LEDGER 파일이 없습니다.")
    g = lambda n, c: c(os.getenv(n) or 0)  # noqa: E731
    lim = Limits(g("CTREC_WEB_SHARED_MAX_USD", float), g("CTREC_WEB_SHARED_MAX_CALLS", int),
                 g("CTREC_WEB_SHARED_MAX_INPUT_TOKENS", int), g("CTREC_WEB_SHARED_MAX_OUTPUT_TOKENS", int))
    # 운영 도구는 재시작 복구를 하지 않음 (서버 프로세스의 진행 중 예약을 UNKNOWN으로 바꾸지 않도록 같은 boot_id를 모름)
    return Ledger(path, lim, os.getenv("CTREC_WEB_BUDGET_PERIOD", "day"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    sub.add_parser("unknown")
    r = sub.add_parser("resolve")
    r.add_argument("reservation_id")
    r.add_argument("--cost", type=float)
    r.add_argument("--input", type=int, default=0)
    r.add_argument("--output", type=int, default=0)
    r.add_argument("--not-billed", action="store_true")
    args = ap.parse_args(argv)
    led = _ledger()
    if args.cmd == "summary":
        win = window_key(led.period)
        by = {}
        for row in led.rows():
            by.setdefault(row["status"], 0)
            by[row["status"]] += 1
        out = {"window": win, "used": led.used(win), "rows_by_status": by, "limits": vars(led.limits)}
    elif args.cmd == "unknown":
        out = [{k: row[k] for k in ("id", "run_id", "window", "model", "est_cost", "est_input", "est_output", "reason",
                                    "created_at")} for row in led.rows("unknown")]
    else:
        if not args.not_billed and args.cost is None:
            raise SystemExit("--cost 또는 --not-billed가 필요합니다.")
        ok = led.resolve_unknown(args.reservation_id, cost=args.cost, inp=args.input, out=args.output,
                                 not_billed=args.not_billed)
        out = {"resolved": ok}
        if not ok:
            print(json.dumps(out, ensure_ascii=False))
            return 1
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
