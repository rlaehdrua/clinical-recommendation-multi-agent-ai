"""DCV+C 공통 엔진 인터페이스 I-1~I-9 (WP0). **PROVISIONAL - 팀 승인 전**.

docs/audit/03_architecture_and_work_packages.md §3의 제안 계약을 pydantic 모델로 옮겼습니다. 설명은
docs/engine_contracts.md에 있습니다.

이름 충돌 방지
- 기존 `decision_state.Action`, `incremental.CriterionResult`, `schemas.TrialMatch`와 다른 모듈입니다.
  같은 파일에서 함께 쓸 때는 `from ctrec.engine import contracts as C`처럼 모듈 이름으로 구분합니다.
- 기존 모델은 바꾸지 않고, 필요한 값은 어댑터(engine.cohort_state)가 읽어서 옮깁니다.

설계 원칙
1. 환자는 불투명 별칭(patient_alias)으로만 가리킵니다. 원래 환자·케이스 ID는 이 계약의 어느 필드에도 들어가지 않습니다.
2. 판정(CriterionResult, PairStatus)은 판정 계층만 만듭니다. 모델은 frozen이고 시퀀스는 tuple이라 정책 코드가 받은
   객체를 고칠 수 없습니다. 전문가 판단 기준은 임상의(clinician)가 아니면 MET/NOT_MET이 될 수 없습니다.
3. 비용은 세 축을 분리합니다: 코디네이터 능동 작업시간(분), 임상의 작업시간(분), LLM 비용(USD)·호출 수.
   환자 응답 대기(지연)는 능동 시간이 아니므로 별도 필드입니다.
4. 정보원(InformationSource) 단위로 비용을 한 번만 청구할 수 있게 출처 키(charge_key)를 둡니다.
5. 성공 확률 같은 근거 없는 추정치는 계약에 두지 않습니다(시뮬레이터 내부 응답 모델은 정책 계층에 보이지 않음).
6. 하위 호환: 새 필드는 기본값을 두고 추가만 합니다. schema_version 문자열로 구분합니다.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt, field_validator, model_validator

CONTRACT_STATUS = "PROVISIONAL"
CONTRACT_VERSION = "dcvc.engine.contracts/0.2"
# 읽을 수 있는 계약 버전 (0.2는 0.1에 기본값 있는 필드만 추가 + 쌍 상태 파생을 보수적으로 바꿈). 변경 기록은
# docs/dev/contract_v02_changelog.md
SUPPORTED_CONTRACT_VERSIONS = frozenset({"dcvc.engine.contracts/0.1", CONTRACT_VERSION})
DECISION_RECORD_SCHEMA = "ctrec.decision_record/v2"

# 불투명 별칭 형식: 접두사 + 16진수. 원래 ID의 일부(rescue/cleanup, 기준 ID 등)를 담을 수 없음
ALIAS_PATTERN = r"^PA-[0-9a-f]{16}$"
# 사유 코드: 소문자·숫자·밑줄만 (자유 서술·환자 원문이 들어가지 않게)
CODE_PATTERN = r"^[a-z0-9_]{1,64}$"
# 기준 키 "<trial_id>:<rule_id>"
CRITERION_KEY_PATTERN = r"^[A-Za-z0-9._-]{1,64}:[A-Za-z0-9._-]{1,32}$"
ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def canonical_hash(obj) -> str:
    """정렬된 JSON의 SHA-256 앞 16자리 (pydantic 모델은 JSON 모드로 덤프)."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# I-1 PairKey / PairStatus
# ---------------------------------------------------------------------------

class PairKey(_Frozen):
    patient_alias: str = Field(pattern=ALIAS_PATTERN)
    trial_id: str = Field(pattern=ID_PATTERN)

    @property
    def key(self) -> str:
        return f"{self.patient_alias}|{self.trial_id}"


class PairStatus(str, Enum):
    CONFIRMED_ELIGIBLE = "CONFIRMED_ELIGIBLE"  # 필수 기준이 모두 충족이고 남은 확인 없음
    CONFIRMED_INELIGIBLE = "CONFIRMED_INELIGIBLE"  # 근거가 확정된 불충족 (자기 보고만으로는 아님)
    VERIFICATION_PENDING = "VERIFICATION_PENDING"  # 정보는 있으나 문서 확인·상충 해결이 남음
    INFO_MISSING = "INFO_MISSING"  # 판정에 필요한 정보가 없음 (질문·문서로 확보 가능)
    EXPERT_REVIEW = "EXPERT_REVIEW"  # 자동으로 정할 수 없고 임상의 판단만 남음
    BLOCKED_FAILURE = "BLOCKED_FAILURE"  # 확보 행동이 실패·무응답으로 소진되어 보류
    NOT_EVALUATED = "NOT_EVALUATED"  # 평가하지 못함 (부적격 아님)


