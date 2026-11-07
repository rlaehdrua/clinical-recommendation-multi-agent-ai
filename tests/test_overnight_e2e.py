"""야간 V3: HTTP API 사용자 흐름 E2E (FastAPI TestClient). 각 시나리오 끝에 V2 불변식 검사(assert_invariants)와 감사 기록
추가 전용 검사(AuditWatch)를 함께 돌림.

주 흐름: 환자 입력 -> 담당자 검토 -> 부족 정보 -> 요청 -> 환자 응답 -> 문서 검증 -> 영향 기준 재평가 -> 갱신된 추천 -> Inspector·Cohort C.
합성 시험(sim.demo T-A..T-D)·합성 권한·LLM 대역만 씁니다 (test_document_clinician_completion의 env fixture와 같음).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
sys.path.insert(0, str(Path(__file__).parent))

import test_authority_withdrawal_state_sync as W  # noqa: E402
import test_document_clinician_completion as D  # noqa: E402
import test_followup_flow as F  # noqa: E402
import test_q_state_contract as Q  # noqa: E402
from test_overnight_invariants import CE, CLOSED, AuditWatch, assert_invariants, clients  # noqa: E402

env = D.env
OPEN = ("pending", "requires_document", "requires_clinician")


def respond(patient, cid, rid, expect=200, **body):
    r = patient.post(f"/api/followup/cases/{cid}/requests/{rid}/response", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def status(staff, cid, rid, value, expect=200):
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid}/status", headers=D.STAFF, json={"status": value})
    assert r.status_code == expect, r.text
    return r.json()


def age_request(patient, staff, trials=("T-A", "T-B", "T-C")):
    cid = D.create(patient, list(trials), text=F.TEXT_NO_AGE)
    case = D.staff_case(staff, cid)
    rid = D.make_requests(staff, cid, D.proposal(case, "age", "ASK_PATIENT")["proposal_id"])["created"][0]
    return cid, rid


# ---------------------------------------------------------------------------
# 주 흐름
# ---------------------------------------------------------------------------

def test_main_flow_patient_to_inspector_and_cohort_queue(env):
    patient, staff, verifier, _ = clients()
    # 1. 환자 입력 (나이 없음, 검사값 없음) -> 시험별 평가
    cid = D.create(patient, ["T-C"], text=F.TEXT_NO_AGE)
    audit = AuditWatch(staff, cid)
    case = D.staff_case(staff, cid)
    assert case["pairs"]["T-C"]["status"] == "INFO_MISSING"
    rec0 = case["view"]["result"]["recommendation"]
    # 2-3. 담당자 검토: 부족 정보 = 나이(환자 질문), ECOG·Hb(진료 기록 필요 -> 문서 확인)
    routes = {(p["info_key"], p["action_type"]) for p in case["proposals"]}
    assert ("age", "ASK_PATIENT") in routes and ("lab:hemoglobin", "REQUEST_DOCUMENT") in routes
    assert ("lab:hemoglobin", "ASK_PATIENT") not in routes
    assert_invariants(staff, patient, cid)
    audit.check()
    # 4. 요청 생성 (나이 질문 + 문서 확인 전부)
    picks = [p["proposal_id"] for p in case["proposals"]
             if (p["info_key"], p["action_type"]) == ("age", "ASK_PATIENT") or p["action_type"] == "REQUEST_DOCUMENT"]
    out = D.make_requests(staff, cid, *picks)
    by_key = {r["info_key"]: r for r in out["case"]["requests"]}
    assert by_key["age"]["status"] == "pending" and by_key["lab:hemoglobin"]["status"] == "requires_document"
    q = patient.get(f"/api/followup/cases/{cid}/requests").json()
    assert q["pending"] == 1 and q["requests"][0]["request_id"] == by_key["age"]["request_id"]
    # 5-6. 환자 응답 -> 재평가 (나이 기준만 바뀜, 확정 없음: Hb·ECOG 문서 확인 남음)
    a = respond(patient, cid, by_key["age"]["request_id"], answer="I am 64 years old", client_response_id="ui-1")
    assert a["status"] == "answered" and a["duplicate"] is False
    case = D.staff_case(staff, cid)
    ev = case["evaluations"][-1]
    assert ev["equivalence"]["status"] == "match" and "T-C" in ev["reevaluated_trials"]
    assert {c["criterion"] for c in ev["criteria_changes"]} >= {"T-C:I1", "T-C:I2"}
    assert case["pairs"]["T-C"]["status"] not in CLOSED
    assert_invariants(staff, patient, cid)
    audit.check()
    # 7-8. 문서 등록 -> 검증자 확인 (Hb 기록, ECOG 확인) -> 영향 기준 재평가
    doc = D.register(staff, cid, content=b"SYNTHETIC LAB REPORT\nHemoglobin 12 g/dL\nECOG 1\n")["document"]
    v1 = D.verify(verifier, cid, by_key["lab:hemoglobin"]["request_id"], doc,
                  [{"field": "lab:hemoglobin", "value": 12.0, "unit": "g/dL", "observed_at": "2026-10-01"}])
    assert v1["pairs"]["T-C"]["status"] != CE  # ECOG 문서 확인 남음
    ecog = [f for f in D.facts_for(verifier, cid, by_key["ecog"]["request_id"]) if f["field"] == "ecog"]
    v2 = D.verify(verifier, cid, by_key["ecog"]["request_id"], doc, [{"fact_id": ecog[0]["fact_id"]}])
    # 9-10. 갱신된 추천: 확정 적격 = 공개 기준 충족, 남은 제안 없음
    assert v2["pairs"]["T-C"]["status"] == CE
    assert {"trial_id": "T-C", "before": "VERIFICATION_PENDING", "after": CE} in v2["transitions"]
    case = D.staff_case(staff, cid)
    shown = {r["trial_id"]: r["match_status"] for r in case["view"]["result"]["recommendation"]["ranked"]}
    assert shown["T-C"] == "CRITERIA_MATCH" and case["view"]["result"]["recommendation"] != rec0
    assert case["evaluations"][-1]["trigger"]["type"] == "document_verification"
    assert not [r for r in case["requests"] if r["status"] in OPEN]
    # 11. Inspector·Cohort C
    insp = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=D.STAFF).json()
    assert [t["round"] for t in insp["timeline"]] and len(insp["verification"]["verifications"]) == 2
    assert {x["trial_id"]: x["match_status"] for x in insp["overview"]["final"]}["T-C"] == "CRITERIA_MATCH"
    queue = staff.get("/api/followup/staff/queue", headers=D.STAFF).json()
    assert not [i for i in queue["items"] if i["case_id"] == cid]  # 확정된 사례에 남은 작업 없음
    assert_invariants(staff, patient, cid)
    entries = audit.check()
    kinds = [e["event"]["kind"] for e in entries]
    assert kinds.count("document_verified") == 2 and "response_recorded" in kinds and "requests_created" in kinds


# ---------------------------------------------------------------------------
# 시나리오
# ---------------------------------------------------------------------------

def test_normal_answer_updates_only_affected_criteria(env):
    patient, staff, _, _ = clients()
    cid, rid = age_request(patient, staff)
    audit = AuditWatch(staff, cid)
    audit.check()
    before = D.staff_case(staff, cid)
    out = respond(patient, cid, rid, answer="I am 80 years old")
    assert out["status"] == "answered"
    case = D.staff_case(staff, cid)
    ev = case["evaluations"][-1]
    assert ev["transitions"] == [{"trial_id": "T-C", "before": "INFO_MISSING", "after": "VERIFICATION_PENDING"}]
    assert {c["criterion"].split(":")[0] for c in ev["criteria_changes"]} <= {"T-A", "T-B", "T-C"}
    assert all(c["criterion"].endswith((":I1", ":I2")) for c in ev["criteria_changes"])  # 나이 기준만
    assert case["pairs"]["T-C"]["eligibility"] == "INELIGIBLE" and case["pairs"]["T-C"]["status"] != "CONFIRMED_INELIGIBLE"
    assert case["revision"] == before["revision"] + 1
    assert_invariants(staff, patient, cid)
    audit.check()


def test_unknown_answer_routes_to_alternative_and_keeps_verdicts(env):
    patient, staff, _, _ = clients()
    cid, rid = age_request(patient, staff)
    before = assert_invariants(staff, patient, cid)
    out = respond(patient, cid, rid, answer="모름")  # 자유 입력 '모름' = 무응답 규칙 (unknown 버튼과 같음)
    assert out["status"] == "unknown" and out["outcome"] == "unknown"
    case = D.staff_case(staff, cid)
    follow = [r for r in case["requests"] if r["follows"] == rid]
    assert len(follow) == 1 and follow[0]["created_by"] == "system"
    assert follow[0]["kind"] in ("document_review", "clinician_review")
    assert assert_invariants(staff, patient, cid)["pairs"] == before["pairs"]
    assert patient.get(f"/api/followup/cases/{cid}/requests").json()["pending"] == 0  # 같은 질문 반복 없음
    assert not [p for p in case["proposals"] if p["action_type"] == "ASK_PATIENT" and p["info_key"] == "age"]


def test_no_response_unreachable_routes_to_documents(env):
    patient, staff, _, _ = clients()
    cid, rid = age_request(patient, staff)
    before = assert_invariants(staff, patient, cid)
    out = status(staff, cid, rid, "unreachable")
    assert len(out["routed"]) == 1
    case = D.staff_case(staff, cid)
    alt = next(r for r in case["requests"] if r["request_id"] == out["routed"][0])
    assert alt["kind"] in ("document_review", "clinician_review") and alt["status"] in OPEN
    respond(patient, cid, rid, expect=409, answer="I am 64 years old")  # 연락 불가로 닫힌 질문에 늦은 답
    assert assert_invariants(staff, patient, cid)["pairs"] == before["pairs"]


def test_ambiguous_answer_is_not_adopted(env):
    patient, staff, _, _ = clients()
    cid, rid = age_request(patient, staff)
    before = assert_invariants(staff, patient, cid)
    out = respond(patient, cid, rid, answer="maybe 64 or 65, not sure")
    assert out["status"] == "answered"
    case = D.staff_case(staff, cid)
    ev = case["evaluations"][-1]
    assert not [c for c in ev["criteria_changes"] if (c["after"] or {}).get("passes") is not None]
    assert assert_invariants(staff, patient, cid)["pairs"] == before["pairs"]
    assert all(p["status"] == "INFO_MISSING" for p in case["pairs"].values())


def test_conflicting_answer_opens_conflict_check_and_holds(env):
    patient, staff, _, _ = clients()
    cid, rid = age_request(patient, staff)
    out = respond(patient, cid, rid, answer="I am 64 years old.\nMy ECOG performance status is 3 now.")
    assert out["conflict"] is True
    case = D.staff_case(staff, cid)
    conflict = [r for r in case["requests"] if r["kind"] == "conflict_resolution"]
    assert len(conflict) == 1 and conflict[0]["conflict"]["field"] == "ecog"
    st = assert_invariants(staff, patient, cid)
    assert all(st["pairs"][t] not in CLOSED for t in ("T-A", "T-C"))
    # 충돌 확인 기록이 없음 -> 임상 검토 (어느 값도 채택하지 않음), 같은 충돌 확인을 다시 만들지 않음
    out = status(staff, cid, conflict[0]["request_id"], "document_unavailable")
    case = D.staff_case(staff, cid)
    nxt = [r for r in case["requests"] if r["follows"] == conflict[0]["request_id"]]
    assert [(r["kind"], r["reason_code"]) for r in nxt] == [("clinician_review", "conflict_unresolved")]
    assert sum(r["kind"] == "conflict_resolution" for r in case["requests"]) == 1
    assert all("conflict" in case["pairs"][t]["gaps"] for t in ("T-A", "T-C"))
    assert_invariants(staff, patient, cid)


def test_missing_document_chain_ends_in_clinician_review_not_completion(env):
    patient, staff, _, _ = clients()
    cid = D.create(patient, ["T-A"])
    case = D.staff_case(staff, cid)
    chain = D.make_requests(staff, cid, D.proposal(case, "lab:platelets", "REQUEST_DOCUMENT")["proposal_id"])["created"][0]
    before = assert_invariants(staff, patient, cid)
    kinds = []
    for _ in range(6):
        out = status(staff, cid, chain, "document_unavailable")
        nxt = next(r for r in out["case"]["requests"] if r["follows"] == chain)
        kinds.append(nxt["kind"])
        assert assert_invariants(staff, patient, cid)["pairs"] == before["pairs"]
        if nxt["kind"] == "clinician_review":
            break
        chain = nxt["request_id"]
    assert kinds[-1] == "clinician_review" and set(kinds[:-1]) <= {"document_review"}
    review = next(r for r in D.staff_case(staff, cid)["requests"] if r["kind"] == "clinician_review")
    assert review["status"] == "requires_clinician" and review["reason_code"] == "item_sources_exhausted"
    status(staff, cid, chain, "document_unavailable", expect=409)  # 이미 닫힌 요청


def test_failed_document_verification_wrong_hash_field_or_tampered_content(env):
    patient, staff, verifier, _ = clients()
    cid, by_key = D.answered_lab_case(patient, staff)
    doc = D.register(staff, cid)["document"]
    audit = AuditWatch(staff, cid)
    audit.check()
    before = D.staff_case(staff, cid)
    rid = by_key["lab:platelets"]
    plt = [f for f in D.facts_for(verifier, cid, rid) if f["field"] == "lab:platelets"][0]
    base = f"/api/followup/verifier/cases/{cid}/requests/{rid}/verification"
    r = verifier.post(base, headers=D.VERIFIER, json={"document_id": doc["document_id"], "document_sha256": "4" * 64,
                                                      "items": [{"fact_id": plt["fact_id"]}]})
    assert r.status_code == 409 and D.code(r) == "document_changed"
    r = verifier.post(base, headers=D.VERIFIER, json={"document_id": doc["document_id"], "document_sha256": doc["sha256"],
                                                      "items": [{"field": "ecog", "value": 1}]})
    assert r.status_code == 409 and D.code(r) == "field_out_of_scope"
    (env / "documents" / cid / doc["document_id"]).write_bytes(b"tampered")
    r = verifier.post(base, headers=D.VERIFIER, json={"document_id": doc["document_id"], "document_sha256": doc["sha256"],
                                                      "items": [{"fact_id": plt["fact_id"]}]})
    assert r.status_code == 409 and D.code(r) == "document_integrity"
    after = D.staff_case(staff, cid)
    assert (after["revision"], after["pairs"], after["requests"]) == (before["revision"], before["pairs"],
                                                                      before["requests"])
    assert_invariants(staff, patient, cid)
    denied = [e["event"]["code"] for e in audit.check() if e["source"] == "denied"]
    assert denied == ["document_changed", "field_out_of_scope", "document_integrity"]


@pytest.mark.parametrize("how", W.WITHDRAW)
def test_authority_withdrawal_lowers_every_staff_and_patient_view(env, how):
    patient, staff, verifier, clinician = clients()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    assert assert_invariants(staff, patient, cid)["pairs"] == {"T-A": CE, "T-D": CE}
    audit = AuditWatch(staff, cid)
    audit.check()
    W.withdraw(env, how)
    st = assert_invariants(staff, patient, cid)
    assert CE not in st["pairs"].values()
    case = D.staff_case(staff, cid)
    assert case["authority_changed"] is True
    summary = staff.get(f"/api/followup/staff/cases/{cid}/verification", headers=D.STAFF).json()
    assert summary["document_verified_facts"] == []
    assert all(j["recognition"] != "recognized" for j in summary["judgments"])
    if how == "mode_unset":  # 검증자·임상의 API 503 (완료를 기록할 수 없음)
        assert verifier.get("/api/followup/verifier/queue", headers=D.VERIFIER).status_code == 503
        assert clinician.get("/api/followup/clinician/queue", headers=D.CLINICIAN).status_code == 503
    audit.check()


def test_clinician_judgment_required_flow(env):
    patient, staff, verifier, clinician = clients()
    cid = D.create(patient, ["T-A", "T-D"])
    case = D.staff_case(staff, cid)
    assert case["pairs"]["T-D"]["status"] == "EXPERT_REVIEW"
    review = next(r for r in case["requests"] if r["kind"] == "clinician_review")
    assert (review["status"], review["created_by"]) == ("requires_clinician", "system")
    q = clinician.get("/api/followup/clinician/queue", headers=D.CLINICIAN).json()["items"]
    assert [i["request_id"] for i in q if i["case_id"] == cid] == [review["request_id"]]
    met = {"criterion": "T-D:I2", "decision": "MET", "rationale_code": "life_expectancy_adequate"}
    assert D.judge(clinician, cid, review["request_id"], [met], expect=409)["detail"]["code"] == "basis_not_document_verified"
    doc_req = D.make_requests(staff, cid, D.proposal(case, "ecog", "REQUEST_DOCUMENT")["proposal_id"])["created"][0]
    doc = D.register(staff, cid)["document"]
    ecog = next(f for f in D.facts_for(verifier, cid, doc_req) if f["field"] == "ecog")
    D.verify(verifier, cid, doc_req, doc, [{"fact_id": ecog["fact_id"]}])
    assert assert_invariants(staff, patient, cid)["pairs"]["T-D"] == "EXPERT_REVIEW"
    out = D.judge(clinician, cid, review["request_id"], [{**met, "basis_fact_ids": [ecog["fact_id"]]}])
    assert out["pairs"]["T-D"]["status"] == CE and out["request"]["status"] == "clinician_decided"
    assert assert_invariants(staff, patient, cid)["pairs"]["T-D"] == CE
    D.judge(clinician, cid, review["request_id"], [{**met, "basis_fact_ids": [ecog["fact_id"]]}], expect=409)


def test_repeated_identical_request_creates_nothing_new(env):
    patient, staff, _, _ = clients()
    cid = D.create(patient, ["T-A", "T-B", "T-C"], text=F.TEXT_NO_AGE)
    case = D.staff_case(staff, cid)
    pid = D.proposal(case, "age", "ASK_PATIENT")["proposal_id"]
    first = D.make_requests(staff, cid, pid)
    rev = D.staff_case(staff, cid)["revision"]
    for _ in range(3):
        again = D.make_requests(staff, cid, pid)
        assert again["created"] == [] and again["existing"] == first["created"]
    after = D.staff_case(staff, cid)
    assert after["revision"] == rev and sum(r["info_key"] == "age" for r in after["requests"]) == 1
    both = D.make_requests(staff, cid, pid, pid)  # 같은 제안을 한 요청에 두 번
    assert both["created"] == [] and D.staff_case(staff, cid)["revision"] == rev
    assert_invariants(staff, patient, cid)


def test_server_restart_mid_flow_preserves_state_and_continues(env):
    patient, staff, verifier, _ = clients()
    cid, by_key = D.answered_lab_case(patient, staff)
    audit = AuditWatch(staff, cid)
    audit.check()
    before = D.staff_case(staff, cid)
    D.restart(env)
    after = D.staff_case(staff, cid)
    for k in ("revision", "requests", "evaluations", "pairs", "proposals", "events"):
        assert after[k] == before[k], k
    audit.check()
    doc = D.register(staff, cid)["document"]
    D.restart(env)
    plt = [f for f in D.facts_for(verifier, cid, by_key["lab:platelets"]) if f["field"] == "lab:platelets"]
    D.verify(verifier, cid, by_key["lab:platelets"], doc, [{"fact_id": plt[0]["fact_id"]}])
    D.restart(env)
    assert_invariants(staff, patient, cid)
    assert audit.check()[-1]["event"]["kind"] == "document_verified"


def test_duplicate_response_by_client_response_id(env):
    patient, staff, _, _ = clients()
    cid, rid = age_request(patient, staff)
    first = respond(patient, cid, rid, answer="I am 64 years old", client_response_id="ui-7")
    rev = first["revision"]
    dup = respond(patient, cid, rid, answer="I am 64 years old", client_response_id="ui-7")
    assert dup["duplicate"] is True and dup["revision"] == rev
    retry = respond(patient, cid, rid, answer="I am 70 years old", client_response_id="ui-7")  # 같은 ID = 재전송
    assert retry["duplicate"] is True and retry["revision"] == rev
    respond(patient, cid, rid, expect=409, answer="I am 70 years old", client_response_id="ui-8")
    case = D.staff_case(staff, cid)
    assert case["revision"] == rev and len(case["responses"]) == 1 and case["responses"][0]["answer"] == "I am 64 years old"
    assert_invariants(staff, patient, cid)


def test_new_information_conflicting_with_existing_holds(env):
    """문서의 새 값이 환자가 처음 말한 값과 다름 -> 충돌 확인, 보류, 문서로 충돌을 닫을 수 없음."""
    patient, staff, verifier, _ = clients()
    cid, by_key = D.answered_lab_case(patient, staff)  # 처음 입력: 혈소판 180
    doc = D.register(staff, cid, content=b"SYNTHETIC LAB REPORT\nPlatelets 90 x10^9/L\n")["document"]
    out = D.verify(verifier, cid, by_key["lab:platelets"], doc,
                   [{"field": "lab:platelets", "value": 90.0, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    assert out["pairs"]["T-A"]["status"] not in CLOSED
    case = D.staff_case(staff, cid)
    conflict = next(r for r in case["requests"] if r["kind"] == "conflict_resolution")
    r = D.verify(verifier, cid, conflict["request_id"], doc, [{"field": "lab:platelets", "value": 90.0}], expect=409)
    assert r["detail"]["code"] == "conflict_not_closed_by_document"
    st = assert_invariants(staff, patient, cid)
    assert st["pairs"]["T-A"] not in CLOSED and "conflict" in D.staff_case(staff, cid)["pairs"]["T-A"]["gaps"]
