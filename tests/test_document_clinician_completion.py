"""문서 검증·임상의 판단 완료 경로 (TRIALOG O 후속). FastAPI TestClient로 실제 HTTP 경로(권한·CSRF·쿠키)를 거침.

- 합성 시험 전용 권한(CTREC_VERIFICATION_MODE=synthetic)만 씁니다. 운영 권한은 없으며 그 거부도 확인합니다.
- conftest가 모든 테스트에 테스트용 합성 문서 권한(document_support.AUTHORITY)을 켜 두지만, 서비스는 사례마다 권한을
  명시적으로 설정하므로 그 권한의 증명도 서비스 안에서는 무효여야 합니다 (아래 test_unverified_documents_never_confirm).
- 시험: 합성 NSCLC 기준표(sim.demo T-A..T-C) + 임상의 판단 기준 시험 T-D (sim.demo.expert_review_trial). 실제 환자·시험 아님.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402
from document_support import AUTHORITY as TEST_AUTHORITY  # noqa: E402
from test_followup_flow import STAFF, STAFF_KEY, TEXT, latest_profiler, staff_client  # noqa: E402

from ctrec import clinician_authority, document_authority, expert_boundary, pipeline  # noqa: E402
from ctrec import question_eval as qe  # noqa: E402
from ctrec.decision_state import build_state  # noqa: E402
from ctrec.fact_store import document_backed  # noqa: E402
from ctrec.followup import verification  # noqa: E402
from ctrec.followup.service import FollowupService, TrialCatalog  # noqa: E402
from ctrec.followup.store import CaseStore  # noqa: E402
from ctrec.sim import demo  # noqa: E402
from ctrec.web import app as web  # noqa: E402
from ctrec.web import followup_api  # noqa: E402

DOC_KEY = "synthetic-doc-verifier-key-0001"
CLIN_KEY = "synthetic-clinician-key-00000001"
SECRET = "synthetic-signing-secret-0000001"
VERIFIER = {"X-CTREC-Verifier-Key": DOC_KEY, "X-CTREC-Verifier-Id": "verifier-1"}
CLINICIAN = {"X-CTREC-Clinician-Key": CLIN_KEY, "X-CTREC-Clinician-Id": "dr-1"}
CE = "CONFIRMED_ELIGIBLE"
LAB_REPORT = b"SYNTHETIC LAB REPORT (not a real patient)\nPlatelets 180 x10^9/L\nANC 2.1 x10^9/L\nECOG 1\n"


def catalog(name: str = "sim-demo-nsclc") -> TrialCatalog:
    st = [*demo.trials(), demo.expert_review_trial()]
    return TrialCatalog(name=name, trials={t.parsed.trial_id: t.trial for t in st},
                        parsed={t.parsed.trial_id: t.parsed for t in st})


def synthetic_env(monkeypatch, mode: str | None = "synthetic", secret: str = SECRET) -> None:
    for name, value in (("CTREC_VERIFICATION_MODE", mode), ("CTREC_SYNTHETIC_DOC_VERIFIER_KEY", DOC_KEY),
                        ("CTREC_SYNTHETIC_CLINICIAN_KEY", CLIN_KEY), ("CTREC_SYNTHETIC_SIGNING_SECRET", secret)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "TEST_LLM_BACKEND", "fake")
    monkeypatch.setenv("CTREC_STAFF_KEY", STAFF_KEY)
    for name in ("CTREC_REQUIRE_HTTPS", "CTREC_ALLOW_INSECURE_HTTP", "CTREC_COOKIE_SECURE", "CTREC_SESSION"):
        monkeypatch.delenv(name, raising=False)
    synthetic_env(monkeypatch)
    with qe.OfflineEnvironment(3):
        pipeline.patient_profiler.profile_patient = latest_profiler
        root = tmp_path / "followup"
        followup_api.configure(FollowupService(CaseStore(root), catalog()))
        yield root
    followup_api.configure(None)


def restart(root: Path, name: str = "sim-demo-nsclc") -> FollowupService:
    svc = FollowupService(CaseStore(root), catalog(name))
    followup_api.configure(svc)
    return svc


# ---------------------------------------------------------------------------
# HTTP 도우미
# ---------------------------------------------------------------------------

def create(patient, trials, text=TEXT):
    r = patient.post("/api/followup/cases", json={"patient_text": text, "trial_ids": trials})
    assert r.status_code == 201, r.text
    return r.json()["case_id"]


def staff_case(client, cid):
    r = client.get(f"/api/followup/staff/cases/{cid}", headers=STAFF)
    assert r.status_code == 200, r.text
    return r.json()


def proposal(case, info_key, action_type):
    return next(p for p in case["proposals"] if p["info_key"] == info_key and p["action_type"] == action_type)


def make_requests(client, cid, *pids):
    r = client.post(f"/api/followup/staff/cases/{cid}/requests", headers=STAFF, json={"proposal_ids": list(pids)})
    assert r.status_code == 200, r.text
    return r.json()


def answer(patient, cid, rid, text):
    r = patient.post(f"/api/followup/cases/{cid}/requests/{rid}/response", json={"answer": text})
    assert r.status_code == 200, r.text
    return r.json()


def register(client, cid, content=LAB_REPORT, headers=STAFF, expect=201):
    r = client.post(f"/api/followup/staff/cases/{cid}/documents", headers=headers,
                    json={"kind": "lab_report", "media_type": "text/plain",
                          "content_base64": base64.b64encode(content).decode()})
    assert r.status_code == expect, r.text
    return r.json()


def verify(client, cid, rid, doc, items, headers=VERIFIER, expect=200, sha=None):
    r = client.post(f"/api/followup/verifier/cases/{cid}/requests/{rid}/verification", headers=headers,
                    json={"document_id": doc["document_id"], "document_sha256": sha or doc["sha256"], "items": items})
    assert r.status_code == expect, r.text
    return r.json()


def judge(client, cid, rid, decisions, headers=CLINICIAN, expect=200):
    # F-03: 판단은 임상의가 본 화면의 revision과 함께 제출 (제출 직전에 화면을 다시 봄)
    seen = client.get(f"/api/followup/clinician/cases/{cid}", headers=CLINICIAN).json().get("revision")
    r = client.post(f"/api/followup/clinician/cases/{cid}/requests/{rid}/judgment", headers=headers,
                    json={"decisions": decisions, "expected_revision": seen})
    assert r.status_code == expect, r.text
    return r.json()


def code(r) -> str:
    return r.json()["detail"]["code"]


LAB_TEXT = TEXT + "\nPlatelets 180 x10^9/L"  # 환자가 처음 입력에서 말한 검사값 (자기 보고)


def answered_lab_case(patient, staff):
    """T-A: 환자가 처음 입력에서 혈소판 값을 말함(자기 보고), ANC는 없음. 그 뒤 문서 검토 요청 (혈소판·ANC·ECOG).

    P 근거 경로(full_dcvc 0.2): 진료 기록 필요 기준(검사·ECOG)은 환자 질문이 아니라 처음부터 문서 확인으로 제안됨.
    (P 이전에는 혈소판·ANC를 환자에게 물어 자기 보고를 만든 뒤 문서로 확인했음)"""
    cid = create(patient, ["T-A", "T-D"], text=LAB_TEXT)
    case = staff_case(staff, cid)
    assert case["pairs"]["T-A"]["status"] == "INFO_MISSING"  # ANC 정보 없음
    assert not [p for p in case["proposals"] if p["action_type"] == "ASK_PATIENT" and p["info_key"].startswith("lab:")]
    docs = [p for p in case["proposals"] if p["action_type"] == "REQUEST_DOCUMENT" and p["target_trials"] == ["T-A"]]
    assert {p["info_key"] for p in docs} == {"lab:platelets", "lab:absolute neutrophil count", "ecog"}
    out = make_requests(staff, cid, *[p["proposal_id"] for p in docs])
    by_key = {r["info_key"]: r["request_id"] for r in out["case"]["requests"] if r["kind"] == "document_review"}
    return cid, by_key


def facts_for(client, cid, rid):
    v = client.get(f"/api/followup/verifier/cases/{cid}", headers=VERIFIER)
    assert v.status_code == 200, v.text
    return next(x for x in v.json()["requests"] if x["request_id"] == rid)["facts"]


# ---------------------------------------------------------------------------
# 1. 합성 증명 기반 문서 확인 -> 재평가 -> 결과 갱신 (+ 재시작 뒤 보존)
# ---------------------------------------------------------------------------

def test_document_verification_reevaluates_and_survives_restart(env):
    patient, staff, verifier = h.browser(), staff_client(), staff_client()
    cid, by_key = answered_lab_case(patient, staff)
    before = staff_case(staff, cid)
    assert before["pairs"]["T-A"]["status"] != CE
    doc = register(staff, cid)["document"]
    assert doc["sha256"] == hashlib.sha256(LAB_REPORT).hexdigest()

    # 검증자는 문서 내용을 받아 볼 수 있음 (해시 헤더 포함)
    r = verifier.get(f"/api/followup/verifier/cases/{cid}/documents/{doc['document_id']}/content", headers=VERIFIER)
    assert r.status_code == 200 and r.content == LAB_REPORT and r.headers["x-document-sha256"] == doc["sha256"]
    q = verifier.get("/api/followup/verifier/queue", headers=VERIFIER).json()["items"]
    assert {i["request_id"] for i in q} == set(by_key.values())

    # 혈소판: 환자 답 사실이 문서와 같음 (confirm). ANC: 문서 값을 새 사실로 (record). 어느 하나만으로는 확정되지 않음
    plt = [f for f in facts_for(verifier, cid, by_key["lab:platelets"]) if f["field"] == "lab:platelets"]
    assert len(plt) == 1 and plt[0]["source_type"] == "self_report" and not plt[0]["document_verified"]
    out = verify(verifier, cid, by_key["lab:platelets"], doc, [{"fact_id": plt[0]["fact_id"]}])
    assert out["request"]["status"] == "document_verified"
    assert out["verification"]["items"] == [{"fact_id": plt[0]["fact_id"], "field": "lab:platelets", "mode": "confirm"}]
    assert out["pairs"]["T-A"]["status"] != CE
    verify(verifier, cid, by_key["lab:absolute neutrophil count"], doc,
           [{"field": "lab:absolute neutrophil count", "value": 2.1, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    ecog = [f for f in facts_for(verifier, cid, by_key["ecog"]) if f["field"] == "ecog"]
    out = verify(verifier, cid, by_key["ecog"], doc, [{"fact_id": ecog[0]["fact_id"]}])
    assert out["pairs"]["T-A"]["status"] == CE  # 필수 기준이 모두 근거와 함께 충족
    assert {"trial_id": "T-A", "before": "VERIFICATION_PENDING", "after": CE} in out["transitions"]

    case = staff_case(staff, cid)
    ev = case["evaluations"][-1]
    assert ev["trigger"]["type"] == "document_verification" and ev["equivalence"]["status"] == "match"
    assert "T-A" in ev["reevaluated_trials"]
    assert "T-A" in [x["trial_id"] for x in case["view"]["result"]["recommendation"]["ranked"]]
    assert not [p for p in case["proposals"] if p["action_type"] == "REQUEST_DOCUMENT" and p["target_trials"] == ["T-A"]]
    assert all(r["status"] != "requires_document" for r in case["requests"] if r["kind"] == "document_review")
    # Inspector와 Cohort C 대기열에도 반영
    insp = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=STAFF).json()
    assert len(insp["verification"]["verifications"]) == 3 and insp["verification"]["authority"]["active"]
    assert len(insp["verification"]["document_verified_facts"]) == 3
    queue = staff.get("/api/followup/staff/queue", headers=STAFF).json()
    assert not [i for i in queue["items"] if i["action_type"] == "REQUEST_DOCUMENT" and i["target_criteria"][0].startswith("T-A:")]
    # 환자 화면: 결과는 보이나 검증자 정보·문서는 없음
    pv = patient.get(f"/api/followup/cases/{cid}").json()
    assert pv["pairs"]["T-A"]["status"] == CE and "verifications" not in pv and "documents" not in pv

    # 서버 재시작 (같은 설정): 증명·판정·감사 기록 유지
    restart(env)
    after = staff_case(staff, cid)
    for k in ("revision", "requests", "evaluations", "pairs", "verifications", "documents"):
        assert after[k] == case[k], k
    assert not after["authority_changed"]
    audit = staff.get(f"/api/followup/staff/cases/{cid}/audit", headers=STAFF).json()
    assert audit["intact"] is True
    kinds = [e["event"]["kind"] for e in audit["entries"]]
    assert kinds.count("document_verified") == 3 and "document_registered" in kinds and "evaluation_recorded" in kinds
    verified = [e["event"] for e in audit["entries"] if e["event"]["kind"] == "document_verified"]
    assert {e["by"] for e in verified} == {"verifier:verifier-1"}
    # 저장된 판정이 같은 권한 아래 전체 재평가와 같음
    svc = followup_api.service()
    data = svc.store.load(cid)
    s = svc._restore(data)
    with svc._scope(data, s):
        stored = {t: m.eligibility for t, m in s.matches.items()}
        svc._match(s, list(s.matches))
        assert {t: m.eligibility for t, m in s.matches.items()} == stored

    # 서명 비밀이 다른 서버: 저장된 증명은 인정되지 않고 확정이 보이지 않음 (실패 시 닫힘)
    import os
    os.environ["CTREC_SYNTHETIC_SIGNING_SECRET"] = "another-signing-secret-000000001"  # monkeypatch가 테스트 뒤 복구
    restart(env)
    changed = staff_case(staff, cid)
    assert changed["authority_changed"] and changed["pairs"]["T-A"]["status"] != CE
    assert patient.get(f"/api/followup/cases/{cid}").json()["pairs"]["T-A"]["status"] != CE
    summary = staff.get(f"/api/followup/staff/cases/{cid}/verification", headers=STAFF).json()
    assert summary["document_verified_facts"] == [] and len(summary["verification_status_only_facts"]) == 3
    # 운영 모드(설정 없음)로 다시 띄워도 같음
    os.environ.pop("CTREC_VERIFICATION_MODE")
    restart(env)
    assert staff_case(staff, cid)["pairs"]["T-A"]["status"] != CE


# ---------------------------------------------------------------------------
# 2. 검증되지 않은 문서·상태 문자열·다른 권한의 증명은 확정 근거가 아님
# ---------------------------------------------------------------------------

def test_unverified_documents_never_confirm(env):
    patient, staff = h.browser(), staff_client()
    cid, by_key = answered_lab_case(patient, staff)
    svc = followup_api.service()
    data = svc.store.load(cid)

    def pair_status(d):
        """사례 권한 아래에서 전체 재매칭 -> (T-A 쌍 상태, 문서 확인으로 인정된 사실이 있는가)."""
        s = svc._restore(d)
        with svc._scope(d, s):
            svc._match(s, list(s.matches))
            _, co = svc._cohort(s, d["alias"], fsvc_ops(d))
            return svc._pairs(co)["T-A"]["status"], any(document_backed(f) for f in s.fact_store.facts(s.case.patient_id))

    # (a) 상태 문자열 위조: 저장된 사실의 verification_status를 document_verified로 바꾸고 문서 참조를 넣음
    forged = json.loads(json.dumps(data))
    for f in forged["session"]["facts"]:
        f["verification_status"] = "document_verified"
        f["verification_history"].append({"from_status": "unknown", "to_status": "document_verified", "reason": "forged",
                                          "by": "staff:x", "document_reference": "DOC-0000000000000000",
                                          "attestation": None, "at": "2026-11-07T00:00:00+00:00"})
    status, backed = pair_status(forged)
    assert status != CE and not backed

    # (b) 테스트 전용 합성 권한(conftest)이 발급한 증명: 서비스의 사례 권한은 인정하지 않음
    s = svc._restore(data)
    for f in s.fact_store.facts(s.case.patient_id):
        s.fact_store.attest(f.fact_id, TEST_AUTHORITY.issue(f), TEST_AUTHORITY, reason="other authority", by="test")
    other = {**data, "session": fsvc_dump(s)}
    status, backed = pair_status(other)
    assert status != CE and not backed
    assert any(document_backed(f) for f in s.fact_store.facts(s.case.patient_id))  # 같은 증명이 테스트 권한에서는 유효

    # (c) 등록되지 않은 문서·다른 서명 비밀로 만든 증명
    reg = {**data, "documents": [{"document_id": "DOC-00000000000000aa", "sha256": "1" * 64}]}
    s = svc._restore(reg)
    wrong = verification.SyntheticCaseDocumentAuthority(b"wrong-key", cid, {"DOC-00000000000000aa": "1" * 64})
    for f in s.fact_store.facts(s.case.patient_id):
        s.fact_store.attest(f.fact_id, wrong.issue(f, "DOC-00000000000000aa", "verifier-1"), wrong, reason="x", by="x")
    status, backed = pair_status({**reg, "session": fsvc_dump(s)})
    assert status != CE and not backed

    # (d) 문서를 등록만 하고 검증하지 않음 -> 판정 변화 없음
    staff_before = staff_case(staff, cid)
    doc = register(staff, cid)["document"]
    after = staff_case(staff, cid)
    assert after["pairs"] == staff_before["pairs"] and doc["document_id"]

    # (e) 연구 담당자 상태 변경 API로 완료 상태를 만들 수 없음
    rid = by_key["lab:platelets"]
    for status_text in ("document_verified", "clinician_decided", "completed"):
        r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid}/status", headers=STAFF,
                       json={"status": status_text})
        assert r.status_code == 422, (status_text, r.text)

    # (f) 해시가 다른 문서, 요청 범위 밖 항목, 다른 환자의 사실 ID
    verifier = staff_client()
    plt = [f for f in facts_for(verifier, cid, rid) if f["field"] == "lab:platelets"][0]
    assert code(verifier.post(f"/api/followup/verifier/cases/{cid}/requests/{rid}/verification", headers=VERIFIER,
                              json={"document_id": doc["document_id"], "document_sha256": "2" * 64,
                                    "items": [{"fact_id": plt["fact_id"]}]})) == "document_changed"
    r = verifier.post(f"/api/followup/verifier/cases/{cid}/requests/{rid}/verification", headers=VERIFIER,
                      json={"document_id": doc["document_id"], "document_sha256": doc["sha256"],
                            "items": [{"field": "lab:hemoglobin", "value": 13.0, "unit": "g/dL"}]})
    assert r.status_code == 409 and code(r) == "field_out_of_scope"
    r = verifier.post(f"/api/followup/verifier/cases/{cid}/requests/{rid}/verification", headers=VERIFIER,
                      json={"document_id": doc["document_id"], "document_sha256": doc["sha256"],
                            "items": [{"fact_id": "F-000000000000"}]})
    assert r.status_code == 404 and code(r) == "fact_not_found"
    # 디스크의 문서 내용이 바뀜 -> 검증 거절
    path = env / "documents" / cid / doc["document_id"]
    path.write_bytes(b"tampered")
    r = verifier.post(f"/api/followup/verifier/cases/{cid}/requests/{rid}/verification", headers=VERIFIER,
                      json={"document_id": doc["document_id"], "document_sha256": doc["sha256"],
                            "items": [{"fact_id": plt["fact_id"]}]})
    assert r.status_code == 409 and code(r) == "document_integrity"
    assert staff_case(staff, cid)["pairs"]["T-A"]["status"] != CE
    audit = staff.get(f"/api/followup/staff/cases/{cid}/audit", headers=STAFF).json()
    denied = [e["event"]["code"] for e in audit["entries"] if e["event"]["kind"] == "denied"]
    assert {"document_changed", "field_out_of_scope", "fact_not_found", "document_integrity"} <= set(denied)


def fsvc_ops(d):
    from ctrec.followup import codec
    return codec.load_ops(d["ops"])


def fsvc_dump(s):
    from ctrec.followup import codec
    return codec.dump_session(s)


# ---------------------------------------------------------------------------
# 3. 권한 분리: 권한 없는 검증·임상 판단 거부, 역할 교차 사용 거부, 환자 답 대리 입력 거부
# ---------------------------------------------------------------------------

def test_roles_are_separated_and_unauthorized_completion_is_refused(env, monkeypatch):
    patient, staff, other = h.browser(), staff_client(), staff_client()
    cid, by_key = answered_lab_case(patient, staff)
    doc = register(staff, cid)["document"]
    rid = by_key["lab:platelets"]
    fact = [f for f in facts_for(other, cid, rid) if f["field"] == "lab:platelets"][0]
    item = [{"fact_id": fact["fact_id"]}]
    clin_req = next(r for r in staff_case(staff, cid)["requests"] if r["kind"] == "clinician_review")

    # 검증자 키 없음·틀림·연구 담당자 키·임상의 키 -> 403 (연구 담당자 키 + 검증자 키를 함께 보내면 400)
    assert verify(other, cid, rid, doc, item, headers={}, expect=403)["detail"]["code"] == "verifier_forbidden"
    verify(other, cid, rid, doc, item, headers={**VERIFIER, "X-CTREC-Verifier-Key": "x" * 20}, expect=403)
    verify(other, cid, rid, doc, item, headers=STAFF, expect=403)
    verify(other, cid, rid, doc, item, headers={"X-CTREC-Verifier-Key": CLIN_KEY, "X-CTREC-Verifier-Id": "dr-1"},
           expect=403)
    assert verify(other, cid, rid, doc, item, headers={**VERIFIER, **STAFF}, expect=400)["detail"]["code"] == "multiple_roles"
    assert other.get(f"/api/followup/verifier/cases/{cid}", headers=CLINICIAN).status_code == 403
    assert other.get(f"/api/followup/verifier/cases/{cid}/documents/{doc['document_id']}/content",
                     headers=STAFF).status_code == 403
    # 임상의 판단: 검증자·연구 담당자 키 -> 403
    decision = [{"criterion": "T-D:I2", "decision": "MET", "rationale_code": "life_expectancy_adequate"}]
    for hdr in ({}, VERIFIER, STAFF, {"X-CTREC-Clinician-Key": DOC_KEY, "X-CTREC-Clinician-Id": "v"}):
        r = judge(other, cid, clin_req["request_id"], decision, headers=hdr, expect=403)
        assert r["detail"]["code"] == "clinician_forbidden"
    # 검증자가 임상 검토 요청을, 임상의가 문서 검토 요청을 완료할 수 없음 (경로 자체가 역할별)
    assert verify(other, cid, clin_req["request_id"], doc, item, expect=409)["detail"]["code"] == "not_document_review"
    assert judge(other, cid, rid, decision, expect=409)["detail"]["code"] == "not_clinician_review"
    # CSRF 헤더 없음
    r = other.post(f"/api/followup/verifier/cases/{cid}/requests/{rid}/verification",
                   headers={**VERIFIER, "X-CTREC-Request": "0"},
                   json={"document_id": doc["document_id"], "document_sha256": doc["sha256"], "items": item})
    assert r.status_code == 403
    # 환자 답 대리 입력: 검증자·임상의 헤더가 붙은 응답 요청도 거절 (기존 O 계약)
    pending = make_requests(staff, cid, proposal(staff_case(staff, cid), "lab:hemoglobin", "ASK_PATIENT")["proposal_id"]
                            ) if any(p["info_key"] == "lab:hemoglobin" and p["action_type"] == "ASK_PATIENT"
                                     for p in staff_case(staff, cid)["proposals"]) else None
    target = pending["created"][0] if pending and pending["created"] else rid
    for hdr in (VERIFIER, CLINICIAN, STAFF):
        r = patient.post(f"/api/followup/cases/{cid}/requests/{target}/response", headers=hdr, json={"answer": "x"})
        assert r.status_code == 403 and code(r) == "staff_cannot_answer"
    assert staff_case(staff, cid)["pairs"]["T-A"]["status"] != CE
    assert all(r["status"] == "requires_document" for r in staff_case(staff, cid)["requests"]
               if r["kind"] == "document_review")

    # 설정 문제: 역할 키가 같음 / 운영 모드 / 설정 없음 -> 503 (완료 없음)
    monkeypatch.setenv("CTREC_SYNTHETIC_CLINICIAN_KEY", DOC_KEY)
    assert verify(other, cid, rid, doc, item, expect=503)["detail"]["code"] == "verification_misconfigured"
    monkeypatch.setenv("CTREC_SYNTHETIC_CLINICIAN_KEY", STAFF_KEY)
    assert verify(other, cid, rid, doc, item, expect=503)["detail"]["code"] == "verification_misconfigured"
    synthetic_env(monkeypatch, mode="production")
    assert verify(other, cid, rid, doc, item, expect=503)["detail"]["code"] == "production_authority_unavailable"
    assert judge(other, cid, clin_req["request_id"], decision, expect=503)["detail"]["code"] == \
        "production_authority_unavailable"
    synthetic_env(monkeypatch, mode=None)
    assert verify(other, cid, rid, doc, item, expect=503)["detail"]["code"] == "verification_not_configured"
    assert register(staff, cid, expect=503)["detail"]["code"] == "verification_not_configured"
    assert other.get("/api/followup/clinician/queue", headers=CLINICIAN).status_code == 503
    assert staff_case(staff, cid)["pairs"]["T-A"]["status"] != CE


def test_synthetic_authority_refuses_real_trial_store_cases(env):
    """고정 시험 저장소 카탈로그(synthetic=False) 사례: 합성 권한으로 문서 등록·검증·판단을 할 수 없음 (403, 감사 기록)."""
    restart(env, name="trial_store")
    patient, staff, other = h.browser(), staff_client(), staff_client()
    cid = create(patient, ["T-A", "T-D"])
    case = staff_case(staff, cid)
    assert case["synthetic"] is False
    assert register(staff, cid, expect=403)["detail"]["code"] == "synthetic_authority_real_case"
    clin = next(r for r in case["requests"] if r["kind"] == "clinician_review")
    r = judge(other, cid, clin["request_id"], [{"criterion": "T-D:I2", "decision": "MET", "rationale_code": "x"}],
              expect=403)
    assert r["detail"]["code"] == "synthetic_authority_real_case"
    audit = staff.get(f"/api/followup/staff/cases/{cid}/audit", headers=STAFF).json()
    assert [e["event"]["code"] for e in audit["entries"] if e["event"]["kind"] == "denied"] == \
        ["synthetic_authority_real_case"] * 2
    assert staff_case(staff, cid)["pairs"]["T-D"]["status"] == "EXPERT_REVIEW"


# ---------------------------------------------------------------------------
# 4. 임상의 판단: 별도 권한·상태, 판단 기준만, 사실이 바뀌면 다시 판단 대기
# ---------------------------------------------------------------------------

def test_clinician_judgment_flow_and_staleness(env):
    patient, staff, clinician = h.browser(), staff_client(), staff_client()
    cid = create(patient, ["T-A", "T-D"])
    case = staff_case(staff, cid)
    assert case["pairs"]["T-D"]["status"] == "EXPERT_REVIEW"
    reqs = [r for r in case["requests"] if r["kind"] == "clinician_review"]
    assert [(r["target_criteria"], r["status"], r["created_by"], r["reason_code"]) for r in reqs] == \
        [(["T-D:I2"], "requires_clinician", "system", "clinician_judgment_required")]
    rid = reqs[0]["request_id"]
    view = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=CLINICIAN).json()
    item = next(x for x in view["requests"] if x["request_id"] == rid)
    assert item["judgeable"] and item["criteria"][0]["clinician_reason"] == "underspecified"
    assert item["criteria"][0]["text"].startswith("Life expectancy")
    assert item["criteria"][0]["required_evidence"] == "clinical_record"  # 범주 other: 진료 기록 필요 (evidence.py)

    # 일부 기준만, 잘못된 기준 -> 거절
    assert judge(clinician, cid, rid, [{"criterion": "T-A:I1", "decision": "MET", "rationale_code": "x"}],
                 expect=409)["detail"]["code"] == "judgment_incomplete"
    # 진료 기록이 필요한 기준: 근거 없음·문서로 확인되지 않은 사실만 -> 거절 (임상의 판단이 문서 요건을 대신하지 않음)
    met = {"criterion": "T-D:I2", "decision": "MET", "rationale_code": "life_expectancy_adequate"}
    assert judge(clinician, cid, rid, [met], expect=409)["detail"]["code"] == "basis_not_document_verified"
    ecog = next(f for f in view["facts"] if f["field"] == "ecog")
    assert not ecog["document_verified"]
    assert judge(clinician, cid, rid, [{**met, "basis_fact_ids": [ecog["fact_id"]]}],
                 expect=409)["detail"]["code"] == "basis_not_document_verified"
    assert judge(clinician, cid, rid, [{**met, "basis_fact_ids": ["F-000000000000"]}],
                 expect=404)["detail"]["code"] == "fact_not_found"
    # ECOG를 문서로 검증 (문서 검증자) -> 그 사실을 근거로 판단
    doc_req = make_requests(staff, cid, proposal(case, "ecog", "REQUEST_DOCUMENT")["proposal_id"])["created"][0]
    doc = register(staff, cid)["document"]
    verify(staff_client(), cid, doc_req, doc, [{"fact_id": ecog["fact_id"]}])
    assert staff_case(staff, cid)["pairs"]["T-D"]["status"] == "EXPERT_REVIEW"  # 문서 확인은 임상의 판단이 아님
    out = judge(clinician, cid, rid, [{**met, "basis_fact_ids": [ecog["fact_id"]]}])
    assert out["request"]["status"] == "clinician_decided"
    assert out["request"]["history"][-1]["by"] == "clinician:dr-1"
    assert out["pairs"]["T-D"]["status"] == CE
    assert {"trial_id": "T-D", "before": "EXPERT_REVIEW", "after": CE} in out["transitions"]
    j = out["judgments"][0]
    assert j["clinician"] == "synthetic-clinician:dr-1" and j["decision"] == "MET"
    assert j["basis_fact_ids"] == [ecog["fact_id"]]
    judge(clinician, cid, rid, [{**met, "basis_fact_ids": [ecog["fact_id"]]}], expect=409)  # 이미 처리됨

    # 판정 계층: 경계 불변식이 판단을 위반으로 보지 않고, 판정 주체가 clinician으로 남음
    svc = followup_api.service()
    data = svc.store.load(cid)
    s = svc._restore(data)
    with svc._scope(data, s):
        parsed = s.parsed["T-D"]
        a = next(x for x in s.matches["T-D"].assessments if x.rule_id == "I2")
        assert a.method == "clinician" and a.passes is True
        assert expert_boundary.violations(s.matches["T-D"], parsed) == []
        _, co = svc._cohort(s, data["alias"], fsvc_ops(data))
        res = next(c for p in co.pairs() if p.pair.trial_id == "T-D" for c in p.criteria if c.rule_id == "I2")
        assert res.adjudication == "expert" and res.decided_by == "clinician" and res.state == "MET"
    # 권한 밖(기본 NoClinicianAuthority)에서는 같은 저장 판정이 위반이자 UNKNOWN
    assert expert_boundary.violations(s.matches["T-D"], parsed) == ["I2"]
    st = build_state(FollowupService._view(s))
    c = next(c for c in st.trials["T-D"].criteria if c.rule_id == "I2")
    assert c.status == "UNKNOWN" and c.gap == "physician_review"

    # 재시작 뒤 유지
    restart(env)
    after = staff_case(staff, cid)
    assert after["pairs"]["T-D"]["status"] == CE and after["judgments"] == staff_case(staff, cid)["judgments"]

    # 환자 사실이 바뀜 (문서 검증자가 문서의 새 검사값을 기록) -> 이전 판단은 인정되지 않음 -> 다시 EXPERT_REVIEW
    # + 새 임상 검토 요청. P 근거 경로 뒤 검사값은 환자 질문이 아니라 문서 확인으로만 들어옴
    anc = proposal(after, "lab:absolute neutrophil count", "REQUEST_DOCUMENT")
    q = make_requests(staff, cid, anc["proposal_id"])["created"][0]
    verify(staff_client(), cid, q, doc, [{"field": "lab:absolute neutrophil count", "value": 2.1, "unit": "x10^9/L",
                                          "observed_at": "2026-10-01"}])
    case = staff_case(staff, cid)
    assert case["pairs"]["T-D"]["status"] == "EXPERT_REVIEW"
    open_reviews = [r for r in case["requests"] if r["kind"] == "clinician_review" and r["status"] == "requires_clinician"]
    assert len(open_reviews) == 1 and open_reviews[0]["request_id"] != rid
    view = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=CLINICIAN).json()
    assert [x["recognition"] for x in view["judgments"]] == ["stale"]
    # 새 판단: NOT_MET -> 확정 부적격 쪽으로 (근거: 임상의)
    out = judge(clinician, cid, open_reviews[0]["request_id"],
                [{"criterion": "T-D:I2", "decision": "NOT_MET", "rationale_code": "life_expectancy_short",
                  "basis_fact_ids": [ecog["fact_id"]]}])
    assert out["pairs"]["T-D"]["eligibility"] == "INELIGIBLE" and out["pairs"]["T-D"]["status"] != CE
    audit = staff.get(f"/api/followup/staff/cases/{cid}/audit", headers=STAFF).json()
    assert audit["intact"] and [e["event"]["kind"] for e in audit["entries"]].count("clinician_judged") == 2


def test_clinician_cannot_close_document_or_patient_criteria(env):
    """출처가 모두 소진된 검사 항목의 임상 검토 요청: 임상의 판단 기준이 아니므로 판단으로 확정하지 않음."""
    patient, staff, clinician = h.browser(), staff_client(), staff_client()
    cid = create(patient, ["T-A"])
    case = staff_case(staff, cid)
    # P 근거 경로: 혈소판(진료 기록 필요)은 처음부터 문서 확인 요청
    make_requests(staff, cid, proposal(case, "lab:platelets", "REQUEST_DOCUMENT")["proposal_id"])
    for _ in range(6):  # 문서 정보원을 모두 소진 -> 임상 검토
        case = staff_case(staff, cid)
        open_doc = [x for x in case["requests"] if x["kind"] == "document_review" and x["status"] == "requires_document"]
        if not open_doc:
            break
        staff.post(f"/api/followup/staff/cases/{cid}/requests/{open_doc[0]['request_id']}/status", headers=STAFF,
                   json={"status": "document_unavailable"})
    review = next(x for x in staff_case(staff, cid)["requests"] if x["kind"] == "clinician_review")
    assert review["target_criteria"] == ["T-A:I3"]
    view = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=CLINICIAN).json()
    assert not next(x for x in view["requests"] if x["request_id"] == review["request_id"])["judgeable"]
    out = judge(clinician, cid, review["request_id"], [{"criterion": "T-A:I3", "decision": "MET", "rationale_code": "x"}],
                expect=409)
    assert out["detail"]["code"] == "not_clinician_criterion"
    assert staff_case(staff, cid)["pairs"]["T-A"]["status"] != CE


# ---------------------------------------------------------------------------
# 5. 감사 기록: 연결 해시, 변조 감지
# ---------------------------------------------------------------------------

def test_audit_chain_detects_tampering(env):
    patient, staff = h.browser(), staff_client()
    cid = create(patient, ["T-A", "T-D"])
    register(staff, cid)
    audit = staff.get(f"/api/followup/staff/cases/{cid}/audit", headers=STAFF).json()
    assert audit["intact"] and [e["seq"] for e in audit["entries"]] == list(range(len(audit["entries"])))
    kinds = [e["event"]["kind"] for e in audit["entries"]]
    assert kinds[:2] == ["case_created", "evaluation_recorded"] and kinds[-1] == "document_registered"
    path = env / "audit" / f"{cid}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["event"]["by"] = "staff:someone-else"
    path.write_text("\n".join([json.dumps(first, ensure_ascii=False, sort_keys=True), *lines[1:]]) + "\n",
                    encoding="utf-8")
    broken = staff.get(f"/api/followup/staff/cases/{cid}/audit", headers=STAFF).json()
    assert broken["intact"] is False and broken["broken_at"] == 0


# ---------------------------------------------------------------------------
# 6. 판정 계층 단위: 권한이 인정하지 않은 판단은 쓰이지 않음
# ---------------------------------------------------------------------------

def test_expert_boundary_uses_only_recognized_judgments():
    t = demo.expert_review_trial()
    rule = next(r for r in t.parsed.rules if r.rule_id == "I2")
    from ctrec.schemas import CriterionAssessment
    a = CriterionAssessment(rule_id="I2", kind="inclusion", criterion_text=rule.text, holds="yes", passes=True,
                            confidence="high", evidence="x", reasoning="llm", missing_info=None, method="llm",
                            underspecified=True)
    assert expert_boundary.enforce("T-D", rule, a).passes is None  # 기본: 판단 권한 없음

    versions = iter(["v1", "v1", "v2"])
    auth = verification.SyntheticCaseClinicianAuthority(b"k" * 32, "FC-" + "0" * 32, [], {"T-D": t.parsed},
                                                       lambda: next(versions))
    j = auth.issue(trial_id="T-D", rule_id="I2", decision="NOT_MET", rationale_code="short", clinician_id="dr")
    auth.judgments.append(j)
    with clinician_authority.use(auth):
        out = expert_boundary.enforce("T-D", rule, a)
        assert out.passes is False and out.method == "clinician" and clinician_authority.judged_id(out) == j.judgment_id
        assert expert_boundary.enforce("T-D", rule, a).passes is None  # 사실 버전이 바뀜 -> 인정 안 함
    forged = j.model_copy(update={"decision": "MET"})  # 서명은 그대로, 내용만 바꿈
    auth2 = verification.SyntheticCaseClinicianAuthority(b"k" * 32, "FC-" + "0" * 32, [forged.model_dump()],
                                                        {"T-D": t.parsed}, lambda: "v1")
    with clinician_authority.use(auth2):
        assert expert_boundary.enforce("T-D", rule, a).passes is None
    # 임상의 판단 기준이 아닌 기준에는 판단이 있어도 적용하지 않음
    age = next(r for r in t.parsed.rules if r.rule_id == "I1")
    b = a.model_copy(update={"rule_id": "I1", "underspecified": False, "passes": None, "holds": "unknown"})
    auth3 = verification.SyntheticCaseClinicianAuthority(b"k" * 32, "FC-" + "0" * 32, [], {"T-D": t.parsed}, lambda: "v")
    auth3.judgments.append(auth3.issue(trial_id="T-D", rule_id="I1", decision="MET", rationale_code="x", clinician_id="d"))
    with clinician_authority.use(auth3):
        assert expert_boundary.enforce("T-D", age, b) is b
    assert isinstance(document_authority.current(), type(TEST_AUTHORITY))  # conftest 기본값은 그대로


def test_document_value_differing_from_answer_holds_and_opens_conflict(env):
    """문서의 값이 환자 답과 다름: 문서 값을 새 사실로 기록하면 같은 항목의 충돌 -> 보류·충돌 확인 요청 (어느 값도 채택하지 않음)."""
    patient, staff, verifier = h.browser(), staff_client(), staff_client()
    cid, by_key = answered_lab_case(patient, staff)
    doc = register(staff, cid, content=b"SYNTHETIC LAB REPORT\nPlatelets 90 x10^9/L\n")["document"]
    out = verify(verifier, cid, by_key["lab:platelets"], doc,
                 [{"field": "lab:platelets", "value": 90.0, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    assert out["pairs"]["T-A"]["status"] != CE and out["pairs"]["T-A"]["eligibility"] != "ELIGIBLE"
    case = staff_case(staff, cid)
    conflict = [r for r in case["requests"] if r["kind"] == "conflict_resolution"]
    assert len(conflict) == 1 and conflict[0]["conflict"]["field"] == "lab:platelets"
    assert "conflict" in case["pairs"]["T-A"]["gaps"]
    # 충돌 확인은 문서 검증으로 닫지 않음
    r = verify(verifier, cid, conflict[0]["request_id"], doc, [{"field": "lab:platelets", "value": 90.0}], expect=409)
    assert r["detail"]["code"] == "conflict_not_closed_by_document"
