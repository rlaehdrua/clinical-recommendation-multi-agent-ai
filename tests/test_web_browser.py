"""웹 화면 브라우저 E2E (선택 실행). 로컬 서버 + 실제 브라우저(설치된 Edge/Chrome) + 합성 사례. 유료 API 호출 없음.

기본 실행에서는 건너뜁니다. 실행: CTREC_BROWSER_TESTS=1 (playwright 패키지와 Edge 또는 Chrome 필요, 브라우저 다운로드 없음)
외부 요청(CDN 글꼴·marked·DOMPurify)은 막아 오프라인으로 실행합니다 -> 보고서는 화면의 대체 표시(<pre>)로 나옴.

두 가지 LLM 대역:
- `server` (테스트 대역 흐름): web_harness.install - llm.create/structured/plain 함수를 합성 출력으로 바꿈.
  실제 FastAPI·오케스트레이터 루프·Session·Matcher·추천 코드는 그대로.
- `sdk_server` (실제 코드 경로 흐름): web_harness.install_sdk - LLM 함수는 그대로 두고 기본 SDK 클라이언트 자리만 대역.
  실제 llm.create·구조화 출력 검증·웹 실행별 기록기(web_metering)까지 실제 코드로 지나감.
"""

from __future__ import annotations

import json
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
from ctrec.schemas import (ClarifyingQuestion, LLMAssessment, MatcherOutput, QuestionSet)
from ctrec.web import app as web

XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'
LONG = "매우긴단어" * 60  # 줄바꿈 없는 긴 문자열


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve():
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return srv, t, f"http://127.0.0.1:{port}"


@pytest.fixture()
def make_server(monkeypatch, tmp_path):
    started = []

    def make(fake=None):
        fake = h.install(monkeypatch, tmp_path, fake)
        srv, t, base = _serve()
        started.append((srv, t))
        return base, fake
    yield make
    for srv, t in started:
        srv.should_exit = True
        t.join(5)


@pytest.fixture()
def server(make_server):
    return make_server()


@pytest.fixture()
def sdk_server(monkeypatch, tmp_path):
    sdk = h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    srv, t, base = _serve()
    yield base, sdk
    srv.should_exit = True
    t.join(5)


def _browser(p):
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel, headless=True)
        except Exception:  # noqa: BLE001 - 설치된 브라우저를 차례로 시도
            continue
    pytest.skip("Edge/Chrome을 찾을 수 없음")


def _page(browser, base, errors=None, **ctx):
    """CDN 등 외부 출처 요청은 모두 막고 기록 (page.external). CSP 위반 콘솔 메시지도 기록 (page.csp)."""
    page = browser.new_context(**ctx).new_page()
    page.external, page.csp = [], []

    def route(r):
        if r.request.url.startswith(base):
            return r.continue_()
        page.external.append(r.request.url)
        return r.abort()
    page.route("**/*", route)
    page.on("console", lambda m: page.csp.append(m.text) if "Content Security Policy" in m.text else None)
    if errors is not None:
        page.on("pageerror", lambda e: errors.append(str(e)))
    return page


def _assert_clean(page):
    """외부 자원 요청 없음(로컬 자원만으로 동작), CSP 위반 없음."""
    assert page.external == [], page.external
    assert page.csp == [], page.csp


def _fill_and_start(page, base, path="", text=h.SYN_TEXT, trials=h.TRIAL_IDS):
    """직접 입력 -> 고급 설정에서 시험 직접 선택 -> 분석 시작 (확인 단계 없음)."""
    page.goto(base + path)
    page.wait_for_selector("#patient option", state="attached")
    page.click("#to-custom")
    page.fill("#custom", text)
    page.click("#advanced > summary")
    page.check('input[name="cand-mode"][value="given"]')
    page.evaluate("ids => { document.querySelectorAll('#trials input').forEach(i => i.checked = ids.includes(i.value));"
                  " document.querySelector('#trials').dispatchEvent(new Event('change')); }", trials)
    page.click("#start")


def _answer(page, value=h.SYN_ANSWER, qid="Q1"):
    page.wait_for_selector(f"#qslot input[data-q='{qid}']", timeout=20000)
    page.fill(f"#qslot input[data-q='{qid}']", value)
    page.click("#send")
    page.wait_for_selector("#confirm-send", state="visible")
    page.click("#confirm-send")


def _stage(page, key):
    return page.get_attribute(f"#stage-{key}", "data-state")


def _wait_js(page, expr, arg=None, timeout=20000):
    """페이지 조건을 기다림. CSP(script-src 'self')가 페이지 안 eval을 막으므로 Playwright wait_for_function(문자열 술어를
    페이지에서 eval) 대신, 개발자 도구 프로토콜로 평가하는 page.evaluate를 짧게 반복."""
    end = time.time() + timeout / 1000
    while True:
        if page.evaluate(expr, arg):
            return
        if time.time() > end:
            raise AssertionError(f"조건이 충족되지 않음: {expr}")
        time.sleep(0.05)


def _wait_end(page, label="완료"):
    """결과(result) 이벤트 뒤에 오는 종료(end) 이벤트까지 기다림 (결과 표시와 종료 표시는 서로 다른 이벤트)."""
    _wait_js(page, "t => document.querySelector('#run-state').textContent === t", label, 15000)


def _no_hscroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")


# ---------------------------------------------------------------- 1. 입력 -> 분석 -> 질문 -> 답변 -> 재분석 -> 결과


