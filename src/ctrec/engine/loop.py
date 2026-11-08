"""공통 엔진 단계 (P). 시뮬레이터(sim.runner)와 웹·CLI(web.engine_runner)가 **같은 객체·같은 함수**로 행동을 고르고
결과를 반영합니다. 판정·사실은 바꾸지 않습니다 (판정은 각 경로의 평가기가 같은 decision_state.build_state로 만듦).

한 라운드
1. plan(cohort): policy.propose -> 반복 억제(fresh) -> policy.select(남은 예산 원장)
   반복 억제: 같은 사실 상태(지문)에서 같은 정보원에 이미 시도한 같은 행동은 다시 고르지 않음 (repeat_key).
   "모름" 뒤 정책이 같은 지문을 문서 요청으로 바꾼 행동, 바꿔 묻기(지문 ".rephrase")는 다른 키. 보류 기록(NON_EXECUTING)은 그대로.
2. dispatch(plan, can_execute, capacity): 선택된 행동을 계획 순서대로 실행할 것 / 이 경로에서 실행할 수 없는 것 / 한도로
   이번 라운드에 못 한 것으로 나눔. 웹은 질문으로 바꿀 수 있는지(render)와 라운드당 질문 수, 시뮬레이터는 모두 실행 가능·한도 없음.
   한도로 못 한 행동은 시도로 세지 않음 (다음 라운드에 다시 고를 수 있음).
3. begin(action, executed): 시도로 기록하고, 실제로 실행했을 때만 원장에 청구 (실패·무응답도 청구, 미실행은 0).
4. settle(cohort, action, outcome): policy.apply_outcome (운영 상태만). 실행할 수 없던 행동은 not_executed 결과로 정산.

경로마다 다른 것은 실행 수단뿐입니다: 시뮬레이터는 CohortEnvironment(숨긴 정답을 쥔 유일한 곳)가 결과를 만들고, 웹은 화면의
답(engine.answers.answer_outcome)이 결과를 만듭니다. 정책은 정답·숨긴 값·성공 확률을 받지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .cohort_state import CohortState
from .contracts import NON_EXECUTING, ZERO_COST, Action, ActionOutcome, EffortBudget, EffortCost, PairTransition, Plan
from .cost import EffortLedger
from .policy import LegacyAdapterPolicy

LOOP_VERSION = "engine_loop/0.1"


def repeat_key(a: Action) -> tuple:
    """같은 행동의 재실행 판단: (환자, 지문, 유형, 정보원)."""
    return (a.patient_alias, a.fingerprint, a.action_type, a.source_id)


@dataclass(frozen=True)
class Dispatch:
    execute: tuple[Action, ...]  # 이번 라운드에 실행할 행동 (계획 순서)
    not_executable: tuple[Action, ...]  # 이 경로에 실행 수단이 없는 행동 (not_executed로 정산)
    deferred: tuple[Action, ...]  # 라운드 한도로 이번에 못 한 행동 (시도로 세지 않음)


@dataclass
class EngineLoop:
    policy: LegacyAdapterPolicy
    budget: EffortBudget
    ledger: EffortLedger | None = None
    tried: dict[tuple, int] = field(default_factory=dict)
    suppressed: int = 0  # 반복 억제로 뺀 후보 수 (plan 호출 기준)

    def __post_init__(self):
        if self.ledger is None:
            self.ledger = EffortLedger(self.budget, self.policy.catalog)
        if self.ledger.budget != self.budget:
            raise ValueError("원장의 예산과 루프 예산이 다름")

    # ------------------------------------------------------------ 1. 계획
    def fresh(self, cands: list[Action]) -> list[Action]:
        return [a for a in cands if a.action_type in NON_EXECUTING or repeat_key(a) not in self.tried]

    def preview(self, cohort: CohortState, keep: Callable[[Action], bool] | None = None) -> Plan:
        """상태를 바꾸지 않는 계획 (원장·시도 기록·억제 수 그대로).

        keep: 이 경로에서 지금 실행해도 끝낼 수 없는 후보를 select 전에 뺌 (예산을 차지하지 않게). None = 모두."""
        cands = self.fresh(self.policy.propose(cohort))
        if keep is not None:
            cands = [a for a in cands if keep(a)]
        return self.policy.select(cands, self.budget, self.ledger, cohort)

    def plan(self, cohort: CohortState) -> Plan:
        cands = self.policy.propose(cohort)
        fresh = self.fresh(cands)
        self.suppressed += len(cands) - len(fresh)
        return self.policy.select(fresh, self.budget, self.ledger, cohort)

    # ------------------------------------------------------------ 2. 실행 배분
    @staticmethod
    def dispatch(plan: Plan, can_execute: Callable[[Action], bool] = lambda a: True,
                 capacity: Callable[[str], int | None] = lambda alias: None) -> Dispatch:
        """capacity(별칭): 이번 라운드에 그 환자에게 실행할 수 있는 행동 수 (None = 제한 없음)."""
        run, no, later = [], [], []
        used: dict[str, int] = {}
        for a in plan.selected:
            if not can_execute(a):
                no.append(a)
                continue
            cap = capacity(a.patient_alias)
            if cap is not None and used.get(a.patient_alias, 0) >= cap:
                later.append(a)
                continue
            used[a.patient_alias] = used.get(a.patient_alias, 0) + 1
            run.append(a)
        return Dispatch(tuple(run), tuple(no), tuple(later))

    # ------------------------------------------------------------ 3. 실행 기록·청구
    def attempt(self, a: Action) -> int:
        """같은 행동을 이미 시도한 횟수 (반복을 막으므로 보통 0). 시뮬레이터 응답 난수의 시도 번호."""
        return self.tried.get(repeat_key(a), 0)

    def begin(self, a: Action, executed: bool) -> tuple[EffortCost, tuple[str, ...]]:
        self.tried[repeat_key(a)] = self.tried.get(repeat_key(a), 0) + 1
        if not executed:
            return ZERO_COST, ()
        cost, key = self.ledger.charge(a)
        return cost, (key,) if key else ()

    # ------------------------------------------------------------ 4. 결과 반영
    def settle(self, cohort: CohortState, a: Action,
               outcome: ActionOutcome) -> tuple[CohortState, tuple[PairTransition, ...]]:
        return self.policy.apply_outcome(cohort, a, outcome)

    def settle_not_executed(self, cohort: CohortState, a: Action, note: str | None = None
                            ) -> tuple[CohortState, tuple[PairTransition, ...]]:
        return self.settle(cohort, a, ActionOutcome(action_id=a.action_id, status="not_executed", error_code=note))


__all__ = ["EngineLoop", "Dispatch", "repeat_key", "LOOP_VERSION"]
