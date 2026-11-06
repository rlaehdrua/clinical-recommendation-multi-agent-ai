"""정책 엔진 기반 (WP5 / M1·M2·M7·M9·M13, I-8). 순수 함수, LLM·네트워크·난수 없음.

LegacyAdapterPolicy
- propose(cohort): 환자마다 기존 decision_policy.candidates(깊은 사본)를 불러 행동을 만들고 엔진 Action으로 옮김.
  행동 ID = "<별칭>:<기존 지문>" (기존 action_id는 uuid라 재현되지 않으므로 옮기지 않음).
  실패한 정보원은 대체 정보원으로 바꾸고, 대체도 없으면 DEFER(sources_exhausted)로 보류.
  항목 단위 실패("모름" = no_answer)는 그 (정보원, 항목)만 막고 같은 항목을 문서 정보원으로 요청
  (alternative_source_after_no_answer). 남은 문서 정보원도 없으면 그 항목만 DEFER(item_sources_exhausted).
  같은 정보원의 다른 항목 질문은 그대로 계속합니다.
  실제 Session에서는 "모름" 뒤 기존 정책이 그 항목을 다시 묻지 않고 DEFER(user_declined_or_exhausted)를 내므로,
  그 보류도 같은 항목의 실패 기록(ops.failed_items)이 있으면 같은 규칙으로 문서 정보원 요청으로 바꿈
  (실패 기록이 없는 보류 - 엔진 밖에서 생긴 거부 등 - 는 그대로 보류).
- select(candidates, budget, ledger): 결정적 탐욕 기준선.
    1) 안전 필수(tier 1: 상충 확인·상태 재계산) 먼저. 예산이 모자라면 버리지 않고 safety_unfunded로 보고하고,
       그 환자의 나머지 행동은 실행하지 않음(blocked_by_unfunded_safety).
    2) 환자별 최소 배정(per_patient_floor_minutes): 열린 쌍이 있는 환자를 별칭 순서로 돌며 한 행동씩.
    3) 나머지는 (priority, 환자 별칭, 기존 순서)로 예산이 허락하는 만큼.
  같은 (환자, 정보원) 요청 비용은 한 번만 청구(EffortLedger). 예산 초과 청구는 원장이 거부.
- apply_outcome(cohort, action, outcome): 운영 상태(실패 정보원·실패 항목·시도 횟수·확보 소진)만 바꿈. 사실·판정은 바꾸지
  않음 (새 사실은 FactStore -> 판정 계층 재평가를 거쳐야만 판정에 반영). 실패 범위는 contracts.failure_scope:
  정보원 단위면 대체가 없을 때 대상 시험 전체를 소진, 항목 단위면 그 항목을 얻을 정보원이 남지 않을 때만 항목을 소진
  (쌍은 남은 항목이 모두 소진됐을 때만 소진 - cohort_state.items_exhausted). 소진 시 쌍 상태는 BLOCKED_FAILURE로만
  (contracts.POLICY_TRANSITIONS).

우선순위에는 구조적 점수(기존 value_score·tier)만 씁니다. 성공 확률 같은 근거 없는 추정치를 쓰지 않습니다.
"""

from __future__ import annotations

import re
from types import MappingProxyType

from ..decision_policy import DEFAULT_POLICY, PolicyConfig, candidates
from .bundles import SourceCatalog
from .cohort_state import CohortState, OpsState
from .contracts import (LEGACY_ACTION_TYPES, NON_EXECUTING, ZERO_COST, Action, ActionOutcome, EffortBudget,
                        PairTransition, Plan, SkippedAction, check_transition, failure_scope)
from .cost import EffortLedger

POLICY_ID = "legacy_adapter_greedy"
POLICY_VERSION = "0.2-provisional"
SAFETY_TIER = 1
LEGACY_DECLINED_DEFER = "user_declined_or_exhausted"  # decision_policy가 "모름"·거부 뒤 내는 보류 사유
# 기존 정책은 답을 decision_state.DECLINED_ANSWERS(좁은 목록)로만 "모름"으로 봄. 엔진(answers.answer_outcome)이 더 넓게
# 무응답으로 본 답("잘 모르겠어요" 등)은 기존 정책에서 답이 있는 것으로 남아 보류·재추출로 나옴 -> 같은 대체 규칙을 적용
LEGACY_HELD = frozenset({LEGACY_DECLINED_DEFER, "answered_but_unresolved"})
LEGACY_UNREFLECTED = "answered_not_reflected"  # REPARSE 사유
VERIFICATION_TYPES = frozenset({"ASK_PATIENT", "REQUEST_DOCUMENT", "VERIFY_RECORD", "CLINICIAN_REVIEW"})


_INFO_KEY = re.compile(r"^[A-Za-z0-9._:+\-]{1,160}$")  # contracts.Action.info_key와 같은 형식 (아니면 옮기지 않음)


def _code(s: str) -> str:
    return s if s and len(s) <= 64 else "unspecified"


