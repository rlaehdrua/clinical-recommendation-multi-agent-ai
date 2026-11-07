"""Q: 단일 판정 상태 계약 - 엔진 쌍 상태와 추천 화면(match_status)이 같은 말을 하는지 (확정 적격 <-> 공개 기준 충족).

재현한 불일치 두 가지와 그 반대 방향:
1. 문서 확인으로 자기 보고 보류가 해제된 시험: evidence_basis.reaggregate가 판정(eligibility)만 다시 집계하고 외부 상태를
   그대로 둬서 ELIGIBLE인데 POTENTIAL_MATCH('기록 확인 시 충족 가능')로 남음
2. 임상의 판단으로 확정된 시험: conservative_view가 인정된 임상의 판단도 'LLM 판정'으로 보고 추천을 '의료진 검토 필요'로 낮춤
3. (반대 방향) 권한 철회 뒤: 쌍 상태는 다시 계산되지만 저장된 추천 화면·사례 목록은 확정 그대로 (환자 화면에도)
합성 시험·합성 권한(test_document_clinician_completion과 같은 고정값), LLM 대역.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
sys.path.insert(0, str(Path(__file__).parent))

import test_document_clinician_completion as D  # noqa: E402

env = D.env
CE = "CONFIRMED_ELIGIBLE"


def statuses(staff, patient, cid) -> dict:
    case = D.staff_case(staff, cid)
    shown = {r["trial_id"]: r["match_status"] for r in case["view"]["result"]["recommendation"]["ranked"]}
    pv = patient.get(f"/api/followup/cases/{cid}").json()
    pshown = {r["trial_id"]: r["match_status"] for r in pv["view"]["result"]["recommendation"]["ranked"]}
    counts = next(c["pair_counts"] for c in staff.get("/api/followup/staff/cases", headers=D.STAFF).json()["cases"]
                  if c["case_id"] == cid)
    return {"pairs": {t: p["status"] for t, p in case["pairs"].items()}, "shown": shown,
            "patient_pairs": {t: p["status"] for t, p in pv["pairs"].items()}, "patient_shown": pshown,
            "counts": counts}


def assert_one_state(st: dict) -> None:
    """확정 적격 <-> 공개 기준 충족, 모든 화면이 같은 쌍 상태."""
    for t, s in st["pairs"].items():
        assert (s == CE) == (st["shown"].get(t) == "CRITERIA_MATCH"), (t, s, st["shown"].get(t))
        assert (st["patient_pairs"][t] == CE) == (st["patient_shown"].get(t) == "CRITERIA_MATCH"), t
    assert st["patient_pairs"] == st["pairs"]
    assert st["counts"].get(CE, 0) == sum(s == CE for s in st["pairs"].values())


def completed_case(patient, staff, verifier, clinician):
    cid, by_key = D.answered_lab_case(patient, staff)
    doc = D.register(staff, cid)["document"]
    plt = [f for f in D.facts_for(verifier, cid, by_key["lab:platelets"]) if f["field"] == "lab:platelets"]
    D.verify(verifier, cid, by_key["lab:platelets"], doc, [{"fact_id": plt[0]["fact_id"]}])
    D.verify(verifier, cid, by_key["lab:absolute neutrophil count"], doc,
             [{"field": "lab:absolute neutrophil count", "value": 2.1, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    ecog = [f for f in D.facts_for(verifier, cid, by_key["ecog"]) if f["field"] == "ecog"]
    D.verify(verifier, cid, by_key["ecog"], doc, [{"fact_id": ecog[0]["fact_id"]}])
    after_docs = statuses(staff, patient, cid)
    cv = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=D.CLINICIAN).json()
    rq = next(r for r in cv["requests"] if r["judgeable"])
    ef = next(f for f in cv["facts"] if f["field"] == "ecog" and f["document_verified"])["fact_id"]
    D.judge(clinician, cid, rq["request_id"], [{"criterion": "T-D:I2", "decision": "MET",
                                                 "rationale_code": "life_expectancy_adequate", "basis_fact_ids": [ef]}])
    return cid, after_docs


def test_confirmed_pairs_and_recommendation_say_the_same(env):
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, after_docs = completed_case(patient, staff, verifier, clinician)
    assert after_docs["pairs"]["T-A"] == CE and after_docs["shown"]["T-A"] == "CRITERIA_MATCH"  # 1: 문서 확인 해제
    assert after_docs["pairs"]["T-D"] == "EXPERT_REVIEW" and after_docs["shown"]["T-D"] == "NEEDS_REVIEW"
    assert_one_state(after_docs)
    st = statuses(staff, patient, cid)
    assert st["pairs"] == {"T-A": CE, "T-D": CE} and st["shown"] == {"T-A": "CRITERIA_MATCH", "T-D": "CRITERIA_MATCH"}  # 2
    assert_one_state(st)


@pytest.mark.parametrize("withdraw", ["other_secret", "mode_unset"])
def test_withdrawn_authority_lowers_every_view(env, withdraw):
    """3: 권한 철회 뒤 확정이 사라지면 추천 화면·환자 화면·사례 목록도 함께 내려감 (저장된 확정 화면이 남지 않음)."""
    patient, staff, verifier, clinician = D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()
    cid, _ = completed_case(patient, staff, verifier, clinician)
    if withdraw == "other_secret":
        os.environ["CTREC_SYNTHETIC_SIGNING_SECRET"] = "another-signing-secret-000000001"  # monkeypatch가 테스트 뒤 복구
    else:
        os.environ.pop("CTREC_VERIFICATION_MODE")
    D.restart(env)
    st = statuses(staff, patient, cid)
    assert CE not in st["pairs"].values()
    assert "CRITERIA_MATCH" not in st["shown"].values() and "CRITERIA_MATCH" not in st["patient_shown"].values()
    assert_one_state(st)
