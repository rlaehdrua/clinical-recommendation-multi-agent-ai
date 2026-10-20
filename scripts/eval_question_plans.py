"""기존 질문 생성 경로 vs Decision Loop 질문 계획 - 오프라인 비교 평가 실행기 (Phase 4-1).

오프라인 대역(LLM 없음) 구조 평가입니다. 결과를 실제 질문 성능·추천 정확도·비용 개선으로 해석하지 마세요.
기본은 화면 출력만 하고 파일을 쓰지 않습니다(--out을 줄 때만 ID·숫자·상태 코드만 저장).

    PYTHONPATH=src python scripts/eval_question_plans.py [--cases ID ...] [--rounds 2] [--budget 3] [--out DIR]
    PYTHONPATH=src python scripts/eval_question_plans.py --dry-run-cost [--max-cost-usd 50]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import question_eval as qe  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", nargs="*", help="평가할 사례 ID (없으면 전체)")
    ap.add_argument("--sample", type=int, help="무작위로 고를 사례 수 (--seed로 고정)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--budget", type=int, default=3, help="라운드당 질문 수 (두 방식 동일)")
    ap.add_argument("--case-timeout", type=float, default=30.0)
    ap.add_argument("--out", type=Path, help="요약 JSON·사례별 CSV 저장 위치 (주지 않으면 저장 안 함)")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--ablation", action="store_true", help="평가 전용 변형(동점을 프로토콜 순서로)도 함께 실행")
    ap.add_argument("--dry-run-cost", action="store_true")
    ap.add_argument("--max-cost-usd", type=float)
    ap.add_argument("--approve-paid-run", action="store_true", help="유료 실행 승인 (이번 단계에서는 실행 경로 없음)")
    args = ap.parse_args(argv)

    parsed = qe_parsed = load_frozen()
    if args.validate_only:
        print(json.dumps(qe.validate_cases(parsed=qe_parsed), ensure_ascii=False, indent=1))
        return
    if args.dry_run_cost or args.approve_paid_run:
        cases = qe.load_cases(parsed)
        avg = sum(len(g.candidate_trials) for _, g in cases) / len(cases)
        est = qe.estimate_cost(len(cases), avg, args.rounds, cap_usd=args.max_cost_usd)
        print(json.dumps(est, ensure_ascii=False, indent=1))
        if args.approve_paid_run:
            qe.require_paid_approval(True, est)  # 이번 단계에서는 항상 거절
        return
    only = args.cases
    if args.sample:
        ids = [g.case_id for _, g in qe.load_cases(parsed)]
        only = sorted(random.Random(args.seed).sample(ids, min(args.sample, len(ids))))
    all_trials = load_trials()
    result = qe.run_evaluation(parsed, {t: all_trials[t] for t in parsed}, only=only, rounds=args.rounds,
                               budget=args.budget, case_timeout_s=args.case_timeout, ablation=args.ablation)
    view = {k: v for k, v in result.items() if k not in ("case_rows", "case_diffs", "question_records")}
    print(json.dumps(view, ensure_ascii=False, indent=1))
    if args.out:
        for p in qe.save_outputs(result, args.out):
            print("saved:", p)


if __name__ == "__main__":
    main()
