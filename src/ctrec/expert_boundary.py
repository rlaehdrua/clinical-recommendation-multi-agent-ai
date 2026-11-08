"""임상의 판단 기준의 안전 경계: 임상의 판단 없이는 이 기준을 확정하지 않습니다 (읽기·판정 계층 공용, LLM 호출 없음).

불변식 (계약 CriterionResult._authority의 "전문가 판단 기준은 임상의만 확정"을 판정 계층에서 지킴)
- 이 기준의 MET/NOT_MET은 LLM·판정기(reviewed 슬롯 연결 포함)·계약 평가기·규칙 엔진 어느 쪽 값으로도 남기지 않습니다.
- 문서 확인은 임상의 판단이 아닙니다: 근거가 문서 검증돼 있어도 같습니다.
- 다른 기준의 판정은 건드리지 않습니다: 독립적인 불충족이 있으면 쌍은 그대로 부적격입니다 (matcher.decide).

분류 신호 (넷이며 완전하지 않음. 모두 '막는' 방향으로만 작동하고, 어느 신호도 판정을 확정 쪽으로 바꾸지 않음)
1. Rule.underspecified: 파서·검증기가 "원문에 구체 기준이 없음"으로 확인한 기준.
2. EXPERT_RULES: 위 플래그가 없지만 슬롯 후보표(adjudication_candidate=expert)가 임상의 판단으로 분류한 저장 규칙.
   기준 ID로 묶으므로 재파싱으로 ID가 바뀌면 다시 맞춰야 합니다 (tests/test_expert_boundary.py가 후보표와 대조).
3. AUDIT_EXPERT_RULES: 2026-11-08 최종 감사(P1-3)에서 찾은, 플래그·후보표 밖의 저장 규칙. 원문이 판단을 임상의에게
   맡기거나("in the opinion of the investigator") 기준값 없는 "clinically significant" 심혈관 질환처럼 열린 판단이며,
   NSCLC 두 건은 규칙 GT(rule_gt.csv)도 NEEDS_EXPERT로 둡니다. 기준 ID + 원문 앵커로 고정합니다(테스트가 대조).
   임상 검수 기록이 없으므로 unreviewed이고, 검수 결과에 따라 빼거나 더할 수 있습니다.
4. 판단 주체 문구 (REASON_WORDING): 원문이 판정 주체를 임상의로 명시한 좁은 표현만("in the opinion/judgement of the
   investigator", "as judged by the investigator", "at the discretion of the investigator" 등, JUDGMENT_WORDING).
   목록에 없는 새 시험(LLM 경로의 즉석 파싱 포함)을 위한 실패-안전 장치입니다. "clinically significant"처럼 판단 여부가
   기준마다 다른 표현은 넣지 않습니다(그런 기준은 3번 목록 또는 임상 검수로). 저장 규칙 631개에서 이 문구가 걸리는
   기준은 12개이고 그중 10개는 이미 underspecified, 나머지 2개는 3번 목록과 같습니다(tests/test_expert_boundary.py).
목록·문구 밖의 새 시험에서 플래그 없이 들어온 임상 판단 기준(예: 판단 주체 없이 "clinically significant ...")은 여전히 막지 못합니다.

임상의 판단 입력은 clinician_authority 하나뿐입니다: 현재 권한이 인정한 판단(ClinicianJudgment)이 있는 기준만
그 판단대로 MET/NOT_MET이 되고 method="clinician"으로 남습니다 (decision_state가 decided_by="clinician"으로 옮김).
인자·상태 문자열·decided_by 값으로 통과시키는 길은 없습니다. 운영 기본 권한(NoClinicianAuthority)은 아무것도 인정하지 않습니다.
임상의 판단 기준이 아닌 기준(문서·환자 답으로 정할 기준)에는 판단이 있어도 적용하지 않습니다.
"""

from __future__ import annotations

import re
from typing import Iterable

from . import clinician_authority
from .schemas import CriterionAssessment, ParsedTrial, Rule, TrialMatch
from .tools import rule_engine

FLAG = "clinician_judgment_required"
REASON_UNDERSPECIFIED = "underspecified"
REASON_REGISTERED = "registered"
REASON_AUDIT = "registered_audit"
REASON_WORDING = "judgment_wording"

# "<시험>:<기준>" -> 슬롯 후보표의 슬롯 (data/nsclc/cohort/slot_candidates.csv, underspecified=False인 저장 규칙만).
# 임상 검수로 바뀌면 여기만 고칩니다.
EXPERT_RULES: dict[str, str] = {
    "NCT07098338:I5": "judge.life_expectancy",
    "NCT07027514:I7": "judge.life_expectancy",
    "NCT06928389:I5": "judge.life_expectancy",
    "NCT07098338:E10": "cm.cardiac",
    "NCT07680790:I2": "dx.disease_setting",
}


