"""로컬 실시간 웹 UI.

실제 파이프라인(오케스트레이터 + 6개 전문 에이전트)을 백그라운드 스레드에서 실행하고,
에이전트 이벤트를 Server-Sent Events(SSE)로 브라우저에 실시간 전송합니다.
확인 질문이 생성되면 파이프라인이 브라우저의 답변을 기다렸다가 이어서 진행합니다.

실행:
    python -m ctrec.web            # http://127.0.0.1:8000
    python -m ctrec.web --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextvars
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import DISCLAIMER, config, llm, web_metering
from ..agents.recommender import is_open
from ..orchestrator import Orchestrator
from ..pipeline import PatientCase, Session, load_patients
from ..schemas import ClarifyingQuestion
from ..status import MATCH_NOTE
from ..tools import ctgov
from ..trace import Tracer
from . import access, budget, display, retention

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("ctrec.web")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "") or default))
    except ValueError:
        return default


ANSWER_TIMEOUT_S = _env_int("CTREC_WEB_ANSWER_TIMEOUT_S", 30 * 60)  # 확인 질문 답변 대기 (넘으면 '모름'으로 진행)
RUN_TIMEOUT_S = _env_int("CTREC_WEB_RUN_TIMEOUT_S", 60 * 60)  # 실행 전체 시간 제한 (협력적: 다음 확인 지점에서 중단)
# 실행 기록(RUNS) 보관 한도 (아키텍처 §67 web RUNS lifecycle): 메모리에 무한히 쌓이지 않도록
MAX_RUNS = int(os.getenv("CTREC_WEB_MAX_RUNS", "20"))          # 동시에 보관하는 실행 수
RUN_TTL_S = int(os.getenv("CTREC_WEB_RUN_TTL_S", str(2 * 60 * 60)))  # 끝난 실행을 보관하는 시간
ENDED_TTL_S = _env_int("CTREC_WEB_ENDED_TTL_S", 120)  # 취소·오류·시간 초과로 끝난 실행 기록 보관 (종료 이벤트 전달용)
MAX_ACTIVE_PER_CLIENT = _env_int("CTREC_WEB_MAX_ACTIVE_PER_CLIENT", 1)  # 같은 브라우저의 동시 실행 수
MAX_STARTS_PER_HOUR = _env_int("CTREC_WEB_MAX_STARTS_PER_HOUR", 20)  # 같은 브라우저의 시간당 실행 시작 수
MAX_BODY_BYTES = 256 * 1024  # 요청 본문 최대 크기
MAX_TEXT_CHARS = 20000  # 직접 입력 환자 정보 최대 길이
MAX_ANSWER_CHARS = 2000  # 확인 질문 답변 하나의 최대 길이

app = FastAPI(title="ctrec - Interactive Clinical Trial Recommendation")

# 배포용 접근 제한: CTREC_PASSWORD가 설정되어 있으면 HTTP Basic 인증 (아이디는 아무거나)
PASSWORD = os.getenv("CTREC_PASSWORD", "")


@app.middleware("http")
async def limit_body(request: Request, call_next):
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
        return JSONResponse(status_code=413, content={"detail": "요청이 너무 큽니다."})
    return await call_next(request)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not PASSWORD or request.url.path == "/healthz":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            _, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except Exception:
            pw = ""
        if secrets.compare_digest(pw.encode(), PASSWORD.encode()):
            return await call_next(request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="ctrec"'})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # 기본 422 응답은 보낸 값(환자 정보일 수 있음)을 그대로 돌려주므로, 필드 위치와 오류 종류만 알림
    fields = sorted({".".join(str(x) for x in e.get("loc", ()) if x != "body") or "body" for e in exc.errors()})
    return JSONResponse(status_code=422, content={"detail": f"요청 형식이 올바르지 않습니다: {', '.join(fields)}"})


# 화면 보안 헤더 (Phase 6-2). 화면 스크립트·스타일은 모두 같은 서버의 파일(/static)이라 인라인·외부 출처를 허용하지 않음.
# connect-src 'self': API·SSE(EventSource)만. img-src 'self' data:: 마크다운 보고서의 data: 이미지는 DOMPurify가 정화한 뒤에만.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'; "
       "manifest-src 'self'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                    "X-Frame-Options": "DENY", "Cross-Origin-Opener-Policy": "same-origin"}


@app.middleware("http")
async def require_https(request: Request, call_next):
    """루프백이 아닌 평문 HTTP 요청 거절 (상태 확인 /healthz 제외). HTTPS 응답에는 HSTS."""
    if request.url.path != "/healthz" and not access.transport_allowed(request):
        return JSONResponse(status_code=403, content={"detail": "HTTPS로 접속하세요."})
    response = await call_next(request)
    if access.is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


# 가장 바깥 미들웨어 (마지막에 선언): 거절 응답(403·401·413)을 포함한 모든 응답에 보안 헤더
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


# ---------------------------------------------------------------------------
# 데이터 로드
# ---------------------------------------------------------------------------

def _load_trials() -> dict[str, dict]:
    trials = {}
    for p in sorted(config.TRIALS_DIR.glob("*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        if "trial_id" in rec:
            trials[rec["trial_id"]] = ctgov.load_local(p)
    return trials


def _load_cases() -> dict[str, tuple[PatientCase, str]]:
    """patient_id -> (case, 출처 그룹)"""
    cases: dict[str, tuple[PatientCase, str]] = {}
    topic_map_path = config.DATA_DIR / "topic_map.json"
    topic_map = json.loads(topic_map_path.read_text(encoding="utf-8")) if topic_map_path.exists() else {}
    for group, folder in [("사업단 제공", "provided"), ("합성 평가셋", "synthetic")]:
        for p in sorted((config.DATA_DIR / "patients" / folder).glob("*.json")):
            for c in load_patients(p):
                if not c.candidate_trials and c.patient_id in topic_map:
                    c.candidate_trials = topic_map[c.patient_id]
                cases[c.patient_id] = (c, group)
    return cases


# ---------------------------------------------------------------------------
# 실행 상태
# ---------------------------------------------------------------------------

# 실행 상태 전이 (run.lock 안에서만 바꿈. 끝 상태는 한 번 정해지면 바뀌지 않음)
#   running -> cancelling (취소 요청: 작업 스레드가 다음 확인 지점에서 멈춤) -> cancelled
#   running -> completed (결과 전송과 같은 잠금 구간에서 확정 -> 이후 취소 요청은 409) / error / timed_out
#   cancelling -> cancelled (작업이 결과를 냈어도 결과를 보내지 않음)
# 결과 이벤트가 있으면 end 상태는 반드시 completed, 없으면 completed가 아님


@dataclass
class Run:
    run_id: str
    owner_digest: str = ""  # 소유권 토큰의 SHA-256 (평문은 저장하지 않음)
    client_digest: str = ""  # 브라우저 식별 쿠키의 SHA-256 (같은 브라우저 실행 수 제한용)
    events: list[dict] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    answer_ready: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    answers: dict[str, str] = field(default_factory=dict)
    pending_questions: set[str] = field(default_factory=set)  # 답변을 기다리는 질문 ID (없으면 답변을 받지 않음)
    state: str = "running"
    done: bool = False  # 작업 스레드가 실제로 끝났는가 (동시 실행 슬롯은 이때만 반환)
    created_at: float = field(default_factory=time.time)
    deadline: float = 0.0
    finished_at: float | None = None
    meter: Any = None  # 실행별 LLM 호출 기록 (web_metering.WebRunMeter)
    budget: Any = None  # enforce 모드의 실행별 예산 (web/budget.RunBudget). record·off에서는 None
    budget_error: str | None = None  # enforce 준비 실패 코드 (운영자용, 화면에는 종류만)
    metering: dict | None = None  # 끝난 뒤 확정한 기록 요약 (숫자·코드만)

    def emit(self, type_: str, **data: Any) -> None:
        with self.lock:
            self.events.append({"id": len(self.events), "type": type_, "ts": time.time(), **data})

    def stop_reason(self) -> str | None:
        """협력적 취소 확인: 'cancelled' / 'timeout' / None."""
        if self.cancel_requested.is_set():
            return "cancelled"
        if self.deadline and time.time() > self.deadline:
            return "timeout"
        return None


RUNS: dict[str, Run] = {}
RUNS_LOCK = threading.Lock()
CLIENT_STARTS: dict[str, list[float]] = {}  # 브라우저 식별값(해시) -> 최근 1시간 실행 시작 시각


def _active(r: Run) -> bool:
    return not r.done


def _prune_runs(now: float | None = None) -> None:
    """끝난 실행 기록 정리. 정상 완료는 RUN_TTL_S, 취소·오류·시간 초과는 ENDED_TTL_S 뒤 삭제하고,
    그래도 MAX_RUNS 이상이면 오래 전에 끝난 실행부터 지움. 작업 스레드가 아직 도는 실행은 지우지 않음."""
    now = time.time() if now is None else now
    with RUNS_LOCK:
        for rid, r in list(RUNS.items()):
            ttl = RUN_TTL_S if r.state == "completed" else ENDED_TTL_S
            if r.done and r.finished_at is not None and now - r.finished_at > ttl:
                del RUNS[rid]
        finished = sorted((r for r in RUNS.values() if r.done), key=lambda r: r.finished_at or r.created_at)
        while len(RUNS) >= MAX_RUNS and finished:
            del RUNS[finished.pop(0).run_id]
        for key, starts in list(CLIENT_STARTS.items()):
            CLIENT_STARTS[key] = [t for t in starts if now - t < 3600]
            if not CLIENT_STARTS[key]:
                del CLIENT_STARTS[key]


class WebAnswerer:
    """확인 질문을 브라우저로 보내고 답변이 올 때까지 대기 (취소·실행 시간 제한을 짧은 간격으로 확인)."""

    def __init__(self, run: Run):
        self.run = run

    def answer(self, questions: list[ClarifyingQuestion], patient_id: str) -> dict[str, str]:
        run = self.run
        llm.guard()
        run.answer_ready.clear()
        with run.lock:
            run.answers = {}
            run.pending_questions = {q.question_id for q in questions}
        run.emit("questions", questions=[q.model_dump() for q in questions])
        wait_until = time.time() + ANSWER_TIMEOUT_S
        while not run.answer_ready.wait(0.2):
            reason = run.stop_reason()
            if reason or time.time() > wait_until:
                with run.lock:
                    run.pending_questions = set()
                if reason:
                    raise llm.RunCancelled(reason)
                run.emit("status", message="답변 대기 시간이 초과되어 '모름'으로 처리합니다.")
                break
        llm.guard()  # 답변을 받는 사이 취소됐으면 재평가(유료 호출)로 넘어가지 않음
        return {q.question_id: (run.answers.get(q.question_id) or "모름") for q in questions}


class RunRequest(BaseModel):
    patient_id: str | None = Field(default=None, max_length=100)
    custom_text: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    trial_ids: list[str] = Field(default=[], max_length=200)
    screen: bool = True  # True: 전체 시험에서 후보 추림 에이전트가 고름 / False: trial_ids만 평가
    allow_search: bool = False
    max_rounds: int = Field(default=config.MAX_CLARIFY_ROUNDS, ge=0, le=config.MAX_CLARIFY_ROUNDS)
    decision_diagnostic: bool = False  # 개발자용 Decision Loop 진단 (기본 off, 판정·질문·추천에 쓰지 않음)


class AnswerRequest(BaseModel):
    answers: dict[str, str] = Field(max_length=50)


# 화면으로 보내는 실행 기록 중 예외 문구를 담는 이벤트: 예외 메시지에는 환자 정보가 들어 있을 수 있으므로 종류만 보냄
_ERROR_TRACES = {("orchestrator", "tool_error"), ("pipeline", "error")}


def _forward(run: "Run", rec: dict) -> None:
    """실행 기록을 화면으로 전달. Decision Loop 진단은 별도 이벤트(decision_diagnostic)로 구분 (켰을 때만 생김)."""
    if rec.get("agent") == "decision":
        run.emit("decision_diagnostic", diagnostic=rec.get("diagnostic"))
        return
    run.emit("trace", record=_client_record(rec))


def _client_record(rec: dict) -> dict:
    if (rec.get("agent"), rec.get("event")) not in _ERROR_TRACES:
        return rec
    kind = str(rec.get("message", "")).split("(", 1)[0].split(":", 1)[0].strip() or "Error"
    return {**rec, "message": f"{kind} (자세한 내용은 서버 실행 기록 참고)"}


_END_MESSAGE = {"cancelled": "실행이 취소되었습니다.", "timed_out": "실행 시간 제한을 넘어 중단했습니다.",
                budget.EXCEEDED: "비용 한도에 도달해 새 AI 호출을 멈췄습니다. 분석이 끝나지 않았으므로 추천 결과가 아닙니다.",
                budget.USAGE_UNKNOWN: "AI 호출의 사용량을 확인할 수 없어 안전을 위해 분석을 멈췄습니다.",
                budget.BUDGET_ERROR: "서버의 비용 관리 기능에 문제가 있어 분석을 멈췄습니다. 관리자에게 문의하세요."}
_BUDGET_OUTCOMES = (budget.EXCEEDED, budget.USAGE_UNKNOWN, budget.BUDGET_ERROR)


def _display(session) -> dict | None:
    """결과 화면용 표시 정보 (판정은 그대로, 분류·종류만). 만들지 못해도 결과 전송은 막지 않음 (화면은 중립 안내)."""
    try:
        return display.build(session)
    except Exception as e:  # noqa: BLE001
        log.warning("web display build failed: %s", type(e).__name__)
        return None


def _execute(run: Run, req: RunRequest, case: PatientCase, trials: dict[str, dict]) -> None:
    """실행 스레드 입구. record: 호출을 기록만 (상한 없음). enforce: 호출마다 예산 예약·정산 (web/budget.py)."""
    run.meter = web_metering.WebRunMeter(run.run_id)
    if run.meter.mode != "enforce":
        with run.meter.recording():
            _execute_run(run, req, case, trials)
        return
    try:  # 준비 실패는 조용히 기록 전용으로 내려가지 않고 실행을 비용 관리 오류로 끝냄
        cfg = budget.load_config(default_run_seconds=RUN_TIMEOUT_S)
        ctx = budget.run_context(run.run_id, cfg, budget.get_ledger(cfg), budget.make_inner(), run.meter)
    except Exception as e:  # noqa: BLE001 - 종류만 기록
        run.budget_error = type(e).__name__
        log.warning("web budget setup failed: %s", run.budget_error)
        _execute_run(run, req, case, trials, preflight=budget.BudgetStop("ledger_unavailable"))
        return
    with ctx as run_budget:
        run.budget = run_budget
        _execute_run(run, req, case, trials)


def _budget_fields(run: Run) -> dict:
    if run.budget is not None:
        return run.budget.summary()
    if run.meter is not None and run.meter.mode == "enforce":
        return {"budget_mode": "enforce", "budget_status": budget.BUDGET_ERROR, "cap_enforced": True,
                "last_block_reason": "ledger_unavailable"}
    return {"budget_mode": run.meter.mode if run.meter is not None else web_metering.mode(), "cap_enforced": False}


def _execute_run(run: Run, req: RunRequest, case: PatientCase, trials: dict[str, dict],
                 preflight: Exception | None = None) -> None:
    llm.reset_usage()
    llm.set_guard(run.stop_reason)  # 이 실행(과 하위 스레드)의 API 호출 직전마다 취소·시간 초과 확인
    outcome, error_type = "completed", None
    session = None
    try:
        if preflight is not None:
            raise preflight
        # 저장은 기본 off (retention.save_enabled). 켜져 있을 때만 trace.jsonl·result.json·report.md를 씀
        out_dir = retention.prepare_run_dir(run.run_id) if retention.save_enabled() else None
        tracer = Tracer(out_dir / "trace.jsonl" if out_dir else None, verbose=False,
                        listener=lambda rec: _forward(run, rec))
        # 웹 UI: 확인 질문이 필요하면 화면에서 사용자가 직접 답변 (가상 환자·답변 없음 모드는 CLI 평가용)
        session = Session(case=case, trials=trials, answerer=WebAnswerer(run), tracer=tracer,
                          max_rounds=req.max_rounds, allow_search=req.allow_search,
                          screen_trials=req.screen and len(trials) > config.SCREEN_MAX_CANDIDATES,
                          decision_loop="diagnostic" if req.decision_diagnostic else "off")
        # 웹 UI는 오케스트레이터(agent) 모드로만 실행
        tracer.log("pipeline", "start", message=f"mode=agent model={config.MODEL} trials={len(trials)}")
        Orchestrator(session).run()
        llm.guard()  # 오케스트레이터가 끝난 뒤라도 취소·시간 초과였으면 결과를 완료로 내보내지 않음
        if run.budget is not None and run.budget.stopped:
            # 예산 차단은 오케스트레이터·병렬 Matcher가 오류로 삼켜도 여기서 다시 확인: 중단된 분석을 완료로 내보내지 않음
            raise budget.BudgetStop(run.budget.stopped)
        if out_dir is not None:
            session.save(out_dir)
        with run.lock:  # 취소 요청과 경합: 아직 running일 때만 결과를 보내고, 같은 잠금 안에서 completed로 확정
            if run.state == "running":
                run.state = "completed"
                run.emit("result", result=display.redact_errors(session.result()), report=session.report, match_note=MATCH_NOTE,
                         display=_display(session),
                         trials={tid: {"title": t.get("title"), "overall_status": t.get("overall_status"),
                                       "url": (t.get("source") or {}).get("url")} for tid, t in session.trials.items()})
    except budget.BudgetStop as e:
        outcome = e.category
    except llm.RunCancelled as e:
        outcome = "timed_out" if e.args and e.args[0] == "timeout" else "cancelled"
    except Exception as e:  # noqa: BLE001 - 오류도 화면에 표시 (예외 메시지에는 환자 정보가 있을 수 있으므로 종류만)
        outcome, error_type = "error", type(e).__name__
    finally:
        if outcome in ("error", "cancelled", "timed_out") and run.budget is not None and run.budget.stopped                 and not run.cancel_requested.is_set():
            outcome = budget.CATEGORY.get(run.budget.stopped, budget.BUDGET_ERROR)  # 원인이 예산 차단이면 그것으로
        with run.lock:
            if run.state == "completed":
                outcome = "completed"  # 결과를 보낸 실행은 이후 무슨 일이 있어도 완료 (취소·오류로 바뀌지 않음)
            elif run.state == "cancelling":
                outcome = "cancelled"  # 취소 요청 뒤에 끝났으면 결과·오류와 상관없이 취소
            elif outcome == "completed":
                outcome = "error"  # 결과 없이 정상 종료된 경우(예: 취소·실행 상태 불일치)는 완료로 보이지 않게
                error_type = error_type or "NoResult"
            if outcome == "error":
                run.emit("error", message=f"실행 중 오류가 발생했습니다 ({error_type}). 다시 시도하거나 관리자에게 문의하세요.")
            elif outcome in _END_MESSAGE:
                run.emit("status", message=_END_MESSAGE[outcome])
            run.state = outcome
            run.pending_questions = set()
            try:  # 기록 요약 실패가 실행 종료 처리에 영향을 주지 않음
                run.meter.finish()
                run.metering = {**run.meter.summary(llm_expected=session is not None and session.profile is not None),
                                **_budget_fields(run)}
            except Exception as e:  # noqa: BLE001
                run.metering = {"run_id": run.run_id, "status": web_metering.UNMETERED,
                                "status_reason": f"metering_error:{type(e).__name__}",
                                "cost_usd": web_metering.UNKNOWN, "cost_cap_enforced": False}
            run.done = True
            run.finished_at = time.time()
            run.emit("end", state=outcome)
        if outcome != "completed":
            log.warning("web run ended: state=%s error_type=%s", outcome, error_type)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    # 화면 수정이 바로 반영되도록 캐시하지 않음
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


# 화면 정적 파일: 이름을 정해 둔 파일만 (경로 조작 불가). 외부 라이브러리는 VENDOR.md 참고
STATIC_FILES = {"app.js": "text/javascript", "app.css": "text/css", "marked.min.js": "text/javascript",
                "purify.min.js": "text/javascript"}


@app.get("/static/{name}")
def static_file(name: str) -> FileResponse:
    if name not in STATIC_FILES:
        raise HTTPException(404, "찾을 수 없습니다.")
    return FileResponse(STATIC / name, media_type=STATIC_FILES[name], headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/config")
def get_config() -> dict:
    return {"model": config.MODEL, "disclaimer": DISCLAIMER, "max_rounds": config.MAX_CLARIFY_ROUNDS}


@app.get("/api/patients")
def list_patients() -> list[dict]:
    return [
        {"patient_id": c.patient_id, "group": group, "text": c.raw_text,
         "candidate_trials": c.candidate_trials or [], "has_hidden": bool(c.hidden_details)}
        for c, group in _load_cases().values()
    ]


@app.get("/api/trials")
def list_trials() -> list[dict]:
    return [
        {"trial_id": tid, "title": t.get("title"), "overall_status": t.get("overall_status"),
         "open": is_open(t), "conditions": t.get("conditions", []), "synthetic": t.get("synthetic", False)}
        for tid, t in _load_trials().items()
    ]


_NOT_FOUND = "실행을 찾을 수 없거나 접근 권한이 없습니다."


def _owned_run(run_id: str, request: Request) -> Run:
    """소유권 쿠키가 맞는 실행. 없는 실행과 권한 없음은 같은 응답 (존재 여부를 드러내지 않음)."""
    run = RUNS.get(run_id)
    if run is None or not access.matches(request.cookies.get(access.RUN_COOKIE), run.owner_digest):
        raise HTTPException(404, _NOT_FOUND)
    return run


_last_purge = 0.0


def _purge_outputs_periodically() -> None:
    global _last_purge
    now = time.time()
    if now - _last_purge < 600:
        return
    _last_purge = now
    try:
        retention.purge_expired(now)
    except Exception as e:  # noqa: BLE001 - 정리 실패가 실행 시작을 막지 않음
        log.warning("web output purge failed: %s", type(e).__name__)


@app.post("/api/runs")
def start_run(req: RunRequest, request: Request, response: Response) -> dict:
    access.check_state_changing(request)
    if web_metering.mode() == "enforce":
        # 비용 통제를 켰는데 설정·원장이 준비되지 않았으면 시작하지 않음 (기록 전용으로 조용히 내려가지 않음)
        try:
            budget.get_ledger(budget.load_config(default_run_seconds=RUN_TIMEOUT_S))
        except (budget.BudgetConfigError, budget.LedgerError) as e:
            log.warning("web budget not ready: %s", getattr(e, "problems", None) or (e.args[0] if e.args else type(e).__name__))
            raise HTTPException(503, "서버의 비용 관리 설정에 문제가 있어 지금은 분석을 시작할 수 없습니다. 관리자에게 문의하세요.")
    all_trials = _load_trials()
    trials = dict(all_trials) if req.screen else {t: all_trials[t] for t in req.trial_ids if t in all_trials}
    if not trials and not req.allow_search:
        raise HTTPException(400, "평가할 임상시험을 하나 이상 선택하세요.")
    if req.custom_text is not None and not req.custom_text.strip() and not req.patient_id:
        raise HTTPException(400, "환자 정보를 입력하세요.")
    if req.custom_text and req.custom_text.strip():
        case = PatientCase(patient_id=f"WEB-{uuid.uuid4().hex[:6].upper()}", raw_text=req.custom_text.strip())
    else:
        if not req.patient_id:
            raise HTTPException(400, "환자를 선택하거나 환자 정보를 입력하세요.")
        cases = _load_cases()
        if req.patient_id not in cases:
            raise HTTPException(404, "선택한 환자를 찾을 수 없습니다.")
        src = cases[req.patient_id][0]
        # 확인 질문 답변이 raw_text에 누적되므로 원본을 복사해 사용
        case = PatientCase(patient_id=src.patient_id, raw_text=src.raw_text, hidden_details=src.hidden_details,
                           input_source=src.input_source)

    _purge_outputs_periodically()
    client_token = request.cookies.get(access.CLIENT_COOKIE) or access.new_token()
    client_digest = access.digest(client_token)
    owner_token = access.new_token()
    # 실행 ID는 식별자일 뿐 권한이 아님 (권한은 소유권 쿠키). 그래도 추측하기 어렵게 128비트 난수
    run = Run(run_id=time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(16),
              owner_digest=access.digest(owner_token), client_digest=client_digest)
    run.deadline = run.created_at + RUN_TIMEOUT_S
    _prune_runs()
    with RUNS_LOCK:
        mine = [r for r in RUNS.values() if r.client_digest == client_digest and _active(r)]
        if len(mine) >= MAX_ACTIVE_PER_CLIENT:
            raise HTTPException(429, "이 브라우저에서 진행 중인 실행이 있습니다. 끝나거나 취소한 뒤 다시 시작하세요.")
        if len(CLIENT_STARTS.get(client_digest, [])) >= MAX_STARTS_PER_HOUR:
            raise HTTPException(429, "이 브라우저의 실행 횟수 한도를 넘었습니다. 잠시 후 다시 시도하세요.")
        if len(RUNS) >= MAX_RUNS:
            raise HTTPException(429, f"진행 중인 실행이 {MAX_RUNS}개라 새 실행을 시작할 수 없습니다. 잠시 후 다시 시도하세요.")
        RUNS[run.run_id] = run
        CLIENT_STARTS.setdefault(client_digest, []).append(run.created_at)
    access.set_cookie(response, request, access.RUN_COOKIE, owner_token, path=f"/api/runs/{run.run_id}",
                      max_age=RUN_TIMEOUT_S + RUN_TTL_S)
    access.set_cookie(response, request, access.CLIENT_COOKIE, client_token, path="/api", max_age=30 * 24 * 3600)
    run.emit("status", message="파이프라인 시작", patient_id=case.patient_id, trial_ids=list(trials))
    ctx = contextvars.copy_context()  # 실행별 토큰 사용량 집계·취소 확인
    threading.Thread(target=ctx.run, args=(_execute, run, req, case, trials), daemon=True).start()
    return {"run_id": run.run_id}


@app.get("/api/runs/active")
def active_run(request: Request) -> dict:
    """탭을 닫았다 다시 연 경우용: 이 브라우저(클라이언트 쿠키)가 시작한 진행 중 실행의 ID만 알려 줌.
    내용 접근은 여전히 실행별 소유권 쿠키가 필요 (같은 브라우저에만 있음). 끝난 실행은 알려 주지 않음."""
    token = request.cookies.get(access.CLIENT_COOKIE)
    if not token:
        return {"run_id": None}
    d = access.digest(token)
    with RUNS_LOCK:
        mine = sorted((r for r in RUNS.values() if hmac.compare_digest(r.client_digest or "", d) and _active(r)),
                      key=lambda r: r.created_at)
    return {"run_id": mine[-1].run_id if mine else None}


@app.get("/api/runs/{run_id}")
def run_status(run_id: str, request: Request) -> dict:
    """새로고침 후 이어 보기용 상태 확인 (소유자만)."""
    run = _owned_run(run_id, request)
    with run.lock:
        return {"run_id": run.run_id, "state": run.state, "done": run.done,
                "waiting_for_answers": bool(run.pending_questions)}


@app.get("/api/runs/{run_id}/metering")
def run_metering(run_id: str, request: Request) -> dict:
    """이 실행의 LLM 호출 기록 (소유자만, 숫자·코드·모델 이름만). 기록 전용 - 비용 상한을 보장하지 않음."""
    run = _owned_run(run_id, request)
    with run.lock:
        if run.metering is not None:
            return {**run.metering, "in_progress": False}
        meter = run.meter
    if meter is None:
        return {"run_id": run.run_id, "status": web_metering.UNMETERED, "status_reason": "not_started",
                "cost_usd": web_metering.UNKNOWN, "cost_cap_enforced": False, "in_progress": True}
    return {**meter.summary(llm_expected=False), **_budget_fields(run), "in_progress": True}  # 진행 중: 확정 전 값


@app.post("/api/runs/{run_id}/answers")
def submit_answers(run_id: str, body: AnswerRequest, request: Request) -> dict:
    access.check_state_changing(request)
    run = _owned_run(run_id, request)
    if any(len(v) > MAX_ANSWER_CHARS for v in body.answers.values()):
        raise HTTPException(400, f"답변은 {MAX_ANSWER_CHARS}자 이하로 입력하세요.")
    with run.lock:
        pending = run.pending_questions
        if run.state != "running" or not pending:  # 끝난·취소된 실행, 질문 전, 이미 답한 질문(중복 제출)
            raise HTTPException(409, "지금은 답변을 받을 수 있는 질문이 없습니다.")
        run.answers = {q: a for q, a in body.answers.items() if q in pending}
        run.pending_questions = set()
        run.emit("answers", n_answered=sum(bool(a.strip()) for a in run.answers.values()))  # 답변 내용은 보내지 않음
    run.answer_ready.set()
    return {"ok": True}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request) -> dict:
    """실행 취소 (소유자만). 작업 스레드는 다음 확인 지점(API 호출 직전, 답변 대기)에서 멈추고 end 이벤트를 보냄."""
    access.check_state_changing(request)
    run = _owned_run(run_id, request)
    with run.lock:
        if run.state != "running":
            raise HTTPException(409, "이미 끝났거나 취소 중인 실행입니다.")
        run.state = "cancelling"
        run.pending_questions = set()
        run.cancel_requested.set()
        run.emit("status", message="취소 요청을 받았습니다. 진행 중인 단계가 끝나면 멈춥니다.", cancelling=True)
    run.answer_ready.set()
    return {"ok": True, "state": "cancelling"}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    run = _owned_run(run_id, request)
    # 이어 받기: 브라우저 자동 재연결은 Last-Event-ID 헤더, 화면이 직접 다시 연결할 때는 ?after=<마지막 id>
    # (EventSource는 헤더를 직접 지정할 수 없음). 둘 다 없으면 처음부터
    resume_from = request.headers.get("last-event-id", request.query_params.get("after", -1))
    try:
        start = max(int(resume_from) + 1, 0)
    except ValueError:  # 잘못된 값은 처음부터
        start = 0

    async def gen():
        i = start
        while True:
            if await request.is_disconnected():
                return
            with run.lock:
                new = run.events[i:]
            for ev in new:
                yield f"id: {ev['id']}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                i = ev["id"] + 1
                if ev["type"] == "end":
                    return
            if not new:
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="ctrec 웹 UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    print(f"\n{DISCLAIMER}\n\n웹 UI: http://{args.host}:{args.port}  (모델: {config.MODEL})\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
