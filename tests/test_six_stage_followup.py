"""N 감사: 같은 6단계 흐름(C·D·J)을 O의 Follow-up API 서비스 경로에서 확인 (환자 -> 연구 담당자 -> 환자 -> 재평가).

기본 Session 경로(/api/runs·CLI)의 결함(test_six_stage_e2e.py의 xfail)과 섞지 않으려고 파일을 나눴습니다.
O의 ctrec.followup이 없는 트리(N 단독)에서는 모듈 단위로 건너뜁니다. 병합 뒤에는 자동으로 실행됩니다.
시험은 실제 NSCLC 프로토콜(NCT07680790, WP2a 파싱본), LLM은 시나리오 대역입니다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("ctrec.followup.service")

import six_stage_harness as H  # noqa: E402
import test_six_stage_e2e as T  # noqa: E402
from ctrec.followup.service import FollowupError, FollowupService, TrialCatalog  # noqa: E402
from ctrec.followup.store import CaseStore  # noqa: E402

E2 = f"{T.TID}:E2"


def service(root) -> FollowupService:
    return FollowupService(CaseStore(root), TrialCatalog(name="nsclc-n-audit", trials={T.TID: H.nsclc_trial(T.TID)},
                                                         parsed={T.TID: H.nsclc_parsed(T.TID)}))


@pytest.fixture()
def case(monkeypatch, tmp_path):
    """간질성 폐질환 정보가 없는 환자 사례 + E2 환자 질문 요청 1건 (연구 담당자가 만듦)."""
    H.install(monkeypatch, H.Scenario(T.profile_full, T.verdicts_ild_missing, T.ild_question))
    svc = service(tmp_path / "followup")
    data = svc.create_case(patient_text=T.TEXT_NO_ILD, trial_ids=[T.TID], patient_token_digest="0" * 64, synthetic=True)
    ask = next(p for p in data["current"]["proposals"] if p["route"] == "ask_patient" and E2 in p["target_criteria"])
    out = svc.create_requests(data["case_id"], [ask["proposal_id"]], staff_id="coord-n")
    return svc, data["case_id"], ask["proposal_id"], out["created"][0], tmp_path / "followup"


def test_followup_unknown_is_not_reasked_and_routes_to_alternative(case):
    """흐름 C·D (Follow-up 경로): '모름' 뒤 같은 환자 질문을 다시 만들지 않고 대체 요청(문서·임상 검토)을 만듦."""
    svc, cid, proposal, req, _ = case
    r = svc.respond(cid, req, answer=None, unknown=True)
    assert r["request"]["status"] == "unknown"
    assert [x["kind"] for x in r["routed"]] and all(x["kind"] in ("document_review", "clinician_review")
                                                   for x in r["routed"])
    assert all(E2 in x["target_criteria"] for x in r["routed"])
    with pytest.raises(FollowupError) as e:
        svc.create_requests(cid, [proposal], staff_id="coord-n")
    assert e.value.code in ("proposal_not_current", "already_unknown")
    current = svc.store.load(cid)["current"]
    assert not [p for p in current["proposals"] if p["route"] == "ask_patient" and E2 in p["target_criteria"]]
    assert current["pairs"][T.TID]["eligibility"] != "ELIGIBLE"


@pytest.mark.xfail(strict=True, reason="P 소관(P-1): 간질성 폐질환 병력의 대체 정보원으로 복용 약 목록(medication_list)을 고름 "
                                       "(engine/policy.item_alternatives / engine/bundles SourceCatalog 대응)")
def test_followup_alternative_source_fits_the_item(case):
    svc, cid, _, req, _ = case
    routed = svc.respond(cid, req, answer=None, unknown=True)["routed"]
    assert routed and routed[0].get("source_id") in ("medical_record", "clinician_review")


def test_followup_answer_reevaluates_affected_criterion(case):
    """흐름 B (Follow-up 경로): 환자 답 -> 사실 재추출 -> E2 재평가, 기록 필요 기준은 여전히 보류."""
    svc, cid, _, req, _ = case
    r = svc.respond(cid, req, answer="둘 다 해당 없음", unknown=False)
    assert r["request"]["status"] == "answered"
    s = svc._restore(svc.store.load(cid))
    assert H.assessment(s, T.TID, "E2").passes is True
    assert s.matches[T.TID].eligibility == "UNCERTAIN"


def test_followup_case_resumes_in_a_new_service(case):
    """흐름 J (Follow-up 경로): 서비스를 새로 만들어도 사례의 문답·판정·사실 이력이 저장소에서 복원됨."""
    svc, cid, _, req, root = case
    svc.respond(cid, req, answer=None, unknown=True)
    fresh = service(root)
    s = fresh._restore(fresh.store.load(cid))
    assert [(p.round, p.answer, p.target) for p in s.qa_log] == [(1, "모름", [E2])]
    assert H.assessment(s, T.TID, "E2").passes is None
    assert s.fact_store.facts(s.case.patient_id)
    assert fresh.patient_requests(cid)["requests"] == [] or all(
        r["request_id"] != req for r in fresh.patient_requests(cid)["requests"] if r.get("status") == "pending")
