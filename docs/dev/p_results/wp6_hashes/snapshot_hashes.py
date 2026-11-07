"""WP6 결과 해시 스냅샷 (리팩터 전후 비교용)."""
import json, sys
from ctrec.sim import demo, runner
from ctrec.engine.contracts import EffortBudget
from ctrec.engine.baselines import policies
from ctrec.cohort import generate as G
from ctrec.sim import from_generator as FG
out = {}
cos = [demo.cohort()]
cfg, sd, cat, parsed = G.load_inputs("data/cohort/generator_config.json")
smap = FG.load_source_map("data/cohort/source_map.json"); h = G.config_hash(cfg, sd, cat)
for sc in cfg.scenarios[1:2]:
    cos.append(FG.to_sim_cohort(G.generate_cohort(cfg, sd, cat, parsed, sc, 0, h), sd, smap))
pids = sorted(policies()) + ["oracle_upper"]
for ci, co in enumerate(cos):
    for m in (20, 400):
        b = EffortBudget(coordinator_minutes=m, clinician_minutes=60, horizon_steps=10)
        for rp in (False, True):
            for pid in pids:
                cfg_ = {"rephrase": True} if rp else {}
                r = runner.run(runner.build_spec(f"h-{pid}", pid, co, b, 1, cfg_), co)
                out[f"{ci}|{m}|{int(rp)}|{pid}"] = r.result_hash
json.dump(out, open(sys.argv[1], "w"), indent=0, sort_keys=True)
print(len(out))