CONFIRMED = frozenset({PairStatus.CONFIRMED_ELIGIBLE, PairStatus.CONFIRMED_INELIGIBLE})

# 허용 전이. 같은 상태 유지는 항상 허용. 확정 상태에서 반대 확정으로 바로 가지 않고, 새 근거·상충이 생기면
# 확인(VERIFICATION_PENDING)·검토(EXPERT_REVIEW)를 거칩니다. NOT_EVALUATED는 재평가로 어디로든 갈 수 있음.
_OPEN = {PairStatus.VERIFICATION_PENDING, PairStatus.INFO_MISSING, PairStatus.EXPERT_REVIEW,
         PairStatus.BLOCKED_FAILURE, PairStatus.NOT_EVALUATED}
ALLOWED_TRANSITIONS: dict[PairStatus, frozenset[PairStatus]] = {
    PairStatus.NOT_EVALUATED: frozenset(PairStatus),
    PairStatus.INFO_MISSING: frozenset(PairStatus),
    PairStatus.VERIFICATION_PENDING: frozenset(PairStatus),
    PairStatus.BLOCKED_FAILURE: frozenset(PairStatus),
    PairStatus.EXPERT_REVIEW: frozenset(PairStatus),
    PairStatus.CONFIRMED_ELIGIBLE: frozenset({PairStatus.CONFIRMED_ELIGIBLE, PairStatus.VERIFICATION_PENDING,
                                              PairStatus.EXPERT_REVIEW, PairStatus.NOT_EVALUATED}),
    PairStatus.CONFIRMED_INELIGIBLE: frozenset({PairStatus.CONFIRMED_INELIGIBLE, PairStatus.VERIFICATION_PENDING,
                                                PairStatus.EXPERT_REVIEW, PairStatus.NOT_EVALUATED}),
}
# 정책 계층이 스스로 만들 수 있는 전이: 판정이 아닌 운영 상태(행동 소진에 따른 보류)뿐
POLICY_TRANSITIONS: frozenset[tuple[PairStatus, PairStatus]] = frozenset(
    (s, PairStatus.BLOCKED_FAILURE) for s in (PairStatus.INFO_MISSING, PairStatus.VERIFICATION_PENDING))


class TransitionError(ValueError):
    pass


def check_transition(before: PairStatus, after: PairStatus, *, authority: Literal["adjudication", "policy"]) -> None:
    """전이가 허용되지 않으면 TransitionError. 정책 계층은 POLICY_TRANSITIONS 외의 상태 변경을 할 수 없음."""
    before, after = PairStatus(before), PairStatus(after)
    if before == after:
        return
    if after not in ALLOWED_TRANSITIONS[before]:
        raise TransitionError(f"허용되지 않는 쌍 상태 전이: {before.value} -> {after.value}")
    if authority == "policy" and (before, after) not in POLICY_TRANSITIONS:
        raise TransitionError(f"정책 계층은 판정 상태를 바꿀 수 없음: {before.value} -> {after.value}")


GapKind = Literal["none", "missing", "unverified", "physician_review", "conflict", "stale"]
# 쌍에 남은 작업 종류 (pending_kinds). gap 값 + 불충족 근거 미검증 + 확보 수단 소진
PendingKind = Literal["missing", "stale", "unverified", "conflict", "physician_review", "exclusion_unverified",
                      "sources_exhausted"]
_GAP_INFO = frozenset({"missing", "stale"})  # 정보 없음 -> INFO_MISSING
_GAP_VERIFY = frozenset({"unverified", "conflict"})  # 확인 필요 -> VERIFICATION_PENDING
_GAP_EXPERT = frozenset({"physician_review"})  # 임상의 판단 -> EXPERT_REVIEW
_KNOWN_GAPS = _GAP_INFO | _GAP_VERIFY | _GAP_EXPERT


def _gaps(gaps) -> set[str]:
    g = set(gaps) - {"none"}
    bad = g - _KNOWN_GAPS
    if bad:
        raise ValueError(f"알 수 없는 gap 값 {len(bad)}개")
    return g


