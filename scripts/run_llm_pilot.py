"""실제 LLM 파일럿 실행 입구 (Phase 5-1). 기본은 계획·계획 해시·승인 게이트 상태만 출력합니다.

--execute를 주면 llm_pilot.run_pilot으로 들어가지만, 승인 게이트(12개 조건)와 코드 스위치
PAID_EXECUTION_ENABLED = False 때문에 이번 단계에서는 항상 거절되고 API 클라이언트를 만들지 않습니다.
결과 파일 저장은 --save-results를 줄 때만 (비식별 검사를 지난 결과만).

    PYTHONPATH=src python scripts/run_llm_pilot.py --experiment A_pipeline \\
        --cases S-NCT07369063-I9-biomarker S-NCT05127980-E1-cohort \\
        --price-input 2 --price-output 10 --price-cache-write 2.5 --price-cache-read 0.2 \\
        --price-source "<공식 가격표 확인 기록>" --price-confirmed \\
        --max-cost-usd 3 --max-calls 30 --approved-by "<승인 기록>" --confirm "RUN A_pipeline <해시 12자>" --execute
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402
from prepare_llm_pilot import EGFR_ADDON, PATHS, REPRESENTATIVE, make_plan  # noqa: E402
from prepare_paid_pilot import select  # noqa: E402

from ctrec import config, llm_pilot as lp, question_eval as qe  # noqa: E402


def load_selected(parsed, case_ids: list[str]):
    pool = {c["case_id"]: c["set"] for c in select(parsed)}
    pool[EGFR_ADDON[1]] = EGFR_ADDON[0]
    pairs = []
    for cid in case_ids:
        if cid not in lp.PILOT_CASE_POOL or cid not in pool:
            raise SystemExit(f"승인 후보 사례가 아님: {cid}")
        pairs += qe.load_cases(parsed, PATHS[pool[cid]], only=[cid])
    return pairs


def build_plan(args, pairs, trials) -> tuple[dict, lp.PriceTable]:
    price = lp.PriceTable(model=config.MODEL, input=args.price_input, output=args.price_output,
                          cache_write=args.price_cache_write, cache_read=args.price_cache_read,
                          confirmed=args.price_confirmed, source=args.price_source)
    measured = lp.measure_offline(pairs, trials)
    est = {sc: lp.estimate(measured, sc, price)["experiments"][args.experiment] for sc in ("base", "worst")}
    lim = lp.suggest_limits(est["worst"], est["base"])
    if args.max_cost_usd is not None:
        lim = replace(lim, max_cost_usd=args.max_cost_usd)
    if args.max_calls is not None:
        lim = replace(lim, max_calls=args.max_calls)
    plan = make_plan(args.experiment, [gt.case_id for _, gt in pairs], lim, price)
    plan["estimate_usd"] = {sc: e["usd"] for sc, e in est.items()}
    return plan, price


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--experiment", choices=sorted(lp.EXPERIMENTS), required=True)
    ap.add_argument("--cases", nargs="+", default=[c for c, _ in REPRESENTATIVE[:3]])
    ap.add_argument("--price-input", type=float, default=lp.ASSUMED_PRICE.input)
    ap.add_argument("--price-output", type=float, default=lp.ASSUMED_PRICE.output)
    ap.add_argument("--price-cache-write", type=float, default=lp.ASSUMED_PRICE.cache_write)
    ap.add_argument("--price-cache-read", type=float, default=lp.ASSUMED_PRICE.cache_read)
    ap.add_argument("--price-source", default="assumed")
    ap.add_argument("--price-confirmed", action="store_true")
    ap.add_argument("--max-cost-usd", type=float)
    ap.add_argument("--max-calls", type=int)
    ap.add_argument("--approved-by", default="")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--save-results", type=Path)
    args = ap.parse_args(argv)

    parsed = load_frozen()
    all_trials = load_trials()
    trials = {t: all_trials[t] for t in parsed}
    pairs = load_selected(parsed, args.cases)
    plan, price = build_plan(args, pairs, trials)
    approval = (lp.PilotApproval(approved_by=args.approved_by, experiment=args.experiment, plan_hash=plan["plan_hash"],
                                 price=price, limits=lp.PilotLimits(**plan["limits"]), confirmation=args.confirm)
                if args.approved_by else None)
    out = {"plan": plan, "gate_problems": lp.approval_problems(plan, approval),
           "paid_execution_enabled": lp.PAID_EXECUTION_ENABLED,
           "first_batch_note": f"첫 실제 배치는 {lp.FIRST_BATCH_RECOMMENDED_MAX}개 이하 권장"}
    if args.execute:
        try:
            out["result"] = lp.run_pilot(plan, approval, pairs=pairs, trials=trials, save_to=args.save_results)
        except lp.PaidRunNotApproved as e:
            out["refused"] = str(e)
    lp.assert_redacted(out, lp.forbidden_texts(pairs))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


if __name__ == "__main__":
    main()
