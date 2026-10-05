"""추론·매칭 에이전트: 규칙별 충족 여부를 판정하고 시험 단위 적격성을 결정.

1) 규칙 엔진(tools/rule_engine.py)으로 기계적으로 판정 가능한 규칙을 먼저 처리
2) 나머지 규칙은 Claude가 환자 정보를 근거로 추론 (근거 인용 필수)
3) 시험 단위 적격성은 아래 결정 규칙으로 계산 (LLM이 아닌 코드가 최종 결정)
   - 선정 기준 불충족 또는 제외 기준 해당이 하나라도 있으면 INELIGIBLE
   - 그렇지 않고 판정 불가(unknown)가 하나라도 있으면 UNCERTAIN
     (예외: 동의·방문 가능 여부 같은 절차·행정 '선정' 기준의 unknown만 적격성을 막지 않고 '절차 확인 필요'로 남김.
      원문에 구체 기준이 없는 underspecified 기준의 unknown은 '의사 확인 필요'로 분류하되 ELIGIBLE을 막음 -
      파서 LLM의 분류 하나로 미확인 기준이 통과 처리되지 않도록)
   - 모두 통과하면 ELIGIBLE
"""

from __future__ import annotations

import json
import re

from .. import config, llm
from ..schemas import CriterionAssessment, LLMAssessment, MatcherOutput, ParsedTrial, PatientProfile, TrialMatch
from ..tools import rule_engine
from .criteria_parser import rules_json

SYSTEM = """당신은 임상시험 적격성을 판정하는 '추론·매칭 에이전트'입니다.
구조화된 환자 정보와 임상시험 규칙 목록을 받아, 각 규칙에 기술된 조건이 이 환자에게 성립하는지(holds)를 판정합니다.

판정 원칙:
- holds="yes": 환자 정보에 조건이 성립한다는 명시적 근거가 있음
- holds="no": 환자 정보에 조건이 성립하지 않는다는 명시적 근거가 있음
- holds="unknown": 근거가 없거나 불충분함. 이때 missing_info에 판정에 필요한 정보를 구체적으로 적습니다.
- 제외 기준 규칙은 '제외 사유가 되는 상태'를 서술합니다. 환자가 그 상태에 해당하면 holds="yes"입니다.
- evidence_type을 정직하게 표시합니다. 기록에 "언급이 없다"는 것은 absent입니다(inferred가 아님). absent이면 holds는 "unknown"입니다.
- 정보가 없다는 사실만으로 "no"라고 판단하지 않습니다. 예: 뇌전이 언급이 없음 -> "unknown" (단, 영상검사에서 뇌전이 없음이 명시되면 "no").
- yes/no 확정은 환자 기록에 명시된 근거(explicit)가 있을 때만 합니다. 나이·성별 등으로부터의 추정이나 "언급이 없으니 아닐 것"은
  evidence_type="inferred" 또는 "absent"로 표시하고 holds="unknown"으로 두며, 추정 내용은 reasoning에, 확인할 사항은 missing_info에 적습니다.
  (예: 62세 여성의 임신 여부 -> 폐경 여부가 명시되지 않았으면 unknown, missing_info="폐경 여부 또는 임신·수유 여부")
- 날짜 조건(예: "28일 이내 검사")은 기준일과 검사일을 비교해 판단합니다. 검사일이 없으면 unknown입니다.
- 단위가 다르면 표준적인 환산을 적용하고 reasoning에 환산식을 적습니다.
- 'consent_or_logistics' 규칙(동의 가능, 방문 가능 등)은 반대 근거가 없으면 unknown으로 두고 missing_info에 확인 필요 사항을 적습니다.
- evidence에는 환자 정보의 source_quote(환자 원문)를 번역·요약 없이 그대로 인용하고, 여러 개면 " / "로 구분합니다.
  단, 나이·성별·ECOG가 근거이면 "sex: male", "age: 62", "ecog: 1"처럼 프로필 필드로 적습니다.
  "medications: []"처럼 빈 목록을 적거나 "언급 없음"을 근거로 삼지 않습니다. 코드는 원문에서 찾을 수 없는 인용(위 필드 제외)을 근거로 인정하지 않습니다.
- underspecified=true 규칙(원문에 약물 목록·기준값이 없는 기준)은, 환자가 명백히 해당할 때만 "yes"로 판정하고 그 외에는 "unknown"으로 둡니다. missing_info에는 의사가 전체 프로토콜과 대조할 때 필요한 환자 정보(예: 현재 복용 약물 전체 목록)를 적습니다.
- 조건부 기준(예: "Men who can father a child: must use contraception", "Women of childbearing potential must have a negative pregnancy test")에서
  환자가 그 대상 집단에 속하지 않으면(예: 여성 환자에게 남성 대상 조건, 폐경이 명시된 환자에게 가임 여성 조건) applicable=false로 표시합니다.
  이 경우 기준은 '해당 없음'으로 통과 처리되므로 holds 값은 무시됩니다. 대상에 속하는지 불확실하면 applicable=true로 두고 판정합니다.
  applicable=false일 때 evidence_type과 evidence는 '대상 집단에 속하지 않는다'는 근거를 가리킵니다. 명시적 근거(explicit)와 인용이 없으면 코드가 '해당 없음'을 인정하지 않습니다.
- cohort가 지정된 규칙은 특정 참여자 군 전용입니다. 환자가 그 군에 속하는지와 상관없이 규칙에 적힌 조건이 성립하는지만 판정하세요(어느 군으로 판정할지는 코드가 결정합니다).
- 입력으로 받은 모든 rule_id에 대해 정확히 하나씩 평가를 반환합니다.
"""