def test_full_journey_with_evidence(server):
    base, fake = server
    errors, console = [], []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        page.on("console", lambda m: console.append(m.text))
        page.goto(base)
        page.wait_for_selector("#patient option", state="attached")
        assert page.locator("#trials input").count() == 26
        assert page.is_visible("#intro") and page.is_visible("#privacy-note")
        assert page.get_attribute("#to-custom", "aria-selected") == "true"
        _fill_and_start(page, base)
        page.wait_for_selector("#qslot .q", timeout=20000)
        # 분석 중 입력 변경 방지, 진행 단계는 서버 이벤트로만
        assert page.is_disabled("#custom") and page.is_disabled("#start")
        assert _stage(page, "profile") == "done" and _stage(page, "criteria") == "done" and _stage(page, "questions") == "active"
        assert page.inner_text("#run-state") == "답변 대기"
        assert "%" not in page.inner_text("#progress-panel")  # 임의 진행률 없음
        _answer(page)
        page.wait_for_selector("#final .rec", timeout=30000)
        final = page.inner_text("#final")
        assert "NCT04063540" in final and "추가 정보 필요" in final and "의사 확인 필요" in final
        assert "참여 자격을 확정하지 않습니다" in final
        assert page.locator("#final a[href='https://clinicaltrials.gov/study/NCT04063540']").count() >= 1
        rel = page.get_attribute("#final a[href='https://clinicaltrials.gov/study/NCT04063540']", "rel")
        assert "noopener" in rel and "noreferrer" in rel
        # 조건별 근거: 서버 판정 필드로만 묶음 (정보 부족 / 추가 확인), AI 판정 표시
        page.click("#final .rec details.more > summary")
        detail = page.inner_text("#final .rec details.more")
        assert "정보가 부족한 조건" in detail and "AI 판정 · 검증된 사실 아님" in detail
        _wait_end(page)
        for k in ("prep", "profile", "criteria", "questions", "reanalysis", "recommend", "done"):
            assert _stage(page, k) == "done", k
        assert page.locator("#cards .card").count() == 2
        # 개인정보: 브라우저 저장소에는 실행 ID만, URL에 환자 정보 없음
        storage = page.evaluate("JSON.stringify({l: {...localStorage}, s: {...sessionStorage}})")
        assert h.SYN_TEXT not in storage and h.SYN_ANSWER not in storage and "ctrec-run-id" in storage
        assert page.evaluate("localStorage.length") == 0
        from urllib.parse import unquote, urlparse
        u = urlparse(page.url)  # 포트 번호는 무작위라 숫자 비교 대신 경로·질의만 확인
        assert u.path == "/" and u.query == "" and u.fragment == ""
        assert not any(x in unquote(page.url) for x in (h.SYN_TEXT, h.SYN_ANSWER, "58세", "합성"))
        # 새 분석 시작
        page.click("#new-run")
        assert page.is_visible("#input-panel") and not page.is_disabled("#custom") and page.is_hidden("#progress-panel")
        browser.close()
    assert errors == [], errors
    assert not any(h.SYN_ANSWER in m or "58세" in m for m in console)


def test_full_journey_real_llm_code_path(sdk_server):
    """실제 코드 경로: LLM 함수를 바꾸지 않고 기본 SDK 클라이언트 자리만 대역 (llm.create·기록기 포함)."""
    base, sdk = sdk_server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        _answer(page)
        page.wait_for_selector("#final .rec", timeout=30000)
        assert "NCT04063540" in page.inner_text("#final")
        _wait_end(page)  # 계측 요약은 종료 시점에 확정
        rid = page.evaluate("sessionStorage.getItem('ctrec-run-id')")
        m = page.evaluate("async id => (await fetch(`/api/runs/${id}/metering`)).json()", rid)
        assert m["status"] == "FULLY_METERED" and m["calls"] == len(sdk.requests) and m["cost_usd"] == "UNKNOWN"
        browser.close()
    assert errors == [], errors


# ---------------------------------------------------------------- 2. 질문 없이 바로 결과


class NoQuestionsLLM(h.FakeLLM):
    def structured(self, model_cls, *, system, user, **k):
        if model_cls is QuestionSet:
            self.calls.append("QuestionSet")
            return QuestionSet(questions=[])
        return super().structured(model_cls, system=system, user=user, **k)


def test_no_questions_goes_straight_to_result(make_server):
    base, _ = make_server(NoQuestionsLLM())
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        page.wait_for_selector("#final .rec", timeout=30000)
        assert page.locator("#qslot .q").count() == 0
        _wait_end(page)
        assert _stage(page, "questions") == "skipped" and _stage(page, "reanalysis") == "skipped"
        assert _stage(page, "done") == "done"
        browser.close()
    assert errors == [], errors


# ---------------------------------------------------------------- 3·4. 모름 답변, 제출 실패 후 재시도


def test_unknown_answer_and_retry_after_failed_submit(server):
    base, _ = server
    errors, failed = [], []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)

        def flaky(route):
            if not failed:  # 첫 제출만 서버 오류로 흉내
                failed.append(1)
                return route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "일시 오류"}))
            return route.continue_()
        page.route("**/answers", flaky)
        _fill_and_start(page, base)
        page.wait_for_selector("#qslot .q", timeout=20000)
        page.check("#qslot input[data-unknown='Q1']")
        assert page.is_disabled("#qslot input[data-q='Q1']")
        page.click("#send")
        page.wait_for_selector("#confirm-send", state="visible")
        assert "모름 · 확인 불가" in page.inner_text("#qreview")
        page.click("#confirm-send")
        _wait_js(page, "() => document.querySelector('#qerror').textContent.includes('보내지 못했습니다')")
        assert page.inner_text("#confirm-send") == "다시 보내기" and not page.is_disabled("#confirm-send")
        rid = page.evaluate("sessionStorage.getItem('ctrec-run-id')")
        page.click("#confirm-send")  # 재시도
        page.wait_for_selector("#final .rec", timeout=30000)
        assert web.RUNS[rid].answers == {"Q1": "모름"}  # 서버가 지원하는 표준 '모름'만 보냄
        browser.close()
    assert errors == [], errors and failed == [1]


# ---------------------------------------------------------------- 5. 분석 중 취소


def test_cancel_during_analysis(make_server):
    fake = h.FakeLLM(block_on="MatcherOutput")
    base, _ = make_server(fake)
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        assert fake.entered.wait(20)  # 조건 비교 단계에서 멈춰 있음
        _wait_js(page, "() => document.querySelector('#stage-criteria').dataset.state === 'active'")
        page.click("#cancel")
        _wait_js(page, "() => document.querySelector('#run-state').textContent === '취소 중'")
        fake.release.set()
        _wait_js(page, "() => document.querySelector('#run-state').textContent === '취소됨'", None, 15000)
        assert _stage(page, "criteria") == "failed" and page.is_disabled('#pages [data-page="result"]')
        assert "취소" in page.inner_text("#end-note") and not page.is_disabled("#custom")
        browser.close()
    assert errors == [], errors


# ---------------------------------------------------------------- 6. SSE 끊김 후 복구


def test_sse_drop_and_recovery_without_duplicates(server):
    base, _ = server
    errors, first = [], []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)

        def first_stream_fails(route):
            if not first:  # 첫 이벤트 연결은 503 -> 브라우저가 스스로 다시 붙지 않음 -> 화면이 직접 복구
                first.append(1)
                return route.fulfill(status=503, body="")
            return route.continue_()
        page.route("**/events*", first_stream_fails)
        _fill_and_start(page, base)
        page.wait_for_selector("#qslot .q", timeout=20000)
        # 진행 중 연결 끊김 흉내: 열린 연결을 닫고 오류 처리 실행 -> ?after=<마지막 id>로 이어 받음
        page.evaluate("() => { const es = state.es; es.close(); es.onerror(new Event('error')); }")
        _wait_js(page, "() => state.es && state.es.readyState === EventSource.OPEN", None, 15000)
        assert page.locator("#qslot .q").count() == 1 and page.locator("#cards .card").count() == 2  # 중복 없음
        _answer(page)
        page.wait_for_selector("#final .rec", timeout=30000)
        assert page.locator("#cards .card").count() == 2
        assert page.evaluate("state.reconnects") >= 2 and page.is_hidden("#conn-note")
        browser.close()
    assert errors == [], errors and first == [1]


