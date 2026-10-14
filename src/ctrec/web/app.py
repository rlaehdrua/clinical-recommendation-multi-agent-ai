"""로컬 실시간 웹 UI.

실제 파이프라인(오케스트레이터 + 6개 전문 에이전트)을 백그라운드 스레드에서 실행하고,
에이전트 이벤트를 Server-Sent Events(SSE)로 브라우저에 실시간 전송합니다.
확인 질문이 생성되면 파이프라인이 브라우저의 답변을 기다렸다가 이어서 진행합니다.

실행:
    python -m ctrec.web            # http://127.0.0.1:8000
    python -m ctrec.web --port 8080

저장된 실행(outputs/web/<run_id>)은 API 호출 없이 '다시 보기'로 재생할 수 있습니다(시연용).
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
from ..pipeline import PatientCase, Session, load_patients, trace_match
from ..schemas import ClarifyingQuestion, TrialMatch
from ..status import MATCH_NOTE
from ..tools import ctgov
from ..trace import Tracer

STATIC = Path(__file__).parent / "static"
ANSWER_TIMEOUT_S = 30 * 60
# 실행 기록(RUNS) 보관 한도 (아키텍처 §67 web RUNS lifecycle): 메모리에 무한히 쌓이지 않도록
MAX_RUNS = int(os.getenv("CTREC_WEB_MAX_RUNS", "20"))          # 동시에 보관하는 실행 수
RUN_TTL_S = int(os.getenv("CTREC_WEB_RUN_TTL_S", str(2 * 60 * 60)))  # 끝난 실행을 보관하는 시간
REPLAY_DIR = config.OUTPUT_DIR / "web"
REPLAY_MAX_GAP_S = 1.2  # 다시 보기: 이벤트 사이 대기 상한 (LLM 대기 시간을 줄여 보여 줌)

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
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def emit(self, type_: str, **data: Any) -> None:
        with self.lock:
            self.events.append({"id": len(self.events), "type": type_, "ts": time.time(), **data})


RUNS: dict[str, Run] = {}
RUNS_LOCK = threading.Lock()


def _prune_runs(now: float | None = None) -> None:
    """끝난 지 RUN_TTL_S가 지난 실행을 지우고, 그래도 MAX_RUNS 이상이면 오래 전에 끝난 실행부터 지움.

    진행 중인 실행은 지우지 않습니다(답변 대기 중일 수 있음). 결과 파일(outputs/web/<run_id>)은 남습니다.
    """
    now = time.time() if now is None else now
    with RUNS_LOCK:
        for rid, r in list(RUNS.items()):
            if r.done and r.finished_at is not None and now - r.finished_at > RUN_TTL_S:
                del RUNS[rid]
        finished = sorted((r for r in RUNS.values() if r.done), key=lambda r: r.finished_at or r.created_at)
        while len(RUNS) >= MAX_RUNS and finished:
            del RUNS[finished.pop(0).run_id]


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
    trial_ids: list[str] = []
    screen: bool = True  # True: 전체 시험에서 후보 추림 에이전트가 고름 / False: trial_ids만 평가
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
                      max_rounds=req.max_rounds, allow_search=req.allow_search,
                      screen_trials=req.screen and len(trials) > config.SCREEN_MAX_CANDIDATES)
    try:
        # 웹 UI는 오케스트레이터(agent) 모드로만 실행
        tracer.log("pipeline", "start", message=f"mode=agent model={config.MODEL} trials={len(trials)}")
        Orchestrator(session).run()
        session.save(out_dir)
        tracer.log("pipeline", "saved", message=str(out_dir))
        run.emit("result", result=session.result(), report=session.report, match_note=MATCH_NOTE,
                 trials=_trial_brief(session.trials),
                 match_views={tid: trace_match(m) for tid, m in session.matches.items()})
    except Exception as e:  # 오류도 화면에 표시
        run.emit("error", message=f"{type(e).__name__}: {e}")
    finally:
        run.done = True
        run.finished_at = time.time()
        run.emit("end")


def _trial_brief(trials: dict[str, dict]) -> dict[str, dict]:
    return {tid: {"title": t.get("title"), "overall_status": t.get("overall_status"),
                  "url": (t.get("source") or {}).get("url")} for tid, t in trials.items()}


def _replay(run: Run, src: Path, speed: float) -> None:
    """저장된 trace.jsonl을 원래 순서대로 다시 보냄. 단계 화면에 필요한 데이터가 없는 예전 기록은 result.json으로 채움."""
    try:
        result = json.loads((src / "result.json").read_text(encoding="utf-8"))
        records = [json.loads(line) for line in (src / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        report_path = src / "report.md"
        final = {tid: TrialMatch(**m) for tid, m in (result.get("matches") or {}).items()}
        prev_t = 0.0
        for rec in records:
            time.sleep(min(max(rec.get("t", prev_t) - prev_t, 0), REPLAY_MAX_GAP_S) / speed)
            prev_t = rec.get("t", prev_t)
            a, e = rec.get("agent"), rec.get("event")
            if a == "patient_profiler" and e == "done" and "profile" not in rec:
                rec["profile"] = result.get("profile")
            elif a == "screener" and e == "done" and "screening" not in rec:
                rec["screening"] = result.get("screening")
            elif a == "matcher" and e == "done" and "assessments" not in rec:
                m = final.get(rec.get("trial_id"))
                # 마지막 판정과 같은 결과일 때만 기준별 판정을 채움 (중간 라운드 기록은 저장되어 있지 않음)
                if m and (rec.get("message") or "").startswith(m.eligibility):
                    rec.update(trace_match(m))
            run.emit("trace", record=rec)
        all_trials = _load_trials()
        run.emit("result", result=result, report=report_path.read_text(encoding="utf-8") if report_path.exists() else None,
                 match_note=MATCH_NOTE, trials=_trial_brief({t: all_trials[t] for t in result.get("matches", {}) if t in all_trials}),
                 match_views={tid: trace_match(m) for tid, m in final.items()})
    except Exception as e:
        run.emit("error", message=f"다시 보기 실패: {type(e).__name__}: {e}")
    finally:
        run.done = True
        run.finished_at = time.time()
        run.emit("end")


def _saved_runs() -> dict[str, Path]:
    if not REPLAY_DIR.exists():
        return {}
    return {d.name: d for d in sorted(REPLAY_DIR.iterdir(), reverse=True)
            if d.is_dir() and (d / "trace.jsonl").exists() and (d / "result.json").exists()}


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


@app.get("/api/ko")
def trials_ko() -> dict:
    """화면 표시용 한국어 번역(시험 제목·질환·기준). 판정에는 쓰지 않고, 원문(en)과 같을 때만 화면이 바꿔 보여 줌."""
    f = config.DATA_DIR / "i18n" / "trials_ko.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


@app.post("/api/runs")
def start_run(req: RunRequest) -> dict:
    all_trials = _load_trials()
    trials = dict(all_trials) if req.screen else {t: all_trials[t] for t in req.trial_ids if t in all_trials}
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
    _register(run)
    run.emit("status", message="파이프라인 시작", patient_id=case.patient_id, patient_text=case.raw_text,
             trial_ids=list(trials))
    ctx = contextvars.copy_context()  # 실행별 토큰 사용량 집계
    threading.Thread(target=ctx.run, args=(_execute, run, req, case, trials), daemon=True).start()
    return {"run_id": run.run_id}


def _register(run: Run) -> None:
    _prune_runs()
    if len(RUNS) >= MAX_RUNS:
        raise HTTPException(429, f"진행 중인 실행이 {MAX_RUNS}개라 새 실행을 시작할 수 없습니다. 잠시 후 다시 시도하세요.")
    with RUNS_LOCK:
        RUNS[run.run_id] = run


@app.get("/api/replays")
def list_replays() -> list[dict]:
    out = []
    for rid, d in _saved_runs().items():
        try:
            r = json.loads((d / "result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"run_id": rid, "patient_id": r.get("patient_id"), "model": r.get("model"),
                    "n_trials": len(r.get("matches") or {}), "n_questions": len(r.get("qa_log") or []),
                    "screened": bool(r.get("screening"))})
    return out


@app.post("/api/replays/{saved_id}")
def start_replay(saved_id: str, speed: float = 1.0) -> dict:
    src = _saved_runs().get(saved_id)  # 목록에 있는 이름만 허용 (경로 조작 방지)
    if src is None:
        raise HTTPException(404, "저장된 실행을 찾을 수 없습니다.")
    try:
        result = json.loads((src / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(400, "저장된 결과 파일을 읽을 수 없습니다.")
    run = Run(run_id="replay-" + uuid.uuid4().hex[:8])
    _register(run)
    cases = _load_cases()
    pid = result.get("patient_id")
    text = cases[pid][0].raw_text if pid in cases else ""
    run.emit("status", message="다시 보기 시작", patient_id=pid, patient_text=text,
             trial_ids=list(result.get("matches") or {}), replay=saved_id)
    threading.Thread(target=_replay, args=(run, src, max(0.25, min(speed, 8.0))), daemon=True).start()
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