def _open_status(g: set[str]) -> PairStatus | None:
    """남은 gap이 가리키는 다음 작업 (우선순위: 정보 없음 > 확인 필요 > 임상의 판단). gap이 없으면 None."""
    if g & _GAP_INFO:
        return PairStatus.INFO_MISSING
    if g & _GAP_VERIFY:
        return PairStatus.VERIFICATION_PENDING
    if g & _GAP_EXPERT:
        return PairStatus.EXPERT_REVIEW
    return None


def derive_pair_status(eligibility: str, gaps: set[str] | frozenset[str] | list[str] = (),
                       mismatch: Literal["none", "provisional", "confirmed"] | None = None,
                       exhausted: bool = False, required_criteria: int | None = None) -> PairStatus:
    """기존 판정(TrialMatch.eligibility) + 판정에 적용된 경로의 필수 기준 gap -> PairStatus (M5). 판정을 바꾸지 않음.

    gaps는 판정에 적용된 기준(decision_state requirement == "required")의 gap만 넘깁니다. 다른 코호트(OR의 다른 갈래,
    requirement == "alternative_cohort")와 절차 기준(waivable)의 gap은 넘기지 않습니다 (engine.cohort_state).

    0.2 안전 경계 (확정은 내리기만 하고 올리지 않음)
    - ELIGIBLE: 필수 기준에 gap이 하나라도 있으면 확정 아님. 상태는 UNCERTAIN과 같은 우선순위
      (정보 없음 missing/stale > 확인 필요 unverified/conflict > 임상의 판단 physician_review).
    - INELIGIBLE: 불충족 근거가 문서로 검증됨(mismatch == "confirmed")일 때만 CONFIRMED_INELIGIBLE.
      provisional·none·None(모름)은 VERIFICATION_PENDING (자기 보고만으로는 확정 부적격이 아님).
    - ELIGIBLE인데 판정에 적용된 필수 기준이 0개(required_criteria == 0): 확정 근거가 없으므로 NOT_EVALUATED.
      기존 Matcher는 기준 목록이 비면(파싱 실패·코호트 기준만 있음 등) ELIGIBLE을 돌려주므로 여기서 막음.
      None은 개수를 모르는 호출(이전 호출 방식)이며 이 검사를 하지 않음.
    - UNCERTAIN: 어떤 gap 조합으로도 확정이 되지 않음. gap이 없으면 INFO_MISSING.
    - 확보 수단 소진(exhausted)은 INFO_MISSING·VERIFICATION_PENDING만 BLOCKED_FAILURE로 바꿈(POLICY_TRANSITIONS와 같은
      범위). 임상의 판단 대기와 확정 상태는 그대로.
    여러 사유가 함께 있으면 상태는 우선순위 하나만 나타내므로, 전체 사유는 pending_kinds()로 따로 보존합니다.
    """
    g = _gaps(gaps)
    if eligibility == "NOT_EVALUATED":
        return PairStatus.NOT_EVALUATED
    if eligibility == "INELIGIBLE":
        if mismatch == "confirmed":
            return PairStatus.CONFIRMED_INELIGIBLE
        base = PairStatus.VERIFICATION_PENDING
    elif eligibility == "ELIGIBLE":
        if required_criteria == 0:
            return PairStatus.NOT_EVALUATED  # 평가한 필수 기준이 없음: 적격 확정 근거 없음
        base = _open_status(g)
        if base is None:
            return PairStatus.CONFIRMED_ELIGIBLE
    elif eligibility == "UNCERTAIN":
        base = _open_status(g) or PairStatus.INFO_MISSING
    else:
        raise ValueError(f"알 수 없는 판정 값: {eligibility!r}")
    if exhausted and base in (PairStatus.INFO_MISSING, PairStatus.VERIFICATION_PENDING):
        return PairStatus.BLOCKED_FAILURE
    return base


def pending_kinds(eligibility: str, gaps: set[str] | frozenset[str] | list[str] = (),
                  mismatch: Literal["none", "provisional", "confirmed"] | None = None,
                  exhausted: bool = False, required_criteria: int | None = None) -> frozenset[str]:
    """쌍에 남은 작업 종류 전체 (derive_pair_status가 우선순위로 하나만 고른 것을 보완). 확정 상태면 빈 집합.

    - INELIGIBLE이고 불충족 근거가 미검증이면 "exclusion_unverified" + 필수 기준 gap
    - 소진(exhausted)이고 남은 작업이 있으면 "sources_exhausted"
    """
    g = _gaps(gaps)
    status = derive_pair_status(eligibility, g, mismatch, exhausted, required_criteria)
    if status in CONFIRMED or status == PairStatus.NOT_EVALUATED:
        return frozenset()
    out = set(g)
    if eligibility == "INELIGIBLE":
        out.add("exclusion_unverified")
    if exhausted and out:
        out.add("sources_exhausted")
    return frozenset(out)