# ---------------------------------------------------------------- 7. 새로고침 후 상태 복구 + 12. 다른 브라우저 차단 + 취소


def test_refresh_restores_and_other_profile_blocked(server):
    base, _ = server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        page.wait_for_selector("#qslot input[data-q='Q1']", timeout=20000)
        run_id = page.evaluate("sessionStorage.getItem('ctrec-run-id')")
        assert run_id and page.evaluate("document.cookie") == ""  # 소유권 토큰은 HttpOnly
        page.reload()  # 같은 탭: 질문 단계 복구, 입력 원문은 다시 표시하지 않음
        page.wait_for_selector("#qslot input[data-q='Q1']", timeout=20000)
        assert page.is_visible("#cancel") and page.input_value("#custom") == ""
        assert "저장하지 않아" in page.inner_text("#input-error")
        # 다른 프로필(다른 브라우저와 같음): 실행 ID를 알아도 접근 불가
        stranger = _page(browser, base)
        stranger.goto(base)
        st = stranger.evaluate("""async id => [(await fetch(`/api/runs/${id}/events`)).status, (await fetch(`/api/runs/${id}`)).status,
            (await fetch(`/api/runs/${id}/metering`)).status, (await fetch(`/api/runs/${id}/answers`, {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CTREC-Request': '1'}, body: JSON.stringify({answers: {Q1: 'x'}})})).status]""", run_id)
        assert st == [404, 404, 404, 404]
        # 답변 후 결과까지, 그다음 새로고침하면 결과가 다시 보임
        _answer(page)
        page.wait_for_selector("#final .rec", timeout=30000)
        page.reload()
        page.wait_for_selector("#final .rec", timeout=20000)
        _wait_end(page)
        assert page.is_visible("#page-result")
        browser.close()
    assert errors == [], errors


def test_cancel_while_waiting_for_answers(server):
    base, _ = server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        page.wait_for_selector("#qslot input[data-q='Q1']", timeout=20000)
        page.click("#cancel")
        _wait_js(page, "() => document.querySelector('#run-state').textContent === '취소됨'", None, 10000)
        assert page.is_hidden("#cancel") and page.inner_html("#qslot") == ""
        assert page.evaluate("sessionStorage.getItem('ctrec-run-id')") is None
        browser.close()
    assert errors == [], errors


# ---------------------------------------------------------------- 8. 실행 시간 초과


def test_run_timeout(make_server, monkeypatch):
    monkeypatch.setattr(web, "RUN_TIMEOUT_S", 3)
    base, _ = make_server()
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        _wait_js(page, "() => document.querySelector('#run-state').textContent === '시간 초과'", None, 20000)
        assert "시간 제한" in page.inner_text("#end-note") and page.is_disabled('#pages [data-page="result"]')
        browser.close()
    assert errors == [], errors


# ---------------------------------------------------------------- 9. 추천 결과 없음


class MismatchLLM(h.FakeLLM):
    def structured(self, model_cls, *, system, user, **k):
        if model_cls is MatcherOutput:  # 모든 LLM 판정 기준을 불충족으로, 근거는 합성 원문 인용 (근거 없는 판정은 Matcher가 받지 않음)
            self.calls.append("MatcherOutput")
            rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
            return MatcherOutput(assessments=[LLMAssessment(
                rule_id=r["rule_id"], applicable=True, holds="no" if r.get("kind") != "exclusion" else "yes",
                evidence_type="explicit", confidence="high", evidence=h.SYN_TEXT, reasoning="합성", missing_info=None)
                for r in rules])
        if model_cls is QuestionSet:
            self.calls.append("QuestionSet")
            return QuestionSet(questions=[])
        return super().structured(model_cls, system=system, user=user, **k)


def test_no_recommendation(make_server):
    base, _ = make_server(MismatchLLM())
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        page.wait_for_selector("#page-result:not([hidden])", timeout=30000)
        final = page.inner_text("#final")
        assert page.locator("#final .rec").count() == 0
        assert page.locator("#final .callout").count() == 1 or "추천할 임상시험이 없습니다" in final
        assert "공개 기준 불충족" in final or "제외" in final
        browser.close()
    assert errors == [], errors


# ---------------------------------------------------------------- 10. 긴 제목·조건, 특수문자 + 11. 모바일


class HostileTextLLM(h.FakeLLM):
    def structured(self, model_cls, *, system, user, **k):
        if model_cls is QuestionSet:
            self.calls.append("QuestionSet")
            return QuestionSet(questions=[
                ClarifyingQuestion(question_id="Q1", question=f"검사 결과가 있나요? {XSS}", answer_type="yes_no", target=[],
                                   purpose="eligibility", why=f"이유 {LONG}"),
                ClarifyingQuestion(question_id="Q2", question="최근 크레아티닌 수치는?", answer_type="number", target=[],
                                   purpose="eligibility", why="신장 기능 기준"),
                ClarifyingQuestion(question_id="Q3", question=f"기타 <b>메모</b> {LONG}", answer_type="free_text", target=[],
                                   purpose="physician_reference", why="참고")])
        if model_cls is MatcherOutput:
            out = super().structured(model_cls, system=system, user=user, **k)
            for a in out.assessments:
                a.evidence = f"{XSS} {LONG}"
            return out
        return super().structured(model_cls, system=system, user=user, **k)


@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}], ids=["desktop", "mobile"])
def test_long_and_special_text_render_safely(make_server, viewport):
    base, _ = make_server(HostileTextLLM())
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors, viewport=viewport)

        def long_titles(route):  # 화면이 받는 시험 목록의 제목만 길고 위험한 문자열로 (서버 시험 데이터·고정 저장소는 그대로)
            r = route.fetch()
            trials = r.json()
            for t in trials:
                if t["trial_id"] in h.TRIAL_IDS:
                    t["title"] = f"{XSS} 아주 긴 임상시험 제목 {LONG}"
            route.fulfill(response=r, body=json.dumps(trials, ensure_ascii=False))
        page.route("**/api/trials", long_titles)
        _fill_and_start(page, base)
        page.wait_for_selector("#qslot .q", timeout=20000)
        assert page.locator("#qslot .q").count() == 3  # 여러 질문을 순서대로
        assert page.locator("#qslot input[data-yn='Q1']").count() == 2  # 예/아니요
        assert page.get_attribute("#qslot input[data-q='Q2']", "inputmode") == "decimal"
        assert XSS in page.inner_text("#qslot")  # 글자로만 표시
        assert XSS in page.inner_text("#cards")  # 긴 제목도 글자로만
        assert _no_hscroll(page)
        page.check("#qslot input[data-yn='Q1'][value='예']")
        page.fill("#qslot input[data-q='Q2']", "1.1 mg/dL")
        page.click("#send")
        page.click("#confirm-send")
        page.wait_for_selector("#page-result:not([hidden])", timeout=30000)
        for d in page.locator("#final details").all():
            d.evaluate("el => el.open = true")
        assert page.evaluate("window.__xss") is None  # 질문·근거·제목의 HTML이 실행되지 않음
        assert page.locator("#final img, #qslot img, #final script").count() == 0
        assert XSS in page.inner_text("#final")
        assert _no_hscroll(page)
        if viewport["width"] < 500:  # 모바일: 주요 버튼이 화면 안에 보임
            assert page.is_visible("#new-run")
            box = page.locator("#new-run").bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"]
        browser.close()
    assert errors == [], errors


