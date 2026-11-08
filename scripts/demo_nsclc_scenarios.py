"""NSCLC 데모 시나리오 고르기·확인 (웹·엔진 코드 변경 없음). 통합본에서 오프라인 실행으로 기대 상태가 실제로 나오는지 기록.

  python scripts/demo_nsclc_scenarios.py --out data/nsclc/eval/checks/demo_scenarios_4f81460.json

(dba06d9 사본, 핸드오프 Agent C: 행동별 가정 비용(assumed_coordinator_min·assumed_latency_min)과 환자별 가정 코디네이터 분을 기록에 더함.
 판정·선택 로직은 70fe9a5 원본과 같음)

- 평가셋(data/nsclc/eval)을 #20 runner로 full_dcvc, 6000분, horizon 80, seed 1 실행 -> 환자별 행동 기록
- A: GT 전문가 판단 대기 적격 + 최종 EXPERT_REVIEW / B: GT 부적격 + 최종 CONFIRMED_INELIGIBLE
- C: 연결된(bindable) 기준의 환자 질문이 '모름'(no_answer)으로 끝난 뒤 같은 항목을 문서·검사지 요청으로 확보한 환자
- D: 지역 조건 (평가셋에 22세 미만 NCT06216301 환자가 없음 -> P0-3 회귀 케이스의 20세·국가 미상 환자를 따로 평가)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import build_nsclc_eval as B  # noqa: E402
from ctrec.cohort import generate as gen  # noqa: E402
from ctrec.cohort.spec import Cohort  # noqa: E402


def run_log():
    from ctrec.engine.contracts import EffortBudget
    from ctrec.sim import runner
    c = Cohort.model_validate(json.loads(B.GENERATED.read_text(encoding="utf-8")))
    _, sd, _, _ = gen.load_inputs(B.CONFIG)
    sim = B.sim_cohort(c, sd)
    spec = runner.build_spec("demo", "full_dcvc", sim, EffortBudget(coordinator_minutes=6000, horizon_steps=80), seed=1)
    r = runner.run(spec, sim)
    final = {p.pair.key: p.status.value for p in r.final.pairs()}
    by_alias: dict[str, list] = {}
    for rec in r.records:
        a = rec.action
        by_alias.setdefault(a.patient_alias, []).append({
            "step": rec.step, "action": a.action_type, "source": a.source_id, "info_key": a.info_key,
            "targets": list(a.target_criteria), "outcome": rec.outcome.status if rec.outcome else None,
            "error": rec.outcome.error_code if rec.outcome else None,
            "new_facts": len(rec.outcome.new_fact_ids) if rec.outcome else None,  # 0이면 값을 새로 얻지 못함 (기존 사실 확인만)
            # 가정 비용 (시뮬레이터 비용표의 코디네이터 분·대기 분, 측정 시간 아님)
            "assumed_coordinator_min": rec.outcome.cost_charged.coordinator_minutes if rec.outcome else None,
            "assumed_latency_min": rec.outcome.latency_minutes if rec.outcome else None,
            "pairs": [f"{t.pair.trial_id}:{t.before.value}->{t.after.value}" for t in rec.pairs]})
    global PER_PATIENT_MIN
    PER_PATIENT_MIN = {k: v.get("coordinator_minutes") for k, v in r.per_patient.items()}
    return c, sim, final, by_alias


PER_PATIENT_MIN: dict = {}


def region_case() -> dict:
    """D: 20세·국가 미상, NCT06216301 (원본 파싱본). P0-3 회귀 케이스와 같은 방식."""
    from ctrec.engine.cohort_state import CohortState
    from ctrec.sim.cohort import SimFact, make_cohort
    from ctrec.sim.evaluator import OfflineEvaluator, make_fact
    from ctrec.sim.from_generator import load_trials
    trials = load_trials(["NCT06216301"], B.NSCLC / "parsed", B.NSCLC / "trials")
    facts = [SimFact(field="age", value=20, unit="years"), SimFact(field="sex", value="female")]
    co = make_cohort("demo-region", "2026-11-07", trials, [("DEMO-REGION", facts, facts)], "demo-region")
    a = co.aliases()["DEMO-REGION"]
    pf = [make_fact(a, f, seq=0, origin=f"t{i}", source_type="clinical_record", verified=True, ref_kind="document", ref=None)
          for i, f in enumerate(facts)]
    st = OfflineEvaluator(co.trials, co.reference_date, None).evaluate(a, pf, ("NCT06216301",))[0]
    ts = st.trials["NCT06216301"]
    return {"patient": "DEMO-REGION (평가셋 밖 데모 환자: 20세 여성, 국가 미상)",
            "engine_status": CohortState.from_decision_states({a: st}).pair(a, "NCT06216301").status.value,
            "I1": next(c.status for c in ts.criteria if c.rule_id == "I1"), "mismatch_basis": ts.mismatch_basis,
            "gt_expected": "NOT_EVALUATED (I1 지역 조건부, 국가 미상)"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    c, sim, final, logs = run_log()
    alias = sim.aliases()
    rev = {v: k for k, v in alias.items()}
    with open(B.EVAL / "pair_gt.csv", encoding="utf-8", newline="") as f:
        gt = {r["sim_pair_key"]: r for r in csv.DictReader(f)}
    pats = {p.patient_id: p for p in c.public.patients}

    def pick(cond):
        best = None
        for k, g in gt.items():
            al = k.split("|")[0]
            if cond(k, g, logs.get(al, [])):
                score = (len(pats[g["patient_id"]].candidate_trials), len(logs.get(al, [])))
                if best is None or score < best[0]:
                    best = (score, k)
        return best[1] if best else None

    with open(B.REVIEW, encoding="utf-8-sig", newline="") as f:
        bindable = {f"{r['trial_id']}:{r['criterion_id']}" for r in csv.DictReader(f) if r["gt_use"] == "bindable"}

    def asked_then_doc(k, g, lg, need_new_fact=False):
        t = g["trial_id"]
        for i, x in enumerate(lg):
            if x["action"] == "ASK_PATIENT" and x["error"] == "no_answer" and any(
                    y.startswith(t) and y in bindable for y in x["targets"]):
                if any(y["action"] == "REQUEST_DOCUMENT" and y["info_key"] == x["info_key"] and y["outcome"] == "succeeded"
                       and (not need_new_fact or (y.get("new_facts") or 0) > 0) for y in lg[i + 1:]):
                    return True
        return False

    chosen = {
        "A_pending_expert": pick(lambda k, g, lg: g["gt_label"] == "PENDING_EXPERT_ELIGIBLE" and final[k] == "EXPERT_REVIEW"),
        "B_ineligible": pick(lambda k, g, lg: g["gt_label"] == "INELIGIBLE" and final[k] == "CONFIRMED_INELIGIBLE"
                             and any(x["outcome"] == "succeeded" for x in lg)),
        "C_unknown_then_document": pick(asked_then_doc),
        # dba06d9 추가: 원래 C 조건은 문서 요청 '성공'만 봄. 성공해도 새 사실이 0건일 수 있어(값 없음) 값을 실제로 얻은 경우를 따로 고름
        "C2_unknown_then_document_new_fact": pick(lambda k, g, lg: asked_then_doc(k, g, lg, need_new_fact=True)),
    }
    c_all = sorted(k for k, g in gt.items() if asked_then_doc(k, g, logs.get(k.split("|")[0], [])))
    out = {"run": {"policy": "full_dcvc", "budget_minutes": 6000, "horizon": 80, "seed": 1, "cohort": sim.cohort_id},
           "scenarios": {}, "C_candidates": c_all,
           "C2_candidates": sorted(k for k, g in gt.items() if asked_then_doc(k, g, logs.get(k.split("|")[0], []), need_new_fact=True)), "D_region": region_case()}
    for name, k in chosen.items():
        if k is None:
            out["scenarios"][name] = None
            continue
        g = gt[k]
        al = k.split("|")[0]
        p = pats[g["patient_id"]]
        out["scenarios"][name] = {
            "sim_pair_key": k, "patient_id": g["patient_id"], "trial_id": g["trial_id"], "gt_label": g["gt_label"],
            "gt_reasons": g["reasons"], "final_status": final[k], "candidate_trials": p.candidate_trials,
            "public_known": p.known, "hidden": c.oracle.hidden.get(p.patient_id, {}),
            "other_pairs_final": {t: final[f"{al}|{t}"] for t in p.candidate_trials},
            "assumed_patient_coordinator_min": PER_PATIENT_MIN.get(al),
            "actions": [x for x in logs.get(al, []) if any(y.startswith(g["trial_id"]) for y in x["targets"]) or not x["targets"]]}
    args = a.out
    args.parent.mkdir(parents=True, exist_ok=True)
    args.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: (v and {kk: v[kk] for kk in ("patient_id", "trial_id", "gt_label", "final_status")}) for k, v in out["scenarios"].items()},
                     ensure_ascii=False), json.dumps(out["D_region"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
