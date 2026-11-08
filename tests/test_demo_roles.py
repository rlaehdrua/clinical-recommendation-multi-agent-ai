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


def test_patient_sees_what_the_team_is_checking_without_values(env):
    patient = h.browser()
    cid = D.create(patient, ["T-A", "T-D"])
    c = D.staff_client()
    case = D.staff_case(c, cid)
    D.make_requests(c, cid, D.proposal(case, "lab:absolute neutrophil count", "REQUEST_DOCUMENT")["proposal_id"])
    d = patient.get(f"/api/followup/cases/{cid}/requests").json()
    work = d["in_progress"]
    assert {"kind": "document_review", "info_key": "lab:absolute neutrophil count", "status": "requires_document"} in work
    assert any(w["kind"] == "clinician_review" for w in work)  # T-D 의사 판단 (서버가 자동으로 만든 요청)
    assert all(set(w) == {"kind", "info_key", "status"} for w in work)  # 값·문서·판단 내용은 환자에게 보내지 않음
    assert all("info_key" in r for r in d["requests"])
    listed = c.get("/api/followup/staff/cases", headers=D.STAFF).json()["cases"]
    assert next(x for x in listed if x["case_id"] == cid)["open_by_status"]["requires_document"] == 1



def test_exhausted_lab_review_is_shown_as_on_hold_not_waiting_for_a_clinician(env):
    """서류를 모두 구할 수 없어 생긴 임상 검토(item_sources_exhausted)는 의사가 끝낼 수 없음 -> 할 일·'판단 중'이 아니라 on_hold."""
    patient = h.browser()
    cid = D.create(patient, ["T-A"])
    c = D.staff_client()
    D.make_requests(c, cid, D.proposal(D.staff_case(c, cid), "lab:platelets", "REQUEST_DOCUMENT")["proposal_id"])
    for _ in range(6):
        open_doc = [x for x in D.staff_case(c, cid)["requests"]
                    if x["kind"] == "document_review" and x["status"] == "requires_document"]
        if not open_doc:
            break
        c.post(f"/api/followup/staff/cases/{cid}/requests/{open_doc[0]['request_id']}/status", headers=D.STAFF,
               json={"status": "document_unavailable"})
    review = next(x for x in D.staff_case(c, cid)["requests"] if x["kind"] == "clinician_review")
    assert review["reason_code"] == "item_sources_exhausted" and review["status"] == "requires_clinician"  # 저장 상태는 그대로
    listed = c.get("/api/followup/staff/cases", headers=D.STAFF).json()["cases"]
    counts = next(x for x in listed if x["case_id"] == cid)["open_by_status"]
    assert counts.get("on_hold") == 1 and "requires_clinician" not in counts
    work = patient.get(f"/api/followup/cases/{cid}/requests").json()["in_progress"]
    assert {"kind": "clinician_review", "info_key": "lab:platelets", "status": "on_hold"} in work
    assert not any(w["status"] == "requires_clinician" for w in work)

def test_demo_server_keeps_the_original_examples_after_the_nsclc_demo_patients():
    import importlib.util
    spec = importlib.util.spec_from_file_location("demo_server", Path(__file__).parents[1] / "scripts" / "followup_demo_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._ORIGINAL_PATIENTS = lambda: [{"patient_id": "S001"}]
    ids = [p["patient_id"] for p in mod.demo_patients()]
    assert ids[:4] == ["DEMO-NSCLC-1", "DEMO-NSCLC-2", "DEMO-NSCLC-3", "DEMO-NSCLC-4"] and ids[4:] == ["S001"]
