"""데모 재확인 실행(scripts/demo_nsclc_scenarios.py)을 그대로 돌리면서 같은 실행의 쌍별 최종 상태와 엔진 관찰 2건을 함께 저장.
사용: python demo_with_final.py <repo> <out_dir> <tag>
"""
import csv
import json
import sys
from collections import Counter
from pathlib import Path

repo, out_dir, tag = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(repo / "scripts"))
sys.path.insert(0, str(repo / "src"))
import demo_nsclc_scenarios as D  # noqa: E402

cap = {}
_orig = D.run_log


def run_log():
    r = _orig()
    cap["r"] = r
    return r


D.run_log = run_log
sys.argv = ["demo", "--out", str(out_dir / f"demo_scenarios_{tag}.json")]
D.main()

c, sim, final, logs = cap["r"]
with open(D.B.EVAL / "pair_gt.csv", encoding="utf-8", newline="") as f:
    gt = {r["sim_pair_key"]: r for r in csv.DictReader(f)}
lab = Counter(g["gt_label"] for g in gt.values())
pend_er = sum(1 for k, g in gt.items() if g["gt_label"] == "PENDING_EXPERT_ELIGIBLE" and final[k] == "EXPERT_REVIEW")
inel_ci = sum(1 for k, g in gt.items() if g["gt_label"] == "INELIGIBLE" and final[k] == "CONFIRMED_INELIGIBLE")
false_ci = sum(1 for k, g in gt.items() if g["gt_label"] != "INELIGIBLE" and final[k] == "CONFIRMED_INELIGIBLE")
ce = sum(1 for v in final.values() if v == "CONFIRMED_ELIGIBLE")

# 엔진 관찰: E12 info_key, 남성 환자에 대한 임신 기준 행동
alias = sim.aliases()
rev = {v: k for k, v in alias.items()}
sex = {}
for p in c.public.patients:
    s = p.known.get("sex") if isinstance(p.known, dict) else None
    if s is None:
        s = c.oracle.hidden.get(p.patient_id, {}).get("sex")
    sex[p.patient_id] = s
e12 = Counter()
preg_male = []
for al, lg in logs.items():
    pid = rev.get(al, al)
    for x in lg:
        for t in x["targets"]:
            if t == "NCT06216301:E12":
                e12[x["info_key"]] += 1
            if t == "NCT06216301:E16" and str(sex.get(pid, "")).lower().startswith("m"):
                preg_male.append({"patient": pid, "step": x["step"], "action": x["action"], "info_key": x["info_key"]})
out = {"tag": tag, "run": {"policy": "full_dcvc", "budget_minutes": 6000, "horizon": 80, "seed": 1},
       "gt_labels": dict(lab), "pending_expert_reached_expert_review": pend_er, "ineligible_confirmed": inel_ci,
       "confirmed_ineligible_not_gt_ineligible": false_ci, "confirmed_eligible": ce,
       "final_status_counts": dict(Counter(final.values())),
       "obs_e12_info_keys": dict(e12), "obs_pregnancy_actions_for_male": preg_male, "sex_known": dict(Counter(map(str, sex.values())))}
(out_dir / f"runner_6000m_h80_{tag}_from_demo.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(json.dumps({k: v for k, v in out.items() if k != "obs_pregnancy_actions_for_male"}, ensure_ascii=False),
      "preg_male", len(preg_male))
