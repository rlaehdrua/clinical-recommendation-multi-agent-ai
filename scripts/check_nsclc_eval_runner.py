"""NSCLC 평가셋이 #20 runner에서 돌아가는지 확인하고, 결과를 독립 규칙 GT로 대조합니다 (시뮬레이터 코드는 바꾸지 않음).

  python scripts/check_nsclc_eval_runner.py [--policies no_action,full_dcvc] [--budget 400] [--seed 1] [--out x.json]

- runner CLI(python -m ctrec.sim)는 시험을 data/parsed에서 읽으므로 이 평가셋(축소 저장소)을 바로 못 씀 ->
  라이브러리 호출로 to_sim_cohort(..., trials=load_trials(ids, store_dir=평가 저장소))를 넘깁니다.
- runner 내부 규칙 GT(rule_labels, 순환)는 그대로 두고, RunResult.final의 쌍 상태를 pair_gt.csv와 따로 대조합니다.
- 결과는 정책 간 상대 비교용 건수이며 정확도가 아닙니다.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import build_nsclc_eval as B  # noqa: E402
from ctrec.cohort import generate as gen  # noqa: E402
from ctrec.cohort import gt as GT  # noqa: E402
from ctrec.cohort.spec import Cohort  # noqa: E402


def load_gt() -> dict[str, str]:
    with open(B.EVAL / "pair_gt.csv", encoding="utf-8", newline="") as f:
        return {r["sim_pair_key"]: r["gt_label"] for r in csv.DictReader(f)}


def run(policies: list[str], budget: float, seed: int, horizon: int = 10) -> dict:
    from ctrec.engine.contracts import EffortBudget
    from ctrec.sim import runner
    c = Cohort.model_validate(json.loads(B.GENERATED.read_text(encoding="utf-8")))
    _, sd, _, _ = gen.load_inputs(B.CONFIG)
    sim = B.sim_cohort(c, sd)
    gt = load_gt()
    out = {"cohort_id": sim.cohort_id, "budget_minutes": budget, "seed": seed, "horizon": horizon, "runs": {}}
    for pid in policies:
        spec = runner.build_spec(f"nsclc-eval-check-{pid}", pid, sim, EffortBudget(coordinator_minutes=budget,
                                                                                   horizon_steps=horizon), seed=seed)
        r = runner.run(spec, sim)
        final = {p.pair.key: p.status.value for p in r.final.pairs()}
        out["runs"][pid] = {"status_final": r.metrics["status_final"], "actions": r.metrics.get("actions"),
                            "audit": r.manifest.get("evaluator", {}).get("ground_truth"),
                            "runner_rule_gt_kind": r.metrics["rule_gt"]["kind"],
                            "independent_gt": GT.score(final, gt)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policies", default="no_action,full_dcvc")
    ap.add_argument("--budget", type=float, default=400)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    res = run(a.policies.split(","), a.budget, a.seed, a.horizon)
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