LOGISTICS = "consent_or_logistics"


def is_waivable_logistics(a: CriterionAssessment) -> bool:
    """적격성을 막지 않는 미확인 절차 기준: 동의·방문 가능 등 연구진이 등록 시 확인하는 '선정' 기준.

    제외 기준은 절차로 분류돼도(예: 다른 임상시험 참여 중) 환자에게 확인할 수 있고,
    파서가 임상 기준을 절차로 잘못 분류할 수 있으므로 면제하지 않습니다.
    """
    return a.passes is None and a.category == LOGISTICS and a.kind == "inclusion" and not a.underspecified


def is_blocking_unknown(a: CriterionAssessment) -> bool:
    """판정 보류(UNCERTAIN)를 일으키는 미확인 항목인가 (underspecified 포함)."""
    return a.passes is None and not is_waivable_logistics(a)


def is_askable_unknown(a: CriterionAssessment) -> bool:
    """확인 질문으로 해소를 시도할 미확인 항목 (underspecified는 의사 참고용 질문으로 따로 다룸)."""
    return is_blocking_unknown(a) and not a.underspecified


def needs_physician_review(a: CriterionAssessment) -> bool:
    return a.passes is None and a.underspecified


def decide(assessments: list[CriterionAssessment]) -> str:
    if any(a.passes is False for a in assessments):
        return "INELIGIBLE"
    if any(is_blocking_unknown(a) for a in assessments):
        return "UNCERTAIN"
    return "ELIGIBLE"


_RANK = {"ELIGIBLE": 2, "UNCERTAIN": 1, "INELIGIBLE": 0}


