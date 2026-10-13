"""웹 실행 소유권·CSRF·데이터 보존·취소·자원 제한 테스트 (Phase 2.5). 합성 환자 정보 + 오프라인 LLM 대역."""

from __future__ import annotations

import base64
import contextvars
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h
from ctrec import llm, store_patch
from ctrec.web import access, retention
from ctrec.web import app as web

BODY = {"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    return h.install(monkeypatch, tmp_path)


def _start(c, body=BODY):
    r = c.post("/api/runs", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    return rid, web.RUNS[rid], r


def _events(c, rid):
    return h.sse_events(c.get(f"/api/runs/{rid}/events").text)


def _finish(c, rid, run):
    h.wait_for(run, "questions")
    c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "모름"}})
    h.wait_for(run, "end", 30)


def _set_cookies(resp) -> list[str]:
    return resp.headers.get_list("set-cookie")


# ------------------------------------------------------------ 실행 소유권
def test_owner_can_read_answer_and_check_status(fake):
    owner = h.browser()
    rid, run, _ = _start(owner)
    h.wait_for(run, "questions")
    assert owner.get(f"/api/runs/{rid}").json() == {"run_id": rid, "state": "running", "done": False,
                                                    "waiting_for_answers": True}
    assert owner.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "모름"}}).status_code == 200
    h.wait_for(run, "end", 30)
    assert [e["type"] for e in _events(owner, rid)][-1] == "end"


def test_other_browser_and_run_id_only_are_denied(fake):
    owner, stranger = h.browser(), h.browser()
    rid, run, _ = _start(owner)
    h.wait_for(run, "questions")
    missing = stranger.get("/api/runs/20260101-000000-" + "0" * 32)
    for resp in (stranger.get(f"/api/runs/{rid}"), stranger.get(f"/api/runs/{rid}/events"),
                 stranger.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "가로채기"}}),
                 stranger.post(f"/api/runs/{rid}/cancel")):
        assert resp.status_code == 404 and resp.json() == missing.json()  # 없는 실행과 같은 응답 (존재 여부 비노출)
        assert h.SYN_TEXT not in resp.text
    assert run.pending_questions == {"Q1"} and run.state == "running"  # 다른 브라우저가 답하거나 취소하지 못함
    _finish(owner, rid, run)


def test_token_of_another_run_is_not_reusable(fake):
    a, b = h.browser(), h.browser()
    rid_a, run_a, resp_a = _start(a)
    rid_b, run_b, _ = _start(b)
    token_a = next(c.split(";")[0].split("=", 1)[1] for c in _set_cookies(resp_a) if c.startswith("ctrec_run="))
    thief = h.browser()
    thief.cookies.set(access.RUN_COOKIE, token_a, path=f"/api/runs/{rid_b}")
    assert thief.get(f"/api/runs/{rid_b}/events").status_code == 404  # A의 토큰으로 B에 접근 불가
    assert thief.get(f"/api/runs/{rid_b}").status_code == 404
    _finish(a, rid_a, run_a)
    _finish(b, rid_b, run_b)


def test_token_only_in_httponly_cookie_and_server_keeps_digest(fake):
    owner = h.browser()
    rid, run, resp = _start(owner)
    cookie = next(c for c in _set_cookies(resp) if c.startswith("ctrec_run="))
    token = cookie.split(";")[0].split("=", 1)[1]
    attrs = cookie.lower()
    assert "httponly" in attrs and "samesite=strict" in attrs and f"path=/api/runs/{rid}".lower() in attrs
    assert "secure" in attrs  # HTTPS 접속
    assert resp.json() == {"run_id": rid} and token not in resp.text  # 응답 본문·URL에 없음
    assert run.owner_digest == access.digest(token) and token not in json.dumps(run.__dict__, default=str)
    _finish(owner, rid, run)
    assert token not in json.dumps(_events(owner, rid), ensure_ascii=False)  # SSE 이벤트에 없음


