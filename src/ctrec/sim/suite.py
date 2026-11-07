"""P 실험: 같은 코호트·같은 예산·같은 안전 제약에서 기준선과 DCV+C, ablation을 비교합니다.

    python -m ctrec.sim.suite [--cohort demo|generator|nsclc] [--scenario ID] [--replicate 0,1,2]
                              [--budgets 20,45,90] [--seeds 1,2,3] [--horizon 10] [--out DIR]

비교 팔(arm) - 모두 engine.baselines.make_policy의 같은 등록표, 같은 후보(LegacyAdapterPolicy.propose)·같은 안전 규칙
(안전 필수 먼저·예산 초과 거부·중복 제거)·같은 실행 루프(engine.loop.EngineLoop, 웹과 같은 객체)를 씁니다.
| arm | 정책 |
|---|---|
| random | 무작위 순서 (seed 고정) |
| patient_independent | 환자별 독립 (patient_order) |
| cost_first | 예상 비용 우선 (cheapest_first) |
| dcvc | full_dcvc (제안 정책) |
| dcvc-no_bundles | 문서 번들링 제거 (점수에 묶음 할인 없음) |
| dcvc-no_dcv | DCV(결정 영향 필터) 제거 |
| dcvc-no_recovery | 실패 회복 제거 (실패 뒤 대체 정보원 없이 보류) |
| dcvc-no_evidence_routing | 근거 경로 제거 (진료 기록 필요 기준도 환자에게 먼저, full_dcvc 0.1 동작) |
no_action(추가 확인 없음)은 기준점으로 같이 냅니다.

정답(GT) 구분 - 섞지 않습니다.
- independent_gt: 생성기의 쌍 라벨(cohort.spec.OraclePart.pair_label). 숨긴 값까지 아는 생성기가 Matcher·근거 게이트·정책과
  다른 코드 경로(슬롯 사전의 검수된 연결 + 수치 비교는 같은 규칙 엔진)로 만든 **합성** 라벨입니다. 임상 GT가 아닙니다.
  라벨 없는 쌍(UNLABELED: 임상의 판단·미연결 기준)은 정확도 분모에서 빼고 confirmed_unverified로 따로 셉니다.
- rule_gt_circular: 같은 평가기에 정답 사실을 넣어 만든 라벨 (runner.rule_labels). 구현 일관성 검사일 뿐이며
  False Eligible로 쓰지 않습니다.
- 합성 데모·NSCLC 파일럿처럼 독립 GT가 없는 코호트는 정확도 칸을 비우고(None) gt_kind = "none"으로 표시합니다.
- 실제 임상 GT는 이 저장소에 없습니다. 어떤 수치도 임상 정확도가 아닙니다.

비용: 정보원 카탈로그의 비용은 가정값(cost_basis = assumed)입니다. assumed_cost_minutes는 정책 간 상대 비교에만 씁니다.
성공 확률(응답 모델)은 환경에만 있고 정책 입력이 아닙니다.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path

from ..engine.bundles import SourceCatalog
from ..engine.contracts import EffortBudget, PairStatus
from ..engine.experiment import catalog_claims
from . import demo, runner
from .cohort import SimCohort
from .metrics import ELIGIBLE, INELIGIBLE, UNLABELED

SUITE_VERSION = "p_suite/0.1"
ARMS: tuple[tuple[str, str, dict], ...] = (
    ("no_action", "no_action", {}),
    ("random", "random", {}),
    ("patient_independent", "patient_order", {}),
    ("cost_first", "cheapest_first", {}),
    ("dcvc", "full_dcvc", {}),
    ("dcvc-no_bundles", "full_dcvc", {"bundles": False}),
    ("dcvc-no_dcv", "full_dcvc", {"dcv_impact": False}),
    ("dcvc-no_recovery", "full_dcvc", {"recovery": False}),
    ("dcvc-no_evidence_routing", "full_dcvc", {"evidence_routing": False}),
)
GT_INDEPENDENT, GT_NONE = "synthetic_generator_gt", "none"
CE, CI = PairStatus.CONFIRMED_ELIGIBLE, PairStatus.CONFIRMED_INELIGIBLE


@dataclass(frozen=True)
class SuiteCohort:
    cohort: SimCohort
    labels: dict[str, str] | None  # PairKey.key "<별칭>|<시험>" -> eligible / ineligible / unlabeled (독립 GT). None = 없음
    gt_kind: str
    note: str = ""


def generator_labels(gen_cohort, sim: SimCohort) -> dict[str, str]:
    """생성기 쌍 라벨("환자|시험") -> 시뮬레이터 별칭 키. 정책·평가기에는 넘기지 않습니다 (지표 계산 전용)."""
    alias = sim.aliases()
    m = {"ELIGIBLE": ELIGIBLE, "INELIGIBLE": INELIGIBLE, "UNLABELED": UNLABELED}
    out = {}
    for key, lab in gen_cohort.oracle.pair_label.items():
        pid, tid = key.split("|", 1)
        if pid in alias:
            out[f"{alias[pid]}|{tid}"] = m[lab]
    return out


def pair_metrics(res: runner.RunResult, labels: dict[str, str] | None, initial_open: dict[str, int]) -> dict:
    """한 실행의 P 지표. labels가 없으면 정확도 칸은 None."""
    pairs = res.final.pairs()
    n = len(pairs)
    ce = [p for p in pairs if p.status == CE]
    ci = [p for p in pairs if p.status == CI]
    unconfirmed = [p for p in pairs if p.status not in (CE, CI)]
    waiting = [p for p in unconfirmed if p.pair.key in res.remaining_pairs or p.status == PairStatus.EXPERT_REVIEW]
    m = res.metrics
    out = {
        "pairs": n, "system_confirmed_eligible": len(ce), "system_confirmed_ineligible": len(ci),
        "coverage": round((len(ce) + len(ci)) / n, 6) if n else 0.0,
        "unresolved": len(unconfirmed), "waiting": len(waiting), "unresolved_no_action": len(unconfirmed) - len(waiting),
        "unresolved_by_status": {s.value: sum(p.status == s for p in unconfirmed) for s in PairStatus
                                 if s not in (CE, CI) and any(p.status == s for p in unconfirmed)},
        "actions": sum(m["actions_executed"].values()), "questions": m["patient_burden"]["questions"],
        "document_requests": m["actions_executed"].get("REQUEST_DOCUMENT", 0),
        "assumed_cost_minutes": m["active_minutes"]["coordinator"],
        "assumed_clinician_minutes": m["active_minutes"]["clinician"],
        "rule_gt_circular_disagreements": m["rule_gt"]["rule_gt_disagreements"],
        "termination": m["termination"],
    }
    if labels is None:
        out.update({k: None for k in ("verified_eligible", "false_eligible", "verified_ineligible", "false_ineligible",
                                      "confirmed_unverified", "gt_eligible", "gt_labeled", "gt_coverage")})
    else:
        lab = lambda p: labels.get(p.pair.key, UNLABELED)  # noqa: E731
        labeled = [p for p in pairs if lab(p) != UNLABELED]
        conf_labeled = [p for p in ce + ci if lab(p) != UNLABELED]
        out.update({
            "verified_eligible": sum(lab(p) == ELIGIBLE for p in ce),
            "false_eligible": sum(lab(p) == INELIGIBLE for p in ce),
            "verified_ineligible": sum(lab(p) == INELIGIBLE for p in ci),
            "false_ineligible": sum(lab(p) == ELIGIBLE for p in ci),
            "confirmed_unverified": sum(lab(p) == UNLABELED for p in ce + ci),
            "gt_eligible": sum(lab(p) == ELIGIBLE for p in pairs), "gt_labeled": len(labeled),
            "gt_coverage": round(len(conf_labeled) / len(labeled), 6) if labeled else None,
        })
    out["fairness"] = fairness(res, labels, initial_open)
    return out


def fairness(res: runner.RunResult, labels: dict[str, str] | None, initial_open: dict[str, int]) -> dict:
    """공정성 보조지표 (정책을 바꾸지 않는 관측). 처음에 열린 쌍이 있던 환자만 봄."""
    needing = [a for a, k in initial_open.items() if k]
    pp = res.per_patient
    acted = {s for s in needing if pp[s]["coordinator_minutes"] > 0}
    out = {"patients_needing": len(needing), "zero_allocation": len(needing) - len(acted),
           "allocation_gini": res.metrics["allocation_gini"],
           "max_questions_per_patient": res.metrics["patient_burden"]["max_questions_per_patient"]}
    if labels is not None:  # 독립 GT로 적격 쌍이 있는 환자 중 하나도 확정되지 않은 환자 (놓친 기회)
        has = {k.split("|", 1)[0] for k, v in labels.items() if v == ELIGIBLE}
        found = {p.pair.patient_alias for p in res.final.pairs() if p.status == CE and labels.get(p.pair.key) == ELIGIBLE}
        out.update({"patients_with_gt_opportunity": len(has), "patients_opportunity_missed": len(has - found)})
    return out


def per_patient_report(res: runner.RunResult) -> dict[str, dict]:
    """환자별 기회(확정 적격)·확정 부적격·대기(남은 행동 또는 임상의 검토)·미해결(남은 행동 없음). 종료 시 상태 그대로."""
    out = {}
    for alias in res.final.aliases():
        ps = res.final.patients[alias].pairs
        un = [p for p in ps if p.status not in (CE, CI)]
        wait = [p for p in un if p.pair.key in res.remaining_pairs or p.status == PairStatus.EXPERT_REVIEW]
        out[alias] = {"opportunities": sorted(p.pair.trial_id for p in ps if p.status == CE),
                      "confirmed_ineligible": sorted(p.pair.trial_id for p in ps if p.status == CI),
                      "waiting": {p.pair.trial_id: p.status.value for p in wait},
                      "unresolved": {p.pair.trial_id: p.status.value for p in un if p not in wait},
                      "coordinator_minutes": res.per_patient[alias]["coordinator_minutes"]}
    return out


def run_suite(cohorts: list[SuiteCohort], budgets: list[EffortBudget], seeds: list[int],
              arms=ARMS, catalog: SourceCatalog | None = None, out: Path | None = None) -> list[dict]:
    catalog = catalog or SourceCatalog()
    rows = []
    for ci, sc in enumerate(cohorts):
        init_open = initial_open(sc.cohort)
        for b in budgets:
            for seed in seeds:
                for arm, pid, cfg in arms:
                    spec = runner.build_spec(f"p-{arm}-{int(b.coordinator_minutes)}-{seed}", pid, sc.cohort, b, seed,
                                             dict(cfg), catalog)
                    res = runner.run(spec, sc.cohort, catalog)
                    row = {"cohort": sc.cohort.cohort_id, "cohort_index": ci, "gt_kind": sc.gt_kind, "arm": arm,
                           "policy_id": pid, "policy_config": dict(cfg), "budget_minutes": b.coordinator_minutes,
                           "seed": seed, "result_hash": res.result_hash, **pair_metrics(res, sc.labels, init_open)}
                    rows.append(row)
                    if out is not None:
                        d = out / f"c{ci}-{arm}-b{int(b.coordinator_minutes)}-s{seed}"
                        d.mkdir(parents=True, exist_ok=True)
                        (d / "result.json").write_text(json.dumps(
                            {"row": row, "per_patient": per_patient_report(res),
                             "status_initial": res.metrics["status_initial"], "initial_open_pairs": init_open,
                             "manifest": res.manifest}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return rows


def initial_open(cohort: SimCohort) -> dict[str, int]:
    """처음 상태(공개 사실만)에서 열린(확정·보류·미평가가 아닌) 쌍 수 - 공정성 분모. 별칭별. runner.run과 같은 평가기."""
    from ..engine.cohort_state import CohortState
    from .evaluator import OfflineEvaluator, make_fact
    closed = {"CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE", "BLOCKED_FAILURE", "NOT_EVALUATED"}
    ev = OfflineEvaluator(cohort.trials, cohort.reference_date, cohort.judge)
    aliases = cohort.aliases()
    states = {}
    for p in cohort.patients:
        a = aliases[p.key]
        facts = [make_fact(a, f, seq=0, origin=f"public{i}", source_type=f.source_type, verified=f.verified,
                           ref_kind="input_text", ref=None) for i, f in enumerate(p.public)]
        states[a] = ev.evaluate(a, facts, p.trials)[0]
    co = CohortState.from_decision_states(states)
    return {a: sum(p.open and p.status.value not in closed for p in co.patients[a].pairs) for a in co.aliases()}


SUM_KEYS = ("verified_eligible", "false_eligible", "verified_ineligible", "false_ineligible", "confirmed_unverified",
            "system_confirmed_eligible", "system_confirmed_ineligible", "unresolved", "waiting", "unresolved_no_action",
            "actions", "questions", "document_requests", "assumed_cost_minutes", "rule_gt_circular_disagreements")


def aggregate(rows: list[dict]) -> list[dict]:
    """(코호트 GT 종류, 예산, arm)별 평균 (seed·replicate 평균). 독립 GT가 없는 코호트의 정확도 칸은 None 그대로."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["gt_kind"], r["budget_minutes"], r["arm"]), []).append(r)
    out = []
    order = {a: i for i, (a, _, _) in enumerate(ARMS)}
    for (gk, b, arm), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], order.get(kv[0][2], 99))):
        agg = {"gt_kind": gk, "budget_minutes": b, "arm": arm, "runs": len(rs)}
        for k in SUM_KEYS + ("coverage", "gt_coverage"):
            vals = [r[k] for r in rs if r.get(k) is not None]
            agg[k] = round(statistics.fmean(vals), 3) if vals else None
        for k in ("zero_allocation", "allocation_gini", "patients_opportunity_missed"):
            vals = [r["fairness"][k] for r in rs if r["fairness"].get(k) is not None]
            agg[k] = round(statistics.fmean(vals), 3) if vals else None
        out.append(agg)
    return out


