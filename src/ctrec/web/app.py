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
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from .. import DISCLAIMER, config, llm
from ..agents.recommender import is_open
from ..orchestrator import Orchestrator
from ..pipeline import PatientCase, Session, load_patients
from ..schemas import ClarifyingQuestion
from ..tools import ctgov
from ..trace import Tracer

STATIC = Path(__file__).parent / "static"
ANSWER_TIMEOUT_S = 30 * 60

app = FastAPI(title="ctrec - Interactive Clinical Trial Recommendation")

# 배포용 접근 제한: CTREC_PASSWORD가 설정되어 있으면 HTTP Basic 인증 (아이디는 아무거나)
PASSWORD = os.getenv("CTREC_PASSWORD", "")


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

@dataclass
class Run:
    run_id: str
    events: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    answer_ready: threading.Event = field(default_factory=threading.Event)
    answers: dict[str, str] = field(default_factory=dict)
    done: bool = False

    def emit(self, type_: str, **data: Any) -> None:
        with self.lock:
            self.events.append({"id": len(self.events), "type": type_, "ts": time.time(), **data})


RUNS: dict[str, Run] = {}


class WebAnswerer:
    """확인 질문을 브라우저로 보내고 답변이 올 때까지 대기."""

    def __init__(self, run: Run):
        self.run = run

    def answer(self, questions: list[ClarifyingQuestion], patient_id: str) -> dict[str, str]:
        self.run.answer_ready.clear()
        self.run.answers = {}
        self.run.emit("questions", questions=[q.model_dump() for q in questions])
        if not self.run.answer_ready.wait(ANSWER_TIMEOUT_S):
            self.run.emit("status", message="답변 대기 시간이 초과되어 '모름'으로 처리합니다.")
        return {q.question_id: (self.run.answers.get(q.question_id) or "모름") for q in questions}


class RunRequest(BaseModel):
    patient_id: str | None = None
    custom_text: str | None = None
    trial_ids: list[str]
    allow_search: bool = False
    max_rounds: int = config.MAX_CLARIFY_ROUNDS


class AnswerRequest(BaseModel):
    answers: dict[str, str]


def _execute(run: Run, req: RunRequest, case: PatientCase, trials: dict[str, dict]) -> None:
    llm.reset_usage()
    out_dir = config.OUTPUT_DIR / "web" / run.run_id
    tracer = Tracer(out_dir / "trace.jsonl", verbose=False,
                    listener=lambda rec: run.emit("trace", record=rec))
    # 웹 UI: 확인 질문이 필요하면 화면에서 사용자가 직접 답변 (가상 환자·답변 없음 모드는 CLI 평가용)
    answerer = WebAnswerer(run)
    session = Session(case=case, trials=trials, answerer=answerer, tracer=tracer,
                      max_rounds=req.max_rounds, allow_search=req.allow_search)
    try:
        # 웹 UI는 오케스트레이터(agent) 모드로만 실행
        tracer.log("pipeline", "start", message=f"mode=agent model={config.MODEL} trials={len(trials)}")
        Orchestrator(session).run()
        session.save(out_dir)
        tracer.log("pipeline", "saved", message=str(out_dir))
        run.emit("result", result=session.result(), report=session.report,
                 trials={tid: {"title": t.get("title"), "overall_status": t.get("overall_status"),
                               "url": (t.get("source") or {}).get("url")} for tid, t in session.trials.items()})
    except Exception as e:  # 오류도 화면에 표시
        run.emit("error", message=f"{type(e).__name__}: {e}")
    finally:
        run.done = True
        run.emit("end")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    # 화면 수정이 바로 반영되도록 캐시하지 않음
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


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


@app.post("/api/runs")
def start_run(req: RunRequest) -> dict:
    all_trials = _load_trials()
    trials = {t: all_trials[t] for t in req.trial_ids if t in all_trials}
    if not trials and not req.allow_search:
        raise HTTPException(400, "평가할 임상시험을 하나 이상 선택하세요.")
    if req.custom_text and req.custom_text.strip():
        case = PatientCase(patient_id=f"WEB-{uuid.uuid4().hex[:6].upper()}", raw_text=req.custom_text.strip())
    else:
        cases = _load_cases()
        if req.patient_id not in cases:
            raise HTTPException(404, f"환자 {req.patient_id}를 찾을 수 없습니다.")
        src = cases[req.patient_id][0]
        # 확인 질문 답변이 raw_text에 누적되므로 원본을 복사해 사용
        case = PatientCase(patient_id=src.patient_id, raw_text=src.raw_text, hidden_details=src.hidden_details)

    run = Run(run_id=time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4])
    RUNS[run.run_id] = run
    run.emit("status", message="파이프라인 시작", patient_id=case.patient_id, patient_text=case.raw_text,
             trial_ids=list(trials))
    ctx = contextvars.copy_context()  # 실행별 토큰 사용량 집계
    threading.Thread(target=ctx.run, args=(_execute, run, req, case, trials), daemon=True).start()
    return {"run_id": run.run_id}


@app.post("/api/runs/{run_id}/answers")
def submit_answers(run_id: str, body: AnswerRequest) -> dict:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "실행을 찾을 수 없습니다.")
    run.answers = body.answers
    run.emit("answers", answers=body.answers)
    run.answer_ready.set()
    return {"ok": True}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "실행을 찾을 수 없습니다.")
    start = int(request.headers.get("last-event-id", -1)) + 1

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