def test_sse_reconnect_and_refresh_with_cookie(fake):
    owner = h.browser()
    rid, run, _ = _start(owner)
    _finish(owner, rid, run)
    events = _events(owner, rid)
    later = h.sse_events(owner.get(f"/api/runs/{rid}/events", headers={"last-event-id": "1"}).text)
    assert [e["id"] for e in later] == [e["id"] for e in events if e["id"] > 1]
    assert owner.get(f"/api/runs/{rid}").json()["state"] == "completed"  # 새로고침 후 상태 확인


def test_basic_auth_users_are_still_separate_owners(fake, monkeypatch):
    monkeypatch.setattr(web, "PASSWORD", "공용-비밀번호")
    auth = {"Authorization": "Basic " + base64.b64encode("u:공용-비밀번호".encode()).decode()}
    a, b = h.browser(), h.browser()
    assert a.post("/api/runs", json=BODY).status_code == 401  # 비밀번호 없으면 거절
    r = a.post("/api/runs", json=BODY, headers=auth)
    rid = r.json()["run_id"]
    run = web.RUNS[rid]
    h.wait_for(run, "questions")
    assert b.get(f"/api/runs/{rid}/events", headers=auth).status_code == 404  # 같은 비밀번호여도 다른 소유자
    assert a.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "모름"}}, headers=auth).status_code == 200
    h.wait_for(run, "end", 30)


# ------------------------------------------------------------ CSRF·Origin
def test_csrf_header_and_origin_required(fake):
    from fastapi.testclient import TestClient

    plain = TestClient(web.app, base_url="https://testserver")  # 확인 헤더 없는 요청 (다른 사이트의 폼 제출과 같은 형태)
    assert plain.post("/api/runs", json=BODY).status_code == 403
    owner = h.browser()
    rid, run, _ = _start(owner)
    h.wait_for(run, "questions")
    for headers in ({"X-CTREC-Request": "0"}, {"origin": "https://evil.example"}, {"origin": "null"}):
        r = owner.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "x"}}, headers=headers)
        assert r.status_code == 403, headers
        assert owner.post(f"/api/runs/{rid}/cancel", headers=headers).status_code == 403
    assert run.pending_questions == {"Q1"} and run.state == "running"
    ok = owner.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "모름"}}, headers={"origin": "https://testserver"})
    assert ok.status_code == 200
    h.wait_for(run, "end", 30)


def test_request_size_limits(fake):
    c = h.browser()
    big = c.post("/api/runs", content=b"{" + b" " * (300 * 1024) + b"}", headers={"content-type": "application/json"})
    assert big.status_code == 413
    many = c.post("/api/runs", json={**BODY, "trial_ids": ["X"] * 201})
    assert many.status_code == 422 and "X" not in many.json()["detail"].replace("trial_ids", "")


# ------------------------------------------------------------ 데이터 보존
def test_default_writes_nothing(fake, tmp_path):
    c = h.browser()
    rid, run, _ = _start(c)
    _finish(c, rid, run)
    assert not (tmp_path / "outputs").exists()


