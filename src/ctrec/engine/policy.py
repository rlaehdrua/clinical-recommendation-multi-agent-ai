"""정책 엔진 기반 (WP5 / M1·M2·M7·M9·M13, I-8). 순수 함수, LLM·네트워크·난수 없음.

LegacyAdapterPolicy
- propose(cohort): 환자마다 기존 decision_policy.candidates(깊은 사본)를 불러 행동을 만들고 엔진 Action으로 옮김.
  행동 ID = "<별칭>:<기존 지문>" (기존 action_id는 uuid라 재현되지 않으므로 옮기지 않음).
  실패한 정보원은 대체 정보원으로 바꾸고, 대체도 없으면 DEFER(sources_exhausted)로 보류.
- select(candidates, budget, ledger): 결정적 탐욕 기준선.
    1) 안전 필수(tier 1: 상충 확인·상태 재계산) 먼저. 예산이 모자라면 버리지 않고 safety_unfunded로 보고하고,
       그 환자의 나머지 행동은 실행하지 않음(blocked_by_unfunded_safety).
    2) 환자별 최소 배정(per_patient_floor_minutes): 열린 쌍이 있는 환자를 별칭 순서로 돌며 한 행동씩.
    3) 나머지는 (priority, 환자 별칭, 기존 순서)로 예산이 허락하는 만큼.
  같은 (환자, 정보원) 요청 비용은 한 번만 청구(EffortLedger). 예산 초과 청구는 원장이 거부.
- apply_outcome(cohort, action, outcome): 운영 상태(실패 정보원·시도 횟수·확보 소진)만 바꿈. 사실·판정은 바꾸지 않음
  (새 사실은 FactStore -> 판정 계층 재평가를 거쳐야만 판정에 반영). 소진 시 쌍 상태는 BLOCKED_FAILURE로만
  (contracts.POLICY_TRANSITIONS).

우선순위에는 구조적 점수(기존 value_score·tier)만 씁니다. 성공 확률 같은 근거 없는 추정치를 쓰지 않습니다.
"""

from __future__ import annotations

from types import MappingProxyType

from ..decision_policy import DEFAULT_POLICY, PolicyConfig, candidates
from .bundles import SourceCatalog
from .cohort_state import CohortState, OpsState
from .contracts import (LEGACY_ACTION_TYPES, NON_EXECUTING, ZERO_COST, Action, ActionOutcome, EffortBudget,
                        PairTransition, Plan, SkippedAction, check_transition)
from .cost import EffortLedger

POLICY_ID = "legacy_adapter_greedy"
POLICY_VERSION = "0.1-provisional"
SAFETY_TIER = 1
VERIFICATION_TYPES = frozenset({"ASK_PATIENT", "REQUEST_DOCUMENT", "VERIFY_RECORD", "CLINICIAN_REVIEW"})


def _code(s: str) -> str:
    return s if s and len(s) <= 64 else "unspecified"


