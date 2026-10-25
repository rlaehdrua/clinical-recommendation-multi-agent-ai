"""시연 화면 캡처 (중간발표용). 실제 웹 화면을 브라우저로 끝까지 진행하며 01~07 화면을 PNG로 남깁니다.

    # 준비: pip install -e ".[dev,web]" playwright  (브라우저는 설치된 Edge/Chrome 사용, 다운로드 없음)
    python scripts/capture_demo_screens.py [--out outputs/demo_screens] [--patient SYN-DEMO-A] [--headed]

흐름 (ad3a222 화면 기준 - '입력 내용 확인'(#review) 단계 없이 "분석 시작"으로 바로 시작):
    01_input            첫 화면
    02_patient          합성 환자 선택 (목록에서 고르기)
    03_trials           고급 설정에서 평가할 시험 직접 선택
    04_analysis         "분석 시작" 직후 진행 화면
    05_question         추가 질문 (질문 하나씩) + 답변 입력
    06_result           추천 결과 목록 (06_result_detail: 시험 상세·조건별 근거)
    07_inspector        개발자 진단 패널 (주소에 ?diagnostic=1일 때만 표시. 07_inspector_page: 패널이 있는 화면 전체)

- 오프라인 전용: LLM은 tests/web_harness.FakeLLM(합성 출력)으로 바꾸고 실제 API 클라이언트 생성을 막습니다.
  브라우저의 외부 출처 요청(CDN 등)은 모두 차단하며, 차단된 요청이 있으면 실패로 끝냅니다. 유료 호출 없음.
- 01~06은 기본 주소(진단 꺼짐)에서, 07은 ?diagnostic=1에서 같은 입력으로 한 번 더 실행해 캡처합니다.
  기본 실행에서 진단 패널이 보이거나 진단 이벤트가 생기면, 또는 두 실행의 판정(match_status)이 다르면 실패로 끝냅니다.
- 판정·추천·환자 데이터를 바꾸지 않습니다: 웹 서버·Session·Matcher 코드는 그대로 쓰고, 결과 저장 위치만 임시 폴더로 돌립니다.
- 출력은 outputs/ 아래(git에 올라가지 않음)에 씁니다. 비어 있지 않은 출력 폴더는 덮어쓰지 않고 거부합니다(--out으로 새 경로).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))  # web_harness: 테스트와 같은 오프라인 LLM 대역

DEFAULT_PATIENT = "SYN-DEMO-A"


class _Patch:
    """web_harness.install이 쓰는 monkeypatch 최소 대역 (pytest 없이 실행, 끝나면 원래 값으로 되돌림)."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def delenv(self, name, raising=True):
        if name in os.environ:
            old = os.environ.pop(name)
            self._undo.append((os.environ, name, old))

    def undo(self):
        for obj, name, old in reversed(self._undo):
            if obj is os.environ:
                os.environ[name] = old
            else:
                setattr(obj, name, old)
        self._undo.clear()


def _serve(app):
    import uvicorn
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    if not srv.started:
        raise SystemExit("로컬 서버를 시작하지 못했습니다")
    return srv, t, f"http://127.0.0.1:{port}"


def _browser(p, headed: bool):
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel, headless=not headed)
        except Exception:  # noqa: BLE001 - 설치된 브라우저를 차례로 시도
            continue
    raise SystemExit("Edge 또는 Chrome을 찾을 수 없습니다")


def _new_page(browser, base: str):
    page = browser.new_context(viewport={"width": 1280, "height": 900}, locale="ko-KR").new_page()
    page.external, page.errors = [], []

    def route(r):
        if r.request.url.startswith(base):
            return r.continue_()
        page.external.append(r.request.url)  # 외부 네트워크 차단
        return r.abort()
    page.route("**/*", route)
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    return page


def _to_send(page):
    """질문을 하나씩 '다음'으로 넘겨 마지막의 '답변 확인'까지."""
    for _ in range(20):
        if page.is_visible("#send"):
            page.click("#send")
            return
        page.click("#qnext")
    raise SystemExit("답변 확인 버튼까지 이동하지 못했습니다")


