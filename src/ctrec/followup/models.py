"""추가 정보 요청·응답 스키마 (TRIALOG O). 판정 값이 아니라 요청의 운영 상태만 담습니다.

요청 종류(kind)와 상태(status)
- ask_patient        : 환자가 화면에서 답하는 질문. pending -> answered | unknown | unreachable | superseded | cancelled
- document_review    : 기록(검사지·진료 기록 등) 확인 필요.
                       requires_document -> document_verified | document_unavailable | superseded | cancelled
- clinician_review   : 담당 의료진 판단 필요. requires_clinician -> clinician_decided | superseded | cancelled
- conflict_resolution: 같은 항목의 사실이 서로 맞지 않음. requires_document -> document_unavailable | superseded | cancelled
                       (어느 값도 채택하지 않고 보류를 유지. 문서 검증으로 닫지 않음)

완료 상태 document_verified·clinician_decided는 요청의 운영 상태일 뿐 판정 근거가 아닙니다.
- 이 두 상태는 상태 변경 API(STAFF_TRANSITIONS)로 만들 수 없고, 검증·판단 API(service.verify_document,
  service.clinician_judgment)가 권한 확인과 서명된 증명·판단을 남길 때만 바뀝니다.
- 판정은 사실 이력의 증명(document_authority)과 판단 기록(clinician_authority)을 현재 권한이 인정할 때만 바뀝니다.
  상태 문자열만 남고 권한이 없으면(서명 비밀 없음, 운영 모드) 기준은 다시 보류됩니다.

'모름'(unknown)·연락 불가(unreachable)·문서 없음(document_unavailable)은 같은 질문을 반복하지 않고 정책 엔진이 낸
대체 경로(문서 검토 -> 임상 검토)를 새 요청으로 만듭니다 (follows로 이어짐).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FORMAT = "ctrec.followup_case/v1"

RequestKind = Literal["ask_patient", "document_review", "clinician_review", "conflict_resolution"]
RequestStatus = Literal["pending", "answered", "unknown", "unreachable", "requires_document", "requires_clinician",
                        "document_unavailable", "document_verified", "clinician_decided", "superseded", "cancelled"]

OPEN_STATUSES = frozenset({"pending", "requires_document", "requires_clinician"})
PATIENT_KINDS = frozenset({"ask_patient"})
# 연구 담당자가 바꿀 수 있는 상태 (답변·문서 확인·임상 판단 완료는 없음: 검증자·임상의 API만)
STAFF_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "ask_patient": {"pending": frozenset({"unreachable", "cancelled"})},
    "document_review": {"requires_document": frozenset({"document_unavailable", "cancelled"})},
    "conflict_resolution": {"requires_document": frozenset({"document_unavailable", "cancelled"})},
    "clinician_review": {"requires_clinician": frozenset({"cancelled"})},
}
# 정책 엔진 행동 -> 요청 종류·처음 상태 (실행할 수 없는 기록 행동 DEFER/FINALIZE/RUN_TOOL/REPARSE는 요청이 아님)
ACTION_ROUTES: dict[str, tuple[str, str]] = {
    "ASK_PATIENT": ("ask_patient", "pending"),
    "REQUEST_DOCUMENT": ("document_review", "requires_document"),
    "VERIFY_RECORD": ("conflict_resolution", "requires_document"),
    "CLINICIAN_REVIEW": ("clinician_review", "requires_clinician"),
}

CODE = r"^[a-z0-9_]{1,64}$"
ACTOR = r"^(patient|system|(staff|verifier|clinician):[A-Za-z0-9._-]{1,64})$"


class StatusEvent(BaseModel):
    at: str
    from_status: str | None
    to_status: str
    by: str = Field(pattern=ACTOR)
    reason: str = Field(pattern=CODE)


class PatientQuestion(BaseModel):
    """환자 화면에 보이는 질문 (문장·형식·선택지만)."""
    question: str
    answer_type: str = "free_text"
    options: list[str] = []
    unit: str | None = None


class FollowupRequest(BaseModel):
    request_id: str
    kind: RequestKind
    status: RequestStatus
    created_at: str
    updated_at: str
    created_by: str = Field(pattern=ACTOR)
    proposal_id: str | None = None  # 정책 엔진 Action.action_id (별칭:지문)
    action: dict | None = None  # engine.contracts.Action (응답 정산에 그대로 씀)
    action_type: str | None = None
    source_id: str | None = None
    info_key: str | None = None
    target_criteria: list[str] = []
    target_trials: list[str] = []
    reason_code: str = Field(pattern=CODE)
    question: PatientQuestion | None = None  # ask_patient만
    follows: str | None = None  # 이 요청을 만든 이전 요청 ('모름' 뒤 대체 경로 등)
    conflict: dict | None = None  # conflict_resolution: {field, relation, fact_ids}
    revision_created: int
    response_id: str | None = None
    history: list[StatusEvent] = []


class FollowupResponse(BaseModel):
    response_id: str
    request_id: str
    at: str
    outcome: Literal["answered", "unknown"]
    answer: str  # unknown이면 "모름"
    answer_digest: str  # 중복 응답 판별 (정규화한 답의 SHA-256)
    client_response_id: str | None = None
    revision_after: int
    fact_ids_added: list[str] = []
    conflicts: list[dict] = []


class EvaluationRecord(BaseModel):
    """평가 한 번의 요약 (Inspector·담당자 화면의 변경 기록). 환자 원문은 넣지 않음."""
    revision: int
    at: str
    trigger: dict
    pairs: dict[str, dict]
    transitions: list[dict] = []
    criteria_changes: list[dict] = []
    reevaluated_trials: list[str] = []
    reused_trials: list[str] = []
    impact: dict | None = None
    equivalence: dict = {}
    proposals: list[dict] = []
    conflicts: list[dict] = []
    fact_version: str | None = None
