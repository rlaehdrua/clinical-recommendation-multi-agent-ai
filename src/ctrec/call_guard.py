"""미계측 LLM 호출 차단(fail-closed)·관측 (Phase 5-3).

contextvars만으로는 부족합니다: 컨텍스트를 넘기지 않은 스레드(맨 ThreadPoolExecutor.submit, run_in_executor,
threading.Thread)는 빈 컨텍스트를 보므로 "컨텍스트가 비었는지"로는 실행 소속을 알 수 없습니다. 그래서 프로세스 전역
상태로 두 지점을 지킵니다.

1. llm 호출 관문 (llm.create / llm.client): 파일럿(배타) 실행이 하나라도 진행 중이면, 지금 컨텍스트의 클라이언트가
   등록된 MeteredClient일 때만 통과. 컨텍스트가 없는 스레드(클라이언트 None)·기본 클라이언트·등록되지 않은 대리 객체는
   UnmeteredCallBlocked.
2. anthropic SDK 요청 (SyncAPIClient.request / _attempt_request, AsyncAPIClient 동일): 배타 실행 중에는 같은 스레드에서
   MeteredClient가 '이 SDK 클라이언트로 보내는 중'이라고 표시(mark)한 요청만 통과. SDK 자동 재시도(재시도 회차 > 0,
   요청별 max_retries > 0)와 미들웨어 체인(클라이언트 측 대체 모델)도 차단. async SDK는 배타 실행 중 전부 차단.

관측(웹 기록 전용 계측)은 같은 두 지점에서 막지 않고 셉니다: 등록된 대리 객체를 거치지 않은 호출·SDK 시도는
unattributed로 세어, 그 사이에 진행된 실행이 '완전히 계측됨'으로 표시되지 않게 합니다.

한계 (막지 못함): SDK를 쓰지 않는 직접 HTTP 요청(httpx·requests·urllib로 api.anthropic.com 호출), 다른 프로세스
(subprocess·multiprocessing), 이 모듈이나 SDK 내부 함수를 다시 바꿔 끼우는 코드, 다른 LLM 제공자 SDK.
"""

from __future__ import annotations

import contextlib
import functools
import threading
from dataclasses import dataclass, field

from . import llm


class UnmeteredCallBlocked(llm.RunCancelled):
    """파일럿 실행 중 계측되지 않은 LLM 호출을 막음 (args[0]: 'unmetered_call_blocked', args[1]: 이유 코드)."""


@dataclass
class Mark:
    """MeteredClient·RecordingClient가 실제 클라이언트로 요청을 보내는 동안 그 스레드에 남기는 표시."""
    kind: str  # "metered" / "recording"
    inner_id: int | None  # 보내는 대상 SDK 클라이언트 id (지연 생성이면 None)
    attempts: int = 0  # SDK HTTP 시도 수 (재시도 포함, anthropic SDK일 때만 보임)


@dataclass
class _State:
    lock: threading.Lock = field(default_factory=threading.Lock)
    proxies: dict = field(default_factory=dict)  # id(proxy) -> kind ("metered" / "recording")
    exclusive: int = 0  # 진행 중인 배타(fail-closed) 실행 수
    observers: int = 0  # 진행 중인 관측(기록 전용) 실행 수
    blocked: int = 0
    unattributed: int = 0
    block_reasons: dict = field(default_factory=dict)
    sdk_saved: dict = field(default_factory=dict)


_S = _State()
_local = threading.local()


def _marks() -> list:
    if not hasattr(_local, "marks"):
        _local.marks = []
    return _local.marks


def current_mark() -> Mark | None:
    m = _marks()
    return m[-1] if m else None


@contextlib.contextmanager
def mark(kind: str, inner=None):
    m = Mark(kind, id(inner) if inner is not None else None)
    _marks().append(m)
    try:
        yield m
    finally:
        _marks().pop()


def _block(reason: str):
    with _S.lock:
        _S.blocked += 1
        _S.block_reasons[reason] = _S.block_reasons.get(reason, 0) + 1
    raise UnmeteredCallBlocked("unmetered_call_blocked", reason)


def counters() -> dict:
    with _S.lock:
        return {"blocked": _S.blocked, "unattributed": _S.unattributed, "exclusive": _S.exclusive,
                "observers": _S.observers, "block_reasons": dict(_S.block_reasons),
                "sdk_hooks_installed": bool(_S.sdk_saved)}


