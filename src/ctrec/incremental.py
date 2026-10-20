"""사실 기반 전체 평가, 부분 재평가(IncrementalEvaluator), 전체·부분 동등성 비교 (Phase 2). 진단용입니다.

실제 판정·질문·추천은 기존 Matcher 경로를 그대로 쓰며, 이 모듈의 결과는 섞지 않습니다.

평가 단위 (fact_compare의 사실 기반 평가와 같은 규칙)
- 사실 스냅샷 -> FactEvidenceView -> PatientProfile -> 기준별 원 판정(파싱 검증 review / 규칙 엔진 / LLM 판정 공급자)
  -> 근거 게이트(fact_view.gate) -> 기준 묶음·코호트 집계(matcher._select_cohort, matcher.decide)
- 구조화 필드의 사실 값이 맞지 않으면(충돌) 그 기준은 판정 불가(conflict)로 둠 (LLM에 묻기 전에 확인)
- LLM 판정은 assessor(판정 공급자)가 있을 때만 씀. 이 모듈은 LLM을 직접 부르지 않으며, assessor가 없으면
  LLM이 필요한 기준은 MET/NOT_MET을 만들지 않고 내부 상태 needs_full_evaluation(reason=llm_required)로 둡니다.

기준 결과의 내부 상태 (외부 판정 상태 ELIGIBLE/UNCERTAIN/... 와 MatchStatus는 추가·변경하지 않음)
- evaluated              : 이번 스냅샷으로 계산 (provenance.evaluated_version = 새 버전, 근거 사실 연결)
- reused                 : 이전 결과를 재사용 (provenance는 원래 것 그대로, validated_version만 새 버전)
- needs_full_evaluation  : 계산할 수 없음 (llm_required, evaluation_error, fallback_not_performed). 판정 값 없음 -
                           이전 결과(stale)를 대신 쓰지 않음
시험 단위: decided(모든 기준 결과가 있어 집계함) / needs_full_evaluation(빠진 기준이 있어 집계하지 않음) /
not_evaluated(기준표가 없음 - 기존 NOT_EVALUATED 보존).

부분 재평가 (update)
1) 변경 감지(fact_changes) 2) 의존 그래프·영향 분석(dependency_graph) 3) 영향받는 기준만 다시 계산
4) 영향 없는 기준은 재사용하되 두 단계로 확인 (LLM 호출 없음)
   - 구조적 재사용 조건(_reuse_allowed): 판정 입력(사실 내용)이 조금이라도 바뀌었으면 규칙 엔진·파싱 검증처럼 의존 항목이
     코드 경로로 검증된 결과만 재사용. LLM 판정은 입력 항목을 검증할 수 없으므로 사실 내용이 그대로일 때(중복 입력,
     fact_id만 다름)만 재사용. 의존 그래프·영향 분석의 결과와 상관없이 적용되므로, 매핑이 틀리거나 동등성 비교를 끄더라도
     이전 LLM 결과가 최신 판정으로 재사용되지 않음
   - 유효성 확인(_revalidate): 근거 사실의 서명이 새 스냅샷에 있어야 하고, LLM 판정은 근거 게이트를 다시 거쳐 결과가 같아야 함
   어느 하나라도 아니면 다시 계산. LLM 판정이 필요한데 공급자가 없으면 needs_full_evaluation(llm_required)
5) 영향받은 시험의 기준 묶음·코호트·시험 판정 재집계
전체 재평가가 필요한 경우(정책 변경, 이전 결과와 스냅샷 버전 불일치, 시험 추가, 기준표 변경)는 그 시험을 전체 평가로
대신하고(full_fallback=True) fallback.performed에 기록. 끄면 계산하지 않고 이유를 기록.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Callable, Literal

from pydantic import BaseModel

from .agents import matcher
from .dependency_graph import CriterionDependency, DependencyGraph, ImpactReport, analyze_impact, resolve
from .fact_changes import FactChangeSet, FactSnapshot, detect_changes, fact_signature, signature_id, snapshot_store
from .fact_compare import _conflict_unknown, _rebuild, state
from .fact_view import FactEvidenceView, _engine_field, gate, relevant_biomarker_facts, supporting_facts
from .schemas import CriterionAssessment, ParsedTrial, PatientProfile, Rule
from .tools import rule_engine

# 판정 공급자: (시험, 기준, 사실 기반 프로필, 사실 보기) -> (원 판정, 출처 표시) 또는 None(판정 없음)
Assessor = Callable[[str, Rule, PatientProfile, FactEvidenceView], "tuple[CriterionAssessment, str] | None"]
CriterionStatus = Literal["evaluated", "reused", "needs_full_evaluation"]


class EvaluationPolicy(BaseModel):
    reference_date: str
    require_document_verification: bool = False


class Provenance(BaseModel):
    evaluated_version: str  # 계산에 쓴 스냅샷 버전
    evaluator: Literal["review", "rule_engine", "llm", "conflict"]
    llm_source: str | None = None
    fact_ids: list[str] = []
    fact_signatures: list[str] = []  # 근거 사실의 내용 요약값 (fact_id가 달라도 같은 근거인지 비교)


class CriterionResult(BaseModel):
    trial_id: str
    rule_id: str
    cohort: str | None
    status: CriterionStatus
    reason: str | None = None
    raw: CriterionAssessment | None = None  # 근거 게이트 전 판정
    assessment: CriterionAssessment | None = None  # 근거 게이트 후 판정 (집계에 쓰는 값)
    gate_reason: str | None = None
    provenance: Provenance | None = None
    validated_version: str | None = None  # 이 결과가 유효하다고 확인한 마지막 스냅샷 버전

    @property
    def state(self) -> str | None:
        return state(self.assessment)


class TrialEvaluation(BaseModel):
    trial_id: str
    status: Literal["decided", "needs_full_evaluation", "not_evaluated"]
    eligibility: str | None = None
    match_status: str | None = None
    matched_cohort: str | None = None
    cohort_results: dict[str, str] = {}
    rules_digest: str | None = None
    aggregated_version: str | None = None
    criteria: dict[str, CriterionResult] = {}


class EvaluationState(BaseModel):
    patient_id: str
    snapshot_version: str
    policy: EvaluationPolicy
    trials: dict[str, TrialEvaluation] = {}


def rules_digest(parsed: ParsedTrial) -> str:
    data = {"rules": [r.model_dump() for r in parsed.rules], "cohorts": parsed.cohorts,
            "meta": {k: m.model_dump() for k, m in sorted(parsed.rule_meta.items())}}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class _Ctx:
    """한 스냅샷의 평가 입력 (사실 보기, 프로필, 충돌 항목)."""

    def __init__(self, snap: FactSnapshot):
        self.snapshot = snap
        self.view = FactEvidenceView(snapshot_store(snap), snap.patient_id)
        self.profile = self.view.to_profile()
        self.excluded = self.view.excluded()
        self.signatures = {signature_id(fact_signature(f)) for f in snap.facts}
        self.sig_by_id = {f.fact_id: signature_id(fact_signature(f)) for f in snap.facts}


def _provenance(ctx: _Ctx, evaluator, refs, llm_source=None) -> Provenance:
    ids = list(dict.fromkeys(r.fact_id for r in refs))
    return Provenance(evaluated_version=ctx.snapshot.version, evaluator=evaluator, llm_source=llm_source,
                      fact_ids=ids, fact_signatures=sorted({ctx.sig_by_id[i] for i in ids if i in ctx.sig_by_id}))


def _support(raw: CriterionAssessment, rule: Rule, view: FactEvidenceView) -> list:
    facts, _, _ = supporting_facts(raw, rule, view)
    if rule.category == "biomarker":
        facts = facts + relevant_biomarker_facts(rule, view)[1]
    return facts


def evaluate_criterion(tid: str, parsed: ParsedTrial, rule: Rule, ctx: _Ctx, policy: EvaluationPolicy,
                       assessor: Assessor | None) -> CriterionResult:
    """기준 하나를 스냅샷으로 계산. 실패하거나 LLM 판정이 없으면 판정 값 없이 needs_full_evaluation."""
    base = {"trial_id": tid, "rule_id": rule.rule_id, "cohort": rule.cohort}
    try:
        meta = matcher._meta(parsed, rule)
        evaluator, source = None, None
        if meta.review:
            raw, evaluator = matcher._review_assessment(rule, meta.review), "review"
        else:
            raw = rule_engine.evaluate(rule, ctx.profile)
            evaluator = "rule_engine" if raw is not None else None
        field = _engine_field(rule)
        if field in ctx.excluded and (raw is None or raw.passes is None):
            a = _conflict_unknown(rule, ctx.excluded[field])
            return CriterionResult(**base, status="evaluated", raw=a, assessment=a, gate_reason="conflict",
                                   provenance=_provenance(ctx, "conflict", ctx.view.fields[field].facts),
                                   validated_version=ctx.snapshot.version)
        if raw is None and assessor is not None:
            got = assessor(tid, rule, ctx.profile, ctx.view)
            if got is not None:
                raw, source = got
                evaluator = "llm"
        if raw is None:
            return CriterionResult(**base, status="needs_full_evaluation", reason="llm_required")
        gated, why = gate(raw, rule, meta, ctx.view, policy.reference_date, policy.require_document_verification)
        return CriterionResult(**base, status="evaluated", raw=raw, assessment=gated, gate_reason=why,
                               provenance=_provenance(ctx, evaluator, _support(raw, rule, ctx.view), source),
                               validated_version=ctx.snapshot.version)
    except Exception as e:  # noqa: BLE001 - 계산 실패: 이전 결과를 쓰지 않고 상태로 남김 (메시지에는 환자 값이 있을 수 있음)
        return CriterionResult(**base, status="needs_full_evaluation", reason=f"evaluation_error:{type(e).__name__}")


def aggregate(patient_id: str, parsed: ParsedTrial, criteria: dict[str, CriterionResult],
              version: str) -> TrialEvaluation:
    """기준 묶음(AND)·코호트(가장 나은 것)·시험 판정. 빠진 기준이 있으면 집계하지 않음 (판정 불가를 불충족으로 바꾸지 않음)."""
    base = {"trial_id": parsed.trial_id, "rules_digest": rules_digest(parsed), "aggregated_version": version,
            "criteria": criteria}
    if any(criteria[r.rule_id].assessment is None for r in parsed.rules):
        return TrialEvaluation(**base, status="needs_full_evaluation")
    m = _rebuild(patient_id, parsed, [criteria[r.rule_id].assessment for r in parsed.rules])
    return TrialEvaluation(**base, status="decided", eligibility=m.eligibility, match_status=m.match_status,
                           matched_cohort=m.matched_cohort, cohort_results=m.cohort_results)


def _full_trial(tid: str, p: ParsedTrial | None, ctx: _Ctx, policy: EvaluationPolicy,
                assessor: Assessor | None) -> TrialEvaluation:
    if p is None:
        return TrialEvaluation(trial_id=tid, status="not_evaluated")
    criteria = {r.rule_id: evaluate_criterion(tid, p, r, ctx, policy, assessor) for r in p.rules}
    return aggregate(ctx.snapshot.patient_id, p, criteria, ctx.snapshot.version)


def evaluate_full(snapshot: FactSnapshot, parsed: dict[str, ParsedTrial | None], policy: EvaluationPolicy,
                  assessor: Assessor | None = None) -> EvaluationState:
    """전체 평가: 모든 시험의 모든 기준을 스냅샷으로 계산. parsed 값이 None이면 NOT_EVALUATED 시험."""
    ctx = _Ctx(snapshot)
    return EvaluationState(patient_id=snapshot.patient_id, snapshot_version=snapshot.version, policy=policy,
                           trials={tid: _full_trial(tid, p, ctx, policy, assessor) for tid, p in parsed.items()})


# ---------------------------------------------------------------------------
# 부분 재평가
# ---------------------------------------------------------------------------

class IncrementalResult(BaseModel):
    state: EvaluationState
    changes: FactChangeSet
    impact: ImpactReport | None
    graph: dict
    stats: dict
    fallback: dict

    def summary(self) -> dict:
        """환자 값 없이 항목·ID·건수·이유 코드만."""
        return {"changes": self.changes.summary(), "impact": self.impact.summary() if self.impact else None,
                "graph": self.graph, "stats": self.stats, "fallback": self.fallback}


def _same_assessment(a: CriterionAssessment | None, b: CriterionAssessment | None) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and a.model_dump() == b.model_dump())


def _revalidate(prev: CriterionResult, parsed: ParsedTrial, rule: Rule, ctx: _Ctx,
                policy: EvaluationPolicy) -> bool:
    """영향 없는 기준의 이전 결과가 새 스냅샷에서도 유효한가 (LLM 호출 없음)."""
    if prev.provenance is None or prev.assessment is None:
        return False
    if any(s not in ctx.signatures for s in prev.provenance.fact_signatures):
        return False  # 근거 사실이 새 스냅샷에 없음
    if prev.provenance.evaluator == "llm" and prev.raw is not None:  # 근거 연결은 다른 항목의 사실에도 영향받음
        gated, why = gate(prev.raw, rule, matcher._meta(parsed, rule), ctx.view, policy.reference_date,
                          policy.require_document_verification)
        return _same_assessment(gated, prev.assessment) and why == prev.gate_reason
    return True


_VERIFIED_EVALUATORS = {"rule_engine", "conflict", "review"}  # 읽는 입력 항목이 코드 경로로 정해진 판정


def _reuse_allowed(prev: CriterionResult, dep: CriterionDependency, changes: FactChangeSet) -> bool:
    """구조적 재사용 조건 (의존 그래프와 별개의 안전장치). 사실 내용이 그대로면 어떤 결과든 재사용 가능."""
    if not changes.changed:
        return True
    ev = prev.provenance.evaluator if prev.provenance else None
    return dep.evaluator in ("rule_engine", "review") and not dep.wildcard and ev in _VERIFIED_EVALUATORS


def update(prev_state: EvaluationState, prev_snapshot: FactSnapshot, new_snapshot: FactSnapshot,
           parsed: dict[str, ParsedTrial | None], policy: EvaluationPolicy, assessor: Assessor | None = None,
           full_fallback: bool = True) -> IncrementalResult:
    """이전 결과 + 두 스냅샷 -> 새 결과. 영향받는 기준만 다시 계산하고 나머지는 확인 후 재사용."""
    if not (prev_state.patient_id == prev_snapshot.patient_id == new_snapshot.patient_id):
        raise ValueError("다른 환자의 결과·스냅샷은 함께 쓰지 않음")
    changes = detect_changes(prev_snapshot, new_snapshot)
    ctx_prev, ctx = _Ctx(prev_snapshot), _Ctx(new_snapshot)
    pid = new_snapshot.patient_id

    global_reasons = []
    if prev_state.snapshot_version != prev_snapshot.version:
        global_reasons.append("state_version_mismatch")
    if prev_state.policy != policy:
        global_reasons.append("policy_changed")
    fallback_trials: dict[str, str] = {}
    incremental: dict[str, ParsedTrial] = {}
    new_trials: dict[str, TrialEvaluation] = {}
    for tid, p in parsed.items():
        old = prev_state.trials.get(tid)
        if p is None:
            new_trials[tid] = TrialEvaluation(trial_id=tid, status="not_evaluated")
        elif global_reasons:
            fallback_trials[tid] = global_reasons[0]
        elif old is None or old.status == "not_evaluated":
            fallback_trials[tid] = "trial_added"
        elif old.rules_digest != rules_digest(p) or set(old.criteria) != {r.rule_id for r in p.rules}:
            fallback_trials[tid] = "rules_changed"
        else:
            incremental[tid] = p

    deps: dict[tuple[str, str], CriterionDependency] = {}
    for tid, p in incremental.items():
        for r in p.rules:
            prev_c = prev_state.trials[tid].criteria[r.rule_id]
            deps[(tid, r.rule_id)] = resolve(tid, r, matcher._meta(p, r), prev_c.raw, ctx_prev.view)
    graph = DependencyGraph.build(dict(incremental), deps, new_snapshot)
    impact = analyze_impact(changes, graph, prev_snapshot, new_snapshot)
    affected = {(a.trial_id, a.rule_id) for a in impact.affected_criteria}

    count = Counter()
    for tid, p in incremental.items():
        old = prev_state.trials[tid]
        criteria: dict[str, CriterionResult] = {}
        touched = False
        for r in p.rules:
            prev_c = old.criteria[r.rule_id]
            key = (tid, r.rule_id)
            if key not in affected and prev_c.status != "needs_full_evaluation":
                if not _reuse_allowed(prev_c, deps[key], changes):
                    count["reuse_blocked"] += 1  # 그래프가 영향 없다고 해도 검증되지 않은 판정은 재사용하지 않음
                    c = evaluate_criterion(tid, p, r, ctx, policy, assessor)
                    criteria[r.rule_id] = c
                    touched = True
                    count["reevaluated"] += 1
                    count["unavailable"] += c.status == "needs_full_evaluation"
                    continue
                count["validity_checks"] += 1
                if _revalidate(prev_c, p, r, ctx, policy):
                    criteria[r.rule_id] = prev_c.model_copy(deep=True, update={
                        "status": "reused", "validated_version": new_snapshot.version})
                    count["reused"] += 1
                    continue
                count["validity_failures"] += 1
            elif key not in affected:
                count["retried_unavailable"] += 1  # 이전에 계산하지 못한 기준: 다시 시도 (이전 상태를 재사용하지 않음)
            c = evaluate_criterion(tid, p, r, ctx, policy, assessor)
            criteria[r.rule_id] = c
            touched = True
            count["reevaluated"] += 1
            if c.status == "needs_full_evaluation":
                count["unavailable"] += 1
        if touched or old.aggregated_version is None:
            new_trials[tid] = aggregate(pid, p, criteria, new_snapshot.version)
            count["reaggregated_trials"] += 1
        else:  # 모든 기준 재사용: 시험 판정도 그대로 (집계 입력이 같음)
            new_trials[tid] = old.model_copy(deep=True, update={"criteria": criteria})

    fallback = {"required": bool(fallback_trials), "performed": False, "trials": dict(sorted(fallback_trials.items())),
                "reason": None, "unresolved_trials": []}
    for tid, why in fallback_trials.items():
        p = parsed[tid]
        if full_fallback:
            t = _full_trial(tid, p, ctx, policy, assessor)
            count["fallback_evaluated"] += len(p.rules)
            count["unavailable"] += sum(c.status == "needs_full_evaluation" for c in t.criteria.values())
        else:
            t = TrialEvaluation(trial_id=tid, status="needs_full_evaluation", rules_digest=rules_digest(p),
                                criteria={r.rule_id: CriterionResult(trial_id=tid, rule_id=r.rule_id, cohort=r.cohort,
                                                                     status="needs_full_evaluation",
                                                                     reason="fallback_not_performed")
                                          for r in p.rules})
        new_trials[tid] = t
    if fallback_trials:
        fallback["performed"] = full_fallback
        fallback["reason"] = None if full_fallback else "full_fallback_disabled"
        fallback["unresolved_trials"] = sorted(t for t in fallback_trials if new_trials[t].status != "decided")

    total = sum(len(p.rules) for p in parsed.values() if p is not None)
    stats = {"total_criteria": total, "changed_fields": len(changes.changed),
             "affected_criteria": len(affected), "reevaluated": count["reevaluated"], "reused": count["reused"],
             "fallback_criteria": len(impact.fallback_criteria) + count["fallback_evaluated"],
             "fallback_evaluated": count["fallback_evaluated"], "unavailable": count["unavailable"],
             "validity_checks": count["validity_checks"], "validity_failures": count["validity_failures"],
             "retried_unavailable": count["retried_unavailable"], "reuse_blocked": count["reuse_blocked"],
             "reaggregated_trials": count["reaggregated_trials"]}
    new_state = EvaluationState(patient_id=pid, snapshot_version=new_snapshot.version, policy=policy,
                                trials={t: new_trials[t] for t in parsed})
    return IncrementalResult(state=new_state, changes=changes, impact=impact, graph=graph.summary(), stats=stats,
                             fallback=fallback)


# ---------------------------------------------------------------------------
# 전체 vs 부분 동등성
# ---------------------------------------------------------------------------

_SEVERITY = {"equivalent": 0, "incomplete_comparison": 1, "fallback_required": 2, "mismatch": 3}
_ASSESSMENT_KEYS = ("holds", "passes", "applicable", "needs_verification", "evidence_source", "required_evidence",
                    "underspecified", "self_reported_holds", "self_reported_applicable", "evidence", "reasoning",
                    "missing_info", "method", "cohort", "confidence")


def _compare_criterion(inc: CriterionResult | None, full: CriterionResult | None) -> tuple[str, list[str]]:
    if inc is None or full is None:
        return "mismatch", ["missing_result"]
    if inc.assessment is None and full.assessment is None:
        return "incomplete_comparison", [f"incremental:{inc.reason}", f"full:{full.reason}"]
    if inc.assessment is None:
        return "fallback_required", [f"incremental:{inc.reason}"]
    if full.assessment is None:
        return "incomplete_comparison", [f"full:{full.reason}"]
    diffs = []
    if inc.state != full.state:
        diffs.append(f"state:{inc.state}->{full.state}")
    a, b = inc.assessment.model_dump(), full.assessment.model_dump()
    diffs += [k for k in _ASSESSMENT_KEYS if a.get(k) != b.get(k)]
    if inc.gate_reason != full.gate_reason:
        diffs.append("gate_reason")
    pi, pf = inc.provenance, full.provenance
    if (pi.evaluator if pi else None) != (pf.evaluator if pf else None):
        diffs.append("evaluator")
    if (pi.fact_signatures if pi else []) != (pf.fact_signatures if pf else []):
        diffs.append("evidence_facts")
    return ("mismatch" if diffs else "equivalent"), diffs


def compare_states(inc: EvaluationState, full: EvaluationState) -> dict:
    """같은 스냅샷·기준표·정책의 부분 재평가 결과와 전체 평가 결과 비교. ID·상태·차이 항목 이름만."""
    if (inc.patient_id, inc.snapshot_version, inc.policy) != (full.patient_id, full.snapshot_version, full.policy):
        return {"status": "mismatch", "reason": "different_inputs", "trials": [], "counts": {}}
    trials, counts = [], Counter()
    for tid in sorted(set(inc.trials) | set(full.trials)):
        ti, tf = inc.trials.get(tid), full.trials.get(tid)
        if ti is None or tf is None:
            trials.append({"trial_id": tid, "status": "mismatch", "diffs": ["missing_trial"], "criteria": []})
            counts["mismatch"] += 1
            continue
        crit = []
        for rid in sorted(set(ti.criteria) | set(tf.criteria)):
            s, diffs = _compare_criterion(ti.criteria.get(rid), tf.criteria.get(rid))
            counts[s] += 1
            if s != "equivalent":
                crit.append({"rule_id": rid, "status": s, "diffs": diffs})
        tdiffs = []
        if ti.status != tf.status:
            tdiffs.append(f"status:{ti.status}->{tf.status}")
        elif ti.status == "decided":
            tdiffs += [k for k in ("eligibility", "match_status", "matched_cohort", "cohort_results")
                       if getattr(ti, k) != getattr(tf, k)]
        if tdiffs and not (ti.status == "needs_full_evaluation" or tf.status == "needs_full_evaluation"):
            tstatus = "mismatch"
        elif ti.status == "needs_full_evaluation" and tf.status == "decided":
            tstatus = "fallback_required"
        elif tf.status == "needs_full_evaluation":
            tstatus = "incomplete_comparison"
        else:
            tstatus = "equivalent"
        worst = max([tstatus] + [c["status"] for c in crit], key=_SEVERITY.__getitem__)
        trials.append({"trial_id": tid, "status": worst, "trial_status": tstatus, "diffs": tdiffs, "criteria": crit})
    overall = max([t["status"] for t in trials] or ["equivalent"], key=_SEVERITY.__getitem__)
    return {"status": overall, "counts": dict(counts), "trials": [t for t in trials if t["status"] != "equivalent"],
            "n_trials": len(trials), "llm_judgments": _llm_scope(inc, full)}


def _llm_scope(inc: EvaluationState, full: EvaluationState) -> dict:
    """LLM 판정 기준의 비교 범위. 양쪽이 같은 판정 공급자(assessor)의 출력을 썼으므로, LLM 기준의 '같음'은
    그 공급자 출력에 대한 동등성일 뿐 실제 LLM을 다시 실행한 판정과의 동등성이 아님."""
    def llm(st):
        return [c for t in st.trials.values() for c in t.criteria.values()
                if c.provenance is not None and c.provenance.evaluator == "llm"]
    li, lf = llm(inc), llm(full)
    unavailable = sum(c.reason == "llm_required" for st in (inc, full) for t in st.trials.values()
                      for c in t.criteria.values())
    return {"compared": min(len(li), len(lf)), "unavailable": unavailable,
            "sources": sorted({c.provenance.llm_source or "unknown" for c in li + lf}),
            "scope": "assessor_outputs_only" if li or lf else "none",
            "note": "LLM 기준의 같음은 양쪽이 쓴 같은 판정 공급자 출력 기준이며, 실제 LLM 재판정과의 동등성을 뜻하지 않음"}


# ---------------------------------------------------------------------------
# Session 진단 (기본 off): 스냅샷마다 부분 재평가와 전체 평가를 모두 하고 비교
# ---------------------------------------------------------------------------

def baseline_assessor(matches: dict, fresh: set[str]) -> Assessor:
    """기존 Matcher가 이번 매칭에서 막 낸 LLM 판정(TrialMatch 사본)만 판정 공급자로 씀. 새 LLM 호출 없음.

    fresh: 이번 매칭에서 다시 판정한 시험. 이번에 다시 매칭되지 않은 시험의 판정은 이전 사실로 낸 것이므로 공급하지 않음
    (이전 LLM 결과를 최신 판정으로 쓰지 않음) -> 그 시험의 LLM 기준은 needs_full_evaluation(llm_required).
    """
    table = {tid: {a.rule_id: a for a in m.assessments + m.other_cohort_assessments if a.method == "llm"}
             for tid, m in matches.items() if tid in fresh}

    def assess(tid: str, rule: Rule, profile: PatientProfile, view: FactEvidenceView):
        a = table.get(tid, {}).get(rule.rule_id)
        return (a.model_copy(deep=True), "baseline_matcher") if a is not None else None
    return assess


def _state_counts(st: EvaluationState) -> dict:
    return {"trials": dict(Counter(t.status for t in st.trials.values())),
            "criteria": dict(Counter(c.status for t in st.trials.values() for c in t.criteria.values()))}


class DependencyTracker:
    """한 Session·한 환자의 진단 상태. 이전 스냅샷과 결과를 들고 있다가 새 스냅샷마다 부분 재평가 + 전체 평가 + 비교."""

    def __init__(self, patient_id: str, policy: EvaluationPolicy):
        self.patient_id = patient_id
        self.policy = policy
        self.snapshot: FactSnapshot | None = None
        self.state: EvaluationState | None = None

    def observe(self, snapshot: FactSnapshot, parsed: dict[str, ParsedTrial | None],
                assessor: Assessor | None = None) -> dict:
        """반환: 진단 기록 (항목 이름·ID·건수·이유 코드만, 환자 값 없음)."""
        if snapshot.patient_id != self.patient_id:
            raise ValueError("다른 환자의 스냅샷")
        full = evaluate_full(snapshot, parsed, self.policy, assessor)
        if self.state is None or self.snapshot is None:
            self.snapshot, self.state = snapshot, full
            return {"mode": "initial", "snapshot_version": snapshot.version, "full": _state_counts(full)}
        r = update(self.state, self.snapshot, snapshot, parsed, self.policy, assessor)
        eq = compare_states(r.state, full)
        # 다음 부분 재평가의 기준: 동등하면 부분 재평가 결과(재사용 이력 유지), 아니면 전체 평가 결과
        base = "incremental" if eq["status"] == "equivalent" else "full"
        self.snapshot, self.state = snapshot, (r.state if base == "incremental" else full)
        return {"mode": "incremental", **r.summary(), "equivalence": eq, "full": _state_counts(full),
                "incremental": _state_counts(r.state), "base_for_next": base}
