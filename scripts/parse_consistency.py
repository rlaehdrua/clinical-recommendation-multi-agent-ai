"""기준 파싱 일관성 측정 + 전체 시험 사전 파싱 (팀 결정 D1·D8 근거 자료).

같은 시험을 같은 모델·프롬프트로 여러 번 파싱해, 결과가 얼마나 흔들리는지 측정합니다.
마지막 실행 결과는 파싱 캐시(data/cache/parsed/)에 남으므로, 실행이 끝나면 모든 시험이 사전 파싱된 상태가 됩니다.

시험별로 비교하는 항목
- 규칙 수, underspecified(원문이 애매하다고 표시한) 규칙 수, 코호트 수, 규칙 엔진용 구조화 규칙 수
- 규칙 단위 일치: 여러 실행에 공통으로 나온 규칙(원문 문장 기준) 중 underspecified·category·구조화 필드가 모두 같은 비율

    python scripts/parse_consistency.py --runs 3 --workers 4
    -> outputs/parse_consistency/ (실행별 원본, summary.json, summary.csv)
"""

from __future__ import annotations

import argparse
import contextvars
import csv
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ctrec import llm
from ctrec.agents import criteria_parser
from ctrec.schemas import ParsedTrial
from ctrec.tools import ctgov


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _sig(rule) -> tuple:
    return (rule.underspecified, rule.category, rule.field, rule.operator, rule.value, rule.cohort is not None)


def compare(runs: list[ParsedTrial]) -> dict:
    counts = {
        "rules": [len(p.rules) for p in runs],
        "underspecified": [sum(r.underspecified for r in p.rules) for p in runs],
        "cohorts": [len(p.cohorts) for p in runs],
        "structured": [sum(r.field is not None for r in p.rules) for p in runs],
        "integrity_issues": [len(p.integrity_issues) for p in runs],
    }
    by_run = [{_key(r.text): r for r in p.rules} for p in runs]
    common = set.intersection(*(set(b) for b in by_run)) if by_run else set()
    union = set.union(*(set(b) for b in by_run)) if by_run else set()
    same = [k for k in common if len({_sig(b[k]) for b in by_run}) == 1]
    unstable = sorted(
        (f"{by_run[0][k].rule_id}: " + " vs ".join(
            f"under={b[k].underspecified},cat={b[k].category},field={b[k].field}" for b in by_run))
        for k in common if k not in same)
    return {
        **{k: v for k, v in counts.items()},
        "spread": {k: max(v) - min(v) for k, v in counts.items()},
        "rule_text_overlap": len(common) / len(union) if union else 1.0,  # 규칙 분할 자체가 같은 정도
        "rule_attr_agreement": len(same) / len(common) if common else None,  # 공통 규칙의 속성 일치율
        "unstable_rules": unstable[:10],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials-dir", default="data/trials")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="outputs/parse_consistency")
    ap.add_argument("--reuse", action="store_true", help="이미 저장된 실행 결과는 다시 파싱하지 않음")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    trials = [ctgov.load_local(p) for p in sorted(Path(args.trials_dir).glob("*.json"))]
    llm.reset_usage()

    def work(trial: dict):
        runs = []
        for i in range(1, args.runs + 1):
            path = out / f"{trial['trial_id']}_run{i}.json"
            if args.reuse and path.exists():
                p = ParsedTrial.model_validate_json(path.read_text(encoding="utf-8"))
                p.integrity_issues = criteria_parser.check_integrity(p, trial)
            else:
                # use_cache=False: 매번 새로 파싱하고 캐시를 덮어씀 -> 마지막 실행이 사전 파싱 결과로 남음
                p = criteria_parser.parse_trial(trial, use_cache=False)
                path.write_text(p.model_dump_json(indent=1), encoding="utf-8")
            runs.append(p)
        return trial["trial_id"], compare(runs)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        # copy_context: 스레드에서도 토큰 사용량이 이 실행에 합산되도록
        futures = [pool.submit(contextvars.copy_context().run, work, t) for t in trials]
        results = dict(f.result() for f in futures)

    (out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    cols = ["trial_id", "rules", "underspecified", "cohorts", "structured", "rule_text_overlap", "rule_attr_agreement"]
    with open(out / "summary.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for tid, r in results.items():
            w.writerow([tid, r["rules"], r["underspecified"], r["cohorts"], r["structured"],
                        f"{r['rule_text_overlap']:.2f}", "" if r["rule_attr_agreement"] is None else f"{r['rule_attr_agreement']:.2f}"])

    print(f"{'시험':<14}{'규칙 수':<14}{'underspec':<14}{'코호트':<10}{'분할 일치':>9}{'속성 일치':>9}")
    for tid, r in results.items():
        agree = "-" if r["rule_attr_agreement"] is None else f"{r['rule_attr_agreement']:.2f}"
        print(f"{tid:<14}{str(r['rules']):<14}{str(r['underspecified']):<14}{str(r['cohorts']):<10}"
              f"{r['rule_text_overlap']:>9.2f}{agree:>9}")
    n = len(results)
    print(f"\n시험 {n}개 × {args.runs}회")
    print(f"  규칙 수가 실행마다 다른 시험:            {sum(r['spread']['rules'] > 0 for r in results.values())}")
    print(f"  underspecified 수가 다른 시험:           {sum(r['spread']['underspecified'] > 0 for r in results.values())}"
          f" (3개 이상 차이: {sum(r['spread']['underspecified'] >= 3 for r in results.values())})")
    print(f"  코호트 구조가 다른 시험:                 {sum(r['spread']['cohorts'] > 0 for r in results.values())}")
    print(f"  평균 규칙 분할 일치율:                   {sum(r['rule_text_overlap'] for r in results.values()) / n:.2f}")
    agrees = [r["rule_attr_agreement"] for r in results.values() if r["rule_attr_agreement"] is not None]
    if agrees:
        print(f"  평균 공통 규칙 속성 일치율:              {sum(agrees) / len(agrees):.2f}")
    print(f"  사용량: {llm.usage_snapshot()}")
    print(f"-> {out / 'summary.json'}")


if __name__ == "__main__":
    main()
