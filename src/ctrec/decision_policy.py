"""Agentic Decision Loop의 결정적 행동 정책 (Phase 3). 코드가 행동 후보를 만들고 순서를 정합니다(LLM 아님).

우선순위 단계(tier, 작을수록 먼저)
  1 안전·상태 정확성: 상충 사실 확인(VERIFY -> 해결 안 되면 NEEDS_REVIEW), 사실 변경 뒤 규칙 엔진 기준 재계산(RUN_TOOL)
  2 필수 기준의 부족한 정보 (ASK_USER)
  3 필수 기준의 검증되지 않은 근거 (REQUEST_DOCUMENT)
  4 이미 물었지만 해결되지 않은 정보 (답했으면 REPARSE 한 번, 모름·거부·시도 소진이면 DEFER - 다시 묻지 않음)
  5 (같은 단계 안에서 여러 시험·기준에 영향을 주는 정보가 앞섬: value_score)
  6 선택 조건·영향이 작은 정보 (다른 코호트 전용, 이미 불충족인 시험, 모집 종료 시험), 의료진 검토, 재평가 보류, 검색 필요
  7 종료 (FINALIZE)

value_score(같은 단계 안의 순서에만 사용, 임상 적합성 판단에 쓰지 않음)
  = w_trials × 결과가 바뀔 수 있는 시험 수 + w_required × 필수 기준 수 + w_other × 그 밖의 기준 수
    - penalty_asked × 이미 물은 횟수 - 비용
  '결과가 바뀔 수 있는 시험': 모집 중이고 아직 열린 경로(불충족이 없거나 근거가 상충하는 코호트)가 있는 시험에서
  그 정보가 그 경로의 필수 기준에 쓰일 때(구조적 개수).
  불충족이 확정된 시험(confirmed: 근거가 문서로 검증되고 상충 없음)은 다른 기준을 확인해도 판정이 바뀌지 않으므로 세지 않음.
  근거가 문서로 검증되지 않은 불충족(provisional)은 확정 제외 근거로 쓰지 않고 w_provisional(작은 가중치)로만 세며,
  그 근거 확인(REQUEST_DOCUMENT exclusion_basis_unverified)을 따로 제안. 질문 하나로 불충족이 풀린다고 가정하지 않음.
정보 충분성은 시험·기준별 상태로 판단하며, 하나의 숫자 임계값으로 판단하지 않습니다.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict

from pydantic import BaseModel

from .decision_state import Action, CriterionState, DecisionState


class PolicyConfig(BaseModel):
    w_trials: float = 3.0
    w_required: float = 2.0
    w_other: float = 0.5
    w_provisional: float = 0.5  # 근거가 검증되지 않은 불충족 시험의 기준 (확정 제외도, 열린 시험도 아님)
    penalty_asked: float = 2.0
    cost_ask: float = 1.0
    cost_document: float = 2.0
    cost_tool: float = 0.2
    cost_review: float = 0.5


DEFAULT_POLICY = PolicyConfig()
ACTION_ORDER = {"VERIFY": 0, "NEEDS_REVIEW": 1, "RUN_TOOL": 2, "ASK_USER": 3, "REQUEST_DOCUMENT": 4, "REPARSE": 5,
                "DEFER": 6, "RETRIEVE": 7, "FINALIZE": 8}
VERSIONLESS = {"ASK_USER", "REQUEST_DOCUMENT", "DEFER", "RETRIEVE"}  # 사실이 바뀌어도 같은 대상이면 다시 만들지 않음
ALLOWED_TOOLS = {"deterministic_reevaluation"}  # 진단 모드에서 실행할 수 있는 도구 (LLM·네트워크·임의 코드 없음)


def fingerprint(action_type: str, info_key: str | None, targets: list[str], tool: str | None, version: str) -> str:
    data = [action_type, info_key, sorted(targets), tool, None if action_type in VERSIONLESS else version]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()[:16]


def _on_open_path(st: DecisionState, c: CriterionState) -> bool:
    """이 기준이 아직 열린 경로(불충족 없음 또는 근거 상충)에 속하는가. 공통 기준은 모든 경로에 속함."""
    t = st.trials[c.trial_id]
    return c.cohort is None or "all" in t.open_paths or c.cohort in t.open_paths


def _counts(st: DecisionState, crit: list[CriterionState]) -> tuple[int, int, int, int]:
    """(결과가 바뀔 수 있는 시험 수, 열린 경로의 필수 기준 수, 근거 미검증 불충족 시험의 기준 수, 그 밖의 기준 수)."""
    gain_trials, required, provisional, other = set(), 0, 0, 0
    for c in crit:
        t = st.trials[c.trial_id]
        if not t.open or c.requirement == "waivable":
            other += 1
        elif t.mismatch == "none" and _on_open_path(st, c):
            if c.requirement == "required":
                required += 1
            else:
                other += 1  # 다른 코호트(OR의 다른 갈래) 기준: 선택 조건으로 두되 그 경로가 열려 있으면 결과를 바꿀 수 있음
            if t.eligibility != "ELIGIBLE":
                gain_trials.add(c.trial_id)
        elif t.mismatch == "provisional":
            provisional += 1
        else:
            other += 1  # 불충족이 확정된 시험·닫힌 경로: 질문으로 판정이 바뀌지 않음
    return len(gain_trials), required, provisional, other


class _Builder:
    def __init__(self, st: DecisionState, cfg: PolicyConfig):
        self.st, self.cfg = st, cfg
        self.out: list[Action] = []
        self.done = {a.fingerprint for a in st.action_history}
        self.blocked_duplicates = 0

    def add(self, action_type, tier, reason_code, reason, crit: list[CriterionState] | None = None, *,
            info_key=None, tool=None, cost=0.0, prerequisites=None, trials=None, fields=None):
        crit = crit or []
        targets = sorted({c.key for c in crit})
        gain, required, provisional, other = _counts(self.st, crit) if crit else (0, 0, 0, 0)
        asked = max((c.asked for c in crit), default=0)
        terms = {"trials": self.cfg.w_trials * gain, "required": self.cfg.w_required * required,
                 "provisional": self.cfg.w_provisional * provisional, "other": self.cfg.w_other * other,
                 "asked": -self.cfg.penalty_asked * asked, "cost": -cost}
        score = round(sum(terms.values()), 3)
        fp = fingerprint(action_type, info_key, targets, tool, self.st.fact_version)
        if fp in self.done:
            self.blocked_duplicates += 1
            return
        self.out.append(Action(
            action_id=f"A-{uuid.uuid4().hex[:10]}", action_type=action_type, reason_code=reason_code,
            human_readable_reason=reason, target_trial_ids=sorted(trials or {c.trial_id for c in crit}),
            target_criterion_ids=targets,
            required_fact_fields=sorted(fields if fields is not None else {f for c in crit for f in c.fields}),
            evidence_refs=sorted({r for c in crit for r in c.evidence_refs}), tier=tier,
            priority=tier + ACTION_ORDER[action_type] / 100 + 1 / (2 + max(score, 0)), value_score=score,
            score_terms=terms, prerequisites=prerequisites or [], expected_information_gain=gain,
            estimated_cost=cost, info_key=info_key, tool=tool, fingerprint=fp))


def _executed(st: DecisionState, action_type: str, info_key: str | None) -> list[Action]:
    return [a for a in st.action_history if a.action_type == action_type and a.info_key == info_key]


def candidates(st: DecisionState, cfg: PolicyConfig = DEFAULT_POLICY,
               pending_reevaluation: list[str] | None = None) -> tuple[list[Action], int]:
    """행동 후보(우선순위 순)와 중복으로 막은 후보 수."""
    b = _Builder(st, cfg)
    by_key: dict[str, list[CriterionState]] = defaultdict(list)
    for c in st.criteria():
        by_key[c.info_key].append(c)

    # 1. 사실 변경 뒤 규칙 엔진 기준 재계산 (상태를 먼저 바로잡음)
    if pending_reevaluation:
        crit = [c for c in st.criteria() if c.key in set(pending_reevaluation)]
        b.add("RUN_TOOL", 1, "fact_changed_reevaluate_rule_engine",
              f"사실이 바뀌어 규칙 엔진 기준 {len(pending_reevaluation)}개를 사실 기반으로 다시 계산해야 함",
              crit, tool="deterministic_reevaluation", cost=cfg.cost_tool, info_key="tool:reevaluation")

    for key, crit in sorted(by_key.items()):
        # 1. 상충 사실: 임의로 하나를 고르지 않음. 코드 확인(VERIFY) 뒤에도 남으면 의료진·담당자 검토
        conflicted = [c for c in crit if c.gap == "conflict" or c.conflict_on_decided]
        if conflicted:
            verified = _executed(st, "VERIFY", key)
            if verified and verified[-1].outcome.get("requires_review"):
                b.add("NEEDS_REVIEW", 1, "conflict_requires_review",
                      f"{key}: 상충하는 사실이 있어 자동으로 정할 수 없음 (기준 {len(conflicted)}개)", conflicted,
                      info_key=key, cost=cfg.cost_review, prerequisites=[verified[-1].action_id])
            else:
                code = "conflict_on_decided_criterion" if any(c.conflict_on_decided for c in conflicted) else "fact_conflict"
                b.add("VERIFY", 1, code, f"{key}: 근거 항목의 사실 값이 맞지 않음 - 출처·검증 상태 확인",
                      conflicted, info_key=key, cost=cfg.cost_tool)

        missing = [c for c in crit if c.gap == "missing"]
        if missing:
            answered = [c for c in missing if c.asked and not c.declined]
            if any(c.declined for c in missing) or any(c.acquisition_status == "unresolved" for c in missing):
                b.add("DEFER", 4, "user_declined_or_exhausted",
                      f"{key}: 이미 물었으나 모름·거부 또는 확보 단계를 모두 시도함 - 다시 묻지 않고 보류", missing,
                      info_key=key)
            elif answered:
                if _executed(st, "REPARSE", key):
                    b.add("DEFER", 4, "answered_but_unresolved",
                          f"{key}: 답변 뒤에도 판정에 쓸 정보가 확인되지 않음 - 보류", missing, info_key=key)
                else:
                    b.add("REPARSE", 4, "answered_not_reflected",
                          f"{key}: 답변이 있으나 판정에 반영되지 않음 - 새 입력으로 재추출 필요", missing, info_key=key,
                          cost=cfg.cost_tool)
            elif _executed(st, "ASK_USER", key):
                b.blocked_duplicates += 1  # 이번 루프에서 이미 물음 (같은 정보 반복 질문 방지)
            else:
                gain, required, _, _ = _counts(st, missing)
                tier = 2 if required else 6
                code = "missing_required_info" if required else "missing_optional_info"
                b.add("ASK_USER", tier, code,
                      f"{key}: {'필수' if required else '선택·영향 작은'} 기준 {len(missing)}개(시험 "
                      f"{len({c.trial_id for c in missing})}개)에 필요한 정보가 없음", missing, info_key=key,
                      cost=cfg.cost_ask)

        unverified = [c for c in crit if c.gap == "unverified"]
        if unverified:
            _, required, _, _ = _counts(st, unverified)
            b.add("REQUEST_DOCUMENT", 3 if required else 6,
                  "document_already_requested" if any(c.acquisition_status == "document_requested" for c in unverified)
                  else "unverified_self_report",
                  f"{key}: 진료 기록이 필요한 기준인데 근거가 자기 보고·출처 미상 - 문서(결과지·보고서) 확인 필요",
                  unverified, info_key=key, cost=cfg.cost_document)

    # 6. 의료진 검토(원문에 구체 기준 없음)는 한 행동으로 묶음 (대상 시험·기준은 모두 기록)
    review = [c for c in st.criteria() if c.gap == "physician_review" and st.trials[c.trial_id].open]
    if review:
        b.add("NEEDS_REVIEW", 6, "criterion_underspecified",
              f"원문에 구체 기준이 없는 기준 {len(review)}개(시험 {len({c.trial_id for c in review})}개) - "
              f"담당 의사가 전체 프로토콜과 대조", review, info_key="review:underspecified", cost=cfg.cost_review)
    # 근거가 문서로 검증되지 않은 불충족: 확정 제외 근거로 쓰기 전에 그 근거 확인 (정보 키별로 묶음)
    basis: dict[str, list[CriterionState]] = defaultdict(list)
    by_key_all = {c.key: c for c in st.criteria()}
    for t in st.trials.values():
        if t.mismatch == "provisional" and t.open:
            for k in t.mismatch_basis:
                c = by_key_all[k]
                if c.basis_tier != "verified":
                    basis[c.info_key].append(c)
    for key, crit in sorted(basis.items()):
        b.add("REQUEST_DOCUMENT", 6, "exclusion_basis_unverified",
              f"{key}: 시험 {len({c.trial_id for c in crit})}개의 불충족 판정 근거가 문서로 검증되지 않음 - "
              f"확정 제외 근거로 쓰기 전에 문서 확인", crit, info_key=f"basis:{key}", cost=cfg.cost_document)
    stale = [c for c in st.criteria() if c.gap == "stale"]
    if stale:
        b.add("DEFER", 6, "llm_reevaluation_required",
              f"사실이 바뀌어 LLM 판정 기준 {len(stale)}개를 다시 판정해야 하나 진단 모드에서는 LLM을 부르지 않음",
              stale, info_key="stale:llm")
    if st.not_evaluated_trials:
        b.add("NEEDS_REVIEW", 6, "evaluation_failed",
              f"평가하지 못한 시험 {len(st.not_evaluated_trials)}개 - 부적격이 아니며 재평가·담당자 확인 필요",
              info_key="not_evaluated", trials=set(st.not_evaluated_trials), cost=cfg.cost_review)
    if st.trials and not any(t.open and t.eligibility in ("ELIGIBLE", "UNCERTAIN") for t in st.trials.values()):
        b.add("RETRIEVE", 6, "no_open_candidate",
              "추천 가능한(모집 중·보류 또는 충족) 시험이 없음 - 추가 임상시험 검색 필요 (진단 모드에서는 검색하지 않음)",
              info_key="retrieve", trials=set(st.trials))
    b.add("FINALIZE", 7, "evaluation_step_complete", "이번 평가 단계 종료 (임상시험 참여 가능 여부를 확정하지 않음)",
          info_key="finalize")
    return sorted(b.out, key=lambda a: (a.priority, a.info_key or "")), b.blocked_duplicates


def validate(action: Action, st: DecisionState) -> str | None:
    """안전 규칙·선행 조건 위반 사유 (없으면 None)."""
    by_key = {c.key: c for c in st.criteria()}
    targets = [by_key[k] for k in action.target_criterion_ids if k in by_key]
    if action.action_type == "ASK_USER" and any(c.gap != "missing" for c in targets):
        return "target_not_missing"  # 이미 해결됐거나 질문으로 해결할 수 없는 정보
    if action.action_type == "ASK_USER" and any(c.declined for c in targets):
        return "user_declined"
    if action.action_type == "RUN_TOOL" and action.tool not in ALLOWED_TOOLS:
        return "tool_not_allowed"
    done = {a.action_id for a in st.action_history}
    if any(p not in done for p in action.prerequisites):
        return "prerequisite_missing"
    return None
