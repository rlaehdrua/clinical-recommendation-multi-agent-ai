"""선우 dba06d9 원자료 재집계 (실행 없음). 사용: python reagg.py <sy_b 폴더> <out.json>"""
import glob, json, statistics as st, sys
from collections import defaultdict
from pathlib import Path
R, out = Path(sys.argv[1]), Path(sys.argv[2])
res = {}
for coh, d in (("nsclc_pilot", "e1_nsclc_out"), ("generator", "e1_gen_out")):
    g = defaultdict(list)
    for f in sorted(glob.glob(str(R / d / "*" / "result.json"))):
        row = json.load(open(f, encoding="utf-8"))["row"]
        g[(row["arm"], row["budget_minutes"])].append((row["system_confirmed_ineligible"], row["verified_ineligible"],
            row.get("false_ineligible"), row["seed"], row.get("cohort_index"), row["head"] if "head" in row else None,
            str(Path(f).relative_to(R)).replace("\\", "/"), row["cohort"]))
    for (arm, b), v in sorted(g.items()):
        ci = [x[0] for x in v]
        res[f"{coh}|{arm}|{int(b)}"] = {"n": len(v), "CI_mean": round(st.mean(ci), 3), "CI_sd": round(st.stdev(ci), 2) if len(ci) > 1 else 0.0,
            "CI_values": ci, "VI_values": [x[1] for x in v], "FI_values": [x[2] for x in v], "cohorts": sorted({x[7] for x in v}),
            "files": [x[6] for x in v]}
w = json.load(open(R / "wp6_eval_400.json", encoding="utf-8"))
res["_wp6_400_head"] = {k: w[k] for k in ("head", "ctrec", "gt_kind")}
for r in w["rows"]:
    res[f"eval240|{r.get('arm')}|400"] = {k: r.get(k) for k in ("arm", "system_confirmed_ineligible", "verified_ineligible", "false_ineligible", "correct_expert_ready", "result_hash", "seed")}
out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
for k, v in res.items():
    if k.startswith("_"): print(k, v); continue
    if any(a in k for a in ("|dcvc|", "|dcvc-no_bundles|", "|dcvc-no_dcv|")):
        print(k, {kk: v[kk] for kk in v if kk in ("n", "CI_mean", "CI_sd", "CI_values", "system_confirmed_ineligible", "verified_ineligible", "result_hash")})
