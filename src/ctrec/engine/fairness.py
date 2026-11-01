"""환자 간 배분 관측 (WP5 / M13). 계획(Plan)이 환자별로 작업시간·검증 기회를 어떻게 나눴는지 보고만 합니다.

정책을 바꾸지 않는 감시 지표입니다. 하위 그룹(나이·성별 등) 분석은 합성 속성이 정해진 뒤 별도로 추가합니다.
- minutes / actions / verification_opportunities: 환자별 배정 (선택된 행동 기준)
- open_pairs: 아직 작업이 남은 쌍 수
- zero_allocation: 열린 쌍이 있는데 아무 행동도 배정받지 못한 환자 (경보)
- gini_minutes: 열린 쌍이 있는 환자 사이 코디네이터 시간의 지니 계수 (0 = 균등)
"""

from __future__ import annotations

from collections import defaultdict

from .cohort_state import CohortState
from .contracts import Plan
from .cost import EffortLedger
from .policy import VERIFICATION_TYPES


def gini(values: list[float]) -> float | None:
    xs = sorted(v for v in values)
    n = len(xs)
    if n == 0:
        return None
    total = sum(xs)
    if total <= 0:
        return 0.0
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    return round((2 * cum) / (n * total) - (n + 1) / n, 6)


def allocation_report(plan: Plan, cohort: CohortState, ledger: EffortLedger) -> dict:
    """ledger: plan을 청구한 원장 (묶음·중복 방지를 반영한 환자별 실제 분). plan_and_charge()의 반환값을 씀."""
    by = defaultdict(lambda: {"minutes": 0.0, "actions": 0, "verification_opportunities": 0})
    for a in plan.selected:
        r = by[a.patient_alias]
        r["actions"] += 1
        r["verification_opportunities"] += a.action_type in VERIFICATION_TYPES
    patients = {}
    for alias in cohort.aliases():
        r = dict(by[alias])
        r["minutes"] = round(ledger.per_patient.get(alias, 0.0), 6)
        r["open_pairs"] = len(cohort.open_pairs(alias))
        r["safety_unfunded"] = sum(x.startswith(alias + ":") for x in plan.safety_unfunded)
        patients[alias] = r
    needing = [a for a, r in patients.items() if r["open_pairs"]]
    zero = [a for a in needing if patients[a]["actions"] == 0]
    mins = [patients[a]["minutes"] for a in needing]
    return {"patients": patients, "patients_with_open_pairs": len(needing), "zero_allocation": zero,
            "alarm": bool(zero), "gini_minutes": gini(mins), "min_minutes": min(mins) if mins else None,
            "max_minutes": max(mins) if mins else None}
