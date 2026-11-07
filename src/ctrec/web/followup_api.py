"""추가 정보 확인 API (TRIALOG O): 환자 입력 -> 연구 담당자 요청 -> 환자 응답 -> 재평가. 계약: docs/dev/followup_api_contract.md

권한 (web/access.py와 같은 원칙)
- 환자: 사례를 만들 때 사례별 토큰(256비트)을 HttpOnly·SameSite=Strict 쿠키 ctrec_case로만 발급
  (Path=/api/followup/cases/<case_id>). 서버는 SHA-256만 저장. 없는 사례·권한 없음은 같은 404.
- 연구 담당자: X-CTREC-Staff-Key 헤더가 서버 설정 CTREC_STAFF_KEY와 같아야 함 (설정이 없으면 담당자 API 전체 503).
  X-CTREC-Staff-Id(영문·숫자 1~64자)는 기록용 이름. 계정 인증을 대신하지 않습니다.
- 담당자는 환자 답변을 대신 입력할 수 없음: 응답 API는 환자 쿠키만 받고, 담당자 헤더가 붙은 응답 요청은 403.
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
from ..followup.service import FollowupError, FollowupService, TrialCatalog
from ..followup.store import CaseStore
from . import access, budget

log = logging.getLogger("ctrec.web.followup")
router = APIRouter(prefix="/api/followup")

CASE_COOKIE = "ctrec_case"
STAFF_HEADER = "x-ctrec-staff-key"
STAFF_ID_HEADER = "x-ctrec-staff-id"
STAFF_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CASE_COOKIE_MAX_AGE = 30 * 24 * 3600
MAX_TEXT_CHARS = 20000
MAX_ANSWER_CHARS = 2000

_SERVICE: FollowupService | None = None


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
    return bool(request.headers.get(STAFF_HEADER))


def require_staff(request: Request) -> str:
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
            "requests": data["requests"],
            "responses": [{k: v for k, v in r.items() if k != "answer_digest"} for r in data["responses"]],
            "evaluations": data["evaluations"], "events": data["events"], "view": cur["view"]}


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
    return patient_case_view(_patient_case(case_id, request))


@router.get("/cases/{case_id}/requests")
def patient_requests(case_id: str, request: Request) -> dict:
    _patient_case(case_id, request)
    with _flow():
        return service().patient_requests(case_id)


@router.post("/cases/{case_id}/requests/{request_id}/response")
def respond(case_id: str, request_id: str, body: ResponseBody, request: Request) -> dict:
    access.check_state_changing(request)
    if _is_staff_request(request):
        raise HTTPException(403, {"code": "staff_cannot_answer",
                                  "message": "연구 담당자는 환자 답변을 대신 입력할 수 없습니다."})
    _patient_case(case_id, request)
    if not body.unknown and not (body.answer or "").strip():
        raise HTTPException(400, {"code": "empty_answer", "message": "답을 입력하거나 '모름'을 선택하세요."})
    _llm_ready()
    with _flow():
        out = service().respond(case_id, request_id, answer=body.answer, unknown=body.unknown,
                                client_response_id=body.client_response_id,
                                validate=lambda spec, a: _app().answer_problem(spec, a))
    r = out["response"]
    return {"duplicate": out["duplicate"], "request_id": request_id, "status": out["request"]["status"],
            "outcome": r["outcome"], "revision": out["case"]["revision"],
            "conflict": bool(r.get("conflicts")), "case": patient_case_view(out["case"])}


# ---------------------------------------------------------------------------
# 연구 담당자 API
# ---------------------------------------------------------------------------

class RequestCreate(BaseModel):
    proposal_ids: list[str] = Field(min_length=1, max_length=50)
    expected_revision: int | None = None


class StatusChange(BaseModel):
    status: str = Field(pattern=r"^(unreachable|document_unavailable|cancelled)$")


@router.get("/staff/cases")
def staff_cases(request: Request) -> dict:
    require_staff(request)
    svc = service()
    out = []
    for cid in svc.store.case_ids():
        d = svc.store.load(cid)
        out.append({"case_id": cid, "created_at": d["created_at"], "revision": d["revision"],
                    "synthetic": d["synthetic"], "pair_counts": _pair_counts(d["current"]["pairs"]),
                    "open_requests": sum(1 for r in d["requests"] if r["status"] in ("pending", "requires_document",
                                                                                     "requires_clinician"))})
    return {"cases": out}


@router.get("/staff/queue")
def staff_queue(request: Request) -> dict:
    require_staff(request)
    with _flow():
        return service().staff_queue()


@router.get("/staff/cases/{case_id}")
def staff_case(case_id: str, request: Request) -> dict:
    require_staff(request)
    with _flow():
        return staff_case_view(service()._load(case_id))


@router.post("/staff/cases/{case_id}/requests")
def staff_create_requests(case_id: str, body: RequestCreate, request: Request) -> dict:
    access.check_state_changing(request)
    sid = require_staff(request)
    with _flow():
        out = service().create_requests(case_id, body.proposal_ids, staff_id=sid,
                                        expected_revision=body.expected_revision)
    return {"created": out["created"], "existing": out["existing"], "case": staff_case_view(out["case"])}


@router.post("/staff/cases/{case_id}/requests/{request_id}/status")
def staff_set_status(case_id: str, request_id: str, body: StatusChange, request: Request) -> dict:
    access.check_state_changing(request)
    sid = require_staff(request)
    with _flow():  # 상태 변경은 다시 매칭하지 않음 (새 정보 없음): 운영 상태·쌍 상태·제안만 다시 계산
        out = service().set_status(case_id, request_id, body.status, staff_id=sid)
    return {"request": out["request"], "routed": [r["request_id"] for r in out["routed"]],
            "case": staff_case_view(out["case"])}


@router.get("/staff/cases/{case_id}/inspector")
def staff_inspector(case_id: str, request: Request) -> dict:
    require_staff(request)
    with _flow():
        return service().inspector_view(case_id)
