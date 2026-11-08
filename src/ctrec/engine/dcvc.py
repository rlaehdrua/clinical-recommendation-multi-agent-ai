"""제안 정책 full_dcvc (M1 DCV -> C). **정의 합의 전 초안** - 팀 결정(#13 안건 ①) 뒤 기본값·식이 바뀔 수 있습니다.

DCV (후보 고르기): LegacyAdapterPolicy.propose 그대로 + 결정 영향 필터 + 근거 경로.
  결정 영향: 행동이 겨냥하는 시험 중 열린 쌍(확정·확보 실패 보류·평가 실패가 아니고 모집 중)이 하나도 없으면 후보에서 뺌.
  안전 필수 행동·임상의 검토·보류 기록은 빼지 않음 (M1: DCV가 필수 검증·전문가 이관을 제거하면 안 됨).
  근거 경로 (0.2, P): 환자 질문의 대상 기준이 모두 진료 기록 근거가 필요한 기준(required_evidence = clinical_record)이면
  환자 답(자기 보고)으로는 그 기준을 확정할 수 없으므로, 같은 항목을 담은 문서 정보원이 남아 있을 때 그 질문을 문서 요청
  (reason record_evidence_required)으로 바꿈. 실패한 정보원·이미 요청한 (문서, 항목)은 쓰지 않음(대체 규칙과 같음).
  그 항목의 문서 경로가 있었는데 모두 실패했으면 환자 질문으로 되돌리지 않고 DEFER(item_sources_exhausted), 실패 없이
  요청한 문서만 다 썼으면 DEFER(requested_documents_unresolved) - _convert의 소진 보류와 같은 사유 (환자 자기 보고로는
  그 기준을 확정할 수 없고, 실패한 문서 요청과 같은 행동 ID라 실행할 수도 없음). 카탈로그에 그 항목의 문서 경로가 처음부터
  없으면 환자 질문 그대로 둠 (0.1 동작). 같은 규칙을 소진 판단(_cut_off_items)에도 씀 (reroute). 기준의 근거 요건(판정
  메타데이터)만 보며 확률을 쓰지 않음.
C (코호트 단위 선택): 안전 필수 -> 환자별 최소 배정까지는 기준선과 같고, 남은 예산은 매번
  점수 = sum(1 / 그 쌍의 남은 필수 기준 수, 행동이 움직일 수 있는 열린 쌍마다) / 지금 원장 기준 추가 비용(분)
  이 가장 큰 행동을 고름. 결정 단락(0.2, P): 불충족(NOT_MET)으로 판정된 필수 기준의 근거를 확인하는 행동은 그것만으로
  그 쌍을 확정 부적격으로 닫을 수 있으므로 그 쌍의 남은 수를 1로 셈 (다른 기준의 미확인과 무관 - AND 규칙). 확정에 가까운 쌍을 여러 시험에 걸쳐 움직이는 행동이 먼저이고, 같은 (환자, 정보원)을 이미
  요청했으면 추가 비용이 줄어 묶음이 유리해짐. 점수는 구조적 개수뿐이며 성공 확률을 쓰지 않음 (M9).

끄는 스위치 (ablation, M10)
- dcv_impact=False: 결정 영향 필터 끔
- bundles=False: 점수의 비용을 묶음 할인 없이 행동의 단독 비용(expected_cost)으로 계산 (청구는 원장 규칙 그대로)
- fairness_floor=False: 환자별 최소 배정 끔
- recovery=False: 실패 뒤 대체 정보원을 찾지 않고 바로 보류
- evidence_routing=False: 근거 경로 끔 (진료 기록 필요 기준도 환자에게 먼저 물음, 0.1 동작)
"""

from __future__ import annotations

from .cohort_state import CohortState, OpsState
from .contracts import NON_EXECUTING, REPHRASE, ZERO_COST, Action, EffortBudget, PairStatus, SkippedAction
from .cost import EffortLedger
from .policy import LegacyAdapterPolicy, item_key, tried_key

DCVC_VERSION = "0.2-draft"
EXHAUSTED = "item_sources_exhausted"  # policy.EXHAUSTED_DEFERS
UNRESOLVED = "requested_documents_unresolved"  # policy._convert와 같은 사유
RECORD_EVIDENCE = "record_evidence_required"
MIN_COST = 0.5  # 분. 추가 비용이 0인 행동(이미 요청한 문서의 같은 항목 등)이 점수를 무한대로 만들지 않게
_CLOSED = frozenset({PairStatus.CONFIRMED_ELIGIBLE, PairStatus.CONFIRMED_INELIGIBLE, PairStatus.BLOCKED_FAILURE,
                     PairStatus.NOT_EVALUATED})


