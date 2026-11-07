"""합성 코호트 생성기의 입력(슬롯 사전·소스 카탈로그·생성 설정)과 산출물(코호트) 스키마.

산출물은 public(정책이 읽어도 되는 부분)과 oracle(시뮬레이터만 읽는 정답)으로 나뉩니다.
정책 코드는 policy_view()로 public만 받아야 하며, oracle의 숨긴 값·기준 정답·쌍 라벨을 보면 안 됩니다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..schemas import Holds, RequiredEvidence

CORE_SOURCE = "core"  # 나이·성별처럼 항상 공개되는 슬롯 (확인 행동 없음)
BindingStatus = Literal["auto", "reviewed", "draft", "needs_review", "excluded"]
USABLE = ("auto", "reviewed")  # 정답 계산에 쓰는 상태. draft·needs_review는 사람 검수 전이므로 쓰지 않음
OVERRIDE_KEYS = {"field", "operator", "value", "unit"}
LAB_AGE = "lab_age_days"  # 환자의 마지막 혈액검사가 며칠 전인지 (항상 공개). 기간 조건이 있는 검사 기준에만 쓰임

# 임상의 판단이 필요한 슬롯 (단일 정의). 이 슬롯을 쓰는 기준은 값을 만들지 않고 정답·쌍 라벨도 만들지 않습니다.
# - 접두어 judge.* : 현황판 web/cohort_board.EXPERT_SLOT_PREFIX와 같은 값 (tests/test_cohort_generator.py에서 일치 검사)
# - EXPERT_SLOTS   : NSCLC 슬롯 후보표(data/nsclc/cohort/slot_candidates.csv)에서 adjudication_candidate=expert인 슬롯
#                    (2026-11-07 결정: judge.* 접두어 ∪ 이 6개). 임상 검수로 바뀌면 여기만 고칩니다.
EXPERT_SLOT_PREFIX = "judge."
EXPERT_SLOTS = frozenset({"dx.disease_setting", "tx.recent_radiotherapy", "cm.cardiac", "other.tumor_tissue",
                          "judge.life_expectancy", "judge.investigator_unsuitable"})


def is_expert_slot(slot: str | None) -> bool:
    return bool(slot) and (slot.startswith(EXPERT_SLOT_PREFIX) or slot in EXPERT_SLOTS)


# 쌍 라벨: UNLABELED = 확정 라벨을 만들지 않음 (사유는 OraclePart.label_reasons)
PairLabel = Literal["ELIGIBLE", "INELIGIBLE", "UNLABELED"]
REASON_EXPERT = "expert_review"  # 임상의 판단 기준이 있음
REASON_UNBOUND = "unbound_rules"  # 검수 전(미연결) 기준이 남음 - 충족으로 간주하지 않음


# ---------------------------------------------------------------------------
# 슬롯 사전
# ---------------------------------------------------------------------------

class SlotSpec(BaseModel):
    slot_id: str = Field(description="예: age, sex, ecog, lab.hemoglobin, tx.prior_steroid")
    kind: Literal["numeric", "categorical"]
    category: str = Field(description="결측률을 정하는 범주 (Rule.category와 같은 이름)")
    source: str = Field(description="이 슬롯을 드러내는 소스 ID, 항상 공개면 'core'")
    unit: str | None = None
    values: list[str] = Field(default_factory=list, description="범주형 슬롯의 값 목록")


class Cond(BaseModel):
    """추가 조건 하나 (범주형 슬롯만)."""
    slot: str
    op: Literal["in", "not_in"]
    values: list[str]


class Binding(BaseModel):
    """기준 하나 -> 주 슬롯 + 추가 조건(also) -> 진위 함수.

    also가 있으면 주 조건과 추가 조건을 combine(all = 모두 성립, any = 하나라도 성립)으로 묶습니다.
    예: Hb > 8.0 그리고 최근 수혈·성장인자 없음 (all) / 원발성 면역결핍 또는 약물로 인한 심한 면역저하 (any).
    """
    trial_id: str
    rule_id: str
    kind: Literal["inclusion", "exclusion"]
    category: str
    text: str
    status: BindingStatus
    slot: str | None = None
    truth: Literal["engine", "categorical"] | None = None  # engine = 기존 규칙 엔진, categorical = 값 집합
    op: Literal["in", "not_in"] | None = None
    values: list[str] = Field(default_factory=list, description="categorical: 이 값(in)/이 값 밖(not_in)이면 기준 성립")
    required_evidence: RequiredEvidence = "clinical_record"
    # engine 판정 때 원본 기준의 일부를 바꿔 읽음 (예: 단위 오기 10^12/L -> 10^9/L, 80 g/L -> 8.0 g/dL).
    # 원문 해석을 바꾸는 것이므로 note에 사유를 적고 사람이 검수해야 함
    rule_override: dict[str, str] | None = None
    also: list[Cond] = Field(default_factory=list)
    combine: Literal["all", "any"] = "all"
    # engine 검사 조건의 유효 기간(일). 환자의 마지막 검사(lab_age_days)가 이보다 오래되면 값이 있어도 판정에 쓰지 않음(미확인)
    time_window_days: int | None = None
    note: str | None = None

    @property
    def key(self) -> str:
        return f"{self.trial_id}:{self.rule_id}"

    def slots_used(self) -> list[str]:
        return ([self.slot] if self.slot else []) + [c.slot for c in self.also]

    def needs_expert(self) -> bool:
        """임상의 판단 슬롯을 하나라도 쓰는 기준 (주 슬롯·추가 조건 모두)."""
        return any(is_expert_slot(s) for s in self.slots_used())

    @model_validator(mode="after")
    def _override_keys(self):
        bad = set(self.rule_override or {}) - OVERRIDE_KEYS
        if bad:
            raise ValueError(f"{self.key}: rule_override에 쓸 수 없는 키 {sorted(bad)}")
        if self.rule_override and self.truth != "engine":
            raise ValueError(f"{self.key}: rule_override는 engine 연결에서만 씀")
        return self

    @model_validator(mode="after")
    def _usable_is_complete(self):
        if self.status in USABLE:
            if not self.slot or not self.truth:
                raise ValueError(f"{self.key}: {self.status} 연결에는 slot과 truth가 필요")
            if self.truth == "categorical" and (not self.op or not self.values):
                raise ValueError(f"{self.key}: categorical 연결에는 op와 values가 필요")
        return self


class SlotDictionary(BaseModel):
    version: int = 1
    built_from: list[str] = Field(default_factory=list, description="초안을 만든 시험 ID")
    slots: dict[str, SlotSpec]
    bindings: list[Binding]

    @model_validator(mode="after")
    def _slots_exist(self):
        for b in self.bindings:
            if b.status not in USABLE:
                continue
            for s in b.slots_used() + ([LAB_AGE] if b.time_window_days is not None else []):
                if s not in self.slots:
                    raise ValueError(f"{b.key}: 슬롯 {s}이 slots에 없음")
            conds = ([Cond(slot=b.slot, op=b.op, values=b.values)] if b.truth == "categorical" else []) + b.also
            for c in conds:
                spec = self.slots[c.slot]
                bad = set(c.values) - set(spec.values)
                if spec.kind != "categorical" or bad:
                    raise ValueError(f"{b.key}: 슬롯 {c.slot}의 값 목록에 없는 값 {sorted(bad)}")
        return self

    def usable(self, trial_id: str) -> list[Binding]:
        return [b for b in self.bindings if b.trial_id == trial_id and b.status in USABLE]

    def unbound(self, trial_id: str) -> list[Binding]:
        return [b for b in self.bindings if b.trial_id == trial_id and b.status not in USABLE and b.status != "excluded"]


# ---------------------------------------------------------------------------
# 소스 카탈로그
# ---------------------------------------------------------------------------

class SourceSpec(BaseModel):
    label: str
    evidence: Literal["record", "self_report", "physician"]
    cost_min: float = Field(gt=0, description="기본 비용(분). 가정값이며 민감도 분석 대상")
    cost_range: tuple[float, float]
    reveals: list[str] = Field(description="드러나는 슬롯 ID 패턴 (fnmatch, 예: 'lab.*')")
    basis: str = Field(description="비용 근거 또는 '가정' 표시")


class SourceCatalog(BaseModel):
    version: int = 1
    sources: dict[str, SourceSpec]


# ---------------------------------------------------------------------------
# 생성 설정
# ---------------------------------------------------------------------------

class Prior(BaseModel):
    dist: Literal["normal", "uniform", "categorical"]
    a: float | None = None  # normal: 평균 / uniform: 하한
    b: float | None = None  # normal: 표준편차 / uniform: 상한
    lo: float | None = None
    hi: float | None = None
    decimals: int = 1
    unit: str | None = None  # 수치 슬롯이면 슬롯 사전의 단위와 같아야 함
    probs: dict[str, float] = Field(default_factory=dict)
    basis: str = "가정"

    @model_validator(mode="after")
    def _complete(self):
        if self.dist == "categorical":
            if not self.probs or abs(sum(self.probs.values()) - 1) > 1e-6:
                raise ValueError("categorical 사전 분포의 확률 합은 1이어야 함")
        elif self.a is None or self.b is None:
            raise ValueError(f"{self.dist} 사전 분포에는 a, b가 필요")
        return self


class Cluster(BaseModel):
    trials: list[str]
    priors: dict[str, Prior]


class Knobs(BaseModel):
    trials_per_patient: int = Field(ge=1)
    cost_heterogeneity: float = Field(1.0, ge=0, le=1, description="0 = 모든 소스 같은 비용, 1 = 카탈로그 비용 그대로")
    grouping: Literal["source", "per_slot"] = "source"  # per_slot: 소스 묶음 없이 슬롯마다 따로 확인 (묶음 효과 제거)
    enrollment_remaining: int | None = Field(None, ge=0, description="시험별 남은 정원, None = 제한 없음")
    missing_rate: dict[str, float] = Field(default_factory=lambda: {"default": 0.3})


NULL_KNOBS = {"trials_per_patient": 1, "cost_heterogeneity": 0.0, "grouping": "per_slot", "enrollment_remaining": None}


class Scenario(BaseModel):
    scenario_id: str
    cluster: str
    n_patients: int = Field(ge=1)
    replicates: int = Field(ge=1)
    dev_replicates: int = Field(ge=0, description="앞의 이 수만큼은 dev, 나머지는 test")
    # 영 시나리오: 시험 겹침·비용 차이·소스 묶음·정원을 없앤 대조 조건 (NULL_KNOBS 강제).
    # 정책 간 성능이 같다는 보장은 없음 (환자마다 남은 미확인 수가 다르면 차이가 날 수 있어 버그 판정에 쓰지 않음)
    null: bool = False
    knobs: Knobs

    def effective_knobs(self) -> Knobs:
        return self.knobs.model_copy(update=NULL_KNOBS) if self.null else self.knobs


class GeneratorConfig(BaseModel):
    version: int = 1
    reference_date: str
    base_seed: int
    slot_dictionary: str
    source_catalog: str
    budgets_min: list[int] = Field(default_factory=lambda: [30, 60, 120, 240])
    max_resample: int = 20
    clusters: dict[str, Cluster]
    scenarios: list[Scenario]


# ---------------------------------------------------------------------------
# 산출물
# ---------------------------------------------------------------------------

class RuleRef(BaseModel):
    rule_id: str
    kind: Literal["inclusion", "exclusion"]
    slot: str
    extra_slots: list[str] = Field(default_factory=list)  # also 조건의 슬롯
    time_window_days: int | None = None
    required_evidence: RequiredEvidence
    cohort: str | None = None  # OR 경로(코호트) 전용 기준이면 그 이름. None = 모든 경로 공통

    def slots(self) -> list[str]:
        return [self.slot, *self.extra_slots]


class EffectiveSource(BaseModel):
    source_id: str
    base_source: str
    evidence: Literal["record", "self_report", "physician"]
    cost_min: float
    reveals: list[str]  # 이 코호트에서 쓰는 슬롯 ID (패턴 아님)


class PublicPatient(BaseModel):
    patient_id: str
    candidate_trials: list[str]
    known: dict[str, float | str]
    unknown_slots: list[str]


class PublicPart(BaseModel):
    trials: list[str]
    rules: dict[str, list[RuleRef]]  # 시험 -> 연결된 기준 (임상의 판단 기준 포함, 그 기준은 initial_holds가 항상 unknown)
    unbound_rules: dict[str, list[str]]  # 시험 -> 검수 전이라 정답에서 빠진 기준 (충족으로 간주하지 않음 -> 쌍 라벨 없음)
    expert_rules: dict[str, list[str]] = Field(default_factory=dict)  # 시험 -> 임상의 판단 기준 (값·정답 없음)
    # 시험 -> OR 경로(코호트). 기준이 모두 미연결인 경로도 포함. 빈 목록 = 코호트 구분 없음 (경로 하나)
    paths: dict[str, list[str]] = Field(default_factory=dict)
    enrollment_remaining: dict[str, int | None]
    sources: dict[str, EffectiveSource]
    budgets_min: list[int]
    patients: list[PublicPatient]
    initial_holds: dict[str, Holds]  # "환자|시험|기준" -> 공개 슬롯으로 판정한 성립 여부, 숨긴 슬롯이면 unknown


class OraclePart(BaseModel):
    hidden: dict[str, dict[str, float | str]]  # 환자 -> 숨긴 슬롯의 실제 값
    truth: dict[str, Holds]  # "환자|시험|기준" -> 실제 성립 여부 (yes/no). 임상의 판단 기준은 unknown (정답 없음)
    pair_label: dict[str, PairLabel]  # "환자|시험"
    label_reasons: dict[str, list[str]] = Field(default_factory=dict)  # UNLABELED 쌍 -> [expert_review, unbound_rules]
    # INELIGIBLE은 미연결·판단 기준이 남아도 모든 OR 경로에서 불통과가 확인되면 허용 (2026-11-07 결정)
    # 연결된 기준 중 임상의 판단 기준을 뺀 나머지만으로 본 OR 경로별 결과 (generate.label_pair).
    # pass = 불통과 없음, or_partial = 일부 경로만 불통과, fail = 모든 경로 불통과 (fail이면 쌍 라벨 INELIGIBLE)
    bound_result: dict[str, Literal["pass", "or_partial", "fail"]] = Field(default_factory=dict)


class Cohort(BaseModel):
    format_version: int = 1
    cohort_id: str
    config_hash: str
    scenario_id: str
    replicate: int
    split: Literal["dev", "test"]
    seed: int
    cluster: str
    reference_date: str
    null: bool
    knobs: Knobs
    # assume_pass = 이전 형식(미연결 기준을 충족으로 간주, cohort-gen-1). unlabeled = 미연결 기준이 남은 쌍은 라벨 없음
    unbound_policy: Literal["assume_pass", "unlabeled"] = "unlabeled"
    public: PublicPart
    oracle: OraclePart


def policy_view(cohort: Cohort) -> PublicPart:
    """정책에 넘길 부분. 숨긴 값·정답·쌍 라벨(oracle)은 포함하지 않습니다."""
    return cohort.public.model_copy(deep=True)
