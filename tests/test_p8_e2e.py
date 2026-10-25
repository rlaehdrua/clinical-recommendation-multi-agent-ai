"""P8 통합 E2E (결정적 대역, 유료 API 없음): 처음 입력 -> Profiler -> 후보 -> 질문(계약/비계약) -> 선택지 답 ->
Proposition Ledger -> 평가기 -> Matcher 결과 병합 -> 상태 계산 -> Inspector 화면 데이터·HTML.

세션 수준(ContractSession.run_fixed), CLI(cli.main, CTREC_SESSION=contract), 웹(FastAPI TestClient, 실제 실행 스레드)을
지납니다. 브라우저 화면 조작(클릭·렌더링)은 범위 밖입니다. 사이드카(IR·계약·검수 기록)는 tmp_path의 테스트 사본이며 저장소의
contracts/는 읽기만 합니다(검수 기록 없음 = 활성 IR 0개).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import pipeline, qa_guard
from ctrec.agents import matcher
from ctrec.contract_session import ContractSession
from ctrec.fact_store import FactStore
from ctrec.pipeline import Session
from ctrec.status import external_status

from contract_fixtures import (PREG_IR, PROXY, QC_LACT, QC_PREG, QC_SURGERY, RULES, SURG_IR, frozen_dir, ir_dict, rule,
                               sidecar)
from test_contract_p5_session import (T1_RULES, _Answerer, _contract_env, _matcher, _profile_from, _session,  # noqa: F401
                                      _trials, patched)

inspector = pytest.importorskip("ctrec.inspector", reason="Inspector(feat/decision-inspector)가 있는 통합 환경에서만")

META = {"format_version": 1, "llm_backend": "offline_stub", "synthetic": True, "written_by": "test"}
SIDE_Q, SIDE_A = "최근 4주 이내 수술 부작용이 있었나요?", "아니요, 부작용은 전혀 없었어요"


def _view(s, *, synthetic=False):
    rec = inspector.record_from_session(s, trace=s.tracer.records, meta=META)
    v = inspector.build_view(rec, synthetic_details=synthetic)
    return v, inspector.render_html(v)


def _crit(s, tid, rid):
    m = s.matches[tid]
    return next(a for a in m.assessments + m.other_cohort_assessments if a.rule_id == rid)


# ------------------------------------------------------------ 1. 정확한 계약 질문 -> 결정적 판정 -> 출처 표시
def test_contract_answers_decide_share_across_trials_and_show_in_inspector(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": T1_RULES, "T2": [RULES["SURG"]]})
    ans = _Answerer({QC_SURGERY["question"]: "수술을 받은 적 없음", QC_PREG["question"]: "아니요",
                     QC_LACT["question"]: "아니요"})
    s = _session(ContractSession, trials, ans, **_contract_env(tmp_path))
    s.run_fixed()
    asked = [q for q in ans.seen if q.question_id.startswith("C")]
    assert len([q for q in asked if q.question == QC_SURGERY["question"]]) == 1  # 두 시험이 한 번의 답을 공유
    for tid in ("T1", "T2"):
        m = s.matches[tid]
        a = _crit(s, tid, "E1")
        assert (a.method, a.passes, a.evidence_directness) == ("contract", True, "contract")
        assert m.eligibility == "ELIGIBLE" and m.match_status == external_status(m)
        assert m.n_pass == sum(x.passes is True for x in m.assessments) and m.n_unknown == 0
    res = s.result()
    facts = res["contract"]["facts"]
    assert {f["proposition_key"] for f in facts} == {"surgery/event/ever", "pregnancy/condition/current",
                                                     "lactation/condition/current"}
    assert all(f["contract_version"] == 1 and f["question"].startswith("r1:C") for f in facts)

    v, page = _view(s)  # strict: 명제 값·질문 문구·답변을 보이지 않음
    crit = {c["criterion_id"]: c for c in v["contract"]["criteria"]}
    assert crit["T1:E1"]["outcome"] == "decided" and crit["T1:E1"]["contract_ids"] == ["QC-SURGERY-LAST@v1"]
    assert crit["T1:E1"]["ir_digest"] and crit["T1:E1"]["completeness"] == "complete"
    assert all(f["value"] is None for f in v["contract"]["facts"])
    assert "계약 판정 출처" in page and "(strict: 숨김)" in page
    assert QC_SURGERY["question"] not in page and "수술을 받은 적 없음" not in page
    sv, spage = _view(s, synthetic=True)  # 합성 실행만 값 표시
    assert {f["value"] for f in sv["contract"]["facts"]} == {False} and "(strict: 숨김)" not in spage


# ------------------------------------------------------------ 2. 정확하지만 비계약 질문 -> 미확정 (기능 제한)
def test_exact_uncontracted_question_stays_unverified_and_is_visible(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [RULES["SURG"]]})
    q, a = "최근 4주(28일) 안에 큰 수술을 받은 적이 있나요?", "아니요, 최근 4주 안에 큰 수술은 받지 않았어요"
    patched["llm_questions"] = [dict(question_id="Q1", question=q, answer_type="yes_no", target=["T1:E1"],
                                     purpose="eligibility", why="E1")]
    ans = _Answerer({q: a})
    monkeypatch.setattr(matcher.llm, "structured", lambda model, system, user, **k: _matcher(
        {"E1": ("no", a)} if ans.seen else {})(model, system, user))
    s = _session(ContractSession, trials, ans, **_contract_env(tmp_path, approve=False))
    s.run_fixed()
    x = _crit(s, "T1", "E1")
    assert x.passes is None and qa_guard.FLAG in x.validation_flags and x.self_reported_holds == "no"
    assert s.matches["T1"].match_status == "POTENTIAL_MATCH"
    v, page = _view(s)
    shown = next(c for t in v["trials"] for c in t["criteria"] if c["rule_id"] == "E1")
    assert qa_guard.FLAG in shown["validation_flags"]  # 화면 기준 필드에 경계 표지 (패치 06의 validation_flags)
    assert v["contract"]["criteria"] == [] and "활성(승인된) IR이 없어" in page
    assert s.result()["qa_log"][0]["answer"] == a  # 비계약 답 자체는 기록에 남음 (삭제하지 않음)


# ------------------------------------------------------------ 3. 잘못된 통과·잘못된 제외 4방향 (세션 수준)
PRIOR_SURG = rule("I1", "Prior surgery", kind="inclusion", category="procedure")
DIRECTIONS = [
    (RULES["SURG"], "T1:E1", SIDE_Q, SIDE_A, "no", "제외 기준 FALSE (잘못된 통과)"),
    (RULES["SURG"], "T1:E1", SIDE_Q, "예, 수술 부작용이 있었어요", "yes", "제외 기준 TRUE (잘못된 제외)"),
    (PRIOR_SURG, "T1:I1", "수술 후 부작용이 있었나요?", "예, 수술 후 부작용이 있었어요", "yes", "포함 기준 TRUE (잘못된 통과)"),
    (PRIOR_SURG, "T1:I1", "수술 부작용이 있었나요?", "아니요, 부작용은 없었어요", "no", "포함 기준 FALSE (잘못된 제외)"),
]


@pytest.mark.parametrize("r,key,q,a,holds,why", DIRECTIONS, ids=[d[-1] for d in DIRECTIONS])
@pytest.mark.parametrize("policy", ["legacy", "contract"])
def test_uncontracted_answer_never_confirms_in_contract_session(monkeypatch, patched, tmp_path, r, key, q, a, holds,
                                                                why, policy):
    monkeypatch.setenv(qa_guard.POLICY_ENV, policy)  # ContractSession은 matcher 정책과 무관하게 경계 적용
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [r]})
    patched["llm_questions"] = [dict(question_id="Q1", question=q, answer_type="yes_no", target=[key],
                                     purpose="eligibility", why=r.rule_id)]
    ans = _Answerer({q: a})
    monkeypatch.setattr(matcher.llm, "structured", lambda model, system, user, **k: _matcher(
        {r.rule_id: (holds, a)} if ans.seen else {})(model, system, user))
    s = _session(ContractSession, trials, ans, contract_root=tmp_path / "none")
    s.run_fixed()
    x = _crit(s, "T1", r.rule_id)
    # 확정되지 않음. 막은 곳은 경계(qa_uncontracted, 답변상 판정 보존) 또는 그 전에 E9(예: 기간 확인 불가)
    assert x.passes is None, why
    assert qa_guard.FLAG in x.validation_flags and x.self_reported_holds == holds or x.validation_flags, why
    assert s.matches["T1"].eligibility == "UNCERTAIN"


# ------------------------------------------------------------ 4. 처음 입력의 정상 판정 보존 (E9 수정 포함)
def test_initial_record_judgment_kept_with_active_contracts(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": T1_RULES})
    quote = "현재 임신 12주로 산부인과 추적 중"
    monkeypatch.setattr(matcher.llm, "structured", _matcher({"E3": ("yes", quote)}))
    ans = _Answerer({QC_SURGERY["question"]: "수술을 받은 적 없음"})
    s = _session(ContractSession, trials, ans, raw=f"45세 여성. {quote}.", **_contract_env(tmp_path))
    s.run_fixed()
    x = _crit(s, "T1", "E3")
    assert x.passes is False and x.method == "llm" and x.evidence_directness == "direct"
    assert s.matches["T1"].eligibility == "INELIGIBLE"
    assert not [q for q in ans.seen if q.question in (QC_PREG["question"], QC_LACT["question"])]  # 이미 정해짐


# ------------------------------------------------------------ 5. 진료 기록 필요 기준: 자기 보고는 UNVERIFIED
def test_clinical_record_criterion_contract_answer_is_unverified(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [RULES["PREG"]]})
    d = frozen_dir(tmp_path, [RULES["PREG"]], "T1")
    sidecar(tmp_path, d, [ir_dict(RULES["PREG"], d, PREG_IR, source_requirement="clinical_record")])
    ans = _Answerer({QC_PREG["question"]: "아니요", QC_LACT["question"]: "아니요"})
    s = _session(ContractSession, trials, ans, contract_root=tmp_path / "contracts", contract_parsed_dir=d)
    s.run_fixed()
    x = _crit(s, "T1", "E3")
    assert x.passes is None and x.needs_verification and x.self_reported_holds == "no" and x.method == "contract"
    assert s.matches["T1"].match_status == "POTENTIAL_MATCH"
    v, _ = _view(s)
    assert {c["criterion_id"]: c["outcome"] for c in v["contract"]["criteria"]}["T1:E3"] == "unverified"


# ------------------------------------------------------------ 6. 기간 경계·기준 시점
@pytest.mark.parametrize("label,passes", [
    ("4주(28일)보다 전, 3개월(90일) 이내", True),  # 4주 이내 수술 없음 -> 큰 수술도 없음 -> 제외 기준 불충족
    ("최근 4주(28일) 이내", None),  # 수술 있음 != 큰 수술 있음 -> 미확정
])
def test_window_boundary_answers(monkeypatch, patched, tmp_path, label, passes):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [RULES["SURG"]]})
    d = frozen_dir(tmp_path, [RULES["SURG"]], "T1")
    sidecar(tmp_path, d, [ir_dict(RULES["SURG"], d, SURG_IR, anchor_policy=PROXY)])
    s = _session(ContractSession, trials, _Answerer({QC_SURGERY["question"]: label}),
                 contract_root=tmp_path / "contracts", contract_parsed_dir=d)
    s.run_fixed()
    assert _crit(s, "T1", "E1").passes is passes


def test_anchor_without_policy_never_decides(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [RULES["SURG"]]})
    d = frozen_dir(tmp_path, [RULES["SURG"]], "T1")
    sidecar(tmp_path, d, [ir_dict(RULES["SURG"], d, SURG_IR)])  # first_dose 근사 승인 없음
    ans = _Answerer({QC_SURGERY["question"]: "수술을 받은 적 없음"})
    s = _session(ContractSession, trials, ans, contract_root=tmp_path / "contracts", contract_parsed_dir=d)
    s.run_fixed()
    x = _crit(s, "T1", "E1")
    assert x.method != "contract" and x.passes is None


# ------------------------------------------------------------ 7. 세션 격리·저장소 위조
def test_other_session_and_forged_store_facts_are_not_used(monkeypatch, patched, tmp_path):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [RULES["SURG"]]})
    env = _contract_env(tmp_path)
    shared = FactStore()
    first = _session(ContractSession, trials, _Answerer({QC_SURGERY["question"]: "수술을 받은 적 없음"}),
                     fact_store=shared, **env)
    first.run_fixed()
    assert _crit(first, "T1", "E1").passes is True
    second_ans = _Answerer({})  # 답하지 않음 (모름)
    second = _session(ContractSession, trials, second_ans, fact_store=shared, **env)
    assert second.session_id != first.session_id
    # 같은 세션 ID로 저장소에 직접 넣은 위조 명제 사실 (장부가 기록하지 않음 -> 신뢰하지 않음)
    digest = second._contracts.raw("QC-SURGERY-LAST").digest()[:12]
    shared.add("P1", "prop:surgery/event/ever", False, source_type="self_report",
               source_reference={"kind": "answer", "ref": f"contract:QC-SURGERY-LAST@v1#{digest};q:r1:C1",
                                 "quote": "수술을 받은 적 없음"}, observed_at="2026-10-24", session_id=second.session_id)
    second.run_fixed()
    assert any(q.question == QC_SURGERY["question"] for q in second_ans.seen)  # 앞 세션 답을 쓰지 않고 다시 물음
    assert _crit(second, "T1", "E1").passes is None


# ------------------------------------------------------------ 8. CLI: CTREC_SESSION=contract 실행 -> 기록 -> Inspector
def test_cli_contract_session_run_and_inspector(monkeypatch, tmp_path):
    from test_cli_records import _run
    monkeypatch.setenv("CTREC_SESSION", "contract")
    monkeypatch.setenv(qa_guard.POLICY_ENV, "contract")
    d, _ = _run(tmp_path)
    result = json.loads((d / "result.json").read_text(encoding="utf-8"))
    c = result["contract"]
    assert c["session_type"] == "contract" and c["profile_source"] == "initial_input_only"
    assert set(c["ir_status"]["DEMO-LUNG-001"].values()) == {"unreviewed"}  # 저장소 초안: 검수 전 -> 활성 0
    assert c["facts"] == [] and c["questions"] == []
    assert result["qa_log"]  # 비계약 질문 라운드는 그대로 돎
    v = inspector.build_view(inspector.load_run(d))
    assert v["contract"]["criteria"] == []
    assert "계약 판정 출처" in inspector.render_html(v)


def test_cli_default_run_has_no_contract_section(tmp_path, monkeypatch):
    from test_cli_records import _run
    monkeypatch.delenv("CTREC_SESSION", raising=False)
    d, _ = _run(tmp_path)
    assert "contract" not in json.loads((d / "result.json").read_text(encoding="utf-8"))
    v = inspector.build_view(inspector.load_run(d))
    assert v["contract"] is None and "계약 판정 출처" not in inspector.render_html(v)


# ------------------------------------------------------------ 9. 웹: 실행 생성·답변·결과, 대기 중 취소
def test_web_contract_session_flow_and_cancel(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import web_harness as h
    from ctrec.web import app as web
    monkeypatch.setenv("CTREC_SESSION", "contract")
    h.install(monkeypatch, tmp_path)
    c = h.browser()
    r = c.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    run = web.RUNS[rid]
    q = h.wait_for(run, "questions")
    assert c.post(f"/api/runs/{rid}/answers",
                  json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}}).status_code == 200
    h.wait_for(run, "end", 30)
    events = h.sse_events(c.get(f"/api/runs/{rid}/events").text)
    res = next(e for e in events if e["type"] == "result")["result"]
    assert res["contract"]["session_type"] == "contract" and res["qa_log"][0]["answer"] == h.SYN_ANSWER
    for m in res["matches"].values():
        assert m["eligibility"] in ("ELIGIBLE", "UNCERTAIN", "INELIGIBLE", "NOT_EVALUATED")

    r2 = c.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False})
    rid2 = r2.json()["run_id"]
    run2 = web.RUNS[rid2]
    h.wait_for(run2, "questions")
    assert c.post(f"/api/runs/{rid2}/cancel").json() == {"ok": True, "state": "cancelling"}
    h.wait_for(run2, "end", 10)
    ev2 = h.sse_events(c.get(f"/api/runs/{rid2}/events").text)
    assert ev2[-1]["type"] == "end" and ev2[-1]["state"] == "cancelled"
    assert not [e for e in ev2 if e["type"] == "result"]