# ---------------------------------------------------------------------------
# I-2 CriterionResult (판정 출력, 판정 계층 전용)
# ---------------------------------------------------------------------------

CriterionState = Literal["MET", "NOT_MET", "UNKNOWN"]
Adjudication = Literal["rule", "fact_llm", "expert"]
# contract_evaluator (0.2): 승인된 Condition IR + 질문 계약 명제 사실로 판정하는 결정적 평가기 (ctrec.evaluator).
# LLM이 아님. 기존 기록(decision_state.EvaluationRecord)은 이 경우를 engine="llm"으로 남기므로, 출처 구분은
# engine.contract_bridge가 평가기 출처(provenance)를 직접 읽어서만 합니다.
DecidedBy = Literal["rule_engine", "contract_evaluator", "llm", "clinician", "none"]
SourceType = Literal["clinical_record", "self_report", "unknown"]  # fact_store.SourceType과 같은 값
VerificationStatus = Literal["unknown", "unverified", "document_verified", "unverifiable"]  # fact_store와 같은 값
EVIDENCE_FIELD_PATTERN = r"^[A-Za-z0-9_:()./+\-]{1,200}$"
QUESTION_CONTRACT_PATTERN = r"^QC-[A-Z0-9\-]{1,60}$"  # question_contract.QuestionContract.contract_id 형식


class EvidenceRef(_Frozen):
    """판정 근거 사실 하나의 출처·검증 상태 (0.2). 값·원문은 담지 않음.

    명제 사실(proposition_store)은 field = "prop:<명제 키>"이고 질문 계약 ID·버전을 함께 남깁니다.
    verified는 verification_status == "document_verified"일 때만 참입니다. 출처가 clinical_record라고 적혀 있어도
    검증되지 않았으면 검증된 임상 근거가 아닙니다 (fact_store 원칙).
    """
    fact_id: str = Field(pattern=ID_PATTERN)
    field: str = Field(pattern=EVIDENCE_FIELD_PATTERN)
    source_type: SourceType = "unknown"
    verification_status: VerificationStatus = "unknown"
    contract_id: str | None = Field(default=None, pattern=QUESTION_CONTRACT_PATTERN)
    contract_version: NonNegativeInt | None = None

    @property
    def verified(self) -> bool:
        return self.verification_status == "document_verified"

    @property
    def proposition_key(self) -> str | None:
        return self.field[len("prop:"):] if self.field.startswith("prop:") else None


class CriterionResult(_Frozen):
    pair: PairKey
    rule_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,32}$")
    state: CriterionState
    adjudication: Adjudication
    decided_by: DecidedBy = "none"
    required_evidence: Literal["self_report_ok", "clinical_record"] = "self_report_ok"
    evidence_satisfied: bool = False
    evidence_fact_ids: tuple[str, ...] = ()
    inputs_used: tuple[str, ...] | None = None  # None = 실제 입력 항목을 모름 (LLM 판정)
    gap: GapKind = "none"
    evaluator_version: str = Field(min_length=1)
    criterion_version: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()  # 0.2: 근거 사실의 출처·검증 상태 (명제 키 포함). 빈 값 = 모름(0.1)

    @property
    def criterion_key(self) -> str:
        return f"{self.pair.trial_id}:{self.rule_id}"

    @model_validator(mode="after")
    def _authority(self):
        decided = self.state in ("MET", "NOT_MET")
        if decided and self.decided_by == "none":
            raise ValueError("MET/NOT_MET 판정에는 판정 주체(decided_by)가 필요함")
        if not decided and self.decided_by != "none":
            raise ValueError("UNKNOWN은 판정 주체를 가질 수 없음")
        if self.adjudication == "expert" and decided and self.decided_by != "clinician":
            raise ValueError("전문가 판단 기준은 임상의만 확정할 수 있음 (자동 확정 금지)")
        if self.adjudication == "rule" and self.decided_by == "llm":
            raise ValueError("규칙 판정 기준을 LLM이 확정했다고 기록할 수 없음")
        if not decided and self.evidence_satisfied:
            raise ValueError("판정되지 않은 기준은 근거 요구를 충족했다고 기록할 수 없음")
        if self.evidence_refs:
            ids = {r.fact_id for r in self.evidence_refs}
            if not ids <= set(self.evidence_fact_ids):
                raise ValueError("evidence_refs의 사실은 evidence_fact_ids 안에 있어야 함")
            if (self.required_evidence == "clinical_record" and self.evidence_satisfied
                    and not all(r.verified for r in self.evidence_refs)):
                raise ValueError("진료 기록이 필요한 기준은 문서 검증되지 않은 근거로 충족될 수 없음 (미검증 근거 승격 금지)")
        return self


