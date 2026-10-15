"""웹 실행별 LLM 호출 기록 (Phase 5-3, 기록 전용).

호출을 막거나 바꾸지 않습니다. 요청 인자·응답·예외·SDK 재시도·대체 모델 동작은 기존 웹과 같고, 비용 상한도 없습니다
(비용 상한을 보장하지 않음). 실행마다 RecordingClient를 그 실행의 컨텍스트에 걸어 단계별 호출 수·토큰·캐시·응답 모델·
재시도(보일 때만)를 셉니다. 기록에는 숫자·코드·모델 이름만 넣고 환자 원문·API 키·LLM 응답은 넣지 않습니다.

계측 상태 (보수적으로 판정, 앞의 것이 우선):
- UNMETERED: 기록이 꺼져 있음, 또는 LLM이 쓰였는데(환자 정보 추출 완료) 기록기를 거친 호출이 0, 또는 기록기를 거친 호출 없이
  실행 동안 등록되지 않은 호출만 관측됨
- PARTIALLY_METERED: 기록기를 거친 호출이 있지만, 실행 동안 기록기를 거치지 않은 LLM 호출·SDK 요청이 프로세스 어딘가에서
  관측됨 (컨텍스트를 잃은 스레드 등. 어느 실행 것인지 알 수 없으므로 그동안 진행된 실행 모두에 표시)
- USAGE_UNKNOWN: 모든 호출이 기록기를 거쳤지만 사용량이 없는 응답, 또는 응답 없이 끝났지만 처리됐을 수 있는 요청
  (시간 초과·연결 오류·5xx)이 있음
- FULLY_METERED: 위에 해당하지 않음 (모든 호출이 기록기를 거쳤고 모든 응답에 사용량이 있음)
비용은 확인된 단가표가 없으므로 항상 UNKNOWN (cost_estimable=False). 대체 모델이 응답했으면 그 모델 단가도 모름.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections import Counter

from . import call_guard, config, llm

UNKNOWN = "UNKNOWN"
FULLY_METERED, PARTIALLY_METERED, UNMETERED, USAGE_UNKNOWN = (
    "FULLY_METERED", "PARTIALLY_METERED", "UNMETERED", "USAGE_UNKNOWN")
# 응답 없이 끝났지만 서버가 처리(과금)했을 수 있는 실패
_MAYBE_BILLED = {"APIConnectionError", "APITimeoutError", "InternalServerError", "OverloadedError",
                 "ServiceUnavailableError"}


MODES = ("off", "record", "enforce")


def mode() -> str:
    """CTREC_WEB_METERING: record(기본, 기록 전용) / off / enforce(호출 전 예산 예약·상한, web/budget.py).

    알 수 없는 값은 enforce로 봄 - 조용히 기록 전용으로 내려가지 않고 설정 검사에서 거절되게 함."""
    v = os.getenv("CTREC_WEB_METERING", "record").strip().lower()
    return v if v in ("off", "record") else "enforce"


_STAGES: list | None = None


def stage_of(system) -> str:
    global _STAGES
    if _STAGES is None:
        from . import orchestrator
        from .agents import explainer, matcher, patient_profiler, question_generator, recommender, screener
        _STAGES = [(m.SYSTEM[:60], name) for name, m in (
            ("orchestrator", orchestrator), ("screener", screener), ("patient_profiler", patient_profiler),
            ("matcher", matcher), ("question_generator", question_generator), ("recommender", recommender),
            ("explainer", explainer))]
    text = system[0]["text"] if isinstance(system, list) else str(system or "")
    return next((name for prefix, name in _STAGES if text.startswith(prefix)), "other")


class _Endpoint:
    def __init__(self, owner: "RecordingClient", beta: bool):
        self._owner, self._beta = owner, beta

    def create(self, **kw):
        return self._owner._call(self._beta, kw)


class RecordingClient:
    """llm.create가 쓰는 클라이언트 자리에 들어가 기존 기본 클라이언트(llm.client())로 그대로 보내고 결과만 기록."""

    def __init__(self, meter: "WebRunMeter"):
        self._meter = meter
        self.messages = _Endpoint(self, False)
        self.beta = type("Beta", (), {"messages": _Endpoint(self, True)})()

    def _call(self, beta: bool, kw: dict):
        stage = stage_of(kw.get("system"))
        t0 = time.monotonic()
        with call_guard.mark("recording") as mk:
            try:
                inner = llm.client()  # 기존과 같은 지점에서 같은 클라이언트·같은 예외 (API 키 없음 등)
                resp = (inner.beta.messages if beta else inner.messages).create(**kw)
            except BaseException as e:
                self._meter.failed(stage, e, mk, time.monotonic() - t0)
                raise
        self._meter.succeeded(stage, kw, resp, mk, time.monotonic() - t0, inner)
        return resp


class WebRunMeter:
    """웹 실행 1개의 기록. 스레드 안전 (Matcher 병렬 스레드가 함께 씀)."""

    def __init__(self, run_id: str, clock=time.monotonic):
        self.run_id, self.clock = run_id, clock
        self.mode = mode()
        self._lock = threading.Lock()
        self.by_stage: dict[str, Counter] = {}
        self.models: Counter = Counter()
        self.errors: Counter = Counter()
        self.usage_missing = self.maybe_billed_failures = self.fallbacks_requested = 0
        self.retries = 0
        self.retries_visible = True  # anthropic SDK로 보낸 요청만 시도 수가 보임
        self.started = clock()
        self.ended: float | None = None
        self._unattributed0 = 0
        self._unattributed1: int | None = None

    @contextlib.contextmanager
    def recording(self):
        """이 실행(과 copy_context로 넘긴 하위 스레드)의 LLM 호출을 기록기로. off면 아무것도 하지 않음."""
        if self.mode != "record":  # off: 기록 안 함, enforce: web/budget.run_context가 대신 기록
            yield self
            return
        proxy = RecordingClient(self)
        self._unattributed0 = call_guard.counters()["unattributed"]
        with call_guard.register(proxy, "recording", observe=True), llm.use_client(proxy):
            try:
                yield self
            finally:
                self._unattributed1 = call_guard.counters()["unattributed"]

    def _stage(self, stage: str) -> Counter:
        return self.by_stage.setdefault(stage, Counter())

    def _attempts(self, mk, inner) -> None:
        if type(inner).__module__.split(".")[0] != "anthropic":
            self.retries_visible = False
        elif mk.attempts > 1:
            self.retries += mk.attempts - 1

    def succeeded(self, stage, kw, resp, mk, latency, inner) -> None:
        usage = getattr(resp, "usage", None)
        g = lambda k: int(getattr(usage, k, 0) or 0)  # noqa: E731
        with self._lock:
            s = self._stage(stage)
            s.update(calls=1)
            if "fallbacks" in kw:
                self.fallbacks_requested += 1
            if usage is None or getattr(usage, "input_tokens", None) is None \
                    or getattr(usage, "output_tokens", None) is None:
                self.usage_missing += 1
                s.update(usage_missing=1)
            else:
                s.update(input_tokens=g("input_tokens"), output_tokens=g("output_tokens"),
                         cache_write=g("cache_creation_input_tokens"), cache_read=g("cache_read_input_tokens"),
                         thinking_tokens=int(getattr(getattr(usage, "output_tokens_details", None),
                                                     "thinking_tokens", 0) or 0))
            self.models[str(getattr(resp, "model", None) or UNKNOWN)] += 1
            s[f"stop_{getattr(resp, 'stop_reason', None)}"] += 1
            s["latency_ms"] += int(latency * 1000)
            self._attempts(mk, inner)

    def failed(self, stage, e, mk, latency) -> None:
        name = type(e).__name__
        with self._lock:
            s = self._stage(stage)
            s.update(failed=1)
            s["latency_ms"] += int(latency * 1000)
            self.errors[name] += 1  # 종류만 (메시지에 요청 내용이 있을 수 있음)
            status = getattr(e, "status_code", None)
            if name in _MAYBE_BILLED or (isinstance(status, int) and status >= 500):
                self.maybe_billed_failures += 1
            if mk.attempts > 1:
                self.retries += mk.attempts - 1

    def summary(self, llm_expected: bool) -> dict:
        """llm_expected: 이 실행에서 LLM이 반드시 쓰였는가 (예: 환자 정보 추출이 끝남)."""
        with self._lock:
            calls = sum(s.get("calls", 0) + s.get("failed", 0) for s in self.by_stage.values())
            end = self._unattributed1 if self._unattributed1 is not None else call_guard.counters()["unattributed"]
            unattributed = end - self._unattributed0
            if self.mode == "off":
                status, reason = UNMETERED, "metering_off"
            elif unattributed > 0:
                status, reason = (PARTIALLY_METERED, "calls_outside_recorder") if calls else \
                    (UNMETERED, "calls_outside_recorder")
            elif calls == 0 and llm_expected:
                status, reason = UNMETERED, "llm_used_but_no_recorded_calls"
            elif self.usage_missing or self.maybe_billed_failures:
                status, reason = USAGE_UNKNOWN, ("usage_missing" if self.usage_missing else "response_lost")
            else:
                status, reason = FULLY_METERED, None
            tot = Counter()
            for s in self.by_stage.values():
                tot.update({k: v for k, v in s.items() if k in ("calls", "failed", "input_tokens", "output_tokens",
                                                                "cache_write", "cache_read", "thinking_tokens")})
            usage_confirmed = status == FULLY_METERED
            return {
                "run_id": self.run_id, "mode": {"record": "record_only", "enforce": "enforce"}.get(self.mode, "off"),
                "status": status, "status_reason": reason,
                "calls": calls, "by_stage": {k: dict(v) for k, v in self.by_stage.items()},
                "input_tokens": tot["input_tokens"], "output_tokens": tot["output_tokens"],
                "cache_write_tokens": tot["cache_write"], "cache_read_tokens": tot["cache_read"],
                "thinking_tokens": tot["thinking_tokens"],  # 출력 토큰에 포함된 사고 토큰 (응답이 알려 준 만큼)
                "retries": self.retries if self.retries_visible else UNKNOWN,
                "response_models": dict(self.models), "requested_model": config.MODEL,
                "fallbacks_requested": self.fallbacks_requested, "errors": dict(self.errors),
                "usage_missing": self.usage_missing, "maybe_billed_failures": self.maybe_billed_failures,
                "unattributed_calls_during_run": unattributed,
                "elapsed_s": round((self.ended or self.clock()) - self.started, 3),
                "usage_confirmed": usage_confirmed,
                # 확인된 단가표가 없음 (모델·대체 모델 단가 미확인) -> 비용은 알 수 없음
                "cost_estimable": False, "cost_usd": UNKNOWN,
                # 기록 전용: 호출을 막지 않으므로 비용 상한 없음. enforce: 예약액 기준 상한 (외부 청구 절대 상한은 아님)
                "cost_cap_enforced": self.mode == "enforce",
                "note": ("호출 전 예산을 예약해 한도를 넘는 요청을 막습니다. 외부 청구액의 절대 상한은 아닙니다."
                         if self.mode == "enforce" else
                         "기록 전용 계측입니다. 호출을 제한하지 않으며 비용 상한을 보장하지 않습니다."),
            }

    def finish(self) -> None:
        self.ended = self.clock()
