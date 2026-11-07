"""야간 V4: Follow-up 경로 상태 안정성 (실제 HTTP 경로, 합성 시험·합성 권한, LLM 대역).

- 매 변경 뒤 저장된 판정·쌍 상태 == 저장소에서 복원해 전체 재평가한 결과 (평가 기록의 equivalence와 독립 확인)
- 문서 1건(ECOG)이 여러 시험의 기준을 함께 갱신
- 감사 기록은 앞부분을 고치지 않고 덧붙기만 함, Cohort C 대기열 = 사례 제안 (매 변경 뒤)
- 서버 재시작 뒤 같은 상태, 두 사례 사이 사실·문서 혼입 없음, 요청 상태·중복 요청·반복 응답, 권한 철회 뒤 재평가
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
sys.path.insert(0, str(Path(__file__).parent))

import test_document_clinician_completion as D  # noqa: E402
import test_q_state_contract as Q  # noqa: E402

from ctrec.followup import codec  # noqa: E402

env = D.env
CE = "CONFIRMED_ELIGIBLE"
TRIALS = ["T-A", "T-B", "T-C", "T-D"]
TEXT_OTHER = ("Synthetic NSCLC patient. 45-year-old female with stage IV non-small cell lung cancer. ECOG 0.\n"
              "Platelets 90 x10^9/L")


def _full_signature(m) -> tuple:
    keys = ("rule_id", "passes", "holds", "method", "needs_verification", "applicable", "underspecified",
            "validation_flags", "required_evidence", "evidence_source")
    rows = lambda xs: tuple(sorted(tuple(str(a.model_dump().get(k)) for k in keys) for a in xs))  # noqa: E731
    return (m.eligibility, m.match_status, m.matched_cohort, rows(m.assessments), rows(m.other_cohort_assessments))


def stored_vs_full(cid: str) -> None:
    """저장된 판정·쌍 상태가 복원본 전체 재평가와 같은지 (같은 사례 권한 아래)."""
    svc = D.followup_api.service()
    data = svc.store.load(cid)
    s = svc._restore(data)
    with svc._scope(data, s):
        stored = {t: _full_signature(m) for t, m in s.matches.items()}
        svc._match(s, list(s.matches))
        full = {t: _full_signature(m) for t, m in s.matches.items()}
        assert full == stored
        pairs = svc._pairs(svc._cohort(s, data["alias"], codec.load_ops(data["ops"]))[1])
    assert {t: p["status"] for t, p in pairs.items()} == {t: p["status"] for t, p in data["current"]["pairs"].items()}


def audit_entries(staff, cid) -> list[dict]:
    a = staff.get(f"/api/followup/staff/cases/{cid}/audit", headers=D.STAFF).json()
    assert a["intact"] is True
    assert [e["seq"] for e in a["entries"]] == list(range(len(a["entries"])))
    return a["entries"]


def queue_ids(staff, cid) -> list[str]:
    items = staff.get("/api/followup/staff/queue", headers=D.STAFF).json().get("items", [])
    return [i["proposal_id"] for i in items if i["case_id"] == cid]


class Tracker:
    """단계마다: 증분==전체, 감사 기록 덧붙기만, 대기열 == 사례 제안 순서."""

    def __init__(self, staff, cid):
        self.staff, self.cid, self.audit = staff, cid, audit_entries(staff, cid)

    def step(self, label: str, *, evaluated: bool = True) -> dict:
        case = D.staff_case(self.staff, self.cid)
        if evaluated:
            ev = case["evaluations"][-1]
            ok = ("match", "initial", "not_applicable") if ev["trigger"]["type"] == "staff_status" else ("match", "initial")
            assert ev["equivalence"]["status"] in ok, label
        stored_vs_full(self.cid)
        entries = audit_entries(self.staff, self.cid)
        assert entries[:len(self.audit)] == self.audit, f"{label}: 감사 기록 앞부분이 바뀜"
        self.audit = entries
        assert queue_ids(self.staff, self.cid) == [p["proposal_id"] for p in case["proposals"]], label
        return case


def _doc_requests(staff, cid, keys):
    case = D.staff_case(staff, cid)
    pids = [D.proposal(case, k, "REQUEST_DOCUMENT")["proposal_id"] for k in keys]
    out = D.make_requests(staff, cid, *pids)
    return {r["info_key"]: r["request_id"] for r in out["case"]["requests"] if r["kind"] == "document_review"}


def test_sequence_incremental_equals_full_audit_and_queue_then_restart(env):
    patient, staff, verifier = D.h.browser(), D.staff_client(), D.staff_client()
    cid = D.create(patient, TRIALS, text=D.LAB_TEXT)
    t = Tracker(staff, cid)
    t.step("intake")
    by_key = _doc_requests(staff, cid, ["lab:platelets", "lab:absolute neutrophil count", "ecog", "lab:hemoglobin"])
    t.step("requests created", evaluated=False)
    doc = D.register(staff, cid)["document"]
    t.step("document registered", evaluated=False)

    plt = [f for f in D.facts_for(verifier, cid, by_key["lab:platelets"]) if f["field"] == "lab:platelets"]
    D.verify(verifier, cid, by_key["lab:platelets"], doc, [{"fact_id": plt[0]["fact_id"]}])
    t.step("platelets confirmed")
    D.verify(verifier, cid, by_key["lab:absolute neutrophil count"], doc,
             [{"field": "lab:absolute neutrophil count", "value": 2.1, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    t.step("anc recorded")

    # 문서 1건의 ECOG 확인이 세 시험의 기준을 함께 갱신 (묶음 요청)
    ecog = [f for f in D.facts_for(verifier, cid, by_key["ecog"]) if f["field"] == "ecog"]
    out = D.verify(verifier, cid, by_key["ecog"], doc, [{"fact_id": ecog[0]["fact_id"]}])
    case = t.step("ecog confirmed")
    ev = case["evaluations"][-1]
    assert ev["trigger"]["type"] == "document_verification"
    assert {"T-A", "T-B", "T-C"} <= set(ev["reevaluated_trials"])
    assert out["pairs"]["T-A"]["status"] == CE
    req = next(r for r in case["requests"] if r["request_id"] == by_key["ecog"])
    assert set(req["target_criteria"]) == {"T-A:I2", "T-B:I2", "T-C:I3"}
    assert not [p for p in case["proposals"] if p["info_key"] == "ecog"]  # 묶음의 모든 기준에서 같은 확인이 빠짐

    # 문서 없음 -> 대체 경로, 판정은 그대로
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{by_key['lab:hemoglobin']}/status", headers=D.STAFF,
                   json={"status": "document_unavailable"})
    assert r.status_code == 200, r.text
    case = t.step("hemoglobin unavailable")

    # 재시작: 사례·대기열·감사 기록 그대로
    queue_before = staff.get("/api/followup/staff/queue", headers=D.STAFF).json()
    D.restart(env)
    after = D.staff_case(staff, cid)
    for k in ("revision", "requests", "responses", "evaluations", "pairs", "proposals", "verifications", "documents"):
        assert after[k] == case[k], k
    assert staff.get("/api/followup/staff/queue", headers=D.STAFF).json() == queue_before
    assert audit_entries(staff, cid) == t.audit
    stored_vs_full(cid)


def test_two_cases_do_not_share_facts_documents_or_state(env):
    patient1, patient2, staff, verifier = D.h.browser(), D.h.browser(client=("198.51.100.22", 50003)), \
        D.staff_client(), D.staff_client()
    c1 = D.create(patient1, ["T-A", "T-D"], text=D.LAB_TEXT)
    c2 = D.create(patient2, ["T-A", "T-D"], text=TEXT_OTHER)
    svc = D.followup_api.service()
    d2_before = svc.store.load(c2)
    s2 = svc._restore(d2_before)
    facts2 = {(f.fact_id, f.field, str(f.value)) for f in s2.fact_store.facts(s2.case.patient_id)}

    by1 = _doc_requests(staff, c1, ["lab:platelets", "ecog"])
    doc1 = D.register(staff, c1)["document"]
    plt1 = [f for f in D.facts_for(verifier, c1, by1["lab:platelets"]) if f["field"] == "lab:platelets"][0]
    D.verify(verifier, c1, by1["lab:platelets"], doc1, [{"fact_id": plt1["fact_id"]}])

    # 사례 2는 그대로 (저장본·사실·쌍 상태)
    d2 = svc.store.load(c2)
    assert d2["revision"] == d2_before["revision"] and d2["current"] == d2_before["current"]
    s2 = svc._restore(d2)
    assert {(f.fact_id, f.field, str(f.value)) for f in s2.fact_store.facts(s2.case.patient_id)} == facts2
    s1 = svc._restore(svc.store.load(c1))
    assert s1.case.patient_id != s2.case.patient_id
    ids1 = {f.fact_id for f in s1.fact_store.all_facts()} if hasattr(s1.fact_store, "all_facts") else \
        {f.fact_id for f in s1.fact_store.facts(s1.case.patient_id)}
    assert not ids1 & {x[0] for x in facts2}
    assert all(f.patient_id == s2.case.patient_id for f in s2.fact_store.facts(s2.case.patient_id))
    assert not s2.fact_store.facts(s1.case.patient_id)  # 사례 2 저장소에 사례 1 환자 사실 없음
    assert {f.field: f.value for f in s2.fact_store.facts(s2.case.patient_id) if f.field == "lab:platelets"} == \
        {"lab:platelets": 90.0}

    # 사례 2 요청으로 사례 1의 사실·문서를 쓸 수 없음
    by2 = _doc_requests(staff, c2, ["lab:platelets"])
    r = verifier.post(f"/api/followup/verifier/cases/{c2}/requests/{by2['lab:platelets']}/verification",
                      headers=D.VERIFIER, json={"document_id": doc1["document_id"], "document_sha256": doc1["sha256"],
                                                "items": [{"fact_id": plt1["fact_id"]}]})
    assert r.status_code == 404 and D.code(r) == "document_not_found"
    doc2 = D.register(staff, c2)["document"]
    r = verifier.post(f"/api/followup/verifier/cases/{c2}/requests/{by2['lab:platelets']}/verification",
                      headers=D.VERIFIER, json={"document_id": doc2["document_id"], "document_sha256": doc2["sha256"],
                                                "items": [{"fact_id": plt1["fact_id"]}]})
    assert r.status_code == 404 and D.code(r) == "fact_not_found"
    # 사례 1의 문서 증명은 사례 2의 판정에 쓰이지 않음
    assert D.staff_case(staff, c2)["pairs"]["T-A"]["status"] != CE
    v2 = verifier.get(f"/api/followup/verifier/cases/{c2}", headers=D.VERIFIER).json()
    assert plt1["fact_id"] not in {f["fact_id"] for x in v2["requests"] for f in x["facts"]}
    # 대기열: 사례별 항목 순서 = 그 사례의 제안 순서
    for cid in (c1, c2):
        assert queue_ids(staff, cid) == [p["proposal_id"] for p in D.staff_case(staff, cid)["proposals"]]
        stored_vs_full(cid)


def test_request_status_duplicates_and_repeated_responses(env):
    import test_followup_flow as F
    patient, staff = D.h.browser(), D.staff_client()
    cid = D.create(patient, ["T-A", "T-B", "T-C"], text=F.TEXT_NO_AGE)
    t = Tracker(staff, cid)
    case = D.staff_case(staff, cid)
    age = D.proposal(case, "age", "ASK_PATIENT")
    hb = D.proposal(case, "lab:hemoglobin", "REQUEST_DOCUMENT")
    first = D.make_requests(staff, cid, age["proposal_id"], hb["proposal_id"])
    rid_age, rid_hb = first["created"]
    again = D.make_requests(staff, cid, age["proposal_id"], hb["proposal_id"])
    assert again["created"] == [] and again["existing"] == [rid_age, rid_hb]
    assert again["case"]["revision"] == first["case"]["revision"]  # 중복 요청은 저장하지 않음
    t.step("requests", evaluated=False)

    a = D.answer(patient, cid, rid_age, "I am 64 years old")
    assert a["status"] == "answered"
    case = t.step("answered")
    svc = D.followup_api.service()
    s = svc._restore(svc.store.load(cid))
    n_facts = len(s.fact_store.facts(s.case.patient_id))
    for _ in range(2):  # 같은 답 반복: 중복, 사실·평가·감사 기록 늘지 않음
        dup = D.answer(patient, cid, rid_age, "I am 64 years old")
        assert dup["duplicate"] is True
    r = patient.post(f"/api/followup/cases/{cid}/requests/{rid_age}/response", json={"answer": "I am 71"})
    assert r.status_code == 409
    after = t.step("duplicates", evaluated=False)
    assert after["revision"] == case["revision"] and after["evaluations"] == case["evaluations"]
    s = svc._restore(svc.store.load(cid))
    assert len(s.fact_store.facts(s.case.patient_id)) == n_facts
    # 이미 답한 질문은 다시 만들 수 없음, 답한 요청은 상태 변경 불가
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=D.STAFF,
                   json={"proposal_ids": [age["proposal_id"]]})
    assert r.status_code == 409
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid_age}/status", headers=D.STAFF,
                   json={"status": "cancelled"})
    assert r.status_code == 409 and D.code(r) == "transition_not_allowed"

    # 문서 요청 취소 -> 다시 취소 불가, 같은 제안으로 새 요청은 새 ID (취소된 요청은 그대로 기록)
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid_hb}/status", headers=D.STAFF,
                   json={"status": "cancelled"})
    assert r.status_code == 200, r.text
    t.step("cancelled")
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid_hb}/status", headers=D.STAFF,
                   json={"status": "cancelled"})
    assert r.status_code == 409
    case = D.staff_case(staff, cid)
    hb_now = [p for p in case["proposals"] if p["info_key"] == "lab:hemoglobin" and p["action_type"] == "REQUEST_DOCUMENT"]
    if hb_now:
        out = D.make_requests(staff, cid, hb_now[0]["proposal_id"])
        assert out["created"] and out["created"][0] != rid_hb
        reqs = {r["request_id"]: r["status"] for r in out["case"]["requests"]}
        assert reqs[rid_hb] == "cancelled" and reqs[out["created"][0]] == "requires_document"
    t.step("recreated", evaluated=False)


def test_reevaluation_after_authority_withdrawal_stays_consistent(env):
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    assert {t: p["status"] for t, p in D.staff_case(staff, cid)["pairs"].items()} == {"T-A": CE, "T-D": CE}
    os.environ["CTREC_SYNTHETIC_SIGNING_SECRET"] = "another-signing-secret-000000001"  # monkeypatch가 테스트 뒤 복구
    D.restart(env)
    t = Tracker(staff, cid)
    case = D.staff_case(staff, cid)
    assert case["authority_changed"] and CE not in {p["status"] for p in case["pairs"].values()}
    # 철회 뒤 새 권한으로 ANC를 다시 문서 확인 -> 재평가 기록이 지금 권한 기준이고 전체 재평가와 같음
    # (혈소판·ECOG는 철회 뒤 제안 자체가 없음: 보고서 V4-B1. 여기서는 제안이 있는 ANC로 재평가 일관성만 확인)
    key = "lab:absolute neutrophil count"
    anc = next(p for p in case["proposals"] if p["info_key"] == key and p["action_type"] == "REQUEST_DOCUMENT")
    rid = D.make_requests(staff, cid, anc["proposal_id"])["created"][0]
    doc = D.register(staff, cid)["document"]
    f = [x for x in D.facts_for(verifier, cid, rid) if x["field"] == key and not x["document_verified"]]
    out = D.verify(verifier, cid, rid, doc, [{"fact_id": f[0]["fact_id"]}])
    case = t.step("re-verified after withdrawal")
    ev = case["evaluations"][-1]
    assert CE not in {p["status"] for p in ev["pairs"].values()}  # ANC·ECOG 증명과 임상의 판단은 여전히 무효
    assert CE not in {p["status"] for p in out["pairs"].values()}
    assert {t_: p["status"] for t_, p in case["pairs"].items()} == {t_: p["status"] for t_, p in ev["pairs"].items()}
    insp = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=D.STAFF).json()
    assert all(x.get("eligibility") != "ELIGIBLE" for x in insp["trials"])
