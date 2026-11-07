"""oracle_upper: 숨긴 값과 응답 결과를 아는 참고 정책 (설계 §5). 시뮬레이터 전용 - oracle에 접근하는 유일한 정책.

정책 비교표의 경쟁 정책이 아니라 "같은 행동 공간에서 미래를 알면 어디까지 가는가"를 보여주는 참고선입니다.
- 후보 행동: 다른 정책과 같은 LegacyAdapterPolicy.propose (같은 행동 공간·같은 비용 원장·같은 예산 규칙).
- 미리 보기: 환경(CohortEnvironment)으로 각 행동의 결과를 실행 전에 봄. 실행기가 같은 행동을 반복하지 않으므로 시도 번호는
  항상 0이고, 미리 본 결과 = 실제 결과 (공통 난수).
- 실패할 행동: 환자가 모르는 값을 묻는 질문 등은 "모름"을 기다리지 않고, 그 항목을 담은 문서 정보원 중 성공할 것으로 바로
  바꿈(oracle_document_first). 성공할 대체가 없으면 실행하지 않음(skipped oracle_will_fail). 안전 필수 행동은 그대로 둠.
- 순서: 안전 필수 -> 규칙 GT가 적격(판단 기준 대기 적격 포함)인 열린 쌍을 겨냥한 행동 -> 나머지, 같은 묶음 안에서는 예상 비용이 낮은 순.

해석: 탐욕 선택이므로 **엄밀한 최적값이 아닙니다** (최적은 설계 §5의 소규모 완전탐색, 미구현). 정답 라벨은 순환적 규칙 GT.
웹에서는 쓰지 않습니다 (oracle이 필요).
"""

from __future__ import annotations

from ..engine.bundles import SourceCatalog
from ..engine.cohort_state import CohortState, OpsState
from ..engine.contracts import NON_EXECUTING, Action, EffortBudget, Plan, SkippedAction
from ..engine.cost import EffortLedger
from ..engine.policy import LegacyAdapterPolicy, base_fingerprint, item_key
from .environment import CohortEnvironment
from .metrics import ELIGIBLE, PENDING_EXPERT

ORACLE_ID = "oracle_upper"
ORACLE_VERSION = "oracle_upper/0.1"
DOCUMENT_FIRST = "oracle_document_first"
WILL_FAIL = "oracle_will_fail"


class OracleUpperPolicy(LegacyAdapterPolicy):
    policy_id = ORACLE_ID
    version = ORACLE_VERSION

    def __init__(self, env: CohortEnvironment, labels: dict[str, str], catalog: SourceCatalog | None = None, **kw):
        """env: 결과 미리 보기용 환경 (실행기와 같은 응답 모델). labels: 규칙 GT ({"<별칭>|<시험>": 라벨})."""
        super().__init__(catalog=catalog, **kw)
        self.env = env
        self.labels = dict(labels)
        self._fails: set[str] = set()

    def succeeds(self, action: Action) -> bool:
        return self.env.execute(action, 0, 0, "oracle-preview").execution == "answered"

    def propose(self, cohort: CohortState, pending_reevaluation: dict[str, list[str]] | None = None) -> list[Action]:
        out, self._fails = [], set()
        for a in super().propose(cohort, pending_reevaluation):
            if a.action_type in NON_EXECUTING or a.source_id is None or a.safety_required or self.succeeds(a):
                out.append(a)
                continue
            doc = self._document_instead(a, cohort.ops.get(a.patient_alias, OpsState()))
            if doc is None:
                self._fails.add(a.action_id)
                out.append(a)
            else:
                out.append(doc)
        return out

    def _document_instead(self, a: Action, ops: OpsState) -> Action | None:
        item = item_key(a.info_key, base_fingerprint(a.fingerprint))
        for sid in self.item_alternatives(a.source_id, a.required_fields, a.info_key, item, ops.failed_sources,
                                          ops.failed_items):
            src = self.catalog[sid]
            cost = src.request_cost.model_copy(update={"coordinator_minutes": src.request_cost.coordinator_minutes
                                                       + src.per_item_minutes * max(len(a.target_criteria), 1)})
            doc = Action.model_validate({**a.model_dump(), "action_type": "REQUEST_DOCUMENT", "source_id": sid,
                                         "reason_code": DOCUMENT_FIRST, "purpose": "eligibility",
                                         "question_contract_id": None, "expected_cost": cost})
            if self.succeeds(doc):
                return doc
        return None

    def order_key(self, a: Action) -> tuple:
        aims = any(self.labels.get(f"{a.patient_alias}|{t}") in (ELIGIBLE, PENDING_EXPERT) for t in a.target_trial_ids)
        return (0 if a.safety_required else 1, 0 if aims else 1, a.expected_cost.coordinator_minutes, a.priority,
                a.patient_alias, a.sequence, a.fingerprint)

    def select(self, cands: list[Action], budget: EffortBudget, ledger: EffortLedger | None = None,
               cohort: CohortState | None = None) -> Plan:
        drop = [a for a in cands if a.action_id in self._fails]
        plan = super().select([a for a in cands if a.action_id not in self._fails], budget, ledger, cohort)
        extra = tuple(SkippedAction(action_id=x, reason_code=WILL_FAIL)
                      for x in dict.fromkeys(a.action_id for a in drop))
        return plan.model_copy(update={"skipped": plan.skipped + extra}) if extra else plan


__all__ = ["OracleUpperPolicy", "ORACLE_ID", "ORACLE_VERSION", "DOCUMENT_FIRST", "WILL_FAIL"]
