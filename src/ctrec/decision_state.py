"""Agentic Decision Loop의 상태와 행동 스키마 (Phase 3). 진단 전용이며 실제 판정·질문·추천을 바꾸지 않습니다.

DecisionState는 Session의 현재 상태를 읽기만 해서 만든 사본입니다.
- 환자 원문은 담지 않고 사실 저장소의 fact_id·항목 이름·스냅샷 버전으로 참조합니다.
- 기준별 상태는 기존 Matcher 판정(기준선)을 그대로 옮깁니다. 사실이 바뀐 뒤 그 판정을 다시 쓸 수 있는지는 Phase 2 규칙을
  따릅니다(규칙 엔진 기준만 좁은 의존성으로 재계산, LLM 기준은 입력 항목을 검증할 수 없어 NEEDS_FULL_EVALUATION).
- 정보 부족(gap)은 판정 상태와 별개로 표시합니다. UNKNOWN을 NOT_MET으로, NOT_EVALUATED를 MET/NOT_MET으로 바꾸지 않습니다.

용어
- requirement: required(판정에 적용되는 기준) / alternative_cohort(다른 코호트 전용, OR 구조의 다른 갈래)
  / waivable(등록 시 확인하는 절차 기준, 판정을 막지 않음)
- gap: none / missing(질문으로 확인 가능) / unverified(답은 있으나 진료 기록 필요) / physician_review(원문에 구체 기준 없음)
  / conflict(근거 항목의 사실 값이 맞지 않음) / stale(사실이 바뀌어 기준선 판정을 다시 쓸 수 없음)
- 불충족(mismatch) 확정 수준 (질문 가치 계산에만 씀, 기존 판정은 그대로):
  경로 = 코호트 하나(공통 기준 + 그 코호트 기준), 코호트가 없으면 시험 전체가 한 경로.
  경로의 필수 기준 중 NOT_MET이 있고 그 근거 사실이 상충하지 않으며 모두 문서 검증(document_verified)이면 confirmed,
  NOT_MET이 있으나 근거가 문서로 검증되지 않았으면(자기 보고·출처 미상·clinical_record 선언뿐) provisional,
  NOT_MET이 없거나 근거가 상충하면 열린 경로. 시험의 mismatch는 경로가 하나라도 열려 있으면 none, 모든 경로가
  confirmed면 confirmed, 그 밖에는 provisional. UNKNOWN·NOT_EVALUATED·LLM 미평가는 불충족으로 세지 않음.
- info key: 같은 정보를 묻는 기준을 시험 사이에서 묶는 키. 구조화 필드(ecog, lab:hemoglobin 등)나 바이오마커 표적
  (biomarker:EGFR)으로만 묶고, 그 밖의 기준은 묶지 않습니다(criterion:<시험>:<기준>, 문장 유사도로 묶지 않음).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from .acquisition import MissingFact
from .agents import matcher
from .agents.recommender import is_open
from .dependency_graph import LLM_CATEGORY_FIELDS
from .fact_changes import FactSnapshot, make_snapshot, snapshot_store
from .fact_compare import state as assessment_state
from .fact_store import FactStore
from .fact_view import FactEvidenceView, _differing, _engine_field, relevant_biomarker_facts, targets_in
from .schemas import CriterionAssessment, ParsedTrial, QAPair, TrialMatch

ActionType = Literal["ASK_USER", "REQUEST_DOCUMENT", "VERIFY", "RUN_TOOL", "REPARSE", "RETRIEVE", "DEFER",
                     "NEEDS_REVIEW", "FINALIZE"]
ActionStatus = Literal["proposed", "selected", "executed", "proposed_not_dispatched", "awaiting_external_input",
                       "needs_review", "deferred", "not_executed", "failed", "rejected", "finalized"]
CriterionStatus = Literal["MET", "NOT_MET", "UNKNOWN", "NEEDS_FULL_EVALUATION"]
Gap = Literal["none", "missing", "unverified", "physician_review", "conflict", "stale"]
Requirement = Literal["required", "alternative_cohort", "waivable"]
DECLINED_ANSWERS = {"", "모름", "모르겠음", "모르겠어요", "잘 모름", "unknown", "답변 거부", "거부"}


class Action(BaseModel):
    action_id: str
    action_type: ActionType
    reason_code: str
    human_readable_reason: str
    target_trial_ids: list[str] = []
    target_criterion_ids: list[str] = []  # "<trial_id>:<rule_id>"
    required_fact_fields: list[str] = []
    evidence_refs: list[str] = []  # fact_id
    priority: float  # 작을수록 먼저 (tier + 1/(1+가치 점수))
    tier: int
    value_score: float = 0.0  # 행동 순서에만 쓰는 설명 가능한 점수 (임상 적합성 판단에 쓰지 않음)
    score_terms: dict[str, float] = {}
    prerequisites: list[str] = []
    expected_information_gain: int = 0  # 결과가 바뀔 수 있는 시험 판정 수 (구조적 개수, 확률 아님)
    estimated_cost: float = 0.0  # 사용자 부담·계산 비용 단위
    info_key: str | None = None
    tool: str | None = None
    status: ActionStatus = "proposed"
    outcome: dict = {}
    fingerprint: str = ""
    created_at: float = Field(default_factory=time.time)


class EvaluationRecord(BaseModel):
    """기준 판정의 입력 계측 (향후 의존성 최적화용). inputs_verified가 아니면 의존성을 좁힐 수 없음."""
    engine: Literal["rule_engine", "llm", "review", "conflict", "unknown"]
    model: str | None = None
    used_fact_ids: list[str] = []
    used_fields: list[str] | None = None  # None = 실제로 쓴 입력 항목을 모름 (LLM은 프로필 전체를 받음)
    inputs_verified: bool = False  # 코드 경로로 입력 항목이 정해진 판정(규칙 엔진 등)만 True
    fact_version: str | None = None
    criterion_version: str
    input_fingerprint: str | None = None
    created_at: float | None = None


def narrowable(record: EvaluationRecord | None) -> bool:
    """이 판정의 의존성을 일부 항목으로 좁혀도 되는가. 계측 기록이 없거나 입력이 검증되지 않았으면 False."""
    return bool(record and record.inputs_verified and record.used_fields)


class CriterionState(BaseModel):
    trial_id: str
    rule_id: str
    kind: str
    category: str
    cohort: str | None
    requirement: Requirement
    status: CriterionStatus
    source: Literal["baseline_matcher", "fact_reevaluation"] = "baseline_matcher"
    gap: Gap = "none"
    conflict_on_decided: bool = False  # 확정 판정(MET/NOT_MET)의 근거 항목에 상충 사실이 있음 (안전 확인 필요)
    basis_tier: str | None = None  # 확정 판정 근거 사실 중 가장 낮은 근거 수준 (verified/declared_record/self_report/unknown)
    info_key: str
    fields: list[str] = []  # 구조화 필드(검증됨) 또는 유형별 후보(fields_are_hints=True)
    fields_are_hints: bool = False
    required_evidence: str = "self_report_ok"
    evidence_refs: list[str] = []
    asked: int = 0  # 이 기준을 겨냥한 확인 질문 수
    declined: bool = False  # 모름·거부 답변
    acquisition_status: str | None = None
    record: EvaluationRecord | None = None

    @property
    def key(self) -> str:
        return f"{self.trial_id}:{self.rule_id}"


class TrialState(BaseModel):
    trial_id: str
    eligibility: str  # 기존 Matcher 판정 그대로 (바꾸지 않음)
    match_status: str | None
    open: bool
    matched_cohort: str | None = None
    cohort_results: dict[str, str] = {}
    matched_version: str | None = None  # 이 판정을 만든 사실 스냅샷 버전 (알 수 있을 때)
    stale: bool = False  # 판정 이후 사실이 바뀜
    mismatch: Literal["none", "provisional", "confirmed"] = "none"  # 질문 가치 계산용 (위 설명)
    mismatch_basis: list[str] = []  # 불충족 근거 기준 "<시험>:<기준>"
    open_paths: list[str] = []  # 불충족이 없거나 근거가 상충해 아직 열린 경로 (코호트 이름, 코호트 없으면 "all")
    criteria: list[CriterionState] = []


class Budget(BaseModel):
    max_iterations: int = 30
    max_tool_calls: int = 10
    max_cost_units: float = 30.0
    max_questions: int = 5
    max_llm_calls: int = 0  # 진단 모드는 LLM을 부르지 않음
    max_seconds: float = 5.0


class BudgetUsage(BaseModel):
    iterations: int = 0
    tool_calls: int = 0
    cost_units: float = 0.0
    questions: int = 0
    llm_calls: int = 0
    seconds: float = 0.0


class DecisionState(BaseModel):
    session_id: str
    patient_id: str
    fact_version: str
    fact_ids: list[str] = []
    trials: dict[str, TrialState] = {}
    not_evaluated_trials: list[str] = []
    conflicting_fields: dict[str, str] = {}  # 항목 -> 상태 (값은 넣지 않음)
    unverified_fact_ids: list[str] = []  # 검증되지 않은 사실 (document_verified가 아님)
    question_history: list[dict] = []  # {round, question_id, targets, declined}
    action_history: list[Action] = []
    pending_actions: list[Action] = []
    stop_reason: str | None = None
    iteration: int = 0
    budget: Budget = Field(default_factory=Budget)
    usage: BudgetUsage = Field(default_factory=BudgetUsage)

    def criteria(self) -> list[CriterionState]:
        return [c for t in self.trials.values() for c in t.criteria]


# ---------------------------------------------------------------------------
# Session 읽기 전용 보기
# ---------------------------------------------------------------------------

@dataclass
class SessionView:
    """Decision Loop가 읽는 Session 상태의 사본 (Session 객체를 바꾸지 않음)."""
    session_id: str
    patient_id: str
    snapshot: FactSnapshot
    matches: dict[str, TrialMatch]
    parsed: dict[str, ParsedTrial]
    trials: dict[str, dict]
    qa_log: list[QAPair] = field(default_factory=list)
    missing_facts: dict[str, MissingFact] = field(default_factory=dict)
    reference_date: str = ""
    matched_versions: dict[str, str] = field(default_factory=dict)  # 시험 -> 판정 당시 사실 버전

    @classmethod
    def from_session(cls, s, matched_versions: dict[str, str] | None = None) -> "SessionView":
        from .fact_changes import take_snapshot
        return cls(session_id=s.session_id, patient_id=s.case.patient_id,
                   snapshot=take_snapshot(s.fact_store, s.case.patient_id),
                   matches={t: m.model_copy(deep=True) for t, m in s.matches.items()},
                   parsed=dict(s.parsed), trials=dict(s.trials),
                   qa_log=[q.model_copy(deep=True) for q in s.qa_log],
                   missing_facts={k: f.model_copy(deep=True) for k, f in s.missing_facts.items()},
                   reference_date=s.reference_date, matched_versions=dict(matched_versions or {}))

    def store(self) -> FactStore:
        return snapshot_store(self.snapshot)


def criterion_version(parsed: ParsedTrial, rule_id: str) -> str:
    rule = next(r for r in parsed.rules if r.rule_id == rule_id)
    meta = parsed.rule_meta.get(rule_id)
    data = {"rule": rule.model_dump(), "meta": meta.model_dump() if meta else None}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def info_key(rule, tid: str) -> tuple[str, list[str], bool]:
    """(키, 항목, 항목이 후보일 뿐인가). 구조화 필드·바이오마커 표적으로만 시험 사이를 묶음."""
    f = _engine_field(rule)
    if f:
        return f, [f], False
    targets = sorted(targets_in(rule.text))
    if targets:
        return "biomarker:" + "+".join(targets), ["biomarker"], False
    hints = sorted(LLM_CATEGORY_FIELDS.get(rule.category, set()))
    return f"criterion:{tid}:{rule.rule_id}", hints, True


def _declined(answer: str) -> bool:
    return answer.strip().casefold() in DECLINED_ANSWERS


def _record(a: CriterionAssessment, rule, parsed: ParsedTrial, refs: list[str], version: str | None,
            model: str | None) -> EvaluationRecord:
    f = _engine_field(rule)
    if a.method == "rule_engine":
        return EvaluationRecord(engine="rule_engine", used_fact_ids=refs, used_fields=[f] if f else None,
                                inputs_verified=bool(f), fact_version=version,
                                criterion_version=criterion_version(parsed, rule.rule_id))
    return EvaluationRecord(engine="llm", model=model, used_fact_ids=refs, used_fields=None, inputs_verified=False,
                            fact_version=version, criterion_version=criterion_version(parsed, rule.rule_id))


def _conflict(rule, view: FactEvidenceView) -> bool:
    f = _engine_field(rule)
    if f and f in view.excluded():
        return True
    if rule.category == "biomarker":
        return _differing(relevant_biomarker_facts(rule, view)[1])
    return False


def _refs(a: CriterionAssessment, rule, view: FactEvidenceView) -> list[str]:
    from .fact_view import supporting_facts
    facts, _, _ = supporting_facts(a, rule, view)
    return list(dict.fromkeys(r.fact_id for r in facts))


def build_state(view: SessionView, overlay: dict[str, CriterionAssessment] | None = None,
                stale: dict[str, set[str]] | None = None, history: list[Action] | None = None,
                budget: Budget | None = None, model: str | None = None) -> DecisionState:
    """Session 사본으로 Decision State를 만듦.

    overlay: "<시험>:<기준>" -> 사실 기반 재평가 판정 (RUN_TOOL deterministic_reevaluation 결과, 규칙 엔진 기준만)
    stale: 시험 -> 사실 변경 뒤 기준선 판정을 다시 쓸 수 없는 기준 (LLM 기준 등)
    """
    overlay, stale = overlay or {}, stale or {}
    store = view.store()
    fv = FactEvidenceView(store, view.patient_id)
    asked: dict[str, list[QAPair]] = {}
    for q in view.qa_log:
        for key in q.target:
            asked.setdefault(key, []).append(q)
    st = DecisionState(
        session_id=view.session_id, patient_id=view.patient_id, fact_version=view.snapshot.version,
        fact_ids=[f.fact_id for f in view.snapshot.facts], conflicting_fields=fv.excluded(),
        unverified_fact_ids=[f.fact_id for f in view.snapshot.facts if f.verification_status != "document_verified"],
        question_history=[{"round": q.round, "question_id": q.question_id, "targets": list(q.target),
                           "purpose": q.purpose, "declined": _declined(q.answer)} for q in view.qa_log],
        action_history=list(history or []), budget=budget or Budget())
    for tid, m in view.matches.items():
        trial = view.trials.get(tid, {})
        if m.eligibility == "NOT_EVALUATED" or tid not in view.parsed:
            st.not_evaluated_trials.append(tid)  # 판정 실패: MET/NOT_MET으로 바꾸지 않음
            continue
        parsed = view.parsed[tid]
        rules = {r.rule_id: r for r in parsed.rules}
        version = view.matched_versions.get(tid)
        ts = TrialState(trial_id=tid, eligibility=m.eligibility, match_status=m.match_status, open=is_open(trial),
                        matched_cohort=m.matched_cohort, cohort_results=dict(m.cohort_results), matched_version=version,
                        stale=bool(version and version != view.snapshot.version))
        applied = {a.rule_id for a in m.assessments}
        for a in m.assessments + m.other_cohort_assessments:
            rule = rules.get(a.rule_id)
            if rule is None:
                continue
            key = f"{tid}:{a.rule_id}"
            source = "baseline_matcher"
            if key in overlay:
                a, source = overlay[key], "fact_reevaluation"
            ik, fields, hints = info_key(rule, tid)
            status: CriterionStatus = assessment_state(a)  # type: ignore[assignment]
            gap: Gap = "none"
            if a.rule_id in stale.get(tid, set()) and key not in overlay:
                status, gap = "NEEDS_FULL_EVALUATION", "stale"  # 사실이 바뀐 뒤 기준선 LLM 판정은 다시 쓰지 않음
            conflict = _conflict(rule, fv)
            if gap == "none" and status == "UNKNOWN":
                if conflict:
                    gap = "conflict"
                elif a.needs_verification:
                    gap = "unverified"
                elif matcher.needs_physician_review(a):
                    gap = "physician_review"
                elif not matcher.is_waivable_logistics(a):
                    gap = "missing"
            qs = asked.get(key, [])
            mf = view.missing_facts.get(key)
            requirement: Requirement = ("alternative_cohort" if a.rule_id not in applied
                                        else "waivable" if matcher.is_waivable_logistics(a) else "required")
            refs = _refs(a, rule, fv) if status in ("MET", "NOT_MET") else []
            ts.criteria.append(CriterionState(
                trial_id=tid, rule_id=a.rule_id, kind=a.kind, category=a.category, cohort=a.cohort,
                requirement=requirement, status=status, source=source, gap=gap,
                conflict_on_decided=conflict and status in ("MET", "NOT_MET"),
                basis_tier=_basis_tier(refs, fv) if status in ("MET", "NOT_MET") else None, info_key=ik, fields=fields,
                fields_are_hints=hints, required_evidence=a.required_evidence, evidence_refs=refs, asked=len(qs),
                declined=bool(qs) and all(_declined(q.answer) for q in qs),
                acquisition_status=mf.status if mf else None,
                record=_record(a, rule, parsed, refs, version, model)))
        _classify_mismatch(ts, m)
        st.trials[tid] = ts
    return st


_TIER_ORDER = {"unknown": 0, "self_report": 1, "declared_record": 2, "verified": 3}


def _basis_tier(refs: list[str], fv: FactEvidenceView) -> str:
    tiers = {r.fact_id: r.tier for e in fv.fields.values() for r in e.facts}
    found = [tiers[i] for i in refs if i in tiers]
    return min(found, key=_TIER_ORDER.__getitem__) if found else "unknown"


def _classify_mismatch(ts: TrialState, m: TrialMatch) -> None:
    """경로(코호트)별 불충족 확정 수준. 기존 판정(eligibility)을 바꾸지 않음."""
    paths = list(m.cohort_results) or ["all"]
    states, basis = {}, set()
    for path in paths:
        crit = [c for c in ts.criteria
                if c.requirement != "waivable" and (c.cohort is None or path == "all" or c.cohort == path)]
        dead = [c for c in crit if c.status == "NOT_MET" and not c.conflict_on_decided]
        if not dead:
            states[path] = "open"
            continue
        basis |= {c.key for c in dead}
        states[path] = "confirmed" if any(c.basis_tier == "verified" for c in dead) else "provisional"
    ts.open_paths = [p for p, s in states.items() if s == "open"]
    ts.mismatch_basis = sorted(basis)
    if ts.open_paths:
        ts.mismatch = "none"
    elif all(s == "confirmed" for s in states.values()):
        ts.mismatch = "confirmed"
    else:
        ts.mismatch = "provisional"


def fresh_snapshot(view: SessionView, facts) -> SessionView:
    """사실을 더한 새 보기 (시뮬레이션용, 원래 보기는 그대로)."""
    return SessionView(**{**view.__dict__, "snapshot": make_snapshot(view.patient_id, facts)})
