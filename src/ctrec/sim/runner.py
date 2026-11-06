"""WP6 Runner (설계 §1·§3.1·§6). spec 검증 -> 단계 반복 -> DecisionRecord·지표·manifest. 정책 내부는 바꾸지 않습니다.

단계(라운드)마다
1. policy.propose(CohortState) - 정책은 public에서 만든 상태만 봄 (oracle 없음)
2. 같은 사실 상태에서 같은 정보원에 이미 시도한 행동(같은 지문·유형·정보원)은 다시 실행하지 않음 (모든 정책 공통).
   같은 질문을 그대로 반복하지 않고, "모름" 뒤 다시 묻는 것은 정책의 바꿔 묻기(policy_config["rephrase"], 지문이 다름)뿐.
   기본 꺼짐, 민감도 분석으로 켬 (11-06 결정). spec에 들어가므로 해시가 다름
3. policy.select(남은 예산 원장) -> 선택된 행동을 순서대로 실행 (CohortEnvironment)
4. 실행한 행동만 원장에 청구 (실패·무응답도 청구, 미실행은 0). 새 사실·문서 확인이 있으면 그 환자를 전체 재평가
5. policy.apply_outcome (운영 상태만) -> record_producer.build_step -> DecisionRecord v2 / 진단
종료: horizon_steps 도달, 고를 행동 없음, 예산 소진. 종료 사유는 지표에 남김.

결정성: 같은 spec·코호트·카탈로그·응답 모델 -> 같은 result_hash (기록·진단·지표의 정규화 해시). 시각·경로·호스트는
manifest에만 두고 해시에 넣지 않습니다. 실제 LLM 호출은 0건이며 오프라인 Matcher 대역 요청 수만 manifest에 남깁니다.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..engine import record_producer as rp
from ..engine.baselines import make_policy
from ..engine.bundles import SourceCatalog
from ..engine.cohort_state import CohortState
from ..engine.contracts import (CONTRACT_VERSION, NON_EXECUTING, SUPPORTED_CONTRACT_VERSIONS, ZERO_COST,
                                ActionOutcome, DataVersion, DecisionRecord, EffortBudget, ExperimentSpec, canonical_hash)
from ..engine.cost import EffortLedger
from ..engine.experiment import ExperimentSpecError, catalog_claims, require_catalog_hash
from .cohort import SimCohort
from .environment import CohortEnvironment
from .evaluator import OfflineEvaluator, make_fact, verify_fact
from .metrics import label_of, summarize
from .response_model import ResponseModel, SourceResponse

RUNNER_VERSION = "wp6_runner/0.1"
CLAIMS = "synthetic_relative_comparison_only"  # 합성 코호트·가정 비용·순환 규칙 GT -> 정책 간 상대 비교만
EVALUATOR_SCOPE = "rule_engine_only_llm_criteria_unknown"
JUDGE_SCOPE = "rule_engine_plus_categorical_judge_upper_bound"  # 연결된 LLM 기준은 결정적 판정기 (LLM 오판 미반영)
_STATUS = {"answered": "succeeded", "declined": "no_response", "failed": "failed", "not_executed": "not_executed"}


@dataclass
class RunResult:
    spec: ExperimentSpec
    records: list[DecisionRecord]
    issues: list[rp.RecordIssue]
    metrics: dict
    manifest: dict
    final: CohortState = field(repr=False)

    @property
    def result_hash(self) -> str:
        return self.manifest["result_hash"]


def build_spec(experiment_id: str, policy_id: str, cohort: SimCohort, budget: EffortBudget, seed: int = 0,
               policy_config: dict | None = None, catalog: SourceCatalog | None = None,
               ablations: tuple[str, ...] = ()) -> ExperimentSpec:
    catalog = catalog or SourceCatalog()
    cfg = dict(policy_config or {})
    pol = make_policy(policy_id, catalog=catalog, **_policy_kwargs(policy_id, cfg, seed))
    return ExperimentSpec(experiment_id=experiment_id, policy_id=policy_id, policy_version=pol.version,
                          policy_config=cfg, seed=seed, budget=budget, ablations=tuple(ablations),
                          data_version=DataVersion(frozen_store_hash=cohort.store_hash(), cohort_hash=cohort.digest(),
                                                   source_catalog_hash=catalog.digest()))


def _policy_kwargs(policy_id: str, cfg: dict, seed: int) -> dict:
    kw = dict(cfg)
    if policy_id == "random":
        kw.setdefault("seed", seed)
    return kw


def _repeat_key(a) -> tuple:
    """같은 사실 상태(지문)에서 같은 정보원에 같은 행동. "모름" 뒤 정책이 같은 지문을 문서 요청으로 바꾼 행동은 다른 키."""
    return (a.patient_alias, a.fingerprint, a.action_type, a.source_id)


def _check(spec: ExperimentSpec, cohort: SimCohort, catalog: SourceCatalog) -> None:
    if spec.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        raise ExperimentSpecError("contract_version_unsupported")
    require_catalog_hash(spec, catalog)
    if spec.data_version.cohort_hash != cohort.digest():
        raise ExperimentSpecError("cohort_hash_mismatch")
    if spec.data_version.frozen_store_hash != cohort.store_hash():
        raise ExperimentSpecError("store_hash_mismatch")


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent, capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def rule_labels(cohort: SimCohort, catalog: SourceCatalog) -> dict[str, str]:
    """규칙 GT (순환적, metrics 참고): 정답 사실 전체를 문서 검증된 기록으로 넣어 같은 평가기로 확정된 쌍만 라벨."""
    ev = OfflineEvaluator(cohort.trials, cohort.reference_date, cohort.judge)
    states = {}
    aliases = cohort.aliases()
    for p in cohort.patients:
        alias = aliases[p.key]
        facts = [make_fact(alias, f, seq=0, origin=f"truth{i}", source_type="clinical_record", verified=True,
                           ref_kind="document", ref=None) for i, f in enumerate(cohort.oracle.truth[p.key])]
        states[alias] = ev.evaluate(alias, facts, p.trials)[0]
    co = CohortState.from_decision_states(states)
    return {p.pair.key: label_of(p.status) for p in co.pairs()}


def run(spec: ExperimentSpec, cohort: SimCohort, catalog: SourceCatalog | None = None,
        responses: dict[str, SourceResponse] | None = None) -> RunResult:
    catalog = catalog or SourceCatalog()
    _check(spec, cohort, catalog)
    policy = make_policy(spec.policy_id, catalog=catalog, **_policy_kwargs(spec.policy_id, spec.policy_config,
                                                                           spec.seed))
    if policy.version != spec.policy_version:
        raise ExperimentSpecError("policy_version_mismatch")
    model = ResponseModel(spec.seed, responses)
    env = CohortEnvironment(cohort, catalog, model)
    ev = OfflineEvaluator(cohort.trials, cohort.reference_date, cohort.judge)
    run_id = f"sim-{spec.spec_hash()}"

    facts, states, snaps = {}, {}, {}
    aliases = cohort.aliases()
    cands_of = {}
    for p in cohort.patients:
        alias = aliases[p.key]
        cands_of[alias] = p.trials
        facts[alias] = [make_fact(alias, f, seq=0, origin=f"public{i}", source_type=f.source_type,
                                  verified=f.verified, ref_kind="input_text", ref=None) for i, f in enumerate(p.public)]
        states[alias], snaps[alias] = ev.evaluate(alias, facts[alias], p.trials)
    co = initial = CohortState.from_decision_states(states)
    trial_ids = {a: frozenset(s.trials) | frozenset(s.not_evaluated_trials) for a, s in states.items()}

    ledger = EffortLedger(spec.budget, catalog)
    tried: dict[tuple, int] = {}
    records, issues, results, steps = [], [], [], []
    progress, elapsed, step, suppressed, unfunded = [], 0.0, 0, 0, 0
    termination, rounds = "horizon_reached", 0
    for rnd in range(spec.budget.horizon_steps):
        cands = policy.propose(co)
        fresh = [a for a in cands if a.action_type in NON_EXECUTING or _repeat_key(a) not in tried]
        suppressed += len(cands) - len(fresh)
        plan = policy.select(fresh, spec.budget, ledger, co)
        unfunded += len(plan.safety_unfunded)
        if not plan.selected:
            over = any(s.reason_code == "over_budget" for s in plan.skipped) or plan.safety_unfunded
            termination = "budget_exhausted" if over else "no_selectable_action"
            break
        rounds += 1
        round_latency = 0.0
        for action in plan.selected:
            alias = action.patient_alias
            # 응답 난수의 시도 번호 = 같은 (환자, 지문, 유형, 정보원)을 시도한 횟수 (반복을 막으므로 항상 0). 정보원마다 따로
            # 세어, 다른 정보원에 먼저 물어본 횟수(바꿔 묻기 설정 등)가 이 정보원의 응답 운을 바꾸지 않게 함 (공통 난수)
            attempt = tried.get(_repeat_key(action), 0)
            ref = f"sim:{step}"
            ex = env.execute(action, attempt, step, ref, facts[alias])
            tried[_repeat_key(action)] = tried.get(_repeat_key(action), 0) + 1
            cost, keys = ZERO_COST, ()
            if ex.execution != "not_executed":
                cost, key = ledger.charge(action)  # 선택 때 같은 원장 사본으로 확인한 순서 그대로 -> 초과 없음
                keys = (key,) if key else ()
                round_latency = max(round_latency, ex.latency_minutes)
            before, fb = co, snaps[alias]
            reevaluated = frozenset()
            mid = co
            if ex.facts or ex.verified:
                checked = set(ex.verified)
                facts[alias] = [verify_fact(f, action.source_id) if f.fact_id in checked else f
                                for f in facts[alias]] + list(ex.facts)
                states[alias], snaps[alias] = ev.evaluate(alias, facts[alias], cands_of[alias])
                mid = CohortState.from_decision_states(states, dict(co.ops))
                reevaluated = trial_ids[alias]
            status = _STATUS[ex.execution]
            outcome = ActionOutcome(action_id=action.action_id, status=status,
                                    new_fact_ids=tuple(sorted(f.fact_id for f in ex.facts)) if status == "succeeded" else (),
                                    cost_charged=cost, charge_keys=keys, error_code=ex.error_code,
                                    latency_minutes=ex.latency_minutes)
            after, ptrans = policy.apply_outcome(mid, action, outcome)
            asked = None if ex.execution == "not_executed" else rp.AskedQuestion(
                question_id=f"q{step}", action_id=action.action_id, target_criteria=action.target_criteria,
                purpose=action.purpose, source_ref=ref)
            obs = rp.StepObservation(
                run_id=run_id, step=step, action=action, execution=ex.execution, before=before, after=after,
                facts_before=fb, facts_after=snaps[alias], asked=asked,
                error_code=ex.error_code if ex.execution == "failed" else None, reevaluated_trials=reevaluated,
                explicit_fact_links=tuple(f.fact_id for f in ex.facts), policy_transitions=ptrans, cost=cost,
                charge_keys=keys, latency_minutes=ex.latency_minutes)
            res = rp.build_step(obs)
            results.append(res)
            if res.record is not None:
                if res.record.outcome != outcome:
                    raise RuntimeError("record_outcome_differs_from_policy_outcome")
                records.append(res.record)
            issues.extend(res.issues)
            steps.append({"action_type": action.action_type, "execution": ex.execution, "note": ex.note,
                          "patient_alias": alias, "error_code": ex.error_code, "reason_code": action.reason_code})
            co = after
            step += 1
        elapsed += round_latency
        progress.append(sum(p.status.value.startswith("CONFIRMED_") for p in co.pairs()))
    else:
        rounds = spec.budget.horizon_steps

    audit = rp.run_status(results)
    metrics = summarize(initial=initial, final=co, labels=rule_labels(cohort, catalog), steps=steps,
                        per_patient_minutes=dict(ledger.per_patient), total_cost=ledger.total,
                        elapsed_minutes=elapsed, rounds=rounds, progress=progress, termination=termination,
                        repeat_suppressed=suppressed, safety_unfunded=unfunded,
                        audit={"records": audit.records, "warnings": audit.warnings, "blocking": audit.blocking,
                               "status": audit.status})
    result_hash = canonical_hash({"records": [r.model_dump(mode="json") for r in records],
                                  "issues": [i.model_dump(mode="json") for i in issues], "metrics": metrics})
    manifest = {
        "runner_version": RUNNER_VERSION, "run_id": run_id, "spec": spec.model_dump(mode="json"),
        "spec_hash": spec.spec_hash(), "result_hash": result_hash, "contract_version": CONTRACT_VERSION,
        "catalog_claims": catalog_claims(catalog), "claims": CLAIMS,
        "cohort": {"cohort_id": cohort.cohort_id, "cohort_hash": cohort.digest(), "store_hash": cohort.store_hash(),
                   "patients": len(cohort.patients), "trials": len(cohort.trials)},
        "evaluator": {"id": ev.evaluator_id, "scope": EVALUATOR_SCOPE if cohort.judge is None else JUDGE_SCOPE,
                      "judge_digest": cohort.judge.digest() if cohort.judge is not None else None,
                      "judge_include_draft": bool(getattr(cohort.judge, "include_draft", False)),
                      "ground_truth": metrics["rule_gt"]["kind"]},
        "response_model": model.describe(), "rephrase": bool(spec.policy_config.get("rephrase", False)),
        "llm": {"real_calls": 0, "offline_matcher_requests": ev.client.requests},
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "commit": _git_commit()},
    }
    return RunResult(spec, records, issues, metrics, manifest, co)


def compare(cohort: SimCohort, policy_ids: list[str], budgets: list[EffortBudget], seeds: list[int],
            catalog: SourceCatalog | None = None, policy_configs: dict[str, dict] | None = None,
            responses: dict[str, SourceResponse] | None = None, rephrase: list[bool] = (False,)) -> list[dict]:
    """같은 코호트·카탈로그·응답 모델·seed로 정책을 돌린 비교표 (M10). 행 = (바꿔 묻기, 예산, seed, 정책).

    rephrase: "모름" 뒤 다른 말로 한 번 더 물을지. 기본 [False], 민감도 분석은 [False, True] (11-06 결정)."""
    catalog = catalog or SourceCatalog()
    rows = []
    for rp_on in rephrase:
        for budget in budgets:
            for seed in seeds:
                for pid in policy_ids:
                    cfg = dict((policy_configs or {}).get(pid, {}))
                    if rp_on:
                        cfg["rephrase"] = True
                    spec = build_spec(f"cmp-{pid}-{seed}", pid, cohort, budget, seed, cfg, catalog)
                    rows.append(_row(pid, cfg, rp_on, budget, seed, run(spec, cohort, catalog, responses)))
    return rows


def _row(pid: str, cfg: dict, rp_on: bool, budget: EffortBudget, seed: int, r: RunResult) -> dict:
    m = r.metrics
    return {"policy_id": pid, "policy_config": cfg, "rephrase": rp_on, "budget_minutes": budget.coordinator_minutes,
            "seed": seed, "confirmed_eligible": m["system_confirmed_eligible"],
            "confirmed_ineligible": m["system_confirmed_ineligible"], "confirmed_share": m["system_confirmed_share"],
            "coordinator_minutes": m["active_minutes"]["coordinator"],
            "clinician_minutes": m["active_minutes"]["clinician"], "elapsed_minutes": m["elapsed_minutes"],
            "actions": sum(m["actions_executed"].values()), "questions": m["patient_burden"]["questions"],
            "rephrased": m["patient_burden"]["rephrased"], "no_answer": m["patient_burden"]["no_answer"],
            "rule_gt_disagreements": m["rule_gt"]["rule_gt_disagreements"],
            "false_confirmed_eligible_vs_rule_gt": m["rule_gt"]["confirmed_eligible_gt_ineligible"],
            "termination": m["termination"], "audit": m["audit"]["status"], "result_hash": r.result_hash}


__all__ = ["run", "build_spec", "compare", "rule_labels", "RunResult", "RUNNER_VERSION", "CLAIMS"]