# ---------------------------------------------------------------------------
# 비용 (I-3/I-4/I-5/I-6 공용)
# ---------------------------------------------------------------------------

class EffortCost(_Frozen):
    """한 행동·정보원의 비용. 능동 작업시간과 대기 시간·LLM 비용을 분리.

    단위: 분(float), USD. 축 5개(코디네이터 분, 임상의 분, LLM 호출 수, LLM USD, 환자 대기 분)는 0.2에서 고정.
    `+`의 대기 시간 최댓값은 **같은 단계에서 병렬로 보낸 요청**에만 맞는 합산입니다(동시에 기다림). 여러 단계에 걸친
    순차 경과 시간(달력 시간)은 이 합으로 구하지 말고 시뮬레이터(WP6)가 단계별 대기를 따로 더해 계산합니다.
    코디네이터·임상의 시간 예산은 engine.cost.EffortLedger, 웹 LLM 비용은 web/budget.py가 관리하며 둘을 합치지 않습니다.
    """
    coordinator_minutes: NonNegativeFloat = 0.0
    clinician_minutes: NonNegativeFloat = 0.0
    llm_calls: NonNegativeInt = 0
    llm_cost_usd: NonNegativeFloat = 0.0
    patient_wait_minutes: NonNegativeFloat = 0.0  # 응답 지연 (능동 시간 아님, 예산에서 차감하지 않음)

    def __add__(self, other: "EffortCost") -> "EffortCost":
        return EffortCost(coordinator_minutes=self.coordinator_minutes + other.coordinator_minutes,
                          clinician_minutes=self.clinician_minutes + other.clinician_minutes,
                          llm_calls=self.llm_calls + other.llm_calls,
                          llm_cost_usd=self.llm_cost_usd + other.llm_cost_usd,
                          patient_wait_minutes=max(self.patient_wait_minutes, other.patient_wait_minutes))


ZERO_COST = EffortCost()


# ---------------------------------------------------------------------------
# I-5 InformationSource
# ---------------------------------------------------------------------------

class InformationSource(_Frozen):
    source_id: str = Field(pattern=CODE_PATTERN)
    kind: Literal["document", "patient", "clinician", "system"]
    provides_fields: tuple[str, ...] = ()  # 이 정보원으로 확인할 수 있는 항목 (예: lab:*, biomarker, ecog)
    request_cost: EffortCost = ZERO_COST  # 환자·정보원당 한 번만 청구 (문서 1건 요청·접촉 1회)
    per_item_minutes: NonNegativeFloat = 0.0  # 같은 요청 안에서 항목(기준)마다 더 드는 코디네이터 시간
    can_verify: bool = False  # 문서 검증(document_verified) 근거가 될 수 있는가
    alternatives: tuple[str, ...] = ()  # 실패·무응답 뒤 시도할 대체 정보원 ID (순서대로)
    # 0.2: 비용 값의 출처. assumed = 근거 없는 가정값(기본). 가정값으로 낸 분 단위 결과는 상대 비교에만 쓰고
    # 실제 병원 업무시간 절감을 주장하지 않음 (engine.experiment.catalog_claims)
    cost_basis: Literal["assumed", "literature", "measured", "elicited"] = "assumed"
    cost_reference: str | None = Field(default=None, pattern=ID_PATTERN)  # 문헌·측정 기록 ID (값·원문 아님)

    @model_validator(mode="after")
    def _basis(self):
        if self.cost_basis != "assumed" and not self.cost_reference:
            raise ValueError("가정값이 아닌 비용에는 출처 ID(cost_reference)가 필요함")
        return self

    @field_validator("alternatives")
    @classmethod
    def _no_self(cls, v, info):
        if info.data.get("source_id") in v:
            raise ValueError("대체 정보원에 자기 자신을 넣을 수 없음")
        return v


