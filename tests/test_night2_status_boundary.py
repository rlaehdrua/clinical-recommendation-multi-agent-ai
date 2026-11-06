"""엔진 쌍 상태와 사용자 출력의 경계 테스트 (C2 전 경로). 합성 데이터, 스텁, LLM·네트워크 없음.

표현 계층(conservative_view)을 검사합니다. 여기서 통과한다고 판정이 바뀐 것이 아닙니다.
eligibility·기준 판정·Fact Store·Session 값이 그대로인지도 함께 검사합니다.

xfail(strict=True)은 보호 파일 승인이 필요한 경로입니다 (docs/dev/night2_r5_status_boundary.md).
임상 정답은 넣지 않습니다. 층 사이 일치, 사실 관계별 구분, 확정 대조군만 봅니다.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from night_consistency_support import AGE, ALIAS, CONSENT, Answerer, Env, cohort_of, lab, rule, session  # noqa: E402

from ctrec import cli, config, conservative_view as cv, llm, pipeline, question_eval as qe  # noqa: E402
from ctrec.agents import question_generator  # noqa: E402
from ctrec.orchestrator import Orchestrator  # noqa: E402
from ctrec.schemas import Fact, LabValue  # noqa: E402
from ctrec.web import app as web  # noqa: E402

BASE = "Synthetic 62-year-old male. "
HB = lab("HB", "hemoglobin", "9")
EG = rule("EG", "EGFR exon 19 deletion", category="biomarker")
OTHER = rule("OT", "Synthetic history item")
DEL, WT, T790M, KRAS = ("EGFR exon 19 deletion detected.", "EGFR wild type on repeat test.", "EGFR T790M detected.",
                        "KRAS G12C detected.")
EG_YES = {"EG": ("yes", True, DEL)}
EG_NO = {"EG": ("no", True, WT)}
RISE = "Hb 8 g/dL on 2026-01-01. Hb 10 g/dL on 2026-09-01."
FALL = "Hb 10 g/dL on 2026-01-01. Hb 8 g/dL on 2026-09-01."
LAB_RE = r"Hb (\d+(?:\.\d+)?) ([a-zA-Z/]+)(?: on (\d{4}-\d\d-\d\d))?"
REPORT_BODY = ("| 순서 | 시험 | 판정 |\n| 1 | {t} | 공개 기준 충족 |\n\n"
               "{t}는 공개 기준 충족으로 볼 수 있습니다.\n\n이번 평가에서 공개 기준 충족 시험을 찾았습니다.")


def profiler(pid, raw, reference_date=None):
    p = qe.stub_profiler(pid, raw, reference_date)
    p.labs = [LabValue(name="hemoglobin", value=float(m[1]), unit=m[2], date=m[3], source_quote=m[0])
              for m in re.finditer(LAB_RE, raw)]
    p.biomarkers = [Fact(description=t, date=None, source_quote=t) for t in (DEL, WT, T790M, KRAS) if t in raw]
    return p


class Run:
    """실제 Session.run_fixed 한 번. with 안에서 사실·판정을 읽음."""

    def __init__(self, tmp_path, trials, raw, *, source="clinical_record", verdicts=None, answer="모름", report=False,
                 rounds=2, cohorts=None):
        self.args = (tmp_path, verdicts)
        self.kw = dict(raw=BASE + raw, answerer=Answerer(lambda q: answer), rounds=rounds, input_source=source,
                       cohorts=cohorts)
        self.trials, self.report, self.prompts = trials, report, []

    def __enter__(self):
        self.env = Env(*self.args)
        e = self.env.__enter__()
        e.oe._set(pipeline.patient_profiler, "profile_patient", profiler)
        e.oe._set(question_generator, "generate_questions", qe.baseline_generator)
        if self.report:
            e.oe._set(config, "WRITE_REPORT", True)
            tid = next(iter(self.trials))
            e.oe._set(llm, "plain", lambda **k: self.prompts.append(k["user"]) or REPORT_BODY.format(t=tid))
        self.s = session(self.trials, **self.kw)
        self.s.run_fixed()
        return self

    def __exit__(self, *exc):
        return self.env.__exit__(*exc)


def view(tmp_path, trials, raw, **kw):
    with Run(tmp_path, trials, raw, **kw) as r:
        return r.s, cv.build(r.s)


# ------------------------------------------------------------ 사실 관계별 분리
@pytest.mark.parametrize("raw,shown,kinds", [
    ("Hb 10 g/dL on 2026-01-01. Hb 10 g/dL on 2026-09-01.", "CRITERIA_MATCH", ()),
    ("Hb 10 g/dL on 2026-01-01. Hb 11 g/dL on 2026-09-01.", "POTENTIAL_MATCH", ("time_dependent",)),
    (RISE, "POTENTIAL_MATCH", ("time_dependent",)),
])
def test_repeated_lab_values_keep_date_difference_apart(tmp_path, raw, shown, kinds):
    s, v = view(tmp_path, {"T": [HB, AGE]}, raw)
    assert s.matches["T"].eligibility == "ELIGIBLE"
    assert v.result["matches"]["T"]["match_status"] == shown
    assert (v.holds["T"].kinds if v.holds else ()) == kinds


@pytest.mark.parametrize("raw", ["Hb 8 g/dL on 2026-09-01. Hb 10 g/dL on 2026-09-01.", "Hb 8 g/dL. Hb 10 g/dL."])
def test_real_lab_contradiction_never_reaches_a_confirmed_label(tmp_path, raw):
    s, v = view(tmp_path, {"T": [HB, AGE]}, raw)
    assert s.matches["T"].match_status == "INSUFFICIENT_INFORMATION" and v.holds == {}


@pytest.mark.parametrize("raw", [f"{DEL} {WT}", f"{DEL} {T790M}"])
def test_differing_biomarker_facts_are_held_without_calling_them_opposite(tmp_path, raw):
    _, v = view(tmp_path, {"T": [EG, AGE]}, raw, verdicts=EG_YES)
    hold = v.holds["T"]
    assert (hold.kinds, hold.display_status) == (("differing_biomarker",), "NEEDS_REVIEW")
    assert any("별개 변이인지 미확인" in n for n in hold.notes())


def test_other_target_biomarker_is_not_held(tmp_path):
    _, v = view(tmp_path, {"T": [EG, AGE]}, f"{DEL} {KRAS}", verdicts=EG_YES)
    assert v.holds == {} and v.result["matches"]["T"]["match_status"] == "CRITERIA_MATCH"


@pytest.mark.parametrize("answer,state", [("Hb 8 g/dL on 2026-09-01", "contradictory"),
                                          ("Hb 11 g/dL on 2026-10-01", "time_dependent")])
def test_source_conflict_between_record_and_answer(tmp_path, answer, state):
    """기록과 환자 답변의 값이 다른 실제 흐름. 어느 경우든 엔진이 확정하지 않은 쌍은 확정으로 보이지 않음."""
    with Run(tmp_path, {"T1": [OTHER], "T2": [HB, AGE]}, "Hb 10 g/dL on 2026-09-01.", answer=answer, rounds=1) as r:
        s, v = r.s, cv.build(r.s)
        facts = s.fact_store.facts(s.case.patient_id, "lab:hemoglobin")
    assert {f.source_type for f in facts} == {"clinical_record", "self_report"}
    assert {f.conflict_status for f in facts} == {state}
    assert v.engine["T2"]["engine_status"] != "CONFIRMED_ELIGIBLE"
    assert v.result["matches"]["T2"]["match_status"] != "CRITERIA_MATCH"


def test_stored_source_conflict_after_the_last_match_is_held(tmp_path):
    """마지막 매칭 뒤 사실이 더해져 판정이 ELIGIBLE로 남은 상태 (저장소 직접 추가로 만든 상태): 출처 상충은 모순으로 표시."""
    with Run(tmp_path, {"T": [HB, AGE]}, "Hb 10 g/dL on 2026-09-01.") as r:
        s = r.s
        s.fact_store.add(s.case.patient_id, "lab:hemoglobin", 8.0, unit="g/dL", source_type="self_report",
                         observed_at="2026-09-01")
        v = cv.build(s)
    assert (s.matches["T"].match_status, v.holds["T"].kinds) == ("CRITERIA_MATCH", ("contradictory",))
    assert v.holds["T"].display_status == "NEEDS_REVIEW"


def test_self_report_source_is_already_not_confirmed(tmp_path):
    _, v = view(tmp_path, {"T": [HB, AGE]}, RISE, source="self_report")
    assert v.holds == {} and v.result["matches"]["T"]["match_status"] == "POTENTIAL_MATCH"


# ------------------------------------------------------------ 불충족 쪽
def test_disputed_exclusion_is_not_shown_as_final_and_is_not_recommended(tmp_path):
    """불충족 근거가 상충뿐이면 '불충족'으로 단정하지 않되, 추천으로 올리지도 않음. 판정은 INELIGIBLE 그대로."""
    s, v = view(tmp_path, {"T": [HB, AGE]}, FALL)
    rec = v.result["recommendation"]
    assert s.matches["T"].eligibility == "INELIGIBLE"
    assert (v.holds["T"].side, v.holds["T"].display_status) == ("mismatch", "NEEDS_REVIEW")
    assert v.result["matches"]["T"]["match_status"] == "NEEDS_REVIEW"
    assert rec["ranked"] == [] and rec["excluded"] == [] and rec["held_out"][0]["trial_id"] == "T"
    assert "확정하지 않은" in rec["no_recommendation_reason"]


def test_disputed_exclusion_stays_out_when_another_trial_is_recommended(tmp_path):
    _, v = view(tmp_path, {"OUT": [HB, AGE], "CLEAN": [AGE]}, FALL)
    rec = v.result["recommendation"]
    assert [r["trial_id"] for r in rec["ranked"]] == ["CLEAN"] and rec["held_out"][0]["trial_id"] == "OUT"


def test_opposite_biomarker_exclusion_is_held(tmp_path):
    _, v = view(tmp_path, {"T": [EG, AGE]}, f"{DEL} {WT}", verdicts=EG_NO)
    assert (v.holds["T"].side, v.holds["T"].kinds) == ("mismatch", ("differing_biomarker",))


def test_clean_failure_next_to_a_conflict_keeps_the_mismatch_label(tmp_path):
    """상충 없는 불충족 기준이 있으면 다른 기준의 상충이 불충족을 풀지 않음."""
    fail = lab("LOW", "platelets", "100", unit="10^3/uL").model_copy(update={"field": "age", "operator": ">=", "value": "99",
                                                                             "unit": "years"})
    _, v = view(tmp_path, {"T": [HB, fail]}, RISE)
    assert v.holds == {} and v.result["matches"]["T"]["match_status"] == "CRITERIA_MISMATCH"
    assert v.engine["T"]["unresolved"] == "exclusion_not_document_verified"


# ------------------------------------------------------------ 상태 교차 점검 + 확정 대조군
def test_confirmed_eligible_control_is_untouched_on_every_field(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, "Hb 10 g/dL on 2026-09-01.", report=True) as r:
        raw, v = r.s.result(), cv.build(r.s)
        text = r.s.report
    assert v.holds == {} and v.report == text
    assert v.result == {**raw, "engine_boundary": v.engine}
    assert v.engine["T"] == {"eligibility": "ELIGIBLE", "external": "CRITERIA_MATCH", "engine_status": "CONFIRMED_ELIGIBLE",
                             "pending_kinds": [], "shown": "CRITERIA_MATCH", "unresolved": None}


def test_confirmed_ineligible_control_keeps_the_mismatch_label(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, "Hb 8 g/dL on 2026-09-01.") as r:
        s = r.s
        fact = s.fact_store.facts(s.case.patient_id, "lab:hemoglobin")[0]
        s.fact_store.set_verification(fact.fact_id, "document_verified", reason="synthetic", by="test",
                                      document_reference="DOC-1")
        v = cv.build(s)
    assert v.holds == {}
    assert (v.engine["T"]["external"], v.engine["T"]["engine_status"], v.engine["T"]["unresolved"]) == (
        "CRITERIA_MISMATCH", "CONFIRMED_INELIGIBLE", None)
    assert [x["trial_id"] for x in v.result["recommendation"]["excluded"]] == ["T"]


def test_unverified_exclusion_is_recorded_but_left_to_r2(tmp_path):
    """문서 검증 없는 불충족: 엔진은 미확정, 화면은 불충족. R2 소관이라 낮추지 않고 불일치로만 기록."""
    _, v = view(tmp_path, {"T": [HB, AGE]}, "Hb 8 g/dL on 2026-09-01.")
    assert v.holds == {}
    assert (v.engine["T"]["engine_status"], v.engine["T"]["pending_kinds"], v.engine["T"]["unresolved"]) == (
        "VERIFICATION_PENDING", ["exclusion_unverified"], "exclusion_not_document_verified")


def test_info_missing_matches_a_non_final_label(tmp_path):
    _, v = view(tmp_path, {"T": [HB, AGE]}, "")
    assert (v.engine["T"]["external"], v.engine["T"]["engine_status"]) == ("INSUFFICIENT_INFORMATION", "INFO_MISSING")
    assert v.holds == {}


def test_no_required_criteria_is_recorded_but_left_to_the_open_policy(tmp_path):
    """필수 기준 0개: 엔진 NOT_EVALUATED, 외부 CRITERIA_MATCH. 정책 미결이므로 낮추지 않고 불일치로만 기록."""
    _, v = view(tmp_path, {"T": [CONSENT]}, "")
    assert v.holds == {} and v.result["matches"]["T"]["match_status"] == "CRITERIA_MATCH"
    assert (v.engine["T"]["engine_status"], v.engine["T"]["unresolved"]) == ("NOT_EVALUATED",
                                                                             "no_required_criteria_policy_open")


def test_evaluation_failure_is_consistent_on_both_sides(tmp_path):
    with Run(tmp_path, {"T": [AGE]}, "") as r:
        r.s.matches["T"] = pipeline._not_evaluated(r.s.case.patient_id, "T", "RuntimeError('x')")
        v = cv.build(r.s)
    assert v.engine["T"]["external"] == v.engine["T"]["engine_status"] == "NOT_EVALUATED" and v.holds == {}


def test_or_tie_is_read_from_the_engine_not_decided_here(tmp_path):
    """동점 정책을 정하지 않음: 엔진이 낸 상태만 따르고, 엔진이 선언 순서를 따르면 표시도 그대로 따름."""
    a, b = HB.model_copy(update={"rule_id": "A1", "cohort": "A"}), AGE.model_copy(update={"rule_id": "B1", "cohort": "B"})
    with Run(tmp_path, {"T": [a, b]}, RISE, cohorts={"T": ("A", "B")}) as r:
        v = cv.build(r.s)
        assert v.engine["T"]["engine_status"] == cohort_of(r.s).pair(ALIAS, "T").status.value
    assert v.engine["T"]["shown"] != "CRITERIA_MATCH" or v.engine["T"]["engine_status"] == "CONFIRMED_ELIGIBLE"


# ------------------------------------------------------------ 표현 사본이 판정을 바꾸지 않음
def test_view_changes_neither_verdicts_nor_facts_nor_session(tmp_path):
    with Run(tmp_path, {"HELD": [HB, AGE], "OUT": [lab("H2", "hemoglobin", "9.5"), AGE]}, RISE) as r:
        s = r.s
        facts = [f.model_dump() for f in s.fact_store.facts(s.case.patient_id)]
        before, source = s.result(), copy.deepcopy(s.result())
        v = cv.build(s, before, None)
        assert before == source == s.result()
        assert [f.model_dump() for f in s.fact_store.facts(s.case.patient_id)] == facts
    assert v.result["matches"]["HELD"]["eligibility"] == "ELIGIBLE"
    assert v.result["matches"]["HELD"]["assessments"] == source["matches"]["HELD"]["assessments"]
    assert all(r["eligibility"] == "ELIGIBLE" for r in v.result["recommendation"]["ranked"])


def test_recommendation_regroups_and_every_held_field_is_lowered(tmp_path):
    _, v = view(tmp_path, {"HELD": [HB, AGE], "CLEAN": [AGE]}, RISE)
    rec = v.result["recommendation"]
    assert [(r["trial_id"], r["rank"], r["match_status"]) for r in rec["ranked"]] == [
        ("CLEAN", 1, "CRITERIA_MATCH"), ("HELD", 2, "POTENTIAL_MATCH")]
    assert [r["trial_id"] for r in rec["recommended"]] == ["CLEAN"]
    assert not any("모순" in n or "상충" in n for n in rec["ranked"][1]["verification_needed"])
    assert "HELD (기록 확인 시 충족 가능)" in rec["overall_comment"]


def test_llm_text_of_a_held_trial_loses_the_confirmed_label(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, RISE) as r:
        result = r.s.result()
        result["recommendation"]["ranked"][0].update(rationale="공개 기준 충족 (CRITERIA_MATCH)",
                                                     key_matches=["공개 기준 충족"])
        v = cv.build(r.s, result, None)
    item = v.result["recommendation"]["ranked"][0]
    assert "공개 기준 충족" not in item["rationale"] and "CRITERIA_MATCH" not in item["rationale"]
    assert item["key_matches"] == ["기록 확인 시 충족 가능"]


# ------------------------------------------------------------ 경로별 출력
def test_report_copy_fixes_code_lines_and_lines_naming_the_trial(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, RISE, report=True) as r:
        v = cv.build(r.s)
    assert v.report.startswith("> **확인 필요:**")
    assert "- **T** (기록 확인 시 충족 가능)" in v.report and "## 추천 그룹: 기록 확인 시 충족 가능" in v.report
    assert "| 1 | T | 기록 확인 시 충족 가능 |" in v.report and "T는 기록 확인 시 충족 가능으로" in v.report
    assert "추천 그룹: 공개 기준 충족" not in v.report


def test_known_limit_report_sentence_without_a_trial_id_stays_below_the_notice(tmp_path):
    """시험을 가리키지 않는 LLM 문장은 사후에 고치지 못함. 맨 위 안내가 우선한다고 밝히는 것까지가 이 계층의 한계."""
    with Run(tmp_path, {"T": [HB, AGE]}, RISE, report=True) as r:
        v = cv.build(r.s)
    assert "이번 평가에서 공개 기준 충족 시험을 찾았습니다." in v.report
    assert v.report.index("이 표시가 우선") < v.report.index("이번 평가에서")


def test_saved_outputs_keep_raw_record_and_add_the_boundary(tmp_path):
    out = tmp_path / "out"
    with Run(tmp_path / "w", {"T": [HB, AGE]}, RISE, report=True) as r:
        r.s.save(out)
        raw = json.loads((out / "result.json").read_text(encoding="utf-8"))
        raw_report = (out / "report.md").read_text(encoding="utf-8")
        cv.save(r.s, out)
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    side = json.loads((out / "conservative_view.json").read_text(encoding="utf-8"))
    assert {k: v for k, v in saved.items() if k != "engine_boundary"} == raw  # 평가·감사 입력은 그대로
    assert saved["engine_boundary"]["T"]["shown"] == "POTENTIAL_MATCH"
    assert side["recommendation"]["ranked"][0]["match_status"] == "POTENTIAL_MATCH"
    assert side["matches"]["T"] == {"eligibility": "ELIGIBLE", "match_status": "POTENTIAL_MATCH"}
    assert (out / "report.raw.md").read_text(encoding="utf-8") == raw_report
    assert (out / "report.md").read_text(encoding="utf-8").startswith("> **확인 필요:**")


def test_saved_outputs_without_a_hold_keep_the_report_file(tmp_path):
    out = tmp_path / "out"
    with Run(tmp_path / "w", {"T": [AGE]}, "", report=True) as r:
        r.s.save(out)
        cv.save(r.s, out)
    assert not (out / "report.raw.md").exists()


def test_cli_prints_the_held_recommendation(tmp_path):
    with Run(tmp_path, {"HELD": [HB, AGE], "CLEAN": [AGE]}, RISE) as r:
        rec = cli.shown_recommendation(r.s)
        original = r.s.recommendation
    assert [(x.trial_id, x.match_status) for x in rec.recommended] == [("CLEAN", "CRITERIA_MATCH")]
    assert [(x.trial_id, x.match_status) for x in rec.ranked][1] == ("HELD", "POTENTIAL_MATCH")
    assert {x.trial_id: x.match_status for x in original.ranked}["HELD"] == "CRITERIA_MATCH"  # Session 값은 그대로


def test_orchestrator_tool_result_uses_the_held_copy(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, RISE) as r:
        out = Orchestrator(r.s)._finalize_recommendation({})
    assert out["ranked"] == [(1, "T", "POTENTIAL_MATCH")]


def test_web_progress_trace_shows_only_the_interim_verdict():
    rec = web._client_record({"agent": "matcher", "event": "done", "eligibility": "ELIGIBLE", "match_status": "CRITERIA_MATCH"})
    assert rec["match_status"] == "ELIGIBLE"  # 화면의 '중간 판정' 표시
    kept = {"agent": "matcher", "event": "done", "eligibility": "UNCERTAIN", "match_status": "NEEDS_REVIEW"}
    assert web._client_record(kept)["match_status"] == "NEEDS_REVIEW"


def test_web_display_does_not_count_a_disputed_exclusion_as_mismatch(tmp_path):
    from ctrec.web import display
    with Run(tmp_path, {"T": [HB, AGE]}, FALL) as r:
        v = cv.build(r.s)
        info = cv.adjust_display(display.build(r.s), v.holds)
    assert info["trials"]["T"]["exclusion"] == cv.HELD_EXCLUSION
    assert info["no_recommendation"]["mismatch"] == [] and info["no_recommendation"][cv.HELD_EXCLUSION] == ["T"]


def test_web_run_and_api_replay_emit_the_held_copy(monkeypatch, tmp_path):
    """실제 웹 실행 경로와 SSE 재생. 하네스의 대역 LLM은 상충 사실을 만들지 않아 보류 판단만 대역."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import web_harness as h
    h.install(monkeypatch, tmp_path)
    monkeypatch.setattr(cv, "evaluate", fake_holds())
    monkeypatch.setenv("CTREC_WEB_SAVE_OUTPUTS", "1")
    monkeypatch.setattr(cv, "adjust_display", lambda info, holds: {"holds": sorted(holds)})
    client = h.browser()
    rid = client.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}).json()["run_id"]
    run = web.RUNS[rid]
    q = h.wait_for(run, "questions")
    client.post(f"/api/runs/{rid}/answers", json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}})
    h.wait_for(run, "end", 30)
    replay = h.sse_events(client.get(f"/api/runs/{rid}/events").text)
    result = next(e for e in replay if e["type"] == "result")["result"]
    assert {m["match_status"] for m in result["matches"].values()} == {"POTENTIAL_MATCH"}
    assert {t: e["shown"] for t, e in result["engine_boundary"].items()} == dict.fromkeys(result["matches"], "POTENTIAL_MATCH")
    assert (tmp_path / "outputs" / "web" / rid / "conservative_view.json").exists()
    assert next(e for e in replay if e["type"] == "result")["display"] == {"holds": sorted(result["matches"])}
    assert "확인 필요" in next(e for e in replay if e["type"] == "result")["report"]


