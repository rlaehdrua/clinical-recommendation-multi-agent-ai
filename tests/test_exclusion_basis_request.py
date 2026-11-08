"""불충족 근거 확인 요청(basis:*, exclusion_basis_unverified)이 다른 서류 확인의 재평가에서 '해결됨'으로 닫히지 않음.

자기 보고 나이(16세)로 'Age >= 18'이 불충족이면 쌍은 VERIFICATION_PENDING(exclusion_unverified)이고, 엔진은 그 근거를
진료 기록으로 확인하는 요청을 냄. 그 기준에는 정보 빈칸(gap)이 없으므로, 열린 기준을 gap으로만 세면 다른 서류 확인 뒤
재평가에서 이 요청이 targets_resolved로 닫히고 다시 제안되지 않아 쌍이 진행할 수 없게 멈췄음 (11-08 브라우저에서 발견).
합성 시험·오프라인 대역만."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sys.path.insert(0, str(Path(__file__).parent))

import test_document_clinician_completion as D  # noqa: E402
import web_harness as h  # noqa: E402

env = D.env
MINOR = "Synthetic NSCLC patient. 16-year-old male with stage IV non-small cell lung cancer. ECOG 1."
RECORD = b"SYNTHETIC MEDICAL RECORD (not a real patient)\nAge 16 years\nPlatelets 180 x10^9/L\n"


def _req(case, key):
    return next(r for r in case["requests"] if r.get("info_key") == key)


def _confirm_age(c, cid, rid, doc):
    """환자가 말한 나이를 서류와 같다고 확인 (데모 화면의 '환자가 말한 값이 서류와 같아요' 체크와 같은 경로)."""
    facts = [f for f in D.facts_for(c, cid, rid) if f["field"] == "age"]
    assert facts, D.facts_for(c, cid, rid)
    return D.verify(c, cid, rid, doc, [{"fact_id": f["fact_id"]} for f in facts])


def test_exclusion_basis_request_survives_other_document_checks_and_closes_the_pair(env):
    cid = D.create(h.browser(), ["T-A"], text=MINOR)
    c = D.staff_client()
    case = D.staff_case(c, cid)
    assert case["pairs"]["T-A"]["status"] == "VERIFICATION_PENDING"
    basis = D.proposal(case, "basis:age", "REQUEST_DOCUMENT")
    lab = D.proposal(case, "lab:platelets", "REQUEST_DOCUMENT")
    D.make_requests(c, cid, basis["proposal_id"], lab["proposal_id"])
    doc = D.register(c, cid, content=RECORD)["document"]

    case = D.staff_case(c, cid)
    D.verify(c, cid, _req(case, "lab:platelets")["request_id"], doc,
             [{"field": "lab:platelets", "value": 180, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    case = D.staff_case(c, cid)
    held = _req(case, "basis:age")
    assert held["status"] == "requires_document", held["history"]  # 다른 확인의 재평가에서 닫히지 않음
    assert case["pairs"]["T-A"]["status"] == "VERIFICATION_PENDING"  # 확인 전에는 확정하지 않음

    out = _confirm_age(c, cid, held["request_id"], doc)
    assert out["request"]["status"] == "document_verified"
    assert D.staff_case(c, cid)["pairs"]["T-A"]["status"] == "CONFIRMED_INELIGIBLE"  # 서류로 확인된 근거로만 확정


def test_requests_whose_targets_really_closed_are_still_superseded(env):
    """고친 뒤에도 대상 기준이 정말 닫힌 요청은 그대로 닫힘 (근거 확인 대상만 열린 것으로 봄)."""
    cid = D.create(h.browser(), ["T-A"], text=MINOR)
    c = D.staff_client()
    case = D.staff_case(c, cid)
    basis = D.proposal(case, "basis:age", "REQUEST_DOCUMENT")
    D.make_requests(c, cid, basis["proposal_id"])
    doc = D.register(c, cid, content=RECORD)["document"]
    rid = _req(D.staff_case(c, cid), "basis:age")["request_id"]
    _confirm_age(c, cid, rid, doc)
    case = D.staff_case(c, cid)
    assert case["pairs"]["T-A"]["status"] == "CONFIRMED_INELIGIBLE"
    assert not [r for r in case["requests"] if r["status"] in ("pending", "requires_document", "requires_clinician")
                and r["kind"] != "clinician_review"]  # 확정 쌍의 남은 확인 요청은 닫힘


