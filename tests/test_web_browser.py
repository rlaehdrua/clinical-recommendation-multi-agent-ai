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
    page = browser.new_context(**ctx).new_page()
    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
    if errors is not None:
        page.on("pageerror", lambda e: errors.append(str(e)))
    return page


def _fill_and_start(page, base, path="", text=h.SYN_TEXT, trials=h.TRIAL_IDS):
    """직접 입력 -> 고급 설정에서 시험 직접 선택 -> 입력 내용 확인 -> 분석 시작."""
    page.goto(base + path)
    page.wait_for_selector("#patient option", state="attached")
    page.click("#to-custom")
    page.fill("#custom", text)
    page.click("#advanced > summary")
    page.check('input[name="cand-mode"][value="given"]')
    page.evaluate("ids => { document.querySelectorAll('#trials input').forEach(i => i.checked = ids.includes(i.value));"
                  " document.querySelector('#trials').dispatchEvent(new Event('change')); }", trials)
    page.click("#review")
    page.wait_for_selector("#review-panel", state="visible")
    page.click("#start")


def _answer(page, value=h.SYN_ANSWER, qid="Q1"):
    page.wait_for_selector(f"#qslot input[data-q='{qid}']", timeout=20000)
    page.fill(f"#qslot input[data-q='{qid}']", value)
    page.click("#send")
    page.wait_for_selector("#confirm-send", state="visible")
    page.click("#confirm-send")


def _stage(page, key):
    return page.get_attribute(f"#stage-{key}", "data-state")


def _wait_end(page, label="완료"):
    """결과(result) 이벤트 뒤에 오는 종료(end) 이벤트까지 기다림 (결과 표시와 종료 표시는 서로 다른 이벤트)."""
    page.wait_for_function("t => document.querySelector('#run-state').textContent === t", arg=label, timeout=15000)


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
        assert page.is_disabled("#custom") and page.is_disabled("#review")
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
        page.wait_for_function("document.querySelector('#qerror').textContent.includes('보내지 못했습니다')")
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
        page.wait_for_function("document.querySelector('#stage-criteria').dataset.state === 'active'")
        page.click("#cancel")
        page.wait_for_function("document.querySelector('#run-state').textContent === '취소 중'")
        fake.release.set()
        page.wait_for_function("document.querySelector('#run-state').textContent === '취소됨'", timeout=15000)
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
        page.wait_for_function("state.es && state.es.readyState === EventSource.OPEN", timeout=15000)
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
        page.wait_for_function("document.querySelector('#run-state').textContent === '취소됨'", timeout=10000)
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
        page.wait_for_function("document.querySelector('#run-state').textContent === '시간 초과'", timeout=20000)
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
        page.click("#review")  # 빈 입력: 화면에서 막음
        assert page.inner_text("#input-error") == "환자 정보를 입력하세요." and page.is_hidden("#review-panel")
        page.fill("#custom", "x" * 30000)  # 한도(서버 MAX_TEXT_CHARS와 같은 20,000자)를 넘는 입력은 화면에서 막음
        n = len(page.input_value("#custom"))
        if n > 20000:
            page.click("#review")
            assert "20,000자 이하" in page.inner_text("#input-error") and page.is_hidden("#review-panel")
        else:
            assert n == 20000 and "20,000 / 20,000" in page.inner_text("#char-count")
        # 서버가 거절하면 서버의 문자열 메시지를 그대로 보여 줌
        page.route("**/api/runs", lambda route: route.fulfill(status=429, content_type="application/json",
                                                              body=json.dumps({"detail": "이 브라우저에서 진행 중인 실행이 있습니다."})))
        page.fill("#custom", h.SYN_TEXT)
        page.click("#review")
        page.click("#start")
        page.wait_for_function("document.querySelector('#start-hint').textContent.includes('진행 중인 실행')")
        assert not page.is_disabled("#custom")
        page.unroute("**/api/runs")
        # 중복 제출: 빠르게 두 번 눌러도 실행 요청은 한 번
        page.click("#review")
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
