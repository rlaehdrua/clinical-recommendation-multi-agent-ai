"""야간 V2: 전역 안전 불변식 (TRIALOG). 판정 결과를 화면에서 읽고 별도로 다시 따져 봄 (서비스 내부 판정 경로를 믿지 않고 기준 단위로 확인).

불변식 (번호는 야간 V2 보고서와 같음)
1. 필요한 근거 없이 CONFIRMED_ELIGIBLE 없음  2. 상충 정보는 확정으로 채택하지 않음  4. 진료 기록 필요 기준은 문서 확인 근거
5. 임상의 전용 기준은 자동 확정하지 않음  6. 권한 철회 뒤 남은 확정 없음  7. UNKNOWN·미해결이 확정으로 바뀌지 않음
8. 감사 기록은 추가 전용이고 연결 해시가 맞음  9. 사례별·시험별 상태가 화면마다 같음  10. Cohort C 대기열 = 사례 화면
(3. 전언·근사·추정 값은 알려진 미해결 P0-B: 이 기반에는 수정이 없어 여기서 검사하지 않음. 측정은 야간 보고서)

범위: follow-up 서비스(웹 HTTP, FastAPI TestClient), WP6 실행기(sim.runner), CLI·웹 Session(pipeline Session을 오프라인 대역으로).
합성 시험(sim.demo T-A..T-D)·합성 권한·LLM 대역만 씁니다. 실제 환자·실제 API 없음.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
sys.path.insert(0, str(Path(__file__).parent))

import test_authority_withdrawal_state_sync as W  # noqa: E402
import test_document_clinician_completion as D  # noqa: E402
import test_followup_flow as F  # noqa: E402
import test_q_state_contract as Q  # noqa: E402

from ctrec import clinician_authority, expert_boundary  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.fact_store import document_backed  # noqa: E402
from ctrec.followup import codec  # noqa: E402
from ctrec.web import followup_api  # noqa: E402

env = D.env
CE = "CONFIRMED_ELIGIBLE"
CLOSED = {"CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE"}


# ---------------------------------------------------------------------------
# 독립 검사 (follow-up 사례)
# ---------------------------------------------------------------------------

def independent_state(cid: str) -> tuple[dict, list[str]]:
    """지금 권한으로 사례를 복원해 쌍 상태와 기준 단위 불변식 위반 목록을 돌려줌 (판정은 다시 하지 않음)."""
    svc = followup_api.service()
    data = svc.store.load(cid)
    s = svc._restore(data)
    problems: list[str] = []
    with svc._scope(data, s):
        _, co = svc._cohort(s, data["alias"], codec.load_ops(data["ops"]))
        pid = s.case.patient_id
        facts = {f.fact_id: f for f in s.fact_store.facts(pid)}
        conflict_fields = {c["field"] for c in s.fact_store.conflicts(pid)}
        auth = clinician_authority.current()
        statuses = {}
        for p in co.pairs():
            tid = p.pair.trial_id
            statuses[tid] = p.status.value
            rules = {r.rule_id: r for r in s.parsed[tid].rules}
            for c in p.criteria:
                key = f"{tid}:{c.rule_id}"
                rule = rules.get(c.rule_id)
                used = set(c.inputs_used or ()) | {facts[i].field for i in c.evidence_fact_ids if i in facts}
                clinician_only = rule is not None and expert_boundary.clinician_reason(tid, rule) is not None
                # 5: 임상의 전용 기준이 판정되었으면 임상의가, 지금 권한이 인정하는 판단으로
                if clinician_only and c.state in ("MET", "NOT_MET"):
                    j = auth.judgment_for(tid, rule) if hasattr(auth, "judgment_for") else None
                    if c.decided_by != "clinician" or j is None:
                        problems.append(f"5 {key}: clinician-only criterion decided by {c.decided_by} (judgment={j})")
                # 4: 진료 기록 필요 기준을 근거 충족으로 닫았으면 근거는 지금 권한의 문서 확인 사실
                if c.required_evidence == "clinical_record" and c.state == "MET" and c.gap == "none" \
                        and not clinician_only:
                    if not c.evidence_fact_ids or not all(i in facts and document_backed(facts[i])
                                                          for i in c.evidence_fact_ids):
                        problems.append(f"4 {key}: record criterion closed without document-verified evidence")
                if p.status.value == CE:
                    # 1·7: 확정 적격이면 모든 기준이 근거와 함께 충족, 남은 확인 없음
                    if c.state != "MET" or c.gap != "none" or not c.evidence_satisfied:
                        problems.append(f"1 {key}: CE with criterion state={c.state} gap={c.gap} "
                                        f"evidence_satisfied={c.evidence_satisfied}")
                    # 2: 상충 중인 항목이 확정 근거로 쓰이지 않음
                    if used & conflict_fields:
                        problems.append(f"2 {key}: CE uses conflicting field(s) {sorted(used & conflict_fields)}")
    return statuses, problems


def assert_invariants(staff, patient, cid: str, *, clinician=None) -> dict:
    """1·2·4·5·7·9·10을 지금 상태에서 확인. 돌려준 값: 화면 상태 (다음 비교용)."""
    statuses, problems = independent_state(cid)
    assert problems == [], problems
    # 9: 독립 계산 = 담당자 사례 화면 = 환자 화면 = 사례 목록 = 추천 표시 (확정 적격 <-> 공개 기준 충족)
    st = Q.statuses(staff, patient, cid)
    assert st["pairs"] == statuses, (st["pairs"], statuses)
    Q.assert_one_state(st)
    case = D.staff_case(staff, cid)
    ce = {t for t, s in statuses.items() if s == CE}
    insp = staff.get(f"/api/followup/staff/cases/{cid}/inspector", headers=D.STAFF).json()
    assert W._eligible_marks(insp, set()) <= ce, (W._eligible_marks(insp, set()), ce)  # Inspector 현재 판정
    final = {x["trial_id"]: x.get("match_status") for x in insp["overview"]["final"]}
    assert {t for t, m in final.items() if m == "CRITERIA_MATCH"} == ce
    # 10: Cohort C 대기열의 이 사례 항목은 사례 화면의 지금 제안이고, 닫힌 쌍을 겨누지 않으며 요청 기록과 같음
    props = {p["proposal_id"]: p for p in case["proposals"]}
    reqs = {r["request_id"]: r for r in case["requests"]}
    items = [i for i in staff.get("/api/followup/staff/queue", headers=D.STAFF).json()["items"] if i["case_id"] == cid]
    for i in items:
        assert i["proposal_id"] in props, i
        p = props[i["proposal_id"]]
        assert (i["action_type"], i["info_key"], i["target_criteria"]) == (p["action_type"], p["info_key"],
                                                                           p["target_criteria"])
        assert not {k.split(":", 1)[0] for k in i["target_criteria"]} <= {t for t, s in statuses.items() if s in CLOSED}
        if i["request_id"]:
            assert reqs[i["request_id"]]["status"] == i["request_status"]
    # 열린 요청은 닫힌 쌍만 겨누지 않음 (확정 뒤 남은 확인 요청 없음)
    for r in case["requests"]:
        if r["status"] in ("pending", "requires_document", "requires_clinician") and r["target_trials"]:
            assert not set(r["target_trials"]) <= {t for t, s in statuses.items() if s in CLOSED}, r
    return st


class AuditWatch:
    """8: 감사 기록 추가 전용 - 매번 이전에 본 항목이 그대로 앞부분에 있고, 연결 해시가 맞고, 사례 이벤트가 빠짐없이 옮겨짐."""

    def __init__(self, staff, cid):
        self.staff, self.cid, self.seen = staff, cid, []

    def check(self) -> list[dict]:
        audit = self.staff.get(f"/api/followup/staff/cases/{self.cid}/audit", headers=D.STAFF).json()
        entries = audit["entries"]
        assert audit["intact"] is True, audit.get("broken_at")
        assert [e["seq"] for e in entries] == list(range(len(entries)))
        assert entries[:len(self.seen)] == self.seen, "audit entries were rewritten or removed"
        assert audit["head"] == (entries[-1]["hash"] if entries else "0" * 64)
        data = followup_api.service().store.load(self.cid)
        moved = [e for e in entries if e.get("source") == "case_event"]
        assert [e["event_index"] for e in moved] == list(range(len(data["events"])))
        assert [e["event"] for e in moved] == data["events"]
        self.seen = entries
        return entries


def clients():
    return D.h.browser(), D.staff_client(), D.staff_client(), D.staff_client()


# ---------------------------------------------------------------------------
# follow-up 서비스: 전체 흐름의 매 단계
# ---------------------------------------------------------------------------

def test_invariants_hold_at_every_step_of_document_and_clinician_completion(env):
    patient, staff, verifier, clinician = clients()
    cid, by_key = D.answered_lab_case(patient, staff)
    audit = AuditWatch(staff, cid)
    assert_invariants(staff, patient, cid)
    audit.check()
    doc = D.register(staff, cid)["document"]
    assert_invariants(staff, patient, cid)
    audit.check()
    plt = [f for f in D.facts_for(verifier, cid, by_key["lab:platelets"]) if f["field"] == "lab:platelets"]
    D.verify(verifier, cid, by_key["lab:platelets"], doc, [{"fact_id": plt[0]["fact_id"]}])
    assert_invariants(staff, patient, cid)
    audit.check()
    D.verify(verifier, cid, by_key["lab:absolute neutrophil count"], doc,
             [{"field": "lab:absolute neutrophil count", "value": 2.1, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    assert_invariants(staff, patient, cid)
    ecog = [f for f in D.facts_for(verifier, cid, by_key["ecog"]) if f["field"] == "ecog"]
    D.verify(verifier, cid, by_key["ecog"], doc, [{"fact_id": ecog[0]["fact_id"]}])
    st = assert_invariants(staff, patient, cid)
    assert st["pairs"] == {"T-A": CE, "T-D": "EXPERT_REVIEW"}  # 5: 문서 확인만으로 임상의 기준이 닫히지 않음
    audit.check()
    cv = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=D.CLINICIAN).json()
    rq = next(r for r in cv["requests"] if r["judgeable"])
    ef = next(f for f in cv["facts"] if f["field"] == "ecog" and f["document_verified"])["fact_id"]
    D.judge(clinician, cid, rq["request_id"], [{"criterion": "T-D:I2", "decision": "MET",
                                                 "rationale_code": "life_expectancy_adequate", "basis_fact_ids": [ef]}])
    st = assert_invariants(staff, patient, cid)
    assert st["pairs"] == {"T-A": CE, "T-D": CE}
    audit.check()
    D.restart(env)
    assert assert_invariants(staff, patient, cid)["pairs"] == {"T-A": CE, "T-D": CE}
    entries = audit.check()
    assert [e["event"]["kind"] for e in entries].count("document_verified") == 3


def test_failed_verifications_change_nothing_and_are_audited(env):
    """잘못된 해시·범위 밖 항목·없는 사실: 판정·개정 번호 그대로, 거부 시도는 감사 기록에 추가만."""
    patient, staff, verifier, _ = clients()
    cid, by_key = D.answered_lab_case(patient, staff)
    doc = D.register(staff, cid)["document"]
    audit = AuditWatch(staff, cid)
    audit.check()
    before = D.staff_case(staff, cid)
    rid = by_key["lab:platelets"]
    plt = [f for f in D.facts_for(verifier, cid, rid) if f["field"] == "lab:platelets"][0]
    bad = [({"document_id": doc["document_id"], "document_sha256": "3" * 64, "items": [{"fact_id": plt["fact_id"]}]},
            "document_changed"),
           ({"document_id": doc["document_id"], "document_sha256": doc["sha256"],
             "items": [{"field": "lab:hemoglobin", "value": 13.0, "unit": "g/dL"}]}, "field_out_of_scope"),
           ({"document_id": doc["document_id"], "document_sha256": doc["sha256"],
             "items": [{"fact_id": "F-000000000000"}]}, "fact_not_found"),
           ({"document_id": "DOC-00000000000000ff", "document_sha256": doc["sha256"],
             "items": [{"fact_id": plt["fact_id"]}]}, "document_not_found")]
    for body, code in bad:
        r = verifier.post(f"/api/followup/verifier/cases/{cid}/requests/{rid}/verification", headers=D.VERIFIER,
                          json=body)
        assert r.status_code in (404, 409) and D.code(r) == code, r.text
        after = D.staff_case(staff, cid)
        assert (after["revision"], after["pairs"], after["requests"]) == (before["revision"], before["pairs"],
                                                                          before["requests"])
        assert_invariants(staff, patient, cid)
        entries = audit.check()
        assert entries[-1]["source"] == "denied" and entries[-1]["event"]["code"] == code


# ---------------------------------------------------------------------------
# 2. 상충 정보
# ---------------------------------------------------------------------------

def test_conflicting_document_value_is_never_adopted(env):
    patient, staff, verifier, _ = clients()
    cid, by_key = D.answered_lab_case(patient, staff)  # 환자 처음 입력: 혈소판 180 (자기 보고)
    doc = D.register(staff, cid, content=b"SYNTHETIC LAB REPORT\nPlatelets 90 x10^9/L\nANC 2.1 x10^9/L\nECOG 1\n")["document"]
    D.verify(verifier, cid, by_key["lab:platelets"], doc,
             [{"field": "lab:platelets", "value": 90.0, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    D.verify(verifier, cid, by_key["lab:absolute neutrophil count"], doc,
             [{"field": "lab:absolute neutrophil count", "value": 2.1, "unit": "x10^9/L", "observed_at": "2026-10-01"}])
    ecog = [f for f in D.facts_for(verifier, cid, by_key["ecog"]) if f["field"] == "ecog"]
    D.verify(verifier, cid, by_key["ecog"], doc, [{"fact_id": ecog[0]["fact_id"]}])
    st = assert_invariants(staff, patient, cid)
    assert st["pairs"]["T-A"] not in CLOSED  # 180(자기 보고) vs 90(문서): 어느 값으로도 확정하지 않음
    case = D.staff_case(staff, cid)
    assert "conflict" in case["pairs"]["T-A"]["gaps"]
    assert [r["conflict"]["field"] for r in case["requests"] if r["kind"] == "conflict_resolution"] == ["lab:platelets"]


def test_conflicting_patient_answer_is_never_adopted(env):
    patient, staff, _, _ = clients()
    cid = D.create(patient, ["T-A", "T-B", "T-C"], text=F.TEXT_NO_AGE)  # ECOG 1
    rid = D.make_requests(staff, cid, D.proposal(D.staff_case(staff, cid), "age", "ASK_PATIENT")["proposal_id"])["created"][0]
    out = patient.post(f"/api/followup/cases/{cid}/requests/{rid}/response",
                       json={"answer": "I am 64 years old.\nMy ECOG performance status is 3 now."}).json()
    assert out["conflict"] is True
    st = assert_invariants(staff, patient, cid)
    case = D.staff_case(staff, cid)
    assert all(st["pairs"][t] not in CLOSED for t in ("T-A", "T-C"))
    assert all("conflict" in case["pairs"][t]["gaps"] for t in ("T-A", "T-C"))


# ---------------------------------------------------------------------------
# 5. 임상의 전용 기준
# ---------------------------------------------------------------------------

def test_clinician_only_criterion_needs_a_recognized_judgment(env):
    patient, staff, verifier, clinician = clients()
    cid = D.create(patient, ["T-A", "T-D"])
    case = D.staff_case(staff, cid)
    assert case["pairs"]["T-D"]["status"] == "EXPERT_REVIEW"
    assert_invariants(staff, patient, cid)
    # 문서로 ECOG를 확인해도 임상의 기준은 닫히지 않음 (문서 확인은 임상의 판단이 아님)
    doc_req = D.make_requests(staff, cid, D.proposal(case, "ecog", "REQUEST_DOCUMENT")["proposal_id"])["created"][0]
    view = clinician.get(f"/api/followup/clinician/cases/{cid}", headers=D.CLINICIAN).json()
    ecog = next(f for f in view["facts"] if f["field"] == "ecog")
    doc = D.register(staff, cid)["document"]
    D.verify(verifier, cid, doc_req, doc, [{"fact_id": ecog["fact_id"]}])
    assert assert_invariants(staff, patient, cid)["pairs"]["T-D"] == "EXPERT_REVIEW"
    rid = next(r for r in D.staff_case(staff, cid)["requests"]
               if r["kind"] == "clinician_review" and r["target_criteria"] == ["T-D:I2"])["request_id"]
    # 판단 기준이 아닌 기준(T-A 검사)을 섞은 판단은 거절
    assert D.judge(clinician, cid, rid, [{"criterion": "T-A:I3", "decision": "MET", "rationale_code": "x"}],
                   expect=409)["detail"]["code"] == "judgment_incomplete"
    met = {"criterion": "T-D:I2", "decision": "MET", "rationale_code": "life_expectancy_adequate"}
    for hdr in (D.STAFF, D.VERIFIER, {}):  # 다른 역할·권한 없음
        assert D.judge(staff, cid, rid, [met], headers=hdr, expect=403)["detail"]["code"] == "clinician_forbidden"
    assert D.judge(clinician, cid, rid, [met], expect=409)["detail"]["code"] == "basis_not_document_verified"
    assert assert_invariants(staff, patient, cid)["pairs"]["T-D"] == "EXPERT_REVIEW"


# ---------------------------------------------------------------------------
# 7. 모름·연락 불가·문서 없음은 판정을 확정 쪽으로 옮기지 않음
# ---------------------------------------------------------------------------

def _no_confirming_change(case, triggers):
    for ev in case["evaluations"]:
        t = ev["trigger"]
        if (t["type"], t.get("outcome") or t.get("status")) in triggers:
            assert not [x for x in ev["transitions"] if x["after"] in CLOSED], ev["transitions"]
            assert not [c for c in ev["criteria_changes"]
                        if (c["before"] or {}).get("passes") is None and (c["after"] or {}).get("passes") is not None], \
                ev["criteria_changes"]


def test_unknown_unreachable_and_unavailable_never_confirm(env):
    patient, staff, _, _ = clients()
    cid = D.create(patient, ["T-A", "T-B", "T-C"], text=F.TEXT_NO_AGE)
    case = D.staff_case(staff, cid)
    out = D.make_requests(staff, cid, D.proposal(case, "age", "ASK_PATIENT")["proposal_id"],
                          D.proposal(case, "lab:platelets", "REQUEST_DOCUMENT")["proposal_id"])
    ask, doc = out["created"]
    before = assert_invariants(staff, patient, cid)
    r = patient.post(f"/api/followup/cases/{cid}/requests/{ask}/response", json={"unknown": True})
    assert r.status_code == 200 and r.json()["status"] == "unknown"
    assert assert_invariants(staff, patient, cid)["pairs"] == before["pairs"]
    chain = doc
    for _ in range(6):
        r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{chain}/status", headers=D.STAFF,
                       json={"status": "document_unavailable"})
        assert r.status_code == 200, r.text
        pairs = assert_invariants(staff, patient, cid)["pairs"]
        # 문서 경로 소진은 보류(BLOCKED_FAILURE)로만 바뀜 - 확정·부적격 없음 (exhausted-doc DEFER 수정 뒤)
        assert all(pairs[t] in (before["pairs"][t], "BLOCKED_FAILURE") for t in pairs), (pairs, before["pairs"])
        nxt = [x for x in r.json()["case"]["requests"] if x["follows"] == chain]
        if not nxt or nxt[0]["kind"] != "document_review":
            break
        chain = nxt[0]["request_id"]
    case = D.staff_case(staff, cid)
    _no_confirming_change(case, {("response", "unknown"), ("staff_status", "document_unavailable"),
                                 ("staff_status", "unreachable")})
    assert not {r["status"] for r in case["requests"]} & {"document_verified", "clinician_decided"}


def test_unreachable_never_confirms(env):
    patient, staff, _, _ = clients()
    cid = D.create(patient, ["T-A", "T-B", "T-C"], text=F.TEXT_NO_AGE)
    rid = D.make_requests(staff, cid, D.proposal(D.staff_case(staff, cid), "age", "ASK_PATIENT")["proposal_id"])["created"][0]
    before = assert_invariants(staff, patient, cid)
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests/{rid}/status", headers=D.STAFF, json={"status": "unreachable"})
    assert r.status_code == 200
    assert assert_invariants(staff, patient, cid)["pairs"] == before["pairs"]
    _no_confirming_change(D.staff_case(staff, cid), {("staff_status", "unreachable")})


# ---------------------------------------------------------------------------
# 6. 권한 철회
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", W.WITHDRAW)
def test_withdrawal_leaves_no_confirmed_state_in_staff_patient_inspector_queue(env, how):
    """담당자·환자·목록·Inspector·대기열 (임상의 화면은 V2 보고서 참조: 여기서는 검사하지 않음)."""
    patient, staff, verifier, clinician = clients()
    cid, _ = Q.completed_case(patient, staff, verifier, clinician)
    audit = AuditWatch(staff, cid)
    audit.check()
    W.withdraw(env, how)
    st = assert_invariants(staff, patient, cid)
    assert CE not in st["pairs"].values()
    audit.check()  # 철회·재시작이 감사 기록을 다시 쓰지 않음
    for pid in [i["proposal_id"] for i in staff.get("/api/followup/staff/queue", headers=D.STAFF).json()["items"]
                if i["case_id"] == cid][:2]:
        assert staff.post(f"/api/followup/staff/cases/{cid}/requests", headers=D.STAFF,
                          json={"proposal_ids": [pid]}).status_code == 200
        assert CE not in assert_invariants(staff, patient, cid)["pairs"].values()
        audit.check()


# ---------------------------------------------------------------------------
# 8. 감사 기록 변조 감지
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tamper", ["edit_middle", "delete_middle", "reorder"])
def test_audit_tampering_is_detected(env, tamper):
    patient, staff, _, _ = clients()
    cid, _ = D.answered_lab_case(patient, staff)
    D.register(staff, cid)
    path = env / "audit" / f"{cid}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 4
    if tamper == "edit_middle":
        e = json.loads(lines[1])
        e["event"]["by"] = "staff:someone-else"
        lines[1] = json.dumps(e, ensure_ascii=False, sort_keys=True)
    elif tamper == "delete_middle":
        del lines[1]
    else:
        lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    audit = staff.get(f"/api/followup/staff/cases/{cid}/audit", headers=D.STAFF).json()
    assert audit["intact"] is False and audit["broken_at"] in (1, 2)


# ---------------------------------------------------------------------------
# WP6 실행기
# ---------------------------------------------------------------------------

WIDE = C.EffortBudget(coordinator_minutes=400, clinician_minutes=60, horizon_steps=10)


def _wp6_problems(result) -> list[str]:
    out = []
    for p in result.final.pairs():
        alias, tid = p.pair.patient_alias, p.pair.trial_id
        facts = {f.fact_id: f for f in result.facts[alias]}
        for c in p.criteria:
            key = f"{alias}|{tid}:{c.rule_id}"
            ev = [facts[i] for i in c.evidence_fact_ids if i in facts]
            if c.required_evidence == "clinical_record" and c.state == "MET" and c.gap == "none":
                if not ev or not all(f.verification_status == "document_verified" for f in ev):
                    out.append(f"4 {key}")
            if p.status.value != CE:
                continue
            if c.state != "MET" or c.gap != "none" or not c.evidence_satisfied or c.decided_by == "llm":
                out.append(f"1/7 {key}: {c.state} {c.gap} {c.decided_by}")
            for field_name in set(c.inputs_used or ()) | {f.field for f in ev}:
                values = {json.dumps(f.value, sort_keys=True) for f in facts.values() if f.field == field_name}
                if len(values) > 1:
                    out.append(f"2 {key}: {field_name} has {sorted(values)}")
    return out


@pytest.mark.parametrize("pid", ["full_dcvc", "legacy_greedy", "cheapest_first", "patient_order", "random"])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_wp6_runner_final_states_hold_the_invariants(pid, seed):
    from ctrec.sim import demo, runner
    cohort = demo.cohort()
    r = runner.run(runner.build_spec("overnight", pid, cohort, WIDE, seed), cohort)
    assert _wp6_problems(r) == []
    # 7: 실패·무응답 단계는 확정 쌍을 만들지 않음 (기록 단위)
    for rec in r.records:
        if rec.outcome.status in ("failed", "no_response"):
            assert not rec.facts_added


# ---------------------------------------------------------------------------
# CLI·웹 Session (pipeline Session, 오프라인 대역): 상충·자기 보고만으로 확정 없음
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    F.TEXT + "\nPlatelets 180 x10^9/L\nANC 2.1 x10^9/L\nHemoglobin 12 g/dL",  # 검사값 자기 보고만
    F.TEXT + "\nMy ECOG performance status is 3 now.\nHemoglobin 12 g/dL",  # ECOG 상충
])
def test_session_without_documents_never_confirms(env, text):
    """웹·CLI가 쓰는 Session(follow-up과 같은 session_class)을 문서 없이: 진료 기록 필요 기준이 있는 시험은 확정 적격이 아님."""
    patient, staff, _, _ = clients()
    cid = D.create(patient, ["T-A", "T-B", "T-C", "T-D"], text=text)
    st = assert_invariants(staff, patient, cid)
    assert CE not in st["pairs"].values()
