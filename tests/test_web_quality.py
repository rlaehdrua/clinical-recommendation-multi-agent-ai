"""웹서비스 품질·보안 (Phase 6-2): 결과 표시 분류의 판정 불변, 오류 원문 비노출, 보안 헤더·정적 자원, 반입 라이브러리 무결성.

브라우저 E2E는 test_web_browser.py (CTREC_BROWSER_TESTS=1). 유료 호출 없음.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402

from ctrec.agents import matcher  # noqa: E402
from ctrec.schemas import CriterionAssessment  # noqa: E402
from ctrec.web import app as web, display  # noqa: E402

STATIC = Path(web.__file__).parent / "static"
CSP_DIRECTIVES = ("default-src 'none'", "script-src 'self'", "style-src 'self'", "img-src 'self' data:", "font-src 'self'",
                  "connect-src 'self'", "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'", "object-src 'none'")


def _a(**kw):
    base = dict(rule_id="R1", kind="inclusion", criterion_text="t", holds="unknown", passes=None, confidence="low",
                evidence="근거 없음", reasoning="r", missing_info=None, method="llm")
    base.update(kw)
    return CriterionAssessment(**base)


# ---------------------------------------------------------------- 표시 분류 = 서버 판정 함수


@pytest.mark.parametrize("kw, group", [
    ({"passes": True, "holds": "yes"}, display.MET),
    ({"passes": False, "holds": "no"}, display.NOT_MET),
    ({}, display.MISSING),
    ({"needs_verification": True, "self_reported_holds": "yes"}, display.RECORD_CHECK),
    ({"underspecified": True}, display.PHYSICIAN),
    ({"category": "consent_or_logistics"}, display.PROCEDURAL),
    ({"category": "consent_or_logistics", "kind": "exclusion"}, display.MISSING),  # 제외 기준 절차는 면제 아님 (서버 규칙)
])
def test_group_follows_matcher_predicates(kw, group):
    a = _a(**kw)
    assert display.group_of(a) == group
    # 서버 함수와 같은 결론
    assert (group == display.MISSING) == matcher.is_askable_unknown(a)
    assert (group == display.PROCEDURAL) == matcher.is_waivable_logistics(a)
    assert (group == display.RECORD_CHECK) == matcher.needs_record_check(a)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    h.install(monkeypatch, tmp_path)
    return h.browser()


def _result(client, fake=None):
    rid = client.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}).json()["run_id"]
    run = web.RUNS[rid]
    q = None
    for _ in range(2):
        try:
            q = h.wait_for(run, "questions", 5)
            break
        except AssertionError:
            pass
    if q:
        client.post(f"/api/runs/{rid}/answers", json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}})
    h.wait_for(run, "end", 30)
    return next(e for e in run.events if e["type"] == "result")


def test_display_payload_consistent_and_judgments_unchanged(client):
    ev = _result(client)
    d, res = ev["display"], ev["result"]
    rec_ids = {r["trial_id"] for r in res["recommendation"]["ranked"]}
    excluded = {x["trial_id"] for x in res["recommendation"]["excluded"]}
    for tid, m in res["matches"].items():
        t = d["trials"][tid]
        assert set(t["groups"]) == {a["rule_id"] for a in m["assessments"]}  # 모든 기준이 분류됨
        for a in m["assessments"]:
            g = t["groups"][a["rule_id"]]
            assert (g == "met") == (a["passes"] is True) and (g == "not_met") == (a["passes"] is False)
        assert sum(t["counts"].values()) == len(m["assessments"])
        assert (t["exclusion"] is None) == (tid in rec_ids)
        if tid in excluded:
            assert t["exclusion"] in ("not_evaluated", "closed", "demo", "mismatch", "unknown")
    # 표시 정보는 판정·추천을 바꾸지 않음: 오류가 없는 결과는 화면용 사본도 원본과 같음 - 단 판정 불가 기준의 일반 문구
    # missing_info만 원문 기준 문장으로 보여 줌 (P14, display_missing_info). 그 밖의 필드는 모두 같아야 함
    again = display.redact_errors(copy.deepcopy(res))
    assert again["recommendation"] == res["recommendation"] and set(again["matches"]) == set(res["matches"])
    for tid, m in res["matches"].items():
        strip = lambda x: {**x, "assessments": [{k: v for k, v in a.items() if k != "missing_info"} for a in x["assessments"]],
                           "other_cohort_assessments": [{k: v for k, v in a.items() if k != "missing_info"}
                                                        for a in x.get("other_cohort_assessments") or []]}
        assert strip(again["matches"][tid]) == strip(m)
        for a, b in zip(m["assessments"], again["matches"][tid]["assessments"]):
            assert b["missing_info"] == display.display_missing_info(a)["missing_info"]
            assert b["missing_info"] == a["missing_info"] or b["missing_info"].endswith(a["criterion_text"][:300])


def test_error_detail_never_sent_to_browser(monkeypatch, tmp_path):
    secret = "비밀-요청-내용-0000"
    h.install(monkeypatch, tmp_path, h.FakeLLM(fail_on="MatcherOutput", fail_message=secret))
    c = h.browser()
    ev = _result(c)
    sent = json.dumps(ev, ensure_ascii=False)
    assert secret not in sent  # 결과·표시 정보 어디에도 오류 원문 없음
    for tid, m in ev["result"]["matches"].items():
        assert m["eligibility"] == "NOT_EVALUATED" and m["match_status"] == "NOT_EVALUATED"  # 판정은 그대로
        assert m["evaluation_error"] == "RuntimeError" and ev["display"]["trials"][tid]["error_type"] == "RuntimeError"
    assert all(e["error"] == "RuntimeError" for e in ev["result"]["errors"])
    stream = c.get(f"/api/runs/{next(iter(web.RUNS))}/events").text
    assert secret not in stream


def test_redact_errors_keeps_judgments():
    res = {"matches": {"T1": {"eligibility": "NOT_EVALUATED", "match_status": "NOT_EVALUATED", "assessments": [],
                              "evaluation_error": "TruncatedError('환자 원문 일부')", "summary": "NOT_EVALUATED: ... 환자 원문 일부"},
                       "T2": {"eligibility": "UNCERTAIN", "match_status": "INSUFFICIENT_INFORMATION", "assessments": [],
                              "evaluation_error": None, "summary": "UNCERTAIN: 통과 1"}},
           "errors": [{"item": "T1", "error": "TruncatedError('환자 원문 일부')"}],
           "recommendation": {"ranked": [], "excluded": [{"trial_id": "T1", "reason": "평가 실패 원인: TruncatedError('환자 원문 일부')"}]}}
    out = display.redact_errors(copy.deepcopy(res))
    assert "환자 원문" not in json.dumps(out, ensure_ascii=False)
    assert out["matches"]["T1"]["eligibility"] == "NOT_EVALUATED" and out["matches"]["T2"] == res["matches"]["T2"]
    assert out["recommendation"]["excluded"][0]["trial_id"] == "T1"


# ---------------------------------------------------------------- 보안 헤더·정적 자원


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/app.css", "/api/config", "/api/runs/nope", "/static/nope.js"])
def test_security_headers_on_every_response(client, path):
    r = client.get(path)
    csp = r.headers["content-security-policy"]
    for d in CSP_DIRECTIVES:
        assert d in csp, d
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "http" not in csp  # 외부 출처 없음
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"


def test_security_headers_on_rejected_plain_http():
    from fastapi.testclient import TestClient
    c = TestClient(web.app, base_url="http://example.org", client=("198.51.100.30", 1))  # 루프백 밖 평문 HTTP -> 403
    r = c.get("/")
    assert r.status_code == 403 and "default-src 'none'" in r.headers["content-security-policy"]


@pytest.mark.parametrize("name", ["app.py", "..%2Fapp.py", "VENDOR.md", "LICENSE-marked.md", "index.html", "x.js"])
def test_static_route_serves_only_listed_files(client, name):
    assert client.get(f"/static/{name}").status_code == 404


def test_static_files_types():
    for name, mime in web.STATIC_FILES.items():
        assert (STATIC / name).is_file(), name
    c = h.browser()
    assert c.get("/static/purify.min.js").headers["content-type"].startswith("text/javascript")


# ---------------------------------------------------------------- 반입 라이브러리·인라인 금지


def _vendor_table():
    rows = {}
    for line in (STATIC / "VENDOR.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\| `([\w.]+\.js)` \| (\S+) \| ([\d.]+) \|.*\| `([0-9a-f]{64})` \|$", line)
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3), m.group(4))
    return rows


def test_vendored_files_match_recorded_hashes_and_licenses():
    rows = _vendor_table()
    assert set(rows) == {"marked.min.js", "purify.min.js"}
    for name, (pkg, ver, sha) in rows.items():
        data = (STATIC / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha, name  # 반입 뒤 바뀌지 않음
        assert ver in data[:400].decode("utf-8", "replace")  # 파일 머리의 버전 표기와 기록이 같음
    for lic in ("LICENSE-marked.md", "LICENSE-dompurify.txt", "LICENSE-dompurify-MPL.txt"):
        assert (STATIC / lic).stat().st_size > 500


def test_no_cdn_or_inline_code():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']https?://""", html)  # 외부 자원 없음
    assert "cdn." not in html + js + css and "@import" not in css
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)  # 인라인 스크립트 없음
    assert "<style" not in html and " style=" not in html and not re.search(r"\son[a-z]+=", html)
    assert " style=" not in js and not re.search(r"""\son[a-z]+=["'`$]""", js)  # 화면 템플릿에도 인라인 style·이벤트 없음
    assert "eval(" not in js and "new Function" not in js and "innerHTML = ev" not in js