COLS = ["gt_kind", "budget_minutes", "arm", "runs", "verified_eligible", "false_eligible", "verified_ineligible",
        "false_ineligible", "confirmed_unverified", "coverage", "gt_coverage", "unresolved", "waiting",
        "unresolved_no_action", "actions", "assumed_cost_minutes", "zero_allocation", "allocation_gini",
        "patients_opportunity_missed", "rule_gt_circular_disagreements"]


def load_generator(config: str, scenario: str, replicates: list[int], source_map: str) -> list[SuiteCohort]:
    from ..cohort import generate as G
    from . import from_generator as FG
    cfg, sd, cat, parsed = G.load_inputs(config)
    sc = next((s for s in cfg.scenarios if s.scenario_id == scenario), None)
    if sc is None:
        raise SystemExit(f"시나리오 없음: {scenario!r} (가능: {[s.scenario_id for s in cfg.scenarios]})")
    smap, h = FG.load_source_map(source_map), G.config_hash(cfg, sd, cat)
    out = []
    for rep in replicates:
        gc = G.generate_cohort(cfg, sd, cat, parsed, sc, rep, h)
        sim = FG.to_sim_cohort(gc, sd, smap)
        out.append(SuiteCohort(sim, generator_labels(gc, sim), GT_INDEPENDENT,
                               "생성기 쌍 라벨 (합성, 임상 GT 아님)"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ctrec.sim.suite", description=__doc__.splitlines()[0])
    ap.add_argument("--cohort", choices=("demo", "generator", "nsclc"), default="generator")
    ap.add_argument("--generator", default="data/cohort/generator_config.json")
    ap.add_argument("--scenario", default="neph-k3-catalog-source")
    ap.add_argument("--replicate", default="0,1,2")
    ap.add_argument("--map", default="data/cohort/source_map.json")
    ap.add_argument("--budgets", default="20,45,90")
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--json", type=Path, default=None, help="행·집계 결과 JSON")
    a = ap.parse_args(argv)
    ints = lambda s: [int(x) for x in s.split(",") if x.strip()]  # noqa: E731
    if a.cohort == "demo":
        cohorts = [SuiteCohort(demo.cohort(), None, GT_NONE, "합성 데모: 독립 GT 없음")]
    elif a.cohort == "nsclc":
        from . import nsclc_pilot
        cohorts = [nsclc_pilot.suite_cohort()]
    else:
        cohorts = load_generator(a.generator, a.scenario, ints(a.replicate), a.map)
    budgets = [EffortBudget(coordinator_minutes=m, clinician_minutes=60, horizon_steps=a.horizon) for m in ints(a.budgets)]
    rows = run_suite(cohorts, budgets, ints(a.seeds), out=a.out)
    agg = aggregate(rows)
    print("\t".join(COLS))
    for r in agg:
        print("\t".join("-" if r[c] is None else str(r[c]) for c in COLS))
    claims = catalog_claims(SourceCatalog())
    print(f"# {SUITE_VERSION}: cohorts={[c.cohort.cohort_id for c in cohorts]}, gt={sorted({c.gt_kind for c in cohorts})}, "
          f"seeds={ints(a.seeds)}, horizon={a.horizon}")
    print(f"# 비용: {claims['allowed_claims']} (가정 비용 정보원 {claims['assumed_cost_sources']})")
    print("# 정확도 칸(verified_*/false_*)은 독립 합성 GT 기준이며 임상 GT가 아님. '-' = 독립 GT 없음(미검증). "
          "rule_gt_circular_disagreements는 같은 평가기의 순환 라벨과의 일관성 검사.")
    if a.json is not None:
        a.json.write_text(json.dumps({"version": SUITE_VERSION, "rows": rows, "aggregate": agg, "catalog_claims": claims},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ARMS", "SuiteCohort", "run_suite", "aggregate", "pair_metrics", "per_patient_report", "generator_labels",
           "main", "SUITE_VERSION"]
