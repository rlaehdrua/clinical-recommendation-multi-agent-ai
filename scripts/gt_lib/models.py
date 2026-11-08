"""독립 GT 스키마 (설계 D 2.1-2.7).

세 층:
- G-U (문장): 원문·기준·인용 또는 값 -> 채택 가능 여부, 전해 들은 값·추정·상충 표시
- G-C (기준): 시험·기준 ID -> 해석, 국가·기관 범위, 적용 조건, 임상 판단 필요 여부
- G-P (쌍): 환자 기록 x 시험 -> 기준별 라벨과 사람이 정한 집계 규칙으로 계산한 쌍 라벨

모든 항목은 `input`(엔진에 줄 수 있는 것)과 라벨(엔진에 절대 주지 않는 것)을 분리합니다.
`input` 모델에는 라벨 필드가 없고 extra="forbid"이므로, 어댑터는 구조상 라벨을 받을 수 없습니다.

모든 항목: 라벨러 ID·시각(annotations), needs_expert(+사유), 판정 기록(adjudication), 출처(source).
세트: 블라인딩 증명(blinding). 증명이 없는 세트는 누출 검사와 동결에서 거부됩니다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "gt-schema/1"
FIXTURE_MARKER = "FIXTURE_ONLY"

Layer = Literal["G-U", "G-C", "G-P"]
NonEmpty = Annotated[str, Field(min_length=1)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 공통 메타데이터
# ---------------------------------------------------------------------------

Origin = Literal[
    "clinician_synthetic",           # (c) 임상가가 직접 쓴 합성 기록
    "separated_llm_draft_reviewed",  # (d) 엔진 작업과 분리된 LLM 초안 + 임상가 검수
    "deidentified_real",             # (a) 실제 비식별 기록 (저장소 밖, 해시만)
    "provided_example",              # (b) 사업단 제공 예시
    "ctgov_protocol",                # (e) CT.gov 프로토콜 원문 (G-C)
    "fixture",                       # 도구 테스트용 가짜 데이터. GT 아님
]


class Source(Strict):
    """사례가 어디서 왔는지."""
    origin: Origin
    detail: NonEmpty  # 예: "임상가 L-X가 2026-11 작성, 프롬프트 없음", "NCT06216301 eligibilityCriteria"
    author_id: NonEmpty
    author_independent_of_engine_work: bool  # 작성자가 엔진 A·B·C 작업에 참여하지 않았는가
    created_at: datetime
    # 이 사례가 기존 세트(held-out 1-6, 회귀, 테스트)에서 오지 않았다는 작성자 진술
    not_derived_from_existing_sets: bool


class BlindingAttestation(Strict):
    """세트 단위 블라인딩 증명 (D 2.6). Literal 값만 허용하므로 '보았다'는 증명은 만들 수 없습니다."""
    attested_by: NonEmpty
    attested_at: datetime
    labelers_saw_engine_output: Literal[False]
    adjudicator_saw_engine_output: Literal[False]
    authors_saw_engine_dictionaries: Literal[False]  # 엔진 동의어 사전·범주 목록을 보고 쓰지 않음
    engine_fields_absent: Literal[True]
    storage: NonEmpty  # 원문 보관 위치 (예: "저장소 밖 비공개 저장소, 해시만 커밋")
    statement: Annotated[str, Field(min_length=20)]


class NeedsExpert(Strict):
    flag: bool
    reason: str | None = None

    @model_validator(mode="after")
    def _reason_when_flagged(self):
        if self.flag and not (self.reason or "").strip():
            raise ValueError("needs_expert.flag=true이면 reason이 필요합니다")
        return self


class Span(Strict):
    """원문(input.text 또는 기록) 안의 문자 오프셋 [start, end)."""
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _order(self):
        if self.end < self.start:
            raise ValueError("span end < start")
        return self


AdjudicationStatus = Literal["agreed", "adjudicated", "ambiguous", "pending"]


class Adjudication(Strict):
    """agreed: 두 라벨러 일치·확신도 3 / adjudicated: 판정자가 결정 / ambiguous: 판정자도 판정 불가 (주 지표 분모 제외)
    / pending: 아직 판정 전 (동결 불가)."""
    status: AdjudicationStatus
    adjudicator_id: str | None = None
    adjudicated_at: datetime | None = None
    reason: str | None = None
    log_ref: str | None = None  # adjudication_log.csv의 log_id

    @model_validator(mode="after")
    def _needs_fields(self):
        if self.status in ("adjudicated", "ambiguous"):
            missing = [k for k in ("adjudicator_id", "adjudicated_at", "reason") if not getattr(self, k)]
            if missing:
                raise ValueError(f"adjudication.status={self.status}에는 {missing}이 필요합니다")
        return self


L = TypeVar("L")


class Annotation(Strict, Generic[L]):
    labeler_id: NonEmpty
    labeled_at: datetime
    confidence: Annotated[int, Field(ge=1, le=3)]  # 1-3, 3 미만은 판정 대상
    evidence_spans: list[Span] = []
    note: str | None = None
    label: L


# ---------------------------------------------------------------------------
# G-U (문장)
# ---------------------------------------------------------------------------

class UtteranceInput(Strict):
    """엔진에 줄 수 있는 입력. 라벨 정보 없음.

    kind=conflict: (기준, 인용, 원문) -> evidence_directness.check
    kind=value: (필드, 값, 단위, 원문) -> value_grounding.ground / ground_lab
    """
    kind: Literal["conflict", "value"]
    text: NonEmpty
    criterion: str | None = None
    quote: str | None = None          # Matcher가 골랐다고 가정한 인용 (원문의 부분 문자열)
    holds: Literal["yes", "no"] = "no"  # 인용이 기준 '해당'(yes) / '해당 없음'(no) 근거로 쓰였다고 가정
    field: str | None = None
    value: float | int | str | None = None
    unit: str | None = None
    reference_date: str | None = None

    @model_validator(mode="after")
    def _kind_fields(self):
        if self.kind == "conflict":
            if not self.criterion or not self.quote:
                raise ValueError("kind=conflict에는 criterion과 quote가 필요합니다")
            if self.quote not in self.text:
                raise ValueError("quote는 text의 부분 문자열이어야 합니다")
        else:
            if not self.field or self.value is None:
                raise ValueError("kind=value에는 field와 value가 필요합니다")
        return self


class UtteranceLabel(Strict):
    """decision: accept = 이 인용/값으로 확정해도 됨, hold = 확정하면 안 됨."""
    decision: Literal["accept", "hold"]
    category: NonEmpty  # 예: hearsay_or_uncertain, contradiction, family_history, normal_table ...
    hearsay: bool       # 전해 들은 값·불확실 진술
    approx: bool        # 범위·추정 표현 ("~10", "around", "10-12")
    conflict: bool      # 같은 개념에 '있음'과 '없음' 진술 공존
    threshold_crossing: bool | None = None  # 범위가 기준 문턱을 가로지르는가 (hearsay/approx 값에서)
    concept_cluster: str | None = None      # 상충 질환 묶음 (CL-2, 엔진 사전과 독립)
    source_priority_note: str | None = None  # 진술 출처 우선순위 (라벨에 반영하지 않고 따로 기록, D 2.3)

    @model_validator(mode="after")
    def _consistency(self):
        if self.conflict and self.decision != "hold":
            raise ValueError("conflict=true이면 decision=hold여야 합니다")
        if self.hearsay and self.threshold_crossing and self.decision != "hold":
            raise ValueError("문턱을 가로지르는 전해 들은 값은 decision=hold여야 합니다 (D 2.3)")
        return self


# ---------------------------------------------------------------------------
# G-C (기준)
# ---------------------------------------------------------------------------

class FileRef(Strict):
    path: NonEmpty
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CriterionInput(Strict):
    trial_id: NonEmpty
    criterion_id: NonEmpty  # 동결 파싱본의 rule_id
    criterion_text: NonEmpty
    trial_source: NonEmpty  # 예: "ClinicalTrials.gov NCT06216301 eligibilityCriteria"
    parsed_store_ref: FileRef | None = None  # 기준 ID 대응을 묶는 파싱본 해시


class CountryScope(Strict):
    scoped: bool  # 국가·지역 범위가 있는 기준인가
    scope_unit: Literal["institution_country", "patient_residence", "unclear", "not_applicable"]
    countries: list[str] = []  # ISO 3166-1 alpha-2 권장
    applies_when: Literal["inside", "outside", "not_applicable"] = "not_applicable"
    note: str | None = None

    @model_validator(mode="after")
    def _scoped(self):
        if self.scoped and (self.scope_unit == "not_applicable" or self.applies_when == "not_applicable"):
            raise ValueError("scoped=true이면 scope_unit과 applies_when을 정해야 합니다")
        return self


class StructuredReading(Strict):
    field: NonEmpty
    operator: Literal[">=", ">", "<=", "<", "==", "!=", "in", "not_in"]
    value: NonEmpty
    unit: str | None = None


class CriterionLabel(Strict):
    interpretation: NonEmpty
    country_scope: CountryScope
    applicability_condition: str | None = None
    needs_clinical_judgement: bool
    structured: StructuredReading | None = None


# ---------------------------------------------------------------------------
# G-P (쌍)
# ---------------------------------------------------------------------------

class GridCell(Strict):
    """국가 격자 (D 3.3, EN-5)."""
    gc_item_id: NonEmpty     # G-C 세트의 item_id
    criterion_id: NonEmpty   # 그 기준의 rule_id (엔진 출력 deciding_criteria와 대조)
    country_cell: Literal["in_scope", "out_of_scope", "unknown"]
    age_band: Literal["below_both", "between", "above_both"]


class PairInput(Strict):
    patient_id: NonEmpty
    record_ref: FileRef  # 환자 기록 (원문은 저장소 밖일 수 있음)
    trial_id: NonEmpty
    parsed_store_ref: FileRef | None = None
    institution_country: str | None = None
    grid: GridCell | None = None


CriterionStatus = Literal["MET", "NOT_MET", "UNKNOWN_FROM_RECORD", "NEEDS_CLINICIAN"]
GTPairStatus = Literal["ELIGIBLE", "INELIGIBLE", "NOT_DETERMINABLE", "NEEDS_CLINICIAN"]


class CriterionStatusLabel(Strict):
    criterion_id: NonEmpty
    status: CriterionStatus
    evidence_spans: list[Span] = []


class PairLabel(Strict):
    criteria: Annotated[list[CriterionStatusLabel], Field(min_length=1)]
    pair_status: GTPairStatus
    aggregation_rule_version: NonEmpty  # CL-9에서 승인된 집계 규칙 버전


# ---------------------------------------------------------------------------
# 항목·세트
# ---------------------------------------------------------------------------

class _Record(Strict):
    item_id: NonEmpty
    needs_expert: NeedsExpert
    adjudication: Adjudication
    source: Source

    @model_validator(mode="after")
    def _final_vs_status(self):
        final = getattr(self, "final")
        st = self.adjudication.status
        if st in ("agreed", "adjudicated") and final is None:
            raise ValueError(f"{self.item_id}: adjudication.status={st}이면 final 라벨이 필요합니다")
        if st in ("ambiguous", "pending") and final is not None:
            raise ValueError(f"{self.item_id}: adjudication.status={st}이면 final은 비어 있어야 합니다")
        ids = [a.labeler_id for a in getattr(self, "annotations")]
        if self.adjudication.adjudicator_id and self.adjudication.adjudicator_id in ids:
            raise ValueError(f"{self.item_id}: 판정자는 라벨러와 다른 사람이어야 합니다")
        return self


class UtteranceRecord(_Record):
    layer: Literal["G-U"]
    input: UtteranceInput
    annotations: Annotated[list[Annotation[UtteranceLabel]], Field(min_length=1)]
    final: UtteranceLabel | None = None


class CriterionRecord(_Record):
    layer: Literal["G-C"]
    input: CriterionInput
    annotations: Annotated[list[Annotation[CriterionLabel]], Field(min_length=1)]
    final: CriterionLabel | None = None


class PairRecord(_Record):
    layer: Literal["G-P"]
    input: PairInput
    annotations: Annotated[list[Annotation[PairLabel]], Field(min_length=1)]
    final: PairLabel | None = None


AnyRecord = Annotated[Union[UtteranceRecord, CriterionRecord, PairRecord], Field(discriminator="layer")]
INPUT_MODELS = {"G-U": UtteranceInput, "G-C": CriterionInput, "G-P": PairInput}


class GTSet(Strict):
    schema_version: Literal["gt-schema/1"]
    set_id: NonEmpty
    gt_version: NonEmpty  # 예: gt-v1.0
    layer: Layer
    fixture_only: bool
    description: NonEmpty
    blinding: BlindingAttestation | None = None
    protocol_ref: FileRef | None = None
    items: list[AnyRecord]

    @model_validator(mode="after")
    def _set_rules(self):
        if self.fixture_only != self.set_id.startswith(FIXTURE_MARKER):
            raise ValueError(f"fixture_only 세트만 set_id가 {FIXTURE_MARKER}로 시작합니다 (그 반대도 금지)")
        if not self.fixture_only and any(r.source.origin == "fixture" for r in self.items):
            raise ValueError("fixture 출처 항목은 fixture_only 세트에만 둘 수 있습니다")
        ids = [r.item_id for r in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("item_id 중복")
        bad = [r.item_id for r in self.items if r.layer != self.layer]
        if bad:
            raise ValueError(f"세트 layer={self.layer}와 다른 항목: {bad[:5]}")
        return self


def freeze_problems(s: GTSet, *, allow_fixture: bool = False) -> list[str]:
    """동결 전에 갖춰야 할 조건 (스키마 검증과 별도). 비어 있으면 동결 가능."""
    p = []
    if s.fixture_only and not allow_fixture:
        p.append(f"{s.set_id}: fixture_only 세트는 GT로 동결할 수 없습니다")
    if s.blinding is None:
        p.append(f"{s.set_id}: 블라인딩 증명(blinding)이 없습니다")
    for r in s.items:
        labelers = {a.labeler_id for a in r.annotations}
        if len(labelers) < 2:
            p.append(f"{r.item_id}: 서로 다른 라벨러 2명 이상의 독립 라벨이 필요합니다")
        if r.adjudication.status == "pending":
            p.append(f"{r.item_id}: 판정 대기(pending)")
        if r.adjudication.status == "agreed":
            finals = {a.label.model_dump_json() for a in r.annotations}
            if len(finals) > 1:
                p.append(f"{r.item_id}: 라벨이 다른데 status=agreed (판정자 결정 필요)")
            if any(a.confidence < 3 for a in r.annotations):
                p.append(f"{r.item_id}: 확신도 3 미만인데 status=agreed (판정자 결정 필요)")
        if not r.source.author_independent_of_engine_work and r.source.origin != "fixture":
            p.append(f"{r.item_id}: 작성자가 엔진 작업과 독립이 아닙니다")
        if not r.source.not_derived_from_existing_sets and r.source.origin != "fixture":
            p.append(f"{r.item_id}: 기존 세트에서 파생되지 않았다는 진술이 없습니다")
    return p


def load_set(path) -> GTSet:
    from pathlib import Path
    return GTSet.model_validate_json(Path(path).read_text(encoding="utf-8"))


def export_json_schemas() -> dict[str, dict]:
    """gt/schema/*.schema.json 내용."""
    return {
        "gt_set.schema.json": GTSet.model_json_schema(),
        "g_u_record.schema.json": UtteranceRecord.model_json_schema(),
        "g_c_record.schema.json": CriterionRecord.model_json_schema(),
        "g_p_record.schema.json": PairRecord.model_json_schema(),
    }