# ---------------------------------------------------------------- 입력 오류 표시, 중복 제출 방지, 서버 오류 메시지


def test_input_errors_and_duplicate_submit(server):
    base, fake = server
    errors, posts = [], []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/api/runs") else None)
        page.goto(base)
        page.wait_for_selector("#patient option", state="attached")
        page.click("#start")  # 빈 입력: 화면에서 막음
        assert page.inner_text("#input-error") == "환자 정보를 입력하세요." and not posts
        page.fill("#custom", "x" * 30000)  # 한도(서버 MAX_TEXT_CHARS와 같은 20,000자)를 넘는 입력은 화면에서 막음
        n = len(page.input_value("#custom"))
        if n > 20000:
            page.click("#start")
            assert "20,000자 이하" in page.inner_text("#input-error") and not posts
        else:
            assert n == 20000 and "20,000 / 20,000" in page.inner_text("#char-count")
        # 서버가 거절하면 서버의 문자열 메시지를 그대로 보여 줌
        page.route("**/api/runs", lambda route: route.fulfill(status=429, content_type="application/json",
                                                              body=json.dumps({"detail": "이 브라우저에서 진행 중인 실행이 있습니다."})))
        page.fill("#custom", h.SYN_TEXT)
        page.click("#start")
        _wait_js(page, "() => document.querySelector('#start-hint').textContent.includes('진행 중인 실행')")
        assert not page.is_disabled("#custom")
        page.unroute("**/api/runs")
        # 중복 제출: 빠르게 두 번 눌러도 실행 요청은 한 번
        page.evaluate("() => { document.querySelector('#start').click(); document.querySelector('#start').click(); }")
        page.wait_for_selector("#qslot .q", timeout=20000)
        assert sum(1 for u in posts if u.endswith("/api/runs")) == 2  # 거절된 1회 + 실제 1회
        browser.close()
    assert errors == [], errors


def test_decision_diagnostic_panel_only_with_flag(server):
    """개발자 진단 패널: 기본 숨김, ?diagnostic=1일 때만 진단 이벤트를 따로 표시하고 추천 화면은 그대로."""
    base, _ = server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        page.goto(base)
        page.wait_for_selector("#patient option", state="attached")
        assert page.is_hidden("#decision-panel")
        _fill_and_start(page, base, "/?diagnostic=1")
        page.wait_for_selector("#decision-body .card", state="attached", timeout=20000)
        body = page.text_content("#decision-body")
        assert "라운드 0" in body and "ASK_USER" in body
        _answer(page)
        page.wait_for_selector("#final .rec", timeout=30000)
        assert page.locator("#decision-body .card").count() == 2 and "NCT04063540" in page.inner_text("#final")
        browser.close()
    assert errors == [], errors


# =============================================================== Phase 6-2: 결과 문구·CSP·입력 검증·접근성

CODES = ("ELIGIBLE|INELIGIBLE|UNCERTAIN|NOT_EVALUATED|UNVERIFIED|CRITERIA_MATCH|POTENTIAL_MATCH|CRITERIA_MISMATCH|"
         "INSUFFICIENT_INFORMATION|NEEDS_REVIEW|ACTIVE_NOT_RECRUITING")
# 결과 화면에서 사용자에게 보이는 글(접힌 '시스템 원문'과 AI 보고서 제외)에 내부 표기가 있는지
VISIBLE_CODE_JS = """codes => { const re = new RegExp('\\\\b(' + codes + ')\\\\b');
  const root = document.querySelector('#final').cloneNode(true);
  root.querySelectorAll('details.raw, details.report').forEach(d => d.remove());
  const t = root.textContent; const m = t.match(re);
  return m ? m[0] + ' @ ' + t.slice(Math.max(0, m.index - 40), m.index + 40) : null; }"""
CONTRAST_JS = r"""() => {
  const parse = c => { const m = /rgba?\(([^)]+)\)/.exec(c); if (!m) return null; const p = m[1].split(',').map(parseFloat);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  const lum = ({ r, g, b }) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const mix = (top, base) => ({ r: top.r * top.a + base.r * (1 - top.a), g: top.g * top.a + base.g * (1 - top.a), b: top.b * top.a + base.b * (1 - top.a) });
  const bgOf = el => { const layers = []; for (let cur = el; cur && cur.nodeType === 1; cur = cur.parentElement) {
      const c = parse(getComputedStyle(cur).backgroundColor); if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; } }
    let base = { r: 255, g: 255, b: 255 }; for (let i = layers.length - 1; i >= 0; i--) base = mix(layers[i], base); return base; };
  const bad = [], seen = new Set(); const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const t = w.currentNode; if (!t.textContent.trim()) continue; const el = t.parentElement;
    if (!el || seen.has(el)) continue; seen.add(el);
    if (!el.getClientRects().length || el.closest('[disabled], .off, svg, option, .sr-only, [hidden]')) continue;
    const cs = getComputedStyle(el); if (cs.visibility === 'hidden') continue;
    const fg = parse(cs.color); if (!fg) continue; const bg = bgOf(el); const f = mix(fg, bg);
    const L1 = lum(f), L2 = lum(bg); const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const size = parseFloat(cs.fontSize), wt = parseInt(cs.fontWeight, 10); const large = size >= 24 || (size >= 18.66 && wt >= 700);
    if (ratio < (large ? 3 : 4.5)) bad.push(el.tagName + '.' + el.className + ' ' + ratio.toFixed(2) + ' "' + t.textContent.trim().slice(0, 24) + '"'); }
  return bad; }"""
