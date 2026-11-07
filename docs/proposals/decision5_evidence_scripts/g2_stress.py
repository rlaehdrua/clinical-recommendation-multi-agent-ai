"""G2: test_recovery_stress 결정 5 전·후 쌍 단위 비교 (읽기 전용, 파일 변경 없음). argv: worktree out.json
조건
- A        : 결정 5 전 (spec.EXPERT_SLOTS = CSV 6개), 시뮬레이터 사본 그대로
- B_regen  : 결정 5 후 재생성 (현재 브랜치 상태 = 테스트 실패 상태), 시뮬레이터 사본 그대로(6개)
- B_fixed  : A와 같은 환자, 결정 5 기준(E11·E5)만 값·정답 제거 + 판단 기준 표시, 시뮬레이터 사본 그대로
- C_fixed  : B_fixed + 시뮬레이터가 cohort.spec 정의 사용 (decision5_sim_expert_slots.patch와 같은 효과)
- C_regen  : B_regen + 시뮬레이터가 cohort.spec 정의 사용 (H 브랜치 = 패치 3개 적용 상태)
"""
import copy, json, sys
from collections import Counter
from pathlib import Path
root = Path(sys.argv[1]); sys.path.insert(0, str(root / "src")); sys.path.insert(0, str(root / "tests"))
from ctrec import config
from ctrec.cohort import generate as G, spec
from ctrec.cohort.spec import Cohort
from ctrec.sim import from_generator as FG, runner
import test_recovery_stress as T

CSV, EXTRA = spec.EXPERT_SLOTS_CSV, spec.EXPERT_SLOTS_EXTRA
FG_ORIG = FG.EXPERT_SLOTS


def gen(slots):
    spec.EXPERT_SLOTS = slots
    cfg, sd, cat, parsed = G.load_inputs(config.DATA_DIR / "cohort" / "generator_config.json")
    return sd, G.generate_all(cfg, sd, cat, parsed)[:4]


def fixed(c: Cohort) -> Cohort:
    d = copy.deepcopy(c.model_dump(mode="json"))
    rr = {}
    for t, refs in d["public"]["rules"].items():
        rr[t] = [r["rule_id"] for r in refs if any(s in EXTRA for s in [r["slot"], *r.get("extra_slots", [])])]
        d["public"]["expert_rules"][t] = sorted(set(d["public"]["expert_rules"].get(t, [])) | set(rr[t]))
    for k in list(d["public"]["initial_holds"]):
        p, t, r = k.split("|")
        if r in rr.get(t, []):
            d["public"]["initial_holds"][k] = "unknown"; d["oracle"]["truth"][k] = "unknown"
    for p in d["public"]["patients"]:
        for s in list(p["known"]):
            if s in EXTRA:
                del p["known"][s]
        p["unknown_slots"] = [s for s in p["unknown_slots"] if s not in EXTRA]
        d["oracle"]["hidden"][p["patient_id"]] = {s: v for s, v in d["oracle"]["hidden"].get(p["patient_id"], {}).items() if s not in EXTRA}
    return Cohort.model_validate(d)


def run(cohorts, sd, fg_slots):
    FG.EXPERT_SLOTS = fg_slots
    smap = FG.load_source_map(config.ROOT / "data" / "cohort" / "source_map.json")
    trials = FG.load_trials(cohorts[0].public.trials)
    sims = [FG.to_sim_cohort(gc, sd, smap, trials) for gc in cohorts]
    out = {}
    for name, resp in T.SCENARIOS.items():
        st = {}
        for sim in sims:
            r = runner.run(runner.build_spec("stress", "legacy_greedy", sim, T.BUDGET, seed=0), sim, responses=resp)
            for p in r.final.pairs():
                alias = p.pair.key.split("|")[0]
                t = r.final.patients[alias]._legacy.trials.get(p.pair.trial_id)
                gaps = sorted(f"{c.rule_id}:{c.gap}" for c in (t.criteria if t else [])
                              if c.requirement == "required" and c.gap != "none") if t else []
                st[f"{sim.cohort_id.split('@')[0]}|{p.pair.key}"] = {"status": p.status.value, "gaps": gaps}
        out[name] = st
    return out


sdA, A = gen(CSV)
res = {"A": run(A, sdA, FG_ORIG)}
res["B_fixed"] = run([fixed(c) for c in A], sdA, FG_ORIG)
res["C_fixed"] = run([fixed(c) for c in A], sdA, FG_ORIG | EXTRA)
sdB, B = gen(CSV | EXTRA)
res["B_regen"] = run(B, sdB, FG_ORIG)
res["C_regen"] = run(B, sdB, FG_ORIG | EXTRA)
summary = {k: {n: dict(Counter(v["status"] for v in s.values())) for n, s in r.items()} for k, r in res.items()}
diff = {}
for cond in ("B_fixed", "C_fixed", "B_regen", "C_regen"):
    diff[cond] = {}
    for n in T.SCENARIOS:
        a, b = res["A"][n], res[cond][n]
        keys = set(a) | set(b)
        diff[cond][n] = {
            "left_blocked": [{"pair": k, "to": b.get(k, {}).get("status", "(쌍 없음)"), "gaps_before": a[k]["gaps"]}
                             for k in sorted(keys) if a.get(k, {}).get("status") == "BLOCKED_FAILURE" and b.get(k, {}).get("status") != "BLOCKED_FAILURE"],
            "entered_blocked": [{"pair": k, "from": a.get(k, {}).get("status", "(쌍 없음)"), "gaps_after": b[k]["gaps"]}
                                for k in sorted(keys) if b.get(k, {}).get("status") == "BLOCKED_FAILURE" and a.get(k, {}).get("status") != "BLOCKED_FAILURE"],
            "same_pairs": len(set(a) & set(b)), "pairs_a": len(a), "pairs_b": len(b)}
json.dump({"summary": summary, "diff": diff}, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1))
