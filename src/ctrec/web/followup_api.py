"""추가 정보 확인 API (TRIALOG O): 환자 입력 -> 연구 담당자 요청 -> 환자 응답 -> 재평가. 계약: docs/dev/followup_api_contract.md

권한 (web/access.py와 같은 원칙)
- 환자: 사례를 만들 때 사례별 토큰(256비트)을 HttpOnly·SameSite=Strict 쿠키 ctrec_case로만 발급
  (Path=/api/followup/cases/<case_id>). 서버는 SHA-256만 저장. 없는 사례·권한 없음은 같은 404.
- 연구 담당자: X-CTREC-Staff-Key 헤더가 서버 설정 CTREC_STAFF_KEY와 같아야 함 (설정이 없으면 담당자 API 전체 503).
  X-CTREC-Staff-Id(영문·숫자 1~64자)는 기록용 이름. 계정 인증을 대신하지 않습니다.
  발표용 데모 예외(DEMO_STAFF): 오프라인 데모 서버(scripts/followup_demo_server.py --demo-staff)만 켤 수 있고(환경 변수로는
  켤 수 없음), 켜져 있어도 루프백 접속에만 키 없이 담당자 권한을 줌. 데모 서버 자체가 루프백 전용·합성 데이터만 씀.
  같은 예외로 발표자가 한 화면에서 검증자·임상의 역할도 보여 줄 수 있음: X-CTREC-Demo-Role: verifier|clinician 헤더
  (키 대신, 한 요청에 한 역할). 합성 검증 모드(CTREC_VERIFICATION_MODE=synthetic)가 켜져 있어야 하고, 기록 이름은
  demo-verifier·demo-clinician으로 고정해 감사 기록에서 발표용 입력임이 보임.
- 담당자는 환자 답변을 대신 입력할 수 없음: 응답 API는 환자 쿠키만 받고, 담당자·검증자·임상의 헤더가 붙은 응답 요청은 403.
- 문서 검증자(X-CTREC-Verifier-Key/-Id)와 임상의(X-CTREC-Clinician-Key/-Id)는 연구 담당자와 다른 권한입니다.
  지금 있는 권한은 합성 시험 전용(CTREC_VERIFICATION_MODE=synthetic, followup/verification.py)뿐이며 합성 사례에만 동작합니다.
  설정이 없거나 운영 모드면 검증자·임상의 API는 503 (운영 의료기관 인증 연동 없음). 한 요청은 한 역할의 헤더만 가질 수 있음.
  계약: docs/dev/document_clinician_api_contract.md
- 상태를 바꾸는 POST는 X-CTREC-Request: 1 + 같은 출처(Origin)만 (access.check_state_changing, CSRF).
- 실제 LLM 호출은 /api/runs와 같은 허가(paid_gate)·기록·예산(web_metering, web/budget)을 거침.

판정 규칙은 여기에 없습니다 (ctrec.followup.service).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import os
import re

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import web_metering
from ..followup import verification
from ..followup.service import NO_PATH_REASONS, FollowupError, FollowupService, TrialCatalog
from ..followup.store import CaseStore
from . import access, budget

log = logging.getLogger("ctrec.web.followup")
router = APIRouter(prefix="/api/followup")

CASE_COOKIE = "ctrec_case"
STAFF_HEADER = "x-ctrec-staff-key"
STAFF_ID_HEADER = "x-ctrec-staff-id"
STAFF_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
VERIFIER_HEADER = "x-ctrec-verifier-key"
VERIFIER_ID_HEADER = "x-ctrec-verifier-id"
CLINICIAN_HEADER = "x-ctrec-clinician-key"
CLINICIAN_ID_HEADER = "x-ctrec-clinician-id"
DEMO_ROLE_HEADER = "x-ctrec-demo-role"
ROLE_HEADERS = (STAFF_HEADER, VERIFIER_HEADER, CLINICIAN_HEADER, DEMO_ROLE_HEADER)
CASE_COOKIE_MAX_AGE = 30 * 24 * 3600
MAX_TEXT_CHARS = 20000
MAX_ANSWER_CHARS = 2000

_SERVICE: FollowupService | None = None
DEMO_STAFF = False  # 발표용 데모 서버만 True로 바꿈 (모듈 속성, 설정·환경 변수 아님)
DEMO_STAFF_ID = "demo-staff"
LOOPBACK = frozenset({"127.0.0.1", "::1"})


def _app():
    from . import app as webapp  # 지연 import (app이 이 모듈을 포함하므로)
    return webapp


def default_catalog() -> TrialCatalog:
    from ..agents.recommender import screen_pool
    return TrialCatalog(name="trial_store", trials=screen_pool(_app()._load_trials()))


def configure(service: FollowupService | None) -> None:
    """테스트·데모 서버가 저장소·시험 카탈로그를 지정 (None이면 기본값으로 다시 만듦)."""
    global _SERVICE
    _SERVICE = service


def service() -> FollowupService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = FollowupService(CaseStore(), default_catalog(), llm_scope=llm_scope)
    return _SERVICE


@contextlib.contextmanager
def llm_scope(label: str):
    """이 평가의 실제 LLM 호출을 /api/runs와 같은 기록·예산 아래에서 (테스트 대역이면 아무것도 하지 않음)."""
    if _app()._llm_backend() != "real":
        yield
        return
    meter = web_metering.WebRunMeter(label)
    if meter.mode != "enforce":
        with meter.recording():
            yield
        meter.finish()
        return
    cfg = budget.load_config(default_run_seconds=_app().RUN_TIMEOUT_S)
    with budget.run_context(label, cfg, budget.get_ledger(cfg), budget.make_inner(), meter) as run_budget:
        yield
        if run_budget.stopped:
            raise budget.BudgetStop(run_budget.stopped)
    meter.finish()


# ---------------------------------------------------------------------------
# 권한
# ---------------------------------------------------------------------------

def _staff_key() -> str:
    return os.getenv("CTREC_STAFF_KEY", "")


def _is_staff_request(request: Request) -> bool:
    """연구 담당자·검증자·임상의 어느 역할 헤더든 붙어 있으면 (환자 답변 대리 입력 차단용)."""
    return any(request.headers.get(h) for h in ROLE_HEADERS)


def _single_role(request: Request, header: str) -> None:
    """이 역할의 헤더와 다른 역할의 헤더를 함께 보내면 거절 (다른 역할 헤더만 있으면 키 확인에서 403)."""
    if request.headers.get(header) and any(request.headers.get(h) for h in ROLE_HEADERS if h != header):
        raise HTTPException(400, {"code": "multiple_roles", "message": "한 요청에는 한 역할의 권한 헤더만 보낼 수 있습니다."})


_ROLE_NOT_READY = {
    "verification_not_configured": "문서 검증·임상의 판단 권한이 설정되지 않았습니다. 운영 의료기관 인증 연동은 아직 없습니다.",
    "production_authority_unavailable": "운영 검증 권한(의료기관 인증·권한 연동)은 구현되지 않았습니다.",
}


def _require_role(request: Request, header: str, id_header: str, key_attr: str, forbidden: str) -> str:
    _single_role(request, header)
    cfg = verification.VerificationConfig()
    problems = cfg.problems()
    if problems:
        code = problems[0] if problems[0] in _ROLE_NOT_READY else "verification_misconfigured"
        log.warning("verification role blocked: %s", problems)
        raise HTTPException(503, {"code": code, "message": _ROLE_NOT_READY.get(code, "검증 권한 설정이 올바르지 않습니다.")})
    role = DEMO_ROLES.get(header)
    if role and demo_staff(request) and request.headers.get(DEMO_ROLE_HEADER) == role:
        return f"demo-{role}"
    if not verification.key_matches(request.headers.get(header), getattr(cfg, key_attr)):
        raise HTTPException(403, {"code": forbidden, "message": "이 작업을 할 권한이 없습니다."})
    actor = request.headers.get(id_header, "")
    if not STAFF_ID.match(actor):
        raise HTTPException(400, {"code": "bad_actor_id", "message": "식별자 헤더는 영문·숫자·._- 1~64자입니다."})
    return actor


def _deny_audit(case_id: str, action: str, e: HTTPException, request_id: str | None = None) -> None:
    """권한 헤더로 거부된 완료 시도도 감사 기록에 (사례가 있을 때만: 사례 ID는 128비트 난수라 외부에서 기록을 늘릴 수 없음)."""
    svc = service()
    if svc.store.exists(case_id):
        code = e.detail.get("code", "forbidden") if isinstance(e.detail, dict) else "forbidden"
        svc.audit.denied(case_id, actor="unauthenticated", action=action, code=code, request_id=request_id)


def _role_for(request: Request, require, case_id: str, action: str, request_id: str | None = None) -> str:
    try:
        return require(request)
    except HTTPException as e:
        if e.status_code in (400, 403):
            _deny_audit(case_id, action, e, request_id)
        raise


DEMO_ROLES = {VERIFIER_HEADER: "verifier", CLINICIAN_HEADER: "clinician"}


def require_verifier(request: Request) -> str:
    return _require_role(request, VERIFIER_HEADER, VERIFIER_ID_HEADER, "doc_key", "verifier_forbidden")


def require_clinician(request: Request) -> str:
    return _require_role(request, CLINICIAN_HEADER, CLINICIAN_ID_HEADER, "clinician_key", "clinician_forbidden")


def demo_staff(request: Request) -> bool:
    """발표용 데모 예외가 이 요청에 적용되는가 (데모 서버가 켰고, 루프백 접속)."""
    return DEMO_STAFF and request.client is not None and request.client.host in LOOPBACK


def require_staff(request: Request) -> str:
    _single_role(request, STAFF_HEADER)
    if demo_staff(request) and not request.headers.get(DEMO_ROLE_HEADER):
        sid = request.headers.get(STAFF_ID_HEADER, DEMO_STAFF_ID)
        return sid if STAFF_ID.match(sid) else DEMO_STAFF_ID
    key = _staff_key()
    if not key:
        raise HTTPException(503, {"code": "staff_access_not_configured",
                                  "message": "연구 담당자 기능이 이 서버에서 설정되지 않았습니다 (CTREC_STAFF_KEY)."})
    given = request.headers.get(STAFF_HEADER, "")
    if not given or not hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(key.encode()).digest()):
        raise HTTPException(403, {"code": "staff_forbidden", "message": "연구 담당자 권한이 없습니다."})
    sid = request.headers.get(STAFF_ID_HEADER, "staff")
    if not STAFF_ID.match(sid):
        raise HTTPException(400, {"code": "bad_staff_id", "message": "X-CTREC-Staff-Id는 영문·숫자·._- 1~64자입니다."})
    return sid


def _patient_case(case_id: str, request: Request) -> dict:
    svc = service()
    try:
        data = svc.store.load(case_id)
    except (KeyError, ValueError):
        data = None
    if data is None or not access.matches(request.cookies.get(CASE_COOKIE), data.get("patient_token_digest")):
        raise HTTPException(404, {"code": "case_not_found", "message": "사례를 찾을 수 없거나 접근 권한이 없습니다."})
    return data


def _llm_ready() -> None:
    webapp = _app()
    problems = webapp._paid_problems()
    if problems:
        log.warning("followup analysis blocked: %s", problems)
        raise HTTPException(503, {"code": "analysis_disabled",
                                  "message": webapp._ANALYSIS_DISABLED if problems == ["web_paid_disabled"]
                                  else webapp._ANALYSIS_NOT_READY})


def _fail(e: FollowupError):
    raise HTTPException(e.status, {"code": e.code, "message": e.message})


@contextlib.contextmanager
def _flow():
    try:
        yield
    except FollowupError as e:
        _fail(e)
    except budget.BudgetStop as e:
        raise HTTPException(503, {"code": e.category, "message": "비용 한도·사용량 확인 문제로 평가를 멈췄습니다. 변경은 저장하지 않았습니다."})
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - 예외 메시지에는 환자 정보가 있을 수 있으므로 종류만
        log.warning("followup flow failed: %s", type(e).__name__)
        raise HTTPException(500, {"code": "evaluation_failed",
                                  "message": f"평가 중 오류가 발생했습니다 ({type(e).__name__}). 변경은 저장하지 않았습니다."})


# ---------------------------------------------------------------------------
# 응답 형태 (환자용은 담당자 내부 정보·사유 코드·사실 ID를 넣지 않음)
# ---------------------------------------------------------------------------

def _pair_counts(pairs: dict) -> dict:
    out: dict[str, int] = {}
    for p in pairs.values():
        out[p["status"]] = out.get(p["status"], 0) + 1
    return out


def patient_case_view(data: dict) -> dict:
    cur = data["current"]
    return {"case_id": data["case_id"], "revision": data["revision"], "created_at": data["created_at"],
            "pairs": {t: {"status": p["status"]} for t, p in cur["pairs"].items()},
            "pending_requests": sum(1 for r in data["requests"] if r["kind"] == "ask_patient" and r["status"] == "pending"),
            "view": cur["view"]}


def staff_case_view(data: dict) -> dict:
    cur = data["current"]
    return {"case_id": data["case_id"], "revision": data["revision"], "created_at": data["created_at"],
            "synthetic": data["synthetic"], "trial_ids": data["trial_ids"], "pairs": cur["pairs"],
            "pair_counts": _pair_counts(cur["pairs"]), "proposals": cur["proposals"], "held": cur["held"],
            "authority_changed": bool(cur.get("authority_changed")),
            "requests": data["requests"],
            "responses": [{k: v for k, v in r.items() if k != "answer_digest"} for r in data["responses"]],
            "evaluations": data["evaluations"], "events": data["events"], "view": cur["view"],
            "documents": data.get("documents", []), "verifications": data.get("verifications", []),
            "judgments": data.get("judgments", [])}


def _live_staff_view(data: dict) -> dict:
    """POST 응답의 사례 화면 = GET 사례 화면 (refresh_pairs). 저장된 current는 그 평가 때의 권한 기준이라, 권한이 바뀐 뒤
    재평가 없이 저장만 하는 변경(요청 생성·문서 등록 등)의 응답에 철회 전 확정이 남지 않게 함. 판정은 다시 하지 않음."""
    return staff_case_view(service().refresh_pairs(data))


# ---------------------------------------------------------------------------
# 환자 API
# ---------------------------------------------------------------------------

class CaseCreate(BaseModel):
    patient_text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    trial_ids: list[str] = Field(min_length=1, max_length=200)
    input_source: str = "self_report"


class ResponseBody(BaseModel):
    answer: str | None = Field(default=None, max_length=MAX_ANSWER_CHARS)
    unknown: bool = False
    client_response_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")


@router.get("/trials")
def catalog_trials() -> dict:
    """이 흐름이 평가할 수 있는 시험 (화면의 후보 목록). 공개 정보만 - /api/trials와 같은 항목."""
    cat = service().catalog
    out = [{"trial_id": tid, "title": t.get("title") or t.get("brief_title") or "",
            "overall_status": t.get("overall_status"), "synthetic": cat.name != "trial_store"}
           for tid, t in cat.trials.items()]
    return {"catalog": cat.name, "synthetic": cat.name != "trial_store", "trials": out}


@router.post("/cases", status_code=201)
def create_case(body: CaseCreate, request: Request, response: Response) -> dict:
    access.check_state_changing(request)
    _llm_ready()
    token = access.new_token()
    with _flow():
        data = service().create_case(patient_text=body.patient_text, trial_ids=body.trial_ids,
                                     input_source=body.input_source, patient_token_digest=access.digest(token),
                                     synthetic=service().catalog.name != "trial_store")
    access.set_cookie(response, request, CASE_COOKIE, token, path=f"/api/followup/cases/{data['case_id']}",
                      max_age=CASE_COOKIE_MAX_AGE)
    return patient_case_view(data)


@router.get("/cases/{case_id}")
def get_case(case_id: str, request: Request) -> dict:
    data = _patient_case(case_id, request)
    with _flow():
        return patient_case_view(service().refresh_pairs(data))


@router.get("/cases/{case_id}/requests")
def patient_requests(case_id: str, request: Request) -> dict:
    _patient_case(case_id, request)
    with _flow():
        return service().patient_requests(case_id)


@router.post("/cases/{case_id}/requests/{request_id}/response")
def respond(case_id: str, request_id: str, body: ResponseBody, request: Request) -> dict:
    access.check_state_changing(request)
    if _is_staff_request(request):
        e = HTTPException(403, {"code": "staff_cannot_answer",
                                "message": "연구 담당자·검증자·임상의는 환자 답변을 대신 입력할 수 없습니다."})
        _deny_audit(case_id, "patient_response", e, request_id)
        raise e
    _patient_case(case_id, request)
    if not body.unknown and not (body.answer or "").strip():
        raise HTTPException(400, {"code": "empty_answer", "message": "답을 입력하거나 '모름'을 선택하세요."})
    _llm_ready()
    with _flow():
        out = service().respond(case_id, request_id, answer=body.answer, unknown=body.unknown,
                                client_response_id=body.client_response_id,
                                validate=lambda spec, a: _app().answer_problem(spec, a))
        case = patient_case_view(service().refresh_pairs(out["case"]))
    r = out["response"]
    return {"duplicate": out["duplicate"], "request_id": request_id, "status": out["request"]["status"],
            "outcome": r["outcome"], "revision": out["case"]["revision"],
            "conflict": bool(r.get("conflicts")), "case": case}


# ---------------------------------------------------------------------------
# 연구 담당자 API
# ---------------------------------------------------------------------------

class RequestCreate(BaseModel):
    proposal_ids: list[str] = Field(min_length=1, max_length=50)
    expected_revision: int | None = None


class StatusChange(BaseModel):
    status: str = Field(pattern=r"^(unreachable|document_unavailable|cancelled)$")


def _open_state(r: dict) -> str:
    if r["status"] == "requires_clinician" and r.get("reason_code") in NO_PATH_REASONS:
        return "on_hold"
    return r["status"]


@router.get("/staff/session")
def staff_session(request: Request) -> dict:
    """화면이 연결 방식을 고름: 발표용 데모면 키 없이 바로, 아니면 담당자 키 입력. 비밀값은 돌려주지 않음."""
    if demo_staff(request):
        roles = [] if verification.VerificationConfig().problems() else ["verifier", "clinician"]
        return {"mode": "demo", "staff_id": DEMO_STAFF_ID, "demo_roles": roles}
    return {"mode": "key", "configured": bool(_staff_key())}


@router.get("/staff/cases")
def staff_cases(request: Request) -> dict:
    require_staff(request)
    svc = service()

    def one(d):  # 손상된 사례 하나가 목록 전체를 막지 않음: refresh를 포함해 사례마다 격리 (감사 F-17)
        cid = d["case_id"]
        d = svc.refresh_pairs(d)  # 사례 화면과 같은 쌍 상태 (권한이 바뀌면 저장값이 아니라 지금 권한 기준)
        return {"case_id": cid, "created_at": d["created_at"], "revision": d["revision"],
                "synthetic": d["synthetic"], "pair_counts": _pair_counts(d["current"]["pairs"]),
                "open_requests": sum(1 for r in d["requests"] if r["status"] in ("pending", "requires_document",
                                                                                 "requires_clinician")),
                # 열린 요청을 상태별로 (화면의 '지금 할 일' 안내용): 환자 답 대기 / 서류 대기 / 의사 판단 대기.
                # 확인할 방법이 없어 멈춘 임상 검토(NO_PATH_REASONS)는 할 일이 아니라 on_hold로 따로 셈
                "open_by_status": {s: n for s in ("pending", "requires_document", "requires_clinician", "on_hold")
                                   if (n := sum(1 for r in d["requests"] if _open_state(r) == s))}}

    out, unreadable = svc.each_readable_case(one)
    return {"cases": out, "unreadable": unreadable}


@router.get("/staff/queue")
def staff_queue(request: Request) -> dict:
    require_staff(request)
    with _flow():
        return service().staff_queue()


@router.get("/staff/cases/{case_id}")
def staff_case(case_id: str, request: Request) -> dict:
    require_staff(request)
    with _flow():
        return staff_case_view(service().refresh_pairs(service()._load(case_id)))


@router.post("/staff/cases/{case_id}/requests")
def staff_create_requests(case_id: str, body: RequestCreate, request: Request) -> dict:
    access.check_state_changing(request)
    sid = require_staff(request)
    with _flow():
        out = service().create_requests(case_id, body.proposal_ids, staff_id=sid,
                                        expected_revision=body.expected_revision)
        case = _live_staff_view(out["case"])
    return {"created": out["created"], "existing": out["existing"], "case": case}


@router.post("/staff/cases/{case_id}/requests/{request_id}/status")
def staff_set_status(case_id: str, request_id: str, body: StatusChange, request: Request) -> dict:
    access.check_state_changing(request)
    sid = require_staff(request)
    with _flow():  # 상태 변경은 다시 매칭하지 않음 (새 정보 없음): 운영 상태·쌍 상태·제안만 다시 계산
        out = service().set_status(case_id, request_id, body.status, staff_id=sid)
        case = _live_staff_view(out["case"])
    return {"request": out["request"], "routed": [r["request_id"] for r in out["routed"]], "case": case}


@router.get("/staff/insight")
def staff_insight(request: Request) -> dict:
    """질문별 코디네이터 작업 시간 보기 (web/followup_insight). 판정은 바꾸지 않음."""
    require_staff(request)
    from .followup_insight import insight
    with _flow():
        return insight(service())


class EstimatePick(BaseModel):
    case_id: str = Field(min_length=1, max_length=80)
    proposal_id: str = Field(min_length=1, max_length=200)


class EstimateBody(BaseModel):
    picks: list[EstimatePick] = Field(default_factory=list, max_length=200)
    groups: list[list[EstimatePick]] = Field(default_factory=list, max_length=40)


@router.post("/staff/insight/estimate")
def staff_insight_estimate(body: EstimateBody, request: Request) -> dict:
    """고른 확인들을 함께 보낼 때의 가정 비용·판정 가능 쌍(상한). 읽기만 하고 요청을 만들지 않음."""
    access.check_state_changing(request)
    require_staff(request)
    from .followup_insight import estimate
    with _flow():
        return estimate(service(), [(p.case_id, p.proposal_id) for p in body.picks],
                        [[(p.case_id, p.proposal_id) for p in g] for g in body.groups])


@router.get("/staff/cases/{case_id}/inspector")
def staff_inspector(case_id: str, request: Request) -> dict:
    require_staff(request)
    with _flow():
        return service().inspector_view(case_id)


# ---------------------------------------------------------------------------
# 문서 등록 (연구 담당자), 문서 검증 (검증자), 임상의 판단 (임상의). 계약: docs/dev/document_clinician_api_contract.md
# ---------------------------------------------------------------------------

class DocumentCreate(BaseModel):
    kind: str = Field(pattern=r"^[a-z_]{1,32}$")
    media_type: str = Field(max_length=64)
    content_base64: str = Field(min_length=1, max_length=(verification.MAX_DOCUMENT_BYTES * 4) // 3 + 8)
    expected_revision: int | None = None


class VerificationItem(BaseModel):
    fact_id: str | None = Field(default=None, pattern=r"^F-[0-9a-f]{12}$")
    field: str | None = Field(default=None, max_length=128)
    value: bool | int | float | str | None = None
    unit: str | None = Field(default=None, max_length=32)
    observed_at: str | None = Field(default=None, max_length=10)


class VerificationBody(BaseModel):
    document_id: str = Field(pattern=r"^DOC-[0-9a-f]{16}$")
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[VerificationItem] = Field(min_length=1, max_length=20)
    expected_revision: int | None = None


class JudgmentDecision(BaseModel):
    criterion: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}:[A-Za-z0-9._-]{1,32}$")
    decision: str = Field(pattern=r"^(MET|NOT_MET)$")
    rationale_code: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    basis_fact_ids: list[str] = Field(default_factory=list, max_length=20)


class JudgmentBody(BaseModel):
    decisions: list[JudgmentDecision] = Field(min_length=1, max_length=20)
    expected_revision: int | None = None


@router.post("/staff/cases/{case_id}/documents", status_code=201)
def staff_register_document(case_id: str, body: DocumentCreate, request: Request) -> dict:
    access.check_state_changing(request)
    sid = _role_for(request, require_staff, case_id, "register_document")
    try:
        content = verification.decode_document(body.content_base64)
    except ValueError as e:
        raise HTTPException(400, {"code": "bad_document", "message": str(e)}) from None
    with _flow():
        out = service().register_document(case_id, content=content, media_type=body.media_type, kind=body.kind,
                                          staff_id=sid, expected_revision=body.expected_revision)
        case = _live_staff_view(out["case"])
    return {"document": out["document"], "case": case}


@router.get("/staff/cases/{case_id}/verification")
def staff_verification(case_id: str, request: Request) -> dict:
    require_staff(request)
    with _flow():
        return service().staff_verification(case_id)


@router.get("/staff/cases/{case_id}/audit")
def staff_audit(case_id: str, request: Request) -> dict:
    require_staff(request)
    with _flow():
        return service().audit_view(case_id)


def _open_queue(kind: str, status: str) -> dict:
    svc = service()

    def one(d):  # 사례마다 격리 (감사 F-17)
        cid = d["case_id"]
        return [{"case_id": cid, "synthetic": d["synthetic"], "request_id": r["request_id"],
                 "info_key": r.get("info_key"), "target_criteria": r["target_criteria"],
                 "reason_code": r["reason_code"], "created_at": r["created_at"]}
                for r in d["requests"] if r["kind"] == kind and r["status"] == status]

    per_case, unreadable = svc.each_readable_case(one)
    return {"items": [x for xs in per_case for x in xs], "unreadable": unreadable}


@router.get("/verifier/queue")
def verifier_queue(request: Request) -> dict:
    require_verifier(request)
    with _flow():
        return _open_queue("document_review", "requires_document")


@router.get("/verifier/cases/{case_id}")
def verifier_case(case_id: str, request: Request) -> dict:
    require_verifier(request)
    with _flow():
        return service().verifier_view(case_id)


@router.get("/verifier/cases/{case_id}/documents/{document_id}/content")
def verifier_document(case_id: str, document_id: str, request: Request) -> Response:
    require_verifier(request)
    with _flow():
        content, doc = service().document_content(case_id, document_id)
    return Response(content, media_type=doc["media_type"],
                    headers={"Content-Disposition": f'attachment; filename="{document_id}"',
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                             "X-Document-SHA256": doc["sha256"]})


@router.post("/verifier/cases/{case_id}/requests/{request_id}/verification")
def verifier_verify(case_id: str, request_id: str, body: VerificationBody, request: Request) -> dict:
    access.check_state_changing(request)
    actor = _role_for(request, require_verifier, case_id, "verify_document", request_id)
    _llm_ready()
    with _flow():
        out = service().verify_document(case_id, request_id, verifier_id=actor, document_id=body.document_id,
                                        document_sha256=body.document_sha256,
                                        items=[i.model_dump(exclude_none=True) for i in body.items],
                                        expected_revision=body.expected_revision)
    case = out["case"]
    return {"verification": out["verification"], "request": out["request"],
            "transitions": out["evaluation"]["transitions"], "revision": case["revision"],
            "pairs": case["current"]["pairs"]}


@router.get("/clinician/queue")
def clinician_queue(request: Request) -> dict:
    require_clinician(request)
    with _flow():
        return _open_queue("clinician_review", "requires_clinician")


@router.get("/clinician/cases/{case_id}")
def clinician_case(case_id: str, request: Request) -> dict:
    require_clinician(request)
    with _flow():
        return service().clinician_view(case_id)


@router.post("/clinician/cases/{case_id}/requests/{request_id}/judgment")
def clinician_judge(case_id: str, request_id: str, body: JudgmentBody, request: Request) -> dict:
    access.check_state_changing(request)
    actor = _role_for(request, require_clinician, case_id, "clinician_judgment", request_id)
    _llm_ready()
    with _flow():
        out = service().clinician_judgment(case_id, request_id, clinician_id=actor,
                                           decisions=[d.model_dump() for d in body.decisions],
                                           expected_revision=body.expected_revision)
    case = out["case"]
    return {"judgments": out["judgments"], "request": out["request"],
            "transitions": out["evaluation"]["transitions"], "revision": case["revision"],
            "pairs": case["current"]["pairs"]}
