"""V4-B1: 권한 철회 뒤 다시 확인 제안 (11_CODE_REVIEW.md V4-B1).

재현 (1f04229): 문서 확인·임상의 판단을 마친 사례에서 합성 서명 비밀이 바뀌면(권한 철회) 증명이 무효가 되지만 사실 지문은
그대로라, 끝난 문서 확인 요청이 반복 억제(engine.loop.repeat_key)에 '이미 시도함'으로 남아 ECOG·혈소판 확인이 다시 제안되지
않음. 그런데 임상 검토(CLINICIAN_REVIEW)는 제안돼, 진료 기록 필요 기준의 근거가 될 문서 확인 사실이 하나도 없어 판단이
409(basis_not_document_verified)로만 끝나는 막다른 행동이 대기열에 남음.

계약
- 증명이 지금 권한으로 인정되지 않는 문서 확인 요청은 '시도함'으로 세지 않음 -> 같은 확인을 다시 제안
- 증명이 철회돼 근거 요건(문서로 확인된 사실)을 지금 충족할 수 없는 동안 임상 검토(CLINICIAN_REVIEW)는 제안하지 않음
  (다시 확인 제안이 먼저). 처음 접수 때의 임상 검토 요청은 기존 계약 그대로
- 다시 확인하면 원래 상태로 회복 (확정은 다시 확인한 뒤에만). 어느 화면에도 확인 전 확정이 보이지 않음
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

env = D.env
CE = "CONFIRMED_ELIGIBLE"


def _withdraw(env) -> None:
    os.environ["CTREC_SYNTHETIC_SIGNING_SECRET"] = "another-signing-secret-000000001"  # monkeypatch가 테스트 뒤 복구
    D.restart(env)


def _queue(staff, cid) -> list[dict]:
    return [i for i in staff.get("/api/followup/staff/queue", headers=D.STAFF).json()["items"] if i["case_id"] == cid]


def _doc_fact(verifier, cid, rid, field):
    return [f for f in D.facts_for(verifier, cid, rid) if f["field"] == field]


def test_withdrawn_proofs_are_reproposed_and_dead_end_review_is_not(env):
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    _withdraw(env)

    case = D.staff_case(staff, cid)
    assert CE not in {p["status"] for p in case["pairs"].values()}
    kinds = {(p["info_key"], p["action_type"]) for p in case["proposals"]}
    # 증명이 무효가 된 확인(ECOG·혈소판)은 다시 제안됨
    assert ("ecog", "REQUEST_DOCUMENT") in kinds, kinds
    assert ("lab:platelets", "REQUEST_DOCUMENT") in kinds, kinds
    # 문서로 확인된 사실이 없으니 진료 기록 필요 기준의 임상 검토는 지금 끝낼 수 없음 -> 제안·대기열에 없음
    assert not [p for p in case["proposals"] if p["action_type"] == "CLINICIAN_REVIEW"], case["proposals"]
    q = _queue(staff, cid)
    assert not [i for i in q if i["action_type"] == "CLINICIAN_REVIEW"], q
    assert {i["proposal_id"] for i in q} <= {p["proposal_id"] for p in case["proposals"]}
    Q.assert_one_state(Q.statuses(staff, patient, cid))

    # 다시 확인 (같은 문서, 새 권한) -> 확정은 다시 확인한 뒤에만
    pids = [D.proposal(case, k, "REQUEST_DOCUMENT")["proposal_id"]
            for k in ("ecog", "lab:platelets", "lab:absolute neutrophil count")
            if any(p["info_key"] == k and p["action_type"] == "REQUEST_DOCUMENT" for p in case["proposals"])]
    out = D.make_requests(staff, cid, *pids)
    assert len(out["created"]) == len(pids)
    reqs = {r["request_id"]: r for r in out["case"]["requests"]}
    doc = D.register(staff, cid)["document"]
    for rid in out["created"]:
        key = reqs[rid]["info_key"]
        st = Q.statuses(staff, patient, cid)
        assert st["pairs"]["T-A"] != CE, key  # 남은 확인이 있는 동안 확정 없음
        Q.assert_one_state(st)
        f = [x for x in _doc_fact(verifier, cid, rid, key) if not x.get("document_verified")]
        D.verify(verifier, cid, rid, doc, [{"fact_id": f[0]["fact_id"]}])
    st = Q.statuses(staff, patient, cid)
    assert st["pairs"]["T-A"] == CE, st  # 회복
    Q.assert_one_state(st)

    # 이제 근거가 있으니 임상 검토가 판단 가능하고, 다시 확인한 ECOG를 근거로 판단이 인정됨
    cv = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=D.CLINICIAN).json()
    rq = [r for r in cv["requests"] if r["judgeable"]]
    if not rq:
        case = D.staff_case(staff, cid)
        review = [p for p in case["proposals"] if p["action_type"] == "CLINICIAN_REVIEW"]
        assert review, case["proposals"]
        D.make_requests(staff, cid, review[0]["proposal_id"])
        cv = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=D.CLINICIAN).json()
        rq = [r for r in cv["requests"] if r["judgeable"]]
    assert rq
    ef = next(f for f in cv["facts"] if f["field"] == "ecog" and f["document_verified"])["fact_id"]
    D.judge(clinician, cid, rq[0]["request_id"], [{"criterion": "T-D:I2", "decision": "MET",
                                                    "rationale_code": "life_expectancy_adequate", "basis_fact_ids": [ef]}])
    st = Q.statuses(staff, patient, cid)
    assert st["pairs"] == {"T-A": CE, "T-D": CE}, st
    Q.assert_one_state(st)
