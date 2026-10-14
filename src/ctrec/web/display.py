"""웹 결과 화면용 표시 정보 (표시 계층, Phase 6-2).

판정·점수·추천 순서를 바꾸지 않습니다. 서버의 판정 함수(matcher의 분류 함수, recommender의 모집·데모 판단)로
기준과 제외 사유를 다시 분류하기만 하고, 사용자에게 보일 설명은 이 구조화된 값으로 화면이 만듭니다.
추천 결과에 이미 있는 서버 문구(내부 표기가 섞일 수 있음)는 지우거나 고치지 않고 화면이 '시스템 원문'으로 접어서 보존합니다.

평가 오류 원문(evaluation_error, 예외 repr - 요청 내용이 섞일 수 있음)은 여기에 넣지 않고 오류 종류만 넣습니다.
"""

from __future__ import annotations

import re

from .. import config
from ..agents import matcher
from ..agents.recommender import is_demo, is_open, status_of

# 기준 분류 (우선순위 순, 서버 판정 함수 기준)
MET, NOT_MET = "met", "not_met"
MISSING = "missing"  # 확인 질문으로 해소를 시도할 미확인 (matcher.is_askable_unknown)
RECORD_CHECK = "record_check"  # 답변은 있으나 진료 기록 확인 필요 (matcher.needs_record_check, UNVERIFIED)
PHYSICIAN = "physician_review"  # 원문에 구체 기준이 없어 의료진 확인 (matcher.needs_physician_review)
PROCEDURAL = "procedural"  # 판정을 막지 않는 절차·행정 기준 (matcher.is_waivable_logistics)
UNCLASSIFIED = "unclassified"  # 위에 해당하지 않는 판정 불가 (분류할 수 없음 -> 화면은 중립 안내)


def group_of(a) -> str:
    if a.passes is True:
        return MET
    if a.passes is False:
        return NOT_MET
    if matcher.needs_record_check(a):
        return RECORD_CHECK
    if matcher.needs_physician_review(a):
        return PHYSICIAN
    if matcher.is_askable_unknown(a):
        return MISSING
    if matcher.is_waivable_logistics(a):
        return PROCEDURAL
    return UNCLASSIFIED


def exclusion_kind(m, trial: dict | None, recommended: set[str]) -> str | None:
    """추천 목록에 없는 시험의 제외 종류 (recommender.recommend의 제외 목록과 같은 순서로 판단)."""
    if m.trial_id in recommended:
        return None
    if trial is None:
        return "unknown"
    if m.eligibility == "NOT_EVALUATED":
        return "not_evaluated"
    if not is_open(trial):
        return "closed"
    if is_demo(trial) and not config.DEMO_MODE:
        return "demo"
    if m.eligibility == "INELIGIBLE":
        return "mismatch"
    return "unknown"


def _said(a) -> str:
    """UNVERIFIED 항목에서 환자 답변이 가리킨 값 (recommender.verification_notes와 같은 해석)."""
    if not a.self_reported_applicable:
        return "not_applicable"
    return {"yes": "yes", "no": "no"}.get(a.self_reported_holds or "", "unclear")


_EXC_NAME = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\(")


def _error_type(err: str | None) -> str:
    """오류 원문에서 예외 클래스 이름만 (예: '적격성 판정 실패: RuntimeError(...)' -> 'RuntimeError'). 메시지는 버림."""
    m = _EXC_NAME.search(err or "")
    if m:
        return m.group(1).rsplit(".", 1)[-1]
    head = (err or "").strip()
    return head if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", head) else "Error"


def redact_errors(result: dict) -> dict:
    """화면으로 보내는 결과 사본에서 평가 오류 원문(예외 repr - 요청 내용이 섞일 수 있음)을 오류 종류로 바꿈.

    판정(eligibility·match_status·기준별 판정)은 그대로. 서버 Session의 값은 바꾸지 않음 (사본만)."""
    out = dict(result)
    matches = {}
    failed = {}
    for tid, m in (result.get("matches") or {}).items():
        if m.get("eligibility") == "NOT_EVALUATED" and m.get("evaluation_error"):
            kind = _error_type(m["evaluation_error"])
            failed[tid] = kind
            m = {**m, "evaluation_error": kind, "summary": f"NOT_EVALUATED: 평가 실패로 판정하지 못함(부적격 아님) - {kind}"}
        matches[tid] = m
    out["matches"] = matches
    # 단계별 실패 기록(repr)도 종류만
    out["errors"] = [{"item": e.get("item"), "error": _error_type(e.get("error"))} for e in result.get("errors") or []]
    rec = result.get("recommendation")
    if rec and failed:
        rec = dict(rec)
        rec["excluded"] = [{**x, "reason": f"평가 실패로 적격 여부를 판정하지 못함(부적격 아님) - 재평가 또는 담당자 확인 필요. 원인: {failed[x['trial_id']]}"}
                           if x.get("trial_id") in failed else x for x in rec.get("excluded") or []]
        out["recommendation"] = rec
    return out


def build(session) -> dict:
    rec = session.recommendation
    ranked = {r.trial_id for r in (rec.ranked if rec else [])}
    qa_log = list(session.qa_log or [])
    out: dict = {"trials": {}}
    for tid, m in session.matches.items():
        trial = session.trials.get(tid)
        groups = {a.rule_id: group_of(a) for a in m.assessments}
        counts: dict[str, int] = {}
        for g in groups.values():
            counts[g] = counts.get(g, 0) + 1
        record_checks = [{"rule_id": a.rule_id, "kind": a.kind, "criterion_text": a.criterion_text, "said": _said(a),
                          "evidence": a.evidence} for a in m.assessments if groups[a.rule_id] == RECORD_CHECK]
        physician = []
        for a in m.assessments:
            if groups[a.rule_id] != PHYSICIAN:
                continue
            key = f"{tid}:{a.rule_id}"
            related = [q for q in qa_log if key in (q.target or [])]
            if not related:  # recommender.physician_notes와 같은 대체 규칙: 같은 시험의 의사 참고용 문답
                related = [q for q in qa_log if q.purpose == "physician_reference"
                           and any(t.split(":")[0] == tid for t in (q.target or []))]
            physician.append({"rule_id": a.rule_id, "kind": a.kind, "criterion_text": a.criterion_text,
                              "related_qa": [{"question": q.question, "answer": q.answer} for q in related]})
        out["trials"][tid] = {
            "groups": groups, "counts": counts,
            "record_checks": record_checks, "physician_items": physician,
            "exclusion": exclusion_kind(m, trial, ranked),
            "recruitment_status": status_of(trial) if trial else None,
            "error_type": _error_type(m.evaluation_error) if m.eligibility == "NOT_EVALUATED" else None,
        }
    # 추천이 없을 때: 제외 종류별 시험 수 (no_recommendation_reason 원문 대신 화면이 설명을 만듦)
    if rec is not None and not rec.ranked:
        kinds: dict[str, list[str]] = {}
        for tid, t in out["trials"].items():
            kinds.setdefault(t["exclusion"] or "unknown", []).append(tid)
        out["no_recommendation"] = kinds
    return out
