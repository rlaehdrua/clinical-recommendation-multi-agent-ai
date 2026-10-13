"""웹 화면 브라우저 스모크 테스트 (선택 실행). LLM 대역 + 로컬 서버 + 실제 브라우저(설치된 Edge/Chrome).

기본 실행에서는 건너뜁니다. 실행: CTREC_BROWSER_TESTS=1 (playwright 패키지와 Edge 또는 Chrome 필요, 브라우저 다운로드 없음)
외부 요청(CDN 글꼴·marked·DOMPurify)은 막아 오프라인으로 실행합니다 -> 보고서는 화면의 대체 표시(<pre>)로 나옴.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

if os.getenv("CTREC_BROWSER_TESTS") != "1":
    pytest.skip("브라우저 테스트는 CTREC_BROWSER_TESTS=1일 때만", allow_module_level=True)
sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h
from ctrec.web import app as web


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(monkeypatch, tmp_path):
    fake = h.install(monkeypatch, tmp_path)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", fake
    srv.should_exit = True
    t.join(5)


def _browser(p):
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel, headless=True)
        except Exception:  # noqa: BLE001 - 설치된 브라우저를 차례로 시도
            continue
    pytest.skip("Edge/Chrome을 찾을 수 없음")


def test_browser_flow(server):
    base, fake = server
    errors, console = [], []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page()
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: console.append(m.text))
        page.goto(base)
        page.wait_for_selector("#patient option", state="attached")  # Flow A: 설정·환자·시험 목록을 API에서 불러옴
        assert page.locator("#trials input").count() == 26
        # Flow B: 직접 입력 + 시험 직접 선택
        page.click("#to-custom")
        page.fill("#custom", h.SYN_TEXT)
        page.check('input[name="cand-mode"][value="given"]')
        page.evaluate("ids => document.querySelectorAll('#trials input').forEach(i => i.checked = ids.includes(i.value))",
                      h.TRIAL_IDS)
        page.click("#start")
        # Flow C: 질문 표시 -> 답변
        page.wait_for_selector("#qslot input[data-q='Q1']", timeout=20000)
        page.fill("#qslot input[data-q='Q1']", h.SYN_ANSWER)
        page.click("#send")
        # Flow D: 결과 화면
        page.wait_for_selector("#final .rec", timeout=30000)
        final = page.inner_text("#final")
        assert "NCT04063540" in final and "추가 정보 필요" in final
        assert page.locator("#final a[href='https://clinicaltrials.gov/study/NCT04063540']").count() >= 1
        assert "의사 확인 필요" in final  # 추가 검토가 필요한 기준
        assert page.inner_text("#run-state") == "완료"
        assert page.locator("#cards .card").count() == 2
        browser.close()
    assert errors == [], errors  # 자바스크립트 오류 없음
    assert not any(h.SYN_ANSWER in m or "58세" in m for m in console)  # 콘솔에 환자 정보 출력 없음


def test_browser_shows_server_validation_message(server):
    base, _ = server
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page()
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
        page.goto(base)
        page.wait_for_selector("#patient option", state="attached")
        page.click("#to-custom")
        page.click("#start")  # 빈 입력: 화면에서 막음
        assert page.inner_text("#start-hint") == "환자 정보를 입력하세요."
        page.fill("#custom", "x" * 20001)  # 서버가 거절 -> 문자열 메시지를 그대로 보여 줌
        page.click("#start")
        page.wait_for_function("document.querySelector('#start-hint').textContent.startsWith('요청 형식')")
        assert not page.is_disabled("#start")
        browser.close()


def _open_custom_run(page, base, path=""):
    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
    page.goto(base + path)
    page.wait_for_selector("#patient option", state="attached")
    page.click("#to-custom")
    page.fill("#custom", h.SYN_TEXT)
    page.check('input[name="cand-mode"][value="given"]')
    page.evaluate("ids => document.querySelectorAll('#trials input').forEach(i => i.checked = ids.includes(i.value))",
                  h.TRIAL_IDS)
    page.click("#start")
    page.wait_for_selector("#qslot input[data-q='Q1']", timeout=20000)


def test_browser_cancel_refresh_and_other_profile(server):
    """취소 버튼, 새로고침 후 이어 보기, 다른 브라우저 프로필(쿠키 없음)의 접근 거부."""
    base, _ = server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        owner = browser.new_context()
        page = owner.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        _open_custom_run(page, base)
        run_id = page.evaluate("sessionStorage.getItem('ctrec-run-id')")
        assert run_id and page.evaluate("document.cookie") == ""  # 토큰은 HttpOnly라 스크립트가 읽지 못함
        # 새로고침: 같은 탭에서 실행을 이어 보고 질문이 다시 표시됨
        page.reload()
        page.wait_for_selector("#qslot input[data-q='Q1']", timeout=20000)
        assert page.is_visible("#cancel")
        # 다른 프로필(다른 브라우저와 같음): 실행 ID를 알아도 접근 불가
        stranger = browser.new_context().new_page()
        stranger.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
        stranger.goto(base)
        status = stranger.evaluate("async id => (await fetch(`/api/runs/${id}/events`)).status", run_id)
        answer = stranger.evaluate("""async id => (await fetch(`/api/runs/${id}/answers`, {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CTREC-Request': '1'},
            body: JSON.stringify({answers: {Q1: 'x'}})})).status""", run_id)
        assert (status, answer) == (404, 404)
        # 취소
        page.click("#cancel")
        page.wait_for_function("document.querySelector('#run-state').textContent === '취소됨'", timeout=10000)
        assert page.is_hidden("#cancel") and not page.is_disabled("#start")
        assert page.inner_html("#qslot") == ""
        browser.close()
    assert errors == [], errors


def test_browser_decision_diagnostic_panel(server):
    """개발자 진단 패널: 기본 숨김, ?diagnostic=1일 때만 진단 이벤트를 따로 표시하고 추천 화면은 그대로."""
    base, _ = server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
        page.goto(base)
        page.wait_for_selector("#patient option", state="attached")
        assert page.is_hidden("#decision-panel")
        _open_custom_run(page, base, "/?diagnostic=1")
        page.wait_for_selector("#decision-body .card", state="attached", timeout=20000)
        assert page.is_visible("#decision-panel")  # 접힌 개발자 영역으로 표시
        body = page.text_content("#decision-body")
        assert "라운드 0" in body and "ASK_USER" in body
        page.fill("#qslot input[data-q='Q1']", h.SYN_ANSWER)
        page.click("#send")
        page.wait_for_selector("#final .rec", timeout=30000)
        assert page.locator("#decision-body .card").count() == 2 and "NCT04063540" in page.inner_text("#final")
        browser.close()
    assert errors == [], errors
