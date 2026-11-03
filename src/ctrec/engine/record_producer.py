"""공통 DecisionRecord 생산기 (WP7 1단계, 계약 0.2 PROVISIONAL). 웹·오프라인 실행기가 함께 쓰는 순수 함수입니다.

실행기는 실제로 관찰한 것만 StepObservation에 담아 넘깁니다(행동, 실행 상태, 질문-행동 대응, 전후 CohortState,
전후 FactSnapshot, 실제 재평가 범위, 정책 전이, 실제 청구 비용). 이 모듈은 Session·평가기·답변 시뮬레이터에
의존하지 않고, 정답 객체나 원래 환자 ID를 받지 않습니다(입력 모델에 그런 필드가 없고 환자 키는 별칭 형식만).

산출: StepResult = (status, record | None, issues)
- "recorded"               : 계약 0.2로 정상 표현된 기록, 진단 없음
- "recorded_with_warnings" : 정상 기록 + 경고 진단(예: 출처를 확정할 수 없는 새 사실). 기록은 유효
- "contract_violation"     : 계약이 표현할 수 없는 사건(예: 확정 적격 <-> 확정 부적격 직접 역전). **정상 기록을 만들지
                             않고**, 원래 전후 상태를 차단 진단(severity="blocking")에 보존. 실행 전체의 감사 상태는
                             RunAuditStatus로 '불완전'이 됨
관찰 입력 자체가 모순이면(질문-행동 불일치, 스냅샷에 없는 사실 ID 등) ObservationError를 냅니다(아무것도 기록 안 함).

사실 귀속 원칙
- 새 사실 = after 스냅샷에 있고 before에 없는 fact_id (실제 FactStore 스냅샷 차이만).
- 행동의 결과로 기록하는 사실은 **명시적 출처**가 있는 것뿐: (1) 실행기가 넘긴 explicit_fact_links, 또는 (2) 사실의
  source_reference.kind == "answer" 이고 ref가 질문의 source_ref와 정확히 같은 것. 인용문 일치만으로는 귀속하지 않음.
- 귀속하지 못한 새 사실은 지우거나 숨기지 않고 unattributed_fact 진단으로 남김. 무응답 표지("모름" 등)를 인용한 사실은
  decline_marker_fact 진단(실제 스냅샷에 있음을 보존하되 답변의 결과로 세지 않음). fact_version_after는 실제 값 그대로.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Mapping

from pydantic import Field, model_validator

from ..decision_state import DECLINED_ANSWERS
from ..fact_changes import FactSnapshot
from .cohort_state import CohortState
from .contracts import (ALIAS_PATTERN, CODE_PATTERN, CONFIRMED, CRITERION_KEY_PATTERN, ID_PATTERN, ZERO_COST, Action,
                        ActionOutcome, CriterionChange, CriterionResult, DecisionRecord, EffortCost, PairKey,
                        PairStatus, PairTransition, TransitionError, _Frozen, check_transition)

PRODUCER_VERSION = "record_producer/0.1"
ISSUES_FILE = "decision_record_issues.jsonl"
FACT_ID_RE = re.compile(r"^F-[0-9a-f]{12}$")  # fact_store.FactStore.add가 만드는 형식
DETAIL_RE = re.compile(r"^[a-z0-9_]{1,40}(:[a-z0-9_]{1,40})?$")  # 보조 코드 (값·원문 아님)
NO_ANSWER_ERROR = "no_answer"
REUSE_REASON = "not_reevaluated"
REEVALUATION_REASON = "reevaluated_by_executor"

Execution = Literal["answered", "declined", "failed", "not_executed", "proposed"]
Severity = Literal["blocking", "warning"]
StepStatus = Literal["recorded", "recorded_with_warnings", "contract_violation"]
# 주의: 단계 상태 contract_violation(감사 기록 보류)은 쌍 상태 PairStatus.BLOCKED_FAILURE(확보 수단 소진에 따른 운영상
# 보류)와 다른 개념. BLOCKED_FAILURE 전이는 계약상 정상 전이이며 기록의 PairTransition으로 남음.


def is_declined(text: str) -> bool:
    """무응답·거부 표지 (decision_state._declined와 같은 규칙: 앞뒤 공백 제거 + casefold 후 DECLINED_ANSWERS)."""
    return text.strip().casefold() in DECLINED_ANSWERS


class ObservationError(ValueError):
    """실행기가 넘긴 관찰이 서로 모순됨 (기록하지 않음). 메시지에는 코드·ID만."""


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------

class AskedQuestion(_Frozen):
    """실행기가 행동을 질문으로 보낸 기록 (질문 렌더 시점에 실행기가 가진 값)."""
    question_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    action_id: str = Field(pattern=ID_PATTERN)
    target_criteria: tuple[str, ...]
    purpose: Literal["eligibility", "physician_reference"]
    source_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")  # 답에서 나온 사실의 출처 ref


@dataclass(frozen=True)
class StepObservation:
    run_id: str
    step: int
    action: Action
    execution: Execution
    before: CohortState
    after: CohortState
    facts_before: FactSnapshot
    facts_after: FactSnapshot
    asked: AskedQuestion | None = None  # 질문 행동을 실제로 보냈을 때
    error_code: str | None = None  # execution="failed"일 때 실행기가 관찰한 오류 코드
    reevaluated_trials: frozenset[str] = frozenset()  # 실제로 다시 판정한 시험 (그 시험의 기준 전체)
    reevaluated_criteria: frozenset[str] = frozenset()  # 기준 단위로 다시 판정한 키 "<trial>:<rule>" (WP4 이후)
    explicit_fact_links: tuple[str, ...] = ()  # 실행기가 이 행동의 결과로 직접 연결한 fact_id
    policy_transitions: tuple[PairTransition, ...] = ()  # policy.apply_outcome 결과 (정책 권한 전이)
    pair_reasons: Mapping[str, str] | None = None  # 실행기가 아는 상태 변경 사유 {trial_id: code}
    cost: EffortCost = ZERO_COST  # 실제로 청구한 비용
    charge_keys: tuple[str, ...] = ()
    latency_minutes: float | None = None  # 관찰한 경우만


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

class RecordIssue(_Frozen):
    """기록과 함께 보는 구조화 진단 (ID·코드·상태만)."""
    issue_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,200}$")
    producer_version: str = PRODUCER_VERSION
    run_id: str = Field(pattern=ID_PATTERN)
    step: int = Field(ge=0)
    action_id: str = Field(pattern=ID_PATTERN)
    code: str = Field(pattern=CODE_PATTERN)
    severity: Severity
    patient_alias: str = Field(pattern=ALIAS_PATTERN)
    trial_id: str | None = Field(default=None, pattern=ID_PATTERN)
    criterion_key: str | None = Field(default=None, pattern=CRITERION_KEY_PATTERN)
    before: str | None = Field(default=None, pattern=r"^[A-Z_]{1,40}$")
    after: str | None = Field(default=None, pattern=r"^[A-Z_]{1,40}$")
    fact_ids: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()  # 보조 코드 (예: source_kind:answer)

    @model_validator(mode="after")
    def _ids(self):
        bad = [f for f in self.fact_ids if not FACT_ID_RE.match(f)]
        if bad:
            raise ValueError("진단의 fact_id는 FactStore 형식이어야 함")
        if any(not DETAIL_RE.match(d) for d in self.detail):
            raise ValueError("detail은 코드만")
        return self


@dataclass(frozen=True)
class StepResult:
    status: StepStatus
    record: DecisionRecord | None
    issues: tuple[RecordIssue, ...]

    @property
    def blocking(self) -> tuple[RecordIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "blocking")


@dataclass(frozen=True)
class RunAuditStatus:
    """실행 전체 감사 상태. 차단 진단이 하나라도 있으면 complete=False (정상 감사 완료로 셀 수 없음)."""
    records: int
    warnings: int
    blocking: int
    violation_steps: tuple[int, ...]  # 계약 위반으로 정상 기록을 보류한 단계

    @property
    def complete(self) -> bool:
        return self.blocking == 0

    @property
    def status(self) -> str:
        return "complete" if self.complete else "incomplete_contract_violation"


def run_status(results: Iterable[StepResult]) -> RunAuditStatus:
    rs = list(results)
    return RunAuditStatus(records=sum(r.record is not None for r in rs),
                          warnings=sum(i.severity == "warning" for r in rs for i in r.issues),
                          blocking=sum(i.severity == "blocking" for r in rs for i in r.issues),
                          violation_steps=tuple(sorted({r.issues[0].step for r in rs if r.status == "contract_violation"})))


# ---------------------------------------------------------------------------
# 관찰 검증
# ---------------------------------------------------------------------------

def _check_observation(obs: StepObservation) -> str:
    a = obs.action
    alias = a.patient_alias
    if not re.match(ID_PATTERN, obs.run_id) or obs.step < 0:
        raise ObservationError("bad_run_id_or_step")
    for name, co in (("before", obs.before), ("after", obs.after)):
        if alias not in co.patients:
            raise ObservationError(f"cohort_{name}_missing_patient")
    for name, snap in (("facts_before", obs.facts_before), ("facts_after", obs.facts_after)):
        if snap.patient_id != alias:
            raise ObservationError(f"{name}_patient_mismatch")
    if obs.execution in ("answered", "declined"):
        if obs.asked is None:
            raise ObservationError("asked_question_required")
    if obs.asked is not None:
        q = obs.asked
        if q.action_id != a.action_id:
            raise ObservationError("question_action_id_mismatch")
        if tuple(q.target_criteria) != tuple(a.target_criteria):
            raise ObservationError("question_targets_mismatch")
        if q.purpose != a.purpose:
            raise ObservationError("question_purpose_mismatch")
        if obs.execution in ("not_executed", "proposed"):
            raise ObservationError("asked_but_not_executed")
    if obs.execution == "failed" and not obs.error_code:
        raise ObservationError("failed_requires_error_code")
    if obs.execution in ("not_executed", "proposed") and (obs.cost != ZERO_COST or obs.charge_keys):
        raise ObservationError("unexecuted_action_charged")
    if obs.execution in ("declined", "failed", "not_executed", "proposed") and obs.explicit_fact_links:
        raise ObservationError("facts_linked_to_unanswered_action")
    for p in obs.policy_transitions:
        if p.authority != "policy" or p.pair.patient_alias != alias:
            raise ObservationError("bad_policy_transition")
    return alias


# ---------------------------------------------------------------------------
# 사실 귀속
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FactAttribution:
    attributed: tuple[str, ...]  # 이 행동의 결과로 기록할 fact_id (명시적 출처만)
    decline_markers: tuple[str, ...]  # 무응답 표지를 인용한 새 사실
    unattributed: tuple[tuple[str, str], ...]  # (fact_id, source_kind)
    removed: tuple[str, ...]  # before에 있었는데 after에 없는 fact_id (사실은 추가만 되어야 함)


def attribute_facts(obs: StepObservation) -> FactAttribution:
    before = {f.fact_id for f in obs.facts_before.facts}
    after = {f.fact_id: f for f in obs.facts_after.facts}
    if any(not FACT_ID_RE.match(i) for i in list(before) + list(after)):
        raise ObservationError("snapshot_fact_id_format")
    new = sorted(i for i in after if i not in before)
    removed = tuple(sorted(i for i in before if i not in after))
    unknown = sorted(set(obs.explicit_fact_links) - set(new))
    if unknown:  # 실제 스냅샷 차이에 없는 ID (정답 데이터 ID, 이전 사실, 오타 등)는 받지 않음
        raise ObservationError("linked_fact_not_in_snapshot_diff")
    ref = obs.asked.source_ref if obs.asked is not None else None
    attributed, markers, unattributed = [], [], []
    for fid in new:
        f = after[fid]
        src = f.source_reference
        quote = (src.quote or "").strip()
        if obs.execution == "answered" and (fid in obs.explicit_fact_links
                                            or (ref is not None and src.kind == "answer" and src.ref == ref)):
            attributed.append(fid)
        elif src.kind == "answer" and is_declined(quote):
            markers.append(fid)
        else:
            unattributed.append((fid, src.kind))
    return FactAttribution(tuple(attributed), tuple(markers), tuple(unattributed), removed)


# ---------------------------------------------------------------------------
# 기준·쌍 상태
# ---------------------------------------------------------------------------

def _criteria(co: CohortState, alias: str) -> dict[str, CriterionResult]:
    return {f"{c.pair.trial_id}:{c.rule_id}": c for p in co.patients[alias].pairs for c in p.criteria}


def _pairs(co: CohortState, alias: str) -> dict[str, object]:
    return {p.pair.trial_id: p for p in co.patients[alias].pairs}


def _issue(obs: StepObservation, code: str, severity: Severity, subject: str = "", **kw) -> RecordIssue:
    return RecordIssue(issue_id=f"{obs.run_id}:{obs.step}:{code}" + (f":{subject}" if subject else ""),
                       run_id=obs.run_id, step=obs.step, action_id=obs.action.action_id, code=code,
                       severity=severity, patient_alias=obs.action.patient_alias, **kw)


def _outcome(obs: StepObservation, attr: FactAttribution) -> ActionOutcome | None:
    if obs.execution == "proposed":
        return None
    status = {"answered": "succeeded", "declined": "no_response", "failed": "failed",
              "not_executed": "not_executed"}[obs.execution]
    err = NO_ANSWER_ERROR if obs.execution == "declined" else obs.error_code if obs.execution == "failed" else None
    extra = {} if obs.latency_minutes is None else {"latency_minutes": obs.latency_minutes}
    return ActionOutcome(action_id=obs.action.action_id, status=status,
                         new_fact_ids=attr.attributed if status == "succeeded" else (), cost_charged=obs.cost,
                         charge_keys=tuple(obs.charge_keys), error_code=err, **extra)


# ---------------------------------------------------------------------------
# 생산
# ---------------------------------------------------------------------------

def build_step(obs: StepObservation) -> StepResult:
    alias = _check_observation(obs)
    attr = attribute_facts(obs)
    outcome = _outcome(obs, attr)
    issues: list[RecordIssue] = []
    for fid in attr.decline_markers:
        issues.append(_issue(obs, "decline_marker_fact", "warning", fid, fact_ids=(fid,), detail=("source_kind:answer",)))
    for fid, kind in attr.unattributed:
        issues.append(_issue(obs, "unattributed_fact", "warning", fid, fact_ids=(fid,), detail=(f"source_kind:{kind}",)))
    if attr.removed:
        issues.append(_issue(obs, "fact_missing_after", "blocking", fact_ids=attr.removed))

    executed = outcome is not None and outcome.status != "not_executed"
    cb, ca = _criteria(obs.before, alias), _criteria(obs.after, alias)
    pb, pa = _pairs(obs.before, alias), _pairs(obs.after, alias)
    re_keys = set(obs.reevaluated_criteria) | {k for k in ca if k.split(":", 1)[0] in obs.reevaluated_trials}
    unknown = sorted(set(obs.reevaluated_criteria) - set(ca)) + sorted(set(obs.reevaluated_trials) - set(pa))
    if unknown:
        raise ObservationError("reevaluated_scope_not_in_after_state")

    changes: list[CriterionChange] = []
    for key in sorted(set(cb) | set(ca)):
        before = cb[key].state if key in cb else "NEEDS_FULL_EVALUATION"
        after_c = ca.get(key)
        after = after_c.state if after_c else before
        is_re = key in re_keys
        if not is_re and before != after:  # 재평가하지 않았다는데 판정이 바뀜 -> 계약상 표현 불가
            issues.append(_issue(obs, "criterion_changed_without_reevaluation", "blocking", key, criterion_key=key,
                                 trial_id=key.split(":", 1)[0], before=before, after=after))
            continue
        if not executed and (is_re or before != after):
            issues.append(_issue(obs, "state_change_without_execution", "blocking", key, criterion_key=key,
                                 before=before, after=after))
            continue
        if not executed:
            continue  # 실행되지 않은 행동 기록에는 기준 변화를 싣지 않음 (실제 실행과 구분)
        changes.append(CriterionChange(
            criterion_key=key, before=before, after=after, reevaluated=is_re,
            reason_code=REEVALUATION_REASON if is_re else REUSE_REASON,
            evaluator_version=after_c.evaluator_version if (is_re and after_c) else None,
            evidence_fact_ids=tuple(after_c.evidence_fact_ids) if (is_re and after_c) else ()))

    policy_by_trial = {p.pair.trial_id: p for p in obs.policy_transitions}
    transitions: list[PairTransition] = []
    relevant = sorted(set(obs.action.target_trial_ids) | obs.reevaluated_trials | set(policy_by_trial)
                      | {t for t in set(pb) & set(pa) if pb[t].status != pa[t].status})
    reasons = dict(obs.pair_reasons or {})
    for tid in relevant:
        if tid not in pb or tid not in pa:
            continue
        b, a2 = pb[tid].status, pa[tid].status
        if tid in policy_by_trial:
            p = policy_by_trial[tid]
            if (p.before, p.after) != (b, a2):
                issues.append(_issue(obs, "policy_transition_disagrees_with_state", "blocking", tid, trial_id=tid,
                                     before=b.value, after=a2.value))
            else:
                transitions.append(p)
            continue
        if b != a2 and not executed:
            issues.append(_issue(obs, "state_change_without_execution", "blocking", tid, trial_id=tid,
                                 before=b.value, after=a2.value))
            continue
        if not executed:
            continue
        try:
            check_transition(b, a2, authority="adjudication")
        except TransitionError:
            code = "direct_confirmed_reversal" if {b, a2} == set(CONFIRMED) else "transition_rejected"
            issues.append(_issue(obs, code, "blocking", tid, trial_id=tid, before=b.value, after=a2.value,
                                 fact_ids=attr.attributed))
            continue
        reason = reasons.get(tid)
        if reason is None and a2 == PairStatus.NOT_EVALUATED and b != a2:
            reason = "evaluation_failed"  # 관찰된 상태 자체가 평가 실패
        if b in CONFIRMED and a2 != b and reason is None:
            issues.append(_issue(obs, "unexplained_confirmed_downgrade", "warning", tid, trial_id=tid,
                                 before=b.value, after=a2.value))
        transitions.append(PairTransition(pair=PairKey(patient_alias=alias, trial_id=tid), before=b, after=a2,
                                          authority="adjudication", reason_code=reason,
                                          pending_kinds=tuple(sorted(pa[tid].pending_kinds))))

    if any(i.severity == "blocking" for i in issues):
        # 정상 기록을 보류해도 이 단계에서 실제로 관찰된 다른 변화(정상 전이·기준 변화·귀속 사실·결과 상태)는 진단으로 남김
        # (보류 때문에 감사에서 조용히 빠지지 않게). 모두 위반 단계의 일부이므로 severity="blocking".
        for p in transitions:
            if p.before != p.after:
                issues.append(_issue(obs, "withheld_pair_transition", "blocking", p.pair.trial_id,
                                     trial_id=p.pair.trial_id, before=p.before.value, after=p.after.value,
                                     detail=(f"authority:{p.authority}",) + ((f"reason:{p.reason_code}",)
                                                                             if p.reason_code else ())))
        for c in changes:
            if c.before != c.after:
                issues.append(_issue(obs, "withheld_criterion_change", "blocking", c.criterion_key,
                                     criterion_key=c.criterion_key, before=c.before, after=c.after))
        if outcome is not None:
            issues.append(_issue(obs, "withheld_outcome", "blocking", fact_ids=tuple(outcome.new_fact_ids),
                                 detail=(f"outcome:{outcome.status}",)))
        return StepResult("contract_violation", None, tuple(issues))
    re_versions = {c.evaluator_version for c in changes if c.reevaluated and c.evaluator_version}
    record = DecisionRecord(
        run_id=obs.run_id, step=obs.step, action=obs.action, outcome=outcome,
        facts_added=outcome.new_fact_ids if outcome else (), criteria=tuple(changes), pairs=tuple(transitions),
        policy_version=obs.action.policy_version,
        evaluator_version=next(iter(re_versions)) if len(re_versions) == 1 else None,
        fact_version_before=obs.facts_before.version, fact_version_after=obs.facts_after.version)
    return StepResult("recorded_with_warnings" if issues else "recorded", record, tuple(issues))


# ---------------------------------------------------------------------------
# 진단 파일 (decision_records.jsonl 옆). 기존 audit.RecordWriter는 기록만 씀
# ---------------------------------------------------------------------------

class IssueWriter:
    """decision_record_issues.jsonl에 한 줄씩. 같은 issue_id 중복과 금지 문자열을 거부."""

    def __init__(self, path: Path, run_id: str, forbidden_texts: Iterable[str] = (), scanner=None):
        self.path, self.run_id, self.scanner = Path(path), run_id, scanner
        self.forbidden = [t for t in forbidden_texts if t and len(t) >= 2]
        self._ids: set[str] = set()
        if self.path.is_symlink():
            raise ObservationError("issue_file_is_link")
        if self.path.exists():
            for issue in load_issues(self.path):
                if issue.run_id != run_id:
                    raise ObservationError("issue_file_other_run")
                self._ids.add(issue.issue_id)

    def write(self, issue: RecordIssue) -> None:
        if issue.run_id != self.run_id:
            raise ObservationError("issue_run_id_mismatch")
        if issue.issue_id in self._ids:
            raise ObservationError("duplicate_issue")
        data = issue.model_dump_json()
        if any(t in data for t in self.forbidden) or (self.scanner is not None and self.scanner.scan(data)):
            raise ObservationError("sensitive_text_in_issue")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(data + "\n")
        self._ids.add(issue.issue_id)


def load_issues(path: Path) -> list[RecordIssue]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(RecordIssue.model_validate(json.loads(line)))
    return out


def write_step(result: StepResult, records, issues: IssueWriter) -> None:
    """기록(audit.RecordWriter)과 진단(IssueWriter)을 함께 씀. 차단된 단계는 기록 없이 진단만 남음."""
    if result.record is not None:
        records.write(result.record)
    for i in result.issues:
        issues.write(i)


__all__ = ["AskedQuestion", "StepObservation", "RecordIssue", "StepResult", "RunAuditStatus", "FactAttribution",
           "ObservationError", "IssueWriter", "attribute_facts", "build_step", "load_issues", "run_status",
           "write_step", "is_declined", "ISSUES_FILE", "PRODUCER_VERSION"]
