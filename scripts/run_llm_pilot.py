"""실제 LLM 파일럿 실행 입구 (Phase 5-1). 기본은 계획·계획 해시·승인 게이트 상태만 출력합니다.

--execute를 주면 llm_pilot.run_pilot으로 들어가지만, 승인 게이트(12개 조건)와 코드 스위치
PAID_EXECUTION_ENABLED = False 때문에 이번 단계에서는 항상 거절되고 API 클라이언트를 만들지 않습니다.
결과 파일 저장은 --save-results를 줄 때만 (비식별 검사를 지난 결과만, 임시 파일 뒤 교체).

--fake-sdk: 같은 계획을 이 프로세스에서 오프라인 Fake SDK로 실제 실행 경로(승인 게이트 제외)대로 돌림 (유료 호출 없음).
파일럿 전용 프로세스 검증용 - 프로세스 보호(fail-closed)·상한·중단 처리가 웹과 분리된 프로세스에서도 같은지 확인.
단가 기본값은 공식 문서 단가(사용자 확인 전, confirmed=False). 확인 문구는 --price-confirmed 뒤 계획 해시가 정해질 때만 나옴.

    PYTHONPATH=src python scripts/run_llm_pilot.py --experiment A_pipeline \\
        --cases S-NCT07369063-I9-biomarker S-NCT05127980-E1-cohort \\
        --price-input 2 --price-output 10 --price-cache-write 2.5 --price-cache-read 0.2 \\
        --price-source "<공식 가격표 확인 기록>" --price-confirmed \\
        --max-cost-usd 3 --max-calls 30 --approved-by "<승인 기록>" --confirm "RUN A_pipeline <해시 12자>" --execute
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402
from prepare_llm_pilot import EGFR_ADDON, PATHS, REPRESENTATIVE, make_plan  # noqa: E402
from prepare_paid_pilot import select  # noqa: E402

from ctrec import call_guard, config, llm, llm_pilot as lp, llm_pilot_exec as lpx, question_eval as qe  # noqa: E402
from ctrec.llm_pilot_fake import FAULTS, INTERRUPT, RESPONSE_FAULTS, FakeSDKClient  # noqa: E402

# 단가 외에 공식 문서로 확인하지 못한 항목 (보수적으로 처리하지만 실행 전 사용자가 알아야 함)
UNKNOWN_ITEMS = {
    "failed_request_billing": "429·5xx·시간 초과 요청의 과금 여부 (공식 문서에 없음) -> 응답 없는 실패는 최악값으로 과금 처리",
    "sonnet5_refusal_classifier": "claude-sonnet-5의 안전 분류기 거절 여부 (거절·대체 모델 문서의 대상 모델 목록에 없음) "
                                  "-> 대체 모델 끔, 거절은 RefusalError로 사례 실패",
    "real_token_counts": "실제 프로필 크기·출력·사고 토큰 (추정치 사용)",
    "cache_hit_rate": "프롬프트 캐시 적중 (시스템 프롬프트 길이가 최소 캐시 길이를 넘는지 모델별 확인 필요)",
}


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
    base = lp.documented_price() or lp.ASSUMED_PRICE

    def pick(v, d):
        return d if v is None else v
    price = lp.PriceTable(model=config.MODEL, input=pick(args.price_input, base.input),
                          output=pick(args.price_output, base.output),
                          cache_write=pick(args.price_cache_write, base.cache_write),
                          cache_read=pick(args.price_cache_read, base.cache_read),
                          confirmed=args.price_confirmed, source=args.price_source or base.source)
    measured = lp.measure_offline(pairs, trials)
    est = {sc: lp.estimate(measured, sc, price)["experiments"][args.experiment] for sc in ("base", "worst")}
    lim = lp.suggest_limits(est["worst"], est["base"])
    if args.max_cost_usd is not None:
        lim = replace(lim, max_cost_usd=args.max_cost_usd)
    if args.max_calls is not None:
        lim = replace(lim, max_calls=args.max_calls)
    if args.run_deadline_s is not None:
        lim = replace(lim, run_deadline_s=args.run_deadline_s)
    plan = make_plan(args.experiment, [gt.case_id for _, gt in pairs], lim, price)
    plan["estimate_usd"] = {sc: e["usd"] for sc, e in est.items()}
    plan["estimate_tokens"] = {sc: {"input": e["input_tokens"], "output": e["output_tokens"], "calls": e["calls"]}
                               for sc, e in est.items()}
    return plan, price


def readiness(plan: dict, price: lp.PriceTable, problems: list[str]) -> dict:
    """실행 가능 상태 판정. 단가 미확인·게이트 문제·코드 스위치 꺼짐 중 하나라도 있으면 False."""
    reasons = list(problems)
    if not price.confirmed:
        reasons.append("단가 사용자 확인 전 (공식 문서 값이어도 확정 단가로 취급하지 않음)")
    if plan.get("model") != config.MODEL:
        reasons.append("계획 모델이 설정 모델과 다름")
    if plan.get("model") != lp.PILOT_MODEL:
        reasons.append(f"파일럿 고정 모델({lp.PILOT_MODEL})이 아님")
    if not lp.PAID_EXECUTION_ENABLED:
        reasons.append("PAID_EXECUTION_ENABLED = False")
    return {"ready_to_execute": not reasons, "reasons": reasons, "unknown_items": UNKNOWN_ITEMS}


def run_fake(args, plan, price, pairs, trials) -> dict:
    """전용 프로세스 검증: 승인 게이트만 빼고 실제 실행 경로(execute + 프로세스 보호)를 Fake SDK로."""
    def faults(i, stage):
        return args.fake_fault if args.fake_fault and i == args.fake_fault_at else None

    def on_call(i, kw):
        if args.fake_delay:
            time.sleep(args.fake_delay)
    fake = FakeSDKClient(faults=faults, on_call=on_call)
    result = lpx.execute(plan, price, lp.PilotLimits(**plan["limits"]), fake, pairs, trials,
                         save_to=args.save_results)
    return {"result": result, "fake_requests": len(fake.calls),
            "process": {"pid": os.getpid(), "web_app_loaded": "ctrec.web.app" in sys.modules,
                        "guard_after": call_guard.counters(), "client_context_after": llm._client_var.get() is None}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--experiment", choices=sorted(lp.EXPERIMENTS), required=True)
    ap.add_argument("--cases", nargs="+", default=[c for c, _ in REPRESENTATIVE[:3]])
    ap.add_argument("--price-input", type=float)  # 기본: 공식 문서 단가 (없으면 가정값)
    ap.add_argument("--price-output", type=float)
    ap.add_argument("--price-cache-write", type=float)
    ap.add_argument("--price-cache-read", type=float)
    ap.add_argument("--price-source")
    ap.add_argument("--price-confirmed", action="store_true", help="사용자가 공식 가격표로 단가를 확인했음")
    ap.add_argument("--run-deadline-s", type=float)
    ap.add_argument("--fake-sdk", action="store_true", help="오프라인 Fake SDK로 이 프로세스에서 실행 (유료 호출 없음)")
    ap.add_argument("--fake-fault", choices=sorted(set(FAULTS) | set(RESPONSE_FAULTS) | {INTERRUPT}))
    ap.add_argument("--fake-fault-at", type=int, default=0, help="장애를 넣을 요청 순번 (0부터)")
    ap.add_argument("--fake-delay", type=float, default=0.0, help="Fake 요청마다 대기 (초)")
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
    problems = lp.approval_problems(plan, approval)
    out = {"plan": plan, "gate_problems": problems, "paid_execution_enabled": lp.PAID_EXECUTION_ENABLED,
           "readiness": readiness(plan, price, problems),
           "first_batch_note": f"첫 실제 배치는 {lp.FIRST_BATCH_RECOMMENDED_MAX}개 이하 권장"}
    if args.execute and args.fake_sdk:
        raise SystemExit("--execute와 --fake-sdk는 함께 쓸 수 없음")
    try:
        if args.execute:
            try:
                out["result"] = lp.run_pilot(plan, approval, pairs=pairs, trials=trials, save_to=args.save_results)
            except lp.PaidRunNotApproved as e:
                out["refused"] = str(e)
        elif args.fake_sdk:
            out["fake_run"] = run_fake(args, plan, price, pairs, trials)
    except KeyboardInterrupt:  # 중단: 보호 장치·컨텍스트는 with 블록이 이미 복구, 결과 파일은 쓰지 않음
        out["interrupted"] = True
        out["process"] = {"guard_after": call_guard.counters(), "client_context_after": llm._client_var.get() is None}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        raise SystemExit(130)
    lp.assert_redacted(out, lp.forbidden_texts(pairs))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


if __name__ == "__main__":
    main()