def _select_cohort(assessments: list[CriterionAssessment], cohorts: list[str]):
    """코호트가 나뉜 시험: 모든 코호트를 공통 기준 + 각 코호트 기준으로 따로 판정하고,
    시험 적격성은 그중 가장 나은 코호트의 판정을 따름 (Trial = ELIGIBLE via Cohort B).

    코호트는 cohorts 목록과 규칙에 실제로 붙은 cohort 값을 모두 사용합니다.
    (파서가 cohorts 목록과 다른 이름을 규칙에 적어도, 서로 다른 코호트 전용 기준이 한꺼번에 적용되지 않도록)
    같은 적격성이면 결정적 순서로 고름: 판정 불가 항목이 적은 코호트 -> 선언 순서.

    반환: (판정에 적용한 기준 목록, 선택된 코호트 또는 None, 코호트별 적격성)
    """
    labels = [c for c in cohorts if any(a.cohort == c for a in assessments)]
    for a in assessments:
        if a.cohort is not None and a.cohort not in labels:
            labels.append(a.cohort)
    if not labels:
        # 코호트 구분이 없음 -> 모든 기준 적용
        return assessments, None, {}
    results = {}
    for c in labels:
        subset = [a for a in assessments if a.cohort in (None, c)]
        results[c] = (subset, decide(subset))
    best = min(range(len(labels)), key=lambda i: (
        -_RANK[results[labels[i]][1]],
        sum(is_blocking_unknown(a) for a in results[labels[i]][0]),
        i,
    ))
    best_label = labels[best]
    return results[best_label][0], best_label, {c: e for c, (_, e) in results.items()}


def _enforce_evidence(a: LLMAssessment) -> LLMAssessment:
    """근거 없는 확정 판정을 코드로 차단 (모델이 바뀌어도 같은 기준 유지).

    - absent(기록에 관련 정보 없음)인데 yes/no로 판정 -> unknown
    - inferred(추론)로 yes/no를 판정 -> unknown (확신도와 무관)
      예: "62세이니 폐경 후일 것", "기록에 없으니 받지 않았을 것"
      모델이 스스로 매긴 확신도는 믿을 수 없으므로(추론에도 high를 매김), 확정 판정은 명시적 근거가 있을 때만 인정합니다.
    이렇게 되돌린 항목은 확인 질문 대상이 되고, 모델의 추정은 reasoning에 남습니다.

    '해당 없음'(applicable=false)도 판정이므로 같은 기준을 적용합니다.
    대상 집단에 속하지 않는다는 명시적 근거(explicit + 인용)가 없으면 applicable=true, holds=unknown으로 되돌립니다.
    (N/A가 적격성을 낙관적으로 올리는 우회로가 되지 않도록)
    """
    if not a.applicable:
        if a.evidence_type == "explicit" and a.evidence.strip() and a.evidence.strip() != "근거 없음":
            return a
        return a.model_copy(update={
            "applicable": True,
            "holds": "unknown",
            "reasoning": f"[코드 검증: '해당 없음'의 명시적 근거가 없어 확정할 수 없음 - 모델 판정 '해당 없음' 보류] {a.reasoning}",
            "missing_info": a.missing_info or f"대상 집단 해당 여부 확인 필요: {a.reasoning[:120]}",
        })
    if a.holds == "unknown":
        return a
    reason = None
    if a.evidence_type == "absent":
        reason = "기록에 관련 정보가 없어 확정할 수 없음"
    elif a.evidence_type == "inferred":
        reason = "명시적 근거가 없는 추론이라 확정할 수 없음"
    if reason is None:
        return a
    return a.model_copy(update={
        "holds": "unknown",
        "reasoning": f"[코드 검증: {reason} - 모델 판정 '{a.holds}' 보류] {a.reasoning}",
        "missing_info": a.missing_info or f"확인 필요: {a.reasoning[:120]}",
    })


_QUOTE_SPLIT = re.compile(r'\s+/\s+|[|;\n]|\.\.\.|…|["“”«»「」『』]')
_MIN_QUOTE_CHARS = 6  # 공백 제외 글자 수. 너무 짧은 조각("no", "남성")은 우연히 일치할 수 있음


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def evidence_in_source(evidence: str, source_text: str) -> bool:
    """인용 근거 중 한 조각 이상이 환자 원문에 그대로 있는가 (공백·대소문자 무시)."""
    source = _norm_text(source_text)
    for frag in _QUOTE_SPLIT.split(evidence):
        frag = _norm_text(frag).strip(" .,:'`()[]")
        if len(frag.replace(" ", "")) >= _MIN_QUOTE_CHARS and frag in source:
            return True
    return False