def tried_key(source: str, item: str) -> str:
    """ops.attempts의 문서 요청 기록 키: 같은 (문서 정보원, 항목)을 요청했는데 gap이 남으면 다시 요청하지 않음."""
    return f"tried:{source}|{item}"


def item_key(info_key: str | None, fingerprint: str) -> str:
    """항목 단위 실패의 키: decision_state.info_key (구조화 필드·바이오마커는 시험 사이에서 공유), 없으면 행동 지문."""
    return info_key or f"fp:{fingerprint}"


class LegacyAdapterPolicy:
    policy_id = POLICY_ID
    version = POLICY_VERSION

    recovery = True  # 실패 뒤 대체 정보원 탐색 (False: 실패한 정보원·항목은 바로 보류, full_dcvc의 recovery 스위치)

    def __init__(self, cfg: PolicyConfig = DEFAULT_POLICY, catalog: SourceCatalog | None = None):
        self.cfg = cfg
        self.catalog = catalog or SourceCatalog()

    # ------------------------------------------------------------ propose
    def propose(self, cohort: CohortState, pending_reevaluation: dict[str, list[str]] | None = None) -> list[Action]:
        out: list[Action] = []
        for alias in cohort.aliases():
            st = cohort.patients[alias].legacy_copy()
            ops = cohort.ops.get(alias, OpsState())
            gaps = {c.key: c.gap for c in st.criteria()}
            legacy, _ = candidates(st, self.cfg, (pending_reevaluation or {}).get(alias))
            for seq, la in enumerate(legacy):
                out.append(self._convert(alias, seq, la, ops, gaps))
        return out

    @staticmethod
    def purpose_of(action_type: str, targets: tuple[str, ...], gaps: dict[str, str]) -> str:
        """질문 목적 (0.2). 대상 기준이 모두 '원문에 구체 기준 없음'(gap physician_review)일 때만 의사 참고용.

        판정 메타데이터(decision_state gap)로만 정하며 렌더러·LLM이 바꾸지 못함. 섞여 있으면 적격성 질문으로 두되,
        physician_review 기준은 어차피 임상의만 확정할 수 있음(CriterionResult adjudication=expert).
        """
        if action_type not in ("ASK_PATIENT", "CLINICIAN_REVIEW") or not targets:
            return "eligibility"
        return "physician_reference" if all(gaps.get(k) == "physician_review" for k in targets) else "eligibility"

    def item_alternatives(self, source: str, fields, info_key: str | None, item: str,
                          failed_sources: frozenset[str] | set[str],
                          failed_items: frozenset[tuple[str, str]] | set[tuple[str, str]]) -> list[str]:
        """항목 단위 실패 뒤 같은 항목을 얻을 문서 정보원 (실패 정보원·같은 항목이 이미 실패한 정보원 제외).
        정보원 자체의 대체 목록 다음에 그 항목의 전용 문서 정보원(route)과 그 대체를 봄. 결정적 순서."""
        cands = list(self.catalog.alternatives(source, failed_sources))
        doc = self.catalog.route("REQUEST_DOCUMENT", list(fields), info_key)
        if doc is not None:
            cands += [doc, *self.catalog.alternatives(doc, failed_sources)]
        return [s for s in dict.fromkeys(cands)
                if s != source and s not in failed_sources and (s, item) not in failed_items
                and self.catalog[s].kind == "document"]

    def _convert(self, alias: str, seq: int, la, ops: OpsState, gaps: dict[str, str] | None = None) -> Action:
        atype = LEGACY_ACTION_TYPES[la.action_type]
        source = self.catalog.route(la.action_type, la.required_fact_fields, la.info_key)
        reason = _code(la.reason_code)
        item = item_key(la.info_key if la.info_key and _INFO_KEY.match(la.info_key) else None, la.fingerprint)
        # 이미 요청한 문서(답이 있었지만 gap이 남음)는 실패한 (정보원, 항목)과 같이 대체 후보에서 뺌
        tried = {(s, item) for s in self.catalog.sources if ops.attempts.get(tried_key(s, item))}
        blocked = ops.failed_items | tried
        if not self.recovery:  # 회복 제거(ablation): 대체 정보원을 찾지 않고 바로 보류. 기존 정책의 "모름" 보류는 그대로
            if source is not None and source in ops.failed_sources:
                atype, source, reason = "DEFER", None, "sources_exhausted"
            elif source is not None and (source, item) in ops.failed_items:
                atype, source, reason = "DEFER", None, "item_sources_exhausted"
        elif source is not None and source in ops.failed_sources:
            alts = [s for s in self.catalog.alternatives(source, ops.failed_sources) if (s, item) not in ops.failed_items]
            if alts:
                source, reason = alts[0], "alternative_source_after_failure"
            else:
                atype, source, reason = "DEFER", None, "sources_exhausted"
        elif source is not None and (source, item) in ops.failed_items:
            alts = self.item_alternatives(source, la.required_fact_fields, la.info_key, item, ops.failed_sources,
                                          blocked)
            if alts:
                atype, source, reason = "REQUEST_DOCUMENT", alts[0], "alternative_source_after_no_answer"
            else:
                atype, source, reason = "DEFER", None, "item_sources_exhausted"
        elif (atype == "DEFER" and reason in LEGACY_HELD) or (la.action_type == "REPARSE"
                                                              and reason == LEGACY_UNREFLECTED):
            # 기존 정책은 "모름" 뒤 같은 항목을 다시 제안하지 않고 보류(또는 답이 있다고 보고 재추출)함 -> 엔진이 기록한
            # 항목 실패가 있으면 대체 문서로. 실패 기록이 없으면(엔진도 답으로 봄) 기존 행동 그대로
            failed = sorted(s for s, it in ops.failed_items if it == item)
            if failed:
                alts = list(dict.fromkeys(a for f in failed for a in self.item_alternatives(
                    f, la.required_fact_fields, la.info_key, item, ops.failed_sources, blocked)))
                if alts:
                    atype, source, reason = "REQUEST_DOCUMENT", alts[0], "alternative_source_after_no_answer"
                else:
                    atype, source, reason = "DEFER", None, "item_sources_exhausted"
        if atype == "REQUEST_DOCUMENT" and source is not None and (source, item) in tried:
            # 같은 문서를 이미 요청했는데 gap이 남음 -> 다음 문서 정보원, 없으면 그 항목만 보류 (같은 요청 반복 방지)
            alts = self.item_alternatives(source, la.required_fact_fields, la.info_key, item, ops.failed_sources,
                                          blocked) if self.recovery else []
            if alts:
                source, reason = alts[0], "alternative_source_after_unresolved_document"
            else:
                atype, source, reason = "DEFER", None, "requested_documents_unresolved"
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
            fingerprint=la.fingerprint, legacy_action_id=None, policy_version=self.version,
            purpose=self.purpose_of(atype, tuple(la.target_criterion_ids), gaps or {}),
            info_key=la.info_key if la.info_key and _INFO_KEY.match(la.info_key) else None)

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

        floor = self.floor_minutes(budget)  # 2) 환자별 최소 배정
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
        self.fill([a for a in rest if a.action_id not in done], led, take, skipped, done, cohort)  # 3) 나머지
        selected.sort(key=self.order_key)
        return Plan(policy_id=self.policy_id, policy_version=self.version, selected=tuple(selected),
                    skipped=tuple(skipped), held=held, total_cost=led.total, safety_unfunded=tuple(unfunded))

    def floor_minutes(self, budget: EffortBudget) -> float:
        """환자별 최소 배정(분). 하위 정책이 끌 수 있음 (full_dcvc의 fairness_floor 스위치)."""
        return budget.per_patient_floor_minutes

    def fill(self, rest: list[Action], led: EffortLedger, take, skipped: list, done: set,
             cohort: CohortState | None) -> None:
        """안전·최소 배정 뒤 남은 예산을 채우는 단계. 기본: order_key 순서대로 들어가는 것만.
        하위 정책은 순서(order_key)나 이 단계(동적 선택)만 바꾸고, 안전 필수·중복·예산 규칙은 그대로 씁니다."""
        for a in rest:
            if a.action_id in done:
                continue
            if not take(a):
                skipped.append(SkippedAction(action_id=a.action_id, reason_code="over_budget"))
                done.add(a.action_id)

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
        if action.action_type == "REQUEST_DOCUMENT" and action.source_id and outcome.status == "succeeded":
            tk = tried_key(action.source_id, item_key(action.info_key, action.fingerprint))
            attempts[tk] = attempts.get(tk, 0) + 1
        failed = set(ops.failed_sources)
        exhausted = set(ops.exhausted_trials)
        failed_items = set(ops.failed_items)
        exhausted_items = set(ops.exhausted_items)
        scope = failure_scope(outcome.status, outcome.error_code)
        reason = "sources_exhausted"
        if scope == "source" and action.source_id:
            failed.add(action.source_id)
            if not self.recovery or not self.catalog.alternatives(action.source_id, frozenset(failed)):
                exhausted.update(action.target_trial_ids)
        elif scope == "item" and action.source_id:
            item = item_key(action.info_key, action.fingerprint)
            failed_items.add((action.source_id, item))
            reason = "item_sources_exhausted"
            if action.info_key and (not self.recovery or not self.item_alternatives(
                    action.source_id, action.required_fields, action.info_key, item, frozenset(failed),
                    frozenset(failed_items))):
                exhausted_items.add(action.info_key)
        new_ops = OpsState(frozenset(failed), MappingProxyType(attempts), frozenset(exhausted),
                           frozenset(failed_items), frozenset(exhausted_items))
        new = cohort.with_ops(alias, new_ops)
        transitions = []
        for tid in action.target_trial_ids:
            before, after = cohort.pair(alias, tid), new.pair(alias, tid)
            if before and after and before.status != after.status:
                check_transition(before.status, after.status, authority="policy")
                transitions.append(PairTransition(pair=after.pair, before=before.status, after=after.status,
                                                  authority="policy", reason_code=reason,
                                                  pending_kinds=tuple(sorted(after.pending_kinds))))
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
