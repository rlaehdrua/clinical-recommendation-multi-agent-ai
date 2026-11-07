"""N 감사: 최초 6단계 파이프라인 E2E 회귀 (흐름 A-J). 실제 Orchestrator·Session·trial_store·Matcher 검증·추천·Inspector.

LLM만 시나리오 대역(tests/six_stage_harness.py)입니다. 시험은 ClinicalTrials.gov NSCLC 프로토콜(data/nsclc, WP2a 파싱본,
임상 검수 전)과 데모 시험을 씁니다. xfail(strict)은 다른 작업(M·O·P) 소관으로 재현만 남긴 결함입니다: 고쳐지면 XPASS로 실패하므로
그때 표시를 지웁니다. 결함·제안은 docs/dev/n_six_stage_audit.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import six_stage_harness as H
from ctrec import config, document_authority, inspector, trial_store
from ctrec.contract_session import ContractSession
from ctrec.pipeline import PatientCase, Session
from ctrec.trace import Tracer

TID = "NCT07680790"
NSCLC_WITH_PARSED = ["NCT06216301", "NCT06928389", "NCT07027514", "NCT07098338", "NCT07416058", "NCT07680790"]
NSCLC_WITHOUT_PARSED = ["NCT04892953", "NCT05255302"]

TEXT_FULL = ("합성 환자. 62세 남성. 조직검사로 확진된 비편평 비소세포폐암 4기(IVA), 2026-10-01 진단. "
             "EGFR exon 19 deletion 양성(2026-10-05 조직 NGS). ECOG 1. RECIST 1.1 측정 가능 병변 있음. "
             "뇌 MRI에서 뇌전이 소견 없음. 간질성 폐질환 병력 없음. 심장 질환 병력 없음.")
QUOTES = {"I1": "조직검사로 확진된 비편평 비소세포폐암", "I2": "비소세포폐암 4기(IVA)", "I3": "EGFR exon 19 deletion 양성",
          "I4": "RECIST 1.1 측정 가능 병변 있음", "E1": "뇌 MRI에서 뇌전이 소견 없음", "E2": "간질성 폐질환 병력 없음",
          "E3": "심장 질환 병력 없음"}
ILD_OPTIONS = ["스테로이드 치료가 필요했던 간질성 폐질환/폐렴(2등급 이상) 병력이 있음", "현재 간질성 폐질환/폐렴이 있거나 의심됨",
               "둘 다 해당 없음"]
TEXT_NO_ILD = TEXT_FULL.replace(" 간질성 폐질환 병력 없음.", "")


def profile_full(user: str):
    return H.base_profile(age=62, sex="male", primary_diagnosis="non-squamous NSCLC", disease_stage="IVA", ecog=1,
                          biomarkers=[H.fact("EGFR exon 19 deletion positive", QUOTES["I3"], "2026-10-05")])


def adult(user: str):
    return H.base_profile(age=62, sex="male")


def verdicts_full(tid, rid, src):
    if rid in QUOTES:
        return ("yes" if rid.startswith("I") else "no", QUOTES[rid], "explicit")
    return None


def verdicts_ild_missing(tid, rid, src):
    """E2(간질성 폐질환)만 처음 입력에 없음. 답변 블록의 선택지 문구가 있으면 그것을 인용."""
    if rid == "E2":
        return ("no", "A: 둘 다 해당 없음", "explicit") if "A: 둘 다 해당 없음" in src else None
    return verdicts_full(tid, rid, src)


def ild_question(rnd, prev):
    return [H.q("Q1", "간질성 폐질환(ILD)/폐렴에 대해 해당하는 것을 고르세요.", [f"{TID}:E2"],
                answer_type="single_choice", options=ILD_OPTIONS)]


def none(*a):
    return None


def run(monkeypatch, scenario, text, answers=None, *, cls=Session, trials=None, **kw):
    H.install(monkeypatch, scenario)
    return H.run_session(text, trials or {TID: H.nsclc_trial(TID)}, H.ScriptedAnswerer(answers or {}),
                         session_cls=cls, **kw)


# ----------------------------------------------------------------------------- 1단계: 시험 기준 입력
def test_stage1_nsclc_trial_set_reaches_session_parser_store(monkeypatch):
    """실제 NSCLC 프로토콜 원문 + 파싱본이 Orchestrator -> Session.parse_criteria(trial_store.get)로 연결됨.
    파싱본이 없는 시험은 부적격이 아니라 NOT_EVALUATED(COMPILATION_PENDING)로 남음."""
    trials = {t: H.nsclc_trial(t) for t in NSCLC_WITH_PARSED + NSCLC_WITHOUT_PARSED}
    s = run(monkeypatch, H.Scenario(profile_full, none), TEXT_FULL, trials=trials)
    for t in NSCLC_WITH_PARSED:
        assert s.matches[t].eligibility != "NOT_EVALUATED", t
        assert s.parsed[t].rules and any(r.kind == "exclusion" for r in s.parsed[t].rules)
    for t in NSCLC_WITHOUT_PARSED:
        assert s.matches[t].eligibility == "NOT_EVALUATED"
        assert "COMPILATION_PENDING" in s.matches[t].evaluation_error
    assert not any(r.trial_id in NSCLC_WITHOUT_PARSED for r in s.recommendation.recommended)


def test_stage1_default_trial_set_has_no_parsed_nsclc_protocols():
    """기본 세트(대회 시험)에서는 같은 NSCLC 시험의 파싱본이 없음 -> NOT_EVALUATED 경로 (연결 전 상태의 기준)."""
    with pytest.raises(trial_store.CompilationPending):
        trial_store.load(H.nsclc_trial(TID), store_dir=config.TRIAL_SETS["default"][1])


def _config_in_subprocess(value: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env.update(CTREC_TRIAL_SET=value, PYTHONPATH=str(H.ROOT / "src"), PYTHONIOENCODING="utf-8")
    code = "from ctrec import config; print(config.TRIAL_SET, config.TRIALS_DIR.parent.name, config.PARSED_DIR.parent.name)"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", env=env, cwd=H.ROOT)


@pytest.mark.parametrize("value,expect,parent", [("nsclc", "nsclc", "nsclc"), ("", "default", "data")])
def test_stage1_trial_set_env_switch(value, expect, parent):
    r = _config_in_subprocess(value)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [expect, parent, parent]


def test_stage1_trial_set_env_rejects_unknown():
    r = _config_in_subprocess("oncology")
    assert r.returncode != 0 and "CTREC_TRIAL_SET" in r.stderr


# ----------------------------------------------------------------------------- A. 완전한 정보 -> 매칭 -> 추천
@pytest.mark.parametrize("cls", [Session, ContractSession])
def test_A_complete_record_without_document_contract_is_not_confirmed(monkeypatch, cls):
    """운영 기본(NoDocumentAuthority): 진료 기록이 필요한 기준은 선언만으로 확정되지 않음 -> 확정 적격 없음.
    구조화 기준(나이·성별)은 규칙 엔진으로 확정. 추천 목록에 올라도 match_status는 확인 필요."""
    s = run(monkeypatch, H.Scenario(profile_full, verdicts_full), TEXT_FULL, cls=cls, input_source="clinical_record")
    m = s.matches[TID]
    assert m.eligibility == "UNCERTAIN" and m.match_status != "CRITERIA_MATCH"
    by = {a.rule_id: a for a in m.assessments}
    assert by["I6"].passes is True and by["I6"].method == "rule_engine"
    assert by["I3"].passes is None and "declared_record_unverified" in by["I3"].validation_flags
    assert all(r.match_status != "CRITERIA_MATCH" for r in s.recommendation.ranked)


def test_A_document_attested_record_values_confirm(monkeypatch):
    """문서 증명이 있으면(합성 권한, 미래 문서 계약 가정) 같은 사실이 기록 필요 기준을 확정: EGFR(I3), ECOG(I5)."""
    from ctrec.sim.evaluator import SIM_AUTHORITY
    orig = Session.profile_patient

    def profile_and_attest(self):
        p = orig(self)
        H.attest_all(self)
        return p
    monkeypatch.setattr(Session, "profile_patient", profile_and_attest)
    with document_authority.use(SIM_AUTHORITY):
        s = run(monkeypatch, H.Scenario(profile_full, verdicts_full), TEXT_FULL, input_source="clinical_record")
    by = {a.rule_id: a for a in s.matches[TID].assessments}
    assert by["I3"].passes is True and by["I5"].passes is True
    assert by["I1"].passes is None  # 조직학 진단은 사실로 연결되지 않음 (프로필에 인용 사실 없음) -> 보류 유지


def test_A_structured_only_trial_reaches_confirmed_recommendation(monkeypatch):
    """확정 경로 끝까지: 기준이 나이·성별뿐이면 ELIGIBLE/CRITERIA_MATCH -> 추천 -> 보고서."""
    H.use_subset(monkeypatch, H.nsclc_parsed(TID), {"I6", "I7"})
    s = run(monkeypatch, H.Scenario(profile_full, none), TEXT_FULL)
    assert s.matches[TID].eligibility == "ELIGIBLE" and s.matches[TID].match_status == "CRITERIA_MATCH"
    assert [r.trial_id for r in s.recommendation.recommended] == [TID]
    assert s.report and "합성 보고서" in s.report


# ----------------------------------------------------------------------------- B. 부족 -> 요청 -> 응답 -> 재평가
def test_B_missing_info_question_answer_reevaluation(monkeypatch):
    s = run(monkeypatch, H.Scenario(profile_full, verdicts_ild_missing, ild_question), TEXT_NO_ILD,
            {1: {"Q1": "둘 다 해당 없음"}})
    assert (s.qa_log[0].round, s.qa_log[0].target, s.qa_log[0].answer) == (1, [f"{TID}:E2"], "둘 다 해당 없음")
    assert H.assessment(s, TID, "E2").passes is True
    assert s.missing_facts[f"{TID}:E2"].status == "resolved"
    assert [h["step"] for h in s.match_history][:2] == ["match(round=0)", "match(round=1)"]


# ----------------------------------------------------------------------------- C/D. 모름·무응답
@pytest.mark.parametrize("answers", [{1: {"Q1": "모름"}, 2: {"Q1": "모름"}}, {}], ids=["unknown", "no_response"])
def test_CD_unknown_or_no_response_preserves_state(monkeypatch, answers):
    """'모름'·무응답(웹 시간 초과와 같은 '모름')은 사실을 만들지 않고 판정을 바꾸지 않음. 항목은 unresolved로 남음."""
    s = run(monkeypatch, H.Scenario(profile_full, verdicts_ild_missing, ild_question), TEXT_NO_ILD, answers)
    assert H.assessment(s, TID, "E2").passes is None
    assert s.missing_facts[f"{TID}:E2"].status == "unresolved"
    assert s.matches[TID].eligibility == "UNCERTAIN"


# O-1: question_generator.screen이 확보 계획(acquisition_plan)에 남은 열린 항목만 대상으로 둠 (기본 Session·ContractSession)
@pytest.mark.parametrize("cls,answers", [(Session, {1: {"Q1": "모름"}}), (ContractSession, {1: {"Q1": "둘 다 해당 없음"}})],
                         ids=["session_unknown", "contract_answered"])
def test_CD_failed_question_is_not_repeated(monkeypatch, cls, answers):
    s = run(monkeypatch, H.Scenario(profile_full, verdicts_ild_missing, ild_question), TEXT_NO_ILD, answers, cls=cls)
    asked = [t for p in s.qa_log for t in p.target]
    assert asked.count(f"{TID}:E2") == 1


@pytest.mark.xfail(strict=True, reason="기본 Session 경로, O/P 소관: '모름' 뒤 대체 문서·기록 경로가 없음 - comorbidity "
                                       "확보 계획의 대안이 비어 있고 legacy 경로는 문서 요청·임상의 행동을 만들지 않음. "
                                       "Follow-up API 경로는 대체 요청을 만듦 (정보원 대응은 P-1)")
def test_C_unknown_offers_alternative_source(monkeypatch):
    s = run(monkeypatch, H.Scenario(profile_full, verdicts_ild_missing, ild_question), TEXT_NO_ILD,
            {1: {"Q1": "모름"}, 2: {"Q1": "모름"}})
    f = s.missing_facts[f"{TID}:E2"]
    assert f.alternative_acquisitions or f.status == "document_requested"


# ----------------------------------------------------------------------------- E. 상충
CONFLICT_NO = "스테로이드 치료가 필요했던 간질성 폐렴 병력은 없습니다"
CONFLICT_YES = "2025년에 스테로이드 치료가 필요했던 간질성 폐렴 병력이 있습니다"


def _e2_only(monkeypatch, text):
    H.use_subset(monkeypatch, H.nsclc_parsed(TID), {"I6", "I7", "E2"})
    return run(monkeypatch, H.Scenario(adult, lambda t, r, s: ("no", CONFLICT_NO, "explicit") if r == "E2" else None),
               text)


# M-1 서술 상충: M(fix/m-evidence-safety-final)에서 해결 -> strict xfail 제거
def test_E_conflicting_narrative_stops_confirmation(monkeypatch):
    s = _e2_only(monkeypatch, f"합성 환자 62세 남성. {CONFLICT_NO}. 이전 병원 기록: {CONFLICT_YES}.")
    assert s.matches[TID].eligibility != "ELIGIBLE"


def test_E_resolved_record_reevaluates_to_confirmed(monkeypatch):
    s = _e2_only(monkeypatch, f"합성 환자 62세 남성. {CONFLICT_NO}.")
    assert s.matches[TID].eligibility == "ELIGIBLE"


# ----------------------------------------------------------------------------- F/G/H. 전문가·미연결·확정 부적격
def test_F_clinician_judgment_criterion_goes_to_expert_review(monkeypatch):
    H.use_subset(monkeypatch, H.nsclc_parsed(TID), {"I6", "I7", "E3"})
    quote = "심장 질환 병력은 전혀 없습니다"
    s = run(monkeypatch, H.Scenario(adult, lambda t, r, src: ("no", quote, "explicit") if r == "E3" else None),
            f"합성 환자 62세 남성. {quote}.")
    e3 = H.assessment(s, TID, "E3")
    assert e3.passes is None and "clinician_judgment_required" in e3.validation_flags
    assert s.matches[TID].eligibility != "ELIGIBLE" and s.matches[TID].match_status == "NEEDS_REVIEW"


def test_G_unconnected_required_criterion_blocks_confirmed_eligible(monkeypatch):
    H.use_subset(monkeypatch, H.nsclc_parsed(TID), {"I6", "I7", "E4"})  # E4: "Other protocol-defined criteria apply."
    s = run(monkeypatch, H.Scenario(adult, none), "합성 환자 62세 남성.")
    assert s.matches[TID].eligibility != "ELIGIBLE" and s.matches[TID].match_status == "NEEDS_REVIEW"
    assert all(r.match_status != "CRITERIA_MATCH" for r in s.recommendation.ranked)


@pytest.mark.parametrize("case", ["age", "exclusion"])
def test_H_confirmed_ineligible_persists_through_questions(monkeypatch, case):
    H.use_subset(monkeypatch, H.nsclc_parsed(TID), {"I6", "I7", "E2", "E1"})
    yes = "스테로이드 치료가 필요한 간질성 폐렴 병력이 있습니다"
    prof = (lambda u: H.base_profile(age=17, sex="male")) if case == "age" else adult
    text = "합성 환자 17세 남성." if case == "age" else f"합성 환자 62세 남성. {yes}."

    def verd(t, r, s):
        return ("yes", yes, "explicit") if r == "E2" and case == "exclusion" else None

    def q1(rnd, prev):
        return [H.q("Q1", "뇌전이(뇌·척수)가 있다면 현재 상태를 고르세요.", [f"{TID}:E1"], "single_choice",
                    options=["불안정하거나 증상이 있거나 조절되지 않는 뇌전이 있음", "뇌전이 없음"])]
    s = run(monkeypatch, H.Scenario(prof, verd, q1), text, {1: {"Q1": "뇌전이 없음"}, 2: {"Q1": "뇌전이 없음"}})
    assert s.match_history and all(h["eligibility"][TID] == "INELIGIBLE" for h in s.match_history)
    assert s.matches[TID].match_status == "CRITERIA_MISMATCH" and not s.recommendation.recommended


@pytest.mark.parametrize("source,expect", [("clinical_record", "INELIGIBLE"), ("unknown", "UNCERTAIN")])
def test_H_record_value_clear_fail(monkeypatch, source, expect):
    """진료 기록으로 선언된 값이 기준을 명백히 넘으면(ECOG 3 > 1) 확정 부적격. 출처를 모르면 보류(확정하지 않음)."""
    H.use_subset(monkeypatch, H.nsclc_parsed(TID), {"I5", "I6", "I7"})
    s = run(monkeypatch, H.Scenario(lambda u: H.base_profile(age=62, sex="male", ecog=3), none),
            "합성 환자 62세 남성. ECOG 3.", input_source=source)
    assert s.matches[TID].eligibility == expect


# M-2 검사 기간 조건: M(fix/m-evidence-safety-final)에서 해결 -> strict xfail 제거
def test_date_window_lab_is_evaluated_deterministically():
    from ctrec.tools import rule_engine
    dp = trial_store.load(H.demo_trial(), store_dir=config.TRIAL_SETS["default"][1])
    rule = next(r for r in dp.rules if r.rule_id == "I9")  # Hb >= 9.0 g/dL within 28 days
    prof = H.base_profile(age=62, sex="male", labs=[H.lab("hemoglobin", 12.1, "g/dL", "2026-10-20", "Hb 12.1 g/dL")])
    assert rule_engine.evaluate(rule, prof) is not None


# ----------------------------------------------------------------------------- J. 이전 결과 재호출 -> Inspector 차이
def test_J_saved_runs_reload_and_inspector_diff(monkeypatch, tmp_path):
    sc = H.Scenario(profile_full, verdicts_ild_missing, ild_question)
    run(monkeypatch, sc, TEXT_NO_ILD, max_rounds=0).save(tmp_path / "prev")
    run(monkeypatch, sc, TEXT_NO_ILD, {1: {"Q1": "둘 다 해당 없음"}}).save(tmp_path / "cur")
    d = inspector.compare_runs(tmp_path / "prev", tmp_path / "cur")
    assert d["status"] == "ok" and d["changed"]
    e2 = next(c for c in d["criteria"] if c["trial_id"] == TID and c["rule_id"] == "E2")
    assert e2["changes"]["passes"] == {"before": None, "after": True}
    assert inspector.compare_runs(tmp_path / "cur", tmp_path / "cur")["changed"] is False
    blob = json.dumps(d, ensure_ascii=False)
    assert "둘 다 해당 없음" not in blob and "합성 환자" not in blob  # 정제 보기끼리만 비교 (답변·원문 없음)


def test_J_compare_refuses_different_patients(monkeypatch, tmp_path):
    sc = H.Scenario(profile_full, verdicts_full)
    run(monkeypatch, sc, TEXT_FULL, patient_id="SYN-N-1").save(tmp_path / "a")
    run(monkeypatch, sc, TEXT_FULL, patient_id="SYN-N-2").save(tmp_path / "b")
    assert inspector.compare_runs(tmp_path / "a", tmp_path / "b")["status"] == "different_patient"


@pytest.mark.xfail(strict=True, reason="기본 Session 경로, O 소관: 새 세션으로 상태 이전 없음 - fact_store_dir는 저장만 하고 "
                                       "어떤 Session도 FactStore.load로 불러오지 않음 (CLI는 기존 사실 파일을 거부). "
                                       "Follow-up API는 사례 저장·복원으로 해결 (test_six_stage_followup.py)")
def test_J_new_session_can_resume_saved_facts(monkeypatch, tmp_path):
    run(monkeypatch, H.Scenario(profile_full, verdicts_full), TEXT_FULL, fact_store_dir=tmp_path / "facts")
    assert (tmp_path / "facts" / "SYN-N-1.json").exists()
    s2 = Session(case=PatientCase(patient_id="SYN-N-1", raw_text=TEXT_FULL), trials={}, answerer=None,
                 tracer=Tracer(None, verbose=False), fact_store_dir=tmp_path / "facts")
    assert s2.fact_store.facts("SYN-N-1")


# ----------------------------------------------------------------------------- CLI -> NSCLC 세트 -> Inspector
def test_cli_trial_set_and_inspector_flag(monkeypatch, tmp_path):
    from ctrec import cli
    H.install(monkeypatch, H.Scenario(profile_full, verdicts_full), nsclc=False)
    monkeypatch.setattr(config, "TRIALS_DIR", config.TRIALS_DIR)  # --trial-set이 바꾼 값을 테스트 뒤 되돌림
    monkeypatch.setattr(config, "PARSED_DIR", config.PARSED_DIR)
    patient = tmp_path / "p.json"
    patient.write_text(json.dumps({"patient_id": "SYN-CLI-1", "text": TEXT_FULL, "candidate_trials": [TID]},
                                  ensure_ascii=False), encoding="utf-8")
    cli.main(["run", "--patient", str(patient), "--trial-set", "nsclc", "--candidates", "given", "--quiet",
              "--out", str(tmp_path / "out"), "--inspector", "--fact-store-dir"])
    run_dir = tmp_path / "out" / "SYN-CLI-1"
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert set(result["matches"]) == {TID}
    assert result["matches"][TID]["eligibility"] == "UNCERTAIN"  # 파싱본으로 평가됨 (NOT_EVALUATED 아님)
    html = (run_dir / "inspector.html").read_text(encoding="utf-8")
    assert TID in html and "합성 환자" not in html


# ----------------------------------------------------------------------------- 웹: NSCLC 시험 -> 질문 -> 환자 답변 -> 재평가 -> SSE 결과
def test_web_nsclc_question_answer_reevaluation_and_result(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    import web_harness as wh
    from ctrec.web import app as web
    wh.install(monkeypatch, tmp_path)  # 웹 기본 설정(저장 off, 실행 목록 초기화, 대역 백엔드 표시)
    H.install(monkeypatch, H.Scenario(profile_full, verdicts_ild_missing, ild_question))  # NSCLC 세트 + 시나리오 대역
    c = wh.browser()
    trials = c.get("/api/trials").json()
    assert TID in {t["trial_id"] for t in trials}  # 웹 시험 목록이 NSCLC 세트를 읽음
    r = c.post("/api/runs", json={"custom_text": TEXT_NO_ILD, "trial_ids": [TID], "screen": False})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    run_ = web.RUNS[rid]
    qev = wh.wait_for(run_, "questions")
    assert [q["target"] for q in qev["questions"]] == [[f"{TID}:E2"]]
    bad = c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "선택지에 없는 답"}})
    assert bad.status_code == 400  # 선택지 검증 (질문은 그대로 대기)
    assert c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "둘 다 해당 없음"}}).status_code == 200
    wh.wait_for(run_, "end", 30)
    events = wh.sse_events(c.get(f"/api/runs/{rid}/events").text)
    [res] = [e for e in events if e["type"] == "result"]
    m = res["result"]["matches"][TID]
    e2 = next(a for a in m["assessments"] if a["rule_id"] == "E2")
    assert e2["passes"] is True and m["eligibility"] == "UNCERTAIN"  # 재평가 반영, 기록 필요 기준은 여전히 보류
    assert res["display"] is not None and TID in res["display"]["trials"]
    assert [e for e in events if e["type"] == "end"][-1]["state"] == "completed"
