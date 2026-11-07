"""G1: 현황판 결정 5 전·후 비교 (읽기 전용). argv: worktree
(a) 환자 고정: 결정 5 전 코호트에 두 기준만 판단 기준으로 표시해 현황판을 다시 계산 -> 재분류 효과만
(b) 실제 재생성: 결정 5 후 생성기로 다시 생성 (판단 슬롯은 값을 뽑지 않아 난수 소비가 바뀌고 환자가 달라짐) -> 총계만
"""
import copy, json, sys
from collections import Counter
from pathlib import Path
root = Path(sys.argv[1]); sys.path.insert(0, str(root / "src"))
from ctrec import config
from ctrec.cohort import generate as gen, spec
from ctrec.web import cohort_board as cb

EXTRA = spec.EXPERT_SLOTS_EXTRA


def gen_cohorts(expert_slots):
    spec.EXPERT_SLOTS = expert_slots
    cfg, sd, cat, parsed = gen.load_inputs(config.DATA_DIR / "cohort" / "generator_config.json")
    return sd, [c.model_dump(mode="json") for c in gen.generate_all(cfg, sd, cat, parsed)]


def board(d, gate=True):
    if not gate:
        d = copy.deepcopy(d); d["public"]["unbound_rules"] = {t: [] for t in d["public"]["trials"]}
    return cb.board(d)["pairs"]


def mark(d, sd):
    """결정 5 전 코호트에서 판단 슬롯(EXTRA)을 쓰는 기준을 판단 기준으로 표시 (생성기와 같은 처리: 초기 상태 unknown)."""
    d = copy.deepcopy(d)
    rules = {}
    for t, refs in d["public"]["rules"].items():
        rules[t] = [r["rule_id"] for r in refs if any(s in EXTRA for s in [r["slot"], *r.get("extra_slots", [])])]
        d["public"]["expert_rules"][t] = sorted(set(d["public"]["expert_rules"].get(t, [])) | set(rules[t]))
    for k in list(d["public"]["initial_holds"]):
        p, t, r = k.split("|")
        if r in rules.get(t, []):
            d["public"]["initial_holds"][k] = "unknown"
    return d, rules


sd0, before = gen_cohorts(spec.EXPERT_SLOTS_CSV)
rows, moved, gate_changes = [], [], []
tot = {k: Counter() for k in ("gate_before", "gate_after", "control_before", "control_after")}
marked_rules = None
for d in before:
    m, marked_rules = mark(d, sd0)
    gb, ga, cbf, caf = board(d), board(m), board(d, False), board(m, False)
    for k in gb:
        key = f"{d['cohort_id']}|{k}"
        tot["gate_before"][gb[k]["status"]] += 1; tot["gate_after"][ga[k]["status"]] += 1
        tot["control_before"][cbf[k]["status"]] += 1; tot["control_after"][caf[k]["status"]] += 1
        t = k.split("|")[1]
        if cbf[k]["status"] == "CONFIRMED_ELIGIBLE":
            moved.append({"pair": key, "control_before": cbf[k]["status"], "control_after": caf[k]["status"],
                          "gate_before": gb[k]["status"], "gate_after": ga[k]["status"],
                          "cause": marked_rules.get(t, []), "unbound_rules": len(d["public"]["unbound_rules"][t])})
        if gb[k]["status"] != ga[k]["status"]:
            gate_changes.append({"pair": key, "before": gb[k]["status"], "after": ga[k]["status"]})
_, after_regen = gen_cohorts(spec.EXPERT_SLOTS_CSV | EXTRA)
regen = {k: Counter() for k in ("gate", "control")}
regen_moved = Counter()
for d in after_regen:
    g, c = board(d), board(d, False)
    for k in g:
        regen["gate"][g[k]["status"]] += 1; regen["control"][c[k]["status"]] += 1
        if c[k]["status"] == "CONFIRMED_ELIGIBLE" and g[k]["status"] == "NOT_EVALUATED":
            regen_moved[k.split("|")[1]] += 1
out = {"marked_rules": marked_rules, "fixed_patients": {
    "control_confirmed_eligible_before": len(moved),
    "control_after_of_those": dict(Counter(m["control_after"] for m in moved)),
    "by_trial_cause": {f"{m_[0]}|{','.join(m_[1])}|{m_[2]}": n for m_, n in
                       Counter((x["pair"].split("|")[2], tuple(x["cause"]), x["control_after"]) for x in moved).items()},
    "gate_confirmed_eligible": [tot["gate_before"]["CONFIRMED_ELIGIBLE"], tot["gate_after"]["CONFIRMED_ELIGIBLE"]],
    "totals": {k: dict(v) for k, v in tot.items()}, "gate_changes": {f"{a}->{b}": n for (a, b), n in Counter((g["before"], g["after"]) for g in gate_changes).items()},
    "moved": moved},
    "regenerated": {"totals": {k: dict(v) for k, v in regen.items()}, "test_moved": dict(regen_moved)}}
print(json.dumps(out, ensure_ascii=False, indent=1))

# 추가: 결정 5 전 확정 부적격 -> 후 아님 쌍의 불충족 근거 기준
from ctrec.tools import rule_engine as _re
basis = Counter()
for d in before:
    m, mr = mark(d, sd0)
    gb, ga = board(d), board(m)
    kinds = {t: {r["rule_id"]: r["kind"] for r in refs} for t, refs in d["public"]["rules"].items()}
    for k in gb:
        if gb[k]["status"] == "CONFIRMED_INELIGIBLE" and ga[k]["status"] != "CONFIRMED_INELIGIBLE":
            p, t = k.split("|")
            fails = sorted(r for r, kind in kinds[t].items()
                           if _re.passes(kind, d["public"]["initial_holds"].get(f"{p}|{t}|{r}", "unknown")) is False)
            basis[(t, ",".join(fails), ga[k]["status"])] += 1
print("CI_LOST_BASIS", json.dumps({f"{a}|{b}|{c}": n for (a, b, c), n in basis.items()}, ensure_ascii=False))