class LegacyAdapterPolicy:
    policy_id = POLICY_ID
    version = POLICY_VERSION

    def __init__(self, cfg: PolicyConfig = DEFAULT_POLICY, catalog: SourceCatalog | None = None):
        self.cfg = cfg
        self.catalog = catalog or SourceCatalog()

    # ------------------------------------------------------------ propose
    def propose(self, cohort: CohortState, pending_reevaluation: dict[str, list[str]] | None = None) -> list[Action]:
        out: list[Action] = []
        for alias in cohort.aliases():
            st = cohort.patients[alias].legacy_copy()
            ops = cohort.ops.get(alias, OpsState())
            legacy, _ = candidates(st, self.cfg, (pending_reevaluation or {}).get(alias))
            for seq, la in enumerate(legacy):
                out.append(self._convert(alias, seq, la, ops))
        return out

    def _convert(self, alias: str, seq: int, la, ops: OpsState) -> Action:
        atype = LEGACY_ACTION_TYPES[la.action_type]
        source = self.catalog.route(la.action_type, la.required_fact_fields, la.info_key)
        reason = _code(la.reason_code)
        if source is not None and source in ops.failed_sources:
            alts = self.catalog.alternatives(source, ops.failed_sources)
            if alts:
                source, reason = alts[0], "alternative_source_after_failure"
            else:
                atype, source, reason = "DEFER", None, "sources_exhausted"
        n_items = max(len(la.target_criterion_ids), 1)
        src = self.catalog.get(source)
        expected = ZERO_COST if src is None else src.request_cost.model_copy(update={
            "coordinator_minutes": src.request_cost.coordinator_minutes + src.per_item_minutes * n_items})
        return Action(
            action_id=f"{alias}:{la.fingerprint}", action_type=atype, patient_alias=alias, source_id=source,
            target_trial_ids=tuple(la.target_trial_ids), target_criteria=tuple(la.target_criterion_ids),
            required_fields=tuple(la.required_fact_fields), evidence_fact_ids=tuple(la.evidence_refs),
            expected_cost=expected, reason_code=reason, tier=la.tier, value_score=la.value_score,
            priority=la.priority, sequence=seq, safety_required=la.tier == SAFETY_TIER and atype not in NON_EXECUTING,
            fingerprint=la.fingerprint, legacy_action_id=None, policy_version=self.version)

    # ------------------------------------------------------------ select
    @staticmethod
    def order_key(a: Action) -> tuple:
        return (0 if a.safety_required else 1, a.priority, a.patient_alias, a.sequence, a.fingerprint)

    def select(self, cands: list[Action], budget: EffortBudget, ledger: EffortLedger | None = None,
               cohort: CohortState | None = None) -> Plan:
        led = (ledger or EffortLedger(budget, self.catalog)).copy()
        if led.budget != budget:
            raise ValueError("원장의 예산과 select 예산이 다름")
        held = tuple(a for a in cands if a.action_type in NON_EXECUTING)
        todo = sorted((a for a in cands if a.action_type not in NON_EXECUTING), key=self.order_key)
        selected: list[Action] = []
        skipped: list[SkippedAction] = []
        unfunded: list[str] = []
        blocked: set[str] = set()
        seen: set[str] = set()
        done: set[str] = set()

        def take(a: Action) -> bool:
            cost, _, _ = led.quote(a)
            if not led.fits(cost, a.patient_alias):
                return False
            led.charge(a)
            selected.append(a)
            done.add(a.action_id)
            return True

        dedup = []
        for a in todo:
            if a.action_id in seen:
                skipped.append(SkippedAction(action_id=a.action_id, reason_code="duplicate"))
                continue
            seen.add(a.action_id)
            dedup.append(a)

        for a in dedup:  # 1) 안전 필수
            if a.safety_required and not take(a):
                unfunded.append(a.action_id)
                blocked.add(a.patient_alias)
        rest = [a for a in dedup if a.action_id not in done and a.action_id not in unfunded]
        for a in rest:
            if a.patient_alias in blocked and not a.safety_required:
                skipped.append(SkippedAction(action_id=a.action_id, reason_code="blocked_by_unfunded_safety"))
                done.add(a.action_id)
        rest = [a for a in rest if a.action_id not in done]

        floor = budget.per_patient_floor_minutes  # 2) 환자별 최소 배정
        if floor > 0:
            eligible = sorted({a.patient_alias for a in rest}
                              & ({x for x in cohort.aliases() if cohort.open_pairs(x)} if cohort else
                                 {a.patient_alias for a in rest}))
            progress = True
            while progress:
                progress = False
                for alias in eligible:
                    if led.per_patient[alias] >= floor - 1e-9:
                        continue
                    nxt = next((a for a in rest if a.patient_alias == alias and a.action_id not in done), None)
                    while nxt is not None and not take(nxt):
                        skipped.append(SkippedAction(action_id=nxt.action_id, reason_code="over_budget"))
                        done.add(nxt.action_id)
                        nxt = next((a for a in rest if a.patient_alias == alias and a.action_id not in done), None)
                    if nxt is not None:
                        progress = True
        for a in rest:  # 3) 나머지
            if a.action_id in done:
                continue
            if not take(a):
                skipped.append(SkippedAction(action_id=a.action_id, reason_code="over_budget"))
                done.add(a.action_id)
        selected.sort(key=self.order_key)
        return Plan(policy_id=self.policy_id, policy_version=self.version, selected=tuple(selected),
                    skipped=tuple(skipped), held=held, total_cost=led.total, safety_unfunded=tuple(unfunded))

    # ------------------------------------------------------------ outcome
    def apply_outcome(self, cohort: CohortState, action: Action,
                      outcome: ActionOutcome) -> tuple[CohortState, tuple[PairTransition, ...]]:
        """운영 상태만 갱신. 반환: (새 코호트, 정책 권한 쌍 상태 전이 - BLOCKED_FAILURE로의 보류만)."""
        if outcome.action_id != action.action_id:
            raise ValueError("outcome과 action이 다름")
        alias = action.patient_alias
        ops = cohort.ops.get(alias, OpsState())
        attempts = dict(ops.attempts)
        attempts[action.fingerprint] = attempts.get(action.fingerprint, 0) + 1
        failed = set(ops.failed_sources)
        exhausted = set(ops.exhausted_trials)
        if outcome.status in ("failed", "no_response") and action.source_id:
            failed.add(action.source_id)
            if not self.catalog.alternatives(action.source_id, frozenset(failed)):
                exhausted.update(action.target_trial_ids)
        new_ops = OpsState(frozenset(failed), MappingProxyType(attempts), frozenset(exhausted))
        new = cohort.with_ops(alias, new_ops)
        transitions = []
        for tid in action.target_trial_ids:
            before, after = cohort.pair(alias, tid), new.pair(alias, tid)
            if before and after and before.status != after.status:
                check_transition(before.status, after.status, authority="policy")
                transitions.append(PairTransition(pair=after.pair, before=before.status, after=after.status,
                                                  authority="policy"))
        return new, tuple(transitions)


def plan_cohort(policy: LegacyAdapterPolicy, cohort: CohortState, budget: EffortBudget,
                ledger: EffortLedger | None = None) -> Plan:
    return policy.select(policy.propose(cohort), budget, ledger, cohort)


def plan_and_charge(policy: LegacyAdapterPolicy, cohort: CohortState, budget: EffortBudget,
                    ledger: EffortLedger | None = None) -> tuple[Plan, EffortLedger]:
    """계획을 만들고 선택된 행동을 새 원장 사본에 청구 (넘긴 원장은 바꾸지 않음)."""
    led = (ledger or EffortLedger(budget, policy.catalog)).copy()
    plan = policy.select(policy.propose(cohort), budget, led, cohort)
    for a in plan.selected:
        led.charge(a)
    return plan, led


__all__ = ["LegacyAdapterPolicy", "plan_cohort", "plan_and_charge", "POLICY_ID", "POLICY_VERSION"]
