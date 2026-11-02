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
from .contracts import ALIAS_PATTERN, CriterionResult, PairKey, PairStatus, derive_pair_status, pending_kinds

EVALUATOR_ADAPTER = "legacy_decision_state"


@dataclass(frozen=True)
class PairState:
    pair: PairKey
    status: PairStatus
    eligibility: str  # 기존 Matcher 판정 그대로
    open: bool  # 모집 중
    criteria: tuple[CriterionResult, ...] = ()
    gaps: frozenset[str] = frozenset()
    pending_kinds: frozenset[str] = frozenset()  # 0.2: 남은 작업 전체 (status는 우선순위 하나만)
    mismatch: str | None = None  # 기존 decision_state 불충족 확정 수준 (none/provisional/confirmed)


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
    _overrides: MappingProxyType = field(default_factory=lambda: MappingProxyType({}), repr=False, compare=False)

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
        else "clinical_record", evidence_satisfied=state != "UNKNOWN" and c.gap == "none" and not c.conflict_on_decided,
        evidence_fact_ids=tuple(c.evidence_refs), gap=c.gap,
        inputs_used=tuple(rec.used_fields) if rec and rec.inputs_verified and rec.used_fields else None,
        evaluator_version=f"{EVALUATOR_ADAPTER}:{rec.engine if rec else 'unknown'}",
        criterion_version=rec.criterion_version if rec else None)


class OverrideError(ValueError):
    pass


def _override(base: CriterionResult, overrides: dict[str, CriterionResult]) -> CriterionResult:
    """판정 출처를 더 정확한 기록으로 바꿈 (예: engine.contract_bridge의 contract_evaluator 결과).

    판정 값(state)·쌍은 바꿀 수 없음 — 다르면 OverrideError. 이 함수는 판정을 만들지 않고 출처·근거 정보만 보충합니다.
    """
    o = overrides.get(base.criterion_key)
    if o is None:
        return base
    if o.pair != base.pair or o.state != base.state:
        raise OverrideError(f"{base.criterion_key}: 출처 보충이 판정 값을 바꿈 ({base.state} -> {o.state})")
    return o


def required_gaps(t) -> frozenset[str]:
    """판정에 적용된 기준(required)의 gap. 다른 코호트 기준(alternative_cohort, OR의 다른 갈래)과 절차 기준(waivable)은
    제외하므로 닫힌 갈래의 gap이 열린 경로를 미해결로 만들지 않음. 확정 판정(MET/NOT_MET)의 근거가 상충하면
    (conflict_on_decided) 그 기준은 gap이 none이어도 확인이 필요하므로 conflict로 셈 (0.2)."""
    out = set()
    for c in t.criteria:
        if c.requirement != "required":
            continue
        if c.gap != "none":
            out.add(c.gap)
        if c.conflict_on_decided:
            out.add("conflict")
    return frozenset(out)


def _pair_states(alias: str, st: DecisionState, ops: OpsState,
                 overrides: dict[str, CriterionResult] | None = None) -> tuple[PairState, ...]:
    out = []
    overrides = overrides or {}
    for tid, t in sorted(st.trials.items()):
        crit = tuple(_override(criterion_result(alias, c), overrides) for c in t.criteria)
        gaps = required_gaps(t)
        ex = tid in ops.exhausted_trials
        status = derive_pair_status(t.eligibility, gaps, t.mismatch, exhausted=ex)
        out.append(PairState(PairKey(patient_alias=alias, trial_id=tid), status, t.eligibility, t.open, crit, gaps,
                             pending_kinds(t.eligibility, gaps, t.mismatch, exhausted=ex), t.mismatch))
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
                             ops: dict[str, OpsState] | None = None,
                             criterion_overrides: dict[str, dict[str, CriterionResult]] | None = None
                             ) -> "CohortState":
        """{별칭: DecisionState} -> CohortState. 별칭 형식이 아니면 거부 (원래 ID 차단).

        criterion_overrides: {별칭: {"<trial>:<rule>": CriterionResult}} — 판정 출처 보충만 (판정 값이 다르면 거부).
        """
        ops = dict(ops or {})
        patients = {}
        for alias, st in sorted(states.items()):
            if not re.match(ALIAS_PATTERN, alias):
                raise ValueError("코호트 상태의 환자 키는 불투명 별칭이어야 함")
            copy = st.model_copy(deep=True)
            copy.patient_id = copy.session_id = alias  # 사본 안에도 원래 ID를 남기지 않음
            o = ops.setdefault(alias, OpsState())
            ov = MappingProxyType(dict((criterion_overrides or {}).get(alias, {})))
            patients[alias] = PatientSlice(alias, _pair_states(alias, copy, o, ov), copy.fact_version, copy, ov)
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
        patients[alias] = replace(sl, pairs=_pair_states(alias, sl._legacy, ops, sl._overrides))
        return CohortState(MappingProxyType(patients), MappingProxyType(new_ops))
