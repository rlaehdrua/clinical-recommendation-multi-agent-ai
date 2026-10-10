"""질문 예산(acquisition budget)별 성능 곡선.

환자 1명당 허용 질문 수를 0, 1, 2, ...로 바꿔 같은 평가셋을 반복 실행하고,
예산마다 정확도·Rescue·Cleanup·안전 지표·LLM 호출·토큰·실제 질문 수를 한 표로 모읍니다.
"질문을 많이 해서 좋아졌다"와 "적은 질문으로 대부분의 개선을 얻었다"를 구분하기 위한 실험입니다.

- 실행은 재현성을 위해 fixed 모드 + 가상 환자 응답(simulated)으로 고정합니다.
- 예산이 실제 제약이 되도록 라운드 한도(--max-rounds)는 기본 3으로 넉넉하게 둡니다.
- 이미 결과가 있는 예산은 --reuse로 다시 실행하지 않고 평가만 할 수 있습니다.

사용:
    python scripts/budget_sweep.py --budgets 0,1,2,3,5 --workers 4
    python scripts/budget_sweep.py --budgets 0,1,2,3,5 --reuse      # 실행 없이 기존 결과만 집계
    # 정보 가리기 평가셋으로 실행 (숨긴 사실 회수율도 함께 집계)
    python scripts/budget_sweep.py --patients-dir data/patients/masked --labels data/labels/masked_labels.csv \
        --out outputs/budget_sweep_masked

출력: <out>/b<예산>/ (환자별 결과), <out>/frontier.csv, <out>/frontier.json
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

from ctrec.metrics import (eligibility_metrics, fact_acquisition_metrics, fmt, load_label_rows, load_labels,
                           load_predictions, load_reveals, load_usage)

COLUMNS = [
    "budget", "pairs", "accuracy", "coverage", "decided_accuracy", "rescue", "rescue_n", "cleanup", "cleanup_n",
    "false_removal", "false_removal_n", "premature_match", "premature_match_n",
    "questions_per_patient", "eligibility_questions_per_patient", "calls_per_patient",
    "input_tokens", "output_tokens", "masked_fact_recovery", "useful_question_rate",
]


def run_budget(budget: int, out_dir: Path, args) -> None:
    cmd = [
        sys.executable, "-m", "ctrec", "batch",
        "--patients-dir", args.patients_dir, "--trials-dir", args.trials_dir,
        "--mode", "fixed", "--answers", "simulated",
        "--question-budget", str(budget), "--max-rounds", str(args.max_rounds),
        "--out", str(out_dir), "--workers", str(args.workers), "--quiet",
    ]
    print(f"\n=== 질문 예산 {budget} 실행: {' '.join(cmd[2:])}")
    subprocess.run(cmd, check=True)


def summarize(budget: int, out_dir: Path, gold: dict, text_only: dict, rows: list[dict]) -> dict:
    pred, _, _ = load_predictions(out_dir)
    m = eligibility_metrics(gold, pred, text_only)
    u = load_usage(out_dir)
    n = u.get("patients") or 1
    row = {k: m.get(k) for k in COLUMNS if k in m}
    row.update({
        "budget": budget,
        "questions_per_patient": u.get("questions", 0) / n,
        "eligibility_questions_per_patient": u.get("eligibility_questions", 0) / n,
        "calls_per_patient": u.get("calls", 0) / n,
        "input_tokens": u.get("input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
    })
    acq = fact_acquisition_metrics(rows, load_reveals(out_dir)) or {}
    row["masked_fact_recovery"] = acq.get("masked_fact_recovery")
    row["useful_question_rate"] = acq.get("useful_question_rate")
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budgets", default="0,1,2,3,5", help="쉼표로 구분한 환자당 질문 예산 목록")
    ap.add_argument("--patients-dir", default="data/patients/synthetic")
    ap.add_argument("--trials-dir", default="data/trials")
    ap.add_argument("--labels", default="data/labels/synthetic_labels.csv")
    ap.add_argument("--out", default="outputs/budget_sweep")
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--reuse", action="store_true", help="결과가 이미 있는 예산은 다시 실행하지 않음")
    args = ap.parse_args()

    budgets = [int(b) for b in args.budgets.split(",")]
    gold = load_labels(Path(args.labels), "label")
    text_only = load_labels(Path(args.labels), "label_text_only")
    label_rows = load_label_rows(Path(args.labels))
    out_root = Path(args.out)

    rows = []
    for b in budgets:
        out_dir = out_root / f"b{b}"
        if not (args.reuse and any(out_dir.glob("*/result.json"))):
            run_budget(b, out_dir, args)
        rows.append(summarize(b, out_dir, gold, text_only, label_rows))

    out_root.mkdir(parents=True, exist_ok=True)
    with open(out_root / "frontier.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    (out_root / "frontier.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n예산  정확도  Rescue        Cleanup      FalseRemoval  PrematureMatch  질문/명  호출/명  사실회수")
    for r in rows:
        print(f"{r['budget']:>4}  {fmt(r['accuracy'])}  {fmt(r['rescue'])} ({r['rescue_n']:>5})  "
              f"{fmt(r['cleanup'])} ({r['cleanup_n']:>4})  {fmt(r['false_removal'])} ({r['false_removal_n']:>5})  "
              f"{fmt(r['premature_match'])} ({r['premature_match_n']:>5})  "
              f"{r['questions_per_patient']:>6.2f}  {r['calls_per_patient']:>6.1f}  {fmt(r['masked_fact_recovery'])}")
    print(f"\n-> {out_root / 'frontier.csv'}")


if __name__ == "__main__":
    main()
