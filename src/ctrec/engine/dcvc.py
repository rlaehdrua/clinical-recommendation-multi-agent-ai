"""제안 정책 full_dcvc (M1 DCV -> C). **정의 합의 전 초안** - 팀 결정(#13 안건 ①) 뒤 기본값·식이 바뀔 수 있습니다.

DCV (후보 고르기): LegacyAdapterPolicy.propose 그대로 + 결정 영향 필터.
  행동이 겨냥하는 시험 중 열린 쌍(확정·확보 실패 보류·평가 실패가 아니고 모집 중)이 하나도 없으면 후보에서 뺌.
  안전 필수 행동·임상의 검토·보류 기록은 빼지 않음 (M1: DCV가 필수 검증·전문가 이관을 제거하면 안 됨).
C (코호트 단위 선택): 안전 필수 -> 환자별 최소 배정까지는 기준선과 같고, 남은 예산은 매번
  점수 = sum(1 / 그 쌍의 남은 필수 기준 수, 행동이 움직일 수 있는 열린 쌍마다) / 지금 원장 기준 추가 비용(분)
  이 가장 큰 행동을 고름. 확정에 가까운 쌍을 여러 시험에 걸쳐 움직이는 행동이 먼저이고, 같은 (환자, 정보원)을 이미
  요청했으면 추가 비용이 줄어 묶음이 유리해짐. 점수는 구조적 개수뿐이며 성공 확률을 쓰지 않음 (M9).

끄는 스위치 (ablation, M10)
- dcv_impact=False: 결정 영향 필터 끔
- bundles=False: 점수의 비용을 묶음 할인 없이 행동의 단독 비용(expected_cost)으로 계산 (청구는 원장 규칙 그대로)
- fairness_floor=False: 환자별 최소 배정 끔
- recovery=False: 실패 뒤 대체 정보원을 찾지 않고 바로 보류
"""

from __future__ import annotations

from .cohort_state import CohortState
from .contracts import NON_EXECUTING, Action, EffortBudget, PairStatus, SkippedAction
from .cost import EffortLedger
from .policy import LegacyAdapterPolicy

DCVC_VERSION = "0.1-draft"
MIN_COST = 0.5  # 분. 추가 비용이 0인 행동(이미 요청한 문서의 같은 항목 등)이 점수를 무한대로 만들지 않게
_CLOSED = frozenset({PairStatus.CONFIRMED_ELIGIBLE, PairStatus.CONFIRMED_INELIGIBLE, PairStatus.BLOCKED_FAILURE,
                     PairStatus.NOT_EVALUATED})


class FullDCVCPolicy(LegacyAdapterPolicy):
    policy_id = "full_dcvc"
    version = DCVC_VERSION

    def __init__(self, *, dcv_impact: bool = True, bundles: bool = True, fairness_floor: bool = True,
                 recovery: bool = True, **kw):
        super().__init__(**kw)
        self.dcv_impact, self.bundles, self.fairness_floor, self.recovery = dcv_impact, bundles, fairness_floor, recovery

    @property
    def switches(self) -> dict[str, bool]:
        return {"dcv_impact": self.dcv_impact, "bundles": self.bundles, "fairness_floor": self.fairness_floor,
                "recovery": self.recovery}

    # ------------------------------------------------------------ DCV
    @staticmethod
    def open_targets(cohort: CohortState, a: Action) -> list:
        out = []
        for tid in a.target_trial_ids:
            p = cohort.pair(a.patient_alias, tid)
            if p is not None and p.open and p.status not in _CLOSED:
                out.append(p)
        return out

    def propose(self, cohort: CohortState, pending_reevaluation=None) -> list[Action]:
        cands = super().propose(cohort, pending_reevaluation)
        if not self.dcv_impact:
            return cands
        return [a for a in cands if a.safety_required or a.action_type in NON_EXECUTING
                or a.action_type == "CLINICIAN_REVIEW" or not a.target_trial_ids or self.open_targets(cohort, a)]

    # ------------------------------------------------------------ C
    def floor_minutes(self, budget: EffortBudget) -> float:
        return budget.per_patient_floor_minutes if self.fairness_floor else 0.0

    def impact(self, cohort: CohortState, a: Action) -> float:
        """행동이 움직일 수 있는 열린 쌍마다 1/남은 필수 기준 수의 합 (구조적 점수)."""
        total = 0.0
        for p in self.open_targets(cohort, a):
            n = cohort.remaining_required(a.patient_alias, p.pair.trial_id)
            if n > 0:
                total += 1.0 / n
        return total

    def cost_minutes(self, a: Action, led: EffortLedger) -> float:
        cost = led.quote(a)[0] if self.bundles else a.expected_cost
        return max(cost.coordinator_minutes + cost.clinician_minutes, MIN_COST)

    def score(self, cohort: CohortState, a: Action, led: EffortLedger) -> float:
        return self.impact(cohort, a) / self.cost_minutes(a, led)

    def fill(self, rest: list[Action], led: EffortLedger, take, skipped: list, done: set,
             cohort: CohortState | None) -> None:
        if cohort is None:  # 점수에 쌍 상태가 필요함 -> 없으면 기준선 순서
            return super().fill(rest, led, take, skipped, done, cohort)
        todo = sorted((a for a in rest if a.action_id not in done), key=self.order_key)  # 동점이면 기존 순서
        impact = {a.action_id: self.impact(cohort, a) for a in todo}
        while todo:
            i = max(range(len(todo)), key=lambda j: (impact[todo[j].action_id] / self.cost_minutes(todo[j], led), -j))
            best = todo.pop(i)
            if not take(best):
                skipped.append(SkippedAction(action_id=best.action_id, reason_code="over_budget"))
                done.add(best.action_id)


__all__ = ["FullDCVCPolicy", "DCVC_VERSION"]
