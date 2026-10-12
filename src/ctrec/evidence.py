"""기준별 근거 수준(evidence requirement) 정책 - 팀 결정 D2 Option D (Hybrid).

1) 유형별 고정 규칙: 규칙 category마다 필요한 근거 수준을 코드로 정합니다(재현 가능).
2) 유형이 애매한 기준(category="other")만 파싱 검증 단계(parser_validator)의 LLM 제안을 씁니다.
3) 고정 규칙과 LLM 제안이 다르면 더 엄격한 쪽(clinical_record)을 택하고 검증 기록에 남깁니다.

근거 수준
- self_report_ok : 환자·보호자 답변(자기 보고)으로 판정해도 됨 (나이, 복용 약, 치료 이력 등)
- clinical_record: 진료 기록(검사 결과지, 병리·유전자 보고서 등)으로 확인해야 함 (바이오마커, 검사 수치 등)

진료 기록이 필요한 기준을 환자 답변만으로 판정하면 판정을 확정하지 않고 UNVERIFIED(판정 불가)로 남깁니다
(아키텍처 §19 User Answer ≠ Verified Clinical Truth: UNKNOWN_UNVERIFIED -> REQUEST_DOCUMENT).
답변이 가리킨 값은 보존하고, 같은 질문을 반복하지 않고 추천·보고서에 '문서 확인 필요'로 남깁니다(matcher._apply_evidence_policy).
"""

from __future__ import annotations

from .schemas import RequiredEvidence, Rule

TYPE_POLICY: dict[str, RequiredEvidence] = {
    "demographic": "self_report_ok",
    "reproductive": "self_report_ok",
    "medication": "self_report_ok",
    "treatment_history": "self_report_ok",
    "comorbidity": "self_report_ok",
    "procedure": "self_report_ok",
    "consent_or_logistics": "self_report_ok",
    "diagnosis": "clinical_record",
    "disease_stage": "clinical_record",
    "biomarker": "clinical_record",
    "lab": "clinical_record",
    "performance_status": "clinical_record",
}

# 유형만으로 정할 수 없는 category -> LLM 제안 사용, 제안이 없으면 엄격한 쪽
AMBIGUOUS_DEFAULT: RequiredEvidence = "clinical_record"


def required_evidence(rule: Rule, proposal: RequiredEvidence | None = None) -> tuple[RequiredEvidence, str | None]:
    """반환: (근거 수준, 고정 규칙과 제안이 충돌하면 그 설명)."""
    fixed = TYPE_POLICY.get(rule.category)
    if fixed is None:
        return proposal or AMBIGUOUS_DEFAULT, None
    if proposal is None or proposal == fixed:
        return fixed, None
    strict: RequiredEvidence = "clinical_record"
    return strict, f"유형 규칙({rule.category}: {fixed})과 검증 제안({proposal})이 달라 {strict} 적용"


# ---------------------------------------------------------------------------
# 판정 근거가 어디서 왔는가: 처음 받은 기록 vs 확인 질문 답변
# ---------------------------------------------------------------------------

ANSWER_MARKER = "\n\n[추가 확인 정보"  # pipeline.Session.clarify가 답변을 붙일 때 쓰는 머리말


def split_source(source_text: str) -> tuple[str, str]:
    """환자 원문을 (처음 받은 기록, 확인 질문 답변들)로 나눔."""
    i = source_text.find(ANSWER_MARKER)
    return (source_text, "") if i < 0 else (source_text[:i], source_text[i:])