# 위 목록의 출처와 검수 상태. 임상 검수 기록이 없으므로 전부 unreviewed이고, 이 값은 판정에 쓰지 않습니다.
# "reviewed"는 사람이 남긴 검수 기록이 생긴 뒤에만 씁니다 (tests/test_expert_p0_integration.py가 후보표와 대조).
CANDIDATE_TABLE = "data/nsclc/cohort/slot_candidates.csv"
EXPERT_RULE_SOURCES: dict[str, dict[str, str]] = {
    key: {"slot": slot, "source": CANDIDATE_TABLE, "review_status": "unreviewed"} for key, slot in EXPERT_RULES.items()
}

# "<시험>:<기준>" -> 저장 규칙 원문에 있어야 하는 앵커 (재파싱으로 ID가 밀리면 테스트가 실패해 다시 맞추게 함).
# 출처: 2026-11-08 최종 감사 P1-3 (저장 규칙 695개 중 임상 판단 표현이 있으나 분류 신호가 없던 기준). 판정 분류는 목록 소속만 봅니다.
AUDIT_SOURCE = "final release audit 2026-11-08 (P1-3)"
AUDIT_EXPERT_RULES: dict[str, str] = {
    "NCT06468397:E1": "in the opinion of the investigator",            # data/parsed (frozen), 연구자 의견상 참여 제한 질환
    "NCT05127980:E1": "judgement of the treating clinician",           # data/parsed (frozen), 대조군 IM 의심·진단은 임상의 판단
    "NCT07027514:E14": "clinically significant cardiac arrhythmias",   # data/nsclc, 규칙 GT NEEDS_EXPERT
    "NCT07416058:E17": "clinically significant cardiovascular disease",  # data/nsclc, 규칙 GT NEEDS_EXPERT (하위 E4~E7 묶음 머리)
}
AUDIT_EXPERT_RULE_SOURCES: dict[str, dict[str, str]] = {
    key: {"anchor": anchor, "source": AUDIT_SOURCE, "review_status": "unreviewed"} for key, anchor in AUDIT_EXPERT_RULES.items()
}

# 판정 주체를 임상의로 명시한 표현만 (실패-안전: 걸리면 임상의 판단 대기, 안 걸리면 기존 분류 그대로).
_WHO = r"(?:treating\s+|study\s+|site\s+|principal\s+|primary\s+)?(?:investigator|physician|clinician|doctor)s?"
JUDGMENT_WORDING = re.compile("|".join([
    rf"\b(?:in|per|according\s+to)\s+the\s+(?:opinion|judge?ment|discretion)\s+of\s+(?:the\s+|a\s+)?{_WHO}",
    rf"\bas\s+per\s+(?:the\s+)?(?:opinion|judge?ment)\s+of\s+(?:the\s+)?{_WHO}",
    r"\b(?:investigator|physician|clinician)['’]s\s+(?:opinion|judge?ment|discretion)",
    rf"\b(?:at|per)\s+(?:the\s+)?discretion\s+of\s+(?:the\s+)?{_WHO}",
    rf"\b(?:deemed|judged|considered|assessed\s+as)\s+(?:\w+\s+){{0,3}}by\s+(?:the\s+)?{_WHO}",
    r"\b(?:the\s+)?investigator\s+(?:believes|considers|deems|judges|feels)\b",
]), re.IGNORECASE)


def clinician_reason(trial_id: str, rule: Rule) -> str | None:
    """이 기준이 임상의 판단을 요구하면 사유, 아니면 None."""
    if getattr(rule, "underspecified", False):
        return REASON_UNDERSPECIFIED
    key = f"{trial_id}:{getattr(rule, 'rule_id', '')}"
    if key in EXPERT_RULES:
        return REASON_REGISTERED
    if key in AUDIT_EXPERT_RULES:
        return REASON_AUDIT
    if JUDGMENT_WORDING.search(getattr(rule, "text", "") or ""):
        return REASON_WORDING
    return None


def enforce(trial_id: str, rule: Rule, a: CriterionAssessment) -> CriterionAssessment:
    """임상의 판단 기준의 판정을 미확정으로 되돌림. 대상이 아니면 같은 객체를 돌려줌 (여러 번 적용해도 같음).

    underspecified=True로 표시하므로 기존 경로가 그대로 의사 확인으로 처리합니다
    (matcher.needs_physician_review -> gap physician_review -> EXPERT_REVIEW, 추천의 의사 확인 메모).
    판정기가 낸 값은 llm_judgment와 reasoning에 감사용으로 남습니다.
    """
    if clinician_reason(trial_id, rule) is None:
        return a
    judgment = clinician_authority.current().judgment_for(trial_id, rule)
    if judgment is not None:
        return _judged(a, rule, judgment)
    if a.passes is None and a.underspecified and not a.needs_verification:
        return a
    update: dict = {"underspecified": True, "needs_verification": False, "self_reported_holds": None,
                    "self_reported_applicable": True}  # 기록 확인으로 끝나는 기준이 아님
    if FLAG not in a.validation_flags:
        update["validation_flags"] = [*a.validation_flags, FLAG]
    if a.passes is not None or a.holds != "unknown" or not a.applicable:
        said = "해당 없음" if not a.applicable else a.holds
        update.update(holds="unknown", passes=None, applicable=True, confidence="low", evidence_source="none",
                      evidence_directness="none",
                      reasoning=f"[코드 검증: 임상의 판단 기준 - '{said}' 판정 보류] {a.reasoning}")
    if not a.missing_info or a.needs_verification:
        update["missing_info"] = "담당 의사가 판단해야 하는 기준"
    return a.model_copy(update=update)


