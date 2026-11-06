"""비용 원장 (WP5 / M2·M7): 공유 코디네이터 능동 작업시간 예산과 정보원 단위 중복 청구 방지.

- 요청 비용(request_cost)은 (환자, 정보원)당 한 번만 청구합니다. 같은 문서를 다시 요청하는 것은 비용 0이 아니라
  '이미 받은 문서에서 항목 확인'으로 보고 항목 비용(per_item_minutes)만 더합니다.
- 같은 (환자, 정보원, 기준) 항목은 두 번 청구하지 않습니다.
- 바꿔 묻기(reason_code REPHRASE, "모름" 뒤 다른 말로 다시 묻는 질문)는 같은 질문이 아니라 새 연락이므로 별도 키
  "<환자>|<정보원>#rephrase"로 요청 비용과 항목 비용을 다시 청구합니다. 같은 환자의 바꿔 묻기끼리는 한 연락으로 묶음.
- 환자 응답 대기(patient_wait_minutes)는 예산에서 빼지 않습니다(능동 시간 아님).
- 예산을 넘는 청구는 BudgetExceeded로 거부합니다(부분 청구 없음). 웹 LLM 비용 원장(web/budget.py)과는 별개입니다.
"""

from __future__ import annotations

import copy
from collections import defaultdict

from .bundles import SourceCatalog, charge_key, items_of
from .contracts import REPHRASE, ZERO_COST, Action, EffortBudget, EffortCost

EPS = 1e-9
REPHRASE_KEY = "#rephrase"  # 바꿔 묻기 연락의 청구 키 접미사


class BudgetExceeded(RuntimeError):
    pass


class EffortLedger:
    def __init__(self, budget: EffortBudget, catalog: SourceCatalog):
        self.budget = budget
        self.catalog = catalog
        self.total = ZERO_COST
        self.per_patient: dict[str, float] = defaultdict(float)  # 코디네이터 분
        self.requested: set[str] = set()  # 요청 비용을 청구한 (환자|정보원)
        self.items: set[tuple[str, str]] = set()  # (환자|정보원, 항목)
        self.charges: list[tuple[str, str, EffortCost]] = []  # (action_id, charge_key, 비용)

    def copy(self) -> "EffortLedger":
        new = copy.copy(self)
        new.per_patient = defaultdict(float, self.per_patient)
        new.requested, new.items, new.charges = set(self.requested), set(self.items), list(self.charges)
        return new

    def quote(self, action: Action) -> tuple[EffortCost, str | None, tuple[str, ...]]:
        """(이 행동의 추가 비용, 청구 키, 새로 청구할 항목). 정보원이 없으면 0."""
        src = self.catalog.get(action.source_id)
        if src is None:
            return ZERO_COST, None, ()
        key = charge_key(action.patient_alias, src.source_id) + (REPHRASE_KEY if action.reason_code == REPHRASE else "")
        new_items = tuple(i for i in dict.fromkeys(items_of(action)) if (key, i) not in self.items)
        base = ZERO_COST if key in self.requested else src.request_cost
        if key in self.requested and not new_items:
            return ZERO_COST, key, ()
        cost = base.model_copy(update={"coordinator_minutes":
                                       base.coordinator_minutes + src.per_item_minutes * len(new_items)})
        return cost, key, new_items

    def fits(self, cost: EffortCost, alias: str) -> bool:
        b = self.budget
        if self.total.coordinator_minutes + cost.coordinator_minutes > b.coordinator_minutes + EPS:
            return False
        if self.total.clinician_minutes + cost.clinician_minutes > b.clinician_minutes + EPS:
            return False
        if b.llm_cost_usd is not None and self.total.llm_cost_usd + cost.llm_cost_usd > b.llm_cost_usd + EPS:
            return False
        if b.per_patient_cap_minutes is not None and \
                self.per_patient[alias] + cost.coordinator_minutes > b.per_patient_cap_minutes + EPS:
            return False
        return True

    def charge(self, action: Action) -> tuple[EffortCost, str | None]:
        cost, key, new_items = self.quote(action)
        if not self.fits(cost, action.patient_alias):
            raise BudgetExceeded("예산 초과 청구 거부")
        if key is not None:
            self.requested.add(key)
            self.items.update((key, i) for i in new_items)
        self.total = self.total + cost
        self.per_patient[action.patient_alias] += cost.coordinator_minutes
        self.charges.append((action.action_id, key or "", cost))
        return cost, key

    def remaining_minutes(self) -> float:
        return self.budget.coordinator_minutes - self.total.coordinator_minutes