def test_save_enabled_writes_marker_and_files(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("CTREC_WEB_SAVE_OUTPUTS", "1")
    c = h.browser()
    rid, run, _ = _start(c)
    _finish(c, rid, run)
    d = tmp_path / "outputs" / "web" / rid
    assert sorted(p.name for p in d.iterdir()) == [retention.MARKER, "report.md", "result.json", "trace.jsonl"]
    assert json.loads((d / retention.MARKER).read_text(encoding="utf-8"))["run_id"] == rid


def _mk(root: Path, name: str, age_s: float, marker=True, marker_id=None):
    d = root / name
    d.mkdir(parents=True)
    (d / "result.json").write_text("{}", encoding="utf-8")
    if marker:
        (d / retention.MARKER).write_text(json.dumps({"run_id": marker_id or name, "created_at": time.time() - age_s}),
                                          encoding="utf-8")
    return d


def test_purge_deletes_only_expired_marked_run_dirs(fake, tmp_path):
    root = tmp_path / "outputs" / "web"
    rid = lambda n: f"20260101-000000-{n:032x}"  # noqa: E731
    old = _mk(root, rid(1), 10_000)
    fresh = _mk(root, rid(2), 10)
    legacy = _mk(root, "20250101-000000-ab12", 10_000, marker=False)  # 정책 이전 형식·표식 없음
    wrong_marker = _mk(root, rid(3), 10_000, marker_id=rid(4))
    out = retention.purge_expired(ttl=3600)
    assert not old.exists() and fresh.exists() and legacy.exists() and wrong_marker.exists()
    assert out == {"deleted": 1, "kept": 1, "legacy": 2, "unsafe": 0}


def _link(target: Path, at: Path) -> None:
    """디렉터리 심볼릭 링크, 안 되면 Windows 정션(관리자 권한 불필요)."""
    try:
        os.symlink(target, at, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(at))
    except (ImportError, OSError):
        pytest.skip("이 환경에서 링크를 만들 수 없음")


def test_purge_refuses_links(fake, tmp_path):
    root = tmp_path / "outputs" / "web"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("지우면 안 됨", encoding="utf-8")
    name = f"20260101-000000-{5:032x}"
    d = _mk(root, name, 10_000)
    _link(outside, d / "link")
    _link(outside, root / f"20260101-000000-{6:032x}")
    out = retention.purge_expired(ttl=3600)
    assert out["unsafe"] == 2 and out["deleted"] == 0
    assert d.exists() and (outside / "keep.txt").exists()


def test_prepare_rejects_path_tricks(fake):
    for bad in ("../escape", "20260101-000000-" + "0" * 31 + "/", "..\\x", ""):
        with pytest.raises(ValueError):
            retention.prepare_run_dir(bad)


def test_existing_files_untouched_without_marker(fake, tmp_path):
    root = tmp_path / "outputs" / "web"
    legacy = _mk(root, "20250101-000000-ab12", 10 ** 7, marker=False)
    (legacy / "trace.jsonl").write_text("{}", encoding="utf-8")
    retention.purge_expired(ttl=60)
    assert sorted(p.name for p in legacy.iterdir()) == ["result.json", "trace.jsonl"]


# ------------------------------------------------------------ 취소
def test_cancel_while_waiting_for_answers(fake):
    c = h.browser()
    rid, run, _ = _start(c)
    h.wait_for(run, "questions")
    calls_before = len(fake.calls)
    assert c.post(f"/api/runs/{rid}/cancel").json() == {"ok": True, "state": "cancelling"}
    h.wait_for(run, "end", 10)
    assert c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "늦은 답"}}).status_code == 409
    assert c.post(f"/api/runs/{rid}/cancel").status_code == 409
    events = _events(c, rid)
    assert [e for e in events if e["type"] == "end"] == [events[-1]] and events[-1]["state"] == "cancelled"
    assert not [e for e in events if e["type"] == "result"]
    assert len(fake.calls) == calls_before  # 취소 뒤 LLM 호출 없음
    assert run.done and c.get(f"/api/runs/{rid}").json()["state"] == "cancelled"
    rid2, run2, _ = _start(c)  # 슬롯 회수: 같은 브라우저가 새 실행을 시작할 수 있음
    _finish(c, rid2, run2)


def test_cancel_during_inflight_call_stops_at_next_checkpoint(monkeypatch, tmp_path):
    fake = h.install(monkeypatch, tmp_path, h.FakeLLM(block_on="MatcherOutput"))
    c = h.browser()
    rid, run, _ = _start(c)
    assert fake.entered.wait(10)
    assert c.post(f"/api/runs/{rid}/cancel").status_code == 200
    assert not run.done  # 진행 중인 호출은 중단할 수 없음 -> 작업이 끝날 때까지 슬롯 유지
    other = h.browser()
    monkeypatch.setattr(web, "MAX_RUNS", 1)
    assert other.post("/api/runs", json=BODY).status_code == 429  # 아직 슬롯이 반환되지 않음
    n = len(fake.calls)
    fake.release.set()
    h.wait_for(run, "end", 10)
    assert run.state == "cancelled" and run.done
    # 취소 뒤 새로 시작된 LLM 호출 없음 (진행 중이던 1건과 같은 단계의 병렬 호출만 끝남)
    assert all(x == "MatcherOutput" for x in fake.calls[n:])
    assert "QuestionSet" not in fake.calls and "RecommendationDraft" not in fake.calls
    rid2, run2, _ = _start(other)  # 이제 슬롯이 회수됨
    _finish(other, rid2, run2)


