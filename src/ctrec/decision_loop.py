"""Agentic Decision Loop 실행기와 루프 제어 (Phase 3). 기본은 진단(shadow) 전용입니다.

흐름: Decision State 구성 -> 행동 후보(decision_policy) -> 안전 규칙·선행 조건 확인 -> 우선순위대로 선택
      -> 실행(아래 범위) -> 사실·상태 변화 확인 -> 종료 조건 확인 -> 다음 반복
기존 Matcher 판정·질문·추천을 바꾸지 않고, LLM·네트워크를 부르지 않습니다.

실행 범위 (EXECUTOR_MODES)
- ASK_USER         DIAGNOSTIC_ONLY: 질문 후보만 기록(proposed_not_dispatched). 기존 웹 질문 흐름에 끼워 넣지 않음.
                   답변 공급자(QuestionChannel)를 주입하면 실행(시뮬레이션·향후 연결용): 답은 자기 보고 사실로 작업용
                   사본 저장소에만 추가. 같은 정보는 한 루프에서 한 번만, 모름·거부 뒤에는 다시 묻지 않음
- REQUEST_DOCUMENT REQUIRES_EXTERNAL_INPUT: 필요한 문서와 이유만 기록, 검증 완료로 표시하지 않음
- VERIFY           EXECUTABLE: 출처·검증 상태·충돌을 코드로 확인. 해소할 수 없으면 requires_review (승격 없음)
- RUN_TOOL         EXECUTABLE: 허용 목록(deterministic_reevaluation: Phase 2 규칙 엔진 재계산)만. 그 밖은 거절
- REPARSE          DIAGNOSTIC_ONLY: 재추출은 LLM(환자 정보 이해 에이전트)이 필요하므로 필요성만 기록
- RETRIEVE         DIAGNOSTIC_ONLY: 추가 검색 필요성만 기록 (네트워크 요청 없음, 고정 기준표 변경 없음)
- DEFER / NEEDS_REVIEW / FINALIZE  EXECUTABLE(상태 기록): 보류·검토·종료 사유와 미해결 항목 기록
종료 사유: sufficient_information, needs_review, awaiting_external_input, awaiting_user_answers, user_declined,
no_valid_action, no_information_change, repeat_risk, iteration_limit, tool_call_limit, cost_limit, time_limit,
cancelled, timeout(실행 시간 제한), tool_failure
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from pydantic import BaseModel, Field

from . import llm
from .agents import matcher
from .dependency_graph import DependencyGraph, analyze_impact, resolve
from .fact_view import _engine_field
from .fact_changes import FactSnapshot, detect_changes, make_snapshot, snapshot_store, take_snapshot
from .decision_policy import ALLOWED_TOOLS, DEFAULT_POLICY, PolicyConfig, candidates, validate
from .decision_state import (Action, Budget, BudgetUsage, DecisionState, SessionView, build_state)
from .schemas import CriterionAssessment, QAPair

EXECUTOR_MODES = {
    "ASK_USER": "DIAGNOSTIC_ONLY",  # 답변 공급자를 주입하면 EXECUTABLE
    "REQUEST_DOCUMENT": "REQUIRES_EXTERNAL_INPUT",
    "VERIFY": "EXECUTABLE",
    "RUN_TOOL": "EXECUTABLE",
    "REPARSE": "DIAGNOSTIC_ONLY",
    "RETRIEVE": "DIAGNOSTIC_ONLY",
    "DEFER": "EXECUTABLE",
    "NEEDS_REVIEW": "EXECUTABLE",
    "FINALIZE": "EXECUTABLE",
}


class LoopConfig(BaseModel):
    budget: Budget = Field(default_factory=Budget)
    policy: PolicyConfig = DEFAULT_POLICY
    max_no_change: int = 2  # 질문을 보냈는데 사실이 바뀌지 않은 연속 횟수 상한


class QuestionChannel(Protocol):
    def ask(self, action: Action, state: DecisionState) -> dict | None:
        """None: 보내지 않음. dict: {"facts": [(field, value, kwargs)], "declined": bool}"""


class NoDispatch:
    """진단 기본값: 질문을 실제로 보내지 않음 (기존 웹 질문 흐름에 끼워 넣지 않음)."""

    def ask(self, action, state):
        return None


@dataclass
class LoopContext:
    view: SessionView
    matched_snapshots: dict[str, FactSnapshot] = field(default_factory=dict)  # 시험 -> 기준선 판정 당시 스냅샷
    overlay: dict[str, CriterionAssessment] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)  # 규칙 엔진으로도 다시 계산하지 못한 기준 (LLM 필요)
    history: list[Action] = field(default_factory=list)
    tools: dict[str, Callable] = field(default_factory=dict)


class LoopResult(BaseModel):
    actions: list[Action]
    rejected: list[dict] = []
    stop_reason: str
    finalize_reasons: list[str] = []
    iterations: int
    usage: BudgetUsage
    stats: dict
    final_state: dict

    def summary(self) -> dict:
        """환자 값 없이 행동·이유 코드·ID·건수만."""
        return {
            "stop_reason": self.stop_reason, "finalize_reasons": self.finalize_reasons,
            "iterations": self.iterations, "usage": self.usage.model_dump(), "stats": self.stats,
            "state": self.final_state, "rejected": self.rejected,
            "actions": [a.model_dump(include={"action_type", "reason_code", "human_readable_reason", "tier",
                                              "priority", "value_score", "expected_information_gain",
                                              "estimated_cost", "target_trial_ids", "target_criterion_ids",
                                              "required_fact_fields", "evidence_refs", "info_key", "tool",
                                              "prerequisites", "status", "outcome"}) for a in self.actions],
        }


# ---------------------------------------------------------------------------
# 사실 변경 -> 기준선 판정의 재사용 가능 여부 (Phase 2 규칙)
# ---------------------------------------------------------------------------

def staleness(ctx: LoopContext) -> tuple[list[str], dict[str, set[str]], dict]:
    """반환: (다시 계산할 규칙 엔진 기준, 시험별 다시 쓸 수 없는 LLM 기준, 통계).

    기준선 판정 당시 스냅샷과 현재 스냅샷을 비교해 영향받는 기준을 찾음. LLM 기준은 입력 항목을 검증할 수 없어
    사실이 바뀌면 항상 영향(dependency_graph.resolve). 규칙 엔진 기준은 그 필드가 바뀐 경우에만.
    영향받은 기준 중 규칙 엔진이 구조적으로 판정할 수 있는 기준(구조화 필드·연산자)은 결정적 재계산을 시도하고
    (기준선이 정보 부족으로 LLM 경로였어도), 재계산하지 못하면 LLM 재판정이 필요한 기준으로 남김.
    """
    v = ctx.view
    pending, stale, stats = [], {}, {"affected": 0, "rule_engine_reused": 0, "rule_engine_pending": 0,
                                     "llm_stale": 0, "trials_compared": 0}
    for tid, m in v.matches.items():
        base = ctx.matched_snapshots.get(tid)
        p = v.parsed.get(tid)
        if base is None or p is None or m.eligibility == "NOT_EVALUATED" or base.version == v.snapshot.version:
            continue
        stats["trials_compared"] += 1
        changes = detect_changes(base, v.snapshot)
        if not changes.changed:
            continue
        by_rule = {a.rule_id: a for a in m.assessments + m.other_cohort_assessments}
        deps = {(tid, r.rule_id): resolve(tid, r, matcher._meta(p, r), by_rule.get(r.rule_id)) for r in p.rules}
        impact = analyze_impact(changes, DependencyGraph.build({tid: p}, deps), base, v.snapshot)
        rules = {r.rule_id: r for r in p.rules}
        for a in impact.affected_criteria:
            stats["affected"] += 1
            key = f"{tid}:{a.rule_id}"
            rule = rules[a.rule_id]
            engine_capable = (bool(_engine_field(rule) and rule.structured_evaluable and rule.operator)
                              and not matcher._meta(p, rule).review)
            if engine_capable and key not in ctx.unavailable:
                if key not in ctx.overlay:
                    pending.append(key)
                    stats["rule_engine_pending"] += 1
            else:
                stale.setdefault(tid, set()).add(a.rule_id)
                stats["llm_stale"] += 1
        stats["rule_engine_reused"] += sum(1 for k in impact.unaffected_criteria
                                           if deps[tuple(k.split(":", 1))].evaluator == "rule_engine")
    return pending, stale, stats


# ---------------------------------------------------------------------------
# 실행기
# ---------------------------------------------------------------------------

def _tool_deterministic_reevaluation(ctx: LoopContext, action: Action) -> dict:
    """Phase 2 사실 기반 평가로 규칙 엔진 기준만 다시 계산 (LLM 공급자 없음 -> LLM 판정은 만들지 않음)."""
    from .incremental import EvaluationPolicy, _Ctx, evaluate_criterion
    v = ctx.view
    fctx = _Ctx(v.snapshot)
    policy = EvaluationPolicy(reference_date=v.reference_date or "2026-01-01")
    done, unavailable = 0, 0
    for key in action.target_criterion_ids:
        tid, rid = key.split(":", 1)
        p = v.parsed[tid]
        rule = next(r for r in p.rules if r.rule_id == rid)
        r = evaluate_criterion(tid, p, rule, fctx, policy, assessor=None)
        if r.assessment is not None:
            ctx.overlay[key] = r.assessment.model_copy(update={"cohort": rule.cohort})
            done += 1
        else:
            ctx.unavailable.add(key)
            unavailable += 1
    return {"reevaluated": done, "unavailable": unavailable}


DEFAULT_TOOLS = {"deterministic_reevaluation": _tool_deterministic_reevaluation}


def _ask(ctx: LoopContext, action: Action, st: DecisionState, channel: QuestionChannel) -> tuple[str, dict]:
    reply = channel.ask(action, st)
    if reply is None:
        return "proposed_not_dispatched", {"dispatched": False}
    v = ctx.view
    store = snapshot_store(v.snapshot)  # 작업용 사본에만 추가 (Session의 사실 저장소는 바꾸지 않음)
    added = 0
    for fld, value, kw in reply.get("facts", []):
        store.add(v.patient_id, fld, value, source_type="self_report",
                  source_reference={"kind": "answer", "ref": f"decision:{action.action_id}"}, **kw)
        added += 1
    declined = bool(reply.get("declined"))
    v.snapshot = make_snapshot(v.patient_id, store.facts(v.patient_id))
    v.qa_log.append(QAPair(round=len(v.qa_log) + 1, question_id=action.action_id, question=action.info_key or "",
                           answer="모름" if declined else "(합성 답변)", purpose="eligibility",
                           target=list(action.target_criterion_ids)))
    return "executed", {"dispatched": True, "facts_added": added, "declined": declined}


def _verify(ctx: LoopContext, action: Action, st: DecisionState) -> tuple[str, dict]:
    """코드로 확인할 수 있는 것만: 근거 사실의 출처·검증 상태, 항목 충돌. 문서 검증은 하지 않고 승격하지 않음."""
    fields = set(action.required_fact_fields)
    checked = [f for f in ctx.view.snapshot.facts if f.field in fields]
    by_key = {c.key: c for c in st.criteria()}
    still = sorted(k for k in action.target_criterion_ids
                   if k in by_key and (by_key[k].gap == "conflict" or by_key[k].conflict_on_decided))
    return "executed", {"checked_fact_ids": sorted(f.fact_id for f in checked),
                        "document_verified": sum(f.verification_status == "document_verified" for f in checked),
                        "source_types": sorted({f.source_type for f in checked}),
                        "unresolved_conflict": still, "requires_review": bool(still)}


def execute(ctx: LoopContext, action: Action, st: DecisionState, channel: QuestionChannel) -> tuple[str, dict]:
    t = action.action_type
    if t == "ASK_USER":
        return _ask(ctx, action, st, channel)
    if t == "REQUEST_DOCUMENT":
        return "awaiting_external_input", {"requires_external_input": True, "verified": False,
                                           "document_for": action.target_criterion_ids}
    if t == "VERIFY":
        return _verify(ctx, action, st)
    if t == "RUN_TOOL":
        fn = ctx.tools.get(action.tool or "") if action.tool in ALLOWED_TOOLS else None
        if fn is None:
            return "rejected", {"reason": "tool_not_allowed"}
        return "executed", fn(ctx, action)
    if t == "REPARSE":
        return "not_executed", {"reason": "diagnostic_mode_no_llm", "reparse_needed": True}
    if t == "RETRIEVE":
        return "not_executed", {"reason": "diagnostic_mode_no_network", "external_search_needed": True}
    if t == "DEFER":
        return "deferred", {"unresolved": action.target_criterion_ids}
    if t == "NEEDS_REVIEW":
        return "needs_review", {"unresolved": action.target_criterion_ids or action.target_trial_ids}
    return "finalized", {}


def _finalize_reasons(history: list[Action]) -> list[str]:
    reasons = []
    if any(a.status == "needs_review" for a in history):
        reasons.append("needs_review")
    if any(a.status == "awaiting_external_input" for a in history):
        reasons.append("awaiting_external_input")
    if any(a.status == "proposed_not_dispatched" for a in history):
        reasons.append("awaiting_user_answers")
    if any(a.action_type == "DEFER" and a.reason_code == "user_declined_or_exhausted" for a in history) or \
            any(a.outcome.get("declined") for a in history):
        reasons.append("user_declined")
    return reasons or ["sufficient_information"]


# ---------------------------------------------------------------------------
# 루프
# ---------------------------------------------------------------------------

def guard_reason() -> str | None:
    """웹 실행의 취소·시간 제한 확인 (llm.guard와 같은 확인 지점, LLM은 부르지 않음)."""
    try:
        llm.guard()
    except llm.RunCancelled as e:
        return "cancelled" if not e.args or e.args[0] != "timeout" else "timeout"
    return None


def run_loop(ctx: LoopContext, channel: QuestionChannel | None = None, config: LoopConfig | None = None,
             stop_check: Callable[[], str | None] = guard_reason) -> LoopResult:
    config = config or LoopConfig()
    channel = channel or NoDispatch()
    ctx.tools = ctx.tools or dict(DEFAULT_TOOLS)
    budget, usage = config.budget, BudgetUsage()
    t0 = time.perf_counter()
    stats = {"candidates_total": 0, "candidates_last": 0, "duplicates_blocked": 0, "state_build_ms": 0.0,
             "reevaluated": 0, "rule_engine_reused": 0, "llm_stale": 0, "affected": 0}
    rejected: list[dict] = []
    no_change, stop, reasons, st = 0, None, [], None
    cache_key, cached = None, None
    for it in range(budget.max_iterations):
        stop = stop_check()
        if stop:
            break
        if time.perf_counter() - t0 > budget.max_seconds:
            stop = "time_limit"
            break
        tb = time.perf_counter()
        # 사실·재계산 결과·질문 이력이 그대로면 상태를 다시 만들지 않고 행동 이력만 갱신 (진단 오버헤드 감소)
        key = (ctx.view.snapshot.version, len(ctx.overlay), len(ctx.unavailable), len(ctx.view.qa_log))
        if key != cache_key:
            pending, stale, sstats = staleness(ctx)
            cached = (pending, stale, sstats, build_state(ctx.view, ctx.overlay, stale, [], budget))
            cache_key = key
            stats["state_builds"] = stats.get("state_builds", 0) + 1
        pending, stale, sstats, base = cached
        st = base.model_copy(update={"action_history": list(ctx.history)})
        stats["state_build_ms"] += (time.perf_counter() - tb) * 1000
        stats["rule_engine_reused"] = sstats["rule_engine_reused"]
        stats["llm_stale"], stats["affected"] = sstats["llm_stale"], sstats["affected"]
        cands, dup = candidates(st, config.policy, pending)
        stats["candidates_total"] += len(cands)
        stats["candidates_last"] = len(cands)
        stats["duplicates_blocked"] += dup
        chosen = None
        for a in cands:
            why = validate(a, st)
            if why is None and a.action_type == "ASK_USER" and usage.questions >= budget.max_questions:
                why = "question_budget"
            if why is None and usage.cost_units + a.estimated_cost > budget.max_cost_units and a.action_type != "FINALIZE":
                why = "cost_budget"
            if why:
                rejected.append({"action_type": a.action_type, "reason_code": a.reason_code, "why": why,
                                 "targets": a.target_criterion_ids[:20]})
                continue
            chosen = a
            break
        if chosen is None:
            stop = "repeat_risk" if dup else "no_valid_action"
            break
        version_before = ctx.view.snapshot.version
        try:
            status, outcome = execute(ctx, chosen, st, channel)
        except Exception as e:  # noqa: BLE001 - 실행 실패: 안전하게 멈춤 (예외 종류만)
            status, outcome = "failed", {"error_type": type(e).__name__}
        chosen.status, chosen.outcome = status, outcome  # type: ignore[assignment]
        ctx.history.append(chosen)
        usage.iterations = it + 1
        usage.cost_units += chosen.estimated_cost
        if chosen.action_type == "RUN_TOOL":
            usage.tool_calls += 1
            stats["reevaluated"] += outcome.get("reevaluated", 0)
        if chosen.action_type == "ASK_USER":
            usage.questions += 1
        if status == "failed":
            stop = "tool_failure"
            break
        if chosen.action_type == "FINALIZE":
            reasons = _finalize_reasons(ctx.history)
            stop = reasons[0]
            break
        if outcome.get("dispatched"):
            no_change = no_change + 1 if ctx.view.snapshot.version == version_before else 0
            if no_change >= config.max_no_change:
                stop = "no_information_change"
                break
        if usage.tool_calls >= budget.max_tool_calls:
            stop = "tool_call_limit"
            break
        if usage.cost_units >= budget.max_cost_units:
            stop = "cost_limit"
            break
    else:
        stop = "iteration_limit"
    usage.seconds = round(time.perf_counter() - t0, 4)
    stats["state_build_ms"] = round(stats["state_build_ms"], 2)
    final = st or build_state(ctx.view, ctx.overlay, None, ctx.history, budget)
    crit = final.criteria()
    summary = {
        "fact_version": final.fact_version, "trials": len(final.trials),
        "not_evaluated_trials": list(final.not_evaluated_trials), "criteria": len(crit),
        "gaps": {g: sum(c.gap == g for c in crit) for g in ("missing", "unverified", "physician_review", "conflict",
                                                             "stale")},
        "conflicting_fields": sorted(final.conflicting_fields), "fact_reevaluated": len(ctx.overlay),
        "questions_in_history": len(final.question_history),
    }
    return LoopResult(actions=ctx.history, rejected=rejected, stop_reason=stop or "no_valid_action",
                      finalize_reasons=reasons, iterations=usage.iterations, usage=usage, stats=stats,
                      final_state=summary)


# ---------------------------------------------------------------------------
# Session 진단 연결
# ---------------------------------------------------------------------------

class DecisionTracker:
    """한 Session의 진단 상태: 시험별로 기준선 판정이 만들어진 사실 스냅샷을 기억 (재사용 가능 여부 판단용)."""

    def __init__(self, config: LoopConfig | None = None):
        self.config = config or LoopConfig()
        self.matched_snapshots: dict[str, FactSnapshot] = {}

    def observe(self, session, fresh: set[str]) -> dict:
        snap = take_snapshot(session.fact_store, session.case.patient_id)
        for tid in fresh:
            self.matched_snapshots[tid] = snap
        view = SessionView.from_session(session, {t: s.version for t, s in self.matched_snapshots.items()})
        ctx = LoopContext(view=view, matched_snapshots=dict(self.matched_snapshots))
        return run_loop(ctx, NoDispatch(), self.config).summary()
