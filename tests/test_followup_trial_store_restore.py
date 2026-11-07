"""Follow-up 복원 회귀 (운영 카탈로그 = 실제 trial_store, 기준표를 카탈로그에 넣지 않음).

test_followup_flow.py는 기준표(parsed)를 함께 넣은 합성 카탈로그(sim.demo)를 써서 이 경로를 보지 않는다. 운영 서버의
followup_api.default_catalog()는 기준표 없이 시험 원문만 넘기므로, 저장했다가 복원한 Session은 처음 Session이
parse_criteria로 채웠던 기준표를 trial_store에서 다시 읽어야 한다. 읽지 않으면 계획(제안)이 비어 담당자가 방금 받은
제안을 요청해도 409(proposal_not_current)가 되고 Cohort C 대기열이 빈다 (Q 통합 점검에서 NSCLC 세트로 발견).
LLM은 오프라인 대역, 시험은 저장소의 사전 컴파일 기준표(data/parsed), 환자는 합성이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402
from ctrec import pipeline  # noqa: E402
from ctrec import question_eval as qe  # noqa: E402
from ctrec.followup import codec  # noqa: E402
from ctrec.followup.service import FollowupService  # noqa: E402
from ctrec.followup.store import CaseStore  # noqa: E402
from ctrec.web import app as web  # noqa: E402
from ctrec.web import followup_api  # noqa: E402

KEY = "synthetic-staff-key-restore"
STAFF = {"X-CTREC-Staff-Key": KEY, "X-CTREC-Staff-Id": "coord-r"}
TID = "NCT04063540"  # 운영 풀의 실제 시험 (사전 컴파일·고정 기준표). 데모 시험은 운영 풀에서 빠짐
TEXT = "Synthetic patient. 34-year-old woman with migraine with aura, about 4 attacks per month."


def latest_profiler(patient_id, raw_text, reference_date=None):
    return qe.stub_profiler(patient_id, "\n".join(reversed(raw_text.splitlines())), reference_date)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "TEST_LLM_BACKEND", "fake")
    monkeypatch.setenv("CTREC_STAFF_KEY", KEY)
    for name in ("CTREC_REQUIRE_HTTPS", "CTREC_ALLOW_INSECURE_HTTP", "CTREC_COOKIE_SECURE", "CTREC_SESSION"):
        monkeypatch.delenv(name, raising=False)
    with qe.OfflineEnvironment(3):
        pipeline.patient_profiler.profile_patient = latest_profiler
        root = tmp_path / "followup"
        followup_api.configure(FollowupService(CaseStore(root), followup_api.default_catalog()))
        yield root
    followup_api.configure(None)


def test_production_catalog_carries_no_parsed_criteria(env):
    cat = followup_api.default_catalog()
    assert cat.name == "trial_store" and TID in cat.trials
    assert not cat.parsed  # 이 회귀가 보는 조건: 기준표는 trial_store에서만 옴


def test_restored_session_reloads_trial_store_criteria(env):
    patient = h.browser()
    r = patient.post("/api/followup/cases", json={"patient_text": TEXT, "trial_ids": [TID]})
    assert r.status_code == 201, r.text
    cid = r.json()["case_id"]
    s = codec.restore_session(CaseStore(env).load(cid)["session"], followup_api.default_catalog().trials)
    assert TID in s.parsed and s.parsed[TID].rules


def test_staff_can_request_the_proposal_it_was_shown(env):
    patient, staff = h.browser(), h.browser(client=("198.51.100.30", 50001))
    cid = patient.post("/api/followup/cases", json={"patient_text": TEXT, "trial_ids": [TID]}).json()["case_id"]
    case = staff.get(f"/api/followup/staff/cases/{cid}", headers=STAFF).json()
    shown = [p for p in case["proposals"] if p["requestable"]]
    assert shown
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=STAFF,
                   json={"proposal_ids": [shown[0]["proposal_id"]]})
    assert r.status_code == 200, r.text  # 수정 전: 409 proposal_not_current
    assert len(r.json()["created"]) == 1
    queue = staff.get("/api/followup/staff/queue", headers=STAFF).json()
    assert [i["proposal_id"] for i in queue["items"] if i["case_id"] == cid] == \
        [p["proposal_id"] for p in r.json()["case"]["proposals"]]