def _journey(base: str, path: str, patient: str, trial_ids: list[str], answer: str, headed: bool, shot, fake) -> tuple[str, object]:
    """입력 -> 환자 선택 -> 시험 선택 -> 분석 시작 -> 질문·답변 -> 결과. 실행 ID와 열린 페이지를 돌려줌."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = _browser(pw, headed)
    page = _new_page(browser, base)
    page.goto(base + path)
    page.wait_for_selector("#patient option", state="attached")
    if page.locator("#review, #review-panel").count():
        raise SystemExit("예상하지 않은 화면: 삭제된 '입력 내용 확인' 단계가 있습니다")
    shot(page, "01_input")

    page.click("#to-list")
    options = page.evaluate("() => [...document.querySelectorAll('#patient option')].map(o => o.value)")
    if patient not in options:
        raise SystemExit(f"합성 환자 {patient}가 목록에 없습니다")
    if not patient.startswith("SYN-"):
        raise SystemExit("시연 캡처는 합성 환자(SYN-...)만 사용합니다")
    page.select_option("#patient", patient)
    shot(page, "02_patient")

    page.click("#advanced > summary")
    page.check('input[name="cand-mode"][value="given"]')
    page.evaluate("ids => { document.querySelectorAll('#trials input').forEach(i => i.checked = ids.includes(i.value));"
                  " document.querySelector('#trials').dispatchEvent(new Event('change')); }", trial_ids)
    if page.locator("#trials input:checked").count() != len(trial_ids):
        raise SystemExit(f"시험 선택 실패: {trial_ids}")
    page.locator("#advanced").scroll_into_view_if_needed()
    shot(page, "03_trials")

    if not page.is_enabled("#start"):
        raise SystemExit("'분석 시작' 버튼이 비활성 상태입니다")
    page.click("#start")  # 입력 확인은 클릭 때 하고 바로 실행 시작 (확인 단계 없음)
    page.wait_for_selector("#progress-panel:not([hidden])", timeout=30000)
    # 대역 LLM은 즉시 답하므로 질문 만들기 직전에 잠시 세워 진행 화면을 찍고 풀어 줌 (FakeLLM.block_on, 출력은 그대로)
    if not fake.entered.wait(30):
        raise SystemExit("분석이 질문 단계까지 진행되지 않았습니다")
    page.wait_for_function("() => document.querySelectorAll('#cards .card, #cards [data-tid]').length > 0", timeout=10000)
    shot(page, "04_analysis")
    fake.release.set()

    page.wait_for_selector("#qslot .q:not([hidden])", timeout=60000)
    qid = page.locator("#qslot .q:not([hidden])").get_attribute("data-qid")
    page.fill(f"#qslot input[data-q='{qid}']", answer)
    shot(page, "05_question")
    _to_send(page)
    page.wait_for_selector("#confirm-send", state="visible")
    page.click("#confirm-send")

    page.wait_for_selector("#page-result:not([hidden])", timeout=60000)
    page.wait_for_selector("#final .rec", timeout=30000)
    page.wait_for_function("() => !document.querySelector('#cancel') || document.querySelector('#cancel').hidden", timeout=30000)
    rid = page.evaluate("() => sessionStorage.getItem('ctrec-run-id')")
    page._pw, page._browser = pw, browser
    return rid, page


def _close(page):
    page._browser.close()
    page._pw.stop()


def _check_clean(page, label: str):
    if page.external:
        raise SystemExit(f"{label}: 외부 요청이 발생했습니다 {page.external}")
    if page.errors:
        raise SystemExit(f"{label}: 화면 오류 {page.errors}")


def capture(out: Path, patient: str = DEFAULT_PATIENT, headed: bool = False) -> list[Path]:
    import web_harness as h
    from ctrec.web import app as web

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(name, None)  # 대역이 빠져도 실제 호출로 이어지지 않게
    out.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    def shot(page, name, full=True):
        p = out / f"{name}.png"
        page.screenshot(path=str(p), full_page=full)
        files.append(p)

    statuses = {}
    for path in ("/", "/?diagnostic=1"):
        patch = _Patch()
        tmp = Path(tempfile.mkdtemp(prefix="ctrec-capture-"))
        fake = h.install(patch, tmp, h.FakeLLM(block_on="QuestionSet"))  # LLM 대역 + 실제 API 클라이언트 차단 + 결과 저장은 임시 폴더
        srv, t, base = _serve(web.app)
        try:
            diag = "diagnostic=1" in path
            take = (lambda page, name, full=True: None) if diag else shot  # 01~06은 기본 화면에서만
            rid, page = _journey(base, path, patient, h.TRIAL_IDS, h.SYN_ANSWER, headed, take, fake)
            events = web.RUNS[rid].events
            result = next(e for e in events if e["type"] == "result")["result"]
            statuses[path] = {tid: m.get("match_status") for tid, m in result["matches"].items()}
            n_diag = sum(e["type"] == "decision_diagnostic" for e in events)
            if not diag:
                shot(page, "06_result")
                page.click(f"#final [data-detail='{h.TRIAL_IDS[-1]}']")
                page.wait_for_selector("#page-detail:not([hidden])")
                shot(page, "06_result_detail")
                page.click("#detail-back")
                page.click('#pages [data-page="run"]')
                if n_diag or page.is_visible("#decision-panel"):
                    raise SystemExit("기본 화면에 진단 패널/진단 이벤트가 있습니다 (?diagnostic=1에서만 보여야 함)")
            else:
                page.click('#pages [data-page="run"]')  # 진단 패널은 입력·진행 화면에 있음
                if not n_diag or not page.is_visible("#decision-panel"):
                    raise SystemExit("?diagnostic=1인데 진단 패널이 표시되지 않았습니다")
                page.evaluate("() => { document.querySelector('#decision-panel').open = true;"
                              " document.querySelector('header.top').style.position = 'static'; }")  # 캡처 때 고정 머리글 겹침 방지
                page.locator("#decision-panel").scroll_into_view_if_needed()
                p = out / "07_inspector.png"
                page.locator("#decision-panel").screenshot(path=str(p))
                files.append(p)
                shot(page, "07_inspector_page")
            _check_clean(page, path)
            _close(page)
        finally:
            fake.release.set()
            srv.should_exit = True
            t.join(5)
            patch.undo()
    if statuses["/"] != statuses["/?diagnostic=1"]:
        raise SystemExit(f"진단 표시 여부에 따라 판정이 달라졌습니다: {statuses}")
    (out / "capture_meta.json").write_text(json.dumps({
        "patient": patient, "trials": h.TRIAL_IDS, "llm": "offline FakeLLM (tests/web_harness.py)",
        "match_status": statuses["/"], "files": [f.name for f in files]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "demo_screens")
    ap.add_argument("--patient", default=DEFAULT_PATIENT, help="합성 환자 ID (SYN-...)")
    ap.add_argument("--headed", action="store_true", help="브라우저 창을 띄워 진행 (기본: 창 없이)")
    a = ap.parse_args(argv)
    if a.out.exists() and any(a.out.iterdir()):
        print(f"출력 폴더가 비어 있지 않습니다 (덮어쓰지 않음): {a.out}", file=sys.stderr)
        return 2
    files = capture(a.out, a.patient, a.headed)
    for f in files:
        print(f)
    print(f"{len(files)}개 화면 저장: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
