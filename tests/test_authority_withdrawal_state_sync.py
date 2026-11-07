"""권한 철회 뒤 상태 일치: GET·POST 응답·Inspector·Insight·Cohort C 대기열이 같은 '지금 권한' 상태를 말하는지.

f835e38 최종 검증에서 재현한 세 불일치 (모두 합성 권한 철회 = 다른 서명 비밀, 또는 검증 모드 해제 뒤 재시작):
1. 요청 생성 POST 응답의 사례 화면이 저장된(철회 전) CONFIRMED_ELIGIBLE / CRITERIA_MATCH를 그대로 돌려줌
2. Inspector가 저장된 추천 결과·판정을 써서 overview·시험별로 CRITERIA_MATCH / ELIGIBLE을 보임
3. 사례 화면의 제안은 저장값(확정 당시 0건), 대기열은 지금 권한으로 계획(2건) -> 다음 행동이 서로 다름

계약: 저장된 current는 그 평가 때의 기록이고, 보기(GET·POST 응답·Inspector·Insight)는 refresh_pairs로 지금 권한 기준을
다시 계산함 (판정·LLM 다시 하지 않음). 제안도 같은 cohort 상태에서 다시 계획 = staff_queue·create_requests가 쓰는 계획.
합성 시험·합성 권한(test_document_clinician_completion과 같은 고정값), LLM 대역.
"""

from __future__ import annotations

import json
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
WITHDRAW = ["other_secret", "mode_unset"]


def withdraw(env, how: str) -> None:
    if how == "other_secret":
        os.environ["CTREC_SYNTHETIC_SIGNING_SECRET"] = "another-signing-secret-000000001"  # monkeypatch가 테스트 뒤 복구
    else:
        os.environ.pop("CTREC_VERIFICATION_MODE")
    D.restart(env)


def clients():
    return D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()


def case_state(case: dict) -> dict:
    return {"pairs": {t: p["status"] for t, p in case["pairs"].items()},
            "shown": {r["trial_id"]: r["match_status"] for r in case["view"]["result"]["recommendation"]["ranked"]},
            "pair_counts": case["pair_counts"], "proposals": [p["proposal_id"] for p in case["proposals"]],
            "authority_changed": case["authority_changed"]}


def assert_no_ce(st: dict) -> None:
    assert CE not in st["pairs"].values(), st["pairs"]
    assert "CRITERIA_MATCH" not in st["shown"].values(), st["shown"]
    assert CE not in st["pair_counts"], st["pair_counts"]


def queue_for(staff, cid) -> list[str]:
    items = staff.get("/api/followup/staff/queue", headers=D.STAFF).json()["items"]
    return [i["proposal_id"] for i in items if i["case_id"] == cid]


NOTICE = "평가 당시 기록 — 현재 판정과 다를 수 있음"


def _eligible_marks(obj, acc, top=True):
    """Inspector 보기의 현재 판정 부분에서 CRITERIA_MATCH / ELIGIBLE 표시. 서버가 과거 평가 기록(record_scope.history)으로
    지정한 키만 빼고 나머지는 모두 검사함 (현재 판정 검사를 완화하지 않음)."""
    if isinstance(obj, dict) and top:
        hist = set(obj["record_scope"]["history"])
        obj = {k: v for k, v in obj.items() if k not in hist}
    if isinstance(obj, dict):
        if "trial_id" in obj and (obj.get("match_status") == "CRITERIA_MATCH" or obj.get("eligibility") == "ELIGIBLE"):
            acc.add(obj["trial_id"])
        for v in obj.values():
            _eligible_marks(v, acc, False)
    elif isinstance(obj, list):
        for v in obj:
            _eligible_marks(v, acc, False)
    return acc


# ---------------------------------------------------------------------------
# 1. POST 응답 = GET
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", WITHDRAW)
def test_create_request_response_matches_get_after_withdrawal(env, how):
    patient, staff, verifier, clinician = clients()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    withdraw(env, how)
    pid = queue_for(staff, cid)[0]
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=D.STAFF, json={"proposal_ids": [pid]})
    assert r.status_code == 200, r.text
    post = case_state(r.json()["case"])
    assert_no_ce(post)
    assert post["authority_changed"] is True
    get = case_state(D.staff_case(staff, cid))
    assert post == get
    Q.assert_one_state(Q.statuses(staff, patient, cid))


@pytest.mark.parametrize("how", WITHDRAW)
def test_set_status_response_matches_get_after_withdrawal(env, how):
    patient, staff, verifier, clinician = clients()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    withdraw(env, how)
    pid = queue_for(staff, cid)[0]
    rid = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=D.STAFF,
                     json={"proposal_ids": [pid]}).json()["created"][0]
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid}/status", headers=D.STAFF,
                   json={"status": "document_unavailable"})
    assert r.status_code == 200, r.text
    post = case_state(r.json()["case"])
    assert_no_ce(post)
    assert post == case_state(D.staff_case(staff, cid))


def test_post_response_unchanged_without_withdrawal(env):
    """대조: 권한이 그대로면 POST 응답은 저장값 그대로 (refresh_pairs가 아무것도 바꾸지 않음)."""
    patient, staff, verifier, clinician = clients()
    cid, by_key = D.answered_lab_case(patient, staff)
    doc = D.register(staff, cid)
    case = doc["case"]
    assert case["authority_changed"] is False
    assert case_state(case) == case_state(D.staff_case(staff, cid))


