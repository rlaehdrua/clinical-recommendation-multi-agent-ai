"""Q&A 안전 경계 (재설계 P1): 계약 없는 확인 답변은 확정 판정의 근거가 될 수 없음.

matcher.match_trial의 결과를 받아, 확정 판정(yes/no, 해당 없음)의 근거가 확인 질문 답변(자기 보고)이면 판정 불가로
되돌립니다. 판정을 올리지 않으며, 통과·제외 양쪽 모두에 같은 규칙을 적용합니다(잘못된 통과와 잘못된 제외 모두 방지).

이 경계는 '근거가 어디서 왔는가(출처)'만 봅니다. 질문이 기준의 명제를 제대로 물었는지(의미)는 검증하지 않으며,
의미 검증은 질문 계약·명제 키·평가기(question_contract, evaluator)의 몫입니다. 즉 보수적 안전 경계이지 의미 검증 수단이 아님.

출처 판별 (인용 조각 단위, matcher._enforce_quote와 같은 조각 규칙)
- 구조 모드 (initial_text·qa_log를 받음, ContractSession): 처음 입력 문자열과 qa_log의 질문·답 문자열에서 직접 찾음.
  원문을 줄 단위로 해석하지 않으므로 처음 입력에 'Q:/A:' 줄이나 머리말이 있어도, 답변에 줄바꿈이 있어도 출처가 바뀌지 않음.
- 머리말 모드 (source_text만, 기존 Session): evidence.split_source의 '[추가 확인 정보' 머리말 앞뒤로 나눔 (보수적 휴리스틱)
판정
- 어떤 조각이 답변 쪽에만 있음 -> 답변에 기댐 (기록과 답변에 걸친 인용 포함)
- 조각이 처음 입력에 있음(답변에도 같은 문구가 있어도) -> 기록이 뒷받침하므로 그대로
- 찾을 조각이 없음(나이·성별·ECOG 같은 프로필 필드) -> matcher가 계산한 evidence_source가 self_report면 답변에 기댐
제외: 결정적 평가기(method="contract")의 판정은 계약 사실로 내린 것이므로 이 경계 대상이 아님.

되돌린 항목은 기존 UNVERIFIED 상태를 그대로 씁니다(needs_verification=True, self_reported_holds에 답변상 판정 보존):
같은 질문을 LLM이 다시 만들지 않고(matcher.is_askable_unknown), 화면에는 '답변상 충족/미충족, 확인 필요'로 표시
(status.external_status). 해소 경로는 검증된 질문 계약(ContractSession) 또는 진료 기록 확인입니다.
"""

from __future__ import annotations

import os
import re

from . import evidence
from .agents import matcher
from .schemas import CriterionAssessment, ParsedTrial, QAPair, TrialMatch

FLAG = "qa_uncontracted"
POLICY_ENV = "CTREC_QA_POLICY"


def policy() -> str:
    """기존 Session 경로(matcher.match_trial)에 적용할 Q&A 판정 정책 (환경 변수 CTREC_QA_POLICY).

    - legacy (기본, 값 없음): 현재 서비스 정책. 확인 답변을 인용한 확정은 기존 검사(인용·E9 직접성·근거 수준)만 거침
    - contract: 계약 없는 확인 답변은 확정 근거가 아님 (이 모듈의 경계를 matcher 끝에서 적용, 머리말 모드)
    그 밖의 값은 contract로 봄 (철자 오류로 경계가 꺼지지 않도록, 엄격한 쪽).
    ContractSession은 이 값과 관계없이 항상 경계를 적용합니다(구조 모드). 이 값은 서비스 기본 정책을 바꾸지 않습니다.
    """
    v = os.getenv(POLICY_ENV, "").strip().lower()
    return "legacy" if v in ("", "legacy") else "contract"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _fragments(quote: str) -> list[str]:
    out = []
    for frag in matcher._QUOTE_SPLIT.split(quote):
        frag = _norm(frag).strip(" .,:'`()[]")
        if len(frag.replace(" ", "")) >= matcher._MIN_QUOTE_CHARS:
            out.append(frag)
    return out


def _quote(a: CriterionAssessment) -> str:
    if a.method == "rule_engine":  # 검사 수치: '... - "인용"' 형식. 나이·성별·ECOG는 인용 없음
        return a.evidence.split(' - "', 1)[-1].rstrip('"') if ' - "' in a.evidence else ""
    return a.evidence