# ---------------------------------------------------------------------------
# I-3 Action / I-4 ActionOutcome
# ---------------------------------------------------------------------------

ActionType = Literal["ASK_PATIENT", "REQUEST_DOCUMENT", "VERIFY_RECORD", "CLINICIAN_REVIEW", "RUN_TOOL", "REPARSE",
                     "RETRIEVE", "DEFER", "FINALIZE"]
# 기존 decision_state.ActionType -> 엔진 ActionType (의미 변경 없음)
LEGACY_ACTION_TYPES: dict[str, str] = {"ASK_USER": "ASK_PATIENT", "REQUEST_DOCUMENT": "REQUEST_DOCUMENT",
                                       "VERIFY": "VERIFY_RECORD", "NEEDS_REVIEW": "CLINICIAN_REVIEW",
                                       "RUN_TOOL": "RUN_TOOL", "REPARSE": "REPARSE", "RETRIEVE": "RETRIEVE",
                                       "DEFER": "DEFER", "FINALIZE": "FINALIZE"}
# 실행하지 않는 행동 (예산을 쓰지 않음): 보류·종료는 상태 기록
NON_EXECUTING: frozenset[str] = frozenset({"DEFER", "FINALIZE"})


class Action(_Frozen):
    action_id: str = Field(pattern=ID_PATTERN)
    action_type: ActionType
    patient_alias: str = Field(pattern=ALIAS_PATTERN)
    source_id: str | None = Field(default=None, pattern=CODE_PATTERN)
    target_trial_ids: tuple[str, ...] = ()
    target_criteria: tuple[str, ...] = ()  # "<trial_id>:<rule_id>"
    required_fields: tuple[str, ...] = ()
    evidence_fact_ids: tuple[str, ...] = ()
    expected_cost: EffortCost = ZERO_COST
    reason_code: str = Field(pattern=CODE_PATTERN)
    tier: int = Field(ge=0, le=9)
    value_score: float = 0.0  # 구조적 점수 (확률 아님, 같은 단계 안의 순서에만)
    priority: float = 0.0  # 작을수록 먼저. 기존 정책의 priority를 그대로 옮김 (0.1 추가 필드)
    sequence: NonNegativeInt = 0  # 같은 환자 안에서 기존 정책이 정한 순서 (동점 처리, 0.1 추가 필드)
    safety_required: bool = False  # 안전상 필수 확인 (상충 근거 등) - 최적화로 버리지 않음
    fingerprint: str = Field(min_length=1)
    legacy_action_id: str | None = None
    policy_version: str = Field(min_length=1)
    # 0.2 질문 연결 (모두 기본값 있음, 질문 문장은 계약에 넣지 않음)
    # purpose: 정책이 판정 메타데이터(gap)로 정함. 렌더러·LLM은 바꿀 수 없음(engine.question_bridge가 검사).
    #   physician_reference = 원문에 구체 기준이 없는 기준에 대한 의사 참고용 정보 수집. 그 답은 적격성 근거가 아님
    #   (evidence_directness.eligibility_answers가 제외).
    purpose: Literal["eligibility", "physician_reference"] = "eligibility"
    info_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:+\-]{1,160}$")  # decision_state.info_key
    question_contract_id: str | None = Field(default=None, pattern=QUESTION_CONTRACT_PATTERN)  # 검수된 질문 계약

    @field_validator("target_criteria")
    @classmethod
    def _keys(cls, v):
        import re
        bad = [k for k in v if not re.match(CRITERION_KEY_PATTERN, k)]
        if bad:
            raise ValueError(f"기준 키 형식 오류 {len(bad)}건 ('<trial>:<rule>')")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        trials = {k.split(":", 1)[0] for k in self.target_criteria}
        if not trials <= set(self.target_trial_ids):
            raise ValueError("target_criteria의 시험이 target_trial_ids에 없음")
        if self.purpose == "physician_reference" and self.action_type not in ("ASK_PATIENT", "CLINICIAN_REVIEW"):
            raise ValueError("의사 참고용 행동은 환자 질문·임상의 검토만 가능")
        if self.question_contract_id is not None:
            if self.action_type != "ASK_PATIENT":
                raise ValueError("질문 계약은 환자 질문 행동에만 붙일 수 있음")
            if self.purpose != "eligibility":
                raise ValueError("질문 계약은 적격성 질문 전용 (question_contract.render는 purpose=eligibility)")
        return self

    def pairs(self) -> tuple[PairKey, ...]:
        return tuple(PairKey(patient_alias=self.patient_alias, trial_id=t) for t in self.target_trial_ids)


