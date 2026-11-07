"""추가 정보 확인 흐름 E2E (TRIALOG O): 환자 입력 -> 연구 담당자 요청 -> 환자 응답 -> 재평가 -> 갱신된 추천.

FastAPI TestClient로 실제 HTTP 경로(권한·CSRF·쿠키)를 거치고, 실제 Session·Matcher·규칙 엔진·정책 엔진(FullDCVCPolicy)을
씁니다. LLM은 오프라인 대역(question_eval.OfflineEnvironment)이고 시험은 합성 NSCLC 기준표(sim.demo, 실제 시험 아님)입니다.
환자 정보 대역은 같은 항목이 여러 번 나오면 가장 나중 언급을 씀 (확인 답변을 반영하는 실제 추출을 흉내).
실제 환자 데이터·실제 API는 쓰지 않습니다.
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
from ctrec.followup import service as fsvc  # noqa: E402
from ctrec.followup.service import FollowupService, TrialCatalog  # noqa: E402
from ctrec.followup.store import CaseStore, StaleWrite  # noqa: E402
from ctrec.sim import demo  # noqa: E402
from ctrec.web import app as web  # noqa: E402
from ctrec.web import followup_api  # noqa: E402

STAFF_KEY = "synthetic-staff-key-for-tests"
STAFF = {"X-CTREC-Staff-Key": STAFF_KEY, "X-CTREC-Staff-Id": "coord-1"}
TEXT = "Synthetic NSCLC patient. 64-year-old male with stage IV non-small cell lung cancer. ECOG 1."
TRIALS = ["T-A", "T-B", "T-C"]
CONFIRMED_ELIGIBLE = "CONFIRMED_ELIGIBLE"


def latest_profiler(patient_id, raw_text, reference_date=None):
    """대역 정보 추출: 문장 순서를 뒤집어 같은 항목은 가장 나중 언급을 씀."""
    return qe.stub_profiler(patient_id, "\n".join(reversed(raw_text.splitlines())), reference_date)


def catalog() -> TrialCatalog:
    st = demo.trials()
    return TrialCatalog(name="sim-demo-nsclc", trials={t.parsed.trial_id: t.trial for t in st},
                        parsed={t.parsed.trial_id: t.parsed for t in st})


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "TEST_LLM_BACKEND", "fake")
    monkeypatch.setenv("CTREC_STAFF_KEY", STAFF_KEY)
    for name in ("CTREC_REQUIRE_HTTPS", "CTREC_ALLOW_INSECURE_HTTP", "CTREC_COOKIE_SECURE", "CTREC_SESSION"):
        monkeypatch.delenv(name, raising=False)
    with qe.OfflineEnvironment(3):
        pipeline.patient_profiler.profile_patient = latest_profiler  # 환경이 끝날 때 원래 값으로 되돌림
        root = tmp_path / "followup"
        followup_api.configure(FollowupService(CaseStore(root), catalog()))
        yield root
    followup_api.configure(None)


def restart(root: Path) -> None:
    """서버 재시작: 메모리 상태 없이 같은 저장 위치에서 새 서비스."""
    followup_api.configure(FollowupService(CaseStore(root), catalog()))


def staff_client():
    return h.browser(client=("198.51.100.30", 50001))


def _create(patient, text=TEXT, trials=TRIALS):
    r = patient.post("/api/followup/cases", json={"patient_text": text, "trial_ids": trials})
    assert r.status_code == 201, r.text
    return r.json()


def _staff_case(staff, cid):
    r = staff.get(f"/api/followup/staff/cases/{cid}", headers=STAFF)
    assert r.status_code == 200, r.text
    return r.json()


def _proposal(case, info_key, action_type="ASK_PATIENT"):
    return next(p for p in case["proposals"] if p["info_key"] == info_key and p["action_type"] == action_type)


def _request(staff, cid, *pids, expect=200):
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=STAFF, json={"proposal_ids": list(pids)})
    assert r.status_code == expect, r.text
    return r.json()


def _answer(patient, cid, rid, expect=200, **body):
    r = patient.post(f"/api/followup/cases/{cid}/requests/{rid}/response", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def _no_confirmed_eligible(case):
    assert all(p["status"] != CONFIRMED_ELIGIBLE for p in case["pairs"].values())
    for ev in case.get("evaluations", []):
        assert all(p["status"] != CONFIRMED_ELIGIBLE for p in ev["pairs"].values())


# ---------------------------------------------------------------------------
# 전체 흐름
# ---------------------------------------------------------------------------

def test_full_patient_staff_patient_reevaluation_flow(env):
    patient, staff = h.browser(), staff_client()

    # 1-3. 환자 입력 -> 시험별 평가 -> 부족 정보와 확인 경로
    created = _create(patient)
    cid = created["case_id"]
    assert set(created["pairs"]) == set(TRIALS)
    assert {p["status"] for p in created["pairs"].values()} == {"INFO_MISSING"}
    assert created["view"]["result"]["recommendation"]["ranked"]  # 웹 실행 결과와 같은 형태의 추천
    case = _staff_case(staff, cid)
    _no_confirmed_eligible(case)
    keys = {(p["action_type"], p["info_key"]) for p in case["proposals"]}
    assert {("ASK_PATIENT", "lab:platelets"), ("ASK_PATIENT", "lab:hemoglobin"),
            ("ASK_PATIENT", "biomarker:EGFR")} <= keys
    assert [p["rank"] for p in case["proposals"]] == list(range(1, len(case["proposals"]) + 1))  # C 순서
    plt, hb, egfr = (_proposal(case, k) for k in ("lab:platelets", "lab:hemoglobin", "biomarker:EGFR"))
    assert plt["patient_question_preview"] and plt["requestable"]

    # 4. 연구 담당자가 검토하고 환자에게 요청 (중복 요청은 새로 만들지 않음)
    out = _request(staff, cid, plt["proposal_id"], hb["proposal_id"], egfr["proposal_id"])
    assert len(out["created"]) == 3 and out["existing"] == []
    again = _request(staff, cid, plt["proposal_id"])
    assert again["created"] == [] and again["existing"] == [out["created"][0]]
    by_key = {r["info_key"]: r["request_id"] for r in again["case"]["requests"]}

    # 5. 요청이 보존되고 환자가 확인 (서버를 다시 띄워도)
    restart(env)
    reqs = patient.get(f"/api/followup/cases/{cid}/requests").json()
    assert reqs["pending"] == 3
    assert {r["request_id"] for r in reqs["requests"]} == set(out["created"])
    assert all(r["question"]["question"] and "target_criteria" not in r for r in reqs["requests"])

    # 6-7. 환자 답변 -> 사실·출처 기록 -> 9. 영향받은 기준 재평가 -> 10-11. 쌍 상태·제안·추천 갱신
    rev0 = _staff_case(staff, cid)["revision"]
    a = _answer(patient, cid, by_key["lab:platelets"], answer="Platelets 180 x10^9/L", client_response_id="c-1")
    assert a["duplicate"] is False and a["status"] == "answered" and a["outcome"] == "answered"
    case = _staff_case(staff, cid)
    ev = case["evaluations"][-1]
    assert ev["trigger"] == {"type": "response", "request_id": by_key["lab:platelets"], "outcome": "answered"}
    assert ev["equivalence"]["status"] == "match"  # 증분 재평가 == 전체 재평가
    assert "T-A" in ev["reevaluated_trials"]  # 혈소판 기준이 있는 시험
    assert set(ev["reevaluated_trials"]) | set(ev["reused_trials"]) == set(TRIALS)  # LLM 경로 기준은 모든 항목에 의존
    change = next(c for c in ev["criteria_changes"] if c["criterion"] == "T-A:I3")
    assert change["before"]["passes"] is None and change["after"]["needs_verification"] is True  # 자기 보고: 확인 대기
    resp = case["responses"][-1]
    assert resp["fact_ids_added"] and resp["revision_after"] == rev0 + 1
    insp = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=STAFF)
    assert insp.status_code == 200
    s = fsvc.codec.restore_session(CaseStore(env).load(cid)["session"], catalog().trials, catalog().parsed)
    plt_fact = [f for f in s.fact_store.facts(s.case.patient_id)
                if f.fact_id in resp["fact_ids_added"] and f.field == "lab:platelets"]
    assert plt_fact and plt_fact[0].source_type == "self_report"  # 출처: 확인 답변
    assert plt_fact[0].verification_status == "unknown"  # 자기 보고는 검증으로 올라가지 않음
    assert insp.json()["followup"]["evaluations"][-1]["revision"] == ev["revision"]  # Inspector에 변경 기록
    assert case["view"]["result"]["recommendation"] != created["view"]["result"]["recommendation"]
    _no_confirmed_eligible(case)

    # 동일 응답 중복 처리 방지 (같은 답 재전송 = 중복, 다른 답 = 거절)
    dup = _answer(patient, cid, by_key["lab:platelets"], answer="Platelets  180 x10^9/L")
    assert dup["duplicate"] is True and dup["revision"] == case["revision"]
    _answer(patient, cid, by_key["lab:platelets"], expect=409, answer="Platelets 90 x10^9/L")
    assert _staff_case(staff, cid)["revision"] == case["revision"]

    # '모르겠다' -> 같은 질문 반복 대신 대체 경로 (문서 검토), 환자 화면에는 나오지 않음
    u = _answer(patient, cid, by_key["lab:hemoglobin"], unknown=True)
    assert u["status"] == "unknown"
    case = _staff_case(staff, cid)
    follow = [r for r in case["requests"] if r["follows"] == by_key["lab:hemoglobin"]]
    assert len(follow) == 1 and follow[0]["kind"] == "document_review" and follow[0]["status"] == "requires_document"
    assert follow[0]["created_by"] == "system"
    assert all(not (p["action_type"] == "ASK_PATIENT" and p["info_key"] == "lab:hemoglobin") for p in case["proposals"])
    assert follow[0]["request_id"] not in {r["request_id"] for r in
                                           patient.get(f"/api/followup/cases/{cid}/requests").json()["requests"]}
    _request(staff, cid, hb["proposal_id"], expect=409)  # 같은 환자 질문을 다시 만들 수 없음

    # 8. 기존 사실과 충돌 -> 보류 + 해결 경로 (어느 값도 채택하지 않음)
    c = _answer(patient, cid, by_key["biomarker:EGFR"],
                answer="EGFR exon 19 deletion.\nMy ECOG performance status is 3 now.")
    assert c["conflict"] is True
    case = _staff_case(staff, cid)
    conflict = [r for r in case["requests"] if r["kind"] == "conflict_resolution"]
    assert len(conflict) == 1 and conflict[0]["status"] == "requires_document"
    assert conflict[0]["conflict"]["field"] == "ecog" and len(conflict[0]["conflict"]["fact_ids"]) == 2
    for tid in ("T-A", "T-C"):  # ECOG 기준이 상충 -> 확인 대기가 남은 작업에 들어감 (쌍 상태는 우선순위 하나만 보임)
        assert "conflict" in case["pairs"][tid]["gaps"], case["pairs"][tid]
        assert "conflict" in case["pairs"][tid]["pending_kinds"], case["pairs"][tid]
        assert case["pairs"][tid]["status"] in ("INFO_MISSING", "VERIFICATION_PENDING")
    assert case["evaluations"][-1]["equivalence"]["status"] == "match"
    assert any(p["action_type"] == "VERIFY_RECORD" and p["safety_required"] for p in case["proposals"])
    _no_confirmed_eligible(case)
    # 임상의 판단 완료·문서 확인 완료는 시스템이 만들지 않음
    assert not {r["status"] for r in case["requests"]} & {"clinician_confirmed", "document_verified", "completed"}

    # 중간 상태 유지: 다시 띄워도 요청·응답·평가 이력이 그대로
    before = _staff_case(staff, cid)
    restart(env)
    after = _staff_case(staff, cid)
    for k in ("revision", "requests", "responses", "evaluations", "pairs", "proposals"):
        assert after[k] == before[k], k
    # 저장된 판정이 전체 재평가와 같음 (복원본에서 모든 시험을 다시 매칭)
    data = CaseStore(env).load(cid)
    s = fsvc.codec.restore_session(data["session"], catalog().trials, catalog().parsed)
    stored = {t: fsvc._match_signature(m) for t, m in s.matches.items()}
    s.match(list(s.matches))
    assert {t: fsvc._match_signature(m) for t, m in s.matches.items()} == stored

    # Cohort C 대기열 (모든 사례, 정책 엔진 순서)
    q = staff.get("/api/followup/staff/queue", headers=STAFF).json()
    assert q["cases"] == 1 and [i["rank"] for i in q["items"]] == list(range(1, len(q["items"]) + 1))
    assert all(i["case_id"] == cid for i in q["items"])


# ---------------------------------------------------------------------------
# 권한·잘못된 요청
# ---------------------------------------------------------------------------

def test_roles_and_bad_requests(env, monkeypatch):
    patient, other, staff = h.browser(), h.browser(client=("198.51.100.21", 50002)), staff_client()
    cid = _create(patient)["case_id"]
    case = _staff_case(staff, cid)
    rid = _request(staff, cid, _proposal(case, "lab:platelets")["proposal_id"])["created"][0]

    # 담당자 키 없음·틀림 -> 403, 서버 설정 없음 -> 503
    assert staff.get(f"/api/followup/staff/cases/{cid}").status_code == 403
    assert staff.get(f"/api/followup/staff/cases/{cid}", headers={**STAFF, "X-CTREC-Staff-Key": "x"}).status_code == 403
    assert staff.get(f"/api/followup/staff/cases/{cid}", headers={**STAFF, "X-CTREC-Staff-Id": "bad id!"}).status_code == 400
    # 환자 쿠키 없음·다른 환자 -> 404 (존재 여부를 드러내지 않음)
    assert other.get(f"/api/followup/cases/{cid}").status_code == 404
    assert other.get(f"/api/followup/cases/{cid}/requests").status_code == 404
    _answer(other, cid, rid, expect=404, answer="Platelets 180 x10^9/L")
    assert patient.get("/api/followup/cases/FC-" + "0" * 32).status_code == 404
    assert patient.get("/api/followup/cases/../../etc").status_code == 404
    # 연구 담당자의 대리 기입 금지 (환자 쿠키가 있어도 담당자 헤더가 붙으면 거절)
    r = patient.post(f"/api/followup/cases/{cid}/requests/{rid}/response", headers=STAFF, json={"answer": "x"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "staff_cannot_answer"
    # CSRF 헤더 없음 -> 403
    r = patient.post(f"/api/followup/cases/{cid}/requests/{rid}/response", headers={"X-CTREC-Request": "0"},
                     json={"answer": "x"})
    assert r.status_code == 403
    # 잘못된 요청 ID·빈 답·지금 제안이 아닌 제안·기록용 행동
    assert _answer(patient, cid, "FR-0000000000000000", expect=404, answer="x")["detail"]["code"] == "request_not_found"
    _answer(patient, cid, rid, expect=400, answer="   ")
    assert _request(staff, cid, "PA-0000000000000000:nope", expect=409)["detail"]["code"] == "proposal_not_current"
    # 개정 번호가 다르면 거절 (이전 화면 기준 요청)
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=STAFF,
                   json={"proposal_ids": [_proposal(case, "lab:hemoglobin")["proposal_id"]], "expected_revision": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_revision"
    # 담당자가 할 수 없는 상태 변경
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid}/status", headers=STAFF,
                   json={"status": "answered"})
    assert r.status_code == 422
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid}/status", headers=STAFF,
                   json={"status": "document_unavailable"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "transition_not_allowed"

    monkeypatch.delenv("CTREC_STAFF_KEY")
    assert staff.get(f"/api/followup/staff/cases/{cid}", headers=STAFF).status_code == 503


def test_unreachable_routes_to_documents_then_clinician(env):
    patient, staff = h.browser(), staff_client()
    cid = _create(patient)["case_id"]
    case = _staff_case(staff, cid)
    out = _request(staff, cid, _proposal(case, "lab:platelets")["proposal_id"],
                   _proposal(case, "lab:absolute neutrophil count")["proposal_id"])
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{out['created'][0]}/status", headers=STAFF,
                   json={"status": "unreachable"})
    assert r.status_code == 200, r.text
    case = r.json()["case"]
    asked = {x["request_id"]: x for x in case["requests"] if x["kind"] == "ask_patient"}
    assert {asked[i]["status"] for i in out["created"]} == {"unreachable"}  # 연락 불가는 같은 환자의 대기 질문 전체
    docs = [x for x in case["requests"] if x["follows"] in out["created"]]
    assert docs and all(x["kind"] == "document_review" and x["status"] == "requires_document" for x in docs)
    assert patient.get(f"/api/followup/cases/{cid}/requests").json()["pending"] == 0
    _answer(patient, cid, out["created"][0], expect=409, answer="Platelets 180 x10^9/L")
    # 문서도 없음 -> 다음 문서 정보원 또는 임상 검토 요청 (완료 상태는 만들지 않음)
    doc = docs[0]
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{doc['request_id']}/status", headers=STAFF,
                   json={"status": "document_unavailable"})
    assert r.status_code == 200, r.text
    case = r.json()["case"]
    nxt = [x for x in case["requests"] if x["follows"] == doc["request_id"]]
    assert len(nxt) == 1 and nxt[0]["status"] in ("requires_document", "requires_clinician")
    chain = nxt[0]
    for _ in range(5):  # 남은 문서 정보원을 모두 소진하면 임상 검토로
        if chain["kind"] == "clinician_review":
            break
        r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{chain['request_id']}/status", headers=STAFF,
                       json={"status": "document_unavailable"}).json()
        chain = next(x for x in r["case"]["requests"] if x["follows"] == chain["request_id"])
    assert chain["kind"] == "clinician_review" and chain["status"] == "requires_clinician"
    assert case["pairs"]["T-A"]["status"] != CONFIRMED_ELIGIBLE


def test_incremental_mismatch_falls_back_to_full(env, monkeypatch):
    """영향 분석이 시험을 놓쳐도 전체 재평가 대조가 잡아 전체 결과를 씀."""
    patient, staff = h.browser(), staff_client()
    cid = _create(patient)["case_id"]
    case = _staff_case(staff, cid)
    rid = _request(staff, cid, _proposal(case, "lab:hemoglobin")["proposal_id"])["created"][0]  # T-B, T-C 대상
    real = fsvc.resolve

    def blind(*a, **k):  # 의존 항목을 모두 놓치는 잘못된 영향 분석 (요청 대상 시험만 다시 평가하게 됨)
        return real(*a, **k).model_copy(update={"evaluator": "rule_engine", "wildcard": False, "fields": []})
    monkeypatch.setattr(fsvc, "resolve", blind)
    _answer(patient, cid, rid, answer="Hemoglobin 12 g/dL\nPlatelets 50 x10^9/L")  # T-A(혈소판)는 대상 밖
    ev = _staff_case(staff, cid)["evaluations"][-1]
    assert ev["equivalence"]["status"] == "mismatch_full_adopted" and "T-A" in ev["equivalence"]["trials"]
    assert any(c["criterion"] == "T-A:I3" for c in ev["criteria_changes"])  # 전체 결과가 반영됨


def test_store_rejects_stale_write_and_keeps_previous(env):
    patient = h.browser()
    cid = _create(patient)["case_id"]
    store = CaseStore(env)
    a, b = store.load(cid), store.load(cid)
    a["events"].append({"kind": "x"})
    store.save(a, expected_revision=a["revision"])
    with pytest.raises(StaleWrite):
        store.save(b, expected_revision=b["revision"])
    assert store.load(cid)["events"][-1] == {"kind": "x"}
    assert not list(env.glob("*.tmp"))


def test_paid_gate_blocks_real_backend(env, monkeypatch):
    monkeypatch.setattr(web, "TEST_LLM_BACKEND", None)
    monkeypatch.setattr(web, "_paid_problems", lambda: ["web_paid_disabled"])
    r = h.browser().post("/api/followup/cases", json={"patient_text": TEXT, "trial_ids": TRIALS})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "analysis_disabled"
    assert not list(env.glob("FC-*.json"))


def test_contract_session_mode_keeps_flow_and_boundary(env, monkeypatch):
    """CTREC_SESSION=contract(ContractSession): 답변 뒤 프로필을 다시 추출하지 않는 Session에서도 흐름·저장·대조가 유지됨."""
    monkeypatch.setenv("CTREC_SESSION", "contract")
    patient, staff = h.browser(), staff_client()
    cid = _create(patient)["case_id"]
    assert CaseStore(env).load(cid)["session"]["session_class"] == "ContractSession"
    case = _staff_case(staff, cid)
    rid = _request(staff, cid, _proposal(case, "lab:platelets")["proposal_id"])["created"][0]
    restart(env)
    monkeypatch.delenv("CTREC_SESSION")  # 나중에 설정이 바뀌어도 만든 때의 Session 종류로 복원
    assert _answer(patient, cid, rid, answer="Platelets 180 x10^9/L")["status"] == "answered"
    assert CaseStore(env).load(cid)["session"]["session_class"] == "ContractSession"
    case = _staff_case(staff, cid)
    assert case["evaluations"][-1]["equivalence"]["status"] == "match"
    _no_confirmed_eligible(case)


def test_answer_moves_pair_status_priority_and_recommendation(env):
    """나이 답변 -> T-C(<=75세) 불충족: 쌍 상태 전이, 추천에서 제외, 자기 보고라 확정 부적격은 아님, C 순서 갱신."""
    patient, staff = h.browser(), staff_client()
    cid = _create(patient, text="Synthetic NSCLC patient, male, stage IV non-small cell lung cancer. ECOG 1.")["case_id"]
    case = _staff_case(staff, cid)
    age = _proposal(case, "age")
    assert set(age["target_criteria"]) == {"T-A:I1", "T-B:I1", "T-C:I1", "T-C:I2"}
    rid = _request(staff, cid, age["proposal_id"])["created"][0]
    before = case["view"]["result"]["recommendation"]
    assert "T-C" in [r["trial_id"] for r in before["ranked"]]
    out = _answer(patient, cid, rid, answer="I am 80 years old")
    assert out["case"]["pairs"]["T-C"]["status"] == "VERIFICATION_PENDING"
    case = _staff_case(staff, cid)
    ev = case["evaluations"][-1]
    assert ev["transitions"] == [{"trial_id": "T-C", "before": "INFO_MISSING", "after": "VERIFICATION_PENDING"}]
    assert ev["equivalence"]["status"] == "match"
    assert case["pairs"]["T-C"]["eligibility"] == "INELIGIBLE" and case["pairs"]["T-C"]["mismatch"] == "provisional"
    rec = case["view"]["result"]["recommendation"]
    assert "T-C" not in [r["trial_id"] for r in rec["ranked"]] and "T-C" in [r["trial_id"] for r in rec["excluded"]]
    assert all(p["info_key"] != "age" for p in case["proposals"])  # 답을 받은 항목은 제안에서 빠짐
    # T-C가 닫히자 T-C만 겨냥하던 제안이 줄고 C 순서가 다시 매겨짐
    assert all(set(p["target_trials"]) != {"T-C"} or p["action_type"] != "ASK_PATIENT" for p in case["proposals"])
    _no_confirmed_eligible(case)
    assert all(p["status"] != "CONFIRMED_INELIGIBLE" for p in case["pairs"].values())


def test_inspector_shows_round_changes(env):
    patient, staff = h.browser(), staff_client()
    cid = _create(patient, text="Synthetic NSCLC patient, male, stage IV non-small cell lung cancer. ECOG 1.")["case_id"]
    rid = _request(staff, cid, _proposal(_staff_case(staff, cid), "age")["proposal_id"])["created"][0]
    _answer(patient, cid, rid, answer="I am 80 years old")
    v = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=STAFF).json()
    assert v["timeline"] and [t["round"] for t in v["timeline"]] == [0, 1]
    assert any(c["trial_id"] == "T-C" for c in v["changes"]["trial"])  # Inspector의 라운드별 판정 변화에 T-C
    assert v["followup"]["evaluations"][-1]["transitions"][0]["trial_id"] == "T-C"