NAMES_JS = r"""() => [...document.querySelectorAll('input, select, textarea, button, a[href], summary')].filter(el => el.getClientRects().length)
  .filter(el => { if (el.type === 'hidden') return false;
    const lab = el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    const by = (el.getAttribute('aria-labelledby') || '').split(' ').map(i => document.getElementById(i)).filter(Boolean).map(e => e.textContent).join('');
    const name = (el.getAttribute('aria-label') || by || (lab && lab.textContent) || (el.closest('label') && el.closest('label').textContent) || el.title || el.textContent || '').trim();
    return !name; }).map(el => el.outerHTML.slice(0, 80))"""


def _check_screen(page, label):
    """한 화면 점검: 가로 스크롤 없음, 글자 대비(WCAG AA), 조작 요소의 접근 가능한 이름."""
    assert _no_hscroll(page), f"{label}: 가로 스크롤"
    bad = page.evaluate(CONTRAST_JS)
    assert bad == [], (label, bad)
    unnamed = page.evaluate(NAMES_JS)
    assert unnamed == [], (label, unnamed)


def _open_details(page, root="#final"):
    page.evaluate("r => document.querySelectorAll(r + ' details').forEach(d => { d.open = true; })", root)


def _visible_code(page):
    return page.evaluate(VISIBLE_CODE_JS, CODES)


def test_csp_headers_local_assets_and_no_violations(server):
    base, _ = server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        resp = page.goto(base)
        csp = resp.headers.get("content-security-policy", "")
        for d in ("script-src 'self'", "style-src 'self'", "connect-src 'self'", "object-src 'none'", "base-uri 'none'",
                  "frame-ancestors 'none'", "default-src 'none'"):
            assert d in csp, d
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert resp.headers.get("x-content-type-options") == "nosniff"
        page.wait_for_selector("#patient option", state="attached")
        assert page.evaluate("() => typeof marked === 'object' && typeof DOMPurify === 'function'")  # 로컬 라이브러리
        _fill_and_start(page, base)
        _answer(page)
        page.wait_for_selector("#final .rec", timeout=30000)
        page.evaluate("() => { document.querySelector('#final details.report').open = true; }")
        assert page.locator("#final .md h1").count() >= 1  # 보고서 마크다운이 CDN 없이 렌더링
        _assert_clean(page)
        browser.close()
    assert errors == [], errors


class ReportXSSLLM(h.FakeLLM):
    def plain(self, *, system, user, **k):
        text = super().plain(system=system, user=user, **k)  # 호출 기록 유지
        return (text + "\n\n<script>window.__xss=3</script><img src=x onerror=\"window.__xss=4\">\n\n"
                "[링크](javascript:window.__xss=5) <style>body{display:none}</style> <div style=\"color:red\">빨강</div>"
                "\n\n<iframe src=\"https://example.com\"></iframe>")


def test_report_xss_sanitized_under_csp(make_server):
    base, _ = make_server(ReportXSSLLM())
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        _answer(page)
        page.wait_for_selector("#final .rec", timeout=30000)
        page.evaluate("() => { document.querySelector('#final details.report').open = true; }")
        md = page.locator("#final .md")
        assert md.locator("script, style, iframe, img, [style], a[href^='javascript']").count() == 0
        assert "window.__xss=3" in md.inner_text()  # 보고서 속 HTML은 글자로만 표시
        assert page.evaluate("() => window.__xss") is None and page.is_visible("body")
        _assert_clean(page)
        browser.close()
    assert errors == [], errors


def test_result_text_without_internal_codes_and_originals_kept(make_server):
    base, _ = make_server(MismatchLLM())
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        page.wait_for_selector("#page-result:not([hidden])", timeout=30000)
        _open_details(page)
        assert _visible_code(page) is None  # 사용자용 글에 내부 표기 없음
        final = page.inner_text("#final")
        assert "현재 추천할 수 있는 임상시험이 없습니다" in final and "맞지 않는 조건이 있어 추천에서 제외" in final
        assert "가상 데모 시험이라 추천에서 제외" in final and "충족하지 않은 조건" in final
        raws = page.evaluate("() => [...document.querySelectorAll('#final details.raw')].map(d => d.textContent)")
        assert any("INELIGIBLE" in r for r in raws)  # 원문은 지우지 않고 접어서 보존
        rid = page.evaluate("() => sessionStorage.getItem('ctrec-run-id')")
        ev = next(e for e in web.RUNS[rid].events if e["type"] == "result")
        for tid, m in ev["result"]["matches"].items():  # 판정 상태는 서버 값 그대로 표시
            assert m["match_status"] == "CRITERIA_MISMATCH"
            card = page.locator("#final .card").filter(has=page.locator(".tid", has_text=tid)).first
            assert "공개 기준 불충족" in card.inner_text()
        _assert_clean(page)
        browser.close()
    assert errors == [], errors


def test_insufficient_information_distinct_from_mismatch(server):
    base, _ = server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        _answer(page)
        page.wait_for_selector("#final .rec", timeout=30000)
        _open_details(page)
        rec = page.inner_text("#final .rec >> nth=0")
        assert "추가 정보 필요" in rec and "불충족이라는 뜻이 아닙니다" in rec
        assert "정보가 부족한 조건" in rec and "충족하지 않은 조건" not in rec and "공개 기준 불충족" not in rec
        assert "AI 판정 · 검증된 사실 아님" in rec and "의사 확인 필요" in rec
        assert _visible_code(page) is None
        # AI 글에 내부 표기가 섞이면 중립 안내 + 원문 접기 (글자를 바꾸지 않음)
        html = page.evaluate("() => aiText('AI 요약', '이 시험은 UNCERTAIN 상태입니다')")
        assert "접어 두었습니다" in html and "details" in html and "UNCERTAIN" in html
        assert "접어 두었습니다" not in page.evaluate("() => aiText('AI 요약', '확인이 필요합니다')")
        browser.close()
    assert errors == [], errors


SECRET = "비밀-환자-문장-요청내용"


def test_evaluation_failure_hides_error_detail(make_server):
    base, _ = make_server(h.FakeLLM(fail_on="MatcherOutput", fail_message=SECRET))
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        page.wait_for_selector("#page-result:not([hidden])", timeout=30000)
        _open_details(page)
        final = page.inner_text("#final")
        assert "평가 실패" in final and "부적격이라는 뜻이 아니" in final
        assert SECRET not in page.content()
        rid = page.evaluate("() => sessionStorage.getItem('ctrec-run-id')")
        sent = json.dumps([e for e in web.RUNS[rid].events if e["type"] == "result"], ensure_ascii=False)
        assert SECRET not in sent and "RuntimeError" in sent  # 화면으로 보낸 결과에는 오류 종류만
        browser.close()
    assert errors == [], errors


