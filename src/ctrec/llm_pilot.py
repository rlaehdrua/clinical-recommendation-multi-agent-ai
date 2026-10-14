"""실제 LLM 소규모 평가 준비 (Phase 5-0). 실행 경로는 꺼져 있습니다 (PAID_EXECUTION_ENABLED = False).

두 실험을 분리합니다.
- 실험 A (파이프라인 안정성): 실제 프로파일러·Matcher의 사실 추출·판정·출처 유지·실패/재시도·지연·토큰, 실제 응답의
  eGFR/EGFR 구분. 질문 정책의 우열은 판단하지 않음 (질문은 Decision Loop 계획, 답변은 결정적 정답 시뮬레이터).
- 실험 B (질문 계획 비교): 같은 초기 프로필·같은 Matcher 판정(라운드 1 직전 상태) 하나에서 기존 LLM 질문 생성기와
  Decision Loop ASK_USER 계획을 각각 만들고 비교. 답변을 반영하지 않으므로 한 방식의 답변으로 상태가 달라진 뒤의
  결과를 다른 방식과 비교하는 일이 없음.

안전장치: 호출 수·입력/출력 토큰·비용 상한(호출 전 최악값 예약), 재시도 상한, 연속 실패·취소·시간 제한 중단,
환자 데이터 비저장, 출력 비식별화 검사, 명시 승인 + 계획 해시 확인 문구. 이번 단계에서는 모든 조건을 갖춰도
run_pilot이 실행하지 않으며, 환경 변수·플래그만으로 켜는 경로가 없습니다 (켜려면 승인 후 코드 변경이 필요).
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import acquisition, config, llm, question_eval as qe
from .agents import matcher, patient_profiler, question_generator, recommender
from .decision_state import Budget, SessionView
from .fact_view import non_biomarker_mentions, targets_in

UNKNOWN = "UNKNOWN"

# 실행 스위치. 이번 단계에서는 코드 상수로 꺼 둠 (환경 변수·명령행 플래그로 바꿀 수 없음)
PAID_EXECUTION_ENABLED = False

# ---------------------------------------------------------------------------
# 실제 LLM 호출 경로 (코드 기준). 모든 호출은 llm.create를 거침 (structured/plain도 create 호출)
# ---------------------------------------------------------------------------

CALL_PATHS = [
    {"stage": "patient_profiler", "site": "agents/patient_profiler.py:profile_patient", "api": "llm.structured",
     "effort": config.EFFORT["patient_profiler"], "max_tokens": 16000,
     "when": "처음 1회 + 확인 질문 라운드마다 전체 원문(답변 포함)으로 재추출", "experiments": ["A", "B"]},
    {"stage": "matcher", "site": "agents/matcher.py:_llm_assess (match_trial)", "api": "llm.structured",
     "effort": config.EFFORT["matcher"], "max_tokens": 16000,
     "when": "시험마다, 규칙 엔진·검토 표시로 정해지지 않은 규칙이 있을 때 1회 + 응답에서 빠진 규칙만 재요청 "
             "(CTREC_MATCHER_RETRIES, 기본 1). 라운드마다 질문 대상 시험 전체를 다시 판정", "experiments": ["A", "B"]},
    {"stage": "question_generator", "site": "agents/question_generator.py:generate_questions", "api": "llm.structured",
     "effort": config.EFFORT["question_generator"], "max_tokens": 16000,
     "when": "확인 질문 라운드마다 1회 (Session.clarify)", "experiments": ["B"]},
    {"stage": "simulated_patient", "site": "agents/answerers.py (LLM 가상 환자 2종)", "api": "llm.structured",
     "effort": config.EFFORT["simulated_patient"], "max_tokens": 4000,
     "when": "LLM 가상 환자를 쓸 때 라운드마다 1회. 이번 실험은 결정적 정답 시뮬레이터(OracleAnswerer)라 호출 없음",
     "experiments": []},
    {"stage": "recommender", "site": "agents/recommender.py:recommend", "api": "llm.structured",
     "effort": config.EFFORT["recommender"], "max_tokens": 16000,
     "when": "마지막 1회. 추천 가능한 모집 중 시험이 없으면 LLM 없이 코드가 작성", "experiments": ["A"]},
    {"stage": "explainer", "site": "agents/explainer.py:explain", "api": "llm.plain",
     "effort": config.EFFORT["explainer"], "max_tokens": 16000,
     "when": "보고서 작성 시 1회. CTREC_WRITE_REPORT=0이면 없음 (두 실험 모두 끔)", "experiments": []},
    {"stage": "screener", "site": "agents/screener.py:screen", "api": "llm.structured",
     "effort": config.EFFORT["screener"], "max_tokens": 16000,
     "when": "screen_trials=True일 때만 (두 실험은 후보 시험이 정해져 있어 끔)", "experiments": []},
    {"stage": "orchestrator", "site": "orchestrator.py (에이전트 모드)", "api": "llm.create (tools)",
     "effort": config.EFFORT["orchestrator"], "max_tokens": 16000,
     "when": "CLI 에이전트 모드에서만. 두 실험은 고정 순서(run_fixed)", "experiments": []},
    {"stage": "criteria_parser/parser_validator", "site": "agents/criteria_parser.py, agents/parser_validator.py",
     "api": "llm.structured", "effort": "high", "max_tokens": 16000,
     "when": "기준표 사전 컴파일 또는 CTREC_LAZY_COMPILE=1일 때만. 실험은 고정 기준표(Frozen Store)를 읽기만 함",
     "experiments": []},
    {"stage": "fact_compare(llm)", "site": "fact_compare.py (fact_compare='llm')", "api": "Matcher 재실행",
     "effort": config.EFFORT["matcher"], "max_tokens": 16000, "when": "명시 선택 시에만 (기본 off, 두 실험 off)",
     "experiments": []},
]

CONTROLS = {
    "sdk_retries": "llm.client(): anthropic.Anthropic(max_retries=4) - SDK가 429·5xx·연결 오류를 자동 재시도 (호출 수에 안 보임). "
                   "평가에서는 MeteredClient가 SDK 재시도 0인 클라이언트를 감싸 재시도를 직접 세고 상한을 둠",
    "matcher_missing_retry": "config.MATCHER_MISSING_RETRIES(기본 1): 응답에서 빠진 규칙만 다시 요청 (유료 호출 1회 추가)",
    "truncation": "stop_reason=max_tokens -> TruncatedError (재시도 없음, 해당 시험 NOT_EVALUATED)",
    "refusal": "CTREC_FALLBACKS=1이면 서버측 대체 모델로 재실행 (대체 모델·단가 UNKNOWN) -> 평가에서는 0을 요구",
    "cache": "시스템 프롬프트에 cache_control(ephemeral). 최소 캐시 길이·적중 여부는 모델별로 다름 (UNKNOWN). 응답 캐시는 없음",
    "concurrency": "Session._parallel: ThreadPoolExecutor(CTREC_MAX_WORKERS=4)로 시험별 Matcher 동시 실행. 사용량은 contextvars로 환자별 집계",
    "cancel_timeout": "llm.guard(): 호출 직전 취소·시간 초과 확인 (웹 실행·평가가 설정). 이미 보낸 호출은 중단하지 못함. "
                      "SDK 요청별 timeout은 기본값 (평가에서는 MeteredClient가 timeout을 넘김)",
}

# 시스템 프롬프트 앞부분으로 호출 단계를 구분 (계측용, 원문은 저장하지 않음)
_STAGE_BY_SYSTEM = [(m.SYSTEM[:60], name) for name, m in (
    ("patient_profiler", patient_profiler), ("matcher", matcher), ("question_generator", question_generator),
    ("recommender", recommender))]


def stage_of(system) -> str:
    text = system[0]["text"] if isinstance(system, list) else str(system or "")
    return next((name for prefix, name in _STAGE_BY_SYSTEM if text.startswith(prefix)), "other")


# ---------------------------------------------------------------------------
# 실험 정의
# ---------------------------------------------------------------------------

EXPERIMENTS = {
    "A_pipeline": {
        "purpose": "실제 프로파일러 사실 추출, 실제 Matcher 판정, 사실 출처 유지, 호출 실패·재시도, 지연·토큰, eGFR/EGFR 구분",
        "not_measured": "질문 생성 정책의 우열 (질문은 Decision Loop 계획을 그대로 사용)",
        "flow": "profile -> match(라운드 0) -> 확인 질문 1라운드(Decision Loop 계획, 결정적 정답 시뮬레이터) "
                "-> 재추출 -> 재판정 -> 추천 (보고서 없음)",
        "rounds": 1, "question_budget": 3, "llm_stages": ["patient_profiler", "matcher", "recommender"],
        "success_criteria": [
            "모든 사례가 status=ok (NOT_EVALUATED·TruncatedError·RefusalError 0)",
            "Matcher 판정 누락 재요청 후 남은 누락 0, 지어낸 rule_id 0 (화이트리스트)",
            "답변에서 온 사실은 모두 self_report, 문서 검증 승격 0 (평가용 상태 구성 항목 제외)",
            "인용 위치를 원문에서 찾지 못한 사실은 모두 source_type=unknown (출처 추정 0)",
            "신장 eGFR 문장이 EGFR 표적으로 연결된 사실 0, 바이오마커 항목에 섞인 신장 수치 건수 보고",
            "실제 호출 수·토큰·비용이 상한 이내, 재시도 상한 도달 0",
            "고정 기준표 무결성 유지, 결과 파일에 원문·숨긴 정답 없음",
        ],
    },
    "B_question_plan": {
        "purpose": "같은 환자 상태·같은 Matcher 판정에서 기존 LLM 질문 생성기와 Decision Loop ASK_USER 계획 비교",
        "flow": "profile -> match(라운드 0) -> 상태 지문 고정 -> (1) Decision Loop 계획 (2) LLM 질문 생성 -> 지문 재확인 "
                "-> 답변 반영 없이 평가기가 정답과 대조",
        "rounds": 0, "question_budget": 3, "llm_stages": ["patient_profiler", "matcher", "question_generator"],
        "comparison_rule": "두 계획은 같은 상태 지문(프로필·판정·사실 버전)에서 만들어야 하며, 지문이 다르면 비교에서 제외",
        "success_criteria": [
            "모든 사례에서 두 계획의 상태 지문 동일 (state_diverged 0)",
            "질문 대상 매핑: LLM 질문의 target이 실제 기준 ID로 해석되는 비율 보고 (UNMAPPED 포함)",
            "중복 질문(같은 기준·같은 정보 키) 수, 불필요 질문(이미 충분·확정 불충족만·후보 아님) 수",
            "예산(3) 안의 정보 가치: 숨긴 기준을 맞힌 질문(EXACT/INFERRED)이 예산 안에 있는지, 첫 적중 순위",
            "생성기 입력에 숨긴 정답 없음",
        ],
        "cost_saving_option": "실험 A와 같은 실행에서 라운드 1 직전 상태를 재사용하면 질문 생성 호출만 추가 (프로필·판정 공유)",
    },
}

# ---------------------------------------------------------------------------
# 단가·상한
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceTable:
    """백만 토큰당 USD. confirmed=True는 사용자가 공식 가격표로 확인했다는 뜻 (코드가 확인하지 않음)."""
    model: str
    input: float
    output: float
    cache_write: float
    cache_read: float
    confirmed: bool = False
    source: str = "assumed"

    def cost(self, inp: float, out: float, cache_write: float = 0, cache_read: float = 0) -> float:
        return (inp * self.input + out * self.output + cache_write * self.cache_write + cache_read * self.cache_read) / 1e6


# 가정값 (확인 전). 캐시 쓰기 1.25배, 읽기 0.1배도 가정
ASSUMED_PRICE = PriceTable(model=config.MODEL, input=3.0, output=15.0, cache_write=3.75, cache_read=0.30)


@dataclass(frozen=True)
class PilotLimits:
    max_cost_usd: float
    max_calls: int  # 재시도를 포함한 실제 API 요청 수
    max_input_tokens: int
    max_output_tokens: int
    max_retries_per_call: int = 2  # 일시 오류(429·5xx·연결·시간 초과)만
    max_total_retries: int = 5
    max_consecutive_failures: int = 3
    call_timeout_s: float = 180.0
    run_deadline_s: float = 1800.0
    max_tokens_per_call: int = 16000  # 출력 상한 예약에 쓰는 값 (에이전트 호출의 max_tokens)
    chars_per_token: float = 1.0  # 호출 전 입력 토큰 예약용 보수적 환산 (가정, 한글은 1자=1토큰 이상일 수도 있음)


class PilotStop(llm.RunCancelled):
    """상한·중단 조건으로 더 이상 API를 호출하지 않음 (args[0]: 이유 코드). 기존 취소 처리 경로를 그대로 탐."""


_RETRYABLE_NAMES = {"RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError",
                    "OverloadedError", "ServiceUnavailableError"}


def is_retryable(e: Exception) -> bool:
    status = getattr(e, "status_code", None)
    return type(e).__name__ in _RETRYABLE_NAMES or (isinstance(status, int) and (status == 429 or status >= 500))


class PilotMeter:
    """호출 전 최악값(입력 추정 + 출력 max_tokens)을 예약해 동시 실행에서도 상한을 넘지 않게 하고, 호출 뒤 실제 사용량으로 정산."""

    def __init__(self, limits: PilotLimits, price: PriceTable, cancel: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.limits, self.price, self.cancel, self.clock = limits, price, cancel, clock
        self.start = clock()
        self._lock = threading.Lock()
        self.calls = self.retries = self.consecutive_failures = 0
        self.input_tokens = self.output_tokens = self.cache_write_tokens = self.cache_read_tokens = 0
        self.cost = 0.0
        self._reserved = {"cost": 0.0, "input": 0, "output": 0}
        self.stopped: str | None = None
        self.failures: Counter = Counter()
        self.by_stage: dict[str, Counter] = {}
        self.latency: dict[str, list[float]] = {}

    def _check_locked(self) -> None:
        if self.stopped:
            raise PilotStop(self.stopped)
        if self.cancel and self.cancel():
            self.stopped = "cancelled"
        elif self.clock() - self.start > self.limits.run_deadline_s:
            self.stopped = "timeout"
        if self.stopped:
            raise PilotStop(self.stopped)

    def reserve(self, est_input: int, max_output: int, retry: bool = False) -> dict:
        lim = self.limits
        with self._lock:
            self._check_locked()
            r = self._reserved
            if retry and self.retries >= lim.max_total_retries:
                self.stopped = "retry_cap"
            elif self.calls + 1 > lim.max_calls:
                self.stopped = "call_cap"
            elif self.input_tokens + r["input"] + est_input > lim.max_input_tokens:
                self.stopped = "input_token_cap"
            elif self.output_tokens + r["output"] + max_output > lim.max_output_tokens:
                self.stopped = "output_token_cap"
            elif self.cost + r["cost"] + self.price.cost(est_input, max_output) > lim.max_cost_usd:
                self.stopped = "cost_cap"
            if self.stopped:
                raise PilotStop(self.stopped)
            tok = {"input": est_input, "output": max_output, "cost": self.price.cost(est_input, max_output)}
            for k, v in tok.items():
                r[k] += v
            self.calls += 1
            self.retries += int(retry)
            return tok

    def _release(self, tok: dict) -> None:
        for k, v in tok.items():
            self._reserved[k] -= v

    def settle(self, tok: dict, usage, stage: str, latency: float) -> None:
        g = lambda k: int(getattr(usage, k, 0) or 0)  # noqa: E731
        with self._lock:
            self._release(tok)
            inp, out, cw, cr = g("input_tokens"), g("output_tokens"), g("cache_creation_input_tokens"), g("cache_read_input_tokens")
            self.input_tokens += inp + cw + cr
            self.output_tokens += out
            self.cache_write_tokens += cw
            self.cache_read_tokens += cr
            self.cost += self.price.cost(inp, out, cw, cr)
            self.consecutive_failures = 0
            s = self.by_stage.setdefault(stage, Counter())
            s.update(calls=1, input_tokens=inp, output_tokens=out, cache_write=cw, cache_read=cr)
            self.latency.setdefault(stage, []).append(round(latency, 3))
            # 실제 사용량이 상한을 넘었으면(입력 추정이 낮았던 경우) 다음 호출부터 중단
            if self.cost > self.limits.max_cost_usd:
                self.stopped = "cost_cap"
            elif self.input_tokens > self.limits.max_input_tokens:
                self.stopped = "input_token_cap"

    def fail(self, tok: dict, stage: str, error: Exception, latency: float) -> None:
        with self._lock:
            self._release(tok)
            self.failures[type(error).__name__] += 1  # 종류만 (메시지에 요청 내용이 있을 수 있음)
            self.consecutive_failures += 1
            self.by_stage.setdefault(stage, Counter()).update(failed=1)
            self.latency.setdefault(stage, []).append(round(latency, 3))
            if self.consecutive_failures >= self.limits.max_consecutive_failures:
                self.stopped = "consecutive_failures"

    def report(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "retries": self.retries, "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens, "cache_write_tokens": self.cache_write_tokens,
                    "cache_read_tokens": self.cache_read_tokens, "cost_usd": round(self.cost, 4),
                    "price_confirmed": self.price.confirmed, "stopped": self.stopped, "failures": dict(self.failures),
                    "by_stage": {k: dict(v) for k, v in self.by_stage.items()},
                    "latency_s": {k: {"n": len(v), "max": max(v), "mean": round(sum(v) / len(v), 3)}
                                  for k, v in self.latency.items() if v},
                    "limits": asdict(self.limits)}


def _request_chars(kw: dict) -> int:
    return len(json.dumps({"s": kw.get("system"), "m": kw.get("messages"), "o": kw.get("output_config"),
                           "t": kw.get("tools")}, ensure_ascii=False))


class _Endpoint:
    def __init__(self, owner: "MeteredClient", inner_create):
        self._owner, self._inner = owner, inner_create

    def create(self, **kw):
        return self._owner._call(self._inner, kw)


class MeteredClient:
    """llm.client() 자리에 들어가는 대리 객체. 모든 API 요청(재시도 포함)이 예약·정산을 거침.

    inner는 SDK 자동 재시도를 끈 클라이언트(max_retries=0)여야 함 - 그래야 모든 요청이 여기서 세어짐."""

    def __init__(self, inner, meter: PilotMeter, sleep: Callable[[float], None] = time.sleep):
        self._inner, self._meter, self._sleep = inner, meter, sleep
        self.messages = _Endpoint(self, inner.messages.create)
        beta = getattr(inner, "beta", None)
        self.beta = type("Beta", (), {"messages": _Endpoint(self, beta.messages.create)})() if beta else None

    def _call(self, inner_create, kw):
        m, lim = self._meter, self._meter.limits
        stage = stage_of(kw.get("system"))
        est_in = math.ceil(_request_chars(kw) / lim.chars_per_token)
        max_out = min(int(kw.get("max_tokens") or lim.max_tokens_per_call), lim.max_tokens_per_call)
        kw = {**kw, "max_tokens": max_out, "timeout": lim.call_timeout_s}
        for attempt in range(1 + lim.max_retries_per_call):
            tok = m.reserve(est_in, max_out, retry=attempt > 0)
            t0 = time.monotonic()
            try:
                resp = inner_create(**kw)
            except Exception as e:  # noqa: BLE001 - 종류만 기록, 일시 오류만 재시도
                m.fail(tok, stage, e, time.monotonic() - t0)
                if not is_retryable(e) or attempt == lim.max_retries_per_call:
                    raise
                self._sleep(min(2 ** attempt, 8))
                continue
            m.settle(tok, getattr(resp, "usage", None), stage, time.monotonic() - t0)
            return resp
        raise PilotStop("retry_exhausted")  # 도달하지 않음 (마지막 시도는 위에서 raise)


@contextlib.contextmanager
def metered(meter: PilotMeter, inner):
    """평가 동안만 llm.client를 계측 대리 객체로 바꿈. 끝나면 원래 함수로 복구."""
    proxy = MeteredClient(inner, meter)
    saved = llm.client
    llm.client = lambda: proxy
    try:
        yield proxy
    finally:
        llm.client = saved


# ---------------------------------------------------------------------------
# 승인 게이트 (이번 단계: 항상 거절)
# ---------------------------------------------------------------------------


class PaidRunNotApproved(qe.PaidRunNotApproved):
    pass


@dataclass(frozen=True)
class PilotApproval:
    approved_by: str  # 사용자 명시 승인 기록 (예: 승인 메시지 날짜)
    experiment: str
    plan_hash: str
    price: PriceTable
    limits: PilotLimits
    confirmation: str  # 실행 직전 최종 확인 문구: f"RUN {experiment} {plan_hash[:12]}"


def plan_hash(plan: dict) -> str:
    keys = ("experiment", "case_ids", "model", "rounds", "question_budget", "limits", "save_patient_data")
    return hashlib.sha256(json.dumps({k: plan.get(k) for k in keys}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def approval_problems(plan: dict, approval: PilotApproval | None) -> list[str]:
    p = []
    if approval is None:
        return ["명시적 사용자 승인 없음"]
    if not approval.approved_by.strip():
        p.append("승인자 기록 없음")
    if approval.experiment != plan.get("experiment"):
        p.append("승인한 실험과 계획이 다름")
    if approval.plan_hash != plan_hash(plan):
        p.append("계획이 승인 뒤 바뀜 (계획 해시 불일치)")
    if approval.confirmation != f"RUN {plan.get('experiment')} {plan_hash(plan)[:12]}":
        p.append("실행 직전 최종 확인 문구 불일치")
    if not approval.price.confirmed:
        p.append("모델 단가 미확인")
    if approval.price.model != config.MODEL or plan.get("model") != config.MODEL:
        p.append("실제 사용 모델과 승인 모델이 다름")
    lim, planned = approval.limits, plan.get("limits") or {}
    for k in ("max_cost_usd", "max_calls", "max_input_tokens", "max_output_tokens", "max_retries_per_call"):
        if k not in planned or getattr(lim, k) > planned[k]:
            p.append(f"상한 {k}가 계획보다 크거나 없음")
    if config.USE_FALLBACKS:
        p.append("CTREC_FALLBACKS=1: 대체 모델 재실행의 모델·단가를 알 수 없음 (0 필요)")
    if plan.get("save_patient_data"):
        p.append("환자 데이터 저장이 켜져 있음")
    if not plan.get("redact_outputs", False):
        p.append("출력 비식별화가 꺼져 있음")
    if os.getenv("CTREC_ALLOW_PAID_EVAL") != "1":
        p.append("CTREC_ALLOW_PAID_EVAL=1 없음")
    return p


def authorize(plan: dict, approval: PilotApproval | None) -> None:
    problems = approval_problems(plan, approval)
    if problems:
        raise PaidRunNotApproved("; ".join(problems))
    if not PAID_EXECUTION_ENABLED:
        raise PaidRunNotApproved("Phase 5-0: 유료 실행 경로가 꺼져 있음 (승인 후 코드 변경 필요)")


def run_pilot(plan: dict, approval: PilotApproval | None, make_client: Callable | None = None) -> dict:
    """실제 유료 실행 입구. 이번 단계에서는 authorize가 항상 거절하므로 클라이언트를 만들지 않음."""
    authorize(plan, approval)
    raise PaidRunNotApproved("Phase 5-0: 실행 경로 없음")  # pragma: no cover - PAID_EXECUTION_ENABLED가 False


# ---------------------------------------------------------------------------
# 실험 B: 같은 상태에서 두 질문 계획 비교
# ---------------------------------------------------------------------------


class _NoAnswer:
    def answer(self, questions, patient_id):  # 실험 B는 답변을 반영하지 않음
        raise RuntimeError("실험 B는 답변 단계를 실행하지 않습니다")


def state_fingerprint(s) -> str:
    """프로필·판정·사실 버전 지문 (ID 정규화 없이 같은 Session 안에서만 비교)."""
    view = SessionView.from_session(s)
    blob = json.dumps({"profile": s.profile.model_dump(mode="json") if s.profile else None,
                       "matches": {t: m.model_dump(mode="json") for t, m in sorted(s.matches.items())},
                       "fact_version": view.snapshot.version}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def plan_metrics(records: list[dict], budget: int) -> dict:
    keys = [k for r in records for k in r["target_criterion_ids"]]
    sems = [tuple(r["normalized_target"]) for r in records]
    rel = Counter()
    for r in records:
        rel.update(r["relevance"])
    status = Counter(r["mapping_status"] for r in records)
    first = next((i + 1 for i, r in enumerate(records) if r["mapping_status"] in ("EXACT_HIT", "INFERRED_HIT")), None)
    return {"n_questions": len(records), "within_budget": len(records) <= budget, "mapping": dict(status),
            "unmapped_questions": status.get("UNMAPPED", 0) + sum(not r["target_criterion_ids"] for r in records),
            "duplicate_criteria": len(keys) - len(set(keys)), "duplicate_info_keys": len(sems) - len(set(sems)),
            "unnecessary_targets": rel.get("already_sufficient", 0) + rel.get("confirmed_mismatch_only", 0)
            + rel.get("not_candidate", 0), "relevance": dict(rel),
            "hit_within_budget": first is not None and first <= budget, "first_hit_rank": first}


def compare_plans(case, gt, trials: dict, budget: int = 3, llm_generate: Callable | None = None,
                  spy: Callable | None = None) -> dict:
    """같은 Session 상태 하나에서 두 질문 계획을 만들어 비교. 정답(gt)은 두 계획을 만든 뒤 평가에만 사용."""
    from .decision_loop import DecisionTracker, LoopConfig
    from .pipeline import PatientCase, Session

    c = PatientCase(patient_id=case.patient_id, raw_text=case.raw_text, input_source=case.input_source,
                    candidate_trials=list(case.candidate_trials or []))
    s = Session(case=c, trials={t: trials[t] for t in gt.candidate_trials}, answerer=_NoAnswer(),
                tracer=qe._NullTracer(), max_rounds=1, question_budget=None, decision_loop="diagnostic")
    s._decision_tracker = DecisionTracker(LoopConfig(budget=Budget(max_questions=budget)))
    out: dict = {"case_id": gt.case_id, "case_type": gt.case_type, "status": "ok"}
    try:
        s.profile_patient()
        for f in s.fact_store.facts(c.patient_id):  # 평가용 상태 구성 (확정 불충족 사례), run_method와 같음
            if f.field in gt.setup_verified_fields and f.verification_status != "document_verified":
                s.fact_store.set_verification(f.fact_id, "document_verified", reason="합성 평가 상태",
                                              by="question_eval", document_reference="SYNTHETIC")
        s.match()
        fp0 = state_fingerprint(s)
        view = SessionView.from_session(s)
        targets = s.open_candidates()
        acq = {k: v for k, v in acquisition.plan(s.missing_facts).items()
               if k.split(":")[0] in {m.trial_id for m in targets}}
        loop_qs = qe.candidate_generator({"session": s})(s.profile, targets, [], budget, acq)
        fp1 = state_fingerprint(s)
        args = (s.profile.model_copy(deep=True), [m.model_copy(deep=True) for m in targets], [], budget,
                copy.deepcopy(acq))
        if spy:
            spy(*args)
        llm_qs = (llm_generate or question_generator.generate_questions)(*args[:4], acquisition_plan=args[4])
        llm_qs.questions = llm_qs.questions[:budget]
        fp2 = state_fingerprint(s)
        out["same_state"] = fp0 == fp1 == fp2
        if not out["same_state"]:
            out["status"] = "state_diverged"
        out["would_clarify"] = s.needs_clarification()
        # 여기서부터 평가기 (정답 사용)
        plans = {"decision_loop": loop_qs.questions, "llm_generator": llm_qs.questions}
        out["plans"] = {}
        for name, qs in plans.items():
            recs = [qe._record(gt, name, 1, i, q, view, []) for i, q in enumerate(qs)]
            out["plans"][name] = {"metrics": plan_metrics(recs, budget),
                                  "targets": [r["target_criterion_ids"] for r in recs],
                                  "normalized_targets": [r["normalized_target"] for r in recs]}
        a, b = (set(map(tuple, out["plans"][n]["normalized_targets"])) for n in plans)
        out["overlap_info_keys"] = len(a & b)
    except llm.RunCancelled as e:
        out.update(status=e.args[0] if e.args else "cancelled", error_type=type(e).__name__)
    except Exception as e:  # noqa: BLE001 - 종류만 (메시지에 원문이 있을 수 있음)
        out.update(status="error", error_type=type(e).__name__)
    return out


# ---------------------------------------------------------------------------
# 실험 A: 비식별 지표
# ---------------------------------------------------------------------------


def pipeline_metrics(run: qe.MethodRun, s, gt) -> dict:
    """실험 A의 결과 요약. 값·원문·인용은 넣지 않고 항목 이름·건수·코드만."""
    facts = s.fact_store.facts(s.case.patient_id)
    by_src, by_ver, by_kind = Counter(), Counter(), Counter()
    egfr = Counter()
    for f in facts:
        by_src[f.source_type] += 1
        by_ver[f.verification_status] += 1
        by_kind[f.source_reference.kind] += 1
        text = f"{f.value} {f.source_reference.quote or ''}"
        labs, tgt = non_biomarker_mentions(text), targets_in(text)
        if "lab:egfr" in labs:
            egfr["kidney_egfr_facts"] += 1
            egfr["kidney_egfr_in_biomarker_field"] += f.field == "biomarker"
            egfr["kidney_egfr_linked_to_EGFR"] += "EGFR" in tgt and not _two_terms(text)
        elif "EGFR" in tgt:
            egfr["egfr_gene_facts"] += 1
    answer_facts = [f for f in facts if f.source_reference.kind == "answer"]
    matches = s.matches.values()
    return {
        "case_id": gt.case_id, "status": run.status, "error_type": run.error_type,
        "facts": {"total": len(facts), "source_type": dict(by_src), "verification": dict(by_ver),
                  "reference_kind": dict(by_kind), "reference_unlocated": by_kind.get("other", 0),
                  # 인용 위치를 원문에서 찾지 못한 사실은 출처 unknown이어야 함 (출처를 추정하지 않음)
                  "unlocated_not_unknown": sum(f.source_reference.kind == "other" and f.source_type != "unknown"
                                               for f in facts),
                  "answer_facts": len(answer_facts),
                  "answer_facts_not_self_report": sum(f.source_type != "self_report" for f in answer_facts),
                  "promoted_to_document_verified": sum(f.verification_status == "document_verified"
                                                       and f.field not in gt.setup_verified_fields for f in facts)},
        "egfr": dict(egfr),
        "matcher": {"eligibility": dict(Counter(m.eligibility for m in matches)),
                    "methods": dict(Counter(a.method for m in matches for a in m.assessments)),
                    "retried_rules": sum(len(m.retried_rule_ids) for m in matches),
                    "missing_after_retry": sum("평가 누락" in a.reasoning for m in matches for a in m.assessments),
                    "not_evaluated": sum(m.eligibility == "NOT_EVALUATED" for m in matches)},
        "errors": sorted({e["error"].split("(")[0] for e in s.errors}),
        "questions": len(run.questions), "safety": dict(run.safety),
    }


def run_experiment_a(case, gt, trials: dict, rounds: int = 1, budget: int = 3) -> dict:
    """실험 A 한 사례: 실제 Session 경로(run_method, Decision Loop 계획, 결정적 정답 시뮬레이터) + 비식별 지표."""
    holder: dict = {}

    def factory(ref):  # 기본 candidate 경로와 같은 생성기, Session만 잡아 둠
        holder["ref"] = ref
        return qe.candidate_generator(ref)
    run = qe.run_method("candidate", case, gt, trials, rounds, budget, factories={"candidate": factory})
    return pipeline_metrics(run, holder["ref"]["session"], gt)


def _two_terms(text: str) -> bool:
    """한 문장에 신장 eGFR과 유전자 EGFR이 둘 다 있는 경우 (절 단위로 구분되므로 연결 오류가 아님)."""
    return "lab:egfr" in non_biomarker_mentions(text) and "EGFR" in text and "eGFR" in text


# ---------------------------------------------------------------------------
# 비식별화 검사
# ---------------------------------------------------------------------------


def forbidden_texts(pairs) -> list[str]:
    """출력에 나오면 안 되는 문자열: 사례 원문의 긴 줄, 숨긴 정답 원문, API 키."""
    out = []
    for case, gt in pairs:
        out += [ln.strip() for ln in case.raw_text.splitlines() if len(ln.strip()) >= 12]
        out += [t for _, t in gt._hidden_texts if t]
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.getenv(k):
            out.append(os.environ[k])
    return out


def assert_redacted(obj, forbidden: list[str]) -> None:
    blob = json.dumps(obj, ensure_ascii=False, default=str)
    leaked = sum(1 for t in forbidden if t and t in blob)
    if leaked:
        raise ValueError(f"출력에 원문·정답·키로 보이는 문자열 {leaked}건 (내용은 표시하지 않음)")


# ---------------------------------------------------------------------------
# 오프라인 측정과 비용 추정
# ---------------------------------------------------------------------------


@dataclass
class _Recorder:
    calls: Counter = field(default_factory=Counter)
    chars: dict = field(default_factory=dict)  # stage -> [요청 문자 수]
    profile_chars: list = field(default_factory=list)

    def add(self, stage: str, n: int):
        self.calls[stage] += 1
        self.chars.setdefault(stage, []).append(n)


def _qset_standin(profile, matches, qa_log, n, acquisition_plan=None):
    """오프라인 대역: 실제 생성기가 보낼 프롬프트 크기는 재고, 질문은 기존 생성기 입력 순서대로 (baseline 대역)."""
    return qe.baseline_generator(profile, matches, qa_log, n, acquisition_plan)


@contextlib.contextmanager
def _recording(env: qe.OfflineEnvironment, rec: _Recorder):
    from . import pipeline
    from .schemas import QuestionSet
    stub, prof = llm.structured, pipeline.patient_profiler.profile_patient
    state: dict = {}

    def structured(model_cls, *, system, user, **k):
        stage = stage_of(system)
        rec.add(stage, len(system) + len(user) + len(json.dumps(llm.strict_schema(model_cls), ensure_ascii=False)))
        if "<patient>" in user:
            rec.profile_chars.append(len(user.split("<patient>")[1].split("</patient>")[0]))
        if model_cls is QuestionSet:
            return state["qset"]
        return stub(model_cls, system=system, user=user, **k)

    def profile(patient_id, raw_text, reference_date=None):
        from .schemas import PatientProfile
        rec.add("patient_profiler", len(patient_profiler.SYSTEM) + len(raw_text) + 120
                + len(json.dumps(llm.strict_schema(PatientProfile), ensure_ascii=False)))
        return prof(patient_id, raw_text, reference_date)

    def gen(profile_, matches, qa_log, n, acquisition_plan=None):
        state["qset"] = _qset_standin(profile_, matches, qa_log, n, acquisition_plan)
        return question_generator.generate_questions(profile_, matches, qa_log, n, acquisition_plan)

    llm.structured = structured
    pipeline.patient_profiler.profile_patient = profile
    try:
        yield gen
    finally:
        llm.structured = stub
        pipeline.patient_profiler.profile_patient = prof


def measure_offline(pairs, trials: dict, budget: int = 3) -> dict:
    """대역(LLM 없음)으로 두 실험을 실제 순서대로 돌려 단계별 호출 수와 요청 크기(문자)를 잼."""
    per_exp: dict = {}
    results: dict = {"A_pipeline": [], "B_question_plan": []}
    for exp in ("A_pipeline", "B_question_plan"):
        rec = _Recorder()
        with qe.OfflineEnvironment(budget) as env, _recording(env, rec) as gen:
            for case, gt in pairs:
                if exp == "A_pipeline":
                    results[exp].append(run_experiment_a(case, gt, trials, EXPERIMENTS[exp]["rounds"], budget))
                else:
                    results[exp].append(compare_plans(case, gt, trials, budget, llm_generate=gen))
        per_exp[exp] = {"calls": dict(rec.calls), "chars": {k: {"mean": round(sum(v) / len(v)), "max": max(v)}
                                                           for k, v in rec.chars.items()},
                        "stub_profile_json_chars_max": max(rec.profile_chars or [0])}
    return {"per_experiment": per_exp, "results": results, "n_cases": len(pairs)}


# 출력 토큰(호출 1회, effort에 따른 사고 토큰 포함) 가정. worst는 max_tokens 상한
OUTPUT_TOKENS = {
    "low": {"patient_profiler": 800, "matcher": 1200, "question_generator": 400, "recommender": 800},
    "base": {"patient_profiler": 2000, "matcher": 4000, "question_generator": 1500, "recommender": 2500},
    "worst": {"patient_profiler": 16000, "matcher": 16000, "question_generator": 16000, "recommender": 16000},
}
CHARS_PER_TOKEN = {"low": 3.0, "base": 2.0, "worst": 1.0}  # 한글·영문 JSON 혼합 가정
REAL_PROFILE_JSON_CHARS = {"low": 1500, "base": 3000, "worst": 8000}  # 실제 프로필 JSON 크기 가정 (대역 프로필은 작음)
RETRY_FACTOR = {"low": 1.0, "base": 1.15, "worst": None}  # worst는 재시도 상한(1 + max_retries_per_call)과 누락 재요청 100%
CACHE = {"low": "system_read_after_first", "base": "none", "worst": "system_write_every_call"}


def estimate(measured: dict, scenario: str, price: PriceTable = ASSUMED_PRICE, retries: int = 2) -> dict:
    """측정한 호출 수·요청 크기 + 시나리오 가정으로 실험별 비용. 확인 전 값은 표시."""
    cpt, out_tok = CHARS_PER_TOKEN[scenario], OUTPUT_TOKENS[scenario]
    res = {}
    for exp, m in measured["per_experiment"].items():
        calls_total = inp_total = out_total = cost = 0.0
        stages = {}
        for stage, n in m["calls"].items():
            if stage == "matcher":  # 누락 규칙 재요청
                n *= {"low": 1.0, "base": 1.0 + 0.1 * config.MATCHER_MISSING_RETRIES,
                      "worst": 1.0 + config.MATCHER_MISSING_RETRIES}[scenario]
            n *= RETRY_FACTOR[scenario] or (1 + retries)  # worst: 모든 요청이 재시도 상한까지
            chars = m["chars"].get(stage, {}).get("mean", 0)
            if stage in ("matcher", "question_generator", "recommender"):
                chars += max(0, REAL_PROFILE_JSON_CHARS[scenario] - m["stub_profile_json_chars_max"])
            inp = chars / cpt
            system_tokens = _system_len(stage) / cpt
            if CACHE[scenario] == "system_read_after_first" and n > 1:
                c = price.cost(inp - system_tokens, out_tok.get(stage, 2000)) * n \
                    + price.cost(0, 0, cache_write=system_tokens) + price.cost(0, 0, cache_read=system_tokens) * (n - 1)
            elif CACHE[scenario] == "system_write_every_call":
                c = (price.cost(inp - system_tokens, out_tok.get(stage, 2000)) + price.cost(0, 0, cache_write=system_tokens)) * n
            else:
                c = price.cost(inp, out_tok.get(stage, 2000)) * n
            stages[stage] = {"calls": round(n, 1), "input_tokens_per_call": int(inp),
                             "output_tokens_per_call": out_tok.get(stage, 2000), "usd": round(c, 3)}
            calls_total += n
            inp_total += inp * n
            out_total += out_tok.get(stage, 2000) * n
            cost += c
        res[exp] = {"scenario": scenario, "calls": round(calls_total, 1), "input_tokens": int(inp_total),
                    "output_tokens": int(out_total), "usd": round(cost, 2), "stages": stages}
    return {"scenario": scenario, "price": asdict(price), "price_confirmed": price.confirmed,
            "tokens_confirmed": False, "cache_effect": CACHE[scenario], "experiments": res}


def _system_len(stage: str) -> int:
    mod = {"patient_profiler": patient_profiler, "matcher": matcher, "question_generator": question_generator,
           "recommender": recommender}.get(stage)
    return len(mod.SYSTEM) if mod else 0


def suggest_limits(worst_exp: dict, base_exp: dict, retries: int = 2) -> PilotLimits:
    """상한 제안: 호출 수는 최악 시나리오 호출 수, 비용은 기준 추정의 3배와 최악값 중 작은 값을 올림."""
    return PilotLimits(max_cost_usd=float(math.ceil(min(worst_exp["usd"], max(1.0, base_exp["usd"] * 3)))),
                       max_calls=int(math.ceil(worst_exp["calls"])),
                       max_input_tokens=int(math.ceil(worst_exp["input_tokens"] / 1000) * 1000),
                       # 동시 실행 중 예약(호출당 max_tokens)이 막히지 않도록 최소 (동시 수 + 1) x 16000
                       max_output_tokens=int(math.ceil(max(min(worst_exp["output_tokens"], base_exp["output_tokens"] * 4),
                                                           (config.MAX_WORKERS + 1) * 16000) / 1000) * 1000),
                       max_retries_per_call=retries)
