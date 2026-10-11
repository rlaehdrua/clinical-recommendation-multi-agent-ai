"""후보 수(D3 Exploration Pool 예산) 실험: 후보 추림 순위의 recall@k.

후보 추림은 환자당 한 번 전체 시험 풀을 훑어 순위를 매기고, 그중 상위 k개만 자세히 평가합니다.
상위 k개가 정답 시험을 얼마나 남기는지(recall)와, k에 비례해 늘어나는 매칭 호출 수를 비교합니다.
환자마다 추림을 한 번만 실행(상한 없이 전체 순위)하고 k별 결과는 그 순위에서 계산하므로, k마다 다시 실행하지 않습니다.

선택 규칙(팀 회의 제안): 적격 시험 recall과 같은 분야 시험 recall이 모두 --target 이상인 가장 작은 k.

    python scripts/screen_budget.py --ks 3,5,8,26
    -> outputs/screen_budget/ (환자별 순위 rankings.json, summary.json)
"""

from __future__ import annotations

import argparse
import contextvars
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ctrec import llm
from ctrec.agents import patient_profiler, screener
from ctrec.pipeline import load_patients
from ctrec.tools import ctgov


def recall_at(rankings: dict[str, list[str]], gold: list[dict], k: int) -> dict:
    eligible = [(r["patient_id"], r["trial_id"]) for r in gold if r["label"] == "ELIGIBLE"]
    relevant = [(r["patient_id"], r["trial_id"]) for r in gold if r.get("auto", "0") == "0"]

    def rate(pairs):
        pairs = [(p, t) for p, t in pairs if p in rankings]
        hit = sum(t in rankings[p][:k] for p, t in pairs)
        return (hit / len(pairs) if pairs else None), len(pairs)

    (re_, ne), (rr, nr) = rate(eligible), rate(relevant)
    return {"k": k, "recall_eligible": re_, "eligible_pairs": ne, "recall_relevant": rr, "relevant_pairs": nr,
            "matcher_calls_per_round": k}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--patients-dir", default="data/patients/synthetic")
    ap.add_argument("--trials-dir", default="data/trials")
    ap.add_argument("--labels", default="data/labels/synthetic_labels_all.csv")
    ap.add_argument("--ks", default="3,5,8,26")
    ap.add_argument("--target", type=float, default=0.95)
    ap.add_argument("--out", default="outputs/screen_budget")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--reuse", action="store_true", help="저장된 순위로 집계만")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    trials = {t["trial_id"]: t for t in (ctgov.load_local(p) for p in sorted(Path(args.trials_dir).glob("*.json")))}
    with open(args.labels, encoding="utf-8") as f:
        gold = list(csv.DictReader(f))
    cases = [c for p in sorted(Path(args.patients_dir).iterdir()) if p.suffix == ".json" for c in load_patients(p)]
    cases = [c for c in cases if any(r["patient_id"] == c.patient_id for r in gold)]

    rank_path = out / "rankings.json"
    rankings: dict[str, list[str]] = json.loads(rank_path.read_text()) if args.reuse and rank_path.exists() else {}
    llm.reset_usage()

    def work(case):
        profile = patient_profiler.profile_patient(case.patient_id, case.raw_text)
        r = screener.screen(profile, trials, len(trials), case.raw_text)
        return case.patient_id, [s.trial_id for s in r.selected]  # 상한이 풀 크기라 selected = keep 전체 순위

    todo = [c for c in cases if c.patient_id not in rankings]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for pid, ranked in pool.map(lambda c: contextvars.copy_context().run(work, c), todo):
            rankings[pid] = ranked
    rank_path.write_text(json.dumps(rankings, ensure_ascii=False, indent=1), encoding="utf-8")

    rows = [recall_at(rankings, gold, int(k)) for k in args.ks.split(",")]
    ok = [r["k"] for r in rows if (r["recall_eligible"] or 0) >= args.target and (r["recall_relevant"] or 0) >= args.target]
    summary = {"patients": len(rankings), "rows": rows, "target": args.target,
               "suggested_k": min(ok) if ok else None, "usage": llm.usage_snapshot()}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"환자 {len(rankings)}명, 시험 풀 {len(trials)}개")
    print(f"{'k':>4}{'적격 recall':>14}{'같은 분야 recall':>18}{'매칭 호출/라운드':>18}")
    for r in rows:
        fmt = lambda v: "-" if v is None else f"{v:.3f}"
        print(f"{r['k']:>4}{fmt(r['recall_eligible']):>14}{fmt(r['recall_relevant']):>18}{r['k']:>18}")
    print(f"\n제안 k (두 recall 모두 {args.target:.0%} 이상인 가장 작은 값): {summary['suggested_k']}")
    print(f"사용량: {summary['usage']}\n-> {out / 'summary.json'}")


if __name__ == "__main__":
    main()