_FIELD_REF = re.compile(r'["\']?\b(sex|age|ecog)\b["\']?\s*[:=]\s*["\']?([a-z0-9.]+)', re.IGNORECASE)


def _profile_field_ref_ok(evidence: str, profile: PatientProfile | None) -> bool:
    """근거가 프로필의 구조화 필드(sex/age/ecog) 값을 정확히 가리키는가. (빈 목록 등 '없음' 덤프는 해당 없음)"""
    if profile is None:
        return False
    known = {"sex": None if profile.sex == "unknown" else profile.sex,
             "age": None if profile.age is None else str(profile.age),
             "ecog": None if profile.ecog is None else str(profile.ecog)}
    refs = _FIELD_REF.findall(evidence)
    return bool(refs) and all(known[k.lower()] is not None and known[k.lower()] == v.lower() for k, v in refs)


def _enforce_quote(a: LLMAssessment, source_text: str | None,
                   profile: PatientProfile | None = None) -> LLMAssessment:
    """확정 판정(yes/no, 해당 없음)의 인용 근거가 환자 원문에 실제로 있는지 코드로 확인.

    모델이 evidence_type="explicit"이라고 표시해도 인용이 원문에 없으면(필드 덤프, 번역·의역, 지어낸 인용)
    확정하지 않고 unknown으로 되돌립니다. source_text가 없으면 검사하지 않습니다.
    """
    if source_text is None or (a.applicable and a.holds == "unknown"):
        return a
    if evidence_in_source(a.evidence, source_text) or _profile_field_ref_ok(a.evidence, profile):
        return a
    label = "해당 없음" if not a.applicable else a.holds
    return a.model_copy(update={
        "applicable": True,
        "holds": "unknown",
        "reasoning": f"[코드 검증: 인용 근거가 환자 원문에서 확인되지 않음 - 모델 판정 '{label}' 보류] {a.reasoning}",
        "missing_info": a.missing_info or f"확인 필요: {a.reasoning[:120]}",
    })


