"""웹 사용자 흐름 (Phase 6-1): SSE 이어 받기 확장과 화면 코드의 정적 불변식. 브라우저·유료 호출 없음.

브라우저 E2E는 test_web_browser.py (CTREC_BROWSER_TESTS=1).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402

from ctrec import status  # noqa: E402
from ctrec.web import app as web  # noqa: E402

INDEX = (Path(web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = (Path(web.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")  # 화면 스크립트 (CSP로 분리)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    h.install(monkeypatch, tmp_path)
    return h.browser()


def _ids(events):
    return [e["id"] for e in events]


def test_sse_resume_with_after_query(client):
    rid = client.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}).json()["run_id"]
    run = web.RUNS[rid]
    q = h.wait_for(run, "questions")
    client.post(f"/api/runs/{rid}/answers", json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}})
    h.wait_for(run, "end", 30)
    all_ids = _ids(h.sse_events(client.get(f"/api/runs/{rid}/events").text))
    assert all_ids == list(range(len(all_ids)))
    # ?after=N: N 다음부터 (화면이 직접 다시 연결할 때)
    assert _ids(h.sse_events(client.get(f"/api/runs/{rid}/events?after=3").text)) == all_ids[4:]
    # Last-Event-ID 헤더가 있으면 헤더 우선 (브라우저 자동 재연결, 기존 동작)
    both = client.get(f"/api/runs/{rid}/events?after=1", headers={"last-event-id": "5"}).text
    assert _ids(h.sse_events(both)) == all_ids[6:]
    # 잘못된 값은 처음부터 (기존과 같음)
    assert _ids(h.sse_events(client.get(f"/api/runs/{rid}/events?after=abc").text)) == all_ids


def test_after_query_does_not_bypass_ownership(client):
    rid = client.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}).json()["run_id"]
    stranger = h.browser(client=("198.51.100.77", 1))
    assert stranger.get(f"/api/runs/{rid}/events?after=0").status_code == 404
    client.post(f"/api/runs/{rid}/cancel")
    h.wait_for(web.RUNS[rid], "end", 30)


def test_frontend_limits_match_server():
    assert re.search(rf"const MAX_TEXT = {web.MAX_TEXT_CHARS};", SCRIPT)
    assert re.search(rf"const MAX_ANSWER = {web.MAX_ANSWER_CHARS};", SCRIPT)
    assert f'maxlength="{web.MAX_TEXT_CHARS}"' in INDEX


def test_frontend_storage_and_links():
    assert "localStorage" not in SCRIPT  # 환자 정보는 브라우저 저장소에 두지 않음
    assert re.findall(r"sessionStorage\.setItem\(([^,]+),", SCRIPT) == ["RUN_KEY"]  # 실행 ID만
    for tag in re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", SCRIPT):
        assert 'rel="noopener noreferrer"' in tag
    assert "safeTrialUrl(" in SCRIPT and "clinicaltrials\\.gov" in SCRIPT
    assert "eval(" not in SCRIPT and "document.write" not in SCRIPT


def test_frontend_status_labels_follow_server():
    """화면의 외부 상태 이름은 서버 공식 상태(status.LABEL_KO)와 같고, 6개 상태 모두 설명이 있음."""
    block = SCRIPT.split("const STATUS = {", 1)[1].split("};", 1)[0]
    shown = dict(re.findall(r'(\w+): \["([^"]+)"', block))
    assert set(shown) == set(status.LABEL_KO)
    for k, v in shown.items():
        assert status.LABEL_KO[k].startswith(v), (k, v)


def test_no_progress_percentage_and_decision_loop_stays_diagnostic():
    # 임의 진행률 없음: 화면 코드가 퍼센트를 만들지 않고, 진행 막대 요소도 없음 (CSS의 width: 100%는 해당 없음)
    assert "%" not in SCRIPT and not re.search(r"<progress|progress-?bar", INDEX, re.I)
    assert "decision_diagnostic = true" in SCRIPT and 'get("diagnostic") === "1"' in SCRIPT  # 진단은 명시할 때만
    assert "/api/runs" in SCRIPT and "question_generator" in SCRIPT  # 질문은 서버(기존 LLM 생성기) 이벤트로만 표시
