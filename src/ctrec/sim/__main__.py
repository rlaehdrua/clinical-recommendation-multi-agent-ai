"""정책 비교표: python -m ctrec.sim [--budgets 20,45,90,400] [--seeds 1,2] [--rephrase off|on|both] [--out DIR]
생성기 코호트: python -m ctrec.sim --generator data/cohort/generator_config.json --scenario ID [--replicate 0]
                [--map data/cohort/source_map.json] [--include-draft]

기본 입력은 합성 데모(sim.demo)입니다. 결과는 합성 조건·가정 비용·순환 규칙 GT에서의 **정책 간 상대 비교**이며, 임상
정확도나 업무시간 절감의 근거가 아닙니다. --rephrase는 "모름" 뒤 다른 말로 한 번 더 물을지 (기본 off, 민감도 both).
--out을 주면 실행마다 manifest.json과 decision_records.jsonl을 남깁니다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..engine import audit
from ..engine.baselines import policies
from ..engine.contracts import EffortBudget
from . import demo, runner

COLS = ["policy_id", "rephrase", "budget_minutes", "seed", "confirmed_eligible", "confirmed_ineligible",
        "coordinator_minutes", "elapsed_minutes", "actions", "questions", "rephrased", "no_answer",
        "rule_gt_disagreements", "termination", "audit"]


REPHRASE_MODES = {"off": [False], "on": [True], "both": [False, True]}


def _ints(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def load_cohort(a):
    if not a.generator:
        return demo.cohort()
    from ..cohort import generate as G
    from . import from_generator as FG
    cfg, sd, cat, parsed = G.load_inputs(a.generator)
    sc = next((s for s in cfg.scenarios if s.scenario_id == a.scenario), None)
    if sc is None:
        raise SystemExit(f"시나리오 없음: {a.scenario!r} (가능: {[s.scenario_id for s in cfg.scenarios]})")
    gc = G.generate_cohort(cfg, sd, cat, parsed, sc, a.replicate, G.config_hash(cfg, sd, cat))
    return FG.to_sim_cohort(gc, sd, FG.load_source_map(a.map), include_draft=a.include_draft)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ctrec.sim", description=__doc__.splitlines()[0])
    ap.add_argument("--budgets", default="20,45,90,400", help="코디네이터 분 (쉼표)")
    ap.add_argument("--seeds", default="1", help="응답 seed (쉼표)")
    ap.add_argument("--rephrase", choices=sorted(REPHRASE_MODES), default="off",
                    help='"모름" 뒤 다른 말로 한 번 더 물을지 (both = 끈 것과 켠 것 모두)')
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--policies", default=",".join(sorted(policies())))
    ap.add_argument("--generator", default=None, help="생성기 설정 (없으면 합성 데모)")
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--replicate", type=int, default=0)
    ap.add_argument("--map", default="data/cohort/source_map.json", help="슬롯·정보원 대응표")
    ap.add_argument("--include-draft", action="store_true", help="판정기에 검수 전(draft) 연결도 씀 (결과에 표시됨)")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    co = load_cohort(a)
    budgets = [EffortBudget(coordinator_minutes=m, clinician_minutes=60, horizon_steps=a.horizon)
               for m in _ints(a.budgets)]
    pids = [p for p in a.policies.split(",") if p]
    modes = REPHRASE_MODES[a.rephrase]
    if a.out is not None:
        for rp_on in modes:
            for b in budgets:
                for seed in _ints(a.seeds):
                    for pid in pids:
                        cfg = {"rephrase": True} if rp_on else {}
                        r = runner.run(runner.build_spec(f"{pid}-{int(b.coordinator_minutes)}-{seed}", pid, co, b, seed,
                                                         cfg), co)
                        d = a.out / f"{pid}-r{int(rp_on)}-b{int(b.coordinator_minutes)}-s{seed}"
                        d.mkdir(parents=True, exist_ok=True)
                        (d / "manifest.json").write_text(json.dumps({**r.manifest, "metrics": r.metrics},
                                                                    ensure_ascii=False, indent=1), encoding="utf-8")
                        path = d / "decision_records.jsonl"
                        if path.exists():
                            path.unlink()
                        w = audit.RecordWriter(path, r.manifest["run_id"])
                        for rec in r.records:
                            w.write(rec)
    rows = runner.compare(co, pids, budgets, _ints(a.seeds), rephrase=modes)
    print("\t".join(COLS))
    for row in rows:
        print("\t".join(str(row[c]) for c in COLS))
    judge = "규칙 엔진 + 결정적 판정기" if co.judge is not None else "규칙 엔진 범위만"
    print(f"# {runner.CLAIMS}: {co.cohort_id}, 환자 {len(co.patients)}명 x 시험 {len(co.trials)}개, {judge}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