def test_cancel_racing_completion_never_reports_completed(monkeypatch, tmp_path):
    """오케스트레이터가 끝난 직후 취소가 들어오면 결과를 보내지 않고 취소로 끝남."""
    fake = h.install(monkeypatch, tmp_path)
    real = web.Orchestrator
    c = h.browser()

    class CancelAtEnd(real):
        def run(self):
            super().run()
            rid = next(iter(web.RUNS))
            assert c.post(f"/api/runs/{rid}/cancel").status_code == 200
    monkeypatch.setattr(web, "Orchestrator", CancelAtEnd)
    rid, run, _ = _start(c)
    _finish(c, rid, run)
    events = _events(c, rid)
    assert events[-1] == {**events[-1], "type": "end", "state": "cancelled"}
    assert not [e for e in events if e["type"] == "result"]


def test_cancel_after_completion_is_rejected(fake):
    c = h.browser()
    rid, run, _ = _start(c)
    _finish(c, rid, run)
    assert c.post(f"/api/runs/{rid}/cancel").status_code == 409 and run.state == "completed"
    assert [e["type"] for e in _events(c, rid)].count("result") == 1


# ------------------------------------------------------------ 시간 제한, 실행 수 제한
def test_run_timeout_while_waiting(fake, monkeypatch):
    monkeypatch.setattr(web, "RUN_TIMEOUT_S", 1)
    c = h.browser()
    rid, run, _ = _start(c)
    h.wait_for(run, "end", 10)
    events = _events(c, rid)
    assert events[-1]["state"] == "timed_out" and run.state == "timed_out"
    assert not [e for e in events if e["type"] == "result"]
    assert c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "x"}}).status_code == 409


def test_answer_timeout_continues_with_unknown(fake, monkeypatch):
    monkeypatch.setattr(web, "ANSWER_TIMEOUT_S", 1)
    c = h.browser()
    rid, run, _ = _start(c)
    h.wait_for(run, "end", 15)
    res = [e for e in _events(c, rid) if e["type"] == "result"][0]["result"]
    assert run.state == "completed" and [q["answer"] for q in res["qa_log"]] == ["모름"]


def test_same_browser_limits(fake, monkeypatch):
    c = h.browser()
    rid, run, _ = _start(c)
    r = c.post("/api/runs", json=BODY)
    assert r.status_code == 429 and "진행 중인 실행" in r.json()["detail"]  # 같은 브라우저 동시 실행 1개
    _finish(c, rid, run)
    monkeypatch.setattr(web, "MAX_STARTS_PER_HOUR", 2)
    rid2, run2, _ = _start(c)
    _finish(c, rid2, run2)
    assert c.post("/api/runs", json=BODY).status_code == 429  # 시간당 실행 수


def test_end_event_consistency_for_all_outcomes(monkeypatch, tmp_path):
    """완료·취소·오류·시간 초과 모두 마지막 이벤트가 end 하나이고 상태가 들어 있음."""
    outcomes = {}
    for kind in ("completed", "cancelled", "error", "timed_out"):
        h.install(monkeypatch, tmp_path, h.FakeLLM(fail_on="PatientProfile" if kind == "error" else None))
        monkeypatch.setattr(web, "RUN_TIMEOUT_S", 1 if kind == "timed_out" else 3600)
        c = h.browser()
        rid, run, _ = _start(c)
        if kind == "completed":
            _finish(c, rid, run)
        elif kind == "cancelled":
            h.wait_for(run, "questions")
            c.post(f"/api/runs/{rid}/cancel")
        h.wait_for(run, "end", 15)
        events = _events(c, rid)
        assert [e["type"] for e in events].count("end") == 1 and events[-1]["type"] == "end"
        outcomes[kind] = events[-1]["state"]
    assert outcomes == {"completed": "completed", "cancelled": "cancelled", "error": "error", "timed_out": "timed_out"}