def fake_holds(status="POTENTIAL_MATCH"):
    return lambda s: ({t: cv.Hold(t, "match", "VERIFICATION_PENDING", ("time_dependent",), ("R1",), status)
                       for t in s.matches}, {t: {"shown": status} for t in s.matches})


def test_cli_run_prints_and_saves_the_held_copy(monkeypatch, tmp_path, capsys):
    """cli.main 전체 경로 연결. 대역 프로필은 상충 사실을 만들지 않아 보류 판단만 대역."""
    demo = Path(__file__).resolve().parents[1] / "data" / "trials" / "DEMO-LUNG-001.json"
    patient = tmp_path / "patient_input.json"
    patient.write_text(json.dumps({"patient_id": "CLI-T1", "text": "62-year-old female. ECOG performance status 1.",
                                   "candidate_trials": ["DEMO-LUNG-001"]}), encoding="utf-8")
    monkeypatch.setattr(cv, "evaluate", fake_holds())
    with qe.OfflineEnvironment(2), pipeline.question_generator.use_generator(qe.baseline_generator):
        monkeypatch.setattr(config, "DEMO_MODE", True)
        cli.main(["run", "--patient", str(patient), "--trials", str(demo), "--mode", "fixed", "--candidates", "given",
                  "--answers", "none", "--max-rounds", "1", "--out", str(tmp_path / "out")])
    out = tmp_path / "out" / "CLI-T1"
    assert json.loads((out / "conservative_view.json").read_text(encoding="utf-8"))["holds"][0]["display_status"] == "POTENTIAL_MATCH"
    assert "engine_boundary" in json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert "기록 확인 시 충족 가능" in capsys.readouterr().out


# ------------------------------------------------------------ 보호 파일 승인이 필요한 경로
PATCH = "pipeline.py 승인 패치 필요 (docs/dev/night2_patches/pipeline_finalize.patch)"


@pytest.mark.xfail(strict=True, reason=f"Session 추천이 엔진 상태를 읽지 않음. {PATCH}")
def test_session_recommendation_itself_is_held(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, RISE) as r:
        assert r.s.recommendation.ranked[0].match_status != "CRITERIA_MATCH"


@pytest.mark.xfail(strict=True, reason=f"보고서 LLM 입력이 원래 상태를 받음. {PATCH}")
def test_report_llm_is_not_told_the_held_trial_is_a_match(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, RISE, report=True) as r:
        assert r.s.recommendation is not None and "CRITERIA_MATCH" not in r.prompts[0]
