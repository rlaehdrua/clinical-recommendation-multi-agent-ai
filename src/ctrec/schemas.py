"""에이전트 간 주고받는 구조화 데이터 스키마.

규칙(Rule)의 판정은 '조건이 환자에게 성립하는가(holds)'로 통일합니다.
- 선정 기준(inclusion): holds == "yes" 여야 통과
- 제외 기준(exclusion): holds == "no" 여야 통과
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

Holds = Literal["yes", "no", "unknown"]
# NOT_EVALUATED: 파싱·매칭 실패로 판정하지 못함 (부적격이 아님, 추천 대상도 아님)
Eligibility = Literal["ELIGIBLE", "INELIGIBLE", "UNCERTAIN", "NOT_EVALUATED"]
Confidence = Literal["high", "medium", "low"]
# 외부 표현(아키텍처 §49): 등록 확정처럼 보이지 않도록 내부 판정(Eligibility)을 바꿔 보여 주는 상태. status.py가 계산
MatchStatus = Literal["CRITERIA_MATCH", "POTENTIAL_MATCH", "CRITERIA_MISMATCH", "INSUFFICIENT_INFORMATION",
                      "NOT_EVALUATED", "NEEDS_REVIEW"]

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


RequiredEvidence = Literal["self_report_ok", "clinical_record"]


class RuleMeta(BaseModel):
    """파싱 검증(D1·D6)과 근거 수준(D2) 결과. 사전 컴파일 때 정해져 고정 저장소에 함께 저장됩니다."""
    # 원문과 구조화 결과가 맞지 않아 자동 판정하지 않는 규칙(UNSUPPORTED). 값은 사유
    review: str | None = None
    # 이 기준의 판정 근거로 무엇이 필요한가 (자기 보고로 충분 / 진료 기록 필요)
    required_evidence: RequiredEvidence = "self_report_ok"
    # underspecified 처리 상태 (D6): 원문이 애매함을 확인 / 원문은 구체적이라 다시 분류 / 애매해 보이나 적용 안 함(검토 전)
    # / 검토 결과 애매하지 않음(질문·검사 결과로 판정 가능)
    underspecified_state: Literal["vague_confirmed", "reparsed_concrete", "vague_suspected",
                                  "reviewed_not_vague"] | None = None
    added_by_validator: bool = False  # 파서가 빠뜨려 검증 단계에서 추가한 기준


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
    # 코드 무결성 검사 결과 (LLM 출력 스키마에는 포함되지 않음, criteria_parser.check_integrity가 채움)
    _integrity_issues: list[str] = PrivateAttr(default_factory=list)
    # 규칙별 검증 메타데이터 (LLM 출력 스키마에는 포함되지 않음, 고정 저장소(trial_store)에서 불러올 때 채움)
    _rule_meta: dict[str, RuleMeta] = PrivateAttr(default_factory=dict)

    @property
    def integrity_issues(self) -> list[str]:
        return self._integrity_issues

    @integrity_issues.setter
    def integrity_issues(self, value: list[str]) -> None:
        self._integrity_issues = value

    @property
    def rule_meta(self) -> dict[str, RuleMeta]:
        return self._rule_meta

    @rule_meta.setter
    def rule_meta(self, value: dict[str, RuleMeta]) -> None:
        self._rule_meta = value


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
    age: int | None = Field(description="만 나이(년, 정수). 1세 미만이면 0")
    age_value: float | None = Field(
        default=None,
        description="원문에 적힌 나이 값 그대로 (예: '3-month-old' -> 3, '54세' -> 54). 모르면 null",
    )
    age_unit: Literal["years", "months", "weeks", "days"] | None = Field(
        default=None, description="age_value의 단위 (원문 표기 그대로, 변환하지 않음)",
    )
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
    # 근거 수준 (D2): 판정 근거가 어디서 왔는지, 이 기준에 필요한 근거 수준, 기록 확인이 필요한지
    # unknown: 판정은 났으나 근거가 기록·답변 어디에서 왔는지 확인할 수 없음 (검증된 기록으로 인정하지 않음)
    evidence_source: Literal["record", "self_report", "unknown", "none"] = "none"
    required_evidence: RequiredEvidence = "self_report_ok"
    # UNVERIFIED(아키텍처 §19 UNKNOWN_UNVERIFIED): 진료 기록이 필요한 기준을 환자 답변(자기 보고)만으로 판정할 수 없어
    # 판정 불가(passes=None)로 남기고 문서 확인을 요청. 답변이 가리킨 값은 self_reported_holds에 보존
    needs_verification: bool = False
    self_reported_holds: Holds | None = None
    self_reported_applicable: bool = True  # 답변상 '해당 없음'이었으면 False


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
    retried_rule_ids: list[str] = []  # 매칭 응답에서 빠져 다시 요청한 규칙 (기록용)
    evaluation_error: str | None = None  # NOT_EVALUATED일 때 실패 단계와 원인
    match_status: MatchStatus | None = None  # 외부 표현. 비워 두면 적격성과 기준별 판정으로 계산

    @model_validator(mode="after")
    def _fill_match_status(self) -> "TrialMatch":
        if self.match_status is None:
            from .status import external_status
            self.match_status = external_status(self)
        return self


# ---------------------------------------------------------------------------
# 4) 질문 생성 에이전트 출력
# ---------------------------------------------------------------------------

class ClarifyingQuestion(BaseModel):
    question_id: str
    question: str = Field(description="환자/보호자가 이해할 수 있는 한국어 질문")
    answer_type: Literal["yes_no", "single_choice", "multi_choice", "number", "date", "free_text"] = Field(
        description="yes_no: 예/아니요 / single_choice: 선택지 중 하나 / multi_choice: 해당하는 것 모두 / "
                    "number: 수치 / date: 날짜 / free_text: 목록·서술이 꼭 필요할 때만"
    )
    options: list[str] = Field(
        default_factory=list,
        description="single_choice·multi_choice의 선택지 2~6개(서로 겹치지 않게). '모름'은 화면이 자동으로 붙이므로 넣지 않음. "
                    "다른 answer_type이면 빈 목록",
    )
    unit: str | None = Field(default=None, description="number일 때 답의 단위(예: mg/dL, kg, 주). 그 외에는 null")
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
    rank: int = Field(description="표시 순서. 코드가 다시 매기므로 1로 두세요.")
    trial_id: str
    eligibility: Eligibility
    match_status: MatchStatus | None = Field(default=None, description="코드가 채움. 비워 두세요.")
    rationale: str
    key_matches: list[str]
    key_concerns: list[str]
    next_steps: list[str]
    physician_review: list[str] = Field(
        default_factory=list,
        description="코드가 채움: 원문에 구체 기준이 없어 의사가 전체 프로토콜과 대조해야 할 항목. 빈 목록으로 두세요.",
    )
    verification_needed: list[str] = Field(
        default_factory=list,
        description="코드가 채움: 진료 기록이 필요한데 환자 답변만 있어 UNVERIFIED로 남은 항목. 빈 목록으로 두세요.",
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
        description="코드가 채움(표시 순서 기준 설명). 빈 문자열로 두세요."
    )
    overall_comment: str


class Recommendation(RecommendationDraft):
    """코드 검증을 거친 최종 추천 (아키텍처 §50: 적격성과 순서를 분리, 시험 간 임상적 우열을 주장하지 않음).

    recommended: 가장 앞 추천 그룹(공개 기준 충족 > 기록 확인 시 충족 가능 > 의료진 검토 필요 > 추가 정보 필요)의 시험 전부
    ranked: 추천 가능한 전체 후보의 표시 순서 (rank = 1, 2, 3 ...; 그룹 -> 모집 상태 -> 남은 미확인 항목 수 -> 후보 순서)
    selection_reason: 표시 순서를 정한 기준 (코드가 작성)
    """
    recommended: list[RankedTrial] = []
    no_recommendation_reason: str | None = None
