"""감사 F-03: 임상의 판단이 임상의가 보지 못한 사실에 묶이던 것.

임상의 화면을 본 뒤 새 검증 사실(ANC)이 들어오고, 임상의가 expected_revision 없이 판단을 제출하면 판단이 새 사실 버전에
묶여 확정 적격까지 갈 수 있었음. 판단 제출에는 본 화면의 revision이 필요하고, 그 사이 바뀌었으면 409 stale_revision.
합성 데이터, LLM 대역.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
sys.path.insert(0, str(Path(__file__).parent))

import test_document_clinician_completion as D  # noqa: E402

env = D.env
CE = D.CE
MET = {"criterion": "T-D:I2", "decision": "MET", "rationale_code": "life_expectancy_adequate"}


def _seen_then_unseen_fact(patient, staff, verifier, clinician):
    """ECOG·혈소판 문서 확인 -> 임상의가 화면을 봄 -> 그 뒤 ANC 사실이 새로 들어옴 (임상의는 보지 못함)."""
    cid, by_key = D.answered_lab_case(patient, staff)
    doc = D.register(staff, cid)["document"]
    plt = [f for f in D.facts_for(verifier, cid, by_key["lab:platelets"]) if f["field"] == "lab:platelets"]
    D.verify(verifier, cid, by_key["lab:platelets"], doc, [{"fact_id": plt[0]["fact_id"]}])
    ecog = [f for f in D.facts_for(verifier, cid, by_key["ecog"]) if f["field"] == "ecog"]
    D.verify(verifier, cid, by_key["ecog"], doc, [{"fact_id": ecog[0]["fact_id"]}])
    cv = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=D.CLINICIAN).json()
    rq = next(r for r in cv["requests"] if r["judgeable"])
    ef = next(f for f in cv["facts"] if f["field"] == "ecog" and f["document_verified"])["fact_id"]
    D.verify(verifier, cid, by_key["lab:absolute neutrophil count"], doc,
             [{"field": "lab:absolute neutrophil count", "value": 0.4, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    return cid, cv, rq["request_id"], [{**MET, "basis_fact_ids": [ef]}]


def _post(clinician, cid, rid, body):
    return clinician.post(f"/api/followup/clinician/cases/{cid}/requests/{rid}/judgment", headers=D.CLINICIAN, json=body)


def test_judgment_without_revision_after_unseen_fact_is_refused(env):
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, _, rid, dec = _seen_then_unseen_fact(patient, staff, verifier, clinician)
    r = _post(clinician, cid, rid, {"decisions": dec})
    assert r.status_code == 428, r.text[:300]
    assert r.json()["detail"]["code"] == "revision_required"
    case = D.staff_case(staff, cid)
    assert case["pairs"]["T-D"]["status"] != CE and not case.get("judgments")
    assert next(x for x in case["requests"] if x["request_id"] == rid)["status"] == "requires_clinician"


def test_judgment_with_seen_revision_after_unseen_fact_is_stale(env):
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, cv, rid, dec = _seen_then_unseen_fact(patient, staff, verifier, clinician)
    r = _post(clinician, cid, rid, {"decisions": dec, "expected_revision": cv["revision"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_revision"
    assert D.staff_case(staff, cid)["pairs"]["T-D"]["status"] != CE


def test_judgment_after_reviewing_current_facts_is_recorded(env):
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, _, rid, dec = _seen_then_unseen_fact(patient, staff, verifier, clinician)
    cv = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=D.CLINICIAN).json()  # 새 사실을 본 화면
    r = _post(clinician, cid, rid, {"decisions": dec, "expected_revision": cv["revision"]})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["request"]["status"] == "clinician_decided"


# ---------------------------------------------------------------------------
# 접근 제어가 약화되지 않음: revision 검사(428)는 역할·권한 확인 뒤에만 일어남
# ---------------------------------------------------------------------------

def test_missing_revision_does_not_bypass_or_precede_role_checks(env):
    """revision 없는 판단이라도 임상의가 아닌 요청은 428이 아니라 403 clinician_forbidden (판정 불변)."""
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, _, rid, dec = _seen_then_unseen_fact(patient, staff, verifier, clinician)
    other = D.staff_client()
    for hdr in ({}, D.VERIFIER, D.STAFF, {"X-CTREC-Clinician-Key": D.DOC_KEY, "X-CTREC-Clinician-Id": "v"}):
        r = other.post(f"/api/followup/clinician/cases/{cid}/requests/{rid}/judgment", headers=hdr,
                       json={"decisions": dec})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "clinician_forbidden", r.text[:300]
    case = D.staff_case(staff, cid)
    assert case["pairs"]["T-D"]["status"] != CE and not case.get("judgments")


def test_missing_revision_in_production_mode_is_503_not_428(env, monkeypatch):
    """운영 설정(권한 없음)에서는 revision 유무와 상관없이 503: 설정 문제가 revision 오류로 가려지지 않음."""
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, _, rid, dec = _seen_then_unseen_fact(patient, staff, verifier, clinician)
    D.synthetic_env(monkeypatch, mode="production")
    r = _post(clinician, cid, rid, {"decisions": dec})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "production_authority_unavailable"


def test_missing_revision_refusal_is_audited_and_changes_nothing(env):
    """428 거절은 감사 기록에 denied로 남고 revision·요청 상태·쌍 상태를 바꾸지 않음."""
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, _, rid, dec = _seen_then_unseen_fact(patient, staff, verifier, clinician)
    before = D.staff_case(staff, cid)
    assert _post(clinician, cid, rid, {"decisions": dec}).status_code == 428
    after = D.staff_case(staff, cid)
    assert after["revision"] == before["revision"] and after["pairs"] == before["pairs"]
    assert after["requests"] == before["requests"]
    audit = staff.get(f"/api/followup/staff/cases/{cid}/audit", headers=D.STAFF).json()
    assert audit["intact"]
    denied = [e["event"] for e in audit["entries"] if e["event"]["kind"] == "denied"]
    assert any(e["code"] == "revision_required" for e in denied)
