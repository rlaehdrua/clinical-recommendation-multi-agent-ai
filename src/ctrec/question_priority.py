"""질문 우선순위 동점 처리 정책 후보 (Phase 4-2, 평가 전용). Phase 3 Decision Policy는 바꾸지 않습니다.

모든 정책은 같은 Decision Policy 후보(decision_policy.candidates)와 같은 순서 기준 (단계 tier, 가치 점수 value_score)을 쓰고,
**같은 단계·같은 점수인 동점만** 다르게 정합니다. 어느 정책도 정답(숨긴 사실·숨긴 기준 ID)을 보지 않으며,
입력은 Session 사본(Decision State, 기준표)뿐입니다. 정책 요소와 표는 평가 전에 정해 둔 고정값이며 평가셋으로 조정하지 않습니다.

A current      Decision Loop 그대로 (동점: 정보 키 문자열 순서 -> criterion:T:E10 < criterion:T:E2 < criterion:T:I1)
B protocol     동점: 후보 시험 순서 -> 기준표의 규칙 순서 (파서 순서, 보통 선정 기준이 먼저)
C natural      동점: 정보 키의 자연 숫자 순서 (E2 < E10, 그러나 문자 E < I는 그대로)
D kind_balance 동점 묶음 안에서 프로토콜 순서로 선정·제외 기준을 번갈아 (선정부터)
E info_value   동점: 미리 정한 정보 가치 요소 순서
               1) 규칙 엔진이 결정적으로 판정할 수 있는 구조화 기준 먼저 (답으로 바로 판정 가능)
               2) 자기 보고로 충분한 기준 먼저 (진료 기록 필요 기준은 답을 받아도 UNVERIFIED)
               3) 질문 부담이 낮은 유형 먼저 (BURDEN 표)
               4) 프로토콜 순서
"""

from __future__ import annotations

import re

from .acquisition import is_open as acquisition_open
from .agents import matcher
from .decision_policy import candidates
from .decision_state import SessionView, build_state
from .fact_view import _engine_field
from .schemas import ClarifyingQuestion, QuestionSet

POLICIES = ("A_current", "B_protocol", "C_natural", "D_kind_balance", "E_info_value")
# 질문 부담 (낮을수록 먼저): 환자가 바로 답하기 쉬운 정보 -> 결과지·기록이 필요한 정보 -> 해석이 필요한 정보
BURDEN = {"demographic": 0, "performance_status": 0, "reproductive": 1, "lab": 1, "biomarker": 1, "medication": 2,
          "treatment_history": 2, "diagnosis": 2, "disease_stage": 2, "comorbidity": 3, "procedure": 3, "other": 4,
          "consent_or_logistics": 5}


def _natural(s: str) -> list:
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s or "")]


def _protocol_index(session) -> dict[str, tuple[int, int]]:
    order = {}
    for ti, tid in enumerate(session.case.candidate_trials or list(session.trials)):
        for ri, r in enumerate(session.parsed[tid].rules if tid in session.parsed else []):
            order[f"{tid}:{r.rule_id}"] = (ti, ri)
    return order


def _rules(session, keys):
    out = []
    for k in keys:
        tid, rid = k.split(":", 1)
        p = session.parsed.get(tid)
        r = next((x for x in p.rules if x.rule_id == rid), None) if p else None
        if r is not None:
            out.append((p, r))
    return out


def order_asks(asks: list, session, policy: str) -> list:
    """같은 (단계, 가치 점수) 안의 동점만 정책에 따라 정렬. 단계·점수 순서는 모든 정책이 같음."""
    proto = _protocol_index(session)

    def pidx(a):
        return min((proto.get(k, (99, 999)) for k in a.target_criterion_ids), default=(99, 999))

    def kind(a):
        rs = _rules(session, a.target_criterion_ids[:1])
        return rs[0][1].kind if rs else "inclusion"

    def info_value(a):
        rs = _rules(session, a.target_criterion_ids)
        if not rs:
            return (9, 9, 9, pidx(a))
        structured = min(0 if (_engine_field(r) and r.structured_evaluable and r.operator) else 1 for _, r in rs)
        record = min(0 if matcher._meta(p, r).required_evidence == "self_report_ok" else 1 for p, r in rs)
        burden = min(BURDEN.get(r.category, 4) for _, r in rs)
        return (structured, record, burden, pidx(a))

    base = sorted(asks, key=lambda a: (a.tier, -a.value_score))
    if policy == "A_current":
        return sorted(asks, key=lambda a: (a.priority, a.info_key or ""))  # decision_policy.candidates와 같은 순서
    if policy == "B_protocol":
        return sorted(base, key=lambda a: (a.tier, -a.value_score, pidx(a)))
    if policy == "C_natural":
        return sorted(base, key=lambda a: (a.tier, -a.value_score, _natural(a.info_key)))
    if policy == "E_info_value":
        return sorted(base, key=lambda a: (a.tier, -a.value_score, info_value(a)))
    if policy == "D_kind_balance":
        out, i = [], 0
        while i < len(base):
            j = i
            while j < len(base) and (base[j].tier, base[j].value_score) == (base[i].tier, base[i].value_score):
                j += 1
            group = sorted(base[i:j], key=pidx)
            inc = [a for a in group if kind(a) == "inclusion"]
            exc = [a for a in group if kind(a) != "inclusion"]
            while inc or exc:  # 선정부터 번갈아
                if inc:
                    out.append(inc.pop(0))
                if exc:
                    out.append(exc.pop(0))
            i = j
        return out
    raise ValueError(f"알 수 없는 정책 {policy}")


def generator_factory(policy: str):
    """question_eval.run_method용 생성기 공장: 같은 Session 상태의 Decision Policy 후보를 정책 순서로 질문으로."""
    def factory(session_ref: dict):
        def generate(profile, matches, qa_log, n, acquisition_plan=None) -> QuestionSet:
            s = session_ref["session"]
            view = SessionView.from_session(s)
            view.matched_versions = {t: view.snapshot.version for t in s.matches}
            asks = [a for a in candidates(build_state(view))[0] if a.action_type == "ASK_USER"]
            asked = {k for q in qa_log for k in q.target}
            qs = []
            for a in order_asks(asks, s, policy):
                keys = [k for k in a.target_criterion_ids if k not in asked and acquisition_open(s.missing_facts, k)]
                if not keys:
                    continue
                qs.append(ClarifyingQuestion(question_id=f"Q{len(qs) + 1}", question=f"[{policy}] {a.info_key}",
                                             answer_type="free_text", target=keys, purpose="eligibility",
                                             why=f"policy:{policy}"))
                asked |= set(keys)
                if len(qs) >= n:
                    break
            return QuestionSet(questions=qs)
        return generate
    return factory