def test_cancelled_runs_released_from_memory(fake, monkeypatch):
    c = h.browser()
    rid, run, _ = _start(c)
    h.wait_for(run, "questions")
    c.post(f"/api/runs/{rid}/cancel")
    h.wait_for(run, "end", 10)
    web._prune_runs(now=time.time() + web.ENDED_TTL_S + 1)
    assert rid not in web.RUNS


# ------------------------------------------------------------ 협력적 취소 확인 지점 (실제 llm.create)
def test_real_llm_create_checks_guard_before_any_request(monkeypatch):
    def no_client():
        raise AssertionError("취소된 실행이 API 클라이언트에 도달하면 안 됨")
    monkeypatch.setattr(llm, "client", no_client)

    def in_context():
        llm.set_guard(lambda: "cancelled")
        with pytest.raises(llm.RunCancelled):
            llm.create(system="s", messages=[{"role": "user", "content": "합성"}])
    contextvars.copy_context().run(in_context)
    seen = []
    t = threading.Thread(target=lambda: seen.append(llm._guard_var.get()))  # 다른 실행·스레드에는 영향 없음
    t.start()
    t.join()
    assert seen == [None] and llm._guard_var.get() is None


def test_frozen_store_intact():
    assert store_patch.verify_frozen() == []


# ------------------------------------------------------------ HTTPS·프록시 신뢰 경계
LOCAL = {"base_url": "http://testserver", "client": ("127.0.0.1", 50000)}
REMOTE_HTTP = {"base_url": "http://testserver", "client": ("203.0.113.5", 50000)}


def _cookie_flags(resp) -> list[bool]:
    return ["secure" in c.lower() for c in _set_cookies(resp)]


def test_localhost_http_development_works_without_secure(fake):
    dev = h.browser(**LOCAL)
    r = dev.post("/api/runs", json=BODY, headers={"x-forwarded-proto": "https"})  # 클라이언트가 보낸 헤더는 무시
    assert r.status_code == 200 and _cookie_flags(r) == [False, False]
    assert "strict-transport-security" not in r.headers
    rid = r.json()["run_id"]
    _finish(dev, rid, web.RUNS[rid])  # 쿠키가 http에서도 오가므로 정상 흐름


def test_remote_plain_http_rejected_but_healthz_allowed(fake):
    remote = h.browser(**REMOTE_HTTP)
    for path in ("/", "/api/config", "/api/trials"):
        assert remote.get(path).status_code == 403
    r = remote.post("/api/runs", json=BODY, headers={"x-forwarded-proto": "https"})  # 헤더로 HTTPS인 척해도 거절
    assert r.status_code == 403 and r.json()["detail"] == "HTTPS로 접속하세요." and web.RUNS == {}
    assert remote.get("/healthz").status_code == 200


def test_https_sets_hsts_and_secure_cookies(fake):
    c = h.browser()
    r = c.post("/api/runs", json=BODY)
    assert r.headers["strict-transport-security"] == "max-age=31536000" and _cookie_flags(r) == [True, True]
    rid = r.json()["run_id"]
    _finish(c, rid, web.RUNS[rid])


def test_proxy_headers_trusted_only_from_trusted_proxy(fake):
    """uvicorn 프록시 미들웨어(배포와 같은 방식): 신뢰한 프록시의 X-Forwarded-Proto만 HTTPS로 인정."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app = ProxyHeadersMiddleware(web.app, trusted_hosts="10.0.0.1")
    spoof = {"x-forwarded-proto": "https", "x-forwarded-for": "127.0.0.1"}
    attacker = h.browser(app, base_url="http://testserver", client=("203.0.113.5", 1))
    assert attacker.get("/api/config", headers=spoof).status_code == 403  # 신뢰하지 않는 곳의 헤더 무시
    proxy = h.browser(app, base_url="http://testserver", client=("10.0.0.1", 1))
    r = proxy.post("/api/runs", json=BODY, headers={"x-forwarded-proto": "https", "x-forwarded-for": "198.51.100.7"})
    assert r.status_code == 200 and _cookie_flags(r) == [True, True] and "strict-transport-security" in r.headers
    web.RUNS[r.json()["run_id"]].cancel_requested.set()


def test_require_https_mode_has_no_exceptions(fake, monkeypatch):
    """운영 설정: 루프백·평문 허용 설정과 관계없이 HTTPS만 (모든 프록시를 신뢰해 루프백 주소를 꾸밀 수 있는 경우 대비)."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setenv("CTREC_REQUIRE_HTTPS", "1")
    monkeypatch.setenv("CTREC_ALLOW_INSECURE_HTTP", "1")
    assert h.browser(**LOCAL).get("/api/config").status_code == 403
    trust_all = ProxyHeadersMiddleware(web.app, trusted_hosts="*")
    spoofed = h.browser(trust_all, base_url="http://testserver", client=("203.0.113.5", 1))
    assert spoofed.get("/api/config", headers={"x-forwarded-for": "127.0.0.1"}).status_code == 403
    assert h.browser().get("/api/config").status_code == 200  # HTTPS는 허용