OutcomeStatus = Literal["succeeded", "partial", "failed", "no_response", "not_executed"]


class ActionOutcome(_Frozen):
    action_id: str = Field(pattern=ID_PATTERN)
    status: OutcomeStatus
    new_fact_ids: tuple[str, ...] = ()
    cost_charged: EffortCost = ZERO_COST
    charge_keys: tuple[str, ...] = ()  # 실제로 청구한 (환자, 정보원) 키 - 중복 청구 검사용
    latency_minutes: NonNegativeFloat = 0.0
    error_code: str | None = Field(default=None, pattern=CODE_PATTERN)
    resolved_criteria: tuple[str, ...] = ()  # 실행기가 보고한 대상 기준 (판정은 재평가가 정함)

    @model_validator(mode="after")
    def _facts(self):
        if self.status in ("failed", "no_response", "not_executed") and self.new_fact_ids:
            raise ValueError("실패·무응답·미실행 행동은 사실을 추가할 수 없음")
        if self.status == "partial" and not self.new_fact_ids:
            raise ValueError("부분 성공은 새 사실이 하나 이상 있어야 함")
        if self.status == "not_executed" and self.cost_charged != ZERO_COST:
            raise ValueError("미실행 행동은 비용을 청구하지 않음")
        if self.status in ("failed", "no_response") and self.error_code is None:
            raise ValueError("실패·무응답에는 error_code가 필요함")
        return self


# ---------------------------------------------------------------------------
# I-6 EffortBudget
# ---------------------------------------------------------------------------

class EffortBudget(_Frozen):
    coordinator_minutes: NonNegativeFloat
    clinician_minutes: NonNegativeFloat = 0.0
    llm_cost_usd: NonNegativeFloat | None = None  # None = LLM 비용은 이 예산에서 관리하지 않음 (웹 원장 별도)
    per_patient_floor_minutes: NonNegativeFloat = 0.0  # 열린 쌍이 있는 환자마다 우선 배정할 최소 시간
    per_patient_cap_minutes: NonNegativeFloat | None = None
    horizon_steps: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _floor(self):
        if self.per_patient_cap_minutes is not None and self.per_patient_floor_minutes > self.per_patient_cap_minutes:
            raise ValueError("환자별 최소 배정이 상한보다 큼")
        if self.per_patient_floor_minutes > self.coordinator_minutes:
            raise ValueError("환자별 최소 배정이 전체 예산보다 큼")
        return self


# ---------------------------------------------------------------------------
# I-7 DecisionRecord (감사)
# ---------------------------------------------------------------------------

class CriterionChange(_Frozen):
    criterion_key: str = Field(pattern=CRITERION_KEY_PATTERN)
    before: CriterionState | Literal["NEEDS_FULL_EVALUATION"]
    after: CriterionState | Literal["NEEDS_FULL_EVALUATION"]
    reevaluated: bool  # False = 재평가하지 않고 이전 판정을 재사용
    reason_code: str = Field(pattern=CODE_PATTERN)  # 재평가·재사용 사유
    evaluator_version: str | None = None
    evidence_fact_ids: tuple[str, ...] = ()


# 0.2 확정 상태 변경 사유 코드 (PairTransition.reason_code). 확정에서 내려갈 때와 다시 확정할 때 씀.
REASON_CONTRADICTING_EVIDENCE = "contradicting_evidence"  # 확정 뒤 반대 근거가 들어와 확인 대기로 내림
REASON_EVIDENCE_REVERIFIED = "evidence_reverified"  # 별도 근거 확인 뒤 다시 확정 (반대 확정 포함)
REVERSAL_REASON_CODES = frozenset({REASON_CONTRADICTING_EVIDENCE, REASON_EVIDENCE_REVERIFIED,
                                   "evidence_withdrawn", "evaluation_failed", "expert_review_requested"})


class PairTransition(_Frozen):
    pair: PairKey
    before: PairStatus
    after: PairStatus
    authority: Literal["adjudication", "policy"] = "adjudication"
    reason_code: str | None = Field(default=None, pattern=CODE_PATTERN)  # 0.2: 상태 변경 사유 (값·원문 아님)
    pending_kinds: tuple[PendingKind, ...] = ()  # 0.2: 전이 뒤 남은 작업 전체 (상태는 우선순위 하나만 나타냄)

    @model_validator(mode="after")
    def _allowed(self):
        check_transition(self.before, self.after, authority=self.authority)
        if self.after in CONFIRMED and self.pending_kinds:
            raise ValueError("확정 상태에는 남은 작업이 있을 수 없음")
        return self