class FormatLLM(h.FakeLLM):
    def structured(self, model_cls, *, system, user, **k):
        if model_cls is QuestionSet:
            self.calls.append("QuestionSet")

            def mk(i, t, q):
                return ClarifyingQuestion(question_id=i, question=q, answer_type=t, target=[], purpose="eligibility", why="합성")
            return QuestionSet(questions=[mk("Q1", "yes_no", "뇌 전이가 있나요?"), mk("Q2", "number", "최근 크레아티닌 수치는?"),
                                          mk("Q3", "date", "마지막 항암 날짜는?"), mk("Q4", "free_text", "기타 참고 사항")])
        return super().structured(model_cls, system=system, user=user, **k)


def test_question_format_validation_and_unknown(make_server):
    base, _ = make_server(FormatLLM())
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        page.wait_for_selector("#qslot input[data-q='Q2']", timeout=20000)
        assert page.get_attribute("#qslot input[data-q='Q2']", "inputmode") == "decimal"
        assert page.get_attribute("#qslot input[data-q='Q3']", "inputmode") == "numeric"
        page.fill("#qslot input[data-q='Q2']", "높음")
        page.fill("#qslot input[data-q='Q3']", "2026-02-30")  # 달력에 없는 날짜
        page.click("#send")
        assert page.is_hidden("#qreview") and "형식이 맞지 않는" in page.inner_text("#qerror")
        assert page.get_attribute("#qslot input[data-q='Q2']", "aria-invalid") == "true"
        assert page.get_attribute("#qslot input[data-q='Q3']", "aria-invalid") == "true"
        assert page.evaluate("() => document.activeElement.dataset.q") == "Q2"  # 첫 오류로 포커스
        assert page.inner_text("#qerr-Q2") and page.inner_text("#qerr-Q3")
        page.fill("#qslot input[data-q='Q2']", "1.2 mg/dL")
        page.fill("#qslot input[data-q='Q3']", "2026-02")
        page.fill("#qslot input[data-q='Q4']", "모름")  # 직접 쓴 '모름'도 표준 미응답으로
        page.click("#send")
        page.wait_for_selector("#confirm-send", state="visible")
        review = page.inner_text("#qreview")
        assert "1.2 mg/dL" in review and "2026-02" in review and review.count("모름 · 확인 불가") == 2  # Q1 빈칸, Q4
        rid = page.evaluate("() => sessionStorage.getItem('ctrec-run-id')")
        page.click("#confirm-send")
        page.wait_for_selector("#page-result:not([hidden])", timeout=30000)
        assert web.RUNS[rid].answers == {"Q1": "모름", "Q2": "1.2 mg/dL", "Q3": "2026-02", "Q4": "모름"}
        browser.close()
    assert errors == [], errors


def _tab_to(page, css, limit=150):
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate("s => !!document.activeElement && document.activeElement.matches(s)", css):
            return
    raise AssertionError(f"키보드로 도달하지 못함: {css}")


def test_keyboard_only_journey(server):
    """마우스 없이 Tab·Enter·입력만으로 입력 -> 분석 -> 질문·답변 -> 결과 -> 근거 -> 새 분석."""
    base, _ = server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        page.goto(base)
        page.wait_for_selector("#patient option", state="attached")
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.className") == "skip"  # 본문 건너뛰기 링크가 처음
        _tab_to(page, "#custom")
        page.keyboard.type(h.SYN_TEXT)
        _tab_to(page, "#start")
        page.keyboard.press("Enter")
        page.wait_for_selector("#qslot input[data-q='Q1']", timeout=30000)
        assert page.evaluate("() => document.activeElement.id") == "q-h"  # 질문 제목으로 포커스
        _tab_to(page, "#qslot input[data-q='Q1']")
        page.keyboard.type(h.SYN_ANSWER)
        _tab_to(page, "#send")
        page.keyboard.press("Enter")
        assert page.evaluate("() => document.activeElement.id") == "confirm-send"
        page.keyboard.press("Enter")
        page.wait_for_selector("#final .rec", timeout=30000)
        assert page.evaluate("() => document.activeElement.id") == "result-h"  # 결과 제목으로 포커스
        _tab_to(page, "#final .rec details.more > summary")
        page.keyboard.press("Enter")
        assert page.evaluate("() => document.querySelector('#final .rec details.more').open")
        _tab_to(page, "#new-run")
        page.keyboard.press("Enter")
        assert page.evaluate("() => document.activeElement.id") == "custom" and page.is_visible("#input-panel")
        browser.close()
    assert errors == [], errors


@pytest.mark.parametrize("ctx", [
    {"viewport": {"width": 320, "height": 720}},
    {"viewport": {"width": 390, "height": 844}, "color_scheme": "dark"},
    {"viewport": {"width": 640, "height": 480}},  # 1280px 화면 200% 확대와 같은 CSS 폭
    {"viewport": {"width": 1280, "height": 900}, "color_scheme": "dark"},
], ids=["320", "390-dark", "zoom200", "desktop-dark"])
def test_responsive_contrast_and_names_each_screen(make_server, ctx):
    base, _ = make_server(FormatLLM())
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors, **ctx)
        page.goto(base)
        page.wait_for_selector("#patient option", state="attached")
        _check_screen(page, "초기 입력")
        page.click("#start")
        _check_screen(page, "입력 오류")
        page.fill("#custom", h.SYN_TEXT)
        page.click("#start")
        page.wait_for_selector("#qslot input[data-q='Q2']", timeout=30000)
        _check_screen(page, "분석 진행·질문")
        page.fill("#qslot input[data-q='Q2']", "높음")
        page.click("#send")
        _check_screen(page, "질문 형식 오류")
        page.fill("#qslot input[data-q='Q2']", "1.2")
        page.click("#send")
        _check_screen(page, "답변 확인")
        page.click("#confirm-send")
        page.wait_for_selector("#page-result:not([hidden])", timeout=30000)
        _open_details(page)
        _check_screen(page, "결과·조건별 근거")
        page.click("#new-run")
        _check_screen(page, "새 분석")
        _assert_clean(page)
        browser.close()
    assert errors == [], errors


def test_end_state_screen_accessible(make_server, monkeypatch):
    monkeypatch.setattr(web, "RUN_TIMEOUT_S", 3)
    base, _ = make_server()
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        for scheme in ("light", "dark"):
            page = _page(browser, base, errors, color_scheme=scheme, viewport={"width": 390, "height": 844})
            _fill_and_start(page, base)
            _wait_js(page, "() => document.querySelector('#run-state').textContent === '시간 초과'", None, 20000)
            assert page.evaluate("() => document.activeElement.id") == "end-note"  # 종료 사유로 포커스
            _check_screen(page, f"시간 초과 ({scheme})")
            page.context.close()
        browser.close()
    assert errors == [], errors


# =============================================================== Phase 6-3: 비용 통제(enforce) 화면 안내


