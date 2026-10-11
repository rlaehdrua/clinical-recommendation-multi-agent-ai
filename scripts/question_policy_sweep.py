"""라운드당 질문 수 정책(D4) 비교: single(항상 1개) vs batch(한도까지 묶음) vs adaptive(독립적인 질문만 묶음).

같은 환자 표본·같은 총 질문 예산으로 정책만 바꿔 실행하고, 정확도·Rescue·숨긴 사실 회수율과 함께
라운드 수(사용자 상호작용 횟수)·LLM 호출 수를 비교합니다.

- 정보 가리기 평가셋에서 case_type별로 고르게 표본을 뽑습니다(--per-type, 고정 seed).
- 재현성을 위해 fixed 모드 + 가상 환자 응답, 보고서 작성은 생략(CTREC_WRITE_REPORT=0)합니다.

    python scripts/question_policy_sweep.py --policies single,batch,adaptive --budget 3 --per-type 5
    -> outputs/question_policy/<정책>/, outputs/question_policy/summary.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from budget_sweep import summarize
from ctrec.metrics import load_label_rows, load_labels

COLUMNS = ["policy", "pairs", "accuracy", "coverage", "rescue", "rescue_n", "false_removal", "premature_match",
           "questions_per_patient", "rounds_per_patient", "calls_per_patient", "masked_fact_recovery",
           "useful_question_rate"]


def sample_cases(path: Path, per_type: int, seed: int) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    by_type = defaultdict(list)
    for c in cases:
        by_type[c.get("case_type", "full")].append(c)
    rng = random.Random(seed)
    out = []
    for t in sorted(by_type):
        out += rng.sample(by_type[t], min(per_type, len(by_type[t])))
    return out


def rounds_per_patient(out_dir: Path) -> float:
    rounds = [max((qa["round"] for qa in json.loads(p.read_text())["qa_log"]), default=0)
              for p in out_dir.glob("*/result.json")]
    return sum(rounds) / len(rounds) if rounds else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policies", default="single,batch,adaptive")
    ap.add_argument("--budget", type=int, default=3, help="환자당 총 질문 수")
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--per-round", type=int, default=3, help="batch·adaptive의 라운드당 한도")
    ap.add_argument("--cases", default="data/patients/masked/masked_cases.json")
    ap.add_argument("--labels", default="data/labels/masked_labels.csv")
    ap.add_argument("--per-type", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="outputs/question_policy")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--reuse", action="store_true")
    args = ap.parse_args()

    out_root = Path(args.out)
    subset_dir = out_root / "patients"
    subset_dir.mkdir(parents=True, exist_ok=True)
    subset = sample_cases(Path(args.cases), args.per_type, args.seed)
    (subset_dir / "sample.json").write_text(json.dumps(subset, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"표본 {len(subset)}명: " + ", ".join(c["patient_id"] for c in subset))

    gold = load_labels(Path(args.labels), "label")
    text_only = load_labels(Path(args.labels), "label_text_only")
    label_rows = load_label_rows(Path(args.labels))

    rows = []
    for policy in args.policies.split(","):
        out_dir = out_root / policy
        if not (args.reuse and any(out_dir.glob("*/result.json"))):
            env = {**os.environ, "CTREC_QUESTION_POLICY": policy, "CTREC_WRITE_REPORT": "0",
                   "CTREC_MAX_QUESTIONS": str(args.per_round)}
            cmd = [sys.executable, "-m", "ctrec", "batch", "--patients-dir", str(subset_dir),
                   "--trials-dir", "data/trials", "--mode", "fixed", "--answers", "simulated",
                   "--candidates", "given", "--question-budget", str(args.budget),
                   "--max-rounds", str(args.max_rounds), "--out", str(out_dir),
                   "--workers", str(args.workers), "--quiet"]
            print(f"\n=== 정책 {policy}")
            subprocess.run(cmd, check=True, env=env)
        row = summarize(args.budget, out_dir, gold, text_only, label_rows)
        row["policy"] = policy
        row["rounds_per_patient"] = rounds_per_patient(out_dir)
        rows.append({k: row.get(k) for k in COLUMNS})

    with open(out_root / "summary.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    (out_root / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    print(f"\n-> {out_root / 'summary.csv'}")


if __name__ == "__main__":
    main()