# ---------------------------------------------------------------- 관문 1: llm.create / llm.client


def gate(c) -> None:
    """llm.current_client(컨텍스트 클라이언트가 있을 때)와 llm.client(기본 클라이언트)가 부름. c=None은 기본 클라이언트."""
    if not (_S.exclusive or _S.observers):
        return
    kind = _S.proxies.get(id(c)) if c is not None else None
    if _S.exclusive:
        if kind != "metered":
            _block("no_metered_client" if c is None else "unregistered_client")
        return
    m = current_mark()
    if kind is None and m is None:  # 등록된 대리 객체 밖의 호출 (컨텍스트를 잃은 스레드 등)
        with _S.lock:
            _S.unattributed += 1


# ---------------------------------------------------------------- 관문 2: anthropic SDK 요청


def _sync_request(orig):
    @functools.wraps(orig)
    def request(self, cast_to, options, *a, **k):
        if _S.exclusive:
            m = current_mark()
            if m is None or m.kind != "metered" or m.inner_id != id(self):
                _block("sdk_without_metered_mark")
            if options.get_max_retries(self.max_retries) != 0:
                _block("sdk_auto_retry")
            if getattr(self, "_middleware_chain", None) is not None:
                _block("sdk_middleware")
        return orig(self, cast_to, options, *a, **k)
    return request


def _sync_attempt(orig):
    @functools.wraps(orig)
    def attempt(self, options, *a, **k):
        m = current_mark()
        if _S.exclusive:
            if m is None or m.kind != "metered" or m.inner_id != id(self):
                _block("sdk_without_metered_mark")
            if k.get("retries_taken", 0):
                _block("sdk_auto_retry")
        if m is not None:
            m.attempts += 1
        elif _S.observers:
            with _S.lock:
                _S.unattributed += 1
        return orig(self, options, *a, **k)
    return attempt


def _async_request(orig):
    @functools.wraps(orig)
    async def request(self, *a, **k):
        if _S.exclusive:  # 계측 경로는 동기 SDK만 씀 -> 배타 실행 중 async SDK는 전부 차단
            _block("async_sdk")
        elif _S.observers:
            with _S.lock:
                _S.unattributed += 1
        return await orig(self, *a, **k)
    return request


def _install_locked() -> None:
    if _S.sdk_saved:
        return
    try:
        from anthropic import _base_client as b
    except ImportError:  # pragma: no cover - SDK가 없으면 SDK 요청도 없음
        return
    targets = {(b.SyncAPIClient, "request"): _sync_request, (b.SyncAPIClient, "_attempt_request"): _sync_attempt,
               (b.AsyncAPIClient, "request"): _async_request}
    for (cls, name), wrap in targets.items():
        orig = cls.__dict__[name]
        _S.sdk_saved[(cls, name)] = orig
        setattr(cls, name, wrap(orig))


def _uninstall_locked() -> None:
    for (cls, name), orig in _S.sdk_saved.items():
        setattr(cls, name, orig)
    _S.sdk_saved.clear()


def _sync_hooks_locked() -> None:
    if _S.exclusive or _S.observers:
        _install_locked()
    else:
        _uninstall_locked()
    llm._gate = gate if (_S.exclusive or _S.observers) else None


# ---------------------------------------------------------------- 등록


@contextlib.contextmanager
def register(proxy, kind: str, *, exclusive: bool = False, observe: bool = False):
    """proxy(MeteredClient·RecordingClient)를 등록. exclusive=True면 끝날 때까지 프로세스 전체 fail-closed,
    observe=True면 등록되지 않은 호출을 셈. 끝나면(예외 포함) 해제하고, 진행 중인 실행이 없으면 훅을 걷음."""
    if kind not in ("metered", "recording"):
        raise ValueError(kind)
    with _S.lock:
        _S.proxies[id(proxy)] = kind
        _S.exclusive += int(exclusive)
        _S.observers += int(observe)
        _sync_hooks_locked()
    try:
        yield
    finally:
        with _S.lock:
            _S.proxies.pop(id(proxy), None)
            _S.exclusive -= int(exclusive)
            _S.observers -= int(observe)
            _sync_hooks_locked()
