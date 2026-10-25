"""모든 테스트 공통: 테스트가 남긴 웹 실행 작업 스레드를 그 테스트가 끝날 때 정리 (P14 웹 테스트 경합).

웹 실행은 데몬 스레드에서 돌고, 하네스의 RUNS.clear()는 스레드를 멈추지 않음. 실행을 시작만 하고 끝난 테스트
(test_run_id_not_guessable 등)의 스레드가 다음 테스트로 넘어가 그 테스트의 LLM 대역에 요청을 섞거나(요청 수 비교 실패),
대역이 복원된 뒤 기본 클라이언트 생성 경로(llm.client -> anthropic.Anthropic)로 갔음(test_web_metering 간헐 실패).
monkeypatch에 의존하므로 대역이 복원되기 전에 정리됨: 실행을 취소하면 다음 LLM 호출 직전 확인 지점(llm.guard)에서 멈춤.
"""

from __future__ import annotations

import sys
import threading

import pytest

STOP_TIMEOUT_S = 30  # 대역 안에서 멈춘 호출(FakeLLM.block_on, 최대 20초)도 끝날 수 있게


@pytest.fixture(autouse=True)
def _stop_leftover_web_runs(monkeypatch):
    before = set(threading.enumerate())
    yield
    web = sys.modules.get("ctrec.web.app")
    if web is None:  # 웹 앱을 쓰지 않은 테스트
        return
    runs = {id(r): r for r in list(web.RUNS.values()) if not r.done}
    workers = []
    for t in threading.enumerate():
        run = next((a for a in getattr(t, "_args", ()) if isinstance(a, web.Run)), None)  # start_run의 작업 스레드
        if t not in before and run is not None:
            runs[id(run)] = run  # RUNS.clear()로 목록에서 빠진 실행도
            workers.append(t)
    for r in runs.values():
        r.cancel_requested.set()
    for t in workers:
        t.join(STOP_TIMEOUT_S)
    left = [t.name for t in workers if t.is_alive()]
    assert not left, f"웹 실행 작업 스레드가 {STOP_TIMEOUT_S}초 안에 끝나지 않음: {left}"