# ---------------------------------------------------------------------------
# 2. Inspector = 사례 화면
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", WITHDRAW)
def test_inspector_shows_current_authority_after_withdrawal(env, how):
    patient, staff, verifier, clinician = clients()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    insp = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=D.STAFF).json()
    assert _eligible_marks(insp, set()) >= {"T-A", "T-D"}  # 대조: 철회 전에는 확정으로 보임
    withdraw(env, how)
    case = D.staff_case(staff, cid)
    insp = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=D.STAFF).json()
    ce = {t for t, p in case["pairs"].items() if p["status"] == CE}
    assert not ce
    assert _eligible_marks(insp, set()) - ce == set()
    final = {x["trial_id"]: (x.get("match_status"), x.get("eligibility")) for x in insp["overview"]["final"]}
    assert all(ms != "CRITERIA_MATCH" and el != "ELIGIBLE" for ms, el in final.values()), final
    assert all(t["eligibility"] != "ELIGIBLE" for t in insp["trials"]), [t["eligibility"] for t in insp["trials"]]
    assert insp["steps"], "라운드 이력은 그대로 남음 (감사 기록)"
    shown = {r["trial_id"]: r["match_status"] for r in case["view"]["result"]["recommendation"]["ranked"]}
    assert "CRITERIA_MATCH" not in json.dumps(insp.get("overview", {}), ensure_ascii=False)
    assert "CRITERIA_MATCH" not in shown.values()


# ---------------------------------------------------------------------------
# 3. 사례 화면의 다음 행동 = Cohort C 대기열 = Insight
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", WITHDRAW)
def test_case_proposals_match_queue_after_withdrawal(env, how):
    patient, staff, verifier, clinician = clients()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    withdraw(env, how)
    queued = queue_for(staff, cid)
    assert queued, "철회 뒤 확인이 다시 필요한 기준에 대기열 행동이 있어야 함"
    case = D.staff_case(staff, cid)
    props = {p["proposal_id"]: p for p in case["proposals"]}
    assert set(queued) <= set(props)
    assert all(props[p]["requestable"] for p in queued)
    insight = staff.get("/api/followup/staff/insight", headers=D.STAFF).json()
    mine = [p for q in insight["questions"] for p in q["patients"] if p["case_id"] == cid]
    assert mine and all(p["requestable"] for p in mine)
    assert {p["proposal_id"] for p in mine} <= set(props)


# ---------------------------------------------------------------------------
# 4. 철회 뒤 어떤 화면·동작에서도 확정 적격이 다시 나타나지 않음
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", WITHDRAW)
def test_confirmed_eligible_never_reappears_after_withdrawal(env, how):
    patient, staff, verifier, clinician = clients()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    withdraw(env, how)

    def check(label):
        st = Q.statuses(staff, patient, cid)
        assert CE not in st["pairs"].values() and CE not in st["counts"], label
        Q.assert_one_state(st)
        insp = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=D.STAFF).json()
        assert not _eligible_marks(insp, set()), label
        assert set(queue_for(staff, cid)) <= {p["proposal_id"] for p in D.staff_case(staff, cid)["proposals"]}, label

    check("after restart")
    created = []
    for pid in queue_for(staff, cid):
        r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=D.STAFF, json={"proposal_ids": [pid]})
        assert r.status_code == 200, r.text
        assert_no_ce(case_state(r.json()["case"]))
        created += r.json()["created"]
        check(f"after create {pid}")
    D.restart(env)  # 같은 (철회된) 권한으로 다시 시작해도 저장된 확정이 되살아나지 않음
    check("after second restart")
    for rid in created:
        r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid}/status", headers=D.STAFF,
                       json={"status": "cancelled"})
        if r.status_code == 200:
            assert_no_ce(case_state(r.json()["case"]))
        check(f"after status {rid}")


# ---------------------------------------------------------------------------
# 5. 과거 평가 기록은 그대로, 현재 판정과 구분되고 안내가 붙음
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", WITHDRAW)
def test_inspector_history_kept_and_labelled(env, how):
    patient, staff, verifier, clinician = clients()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    before = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=D.STAFF).json()
    withdraw(env, how)
    after = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=D.STAFF).json()
    scope = after["record_scope"]
    assert scope["history_notice"] == NOTICE and before["record_scope"]["history_notice"] == NOTICE
    assert "steps" in scope["history"] and not set(scope["history"]) & set(scope["current"])
    assert {"overview", "trials", "recommendation"} <= set(scope["current"])
    # 과거 이력은 고쳐 쓰거나 지우지 않음: 철회 전과 같은 기록 (평가 당시 확정 포함)
    assert after["steps"] == before["steps"]
    assert any(s["trial_id"] == "T-D" and s["eligibility"] == "ELIGIBLE" for s in after["steps"])
    # 현재 판정 부분에는 확정이 없음 (과거 이력에만 있음)
    assert not _eligible_marks(after, set())
    assert after["authority_changed"] is True and before["authority_changed"] is False


def test_staff_inspector_screen_separates_history():
    js = (Path(__file__).parents[1] / "src" / "ctrec" / "web" / "static" / "staff.js").read_text(encoding="utf-8")
    body = js[js.index("async function inspector()"):js.index("// ------------------------------------------------------------ 확인 대기열")]
    assert 'data-scope="current"' in body and 'data-scope="history"' in body
    assert "과거 평가 기록" in body and "scope.history_notice" in body and NOTICE in body
    assert body.index('data-scope="current"') < body.index('data-scope="history"')
