"""문서 정보원 소진 뒤 실행할 수 없는 ASK_PATIENT 추천 수정 (PR #53 dba06d9 재현, 합성 데이터, LLM 호출 없음).

결함: 진료 기록 근거가 필요한 기준(검사 수치)의 문서 정보원(lab_report, medical_record)이 모두 '문서 없음'이 되면
full_dcvc.route_evidence가 원래 환자 질문(ASK_PATIENT missing_required_info)을 되돌려, 실패한 문서 요청과 같은 행동 ID로
대기열 1순위에 남음 (요청하면 409 already_unavailable). _cut_off_items는 근거 경로를 거치지 않아 그 질문을 '살아 있는
행동'으로 보고 항목을 소진으로 세지 않음.
수정: 문서 경로가 있었는데 모두 실패하면 DEFER(item_sources_exhausted). 후보 생성·소진 판단이 같은 reroute를 씀.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.agents import matcher  # noqa: E402
from ctrec.decision_state import build_state  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.dcvc import FullDCVCPolicy  # noqa: E402
from ctrec.eval_hygiene import AliasMap  # noqa: E402

import decision_fixtures as d  # noqa: E402
import test_document_clinician_completion as D  # noqa: E402
import test_followup_flow as F  # noqa: E402
from test_overnight_invariants import assert_invariants, clients  # noqa: E402
from test_pr20_review_regressions import HB, LAB_REPORT, RECORD, _lab  # noqa: E402

env = D.env
AM = AliasMap(salt="exhausted-doc-defer")
PA = AM.alias("SYN-EXH")
HGB, ANC, PLT = "lab:hemoglobin", "lab:absolute neutrophil count", "lab:platelets"
LABS = [HB, _lab("I3", "absolute neutrophil count"), _lab("I4", "platelets")]
UNAVAILABLE = ("failed", C.UNREACHABLE)  # 웹 '문서 없음' = 정보원 단위 실패 (service.update_status)
NO_ANSWER = ("no_response", C.NO_ANSWER)  # 항목 단위 실패 (문서는 왔지만 그 값이 없음 / 환자 '모름')
EXHAUSTED = ("sources_exhausted", "item_sources_exhausted")


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)


def _cohort(rules=tuple(LABS)):
    s = d.store(("age", 60, {"unit": "years"}), patient="SYN-EXH")
    return CohortState.from_decision_states(
        {PA: build_state(d.view({"T1": d.trial("T1", list(rules))}, s, patient="SYN-EXH"))})


def _by_key(pol, co):
    out = {}
    for a in pol.propose(co):
        out.setdefault(a.info_key, []).append(a)
    return out


def _executing(acts):
    return [a for a in acts if a.action_type not in C.NON_EXECUTING]


def _fail_docs(pol, co, key, outcome):
    """그 항목의 문서 요청을 남은 문서가 없을 때까지 차례로 실패시킴. 반환: (코호트, 실패한 행동들)."""
    failed = []
    for _ in range(6):
        acts = [a for a in _executing(_by_key(pol, co).get(key, [])) if a.action_type == "REQUEST_DOCUMENT"]
        if not acts:
            break
        a = acts[0]
        failed.append(a)
        co = pol.apply_outcome(co, a, C.ActionOutcome(action_id=a.action_id, status=outcome[0],
                                                       error_code=outcome[1]))[0]
    return co, failed


# --------------------------------------------------------------------------- 엔진 (순수 정책)

def test_record_only_lab_starts_as_document_request():
    """정상 경로 유지: 문서가 남아 있으면 REQUEST_DOCUMENT(record_evidence_required)."""
    acts = _by_key(FullDCVCPolicy(), _cohort())
    for key in (HGB, ANC, PLT):
        ex = _executing(acts[key])
        assert [(a.action_type, a.source_id, a.reason_code) for a in ex] == \
            [("REQUEST_DOCUMENT", LAB_REPORT, "record_evidence_required")], (key, ex)


def test_hb_documents_unavailable_defers_instead_of_asking_patient():
    pol = FullDCVCPolicy()
    co, failed = _fail_docs(pol, _cohort(), HGB, UNAVAILABLE)
    assert [a.source_id for a in failed] == [LAB_REPORT, RECORD]
    hb = _by_key(pol, co)[HGB]
    assert not _executing(hb), hb  # ASK_PATIENT 미생성, 1순위 없음
    assert [(a.action_type, a.reason_code, a.source_id) for a in hb] == [("DEFER", "item_sources_exhausted", None)]
    # 죽은 1순위였던 행동 ID(실패한 문서 요청과 같음)는 실행 대상이 아님
    assert hb[0].action_id == failed[-1].action_id and not hb[0].safety_required
    # 소진 판단이 후보와 같은 값: 항목 소진으로 기록, 쌍은 BLOCKED_FAILURE (INELIGIBLE 아님)
    assert HGB in co.ops[PA].exhausted_items
    assert co.pair(PA, "T1").status == C.PairStatus.BLOCKED_FAILURE


def test_source_wide_unavailable_covers_anc_and_platelets_from_the_same_reports():
    """'문서 없음'은 정보원 단위: lab_report·medical_record에서 받을 ANC·혈소판도 같은 이유로 보류 (환자 질문 아님)."""
    pol = FullDCVCPolicy()
    co, _ = _fail_docs(pol, _cohort(), HGB, UNAVAILABLE)
    acts = _by_key(pol, co)
    for key in (ANC, PLT):
        assert not _executing(acts[key]), (key, acts[key])
        assert {(a.action_type, a.reason_code) for a in acts[key]} <= {("DEFER", r) for r in EXHAUSTED}
        assert key in co.ops[PA].exhausted_items


def test_item_level_document_failure_leaves_other_labs_requestable():
    """항목 단위 실패(문서에 Hb 값 없음)는 Hb만 막음: 같은 검사지의 ANC·혈소판은 여전히 lab_report 요청."""
    pol = FullDCVCPolicy()
    co, failed = _fail_docs(pol, _cohort(), HGB, NO_ANSWER)
    assert [a.source_id for a in failed] == [LAB_REPORT, RECORD]
    acts = _by_key(pol, co)
    assert [(a.action_type, a.reason_code) for a in acts[HGB]] == [("DEFER", "item_sources_exhausted")]
    for key in (ANC, PLT):
        assert [(a.action_type, a.source_id) for a in _executing(acts[key])] == [("REQUEST_DOCUMENT", LAB_REPORT)]
    assert co.ops[PA].exhausted_items == frozenset({HGB})
    assert co.pair(PA, "T1").status != C.PairStatus.BLOCKED_FAILURE  # 다른 항목을 아직 얻을 수 있음


def test_candidates_and_cut_off_items_agree():
    """후보 생성(propose)과 소진 판단(_cut_off_items)이 같은 근거 경로를 씀."""
    pol = FullDCVCPolicy()
    co, _ = _fail_docs(pol, _cohort(), HGB, NO_ANSWER)
    ops = co.ops[PA]
    cut = pol._cut_off_items(co, PA, {"T1"}, ops)
    held = {a.info_key for acts in _by_key(pol, co).values() for a in acts
            if a.action_type == "DEFER" and a.reason_code in EXHAUSTED}
    live = {a.info_key for acts in _by_key(pol, co).values() for a in _executing(acts)}
    assert cut == held - live == {HGB}
    assert ops.exhausted_items >= cut


def test_routing_off_keeps_legacy_ask_and_remaining_asks_are_not_record_only():
    """evidence_routing=False(0.1 동작)는 그대로. 근거 경로 뒤 남은 환자 질문은 진료 기록 필요 기준을 겨냥하지 않음."""
    co = _cohort()
    acts = _by_key(FullDCVCPolicy(evidence_routing=False), co)
    assert any(a.action_type == "ASK_PATIENT" for a in acts[HGB])
    pol = FullDCVCPolicy()
    for a in pol.propose(co):
        if a.action_type == "ASK_PATIENT":
            assert not pol.record_only(co, a)  # 남은 환자 질문은 진료 기록 필요 기준을 겨냥하지 않음


# --------------------------------------------------------------------------- 웹 follow-up (Cohort C 대기열·409)

def _queue(staff, cid):
    q = staff.get("/api/followup/staff/queue", headers=D.STAFF).json()
    return [i for i in q["items"] if i["case_id"] == cid]


def _exhaust_hb(patient, staff, trials, text=D.TEXT):
    cid = D.create(patient, trials, text=text)
    case = D.staff_case(staff, cid)
    chain = D.make_requests(staff, cid, D.proposal(case, HGB, "REQUEST_DOCUMENT")["proposal_id"])["created"][0]
    routed = []
    for _ in range(6):
        r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{chain}/status", headers=D.STAFF,
                       json={"status": "document_unavailable"})
        assert r.status_code == 200, r.text
        nxt = [x for x in r.json()["case"]["requests"] if x["follows"] == chain]
        routed += nxt
        if not nxt or nxt[0]["kind"] == "clinician_review":
            break
        chain = nxt[0]["request_id"]
    return cid, routed


def test_web_queue_has_no_dead_rank_one_after_hb_documents_unavailable(env):
    patient, staff, _, _ = clients()
    cid, routed = _exhaust_hb(patient, staff, ["T-A", "T-B", "T-C", "T-D"])
    assert [(r["kind"], r["reason_code"]) for r in routed] == [
        ("document_review", "record_evidence_required"), ("clinician_review", "item_sources_exhausted")]
    case = D.staff_case(staff, cid)
    labs = [p for p in case["proposals"] if p["info_key"].startswith("lab:")]
    assert labs == [], labs  # ASK_PATIENT 없음, 실행할 수 없는 제안 없음
    queue = _queue(staff, cid)
    assert not [i for i in queue if i["action_type"] == "ASK_PATIENT" or i["request_status"] == "document_unavailable"]
    # 남은 제안은 모두 요청 가능 (409 already_unavailable 없음)
    for p in case["proposals"]:
        r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=D.STAFF,
                       json={"proposal_ids": [p["proposal_id"]]})
        assert r.status_code == 200, (p, r.text)
    assert case["pairs"]["T-C"]["status"] == "BLOCKED_FAILURE"  # 보류 (INELIGIBLE 아님)
    assert_invariants(staff, patient, cid)


def test_web_four_trials_anc_platelets_and_other_sources(env):
    """ANC·혈소판(같은 검사지)도 환자 질문으로 바뀌지 않음. 다른 정보원(병리 EGFR)과 임상 검토는 그대로."""
    patient, staff, _, _ = clients()
    cid, _ = _exhaust_hb(patient, staff, ["T-A", "T-B", "T-C", "T-D"])
    case = D.staff_case(staff, cid)
    props = [(p["action_type"], p["info_key"]) for p in case["proposals"]]
    assert not [p for p in props if p[1].startswith("lab:")], props
    assert ("REQUEST_DOCUMENT", "biomarker:EGFR") in props
    assert any(t == "CLINICIAN_REVIEW" for t, _ in props)
    assert not [i for i in _queue(staff, cid) if i["action_type"] == "ASK_PATIENT"]
    assert not [t for t, p in case["pairs"].items()
                if p["status"] in ("CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE") and t != "T-D"]
    assert_invariants(staff, patient, cid)


def test_web_self_report_after_exhaustion_cannot_confirm(env):
    """환자가 처음 입력에서 Hb 값을 자기 보고했어도, 문서 경로가 소진되면 CE가 되지 않고 환자 질문도 다시 생기지 않음."""
    patient, staff, _, _ = clients()
    cid, _ = _exhaust_hb(patient, staff, ["T-C"], text=D.TEXT + "\nHemoglobin 13.5 g/dL")
    case = D.staff_case(staff, cid)
    assert not [p for p in case["proposals"] if p["action_type"] == "ASK_PATIENT"]
    assert all(p["status"] != "CONFIRMED_ELIGIBLE" for p in case["pairs"].values()), case["pairs"]
    reqs = patient.get(f"/api/followup/cases/{cid}/requests").json()["requests"]
    assert not [r for r in reqs if r.get("info_key") == HGB]
    assert_invariants(staff, patient, cid)


def test_web_unknown_after_hb_exhaustion_does_not_reask(env):
    """자기 보고 허용 기준(나이)은 환자 질문 경로 유지. '모르겠어요' 뒤 같은 질문 없음, Hb도 환자에게 묻지 않음."""
    patient, staff, _, _ = clients()
    cid, _ = _exhaust_hb(patient, staff, ["T-A", "T-B", "T-C"], text=F.TEXT_NO_AGE)
    case = D.staff_case(staff, cid)
    age = D.proposal(case, "age", "ASK_PATIENT")  # 정상 경로: 자기 보고 허용 기준은 ASK_PATIENT
    rid = D.make_requests(staff, cid, age["proposal_id"])["created"][0]
    r = patient.post(f"/api/followup/cases/{cid}/requests/{rid}/response", json={"unknown": True})
    assert r.status_code == 200 and r.json()["status"] == "unknown", r.text
    case = D.staff_case(staff, cid)
    asks = [(p["info_key"], p["proposal_id"]) for p in case["proposals"] if p["action_type"] == "ASK_PATIENT"]
    assert not [k for k, _ in asks if k in ("age", HGB)], asks
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=D.STAFF,
                   json={"proposal_ids": [age["proposal_id"]]})
    assert r.status_code == 409 and D.code(r) in ("already_unknown", "proposal_not_current")
    assert [x["info_key"] for x in case["requests"] if x["kind"] == "ask_patient"] == ["age"]
    assert not [t for t, p in case["pairs"].items() if p["status"] == "CONFIRMED_ELIGIBLE"]
    assert_invariants(staff, patient, cid)