def test_budget_exceeded_shown_and_not_completed(monkeypatch, tmp_path):
    """enforce 모드에서 실행별 호출 한도에 걸리면: 완료·추천으로 보이지 않고 '비용 한도 도달' 안내, 중간 판정은 참고용으로 남김."""
    from ctrec import config
    from ctrec.web import budget
    sdk = h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    env = {"CTREC_WEB_METERING": "enforce", "CTREC_WEB_BUDGET_LEDGER": str(tmp_path / "ledger.db"),
           "CTREC_WEB_BUDGET_LEDGER_PERSISTENT": "1", "CTREC_WEB_BUDGET_SINGLE_INSTANCE": "1",
           "CTREC_WEB_RUN_MAX_USD": "10", "CTREC_WEB_RUN_MAX_CALLS": "3", "CTREC_WEB_RUN_MAX_INPUT_TOKENS": "5000000",
           "CTREC_WEB_RUN_MAX_OUTPUT_TOKENS": "2000000", "CTREC_WEB_SHARED_MAX_USD": "100", "CTREC_WEB_SHARED_MAX_CALLS": "500",
           "CTREC_WEB_SHARED_MAX_INPUT_TOKENS": "50000000", "CTREC_WEB_SHARED_MAX_OUTPUT_TOKENS": "20000000",
           "CTREC_WEB_PRICE_INPUT": "2", "CTREC_WEB_PRICE_OUTPUT": "10", "CTREC_WEB_PRICE_CACHE_WRITE": "2.5",
           "CTREC_WEB_PRICE_CACHE_READ": "0.2", "CTREC_WEB_PRICE_CONFIRMED": "1", "CTREC_WEB_PRICE_SOURCE": "test",
           "CTREC_WEB_PRICE_MODEL": config.MODEL}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    budget.reset_ledgers()
    budget.Ledger(env["CTREC_WEB_BUDGET_LEDGER"], budget.Limits(1, 1, 1, 1), "day").open(create=True)
    srv, t, base = _serve()
    errors = []
    try:
        with sync_api.sync_playwright() as p:
            browser = _browser(p)
            page = _page(browser, base, errors)
            _fill_and_start(page, base)
            _wait_js(page, "() => document.querySelector('#run-state').textContent === '비용 한도 도달'", None, 30000)
            note = page.inner_text("#end-note")
            assert "비용 한도에 도달" in note and "추천 결과가 아닙니다" in note
            assert page.is_disabled('#pages [data-page="result"]') and page.locator("#final .rec").count() == 0
            assert page.evaluate("() => document.activeElement.id") == "end-note"
            rid = page.evaluate("() => sessionStorage.getItem('ctrec-run-id')")
            assert rid is None  # 끝나지 않은 분석은 새로고침 복원 대상으로 남기지 않음 (완료만 기억)
            assert len(sdk.requests) == 3
            _assert_clean(page)
            browser.close()
    finally:
        srv.should_exit = True
        t.join(5)
        budget.reset_ledgers()
    assert errors == [], errors


# =============================================================== Phase 6-6: 탭 종료 후 복귀, 운영 시간 측정


def test_tab_closed_and_returned(server):
    """탭을 닫으면 실행 ID(sessionStorage)가 사라짐: 같은 브라우저의 새 탭은 진행 중 분석을 이어 볼 수 있고,
    새 분석은 한도(동시 1개) 안내와 함께 이어 보기를 권함. 다른 브라우저에는 알려 주지 않음."""
    base, _ = server
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        first = _page(browser, base, errors)
        _fill_and_start(first, base)
        first.wait_for_selector("#qslot .q", timeout=20000)
        ctx = first.context
        first.close()
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.wait_for_selector("#active-note", state="visible", timeout=10000)
        assert page.evaluate("sessionStorage.length") == 0 and "보안상" in page.inner_text("#active-note")
        page.click("#to-custom")
        page.fill("#custom", h.SYN_TEXT)
        page.click("#start")
        _wait_js(page, "() => document.querySelector('#start-hint').textContent.includes('진행 중인 실행')")
        assert page.is_visible("#active-note")
        page.click("#resume-active")
        page.wait_for_selector("#qslot .q", timeout=20000)
        assert page.is_hidden("#active-note") and page.evaluate("sessionStorage.getItem('ctrec-run-id')")
        _answer(page)
        page.wait_for_selector("#final .rec", timeout=30000)
        _wait_end(page)
        other = _page(browser, base, errors)
        other.goto(base)
        other.wait_for_selector("#patient option", state="attached")
        time.sleep(0.5)
        assert other.is_hidden("#active-note")
        browser.close()
    assert errors == [], errors


def test_operational_timings(make_server):
    """Fake LLM(즉시 응답) 기준 서버·화면 처리 시간. 실제 LLM 응답 시간이 아님. 결과는 CTREC_TIMING_OUT 파일에 기록."""
    fake = h.FakeLLM(block_on="MatcherOutput")
    base, _ = make_server(fake)
    errors, t = [], {}
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        # 1) 취소 처리: 조건 비교 중(대역이 멈춘 외부 호출) 취소 -> 호출이 끝나는 즉시 멈춤
        _fill_and_start(page, base)
        assert fake.entered.wait(20)
        _wait_js(page, "() => document.querySelector('#stage-criteria').dataset.state === 'active'")
        s = time.perf_counter()
        page.click("#cancel")
        _wait_js(page, "() => document.querySelector('#run-state').textContent === '취소 중'")
        t["cancel_request_ack_s"] = time.perf_counter() - s
        s = time.perf_counter()
        fake.release.set()
        _wait_js(page, "() => document.querySelector('#run-state').textContent === '취소됨'", None, 15000)
        t["cancel_after_inflight_call_s"] = time.perf_counter() - s
        # 2) 정상 흐름
        s = time.perf_counter()
        page.click("#start")
        _wait_js(page, "() => !!sessionStorage.getItem('ctrec-run-id') && document.querySelector('#run-state').textContent === '분석 중'")
        t["start_response_s"] = time.perf_counter() - s
        _wait_js(page, "() => document.querySelectorAll('#feed .ev').length > 0")
        t["first_sse_event_s"] = time.perf_counter() - s
        page.wait_for_selector("#qslot .q", timeout=20000)
        t["questions_shown_s"] = time.perf_counter() - s
        # 3) 동시 실행 제한: 같은 브라우저의 두 번째 시작은 즉시 거절
        s2 = time.perf_counter()
        st = page.evaluate("async () => (await fetch('/api/runs', {method: 'POST', headers: {'Content-Type': 'application/json',"
                           " 'X-CTREC-Request': '1'}, body: JSON.stringify({custom_text: 'x', trial_ids: ['NCT04063540']})})).status")
        t["concurrent_start_rejected_s"] = time.perf_counter() - s2
        assert st == 429
        page.wait_for_selector(f"#qslot input[data-q='Q1']")
        page.fill("#qslot input[data-q='Q1']", h.SYN_ANSWER)
        page.click("#send")
        page.wait_for_selector("#confirm-send", state="visible")
        s = time.perf_counter()
        page.click("#confirm-send")
        page.wait_for_selector("#final .rec", timeout=30000)
        t["answer_to_result_s"] = time.perf_counter() - s
        _wait_end(page)
        t["answer_to_completed_s"] = time.perf_counter() - s
        s = time.perf_counter()
        page.click('#pages [data-page="result"]')
        page.click("#final .rec details.more > summary")
        page.wait_for_selector("#final .rec details.more[open]")
        t["result_tab_and_detail_render_s"] = time.perf_counter() - s
        _assert_clean(page)
        browser.close()
    assert errors == [], errors
    t = {k: round(v, 3) for k, v in t.items()}
    print("TIMINGS", json.dumps(t, ensure_ascii=False))
    if os.getenv("CTREC_TIMING_OUT"):
        Path(os.environ["CTREC_TIMING_OUT"]).write_text(json.dumps({"basis": "Fake LLM, 로컬 서버, 헤드리스 Edge", **t},
                                                                   ensure_ascii=False, indent=1), encoding="utf-8")
    assert all(v < 15 for v in t.values()), t