class DecisionRecord(_Frozen):
    schema_version: Literal["ctrec.decision_record/v2"] = DECISION_RECORD_SCHEMA
    contract_version: str = CONTRACT_VERSION
    run_id: str = Field(pattern=ID_PATTERN)
    step: NonNegativeInt
    action: Action
    outcome: ActionOutcome | None = None  # None = 제안·선택만 되고 아직 실행 결과 없음
    facts_added: tuple[str, ...] = ()
    criteria: tuple[CriterionChange, ...] = ()
    pairs: tuple[PairTransition, ...] = ()
    policy_version: str = Field(min_length=1)
    evaluator_version: str | None = None
    fact_version_before: str | None = None
    fact_version_after: str | None = None

    @model_validator(mode="after")
    def _links(self):
        if self.outcome is not None:
            if self.outcome.action_id != self.action.action_id:
                raise ValueError("outcome.action_id가 action.action_id와 다름")
            if not set(self.facts_added) <= set(self.outcome.new_fact_ids):
                raise ValueError("facts_added는 outcome.new_fact_ids 안에 있어야 함")
        elif self.facts_added or self.criteria:
            raise ValueError("실행 결과 없이 사실 추가·재평가를 기록할 수 없음")
        if any(p.pair.patient_alias != self.action.patient_alias for p in self.pairs):
            raise ValueError("다른 환자의 쌍 상태를 이 행동 기록에 넣을 수 없음")
        keys = [p.pair.key for p in self.pairs]
        if len(keys) != len(set(keys)):  # 0.2: 한 기록 안에서 확정 -> 대기 -> 반대 확정을 잇는 우회 금지
            raise ValueError("한 행동 기록에 같은 쌍의 전이가 둘 이상 있음")
        if self.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
            raise ValueError("지원하지 않는 계약 버전")
        reevaluated = [c for c in self.criteria if c.before != c.after and not c.reevaluated]
        if reevaluated:
            raise ValueError("재평가하지 않은(재사용) 기준의 판정이 바뀔 수 없음")
        return self


# ---------------------------------------------------------------------------
# I-8 PolicyEngine
# ---------------------------------------------------------------------------

class SkippedAction(_Frozen):
    action_id: str
    reason_code: str = Field(pattern=CODE_PATTERN)


class Plan(_Frozen):
    policy_id: str
    policy_version: str
    selected: tuple[Action, ...] = ()
    skipped: tuple[SkippedAction, ...] = ()
    held: tuple[Action, ...] = ()  # 실행하지 않는 보류·종료 기록 (DEFER/FINALIZE)
    total_cost: EffortCost = ZERO_COST
    safety_unfunded: tuple[str, ...] = ()  # 예산이 모자라 실행 못 한 안전 필수 행동 (버리지 않고 보고)


@runtime_checkable
class PolicyEngine(Protocol):
    """정책 계층 프로토콜. 상태는 읽기만 하고 판정을 바꾸지 않음. LLM·네트워크를 쓰지 않음."""
    policy_id: str
    version: str

    def propose(self, state) -> list[Action]:  # state: engine.cohort_state.CohortState
        ...

    def select(self, candidates: list[Action], budget: EffortBudget, ledger=None) -> Plan:
        ...


# ---------------------------------------------------------------------------
# I-9 ExperimentSpec
# ---------------------------------------------------------------------------

class DataVersion(_Frozen):
    frozen_store_hash: str = Field(min_length=1)
    cohort_hash: str = Field(min_length=1)
    source_catalog_hash: str | None = None


class ExperimentSpec(_Frozen):
    experiment_id: str = Field(pattern=ID_PATTERN)
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    policy_config: dict = Field(default_factory=dict)
    data_version: DataVersion
    seed: NonNegativeInt
    budget: EffortBudget
    ablations: tuple[str, ...] = ()
    contract_version: str = CONTRACT_VERSION

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.policy_config)

    def spec_hash(self) -> str:
        """같은 조건이면 같은 값 (experiment_id는 제외: 이름만 다른 같은 실험을 알아볼 수 있게)."""
        d = self.model_dump(mode="json", exclude={"experiment_id"})
        d["ablations"] = sorted(d["ablations"])
        return canonical_hash(d)
