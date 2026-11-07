"""Q 통합 점검: O 추가 정보 확인 API(실제 HTTP 경로) x N의 NSCLC 시험 세트(운영 카탈로그 = trial_store).

O의 테스트는 기준표를 함께 넣은 합성 카탈로그(sim.demo)만 썼고, N의 E2E는 기본 Session만 썼다. 이 파일은 두 변경이 만나는
운영 경로를 본다: followup_api.default_catalog() -> app._load_trials() -> config.TRIALS_DIR(N: CTREC_TRIAL_SET=nsclc)
-> trial_store(data/nsclc/parsed). LLM은 N의 시나리오 대역(six_stage_harness)이고 실제 API 호출은 없다. 시험은 실제
ClinicalTrials.gov NSCLC 프로토콜의 WP2a 파싱본(임상 검수 전), 환자는 합성이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sys.path.insert(0, str(Path(__file__).parent))

import six_stage_harness as H  # noqa: E402
import test_six_stage_e2e as N  # noqa: E402
import web_harness as h  # noqa: E402
from ctrec.followup import codec  # noqa: E402
from ctrec.followup.service import FollowupService  # noqa: E402
from ctrec.followup.store import CaseStore  # noqa: E402
from ctrec.web import app as web  # noqa: E402
from ctrec.web import followup_api  # noqa: E402

KEY = "synthetic-staff-key-q"
STAFF = {"X-CTREC-Staff-Key": KEY, "X-CTREC-Staff-Id": "coord-q"}
TID, UNPARSED = N.TID, "NCT04892953"
E2 = f"{TID}:E2"
E2_KEY = f"criterion:{E2}"
CONFIRMED = "CONFIRMED_ELIGIBLE"


def profile_ecog_from_text(user: str):
    """처음 입력은 ECOG 1. 답변에 'ECOG 3'이 오면 그 값을 함께 추출 (충돌 확인용 대역)."""
    p = N.profile_full(user)
    if "ECOG 3" in user:
        p.ecog = 3
    return p


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "TEST_LLM_BACKEND", "fake")
    monkeypatch.setenv("CTREC_STAFF_KEY", KEY)
    for name in ("CTREC_REQUIRE_HTTPS", "CTREC_ALLOW_INSECURE_HTTP", "CTREC_COOKIE_SECURE", "CTREC_SESSION"):
        monkeypatch.delenv(name, raising=False)
    H.install(monkeypatch, H.Scenario(profile_ecog_from_text, N.verdicts_ild_missing, N.ild_question))
    root = tmp_path / "followup"
    followup_api.configure(FollowupService(CaseStore(root), followup_api.default_catalog()))
    yield root
    followup_api.configure(None)


def restart(root: Path) -> None:
    followup_api.configure(FollowupService(CaseStore(root), followup_api.default_catalog()))


def clients():
    return h.browser(), h.browser(client=("198.51.100.30", 50001))


def create(patient, trials=(TID, UNPARSED)) -> str:
    r = patient.post("/api/followup/cases", json={"patient_text": N.TEXT_NO_ILD, "trial_ids": list(trials)})
    assert r.status_code == 201, r.text
    return r.json()["case_id"]


def staff_case(staff, cid) -> dict:
    r = staff.get(f"/api/followup/staff/cases/{cid}", headers=STAFF)
    assert r.status_code == 200, r.text
    return r.json()


def proposal(case, info_key, action="ASK_PATIENT") -> dict:
    return next(p for p in case["proposals"] if p["info_key"] == info_key and p["action_type"] == action)


def request(staff, cid, pid, expect=200) -> dict:
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=STAFF, json={"proposal_ids": [pid]})
    assert r.status_code == expect, r.text
    return r.json()


def answer(patient, cid, rid, expect=200, **body) -> dict:
    r = patient.post(f"/api/followup/cases/{cid}/requests/{rid}/response", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def assert_consistent(staff, cid, case) -> None:
    """쌍 상태, 추천, Inspector, Cohort C 대기열이 같은 평가를 말하는지."""
    pairs = case["pairs"]
    assert all(p["status"] != CONFIRMED for p in pairs.values())
    rec = case["view"]["result"]["recommendation"]
    for r in rec["recommended"]:
        assert r["match_status"] != "CRITERIA_MATCH" or pairs[r["trial_id"]]["status"] == CONFIRMED
    excluded = {e["trial_id"] if isinstance(e, dict) else e for e in rec["excluded"]}
    for tid, p in pairs.items():
        if p["status"] == "NOT_EVALUATED":
            assert tid in excluded
    ins = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=STAFF)
    assert ins.status_code == 200
    ins_rec = ins.json()["recommendation"]
    assert ins_rec["recommended"] == [r["trial_id"] for r in rec["recommended"]]
    assert set(ins_rec["excluded"]) == excluded
    assert ins.json()["followup"]["evaluations"][-1]["revision"] == case["evaluations"][-1]["revision"]
    queue = staff.get("/api/followup/staff/queue", headers=STAFF).json()
    mine = [i["proposal_id"] for i in queue["items"] if i["case_id"] == cid]
    assert mine == [p["proposal_id"] for p in case["proposals"]]


# ----------------------------------------------------------------------------- 1. NSCLC 파싱본이 운영 경로에 닿음
def test_production_catalog_reads_nsclc_protocols(env):
    cat = followup_api.default_catalog()
    assert set(N.NSCLC_WITH_PARSED + N.NSCLC_WITHOUT_PARSED) <= set(cat.trials)
    patient, staff = clients()
    cid = create(patient)
    case = staff_case(staff, cid)
    assert case["synthetic"] is False
    assert case["pairs"][UNPARSED]["status"] == "NOT_EVALUATED"  # 파싱본 없음 -> 평가하지 않음
    assert case["pairs"][TID]["status"] == "INFO_MISSING"
    assert proposal(case, E2_KEY)["requestable"]


def test_restored_session_keeps_trial_store_criteria(env):
    """회귀: 운영 카탈로그는 기준표를 주지 않으므로 복원한 Session이 trial_store에서 다시 읽어야 함.

    읽지 않으면 계획이 비어 담당자의 모든 요청이 proposal_not_current(409)가 되고 Cohort C 대기열이 빔."""
    patient, staff = clients()
    cid = create(patient)
    s = codec.restore_session(CaseStore(env).load(cid)["session"], followup_api.default_catalog().trials)
    assert TID in s.parsed and UNPARSED not in s.parsed
    case = staff_case(staff, cid)
    request(staff, cid, proposal(case, E2_KEY)["proposal_id"])
    assert staff.get("/api/followup/staff/queue", headers=STAFF).json()["items"]


# ----------------------------------------------------------------------------- 2·4·5·6. 요청 -> 응답 -> 재평가
def test_nsclc_patient_staff_patient_reevaluation(env):
    patient, staff = clients()
    cid = create(patient)
    case = staff_case(staff, cid)
    assert_consistent(staff, cid, case)
    pid = proposal(case, E2_KEY)["proposal_id"]
    rid = request(staff, cid, pid)["created"][0]

    restart(env)  # 요청 이력은 서버를 다시 띄워도 남음
    mine = patient.get(f"/api/followup/cases/{cid}/requests").json()
    assert mine["pending"] == 1 and mine["requests"][0]["request_id"] == rid
    assert "target_criteria" not in mine["requests"][0]  # 환자 화면에 내부 기준 키 없음

    a = answer(patient, cid, rid, answer="둘 다 해당 없음")
    assert a["status"] == "answered" and a["conflict"] is False
    case = staff_case(staff, cid)
    ev = case["evaluations"][-1]
    assert ev["equivalence"]["status"] == "match"  # 증분 재평가 == 전체 재평가
    change = next(c for c in ev["criteria_changes"] if c["criterion"] == E2)
    assert change["before"]["passes"] is None and change["after"]["passes"] is True
    assert case["pairs"][TID]["status"] != CONFIRMED  # 자기 보고 + 문서·임상 확인 남음
    assert {"physician_review", "unverified"} <= set(case["pairs"][TID]["gaps"])
    assert_consistent(staff, cid, case)

    # 반복·중복 안전: 같은 제안은 다시 요청할 수 없고, 같은 답 재전송은 중복, 다른 답은 거절
    request(staff, cid, pid, expect=409)
    assert all(not (p["action_type"] == "ASK_PATIENT" and p["info_key"] == E2_KEY) for p in case["proposals"])
    assert answer(patient, cid, rid, answer="둘 다  해당 없음")["duplicate"] is True
    answer(patient, cid, rid, expect=409, answer="현재 간질성 폐질환/폐렴이 있거나 의심됨")
    assert staff_case(staff, cid)["revision"] == case["revision"]


# ----------------------------------------------------------------------------- 3. 모름·무응답·상충
@pytest.mark.parametrize("how", ["unknown", "unreachable"])
def test_nsclc_unknown_or_unreachable_routes_to_document_without_repeat(env, how):
    patient, staff = clients()
    cid = create(patient)
    pid = proposal(staff_case(staff, cid), E2_KEY)["proposal_id"]
    rid = request(staff, cid, pid)["created"][0]
    before = staff_case(staff, cid)["pairs"][TID]["status"]
    if how == "unknown":
        assert answer(patient, cid, rid, unknown=True)["status"] == "unknown"
    else:
        r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid}/status", headers=STAFF,
                       json={"status": "unreachable"})
        assert r.status_code == 200, r.text
    case = staff_case(staff, cid)
    follow = [r for r in case["requests"] if r["follows"] == rid]
    assert len(follow) == 1 and follow[0]["kind"] in ("document_review", "clinician_review")
    assert follow[0]["created_by"] == "system"
    assert case["pairs"][TID]["status"] == before  # 사실을 만들지 않음 -> 판정 그대로
    # 같은 제안을 다시 골라도 새 요청이 생기지 않음 (환자 질문 반복 없음). '모름'은 제안이 사라져 409,
    # 연락 불가는 같은 행동 ID의 대체 요청을 돌려줌 (created 없음)
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=STAFF, json={"proposal_ids": [pid]})
    assert r.status_code in (200, 409), r.text
    if r.status_code == 200:
        assert r.json()["created"] == [] and r.json()["existing"] == [follow[0]["request_id"]]
    assert len(staff_case(staff, cid)["requests"]) == len(case["requests"])
    assert all(not (p["action_type"] == "ASK_PATIENT" and p["info_key"] == E2_KEY) for p in case["proposals"])
    assert patient.get(f"/api/followup/cases/{cid}/requests").json()["pending"] == 0
    assert_consistent(staff, cid, case)


def test_nsclc_conflicting_answer_holds_and_routes(env):
    patient, staff = clients()
    cid = create(patient)
    rid = request(staff, cid, proposal(staff_case(staff, cid), E2_KEY)["proposal_id"])["created"][0]
    a = answer(patient, cid, rid, answer="둘 다 해당 없음\nECOG 3")
    assert a["conflict"] is True
    case = staff_case(staff, cid)
    conflict = [r for r in case["requests"] if r["kind"] == "conflict_resolution"]
    assert len(conflict) == 1 and conflict[0]["conflict"]["field"] == "ecog"
    assert "conflict" in case["pairs"][TID]["gaps"]
    assert case["pairs"][TID]["status"] not in (CONFIRMED, "CONFIRMED_INELIGIBLE")  # 어느 값도 채택하지 않음
    assert any(p["action_type"] == "VERIFY_RECORD" and p["safety_required"] for p in case["proposals"])
    assert_consistent(staff, cid, case)


# ----------------------------------------------------------------------------- Q-P: P 공통 엔진과 Follow-up의 연결
def test_followup_routes_by_evidence_requirement(env):
    """자기 보고 허용 기준 = 환자 질문, 진료 기록 필요 기준 = 문서 확인, 임상의 전용(원문에 구체 기준 없음) = 임상 검토.

    근거 요건은 판정 메타데이터(required_evidence, gap)에서 오고, 행동 유형은 P full_dcvc 근거 경로가 정함."""
    patient, staff = clients()
    cid = create(patient, trials=(TID,))
    case = staff_case(staff, cid)
    routes = {}
    for p in case["proposals"]:
        for k in p["target_criteria"]:
            routes.setdefault(k, set()).add((p["action_type"], p["route"]))
    assert routes[E2] == {("ASK_PATIENT", "ask_patient")}  # self_report_ok, 정보 없음
    for k in ("I1", "I3", "I4", "I5"):  # clinical_record
        assert routes[f"{TID}:{k}"] == {("REQUEST_DOCUMENT", "document_review")}, k
    for k in ("I2", "E3", "E4"):  # physician_review (원문에 구체 기준 없음, 임상의만 판단)
        assert routes[f"{TID}:{k}"] == {("CLINICIAN_REVIEW", "clinician_review")}, k
    review = next(p for p in case["proposals"] if p["action_type"] == "CLINICIAN_REVIEW")
    out = request(staff, cid, review["proposal_id"])
    made = next(r for r in out["case"]["requests"] if r["request_id"] == out["created"][0])
    assert (made["kind"], made["status"]) == ("clinician_review", "requires_clinician")
    assert patient.get(f"/api/followup/cases/{cid}/requests").json()["pending"] == 0  # 환자 화면에 나오지 않음
    assert out["case"]["pairs"][TID]["status"] != CONFIRMED


def test_no_document_authority_keeps_the_pair_held(env):
    """문서 확인 권한·임상의 판단 입력이 없으면 모든 확인을 요청해도 판정은 보류 (확정 적격 없음, 완료 상태 없음)."""
    patient, staff = clients()
    cid = create(patient, trials=(TID,))
    case = staff_case(staff, cid)
    out = request_all = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=STAFF,
                                   json={"proposal_ids": [p["proposal_id"] for p in case["proposals"]
                                                          if p["requestable"]]})
    assert out.status_code == 200, out.text
    ask = next(r for r in request_all.json()["case"]["requests"] if r["kind"] == "ask_patient")
    answer(patient, cid, ask["request_id"], answer="둘 다 해당 없음")
    case = staff_case(staff, cid)
    assert case["pairs"][TID]["status"] != CONFIRMED
    assert {"physician_review", "unverified"} <= set(case["pairs"][TID]["gaps"])
    assert {r["kind"] for r in case["requests"]} >= {"ask_patient", "document_review", "clinician_review"}
    assert not {r["status"] for r in case["requests"]} & {"document_verified", "clinician_confirmed", "completed"}
    assert_consistent(staff, cid, case)


def test_followup_plans_and_settles_through_the_common_engine_loop(env, monkeypatch):
    """O가 정책을 직접 부르지 않고 P의 EngineLoop(웹·WP6와 같은 객체)로 계획·정산 (세 번째 선택 경로 없음)."""
    import ast
    import inspect

    from ctrec.engine import loop as engine_loop
    from ctrec.followup import service as fsvc
    tree = ast.parse(inspect.getsource(fsvc))
    direct = sorted({n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr in ("select", "apply_outcome", "propose")})
    assert direct == [], direct
    calls = []
    for name in ("preview", "settle"):
        real = getattr(engine_loop.EngineLoop, name)
        monkeypatch.setattr(engine_loop.EngineLoop, name,
                            lambda self, *a, _n=name, _r=real, **k: (calls.append(_n), _r(self, *a, **k))[1])
    patient, staff = clients()
    cid = create(patient, trials=(TID,))
    rid = request(staff, cid, proposal(staff_case(staff, cid), E2_KEY)["proposal_id"])["created"][0]
    answer(patient, cid, rid, unknown=True)
    assert "preview" in calls and "settle" in calls
