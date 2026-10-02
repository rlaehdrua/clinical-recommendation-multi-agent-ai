"""에이전트 간 주고받는 구조화 데이터 스키마.

규칙(Rule)의 판정은 '조건이 환자에게 성립하는가(holds)'로 통일합니다.
- 선정 기준(inclusion): holds == "yes" 여야 통과
- 제외 기준(exclusion): holds == "no" 여야 통과
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Holds = Literal["yes", "no", "unknown"]
Eligibility = Literal["ELIGIBLE", "INELIGIBLE", "UNCERTAIN"]
Confidence = Literal["high", "medium", "low"]

RuleCategory = Literal[
    "demographic", "diagnosis", "disease_stage", "biomarker", "lab", "treatment_history",
    "medication", "comorbidity", "performance_status", "reproductive", "procedure",
    "consent_or_logistics", "other",
]
Operator = Literal[">=", "<=", ">", "<", "==", "!=", "in", "not_in", "exists", "not_exists"]


# ---------------------------------------------------------------------------
# 1) 기준 파싱 에이전트 출력
# ---------------------------------------------------------------------------

class Rule(BaseModel):
    rule_id: str = Field(description="I1, I2, ... (inclusion) / E1, E2, ... (exclusion)")
    kind: Literal["inclusion", "exclusion"]
    text: str = Field(description="원문 기준 문장(해당 원자 조건 부분)")
    category: RuleCategory
    field: str | None = Field(
        description="규칙 엔진용 필드: 'age' | 'sex' | 'ecog' | 'lab:<english lab name>' | null"
    )
    operator: Operator | None
    value: str | None = Field(description="비교값(문자열). 예: '18', 'female', '1.5'")
    unit: str | None
    time_window_days: int | None = Field(description="'최근 N일 이내' 등 시간 조건, 없으면 null")
    structured_evaluable: bool = Field(description="field/operator/value 만으로 기계적 판정이 가능한지")
    underspecified: bool = Field(
        description="프로토콜 원문에 구체 내용(약물 목록, 수치 기준 등)이 없어 원문만으로 판정 불가한 기준. "
                    "예: 'Use of certain medications', 'clinically significant abnormalities', 연구자 판단"
    )
    cohort: str | None = Field(
        description="특정 코호트(군)에만 적용되는 기준이면 그 코호트 이름(cohorts 목록의 값 그대로), 모든 참여자에 적용되면 null"
    )


class ParsedTrial(BaseModel):
    trial_id: str
    title: str
    target_population: str = Field(description="대상 환자군 한 줄 요약")
    intervention_summary: str
    key_conditions: list[str]
    cohorts: list[str] = Field(
        description="기준이 코호트(군)별로 나뉘면 코호트 이름 목록(예: 'IM group', 'control group'), 나뉘지 않으면 빈 목록"
    )
    rules: list[Rule]
    parsing_notes: str = Field(description="모호하거나 해석이 필요한 기준에 대한 메모")


# ---------------------------------------------------------------------------
# 2) 환자 정보 이해 에이전트 출력
# ---------------------------------------------------------------------------

class Fact(BaseModel):
    description: str
    date: str | None
    source_quote: str = Field(description="환자 입력 원문에서 그대로 인용한 근거")


class LabValue(BaseModel):
    name: str = Field(description="영문 소문자 표준 검사명. 예: hemoglobin, platelets, creatinine")
    value: float | None
    unit: str | None
    date: str | None
    source_quote: str


class PatientProfile(BaseModel):
    patient_id: str
    age: int | None
    sex: Literal["male", "female", "other", "unknown"]
    primary_diagnosis: str | None
    disease_stage: str | None
    diagnoses: list[Fact]
    biomarkers: list[Fact]
    labs: list[LabValue]
    medications: list[Fact]
    treatment_history: list[Fact]
    ecog: int | None = Field(description="ECOG 수행 상태 0-5, 모르면 null")
    other_facts: list[Fact]
    missing_or_ambiguous: list[str] = Field(description="중요하지만 누락/모호한 정보")


# ---------------------------------------------------------------------------
# 3) 추론·매칭 에이전트 출력
# ---------------------------------------------------------------------------

class LLMAssessment(BaseModel):
    rule_id: str
    applicable: bool = Field(
        description="조건부 기준('Men who can father a child: ...', 'Women of childbearing potential must ...' 등)의 "
                    "대상 집단에 환자가 속하지 않으면 false(해당 없음 = 통과). 그 외 모든 기준은 true"
    )
    holds: Holds
    evidence_type: Literal["explicit", "inferred", "absent"] = Field(
        description="explicit: 환자 기록에 직접 명시 / inferred: 명시된 사실에서 임상적으로 추론 / "
                    "absent: 관련 정보가 기록에 없음(언급 없음 포함)"
    )
    confidence: Confidence
    evidence: str = Field(description="판단 근거가 된 환자 정보(인용). 없으면 '근거 없음'")
    reasoning: str
    missing_info: str | None = Field(description="unknown일 때 판정에 필요한 정보")


class MatcherOutput(BaseModel):
    assessments: list[LLMAssessment]


class CriterionAssessment(BaseModel):
    rule_id: str
    kind: Literal["inclusion", "exclusion"]
    criterion_text: str
    holds: Holds
    passes: bool | None  # None = 판정 불가
    confidence: Confidence
    evidence: str
    reasoning: str
    missing_info: str | None
    method: Literal["rule_engine", "llm"]
    category: str = "other"  # Rule.category (절차·행정 기준 구분용)
    underspecified: bool = False  # 원문에 구체 기준이 없어 '의사 확인 필요'로 분류
    cohort: str | None = None  # 특정 코호트 전용 기준이면 코호트 이름
    applicable: bool = True  # False = 조건부 기준의 대상이 아님(해당 없음 → 통과)


class TrialMatch(BaseModel):
    patient_id: str
    trial_id: str
    eligibility: Eligibility
    assessments: list[CriterionAssessment]  # 판정에 적용된 기준 (공통 + 선택된 코호트)
    n_pass: int
    n_fail: int
    n_unknown: int
    summary: str
    matched_cohort: str | None = None  # 코호트가 나뉜 시험에서 판정에 사용한 코호트
    cohort_results: dict[str, str] = {}  # 코호트별 적격성
    other_cohort_assessments: list[CriterionAssessment] = []  # 다른 코호트 전용 기준 (판정 미적용, 기록용)


# ---------------------------------------------------------------------------
# 4) 질문 생성 에이전트 출력
# ---------------------------------------------------------------------------

class ClarifyingQuestion(BaseModel):
    question_id: str
    question: str = Field(description="환자/보호자가 이해할 수 있는 한국어 질문")
    answer_type: Literal["yes_no", "number", "date", "free_text"]
    target: list[str] = Field(description="해결 대상 '<trial_id>:<rule_id>' 목록")
    purpose: Literal["eligibility", "physician_reference"] = Field(
        description="eligibility: 적격성 판정용 / physician_reference: 원문에 구체 기준이 없는 항목에 대해 의사 참고용 정보 수집"
    )
    why: str


class QuestionSet(BaseModel):
    questions: list[ClarifyingQuestion]


class QAPair(BaseModel):
    round: int
    question_id: str
    question: str
    answer: str
    purpose: str = "eligibility"
    target: list[str] = []


# ---------------------------------------------------------------------------
# 5) 추천 에이전트 출력
# ---------------------------------------------------------------------------

class RankedTrial(BaseModel):
    rank: int
    trial_id: str
    eligibility: Eligibility
    rationale: str
    key_matches: list[str]
    key_concerns: list[str]
    next_steps: list[str]
    physician_review: list[str] = Field(
        default_factory=list,
        description="코드가 채움: 원문에 구체 기준이 없어 의사가 전체 프로토콜과 대조해야 할 항목. 빈 목록으로 두세요.",
    )


class ExcludedTrial(BaseModel):
    trial_id: str
    reason: str


class RecommendationDraft(BaseModel):
    """추천 에이전트(LLM)의 출력 스키마."""
    patient_id: str
    ranked: list[RankedTrial]
    excluded: list[ExcludedTrial]
    selection_reason: str = Field(
        default="",
        description="1순위 시험을 고른 이유와 다른 후보보다 나은 점(공동 1순위면 서로 구분되지 않는 이유)"
    )
    overall_comment: str


class Recommendation(RecommendationDraft):
    """코드 검증을 거친 최종 추천.

    recommended: 최종 추천 = 1순위 시험 (적합성이 구분되지 않는 공동 1순위면 여러 개)
    ranked: 추천 가능한 전체 후보 순위 (recommended 포함, 공동 순위 허용)
    """
    recommended: list[RankedTrial] = []
    no_recommendation_reason: str | None = None
