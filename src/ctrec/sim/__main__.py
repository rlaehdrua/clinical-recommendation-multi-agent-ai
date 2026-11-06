"""데모 코호트 정책 비교: python -m ctrec.sim [--budgets 20,45,90,400] [--seeds 1,2] [--out DIR]

합성 데모(sim.demo) 점검용입니다. 결과는 합성 조건·가정 비용·순환 규칙 GT에서의 **정책 간 상대 비교**이며, 임상 정확도나
업무시간 절감의 근거가 아닙니다. --out을 주면 실행마다 manifest.json과 decision_records.jsonl을 남깁니다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..engine import audit
from ..engine.baselines import policies
from ..engine.contracts import EffortBudget
from . import demo, runner

COLS = ["policy_id", "budget_minutes", "seed", "confirmed_eligible", "confirmed_ineligible", "coordinator_minutes",
        "elapsed_minutes", "actions", "questions", "rule_gt_disagreements", "termination", "audit"]


def _ints(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ctrec.sim", description=__doc__.splitlines()[0])
    ap.add_argument("--budgets", default="20,45,90,400", help="코디네이터 분 (쉼표)")
    ap.add_argument("--seeds", default="1", help="응답 seed (쉼표)")
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--policies", default=",".join(sorted(policies())))
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    co = demo.cohort()
    budgets = [EffortBudget(coordinator_minutes=m, clinician_minutes=60, horizon_steps=a.horizon)
               for m in _ints(a.budgets)]
    pids = [p for p in a.policies.split(",") if p]
    if a.out is not None:
        for b in budgets:
            for seed in _ints(a.seeds):
                for pid in pids:
                    r = runner.run(runner.build_spec(f"demo-{pid}-{int(b.coordinator_minutes)}-{seed}", pid, co, b,
                                                     seed), co)
                    d = a.out / f"{pid}-b{int(b.coordinator_minutes)}-s{seed}"
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "manifest.json").write_text(json.dumps({**r.manifest, "metrics": r.metrics},
                                                                ensure_ascii=False, indent=1), encoding="utf-8")
                    path = d / "decision_records.jsonl"
                    if path.exists():
                        path.unlink()
                    w = audit.RecordWriter(path, r.manifest["run_id"])
                    for rec in r.records:
                        w.write(rec)
    rows = runner.compare(co, pids, budgets, _ints(a.seeds))
    print("\t".join(COLS))
    for row in rows:
        print("\t".join(str(row[c]) for c in COLS))
    print(f"# {runner.CLAIMS}: 합성 데모 {len(co.patients)}명 x {len(co.trials)}개 시험, 규칙 엔진 범위만 평가")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