def test_explicit_insecure_opt_in_and_forced_secure(fake, monkeypatch):
    monkeypatch.setenv("CTREC_ALLOW_INSECURE_HTTP", "1")
    assert h.browser(**REMOTE_HTTP).get("/api/config").status_code == 200  # 명시적으로 켰을 때만
    monkeypatch.setenv("CTREC_COOKIE_SECURE", "1")
    r = h.browser(**LOCAL).post("/api/runs", json=BODY)
    assert _cookie_flags(r) == [True, True]
    web.RUNS[r.json()["run_id"]].cancel_requested.set()


def test_render_config_enforces_https():
    text = (Path(__file__).resolve().parents[1] / "render.yaml").read_text(encoding="utf-8")
    assert "--proxy-headers" in text and "--forwarded-allow-ips" in text
    for key in ("CTREC_REQUIRE_HTTPS", "CTREC_COOKIE_SECURE"):
        assert f'key: {key}\n        value: "1"' in text


# ------------------------------------------------------------ 결과 전송·취소 경합
def test_cancel_during_result_emission_is_rejected(fake, monkeypatch):
    """결과를 보내는 잠금 구간 안에 취소 요청이 들어와도: 취소 409, result 1개, end=completed."""
    c = h.browser()
    attempt = {}
    real_emit = web.Run.emit

    def emit(self, type_, **data):
        real_emit(self, type_, **data)
        if type_ == "result":  # 잠금을 쥔 상태에서 다른 스레드가 취소를 시도 (잠금이 풀린 뒤에 처리됨)
            th = threading.Thread(target=lambda: attempt.setdefault(
                "code", c.post(f"/api/runs/{self.run_id}/cancel").status_code))
            th.start()
            attempt["thread"] = th
    monkeypatch.setattr(web.Run, "emit", emit)
    rid, run, _ = _start(c)
    _finish(c, rid, run)
    attempt["thread"].join(10)
    events = _events(c, rid)
    assert attempt["code"] == 409 and run.state == "completed"
    assert [e["type"] for e in events].count("result") == 1 and events[-1]["state"] == "completed"


def test_result_and_cancel_race_invariant(fake):
    """여러 번 경합시켜도 'result가 있음' <=> 'end=completed', '취소 200' <=> 'end=cancelled'."""
    seen = set()
    for i in range(12):
        c = h.browser()
        rid, run, _ = _start(c)
        h.wait_for(run, "questions")
        codes = []

        def cancel(delay=0.002 * (i % 6)):
            time.sleep(delay)
            codes.append(c.post(f"/api/runs/{rid}/cancel").status_code)
        canceller = threading.Thread(target=cancel)
        canceller.start()
        c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "모름"}})
        canceller.join(10)
        h.wait_for(run, "end", 30)
        events = _events(c, rid)
        state = events[-1]["state"]
        has_result = any(e["type"] == "result" for e in events)
        assert has_result == (state == "completed"), (i, state)
        assert (codes == [200]) == (state == "cancelled"), (i, codes, state)
        assert [e["type"] for e in events].count("end") == 1
        seen.add(state)
    assert "cancelled" in seen  # 경합 중 최소 한 번은 취소가 이김 (모두 완료면 경합이 일어나지 않은 것)