class FullDCVCPolicy(LegacyAdapterPolicy):
    policy_id = "full_dcvc"
    version = DCVC_VERSION

    def __init__(self, *, dcv_impact: bool = True, bundles: bool = True, fairness_floor: bool = True,
                 recovery: bool = True, evidence_routing: bool = True, **kw):
        super().__init__(**kw)
        self.dcv_impact, self.bundles, self.fairness_floor, self.recovery = dcv_impact, bundles, fairness_floor, recovery
        self.evidence_routing = evidence_routing

    @property
    def switches(self) -> dict[str, bool]:
        return {"dcv_impact": self.dcv_impact, "bundles": self.bundles, "fairness_floor": self.fairness_floor,
                "recovery": self.recovery, "evidence_routing": self.evidence_routing}

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
        if self.evidence_routing:
            cands = [self.reroute(cohort, a) for a in cands]
        if not self.dcv_impact:
            return cands
        return [a for a in cands if a.safety_required or a.action_type in NON_EXECUTING
                or a.action_type == "CLINICIAN_REVIEW" or not a.target_trial_ids or self.open_targets(cohort, a)]

    @staticmethod
    def record_only(cohort: CohortState, a: Action) -> bool:
        """대상 기준이 모두 진료 기록 근거가 필요한 기준인지 (기준을 하나도 못 찾으면 False)."""
        st = cohort.patients[a.patient_alias]._legacy
        found = [c for k in a.target_criteria for t in [st.trials.get(k.split(":", 1)[0])] if t is not None
                 for c in t.criteria if c.key == k]
        return bool(found) and all(c.required_evidence == "clinical_record" for c in found)

    def reroute(self, cohort: CohortState, a: Action, ops: OpsState | None = None) -> Action:
        """후보 생성(propose)과 소진 판단(LegacyAdapterPolicy._cut_off_items)이 함께 쓰는 근거 경로."""
        return self.route_evidence(cohort, a, ops) if self.evidence_routing else a

    def route_evidence(self, cohort: CohortState, a: Action, ops: OpsState | None = None) -> Action:
        """진료 기록이 필요한 기준만 겨냥한 환자 질문 -> 남은 문서 정보원의 문서 요청.
        문서 경로가 모두 실패·사용됐으면 보류(DEFER), 문서 경로가 처음부터 없으면 그대로.
        ops: 아직 코호트에 반영하지 않은 운영 상태 (apply_outcome의 소진 판단). 없으면 cohort.ops."""
        src = self.catalog.get(a.source_id)
        if a.action_type != "ASK_PATIENT" or a.reason_code == REPHRASE or src is None or src.kind != "patient"                 or a.purpose != "eligibility" or not self.record_only(cohort, a):
            return a
        ops = ops if ops is not None else cohort.ops.get(a.patient_alias)
        item = item_key(a.info_key, a.fingerprint)
        failed_sources = ops.failed_sources if ops else frozenset()
        failed_items = ops.failed_items if ops else frozenset()
        tried = {(s, item) for s in self.catalog.sources if ops and ops.attempts.get(tried_key(s, item))}
        docs = self.item_alternatives(a.source_id, a.required_fields, a.info_key, item, failed_sources,
                                      failed_items | tried)
        if not docs:
            if not self.item_alternatives(a.source_id, a.required_fields, a.info_key, item, frozenset(), frozenset()):
                return a  # 이 항목을 담는 문서 정보원이 카탈로그에 없음 (소진이 아님)
            unfailed = self.item_alternatives(a.source_id, a.required_fields, a.info_key, item, failed_sources,
                                              failed_items)
            return a.model_copy(update={"action_type": "DEFER", "source_id": None, "expected_cost": ZERO_COST,
                                        "safety_required": False,
                                        "reason_code": UNRESOLVED if unfailed else EXHAUSTED})
        doc = self.catalog[docs[0]]
        n_items = max(len(a.target_criteria), 1)
        cost = ZERO_COST if doc is None else doc.request_cost.model_copy(update={
            "coordinator_minutes": doc.request_cost.coordinator_minutes + doc.per_item_minutes * n_items})
        return a.model_copy(update={"action_type": "REQUEST_DOCUMENT", "source_id": doc.source_id,
                                    "reason_code": RECORD_EVIDENCE, "expected_cost": cost})

    # ------------------------------------------------------------ C
    def floor_minutes(self, budget: EffortBudget) -> float:
        return budget.per_patient_floor_minutes if self.fairness_floor else 0.0

    @staticmethod
    def decisive_not_met(cohort: CohortState, a: Action, trial_id: str) -> bool:
        """행동이 그 시험에서 불충족 판정이 이미 난 필수 기준을 겨냥하는지 (그 근거 확인이 쌍을 닫음)."""
        t = cohort.patients[a.patient_alias]._legacy.trials.get(trial_id)
        if t is None or t.eligibility != "INELIGIBLE":
            return False
        keys = set(a.target_criteria)
        return any(c.key in keys and c.status == "NOT_MET" and c.requirement == "required" for c in t.criteria)

    def impact(self, cohort: CohortState, a: Action) -> float:
        """행동이 움직일 수 있는 열린 쌍마다 1/남은 필수 기준 수의 합 (구조적 점수). 결정 단락이면 그 쌍은 1."""
        total = 0.0
        for p in self.open_targets(cohort, a):
            n = 1 if self.decisive_not_met(cohort, a, p.pair.trial_id) else                 cohort.remaining_required(a.patient_alias, p.pair.trial_id)
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
