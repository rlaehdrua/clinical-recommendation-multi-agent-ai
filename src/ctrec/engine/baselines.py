"""기준선 정책 (M10, WP6 설계 §5). 순수 함수, LLM·네트워크 없음. 웹(WP8)과 시뮬레이터가 같은 클래스를 씁니다 (M14).

공정한 비교를 위해 모든 정책이 같은 것을 공유하고 **남은 예산을 채우는 순서만** 다릅니다.
- 후보: LegacyAdapterPolicy.propose (같은 DCV 후보 목록, "모름" 뒤 대체 정보원 규칙 포함)
- 결과 반영: LegacyAdapterPolicy.apply_outcome (운영 상태만)
- 선택 규칙: 안전 필수 행동 먼저(예산이 모자라면 safety_unfunded로 보고하고 그 환자 중단), 중복 제거, 예산 초과 거부,
  같은 (환자, 정보원) 요청 비용 한 번만 청구, 예산에 정한 환자별 최소 배정. 안전 필수 행동은 정책 선택이 아니라 제약입니다.

| policy_id | 순서 |
|---|---|
| no_action | 아무것도 실행하지 않음 (최초 판정 기준점, P0) |
| random | seed와 행동 ID로 정한 무작위 순서 (같은 seed·같은 행동이면 같은 순위 - 공통 난수) |
| patient_order | 환자 별칭 순서로 한 명씩, 환자 안에서는 기존 정책 순서 (환자별 독립, P4) |
| cheapest_first | 예상 코디네이터 작업시간이 적은 행동부터 (묶음 할인은 보지 않음) |
| legacy_greedy | 기존 LegacyAdapterPolicy (참고용) |
| full_dcvc | engine.dcvc.FullDCVCPolicy (제안 정책, 정의 합의 전 초안) |

우선순위에 성공 확률 같은 근거 없는 추정치를 쓰지 않습니다 (M9).
"""

from __future__ import annotations

import hashlib

from .contracts import NON_EXECUTING, Action, EffortBudget, Plan, SkippedAction
from .cost import EffortLedger
from .policy import LegacyAdapterPolicy

BASELINE_VERSION = "0.1-provisional"


class NoActionPolicy(LegacyAdapterPolicy):
    """P0: 추가 확인 없음. 후보는 모두 실행하지 않은 것으로 남김 (안전 필수 행동도 실행하지 않았음을 skipped로 보고)."""
    policy_id = "no_action"
    version = BASELINE_VERSION

    def select(self, cands: list[Action], budget: EffortBudget, ledger: EffortLedger | None = None,
               cohort=None) -> Plan:
        held = tuple(a for a in cands if a.action_type in NON_EXECUTING)
        todo = dict.fromkeys(a.action_id for a in cands if a.action_type not in NON_EXECUTING)
        skipped = tuple(SkippedAction(action_id=x, reason_code="no_action_policy") for x in todo)
        return Plan(policy_id=self.policy_id, policy_version=self.version, skipped=skipped, held=held)


class RandomPolicy(LegacyAdapterPolicy):
    """무작위 순서. 순위 = sha256(seed, 행동 ID) -> 같은 seed면 같은 상태에서 같은 계획 (재현 가능)."""
    policy_id = "random"
    version = BASELINE_VERSION

    def __init__(self, seed: int = 0, **kw):
        super().__init__(**kw)
        self.seed = int(seed)

    def order_key(self, a: Action) -> tuple:
        rank = hashlib.sha256(f"{self.seed}:{a.action_id}".encode()).hexdigest()
        return (0 if a.safety_required else 1, rank)


class PatientOrderPolicy(LegacyAdapterPolicy):
    """환자별 독립 (P4): 별칭 순서로 한 환자의 행동을 기존 정책 순서대로 먼저 채우고 다음 환자로."""
    policy_id = "patient_order"
    version = BASELINE_VERSION

    @staticmethod
    def order_key(a: Action) -> tuple:
        return (0 if a.safety_required else 1, a.patient_alias, a.priority, a.sequence, a.fingerprint)

    def floor_minutes(self, budget: EffortBudget) -> float:
        return 0.0  # 환자를 끝까지 처리하는 순서이므로 최소 배정으로 다른 환자에게 먼저 나누지 않음


class CheapestFirstPolicy(LegacyAdapterPolicy):
    """예상 비용이 낮은 행동부터 (expected_cost의 코디네이터 분, 같으면 기존 순서)."""
    policy_id = "cheapest_first"
    version = BASELINE_VERSION

    @staticmethod
    def order_key(a: Action) -> tuple:
        return (0 if a.safety_required else 1, a.expected_cost.coordinator_minutes, a.priority, a.patient_alias,
                a.sequence, a.fingerprint)


def policies() -> dict[str, type[LegacyAdapterPolicy]]:
    from .dcvc import FullDCVCPolicy  # 순환 import 방지
    return {"no_action": NoActionPolicy, "random": RandomPolicy, "patient_order": PatientOrderPolicy,
            "cheapest_first": CheapestFirstPolicy, "legacy_greedy": LegacyAdapterPolicy, "full_dcvc": FullDCVCPolicy}


def make_policy(policy_id: str, **kw) -> LegacyAdapterPolicy:
    """policy_id -> 정책 객체. random은 seed, full_dcvc는 스위치(dcv_impact·bundles·fairness_floor·recovery)를 받음."""
    table = policies()
    if policy_id not in table:
        raise ValueError(f"알 수 없는 정책: {policy_id!r} (가능: {sorted(table)})")
    return table[policy_id](**kw)


__all__ = ["NoActionPolicy", "RandomPolicy", "PatientOrderPolicy", "CheapestFirstPolicy", "make_policy", "policies"]
