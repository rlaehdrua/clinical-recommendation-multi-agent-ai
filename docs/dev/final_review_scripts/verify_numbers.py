"""발표 수치 대조: urgent/integration worktree의 데이터 파일에서 다시 셈. 사용: python verify_numbers.py <repo> <docs_repo>"""
import csv
import glob
import json
import sys
from collections import Counter
from pathlib import Path

R, G = Path(sys.argv[1]), Path(sys.argv[2])
o = {}
tr = sorted(glob.glob(str(R / "data/nsclc/trials/*.json")))
o["trials"] = len(tr)
o["retrieved_at"] = sorted({str(json.load(open(f, encoding="utf-8")).get("source", {}).get("retrieved_at", ""))[:10] for f in tr})
with open(R / "data/nsclc/review/protocol_status.csv", encoding="utf-8-sig") as f:
    ps = list(csv.DictReader(f))
o["protocol_status_cols"] = list(ps[0].keys())
o["protocol_status"] = [{k: r[k] for k in list(r)[:4]} for r in ps]
with open(R / "data/nsclc/review/criteria_verification.csv", encoding="utf-8-sig") as f:
    cv = list(csv.DictReader(f))
o["criteria"] = len(cv)
o["gt_use"] = dict(Counter(r["gt_use"] for r in cv))
o["cols"] = list(cv[0].keys())
for col in ("match_status", "verification", "source_match", "status"):
    if col in cv[0]:
        o[f"by_{col}"] = dict(Counter(r[col] for r in cv if r["gt_use"] not in ("out_of_scope", "excluded")))
o["clinical_review_values"] = dict(Counter(r.get("clinical_review", "?") for r in cv))
pt = json.load(open(R / "data/nsclc/eval/patients.json", encoding="utf-8"))
o["patients"] = len(pt) if isinstance(pt, list) else len(pt.get("patients", pt))
m = json.load(open(R / "data/nsclc/eval/manifest.json", encoding="utf-8"))
o["manifest_keys"] = {k: m[k] for k in m if k in ("seed", "config_hash", "n_patients", "n_pairs", "content_hash")}
o["label_summary"] = json.load(open(R / "data/nsclc/eval/label_summary.json", encoding="utf-8"))
with open(R / "data/nsclc/eval/pair_gt.csv", encoding="utf-8") as f:
    pg = list(csv.DictReader(f))
o["pairs"] = len(pg)
o["pair_labels"] = dict(Counter(r["gt_label"] for r in pg))
gc = json.load(open(R / "data/nsclc/eval/inputs/generator_config.json", encoding="utf-8"))
o["gen_seed"] = gc.get("seed")
o["design_targets"] = gc.get("design_targets")
lr = json.load(open(R / "data/nsclc/eval/inputs/lab_reference.json", encoding="utf-8"))
o["lab_reference"] = lr
ov = json.load(open(R / "data/nsclc/review/overrides.json", encoding="utf-8"))
o["overrides_n"] = len(ov) if isinstance(ov, (list, dict)) else None
for fn in ("circular_vs_independent_4f81460.json", "runner_400m_h10_4f81460.json", "runner_6000m_h80_4f81460.json"):
    p = G / "data/nsclc/eval/checks" / fn
    d = json.load(open(p, encoding="utf-8"))
    o[fn] = {k: d[k] for k in list(d)[:12]}
print(json.dumps(o, ensure_ascii=False, indent=1, default=str)[:12000])
