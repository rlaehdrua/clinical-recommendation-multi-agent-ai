"""P1-4 (2026-11-08 최종 감사): 엔진 쌍 상태는 VERIFICATION_PENDING인데 환자 API 결과는 CRITERIA_MISMATCH로 보이던 문제.

재현 (수정 전): POST /api/followup/cases에 input_source="clinical_record"와 낮은 혈소판·ANC·Hb ->
쌍은 모두 VERIFICATION_PENDING(exclusion_unverified)인데 view.result의 matches는 전부 CRITERIA_MISMATCH,
no_recommendation_reason은 "모두 선정/제외 기준에 맞지 않았습니다". 같은 값을 self_report로 넣으면 INSUFFICIENT_INFORMATION.
수정: conservative_view가 확정되지 않은 불충족을 'mismatch' 보류로 낮춤 (판정·쌍 상태는 그대로).
실제 HTTP 경로(FastAPI TestClient), 오프라인 대역, 합성 시험(sim.demo). 실제 환자·API 없음.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402
from test_followup_flow import STAFF, TRIALS, env, staff_client  # noqa: E402,F401  (env: fixture)

FAIL = ("Synthetic NSCLC patient. 64-year-old male with stage IV non-small cell lung cancer. ECOG 1.\n"
        "Platelets 50 x10^9/L\nANC 0.5 x10^9/L\nHemoglobin 7 g/dL")
NOT_FIT = "모집 중인 후보 시험이 모두 선정/제외 기준에 맞지 않았습니다."


def _create(source: str, text: str = FAIL):
    r = h.browser().post("/api/followup/cases", json={"patient_text": text, "trial_ids": TRIALS, "input_source": source})
    assert r.status_code == 201, r.text
    return r.json()


def _shown(created: dict) -> dict[str, str]:
    return {t: m["match_status"] for t, m in created["view"]["result"]["matches"].items()}


@pytest.mark.parametrize("source", ["clinical_record", "self_report"])
def test_unverified_failing_values_are_not_shown_as_confirmed_mismatch(env, source):
    created = _create(source)
    pairs = created["pairs"]
    assert all(p["status"] != "CONFIRMED_INELIGIBLE" for p in pairs.values())  # 엔진은 확정하지 않음 (그대로)
    shown = _shown(created)
    assert "CRITERIA_MISMATCH" not in shown.values(), shown
    rec = created["view"]["result"]["recommendation"]
    assert rec["ranked"] == [] or all(r["match_status"] != "CRITERIA_MATCH" for r in rec["ranked"])  # 추천으로 올리지 않음
    assert NOT_FIT not in (rec.get("no_recommendation_reason") or "")
    boundary = created["view"]["result"]["engine_boundary"]
    for tid, p in pairs.items():
        assert boundary[tid]["engine_status"] == p["status"]
        assert boundary[tid]["unresolved"] != "exclusion_not_document_verified"  # 남은 불일치 없음


def test_declared_record_and_self_report_show_the_same_status(env):
    """같은 값: 진료 기록 선언과 자기 보고가 같은 표시 (선언만으로 부적격 확정처럼 보이지 않음)."""
    assert _shown(_create("clinical_record")) == _shown(_create("self_report"))


def test_declared_record_hold_names_the_unverified_basis(env):
    created = _create("clinical_record")
    rec = created["view"]["result"]["recommendation"]
    held = {x["trial_id"]: x for x in rec.get("held_out") or []}
    mism = {t for t, m in created["view"]["result"]["matches"].items() if m["eligibility"] == "INELIGIBLE"}
    assert mism and mism <= set(held)
    for t in mism:
        assert held[t]["match_status"] == "INSUFFICIENT_INFORMATION"
        assert any("문서로 확인되지 않아" in n for n in held[t]["reasons"])
    assert not ({x["trial_id"] for x in rec.get("excluded") or []} & mism)  # 확정 제외 목록에 넣지 않음


def test_staff_view_keeps_engine_status(env):
    """담당자 화면 계약: 쌍 상태·근거 확인 제안은 그대로 (표시 사본만 바뀜)."""
    cid = _create("clinical_record")["case_id"]
    case = staff_client().get(f"/api/followup/staff/cases/{cid}", headers=STAFF).json()
    assert {p["status"] for p in case["pairs"].values()} <= {"VERIFICATION_PENDING", "INFO_MISSING"}
    assert any(p["action_type"] == "REQUEST_DOCUMENT" for p in case["proposals"])