def match_trial(profile: PatientProfile, parsed: ParsedTrial, trial: dict,
                reference_date: str | None = None, source_text: str | None = None) -> TrialMatch:
    """source_text: 환자 원문(확인 질문 답변 포함). 주어지면 LLM 판정의 인용 근거를 원문과 대조합니다."""
    ref = reference_date or config.reference_date()
    by_id = {r.rule_id: r for r in parsed.rules}
    results: dict[str, CriterionAssessment] = {}

    for rule in parsed.rules:
        res = rule_engine.evaluate(rule, profile)
        if res is not None:
            results[rule.rule_id] = res

    pending = [r for r in parsed.rules if r.rule_id not in results]
    if pending:
        pending_parsed = parsed.model_copy(update={"rules": pending})
        user = f"""기준일: {ref}

<patient>
{profile.model_dump_json(indent=1)}
</patient>

<trial id="{parsed.trial_id}">
title: {parsed.title}
target_population: {parsed.target_population}
intervention: {parsed.intervention_summary}
parsing_notes: {parsed.parsing_notes}
brief_summary: {trial.get('brief_summary', '')[:3000]}
</trial>

<rules_to_assess>
{rules_json(pending_parsed)}
</rules_to_assess>

각 규칙의 holds를 판정하세요."""
        out = llm.structured(
            MatcherOutput,
            system=SYSTEM,
            user=user,
            effort=config.EFFORT["matcher"],
        )
        for a in out.assessments:
            rule = by_id.get(a.rule_id)
            if rule is None or a.rule_id in results:
                continue
            a = _enforce_quote(_enforce_evidence(a), source_text, profile)
            results[a.rule_id] = CriterionAssessment(
                rule_id=a.rule_id,
                kind=rule.kind,
                criterion_text=rule.text,
                holds=a.holds,
                # 조건부 기준의 대상이 아니면 '해당 없음'으로 통과
                passes=True if not a.applicable else rule_engine.passes(rule.kind, a.holds),
                applicable=a.applicable,
                confidence=a.confidence,
                evidence=a.evidence,
                reasoning=a.reasoning,
                missing_info=a.missing_info,
                method="llm",
                category=rule.category,
                underspecified=rule.underspecified,
                cohort=rule.cohort,
            )

    # LLM이 누락한 규칙은 unknown으로 채움
    for rule in parsed.rules:
        if rule.rule_id not in results:
            results[rule.rule_id] = CriterionAssessment(
                rule_id=rule.rule_id, kind=rule.kind, criterion_text=rule.text, holds="unknown",
                passes=None, confidence="low", evidence="근거 없음", reasoning="평가 누락",
                missing_info=rule.text, method="llm", category=rule.category,
                underspecified=rule.underspecified,
                cohort=rule.cohort,
            )

    all_assessments = [results[r.rule_id] for r in parsed.rules]
    assessments, matched_cohort, cohort_results = _select_cohort(all_assessments, parsed.cohorts)
    other = [a for a in all_assessments if a not in assessments]
    eligibility = decide(assessments)
    n_pass = sum(a.passes is True for a in assessments)
    n_fail = sum(a.passes is False for a in assessments)
    n_unknown = sum(a.passes is None for a in assessments)
    failed = [f"{a.rule_id}({a.criterion_text[:60]})" for a in assessments if a.passes is False]
    n_blocking = sum(is_askable_unknown(a) for a in assessments)
    n_physician = sum(needs_physician_review(a) for a in assessments)
    n_logistics = sum(is_waivable_logistics(a) for a in assessments)
    summary = f"{eligibility}: 통과 {n_pass} / 불충족 {n_fail} / 판정불가 {n_blocking}"
    if n_physician:
        summary += f" (+의사 확인 필요 {n_physician})"
    if n_logistics:
        summary += f" (+절차 기준 확인 필요 {n_logistics})"
    if matched_cohort:
        summary += f" [코호트: {matched_cohort}]"
    if failed:
        summary += " | 불충족: " + "; ".join(failed[:3])

    return TrialMatch(
        patient_id=profile.patient_id,
        trial_id=parsed.trial_id,
        eligibility=eligibility,  # type: ignore[arg-type]
        assessments=assessments,
        n_pass=n_pass,
        n_fail=n_fail,
        n_unknown=n_unknown,
        summary=summary,
        matched_cohort=matched_cohort,
        cohort_results=cohort_results,
        other_cohort_assessments=other,
    )


def compact(match: TrialMatch) -> dict:
    """오케스트레이터/추천 에이전트에 넘길 요약 형태."""
    return {
        "trial_id": match.trial_id,
        "eligibility": match.eligibility,
        "summary": match.summary,
        "unknown": [
            {"rule_id": a.rule_id, "criterion": a.criterion_text, "missing_info": a.missing_info}
            for a in match.assessments if is_askable_unknown(a)
        ],
        "physician_review": [
            {"rule_id": a.rule_id, "criterion": a.criterion_text, "info_needed": a.missing_info}
            for a in match.assessments if needs_physician_review(a)
        ],
        "logistics_to_confirm": [
            a.criterion_text for a in match.assessments
            if is_waivable_logistics(a)
        ],
        "failed": [
            {"rule_id": a.rule_id, "criterion": a.criterion_text, "evidence": a.evidence}
            for a in match.assessments if a.passes is False
        ],
    }


def compact_json(matches: list[TrialMatch]) -> str:
    return json.dumps([compact(m) for m in matches], ensure_ascii=False, indent=1)
