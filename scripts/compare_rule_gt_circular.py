"""시뮬레이터 순환 규칙 GT(runner.rule_labels)와 독립 규칙 GT(pair_gt.csv)를 쌍별로 대조 (읽기 전용).

  python scripts/compare_rule_gt_circular.py [--out data/nsclc/eval/checks/circular_vs_independent.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import build_nsclc_eval as B  # noqa: E402
from ctrec.cohort import generate as gen  # noqa: E402
from ctrec.cohort import gt as GT  # noqa: E402
from ctrec.cohort.spec import Cohort  # noqa: E402

TO_GT = {"eligible": GT.CONFIRMED_ELIGIBLE, "eligible_pending_expert": GT.PENDING_EXPERT_ELIGIBLE,
         "ineligible": GT.INELIGIBLE, "unlabeled": GT.NOT_EVALUATED}


def main() -> int:
    from ctrec.engine.bundles import SourceCatalog
    from ctrec.sim import runner
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    c = Cohort.model_validate(json.loads(B.GENERATED.read_text(encoding="utf-8")))
    _, sd, _, _ = gen.load_inputs(B.CONFIG)
    sim = B.sim_cohort(c, sd)
    circ = runner.rule_labels(sim, SourceCatalog())
    with open(B.EVAL / "pair_gt.csv", encoding="utf-8", newline="") as f:
        ind = {r["sim_pair_key"]: r for r in csv.DictReader(f)}
    cells = Counter((circ[k], ind[k]["gt_label"]) for k in ind)
    diff = [{"sim_pair_key": k, "patient_id": ind[k]["patient_id"], "trial_id": ind[k]["trial_id"], "circular": circ[k],
             "independent": ind[k]["gt_label"], "reasons": ind[k]["reasons"]}
            for k in ind if TO_GT.get(circ[k]) != ind[k]["gt_label"]]
    out = {"pairs": len(ind), "agree": len(ind) - len(diff), "cells": {f"{x}|{y}": n for (x, y), n in sorted(cells.items())},
           "disagreements": diff, "note": "순환 GT = 같은 엔진 평가기로 정답 사실을 판정. 불일치 원인은 엔진 판정 경로라 미검증"}
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("pairs", "agree", "cells")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
