"""사실 기반 비교 Matcher와 기준 단위 차이 (Phase 1-3B/1-4). 진단용이며 실제 판정·추천을 바꾸지 않습니다.

기존 Matcher 결과(기준선)는 그대로 두고, 같은 고정 기준표(Session이 이미 불러온 ParsedTrial)로 사실 기반 입력을 따로
평가해 시험·기준 단위로 비교합니다. 기준표를 다시 읽거나 컴파일하지 않고, 검수 패치도 적용하지 않습니다.

모드
- deterministic (기본): 규칙 엔진으로 판정되는 기준(나이·성별·ECOG·검사)과 의사 확인 표시 기준만 사실 기반으로
  다시 평가. LLM을 부르지 않으므로 나머지 기준은 not_compared이고 시험 단위 판정은 만들지 않음.
- llm (명시적 선택): 기존 Matcher(match_trial)를 사실 기반 프로필로 한 번 더 실행해 모든 기준과 시험 단위 판정을 비교.
  유료 API를 부르므로 기본값이 아님.
사실 기반 판정은 모두 fact_view.gate를 거칩니다(출처·검증·충돌·기간 확인, 확정 -> 판정 불가 방향으로만).

기록하는 변화 (기준 단위: MET / NOT_MET / UNKNOWN)
same, met_to_unknown, not_met_to_unknown, unknown_to_met, unknown_to_not_met, not_met_to_met, met_to_not_met, not_compared
원인(cause): gate의 이유 코드(self_report_only, unknown_source, declared_unverified, conflict, observation_window,
biomarker_unresolved, ungrounded) 또는 input_difference(입력 자체가 달라짐), not_evaluated(평가할 수 없음).
더 긍정적인 방향(unknown_to_met, not_met_to_met, 시험 단위 등급 상승)은 자동으로 받아들이지 않고 risks에 따로 모읍니다.
출력에는 시험·기준 ID, 상태, 이유 코드만 넣고 환자 값·원문은 넣지 않습니다.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from .agents import matcher
from .fact_store import FactStore
from .fact_view import FactEvidenceView, _engine_field, gate
from .schemas import CriterionAssessment, ParsedTrial, TrialMatch
from .tools import rule_engine

Mode = Literal["deterministic", "llm"]
_RANK = {"INELIGIBLE": 0, "UNCERTAIN": 1, "ELIGIBLE": 2}
RISKY = {"unknown_to_met", "not_met_to_met"}


def state(a: CriterionAssessment | None) -> str | None:
    if a is None:
        return None
    return "MET" if a.passes is True else "NOT_MET" if a.passes is False else "UNKNOWN"


def _change(base: str, new: str | None) -> str:
    if new is None:
        return "not_compared"
    if base == new:
        return "same"
    return f"{base.lower()}_to_{new.lower()}"


def _conflict_unknown(rule, field_state: str) -> CriterionAssessment:
    return CriterionAssessment(
        rule_id=rule.rule_id, kind=rule.kind, criterion_text=rule.text, holds="unknown", passes=None,
        confidence="low", evidence="근거 없음", reasoning=f"[사실 근거 확인: 근거 항목의 사실 값이 맞지 않음({field_state})]",
        missing_info="상충하는 정보 확인 필요", method="rule_engine", category=rule.category,
        underspecified=rule.underspecified, cohort=rule.cohort)


def _rebuild(patient_id: str, parsed: ParsedTrial, all_assessments: list[CriterionAssessment]) -> TrialMatch:
    """기존 Matcher와 같은 코호트 선택·결정 규칙으로 시험 단위 판정을 다시 계산."""
    assessments, cohort, cohort_results = matcher._select_cohort(all_assessments, parsed.cohorts)
    eligibility = matcher.decide(assessments)
    return TrialMatch(
        patient_id=patient_id, trial_id=parsed.trial_id, eligibility=eligibility,  # type: ignore[arg-type]
        assessments=assessments, n_pass=sum(a.passes is True for a in assessments),
        n_fail=sum(a.passes is False for a in assessments), n_unknown=sum(a.passes is None for a in assessments),
        summary=f"{eligibility} (사실 기반 비교)", matched_cohort=cohort, cohort_results=cohort_results,
        other_cohort_assessments=[a for a in all_assessments if a not in assessments],
    )


def evaluate_trial(view: FactEvidenceView, parsed: ParsedTrial, trial: dict, reference_date: str, mode: Mode = "deterministic",
                   require_document_verification: bool = False
                   ) -> tuple[dict[str, CriterionAssessment | None], dict[str, str], TrialMatch | None]:
    """사실 기반 평가. 반환: (rule_id -> 판정 또는 None(이 모드에서 평가하지 않음), rule_id -> gate 이유, 시험 판정 또는 None)."""
    profile = view.to_profile()
    by_id = {r.rule_id: r for r in parsed.rules}
    raw: dict[str, CriterionAssessment | None] = {}
    if mode == "llm":
        m = matcher.match_trial(profile, parsed, trial, reference_date=reference_date, source_text=None)
        for a in m.assessments + m.other_cohort_assessments:
            raw[a.rule_id] = a
    else:
        for rule in parsed.rules:
            review = matcher._meta(parsed, rule).review
            raw[rule.rule_id] = (matcher._review_assessment(rule, review) if review
                                 else rule_engine.evaluate(rule, profile))
    out: dict[str, CriterionAssessment | None] = {}
    reasons: dict[str, str] = {}
    excluded = view.excluded()
    for rid, a in raw.items():
        rule = by_id[rid]
        field = _engine_field(rule)
        if field in excluded and (a is None or a.passes is None):
            # 사실 값이 맞지 않아 프로필에서 뺀 항목의 규칙 엔진 기준: 비교 대상에서 빠지지 않게 판정 불가로 명시
            out[rid] = _conflict_unknown(rule, excluded[field])
            reasons[rid] = "conflict"
            continue
        if a is None:
            out[rid] = None
            continue
        g, why = gate(a, rule, matcher._meta(parsed, rule), view, reference_date, require_document_verification)
        out[rid] = g
        if why:
            reasons[rid] = why
    fact_match = None
    if mode == "llm":
        fact_match = _rebuild(view.patient_id, parsed, [out[r.rule_id] for r in parsed.rules if out.get(r.rule_id)])
    return out, reasons, fact_match


def compare_trial(base: TrialMatch, parsed: ParsedTrial | None, fact: dict[str, CriterionAssessment | None] | None,
                  reasons: dict[str, str], fact_match: TrialMatch | None) -> dict:
    """시험 하나의 기준선 vs 사실 기반 비교 (ID·상태·이유 코드만)."""
    if base.eligibility == "NOT_EVALUATED" or parsed is None or fact is None:
        return {"trial_id": base.trial_id, "status": "not_evaluated",
                "baseline": {"eligibility": base.eligibility, "match_status": base.match_status},
                "cause": "not_evaluated", "criteria": []}
    base_all = {a.rule_id: a for a in base.assessments + base.other_cohort_assessments}
    criteria = []
    for rule in parsed.rules:
        b = state(base_all.get(rule.rule_id))
        f = fact.get(rule.rule_id)
        if b is None:
            continue
        change = _change(b, state(f))
        cause = reasons.get(rule.rule_id)
        if cause is None and change not in ("same", "not_compared"):
            cause = "input_difference"
        criteria.append({"rule_id": rule.rule_id, "cohort": rule.cohort, "baseline": b, "fact": state(f),
                         "change": change, "cause": cause, "risk": change in RISKY})
    out = {"trial_id": base.trial_id, "status": "compared", "criteria": criteria,
           "baseline": {"eligibility": base.eligibility, "match_status": base.match_status}}
    if fact_match is not None:
        up = _RANK.get(fact_match.eligibility, -1) > _RANK.get(base.eligibility, -1)
        out["fact"] = {"eligibility": fact_match.eligibility, "match_status": fact_match.match_status}
        out["trial_change"] = ("same" if fact_match.eligibility == base.eligibility
                               else f"{base.eligibility.lower()}_to_{fact_match.eligibility.lower()}")
        out["trial_risk"] = up
    return out


def compare_session(store: FactStore, patient_id: str, matches: dict[str, TrialMatch], parsed: dict[str, ParsedTrial],
                    trials: dict[str, dict], reference_date: str, mode: Mode = "deterministic",
                    require_document_verification: bool = False) -> dict:
    """Session의 기준선 판정 전체를 사실 기반으로 비교. 기준선 객체는 읽기만 함."""
    view = FactEvidenceView(store, patient_id)
    results = []
    for tid, base in matches.items():
        p = parsed.get(tid)
        if base.eligibility == "NOT_EVALUATED" or p is None:
            results.append(compare_trial(base, None, None, {}, None))
            continue
        fact, reasons, fact_match = evaluate_trial(view, p, trials[tid], reference_date, mode,
                                                   require_document_verification)
        results.append(compare_trial(base, p, fact, reasons, fact_match))
    changes = Counter(c["change"] for t in results for c in t["criteria"])
    causes = Counter(c["cause"] for t in results for c in t["criteria"] if c["cause"])
    risks = [{"trial_id": t["trial_id"], "rule_id": c["rule_id"], "change": c["change"], "cause": c["cause"]}
             for t in results for c in t["criteria"] if c["risk"]]
    risks += [{"trial_id": t["trial_id"], "rule_id": None, "change": t["trial_change"], "cause": "trial_level"}
              for t in results if t.get("trial_risk")]
    return {"mode": mode, "require_document_verification": require_document_verification,
            "excluded_fields": sorted(view.excluded()), "trials": results,
            "summary": {"changes": dict(changes), "causes": dict(causes),
                        "not_evaluated_trials": sum(t["status"] == "not_evaluated" for t in results)},
            "risks": risks}