def test_expired_run_after_refresh(make_server):
    """보관 기간이 지난 실행: 새로고침하면 접근 불가 안내, 실행 ID를 지우고 새 입력은 가능 (완료로 보이지 않음)."""
    base, _ = make_server(NoQuestionsLLM())
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        _fill_and_start(page, base)
        page.wait_for_selector("#final .rec", timeout=30000)
        _wait_end(page)
        web._prune_runs(now=time.time() + web.RUN_TTL_S + 1)  # 세션(실행 기록) 만료
        page.reload()
        _wait_js(page, "() => document.querySelector('#start-hint').textContent.includes('접근할 수 없습니다')")
        assert page.evaluate("sessionStorage.getItem('ctrec-run-id')") is None
        assert page.is_hidden("#progress-panel") and not page.is_disabled("#custom")
        assert page.is_disabled('#pages [data-page="result"]') and page.locator("#final .rec").count() == 0
        browser.close()
    assert errors == [], errors


# =============================================================== Phase 8: 실제 분석이 꺼진 서버 (기본)


def test_real_analysis_off_by_default_is_explained(monkeypatch):
    """하네스 대역 없이 띄운 기본 서버: API 키가 있어도 실제 분석 꺼짐 안내, 시작해도 실행·외부 요청 없음."""
    import anthropic
    from ctrec import llm
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: made.append(1) or (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(web, "TEST_LLM_BACKEND", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-browser-key-0000")
    for k in ("CTREC_WEB_PAID_ENABLED", "CTREC_WEB_METERING", "CTREC_REQUIRE_HTTPS"):
        monkeypatch.delenv(k, raising=False)
    web.RUNS.clear()
    web.CLIENT_STARTS.clear()
    srv, t, base = _serve()
    errors = []
    try:
        with sync_api.sync_playwright() as p:
            browser = _browser(p)
            page = _page(browser, base, errors)
            page.goto(base)
            page.wait_for_selector("#analysis-off", state="visible", timeout=10000)
            assert "실제 AI 분석이 꺼져" in page.inner_text("#analysis-off")
            _fill_and_start(page, base)
            _wait_js(page, "() => document.querySelector('#start-hint').textContent.includes('꺼져')")
            assert page.is_hidden("#progress-panel") and not page.is_disabled("#custom")
            assert page.evaluate("sessionStorage.getItem('ctrec-run-id')") is None
            _assert_clean(page)
            browser.close()
    finally:
        srv.should_exit = True
        t.join(5)
    assert errors == [] and web.RUNS == {} and made == [] and llm._client is None


# =============================================================== Phase 9: 팀원 기능 통합 (객관식 질문, 한국어 번역)


def test_choice_questions_and_korean_titles(make_server):
    """객관식(하나·여러 개·기타)·예/아니요·단위 숫자·모름을 화면에서 답하고, 저장값(선택지 문구)이 재분석에 반영됨.
    시험 제목·기준은 번역 파일이 있을 때 한국어로, 원문은 함께 보임."""
    from test_team_integration import ChoiceLLM
    import json as _json
    base, fake = make_server(ChoiceLLM())
    ko = _json.loads((Path(__file__).resolve().parents[1] / "data" / "i18n" / "trials_ko.json").read_text(encoding="utf-8"))
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        page = _page(browser, base, errors)
        page.goto(base)
        page.wait_for_selector("#patient option", state="attached")
        _wait_js(page, "t => [...document.querySelectorAll('#trials .ttl')].some(e => e.textContent === t)",
                 ko["NCT04063540"]["title"])
        _fill_and_start(page, base)
        page.wait_for_selector("#qslot .q", timeout=20000)
        page.check("input[data-yn='Q1'][value='아니요']")
        assert page.is_disabled("input[data-etc='Q2']")
        page.check("input[data-ch='Q2'][value='3']")  # 기타(직접 입력)
        assert not page.is_disabled("input[data-etc='Q2']")
        page.fill("input[data-etc='Q2']", "6주 전")
        page.check("input[data-mc='Q3'][value='0']")
        page.check("input[data-mc='Q3'][value='1']")  # 선택지 안에 쉼표 ("당뇨, 2형")
        assert "단위: g/dL" in page.inner_text("#qslot .q[data-qid='Q4']")
        page.fill("input[data-q='Q4']", "11.2")
        page.check("input[data-unknown='Q5']")
        page.click("#send")
        page.wait_for_selector("#confirm-send", state="visible")
        review = page.inner_text("#qreview")
        for v in ("아니요", "기타: 6주 전", "고혈압, 당뇨, 2형", "11.2 g/dL", "모름 · 확인 불가"):
            assert v in review, v
        page.click("#confirm-send")
        page.wait_for_selector("#final .rec", timeout=30000)
        _wait_end(page)
        second = fake.profiler_inputs[-1]
        for v in ("아니요", "기타: 6주 전", "고혈압, 당뇨, 2형", "11.2 g/dL"):
            assert v in second, v
        page.click("#final .rec details.more > summary")
        assert "원문:" in page.inner_text("#final")  # 번역한 제목·기준 옆에 원문
        assert "AI 요약" not in page.inner_text("#final")  # 추천 카드 줄글 설명 삭제 (팀원 결정)
        _assert_clean(page)
        browser.close()
    assert errors == [], errors
