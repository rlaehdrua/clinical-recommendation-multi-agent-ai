"""환자 정보 이해 에이전트: 자유 형식 환자 프로파일 -> 구조화된 PatientProfile."""

from __future__ import annotations

from .. import config, llm
from ..schemas import PatientProfile

SYSTEM = """당신은 임상시험 매칭을 위해 환자 정보를 정리하는 '환자 정보 이해 에이전트'입니다.
한국어/영어가 섞인 자유 형식의 환자 프로파일(인구학 정보, 병력, 검사 결과, 투약 등)에서 핵심 정보를 추출합니다.

원칙:
- 입력에 명시된 정보만 추출합니다. 추정하거나 일반적인 값으로 채우지 않습니다. 모르면 null 또는 빈 목록으로 둡니다.
- 모든 사실(Fact, LabValue)에는 입력 원문에서 그대로 인용한 source_quote를 붙입니다.
- 검사명(labs.name)은 영문 소문자 표준명으로 씁니다. 예: hemoglobin, platelets, absolute neutrophil count, creatinine, creatinine clearance, total bilirubin, ast, alt, hba1c, ldl cholesterol, inr, albumin, lvef. 유전자 EGFR과 신기능 eGFR은 혼동하지 않습니다(신기능은 "egfr (renal)").
- 검사 수치는 숫자(value)와 단위(unit)를 분리합니다. 단위 변환은 하지 않습니다.
- ECOG가 명시되지 않았으면 null입니다. Karnofsky 점수는 other_facts에 기록하고 ECOG로 바꾸지 않습니다.
- '[추가 확인 정보]' 섹션은 환자/보호자가 확인 질문에 답한 내용입니다. 기존 정보와 충돌하면 더 최근 답변을 우선하되 other_facts에 충돌 사실을 남깁니다.
- missing_or_ambiguous에는 임상시험 적격성 판단에 흔히 필요하지만 누락되었거나 모호한 항목(예: 병기, 최근 검사일, 이전 치료 차수, 임신 가능 여부)을 적습니다.
"""


def profile_patient(patient_id: str, raw_text: str, reference_date: str | None = None) -> PatientProfile:
    user = f"""기준일: {reference_date or config.reference_date()}
patient_id: {patient_id}

<patient_profile>
{raw_text}
</patient_profile>

위 환자 정보를 구조화하세요."""
    profile = llm.structured(
        PatientProfile,
        system=SYSTEM,
        user=user,
        effort=config.EFFORT["patient_profiler"],
    )
    profile.patient_id = patient_id
    return profile
