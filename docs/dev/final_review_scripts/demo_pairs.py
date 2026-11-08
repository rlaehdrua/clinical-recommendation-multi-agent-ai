import json, sys
from pathlib import Path
repo, out = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo/"scripts")); sys.path.insert(0, str(repo/"src"))
import demo_nsclc_scenarios as D
c, sim, final, logs = D.run_log()
rev = {v: k for k, v in sim.aliases().items()}
want = {"P045": "NCT06928389", "P061": "NCT06216301", "P052": "NCT07680790", "P058": "NCT07416058"}
res = {}
for al, lg in logs.items():
    pid = rev.get(al, al)
    if pid in want:
        t = want[pid]
        res[pid] = {"trial": t, "final": final[f"{al}|{t}"],
                    "other": {k.split("|")[1]: v for k, v in final.items() if k.startswith(al + "|")},
                    "actions": [x for x in lg if any(y.startswith(t) for y in x["targets"]) or not x["targets"]]}
out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
for p, r in res.items(): print(p, r["trial"], r["final"], r["other"], len(r["actions"]))
