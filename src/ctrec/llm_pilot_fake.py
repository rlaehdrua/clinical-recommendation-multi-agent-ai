"""실제 SDK와 같은 호출 인터페이스(messages.create / beta.messages.create)의 오프라인 대역 (Phase 5-1).

네트워크·유료 호출이 없습니다. 실험 실행기(llm_pilot_exec.execute)는 승인 표 없이는 이 대역만 받습니다.
응답 내용은 오프라인 대역(question_eval.stub_profiler, StubLLM)과 같은 규칙으로 만들고, 사용량은 요청 크기로 추정합니다.
장애 주입(faults)으로 429·5xx·잘못된 요청·잘림·거절·시간 초과·사용량 누락·다른 모델 응답·대체 모델 실행을 재현합니다.
"""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace
from typing import Callable

from . import llm_pilot as lp, question_eval as qe
from .schemas import (ClarifyingQuestion, LLMAssessment, MatcherOutput, QuestionSet, RecommendationDraft)


# SDK 예외와 같은 이름·status_code (재시도·과금 분류는 이름과 상태 코드로 판단)
class FakeAPIError(Exception):
    status_code: int | None = None


class RateLimitError(FakeAPIError):
    status_code = 429


class InternalServerError(FakeAPIError):
    status_code = 500


class BadRequestError(FakeAPIError):
    status_code = 400


class APITimeoutError(FakeAPIError):
    pass


class APIConnectionError(FakeAPIError):
    pass


FAULTS = {"429": RateLimitError, "5xx": InternalServerError, "400": BadRequestError, "timeout": APITimeoutError,
          "connection": APIConnectionError}
# 응답은 오지만 문제가 있는 경우
RESPONSE_FAULTS = ("truncated", "refusal", "no_usage", "other_model", "fallback_iteration", "pii", "priority_tier")
# 요청 도중 프로세스 중단(Ctrl+C 등) 흉내
INTERRUPT = "interrupt"
FAKE_PII = "연락처 010-1234-5678 test.patient@example.com"


def _between(text: str, tag: str) -> str:
    return text.split(f"<{tag}>")[1].split(f"</{tag}>")[0]


def respond(stage: str, user: str) -> str:
    """단계별 결정적 응답(JSON 문자열). 판정은 모두 '판정 불가', 질문은 확보 계획 순서대로."""
    if stage == "patient_profiler":
        pid = re.search(r"patient_id: (\S+)", user).group(1)
        return qe.stub_profiler(pid, _between(user, "patient_profile").strip("\n")).model_dump_json()
    if stage == "matcher":
        rules = json.loads(_between(user, "rules_to_assess"))
        return MatcherOutput(assessments=[LLMAssessment(
            rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent", confidence="low",
            evidence="근거 없음", reasoning="fake sdk", missing_info="확인 필요") for r in rules]).model_dump_json()
    if stage == "question_generator":
        plan = json.loads(_between(user, "acquisition_plan"))
        n = int(re.search(r"최대 (\d+)개", user).group(1))
        return QuestionSet(questions=[ClarifyingQuestion(
            question_id=f"Q{i + 1}", question="[fake] 확인 질문", answer_type="free_text", target=[k],
            purpose="eligibility", why="fake sdk") for i, k in enumerate(list(plan)[:n])]).model_dump_json()
    if stage == "recommender":
        pid = user.split("patient_id=")[1].split(" ")[0]
        return RecommendationDraft(patient_id=pid, ranked=[], excluded=[], overall_comment="fake sdk").model_dump_json()
    raise BadRequestError("fake sdk: 지원하지 않는 단계")


class _Messages:
    def __init__(self, owner: "FakeSDKClient"):
        self._owner = owner

    def create(self, **kw):
        return self._owner._create(kw)


class FakeSDKClient:
    """anthropic.Anthropic과 같은 모양의 대역. max_retries=0 (SDK 자동 재시도 없음).

    faults: 호출 순번(0부터)과 단계를 받아 장애 코드(FAULTS·RESPONSE_FAULTS 키) 또는 None을 돌려주는 함수.
    on_call: 요청을 받을 때마다 호출 (취소·동시성 재현용)."""

    max_retries = 0

    def __init__(self, faults: Callable[[int, str], str | None] | None = None, chars_per_token: float = 2.0,
                 output_tokens: dict | None = None, cache_read_ratio: float = 0.0,
                 on_call: Callable[[int, dict], None] | None = None):
        self._faults, self._cpt, self._cache = faults, chars_per_token, cache_read_ratio
        self._out = output_tokens or lp.OUTPUT_TOKENS["base"]
        self._on_call = on_call
        self._lock = threading.Lock()
        self.calls: list[dict] = []  # 단계·요청 옵션만 (원문 없음)
        self.messages = _Messages(self)
        self.beta = SimpleNamespace(messages=_Messages(self))

    def _create(self, kw: dict):
        system = kw.get("system")
        stage = lp.stage_of(system)
        with self._lock:
            i = len(self.calls)
            self.calls.append({"stage": stage, "timeout": kw.get("timeout"), "max_tokens": kw.get("max_tokens"),
                               "model": kw.get("model"), "fallbacks": "fallbacks" in kw})
        if self._on_call:
            self._on_call(i, kw)
        fault = self._faults(i, stage) if self._faults else None
        if fault == INTERRUPT:
            raise KeyboardInterrupt("fake sdk interrupt")
        if fault in FAULTS:
            raise FAULTS[fault]("fake sdk fault")
        user = kw["messages"][0]["content"]
        text = respond(stage, user)
        if fault == "pii":  # 응답에 개인정보가 섞인 경우 (출력 비식별화 확인용)
            text = text.replace("fake sdk", FAKE_PII)
            if stage == "patient_profiler":
                d = json.loads(text)
                d["other_facts"].append({"description": FAKE_PII, "date": None, "source_quote": FAKE_PII})
                text = json.dumps(d, ensure_ascii=False)
        est_in = int(lp._request_chars(kw) / self._cpt)
        system_tokens = int(len(system[0]["text"]) / self._cpt) if isinstance(system, list) else 0
        cache_read = int(system_tokens * self._cache)
        out = min(self._out.get(stage, 1000), kw.get("max_tokens") or 16000)
        # 공식 응답 구조: 출력 토큰 중 사고 토큰은 output_tokens_details.thinking_tokens (여기서는 출력의 절반으로 가정)
        usage = SimpleNamespace(input_tokens=est_in - cache_read, output_tokens=out,
                                output_tokens_details=SimpleNamespace(thinking_tokens=out // 2),
                                cache_creation_input_tokens=0, cache_read_input_tokens=cache_read, iterations=None,
                                service_tier="standard")
        stop = "end_turn"
        model = kw.get("model")
        if fault == "truncated":
            stop, usage.output_tokens = "max_tokens", kw.get("max_tokens") or 16000
        elif fault == "refusal":
            stop, text = "refusal", ""
        elif fault == "no_usage":
            usage = None
        elif fault == "other_model":
            model = "claude-other-model"
        elif fault == "priority_tier":
            usage.service_tier = "priority"
        elif fault == "fallback_iteration":
            usage.iterations = [SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")]
        return SimpleNamespace(
            id=f"msg_fake_{i}", model=model, stop_reason=stop,
            stop_details=SimpleNamespace(category="fake") if stop == "refusal" else None,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            usage=usage)
