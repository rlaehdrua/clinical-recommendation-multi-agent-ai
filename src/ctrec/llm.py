"""Claude API 공통 래퍼.

- 모든 에이전트는 이 모듈을 통해서만 Claude를 호출합니다.
- 구조화 출력: Pydantic 모델 -> strict JSON schema -> output_config.format
- 안전 분류기에 의한 거절(stop_reason == "refusal")은 서버측 fallback으로 1차 대응하고,
  그래도 거절되면 RefusalError를 발생시킵니다.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from . import config

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class RefusalError(RuntimeError):
    pass


class TruncatedError(RuntimeError):
    pass


_client: anthropic.Anthropic | None = None

# 토큰 사용량 누적. 환자별로 따로 집계되도록 contextvars로 현재 집계 대상(dict)을 지정합니다.
# (여러 환자를 동시에 처리해도 섞이지 않음. 하위 스레드에는 copy_context()로 전달)
_usage_lock = threading.Lock()
_usage_var: contextvars.ContextVar[dict[str, int]] = contextvars.ContextVar("ctrec_usage")


def reset_usage() -> None:
    """현재 컨텍스트(환자)의 사용량 집계를 새로 시작."""
    _usage_var.set({})


def _record_usage(response) -> None:
    u = getattr(response, "usage", None)
    usage = _usage_var.get(None)
    if u is None or usage is None:
        return
    with _usage_lock:
        usage["calls"] = usage.get("calls", 0) + 1
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            usage[key] = usage.get(key, 0) + (getattr(u, key, 0) or 0)


def usage_snapshot() -> dict[str, int]:
    with _usage_lock:
        return dict(_usage_var.get(None) or {})


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # 자격 증명: ANTHROPIC_API_KEY -> ANTHROPIC_AUTH_TOKEN -> `ant auth login` 프로필
        _client = anthropic.Anthropic(max_retries=4)
    return _client


# ---------------------------------------------------------------------------
# JSON schema 변환
# ---------------------------------------------------------------------------

# 구조화 출력 스키마에서 제거할 키(검증은 응답 수신 후 Pydantic이 다시 수행)
_DROP_KEYS = {
    "title", "default", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "minLength", "maxLength", "pattern", "minItems", "maxItems",
    "format",
}


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(n) for n in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("properties", "$defs"):
            # 이 딕셔너리의 키는 필드명이므로 그대로 두고 값만 변환
            out[key] = {name: _strictify(sub) for name, sub in value.items()}
        elif key in _DROP_KEYS:
            continue
        else:
            out[key] = _strictify(value)
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out


def strict_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    return _strictify(model_cls.model_json_schema())


# ---------------------------------------------------------------------------
# 호출
# ---------------------------------------------------------------------------

def create(
    *,
    system: str,
    messages: list[dict[str, Any]],
    effort: str = "high",
    max_tokens: int = 16000,
    tools: list[dict[str, Any]] | None = None,
    output_schema: dict[str, Any] | None = None,
):
    """Messages API 단일 호출. 거절 시 RefusalError."""
    output_config: dict[str, Any] = {"effort": effort}
    if output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": output_schema}

    kwargs: dict[str, Any] = dict(
        model=config.MODEL,
        max_tokens=max_tokens,
        # 시스템 프롬프트는 에이전트별로 고정 -> 프롬프트 캐싱 대상
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        output_config=output_config,
    )
    if tools:
        kwargs["tools"] = tools

    if config.USE_FALLBACKS:
        # 거절 카테고리에 따라 서버가 대체 모델로 재실행 (Claude API 전용 beta)
        response = client().beta.messages.create(
            betas=[FALLBACK_BETA], fallbacks="default", **kwargs
        )
    else:
        response = client().messages.create(**kwargs)
    _record_usage(response)

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise RefusalError(f"Claude declined the request (category={category})")
    return response


def text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text")


def structured(
    model_cls: type[T],
    *,
    system: str,
    user: str,
    effort: str = "medium",
    max_tokens: int = 16000,
) -> T:
    """Pydantic 모델 형태의 구조화 출력을 받아 검증 후 반환."""
    response = create(
        system=system,
        messages=[{"role": "user", "content": user}],
        effort=effort,
        max_tokens=max_tokens,
        output_schema=strict_schema(model_cls),
    )
    if response.stop_reason == "max_tokens":
        raise TruncatedError(f"{model_cls.__name__}: output truncated at max_tokens={max_tokens}")
    return model_cls.model_validate_json(text_of(response))


def plain(*, system: str, user: str, effort: str = "medium", max_tokens: int = 16000) -> str:
    response = create(
        system=system,
        messages=[{"role": "user", "content": user}],
        effort=effort,
        max_tokens=max_tokens,
    )
    return text_of(response)