def qa_dependent(a: CriterionAssessment, source_text: str | None = None, *, initial_text: str | None = None,
                 qa_log: list[QAPair] | None = None) -> bool:
    """이 확정 판정이 확인 질문 답변에 기대는가. initial_text를 주면 구조 모드, 아니면 머리말 모드."""
    if a.passes is None or a.method == "contract":
        return False
    if initial_text is not None:
        record = _norm(initial_text)
        answers = [_norm(t) for p in (qa_log or []) for t in (p.question, p.answer, f"q: {p.question} a: {p.answer}")]
    elif source_text is not None:
        initial, rest = evidence.split_source(source_text)
        record, answers = _norm(initial), [_norm(rest)] if rest else []
    else:
        return a.evidence_source == "self_report"
    located = False
    for f in _fragments(_quote(a)):
        in_record = f in record
        if not in_record and any(f in t for t in answers):
            return True
        located = located or in_record
    return False if located else a.evidence_source == "self_report"


def downgrade(a: CriterionAssessment) -> CriterionAssessment:
    said = "해당 없음" if not a.applicable else {"yes": "해당", "no": "해당하지 않음"}.get(a.holds, a.holds)
    return a.model_copy(update={
        "holds": "unknown", "passes": None, "applicable": True, "confidence": "low",
        "needs_verification": True, "self_reported_holds": a.holds, "self_reported_applicable": a.applicable,
        "evidence_source": "self_report",
        "validation_flags": [*a.validation_flags, FLAG],
        "reasoning": f"[Q&A 안전 경계: 계약 없는 확인 답변은 확정 근거가 아님 - 답변상 '{said}'] {a.reasoning}",
        "missing_info": "검증된 질문 계약(명제·기간이 고정된 질문) 또는 진료 기록으로 확인 필요",
    })


def guard_assessments(assessments: list[CriterionAssessment], source_text: str | None, *,
                      initial_text: str | None = None, qa_log: list[QAPair] | None = None) -> list[CriterionAssessment]:
    return [downgrade(a) if qa_dependent(a, source_text, initial_text=initial_text, qa_log=qa_log) else a
            for a in assessments]


def recompose(match: TrialMatch, parsed: ParsedTrial, replaced: dict[str, CriterionAssessment]) -> TrialMatch:
    """기준별 판정 일부를 바꾼 뒤 시험 적격성·코호트·요약을 matcher와 같은 규칙으로 다시 계산."""
    if match.eligibility == "NOT_EVALUATED" or not replaced:
        return match
    current = {a.rule_id: a for a in match.assessments + match.other_cohort_assessments}
    order = [r.rule_id for r in parsed.rules if r.rule_id in current] + \
            [rid for rid in current if rid not in {r.rule_id for r in parsed.rules}]
    all_assessments = [replaced.get(rid, current[rid]) for rid in order]
    assessments, cohort, cohort_results = matcher._select_cohort(all_assessments, parsed.cohorts)
    other = [a for a in all_assessments if a not in assessments]
    eligibility = matcher.decide(assessments)
    n_pass = sum(a.passes is True for a in assessments)
    n_fail = sum(a.passes is False for a in assessments)
    n_unknown = sum(a.passes is None for a in assessments)
    failed = [f"{a.rule_id}({a.criterion_text[:60]})" for a in assessments if a.passes is False]
    summary = f"{eligibility}: 통과 {n_pass} / 불충족 {n_fail} / 판정불가 {sum(map(matcher.is_askable_unknown, assessments))}"
    for n, label in ((sum(map(matcher.needs_physician_review, assessments)), "의사 확인 필요"),
                     (sum(map(matcher.is_waivable_logistics, assessments)), "절차 기준 확인 필요"),
                     (sum(map(matcher.needs_record_check, assessments)), "UNVERIFIED·문서 확인 필요")):
        if n:
            summary += f" (+{label} {n})"
    if cohort:
        summary += f" [코호트: {cohort}]"
    if failed:
        summary += " | 불충족: " + "; ".join(failed[:3])
    # match_status는 비워 두면 TrialMatch 검증기가 바뀐 판정으로 다시 계산 (status.external_status)
    return TrialMatch(**{**{k: getattr(match, k) for k in TrialMatch.model_fields},
                         "eligibility": eligibility, "assessments": assessments, "other_cohort_assessments": other,
                         "matched_cohort": cohort, "cohort_results": cohort_results, "n_pass": n_pass,
                         "n_fail": n_fail, "n_unknown": n_unknown, "summary": summary, "match_status": None})


def guard_match(match: TrialMatch, parsed: ParsedTrial, source_text: str | None, *,
                initial_text: str | None = None, qa_log: list[QAPair] | None = None) -> TrialMatch:
    """계약 없는 답변에 기댄 확정 판정을 되돌리고 시험 판정을 다시 계산."""
    if match.eligibility == "NOT_EVALUATED":
        return match
    replaced = {a.rule_id: downgrade(a) for a in match.assessments + match.other_cohort_assessments
                if qa_dependent(a, source_text, initial_text=initial_text, qa_log=qa_log)}
    return recompose(match, parsed, replaced)
