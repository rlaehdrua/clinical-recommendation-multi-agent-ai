"""코호트 상태 (WP5 / M1·M5): 여러 환자–시험 쌍의 읽기 전용 상태. 판정을 만들거나 바꾸지 않습니다.

- 환자별 기존 DecisionState(decision_state.build_state 결과)를 감싸고, 쌍 상태(PairStatus)와 기준 결과
  (contracts.CriterionResult)를 **파생**합니다. 원래 판정 값(eligibility, 기준 상태)은 그대로 복사만 합니다.
- 정책이 기존 decision_policy.candidates를 부를 때는 legacy_copy()로 깊은 사본을 받습니다 (원본 불변).
- 운영 상태(실패한 정보원, 시도 횟수, 확보 소진)는 판정과 분리된 OpsState에만 있습니다.
- 정답·숨긴 사실·원래 환자 ID는 담지 않습니다. 환자는 별칭으로만 가리킵니다 (사본의 patient_id도 별칭으로 바꿈).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from ..decision_state import CriterionState as LegacyCriterion
from ..decision_state import DecisionState
from .contracts import ALIAS_PATTERN, CriterionResult, PairKey, PairStatus, derive_pair_status

EVALUATOR_ADAPTER = "legacy_decision_state"


@dataclass(frozen=True)
class PairState:
    pair: PairKey
    status: PairStatus
    eligibility: str  # 기존 Matcher 판정 그대로
    open: bool  # 모집 중
    criteria: tuple[CriterionResult, ...] = ()
    gaps: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OpsState:
    """판정과 무관한 운영 상태 (정책이 관리)."""
    failed_sources: frozenset[str] = frozenset()
    attempts: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))  # fingerprint -> 횟수
    exhausted_trials: frozenset[str] = frozenset()  # 확보 수단이 소진되어 보류한 시험


@dataclass(frozen=True)
class PatientSlice:
    alias: str
    pairs: tuple[PairState, ...]
    fact_version: str
    _legacy: DecisionState = field(repr=False, compare=False)

    def legacy_copy(self) -> DecisionState:
        return self._legacy.model_copy(deep=True)


def _adjudication(c: LegacyCriterion, state: str) -> tuple[str, str]:
    engine = c.record.engine if c.record else "unknown"
    if state == "UNKNOWN" and c.gap == "physician_review":
        return "expert", "none"
    if engine == "rule_engine":
        return "rule", "rule_engine" if state != "UNKNOWN" else "none"
    return "fact_llm", "llm" if state != "UNKNOWN" else "none"


def criterion_result(alias: str, c: LegacyCriterion) -> CriterionResult:
    state = "UNKNOWN" if c.status == "NEEDS_FULL_EVALUATION" else c.status
    adj, by = _adjudication(c, state)
    rec = c.record
    return CriterionResult(
        pair=PairKey(patient_alias=alias, trial_id=c.trial_id), rule_id=c.rule_id, state=state, adjudication=adj,
        decided_by=by, required_evidence=c.required_evidence if c.required_evidence in ("self_report_ok",
                                                                                         "clinical_record")
        else "clinical_record", evidence_satisfied=state != "UNKNOWN" and c.gap == "none",
        evidence_fact_ids=tuple(c.evidence_refs), gap=c.gap,
        inputs_used=tuple(rec.used_fields) if rec and rec.inputs_verified and rec.used_fields else None,
        evaluator_version=f"{EVALUATOR_ADAPTER}:{rec.engine if rec else 'unknown'}",
        criterion_version=rec.criterion_version if rec else None)


def _pair_states(alias: str, st: DecisionState, ops: OpsState) -> tuple[PairState, ...]:
    out = []
    for tid, t in sorted(st.trials.items()):
        crit = tuple(criterion_result(alias, c) for c in t.criteria)
        gaps = frozenset(c.gap for c in t.criteria if c.requirement == "required")
        status = derive_pair_status(t.eligibility, gaps, t.mismatch, exhausted=tid in ops.exhausted_trials)
        out.append(PairState(PairKey(patient_alias=alias, trial_id=tid), status, t.eligibility, t.open, crit, gaps))
    for tid in sorted(st.not_evaluated_trials):
        out.append(PairState(PairKey(patient_alias=alias, trial_id=tid), PairStatus.NOT_EVALUATED, "NOT_EVALUATED",
                             True))
    return tuple(out)


@dataclass(frozen=True)
class CohortState:
    patients: MappingProxyType  # alias -> PatientSlice
    ops: MappingProxyType  # alias -> OpsState

    @classmethod
    def from_decision_states(cls, states: dict[str, DecisionState],
                             ops: dict[str, OpsState] | None = None) -> "CohortState":
        """{별칭: DecisionState} -> CohortState. 별칭 형식이 아니면 거부 (원래 ID 차단)."""
        ops = dict(ops or {})
        patients = {}
        for alias, st in sorted(states.items()):
            if not re.match(ALIAS_PATTERN, alias):
                raise ValueError("코호트 상태의 환자 키는 불투명 별칭이어야 함")
            copy = st.model_copy(deep=True)
            copy.patient_id = copy.session_id = alias  # 사본 안에도 원래 ID를 남기지 않음
            o = ops.setdefault(alias, OpsState())
            patients[alias] = PatientSlice(alias, _pair_states(alias, copy, o), copy.fact_version, copy)
        return cls(MappingProxyType(patients), MappingProxyType(ops))

    def aliases(self) -> list[str]:
        return sorted(self.patients)

    def pairs(self) -> list[PairState]:
        return [p for a in self.aliases() for p in self.patients[a].pairs]

    def pair(self, alias: str, trial_id: str) -> PairState | None:
        return next((p for p in self.patients[alias].pairs if p.pair.trial_id == trial_id), None)

    def open_pairs(self, alias: str) -> list[PairState]:
        """확정·평가 실패가 아니고 모집 중인 쌍 (아직 작업이 남은 쌍)."""
        done = {PairStatus.CONFIRMED_ELIGIBLE, PairStatus.CONFIRMED_INELIGIBLE, PairStatus.BLOCKED_FAILURE}
        return [p for p in self.patients[alias].pairs if p.open and p.status not in done]

    def with_ops(self, alias: str, ops: OpsState) -> "CohortState":
        """운영 상태만 바꾼 새 코호트 (판정·사실은 그대로, 쌍 상태는 보류 표시만 다시 파생)."""
        new_ops = dict(self.ops)
        new_ops[alias] = ops
        sl = self.patients[alias]
        patients = dict(self.patients)
        patients[alias] = replace(sl, pairs=_pair_states(alias, sl._legacy, ops))
        return CohortState(MappingProxyType(patients), MappingProxyType(new_ops))
