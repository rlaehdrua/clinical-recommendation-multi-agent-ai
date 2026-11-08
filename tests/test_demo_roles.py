"""발표용 데모 서버에서 한 화면으로 검증자·임상의 역할을 보여 주는 경로 (X-CTREC-Demo-Role)와 질문별 '따로 요청' 비용.

데모 예외는 DEMO_STAFF(데모 서버 스크립트만 켬) + 루프백 + 합성 검증 모드일 때만. 역할은 섞이지 않음. 합성 데이터만."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sys.path.insert(0, str(Path(__file__).parent))

import test_document_clinician_completion as D  # noqa: E402
import web_harness as h  # noqa: E402
from ctrec.web import followup_api  # noqa: E402

env = D.env
V, C = {"X-CTREC-Demo-Role": "verifier"}, {"X-CTREC-Demo-Role": "clinician"}


def _loopback():
    return h.browser(base_url="http://127.0.0.1", client=("127.0.0.1", 50100))


def _doc_request(c, cid):
    case = c.get(f"/api/followup/staff/cases/{cid}").json()
    pid = D.proposal(case, "lab:absolute neutrophil count", "REQUEST_DOCUMENT")["proposal_id"]
    out = c.post(f"/api/followup/staff/cases/{cid}/requests", json={"proposal_ids": [pid]}).json()
    return out["created"][0]


def test_demo_roles_need_the_demo_server_and_loopback(env, monkeypatch):
    cid = D.create(h.browser(), ["T-A", "T-D"])
    c = _loopback()
    assert c.get(f"/api/followup/verifier/cases/{cid}", headers=V).status_code == 403  # DEMO_STAFF 꺼짐
    monkeypatch.setattr(followup_api, "DEMO_STAFF", True)
    assert c.get("/api/followup/staff/session").json()["demo_roles"] == ["verifier", "clinician"]
    assert c.get(f"/api/followup/verifier/cases/{cid}", headers=V).status_code == 200
    assert c.get(f"/api/followup/clinician/cases/{cid}", headers=C).status_code == 200
    outside = D.staff_client()  # 루프백이 아니면 데모 예외 없음
    assert outside.get(f"/api/followup/verifier/cases/{cid}", headers=V).status_code == 403


def test_demo_roles_stay_separate(env, monkeypatch):
    cid = D.create(h.browser(), ["T-A", "T-D"])
    monkeypatch.setattr(followup_api, "DEMO_STAFF", True)
    c = _loopback()
    assert c.get(f"/api/followup/clinician/cases/{cid}", headers=V).status_code == 403  # 검증자 표시로 임상의 불가
    assert c.get(f"/api/followup/verifier/cases/{cid}", headers=C).status_code == 403
    assert c.get("/api/followup/staff/cases", headers=V).status_code == 403  # 역할 표시가 있으면 담당자 데모 예외도 없음
    both = {**V, "X-CTREC-Verifier-Key": D.DOC_KEY, "X-CTREC-Verifier-Id": "v"}
    assert c.get(f"/api/followup/verifier/cases/{cid}", headers={**both, "X-CTREC-Staff-Key": "x"}).status_code == 400
    rid = _doc_request(c, cid)
    r = c.post(f"/api/followup/cases/{cid}/requests/{rid}/response", json={"answer": "2.1"}, headers=V)
    assert r.status_code == 403 and D.code(r) == "staff_cannot_answer"


def test_demo_verifier_records_a_document_value_with_a_demo_name(env, monkeypatch):
    cid = D.create(h.browser(), ["T-A", "T-D"])
    monkeypatch.setattr(followup_api, "DEMO_STAFF", True)
    c = _loopback()
    rid = _doc_request(c, cid)
    doc = c.post(f"/api/followup/staff/cases/{cid}/documents", json={"kind": "lab_report", "media_type": "text/plain",
                 "content_base64": "U1lOVEhFVElDIEFOQyAyLjE="}).json()["document"]
    out = D.verify(c, cid, rid, doc, [{"field": "lab:absolute neutrophil count", "value": 2.1, "unit": "x10^9/L",
                                       "observed_at": "2026-10-01"}], headers=V)
    assert out["request"]["status"] == "document_verified"
    assert out["verification"]["verifier"] == "synthetic-document-verifier:demo-verifier"


def test_demo_roles_are_off_without_synthetic_verification(env, monkeypatch):
    D.synthetic_env(monkeypatch, mode=None)
    cid = D.create(h.browser(), ["T-A"])
    monkeypatch.setattr(followup_api, "DEMO_STAFF", True)
    c = _loopback()
    assert c.get("/api/followup/staff/session").json()["demo_roles"] == []
    assert c.get(f"/api/followup/verifier/cases/{cid}", headers=V).status_code == 503


def test_insight_shows_the_cost_of_asking_one_by_one(env):
    for _ in range(2):
        D.create(h.browser(), ["T-A", "T-B", "T-C"])
    ins = D.staff_client().get("/api/followup/staff/insight", headers=D.STAFF).json()
    qs, s = ins["questions"], ins["summary"]
    assert qs and all(q["minutes_alone"] >= q["minutes"] for q in qs)
    assert all(p["minutes_alone"] >= p["minutes"] for q in qs for p in q["patients"])
    assert s["minutes_all_alone"] >= s["minutes_all"] and s["spent_minutes"] == 0
    labs = [q for q in qs if q["info_key"].startswith("lab:")]
    assert labs and {q["source_id"] for q in labs} == {"lab_report"}
    assert sum(q["minutes_alone"] for q in labs) > sum(q["minutes"] for q in labs)  # 같은 결과지로 묶으면 연락 비용이 한 번
