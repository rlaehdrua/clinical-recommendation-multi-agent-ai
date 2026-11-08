import csv, json, sys
from pathlib import Path
repo, out, tag = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(repo/"src")); sys.path.insert(0, str(repo/"scripts"))
import build_nsclc_eval as B
from ctrec.cohort import generate as gen
from ctrec.cohort.spec import Cohort
from ctrec.engine.contracts import EffortBudget
from ctrec.sim import runner
c = Cohort.model_validate(json.loads(B.GENERATED.read_text(encoding="utf-8")))
_, sd, _, _ = gen.load_inputs(B.CONFIG)
sim = B.sim_cohort(c, sd)
POL, BUD, HOR = sys.argv[4], float(sys.argv[5]), int(sys.argv[6])
spec = runner.build_spec(f"r-{POL}", POL, sim, EffortBudget(coordinator_minutes=BUD, horizon_steps=HOR), seed=1)
r = runner.run(spec, sim)
final = {p.pair.key: p.status.value for p in r.final.pairs()}
gt = {x["sim_pair_key"]: x for x in csv.DictReader(open(B.EVAL/"pair_gt.csv", encoding="utf-8"))}
ci = sorted(k for k, v in final.items() if v == "CONFIRMED_INELIGIBLE")
from collections import Counter
res = {"tag": tag, "cohort_id": sim.cohort_id, "policy": POL, "budget": BUD, "horizon": HOR, "seed": 1,
       "status_final": dict(Counter(final.values())), "runner_status_final": r.metrics.get("status_final"),
       "confirmed_ineligible": len(ci), "ci_and_gt_ineligible": sum(gt[k]["gt_label"] == "INELIGIBLE" for k in ci),
       "result_hash": r.manifest.get("result_hash") if hasattr(r, "manifest") else None,
       "actions": len(r.records), "final": final,
       "records": [{"step": x.step, "alias": x.action.patient_alias, "action": x.action.action_type, "source": x.action.source_id,
                    "info_key": x.action.info_key, "targets": list(x.action.target_criteria),
                    "outcome": x.outcome.status if x.outcome else None,
                    "pairs": [f"{t.pair.trial_id}:{t.before.value}->{t.after.value}" for t in x.pairs]} for x in r.records]}
out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
print(tag, res["confirmed_ineligible"], res["ci_and_gt_ineligible"], res["status_final"], res["actions"], res["result_hash"])
