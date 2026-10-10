"""정답 라벨을 전체 시험 풀로 확장 (후보 추림 방식 평가용).

기존 라벨은 환자마다 같은 질환 분야의 시험 2–3개에만 있습니다. 후보 추림(--candidates screen)으로 전체 시험을
후보로 두면 다른 분야 시험에도 정답이 필요하므로, 라벨이 없는 (환자, 시험) 쌍을 INELIGIBLE로 채웁니다.
근거: 다른 질환 분야 시험의 대상 질환을 환자가 갖고 있지 않음. 자동 라벨은 auto=1로 표시하며 연구진 검토가 필요합니다.

    python scripts/expand_labels.py
    -> data/labels/synthetic_labels_all.csv
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from ctrec.tools import ctgov


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="data/labels/synthetic_labels.csv")
    ap.add_argument("--trials-dir", default="data/trials")
    ap.add_argument("--out", default="data/labels/synthetic_labels_all.csv")
    args = ap.parse_args()

    with open(args.labels, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    trials = sorted(ctgov.load_local(p)["trial_id"] for p in Path(args.trials_dir).glob("*.json"))
    labeled = {(r["patient_id"], r["trial_id"]) for r in rows}
    patients = sorted({r["patient_id"] for r in rows})

    out = [{**r, "auto": "0"} for r in rows]
    for pid in patients:
        for tid in trials:
            if (pid, tid) not in labeled:
                out.append({"patient_id": pid, "trial_id": tid, "label": "INELIGIBLE", "label_text_only": "INELIGIBLE",
                            "rationale": "자동 라벨: 다른 질환 분야 시험 (연구진 검토 필요)", "auto": "1"})
    fields = list(rows[0]) + ["auto"]
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    print(f"기존 {len(rows)}쌍 + 자동 {len(out) - len(rows)}쌍 = {len(out)}쌍 (환자 {len(patients)}명 × 시험 {len(trials)}개)")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