def _judgment_evidence(j: clinician_authority.ClinicianJudgment) -> str:
    """판정 근거 문자열. 근거 사실 ID를 그대로 넣어 fact_view.link_evidence가 그 사실들로 잇게 함 (decision_state는 그 사실의
    문서 확인 범위로 진료 기록 요건을 다른 판정과 똑같이 확인). 근거가 없으면 사실과 이어지지 않음."""
    basis = f" 근거 사실: {', '.join(j.basis_fact_ids)}" if j.basis_fact_ids else ""
    return f"임상의 판단 {j.judgment_id} ({j.rationale_code}).{basis}"


def unrecognized_to_unknown(trial_id: str, rule: Rule, a: CriterionAssessment) -> CriterionAssessment:
    """저장된 method="clinician" 판정을 지금 권한이 인정하지 않으면 임상의 판단 대기로 (보기·재계산용, 같은 객체 또는 사본)."""
    if a.method != "clinician" or recognized(trial_id, rule, a):
        return a
    return a.model_copy(update={"holds": "unknown", "passes": None, "applicable": True, "confidence": "low",
                                "underspecified": True, "needs_verification": False,
                                "missing_info": "담당 의사가 판단해야 하는 기준",
                                "reasoning": f"[임상의 판단이 지금 권한으로 인정되지 않음 - 판단 대기] {a.reasoning}"})


def _judged(a: CriterionAssessment, rule: Rule, j: clinician_authority.ClinicianJudgment) -> CriterionAssessment:
    """권한이 인정한 임상의 판단을 판정으로. 판정기가 낸 값은 llm_judgment에 남고, 판단 ID는 validation_flags에 남음."""
    holds = "yes" if j.decision == "MET" else "no"
    flags = [f for f in a.validation_flags if f != FLAG and not f.startswith(clinician_authority.JUDGED_FLAG + ":")]
    return a.model_copy(update={
        "holds": holds, "passes": rule_engine.passes(rule.kind, holds), "applicable": True, "confidence": "high",
        "method": "clinician", "underspecified": True, "needs_verification": False, "self_reported_holds": None,
        "self_reported_applicable": True, "evidence": _judgment_evidence(j),
        "evidence_source": "none", "evidence_directness": "none", "missing_info": None,
        "validation_flags": [*flags, FLAG, clinician_authority.judgment_flag(j)],
        "reasoning": f"[임상의 판단: {j.decision} - {j.clinician}, {j.at}]"})


def recognized_judgment(trial_id: str, rule: Rule, a: CriterionAssessment):
    """method="clinician" 판정을 낸, 지금 권한이 인정하는 바로 그 판단 (없으면 None). 저장된 판정의 재확인용."""
    jid = clinician_authority.judged_id(a)
    if a.method != "clinician" or jid is None or clinician_reason(trial_id, rule) is None:
        return None
    j = clinician_authority.current().judgment_for(trial_id, rule)
    return j if j is not None and j.judgment_id == jid else None


def recognized(trial_id: str, rule: Rule, a: CriterionAssessment) -> bool:
    return recognized_judgment(trial_id, rule, a) is not None


def enforce_all(parsed: ParsedTrial, assessments: Iterable[CriterionAssessment]) -> list[CriterionAssessment]:
    """시험 하나의 판정 목록에 경계를 적용 (matcher.match_trial이 판정을 집계하기 직전에 부름)."""
    rules = {r.rule_id: r for r in parsed.rules}
    return [enforce(parsed.trial_id, rules[a.rule_id], a) if a.rule_id in rules else a for a in assessments]


def violations(match: TrialMatch, parsed: ParsedTrial) -> list[str]:
    """임상의 판단 기준을 확정한 채로 남은 판정의 기준 ID (빈 목록 = 불변식 성립). 다른 코호트 기준도 포함."""
    rules = {r.rule_id: r for r in parsed.rules}
    return [a.rule_id for a in match.assessments + match.other_cohort_assessments
            if a.rule_id in rules and a.passes is not None and clinician_reason(parsed.trial_id, rules[a.rule_id])
            and not recognized(parsed.trial_id, rules[a.rule_id], a)]


__all__ = ["AUDIT_EXPERT_RULES", "AUDIT_EXPERT_RULE_SOURCES", "CANDIDATE_TABLE", "EXPERT_RULES", "EXPERT_RULE_SOURCES",
           "FLAG", "JUDGMENT_WORDING", "clinician_reason", "enforce", "enforce_all", "recognized", "recognized_judgment",
           "unrecognized_to_unknown",
           "violations"]
