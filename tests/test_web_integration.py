"""웹 서비스 통합 테스트 (HTTP 수준). FastAPI TestClient + 실제 오케스트레이터·Session·Matcher, LLM만 오프라인 대역.

API 키·네트워크 없이 재현됩니다. 합성 환자 정보만 사용하고, 결과 파일은 임시 폴더에만 씁니다.
실제 Claude API 연결과 실제 LLM 판정 품질은 이 테스트 범위가 아닙니다.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

sys.path.insert(0, str(Path(__file__).parent))


import web_harness as h
from ctrec import store_patch
from ctrec.orchestrator import Orchestrator
from ctrec.pipeline import PatientCase, Session
from ctrec.trace import Tracer
from ctrec.web import app as web


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    return h.install(monkeypatch, tmp_path)


@pytest.fixture()
def client(fake):
    return h.browser()  # 실행을 만드는 브라우저 (소유권 쿠키를 받음)


def _start(client, text=h.SYN_TEXT, **kw):
    r = client.post("/api/runs", json={"custom_text": text, "trial_ids": h.TRIAL_IDS, "screen": False, **kw})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    return rid, web.RUNS[rid]


def _complete(client, answer=h.SYN_ANSWER):
    """한 번의 전체 흐름: 시작 -> 질문 -> 답변 -> 결과. 반환: (run_id, SSE 이벤트 목록)."""
    rid, run = _start(client)
    q = h.wait_for(run, "questions")
    assert client.post(f"/api/runs/{rid}/answers",
                       json={"answers": {x["question_id"]: answer for x in q["questions"]}}).status_code == 200
    h.wait_for(run, "end", 30)
    return rid, h.sse_events(client.get(f"/api/runs/{rid}/events").text)


def _of(events, kind):
    return [e for e in events if e["type"] == kind]


# ------------------------------------------------------------ Flow A. 최초 접속
def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    for path in ("/api/config", "/api/patients", "/api/trials", "/api/runs", "/answers", "/events"):
        assert path in r.text  # 화면 스크립트가 쓰는 API 경로가 실제 서버 경로와 같음
    assert client.get("/healthz").json() == {"ok": True}


def test_config_trials_patients_shape(client):
    cfg = client.get("/api/config").json()
    assert set(cfg) == {"model", "disclaimer", "max_rounds"}
    trials = client.get("/api/trials").json()
    assert len(trials) == 26 and {"trial_id", "title", "overall_status", "open", "conditions", "synthetic"} <= set(trials[0])
    patients = client.get("/api/patients").json()
    assert patients and {"patient_id", "group", "text", "candidate_trials", "has_hidden"} <= set(patients[0])


# ------------------------------------------------------------ Flow B-D. 입력 -> 질문·답변 -> 추천
def test_full_flow_questions_answers_and_result(client, fake):
    rid, events = _complete(client)
    types = [e["type"] for e in events if e["type"] != "trace"]
    assert types == ["status", "questions", "answers", "result", "end"]
    status = _of(events, "status")[0]
    assert status["patient_id"].startswith("WEB-") and status["trial_ids"] == h.TRIAL_IDS
    assert "patient_text" not in status  # 원문을 이벤트로 다시 보내지 않음
    assert _of(events, "answers")[0] == {**_of(events, "answers")[0], "n_answered": 1}
    assert h.SYN_ANSWER not in json.dumps(_of(events, "answers"), ensure_ascii=False)

    ev = _of(events, "result")[0]
    res = ev["result"]
    assert set(res["matches"]) == set(h.TRIAL_IDS)
    for m in res["matches"].values():
        assert m["eligibility"] in ("ELIGIBLE", "UNCERTAIN", "INELIGIBLE", "NOT_EVALUATED") and m["match_status"]
    rec = res["recommendation"]
    assert {"ranked", "recommended", "excluded", "selection_reason", "no_recommendation_reason"} <= set(rec)
    shown = {r["trial_id"] for r in rec["ranked"]} | {x["trial_id"] for x in rec["excluded"]}
    assert shown == set(h.TRIAL_IDS)  # 모든 시험이 추천 또는 제외 사유와 함께 표시됨
    for r in rec["ranked"]:  # 화면(recCard)이 읽는 필드
        assert {"rank", "trial_id", "match_status", "rationale", "key_concerns", "next_steps", "physician_review",
                "verification_needed"} <= set(r)
    assert ev["trials"]["NCT04063540"]["url"] == "https://clinicaltrials.gov/study/NCT04063540"
    assert ev["trials"]["DEMO-LUNG-001"]["url"] is None  # 가상 시험은 링크 없음 (화면은 링크를 숨김)
    assert ev["match_note"] and ev["report"]
    assert res["qa_log"][0]["answer"] == h.SYN_ANSWER  # 같은 실행(Session)에 답변이 반영됨
    assert [e["round"] for e in res["fact_shadow"]] == [0, 1] and res["fact_shadow"][1]["added"] >= 1
    assert "fact_comparison" not in res and "dependency_diagnostic" not in res  # 진단 기능 기본 off


def test_web_results_match_direct_pipeline(client, fake, monkeypatch):
    """웹 경로가 기존 판정을 바꾸지 않음: 같은 입력·대역으로 Session을 직접 실행한 결과와 기준별 판정이 같음."""
    _, events = _complete(client)
    web_res = _of(events, "result")[0]["result"]

    class Ans:
        def answer(self, questions, patient_id):
            return {q.question_id: h.SYN_ANSWER for q in questions}
    trials = {t: web._load_trials()[t] for t in h.TRIAL_IDS}
    s = Session(case=PatientCase(web_res["patient_id"], h.SYN_TEXT), trials=trials, answerer=Ans(),
                tracer=Tracer(None, verbose=False))
    Orchestrator(s).run()

    def table(matches):
        return {t: (m["eligibility"], m["match_status"],
                    [(a["rule_id"], a["passes"], a["needs_verification"]) for a in m["assessments"]])
                for t, m in matches.items()}
    assert table(web_res["matches"]) == table({t: m.model_dump() for t, m in s.matches.items()})
    assert web_res["recommendation"]["ranked"] == s.recommendation.model_dump()["ranked"]


def test_default_screen_mode_flow(client, fake):
    """화면의 기본값(전체 시험에서 자동 추림): 26개 풀 -> 추림 에이전트 -> 고른 시험만 평가."""
    r = client.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": [], "screen": True})
    rid = r.json()["run_id"]
    run = web.RUNS[rid]
    assert len(h.wait_for(run, "status")["trial_ids"]) == 26
    try:
        q = h.wait_for(run, "questions", 10)
        client.post(f"/api/runs/{rid}/answers", json={"answers": {x["question_id"]: "모름" for x in q["questions"]}})
    except AssertionError:
        pass  # 추림 결과에 따라 질문이 없을 수 있음
    h.wait_for(run, "end", 30)
    res = _of(h.sse_events(client.get(f"/api/runs/{rid}/events").text), "result")[0]["result"]
    assert "ScreeningOutput" in fake.calls and res["screening"]["pool_size"] == 26
    assert set(res["matches"]) <= set(h.TRIAL_IDS) and res["matches"]


def test_sse_resume_and_bad_last_event_id(client):
    rid, events = _complete(client)
    later = h.sse_events(client.get(f"/api/runs/{rid}/events", headers={"last-event-id": "2"}).text)
    assert [e["id"] for e in later] == [e["id"] for e in events if e["id"] > 2]
    r = client.get(f"/api/runs/{rid}/events", headers={"last-event-id": "abc"})
    assert r.status_code == 200 and h.sse_events(r.text)[-1]["type"] == "end"


# ------------------------------------------------------------ Flow E. 오류·예외
@pytest.mark.parametrize("body, code", [
    ({"trial_ids": h.TRIAL_IDS, "screen": False}, 400),                       # 환자 없음
    ({"custom_text": "   ", "trial_ids": h.TRIAL_IDS, "screen": False}, 400),  # 빈 입력
    ({"custom_text": "x", "trial_ids": [], "screen": False}, 400),            # 시험 없음
    ({"patient_id": "NO-SUCH", "trial_ids": h.TRIAL_IDS, "screen": False}, 404),
    ({"custom_text": 12345, "trial_ids": h.TRIAL_IDS, "screen": False}, 422),  # 잘못된 형식
    ({"custom_text": "x" * 20001, "trial_ids": h.TRIAL_IDS, "screen": False}, 422),
    ({"custom_text": "x", "trial_ids": h.TRIAL_IDS, "screen": False, "max_rounds": 99}, 422),
])
def test_invalid_run_requests(client, fake, body, code):
    r = client.post("/api/runs", json=body)
    assert r.status_code == code and isinstance(r.json()["detail"], str)  # 화면 hint가 문자열로 보여 줄 수 있음
    for v in ("12345", "NO-SUCH", "xxxxxxxxxx"):
        assert v not in r.text  # 보낸 값을 오류 응답에 되돌려 보내지 않음
    assert web.RUNS == {} and fake.calls == []  # 실행이 만들어지지 않고 LLM도 부르지 않음


def test_malformed_json_body(client):
    r = client.post("/api/runs", content='{"custom_text": "58세 합성'.encode(), headers={"content-type": "application/json"})
    assert r.status_code == 422 and "58세" not in r.text


def test_unknown_run_and_answer_state(client):
    assert client.get("/api/runs/nope/events").status_code == 404
    assert client.post("/api/runs/nope/answers", json={"answers": {"Q1": "a"}}).status_code == 404
    rid, run = _start(client)
    h.wait_for(run, "questions")
    assert client.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "x" * 2001}}).status_code == 400
    ok = client.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": h.SYN_ANSWER, "Q999": "무관한 키"}})
    assert ok.status_code == 200
    again = client.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "두 번째"}})
    assert again.status_code == 409  # 같은 질문에 중복 제출
    h.wait_for(run, "end", 30)
    assert client.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "끝난 뒤"}}).status_code == 409
    res = _of(h.sse_events(client.get(f"/api/runs/{rid}/events").text), "result")[0]["result"]
    assert [q["answer"] for q in res["qa_log"]] == [h.SYN_ANSWER]  # 나중 제출·무관한 키가 섞이지 않음


SECRET = "합성-민감-문구-58세"


@pytest.mark.parametrize("where", ["orchestrator", "PatientProfile"])
def test_pipeline_failure_reported_without_details(monkeypatch, tmp_path, where):
    h.install(monkeypatch, tmp_path, h.FakeLLM(fail_on=where, fail_message=SECRET))
    c = h.browser()
    rid, run = _start(c)
    h.wait_for(run, "end", 30)
    events = h.sse_events(c.get(f"/api/runs/{rid}/events").text)
    [err] = _of(events, "error")
    assert "RuntimeError" in err["message"] and SECRET not in json.dumps(events, ensure_ascii=False)
    assert _of(events, "end") and run.done


def test_matcher_failure_marks_not_evaluated(monkeypatch, tmp_path):
    h.install(monkeypatch, tmp_path, h.FakeLLM(fail_on="MatcherOutput", fail_message=SECRET))
    c = h.browser()
    rid, run = _start(c)
    h.wait_for(run, "end", 30)
    events = h.sse_events(c.get(f"/api/runs/{rid}/events").text)
    res = _of(events, "result")[0]["result"]
    assert {m["eligibility"] for m in res["matches"].values()} == {"NOT_EVALUATED"}  # 부적격으로 바꾸지 않음
    traces = json.dumps([e for e in events if e["type"] == "trace"], ensure_ascii=False)
    assert SECRET not in traces  # 화면으로 가는 실행 기록에는 예외 종류만


def test_llm_provider_not_configured(monkeypatch, tmp_path):
    """API 키가 없을 때처럼 LLM 클라이언트를 만들 수 없어도 서버는 응답하고, 실행은 오류로 끝남 (실제 요청 없음)."""
    from ctrec import llm

    real_create = llm.create  # 실제 llm.create 경로를 쓰되, 클라이언트 생성에서 실패시킴
    h.install(monkeypatch, tmp_path)
    monkeypatch.setattr(llm, "create", real_create)

    def no_key():
        raise TypeError("Could not resolve authentication method")
    monkeypatch.setattr(llm, "client", no_key)
    c = h.browser()
    rid, run = _start(c)
    h.wait_for(run, "end", 30)
    [err] = _of(h.sse_events(c.get(f"/api/runs/{rid}/events").text), "error")
    assert "TypeError" in err["message"] and "authentication" not in err["message"]
    assert c.get("/healthz").status_code == 200


# ------------------------------------------------------------ 반복 요청, 격리, 실행 ID
def test_repeated_requests_are_independent_runs(client):
    """다른 브라우저 두 곳의 실행은 서로 독립 (같은 브라우저의 동시 실행 제한은 test_web_security에서 확인)."""
    other = h.browser()
    a, _ = _start(client)
    b, _ = _start(other)
    assert a != b and len(web.RUNS) == 2
    for c, rid in ((client, a), (other, b)):
        h.wait_for(web.RUNS[rid], "questions")
        c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "모름"}})
        h.wait_for(web.RUNS[rid], "end", 30)


def test_sessions_isolated(client):
    other = h.browser()
    text_a, text_b = h.SYN_TEXT + " 합성A", h.SYN_TEXT + " 합성B"
    a, run_a = _start(client, text_a)
    b, run_b = _start(other, text_b)
    h.wait_for(run_a, "questions")
    h.wait_for(run_b, "questions")
    client.post(f"/api/runs/{a}/answers", json={"answers": {"Q1": "A만의-답변"}})
    h.wait_for(run_a, "end", 30)
    assert not run_b.done and run_b.pending_questions == {"Q1"}  # A의 답변이 B를 진행시키지 않음
    other.post(f"/api/runs/{b}/answers", json={"answers": {"Q1": "B만의-답변"}})
    h.wait_for(run_b, "end", 30)
    ra = _of(h.sse_events(client.get(f"/api/runs/{a}/events").text), "result")[0]["result"]
    rb = _of(h.sse_events(other.get(f"/api/runs/{b}/events").text), "result")[0]["result"]
    assert ra["patient_id"] != rb["patient_id"]
    assert [q["answer"] for q in ra["qa_log"]] == ["A만의-답변"] and [q["answer"] for q in rb["qa_log"]] == ["B만의-답변"]
    dump_a = json.dumps(h.sse_events(client.get(f"/api/runs/{a}/events").text), ensure_ascii=False)
    assert "합성B" not in dump_a and "B만의-답변" not in dump_a


def test_run_id_not_guessable(client):
    rid, _ = _start(client)
    token = rid.rsplit("-", 1)[1]
    assert len(token) == 32 and int(token, 16) >= 0  # 128비트 난수 (이전: 4자리 16진수)


def test_no_paid_api_and_no_files_by_default(client, fake, tmp_path):
    _complete(client)
    assert "orchestrator" in fake.calls and "MatcherOutput" in fake.calls  # 모든 LLM 호출이 대역으로 감
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []  # 웹 실행은 기본적으로 디스크에 저장하지 않음
    assert store_patch.verify_frozen() == []


def test_run_limit_returns_429(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_RUNS", 1)
    rid, run = _start(client)
    other = h.browser()  # 다른 브라우저도 전체 상한에 걸림
    assert other.post("/api/runs", json={"custom_text": "x", "trial_ids": h.TRIAL_IDS, "screen": False}).status_code == 429
    h.wait_for(run, "questions")
    client.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "모름"}})
    h.wait_for(run, "end", 30)


def test_concurrent_answer_posts_accept_one(client):
    rid, run = _start(client)
    h.wait_for(run, "questions")
    codes = []

    def post(i):
        codes.append(client.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": f"동시-{i}"}}).status_code)
    threads = [threading.Thread(target=post, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200, 409, 409, 409, 409]
    h.wait_for(run, "end", 30)
