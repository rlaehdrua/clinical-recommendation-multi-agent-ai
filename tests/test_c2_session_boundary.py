"""C2: Session 추천, LLM 입력, 저장 결과, 웹·API, CLI가 같은 확정성 기준을 쓰는지 검사. 합성 데이터, 스텁, LLM·네트워크 없음.

통과가 판정 수정을 뜻하지 않습니다. eligibility·Fact Store가 그대로인지 함께 봅니다.
LLM 문장 점검은 정규식이라 바꿔 쓴 표현을 놓칩니다. 놓치는 표현은 test_known_limit_*에 그대로 고정합니다.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from night_consistency_support import AGE, ALIAS, CONSENT, Env, cohort_of, lab, layers, rule, run_once, session  # noqa: E402
from test_night2_status_boundary import DEL, EG, EG_YES, FALL, HB, OTHER, RISE, WT, Run  # noqa: E402

from ctrec import cli, config, conservative_view as cv, pipeline, question_eval as qe  # noqa: E402
from ctrec.orchestrator import Orchestrator  # noqa: E402
from ctrec.web import app as web, display  # noqa: E402

CLEAN = "Hb 10 g/dL on 2026-09-01."
VAGUE = rule("VG", "Adequate organ function per investigator", underspecified=True)
VAGUE_YES = {"VG": ("yes", True, "62-year-old")}


def paths(r, out: Path) -> dict[str, dict]:
    """경로별 시험 -> 외부 상태. 추천과 시험별 상태를 나눠 모음."""
    s = r.s
    s.save(out)
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    shown = cv.build(s, display.redact_errors(s.result()), s.report).result  # 웹 result 이벤트와 같은 호출
    ranked = lambda rec: {x["trial_id"]: x["match_status"] for x in rec["ranked"]}  # noqa: E731
    return {"session": {x.trial_id: x.match_status for x in s.recommendation.ranked},
            "saved": ranked(saved["recommendation"]), "web": ranked(shown["recommendation"]),
            "cli": {x.trial_id: x.match_status for x in cli.shown_recommendation(s).ranked},
            "orchestrator": {t: st for _, t, st in Orchestrator(s)._finalize_recommendation({})["ranked"]},
            "saved_match": {t: m["match_status"] for t, m in saved["matches"].items()},
            "web_match": {t: m["match_status"] for t, m in shown["matches"].items()}}


# (이름, 시험, 원문, Run 인자, 판정, 표시, 엔진 상태, 추천 후보 여부)
STATES = [
    ("confirmed", {"T": [HB, AGE]}, CLEAN, {}, "ELIGIBLE", "CRITERIA_MATCH", "CONFIRMED_ELIGIBLE", True),
    ("conflict_dates", {"T": [HB, AGE]}, RISE, {"gate": False}, "ELIGIBLE", "POTENTIAL_MATCH", "VERIFICATION_PENDING", True),
    ("conflict_biomarker", {"T": [EG, AGE]}, f"{DEL} {WT}", {"verdicts": EG_YES, "gate": False}, "ELIGIBLE", "NEEDS_REVIEW",
     "VERIFICATION_PENDING", True),
    # 문서 게이트가 적용되는 기본 경로: 판정은 보류(UNCERTAIN)되어도 상충 사유와 낮춘 표시가 남아야 함
    ("conflict_dates_gated", {"T": [HB, AGE]}, RISE, {}, "UNCERTAIN", "POTENTIAL_MATCH", "VERIFICATION_PENDING", True),
    ("conflict_biomarker_gated", {"T": [EG, AGE]}, f"{DEL} {WT}", {"verdicts": EG_YES}, "UNCERTAIN", "NEEDS_REVIEW",
     "VERIFICATION_PENDING", True),
    ("unknown_answer", {"T": [OTHER, AGE]}, "", {"answer": "모름"}, "UNCERTAIN", "INSUFFICIENT_INFORMATION", "INFO_MISSING", True),
    ("missing_evidence", {"T": [HB, AGE]}, "", {}, "UNCERTAIN", "INSUFFICIENT_INFORMATION", "INFO_MISSING", True),
    ("clinical_pending", {"T": [VAGUE, AGE]}, "", {}, "UNCERTAIN", "NEEDS_REVIEW", "EXPERT_REVIEW", True),
    # 계약 변경: 임상의 경계(A)가 판정 자체를 보류함. 예전 기대값 ELIGIBLE/CONFIRMED_ELIGIBLE은 표시만 낮추던 시기의 값
    ("clinical_llm_pass", {"T": [VAGUE, AGE]}, "", {"verdicts": VAGUE_YES}, "UNCERTAIN", "NEEDS_REVIEW", "EXPERT_REVIEW", True),
    # 계약 변경: R5 P3 확정. 판정부터 NOT_EVALUATED이고 추천 후보가 아님. 예전 ELIGIBLE/NEEDS_REVIEW는 정책 미정 시기의 값
    ("no_required", {"T": [CONSENT]}, "", {}, "NOT_EVALUATED", "NOT_EVALUATED", "NOT_EVALUATED", False),
    ("ineligible", {"T": [HB, AGE]}, "Hb 8 g/dL on 2026-09-01.", {"documented": False}, "INELIGIBLE", "INSUFFICIENT_INFORMATION", "VERIFICATION_PENDING", False),  # P1-4
    ("disputed_exclusion", {"T": [HB, AGE]}, FALL, {}, "INELIGIBLE", "NEEDS_REVIEW", "VERIFICATION_PENDING", False),
]


@pytest.mark.parametrize("name,trials,raw,kw,verdict,shown,engine,candidate", STATES, ids=[s[0] for s in STATES])
def test_every_path_uses_the_same_status(tmp_path, name, trials, raw, kw, verdict, shown, engine, candidate):
    with Run(tmp_path / "w", trials, raw, rounds=1, **kw) as r:
        facts = [f.model_dump() for f in r.s.fact_store.facts(r.s.case.patient_id)]
        got = paths(r, tmp_path / "out")
        v = cv.build(r.s)
        assert r.s.matches["T"].eligibility == verdict  # 판정 보존
        assert [f.model_dump() for f in r.s.fact_store.facts(r.s.case.patient_id)] == facts  # Fact Store 보존
        assert cohort_of(r.s).pair(ALIAS, "T").status.value == engine
    assert v.engine["T"]["shown"] == shown
    ranked = {"T": shown} if candidate else {}
    assert got.pop("saved_match") == got.pop("web_match") == {"T": shown}
    assert all(x == ranked for x in got.values()), got


def test_confirmed_results_are_unchanged_on_every_field(tmp_path):
    """확정 적격·확정 부적격 대조군: 표현 계층을 거쳐도 값이 그대로."""
    with Run(tmp_path, {"OK": [HB, AGE], "NO": [lab("HI", "hemoglobin", "50"), AGE]}, CLEAN, report=True) as r:
        s = r.s
        raw, v = s.result(), cv.build(s)
        assert v.report == s.report
    assert v.holds == {} and v.claims == []
    assert {k: x for k, x in v.result.items() if k != "engine_boundary"} == raw
    assert (v.engine["OK"]["engine_status"], v.engine["NO"]["engine_status"]) == ("CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE")
    assert raw["recommendation"]["held_out"] == []


def test_failed_boundary_at_save_leaves_no_unlowered_file(tmp_path, monkeypatch):
    out = tmp_path / "out"
    with Run(tmp_path / "w", {"T": [HB, AGE]}, RISE) as r:
        monkeypatch.setattr(cv, "evaluate", lambda s: (_ for _ in ()).throw(RuntimeError("boundary")))
        with pytest.raises(RuntimeError):
            r.s.save(out)
    assert not (out / "result.json").exists()


@pytest.mark.parametrize("flag", [[], ["--redact-console"]], ids=["plain", "redacted"])
def test_cli_lists_a_held_out_trial_instead_of_dropping_it(monkeypatch, tmp_path, capsys, flag):
    """실제 cli.main 경로. 대역 프로필은 상충 사실을 만들지 않아 보류 판단만 대역. PR #24 --redact-console과 함께 확인."""
    demo = Path(__file__).resolve().parents[1] / "data" / "trials" / "DEMO-LUNG-001.json"
    patient = tmp_path / "patient_input.json"
    patient.write_text(json.dumps({"patient_id": "CLI-T3", "text": "62-year-old female. ECOG performance status 1.",
                                   "candidate_trials": ["DEMO-LUNG-001"]}), encoding="utf-8")
    hold = cv.Hold("DEMO-LUNG-001", "mismatch", "VERIFICATION_PENDING", ("contradictory",), ("R1",), "NEEDS_REVIEW")
    monkeypatch.setattr(cv, "evaluate", lambda s: ({"DEMO-LUNG-001": hold}, {}))
    with qe.OfflineEnvironment(2), pipeline.question_generator.use_generator(qe.baseline_generator):
        monkeypatch.setattr(config, "DEMO_MODE", True)
        cli.main(["run", "--patient", str(patient), "--trials", str(demo), "--mode", "fixed", "--candidates", "given",
                  "--answers", "none", "--max-rounds", "1", *flag, "--out", str(tmp_path / "out")])
    text = capsys.readouterr().out
    assert "근거 확인 대기(확정 제외 아님): DEMO-LUNG-001(의료진 검토 필요)" in text
    assert ("CLI-T3" in text) != bool(flag)


# ------------------------------------------------------------ 추천 없는 상태
def test_no_recommendation_when_every_trial_fails(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, "Hb 8 g/dL on 2026-09-01.") as r:
        rec = r.s.recommendation
    assert rec.ranked == [] and [x.trial_id for x in rec.excluded] == ["T"] and rec.held_out == []
    assert "맞지 않았습니다" in rec.no_recommendation_reason  # 실제 불충족은 문구를 바꾸지 않음


def test_no_recommendation_with_a_disputed_exclusion_does_not_say_every_trial_failed(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, FALL, report=True) as r:
        rec, report = r.s.recommendation, r.s.report
    assert rec.ranked == [] and rec.excluded == [] and rec.held_out[0].trial_id == "T"
    assert "맞지 않았습니다" not in rec.no_recommendation_reason and "확정된 시험이 없습니다" in rec.no_recommendation_reason
    assert "## 추천 결과: 추천 가능한 임상시험 없음" in report and "## 정책 엔진 미확정 항목" in report


def test_held_closed_trial_is_not_described_as_a_match(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, RISE, status={"T": "COMPLETED"}) as r:
        rec = r.s.recommendation
    assert rec.ranked == [] and "공개 기준 충족" not in rec.no_recommendation_reason
    assert "기록 확인 시 충족 가능" in rec.no_recommendation_reason


def test_not_evaluated_stays_out_and_is_not_called_ineligible(tmp_path):
    with Run(tmp_path, {"T": [AGE]}, "") as r:
        r.s.matches["T"] = pipeline._not_evaluated(r.s.case.patient_id, "T", "RuntimeError('x')")
        r.s.finalize()
        rec, v = r.s.recommendation, cv.build(r.s)
    assert v.holds == {} and rec.ranked == [] and "부적격 아님" in rec.no_recommendation_reason
    assert r.s.matches["T"].eligibility == "NOT_EVALUATED"


def test_no_matches_is_an_empty_recommendation(tmp_path):
    with Env(tmp_path):
        s = session({})
        s.profile_patient()
        s.finalize()
        assert s.recommendation.ranked == [] and cv.build(s).holds == {}


# ------------------------------------------------------------ LLM 입력
def test_recommender_llm_gets_the_held_status_and_summary(tmp_path):
    with Run(tmp_path, {"T": [HB, AGE]}, RISE, gate=False) as r:
        prompt = r.rec_prompts[0]
        kept = r.s.matches["T"]
    assert "CRITERIA_MATCH" not in prompt.split("</patient>")[1] and "ELIGIBLE:" not in prompt
    assert '"match_status": "POTENTIAL_MATCH"' in prompt and kept.summary.startswith("ELIGIBLE")


def test_report_llm_gets_no_verdict_code_for_held_trials_only(tmp_path):
    with Run(tmp_path, {"HELD": [HB, AGE], "OK": [AGE]}, RISE, report=True, gate=False) as r:
        payload = json.loads(r.prompts[0].split("<results>")[1].split("</results>")[0])
        kept = r.s.match_history
    by_id = {m["trial_id"]: m for m in payload["matches"]}
    assert (by_id["HELD"]["eligibility"], by_id["HELD"]["match_status"]) == ("UNCERTAIN", "POTENTIAL_MATCH")
    assert (by_id["OK"]["eligibility"], by_id["OK"]["match_status"]) == ("ELIGIBLE", "CRITERIA_MATCH")
    assert not by_id["HELD"]["summary"].startswith("ELIGIBLE")
    assert all(h["eligibility"]["HELD"] == "보류" for h in payload["eligibility_history"])
    assert all(h["eligibility"]["HELD"] == "ELIGIBLE" for h in kept)  # 원래 이력은 그대로
    held = next(x for x in payload["recommendation"]["ranked"] if x["trial_id"] == "HELD")
    assert held["eligibility"] == "UNCERTAIN"


def test_recommender_llm_text_on_an_unconfirmed_trial_is_removed(tmp_path):
    with Run(tmp_path, {"T": [OTHER, AGE]}, "", rationale="이 시험에 참여 가능합니다.") as r:
        item, comment = r.s.recommendation.ranked[0], r.s.recommendation.overall_comment
    assert "참여 가능" not in item.rationale and item.rationale.startswith("추가 정보 필요:")
    assert "참여 가능" not in " ".join(item.key_matches) and "참여 가능" not in comment


def test_recommender_llm_text_on_a_confirmed_trial_is_kept(tmp_path):
    with Run(tmp_path, {"T": [AGE]}, "", rationale="공개 기준을 모두 충족합니다.") as r:
        assert r.s.recommendation.ranked[0].rationale == "공개 기준을 모두 충족합니다."


def test_boundary_failure_stops_the_run_instead_of_recommending(tmp_path, monkeypatch):
    with Run(tmp_path, {"T": [AGE]}, "") as r:
        r.s.recommendation = None
        monkeypatch.setattr(cv, "evaluate", lambda s: (_ for _ in ()).throw(RuntimeError("boundary")))
        with pytest.raises(RuntimeError):
            r.s.finalize()
        assert r.s.recommendation is None


def test_applying_the_view_twice_changes_nothing(tmp_path):
    with Run(tmp_path, {"HELD": [HB, AGE], "OUT": [lab("H2", "hemoglobin", "9.5"), AGE], "OK": [AGE]}, RISE) as r:
        once = r.s.recommendation.model_dump()
        twice = cv.build(r.s).result["recommendation"]
        again = cv.build(r.s, {**r.s.result(), "recommendation": twice}, None).result["recommendation"]
    assert once == twice == again
    assert sum(cv.NOTE_BY_KIND["time_dependent"] in n for n in once["ranked"][1]["verification_needed"]) == 1


# ------------------------------------------------------------ 보고서 LLM 원문의 잔여 위험
CLAIMS = ["{t}는 공개 기준을 모두 충족합니다.", "{t}: 공개 기준 충족", "{t}는 적격입니다.", "{t}는 ELIGIBLE입니다.", "{t}는 eligible 입니다.",
          "{t}에 참여 가능합니다.", "{t}에 참여할 수 있습니다.", "{t}에 등록 가능합니다.", "{t}는 참여 대상입니다.",
          "{t}는 모든 선정 기준을 충족합니다.", "{t}는 선정/제외 기준을 충족합니다.", "{t}의 기준을 전부 만족합니다.", "{t}가 가장 적합합니다."]
NOT_CLAIMS = ["{t}의 공개 기준 충족 여부는 확인이 필요합니다.", "{t}의 실제 참여 가능 여부는 연구기관이 정합니다.", "{t}는 공개 기준 불충족입니다.",
              "{t}는 공개 기준을 충족하지 못합니다.", "{t}는 부적격이 아닙니다. 평가 실패입니다.", "{t}의 적격성은 판단하지 않았습니다.",
              "{t}는 기록 확인 시 충족 가능입니다.", "{t}: 나이 기준 충족, 혈색소 기준 미확인.", "{t}에 참여할 수 있는지 확인이 필요합니다."]


@pytest.mark.parametrize("line", CLAIMS)
def test_report_claim_on_a_held_trial_is_lowered(tmp_path, line):
    text = line.format(t="T")
    with Run(tmp_path, {"T": [HB, AGE]}, RISE, report=True, body=text) as r:
        v = cv.build(r.s)
        assert text in r.s.report  # LLM 원문은 Session에 그대로
    assert text not in v.report and all(c["trials"] == ["T"] for c in v.claims)  # 라벨 그대로면 앞 단계에서 이미 바뀜
    assert "T: 기록 확인 시 충족 가능" in v.report or "T는 기록 확인 시 충족 가능" in v.report


@pytest.mark.parametrize("line", CLAIMS)
def test_report_claim_on_a_trial_without_a_hold_is_lowered_too(tmp_path, line):
    """보류가 아닌 미확정(추가 정보 필요)도 같은 점검을 받음."""
    with Run(tmp_path, {"T": [OTHER, AGE]}, "", report=True, body=line.format(t="T")) as r:
        v = cv.build(r.s)
    assert v.holds == {} and len(v.claims) == 1 and "T: 추가 정보 필요" in v.report


@pytest.mark.parametrize("line", NOT_CLAIMS)
def test_report_negation_and_open_questions_are_not_touched(tmp_path, line):
    text = line.format(t="T")
    with Run(tmp_path, {"T": [OTHER, AGE]}, "", report=True, body=text) as r:
        v = cv.build(r.s)
    assert v.claims == [] and text in v.report


@pytest.mark.parametrize("line", CLAIMS)
def test_report_claim_on_a_confirmed_trial_is_kept(tmp_path, line):
    text = line.format(t="T")
    with Run(tmp_path, {"T": [HB, AGE]}, CLEAN, report=True, body=text) as r:
        v = cv.build(r.s)
        assert v.report == r.s.report


def test_report_line_under_a_trial_heading_follows_the_heading(tmp_path):
    body = "\n".join(["### HELD", "이 시험은 공개 기준을 모두 충족합니다.", "", "### OK", "이 시험은 공개 기준을 모두 충족합니다."])
    with Run(tmp_path, {"HELD": [HB, AGE], "OK": [AGE]}, RISE, report=True, body=body) as r:
        v = cv.build(r.s)
    assert [c["trials"] for c in v.claims] == [["HELD"]]
    assert v.report.count("이 시험은 공개 기준을 모두 충족합니다.") == 1


def test_report_table_row_keeps_its_cells(tmp_path):
    with Run(tmp_path, {"T": [OTHER, AGE]}, "", report=True, body="| 1 | T | 참여 가능 | 모집 중 |") as r:
        v = cv.build(r.s)
    assert "| 1 | T | 추가 정보 필요 | 모집 중 |" in v.report


def test_general_sentence_stays_when_a_confirmed_trial_exists_and_nothing_is_held(tmp_path):
    body = "이번 평가에서 공개 기준 충족 시험을 찾았습니다."
    with Run(tmp_path, {"OK": [AGE], "MORE": [OTHER, AGE]}, "", report=True, body=body) as r:
        v = cv.build(r.s)
    assert v.claims == [] and body in v.report


def test_general_sentence_is_lowered_when_a_hold_exists(tmp_path):
    body = "이번 평가에서 공개 기준 충족 시험을 찾았습니다."
    with Run(tmp_path, {"HELD": [HB, AGE], "OK": [AGE]}, RISE, report=True, body=body) as r:
        v = cv.build(r.s)
    assert body not in v.report and v.claims[0]["trials"] == ["HELD"]


def test_code_lines_of_the_report_are_not_scanned(tmp_path):
    with Run(tmp_path, {"HELD": [HB, AGE], "OK": [AGE]}, RISE, report=True, body="본문") as r:
        v = cv.build(r.s)
    assert v.claims == [] and "## 추천 그룹: 공개 기준 충족" in v.report and "표시 순서 기준" in v.report


def test_claim_scan_on_a_saved_report_keeps_the_llm_original(tmp_path):
    out = tmp_path / "out"
    with Run(tmp_path / "w", {"T": [OTHER, AGE]}, "", report=True, body="T에 참여 가능합니다.") as r:
        r.s.save(out)
    assert "참여 가능합니다" in (out / "report.raw.md").read_text(encoding="utf-8")
    assert "참여 가능합니다" not in (out / "report.md").read_text(encoding="utf-8")
    side = json.loads((out / "conservative_view.json").read_text(encoding="utf-8"))
    assert side["report_claims"][0]["trials"] == ["T"] and side["holds"] == []


@pytest.mark.parametrize("line", ["T는 이 환자에게 잘 맞는 시험입니다.", "T에 들어갈 수 있습니다.", "T는 조건에 해당합니다.", "The patient qualifies for T."])
def test_known_limit_paraphrases_pass_the_scan(tmp_path, line):
    """정규식 밖의 바꿔 쓴 표현은 남음. 보류가 있으면 맨 위 안내가 우선한다는 표시까지가 이 계층의 한계 (실제 LLM 문체는 미측정)."""
    with Run(tmp_path, {"T": [HB, AGE]}, RISE, report=True, body=line) as r:
        v = cv.build(r.s)
    assert line in v.report and v.report.startswith("> **확인 필요:**")


# ------------------------------------------------------------ G2: 코호트 선언 순서 (N5에서 엔진 부분만 가져옴)
HB_A = HB.model_copy(update={"rule_id": "A1", "cohort": "A"})
AGE_B = AGE.model_copy(update={"rule_id": "B1", "cohort": "B"})
EXTRA_C = AGE.model_copy(update={"rule_id": "C1", "cohort": "C"})


def tied(tmp_path, order, raw=RISE, rules=(HB_A, AGE_B, EXTRA_C)):
    with Run(tmp_path, {"T": list(rules)}, raw, cohorts={"T": tuple(order)}, gate=False) as r:
        return r.s.recommendation.ranked[0].match_status, cv.build(r.s), r.s.matches["T"].matched_cohort


def test_tied_cohort_order_does_not_change_the_recommendation(tmp_path):
    seen = [tied(tmp_path / "".join(o), o) for o in itertools.permutations("ABC")]
    assert {x[0] for x in seen} == {"POTENTIAL_MATCH"}
    assert {x[1].engine["T"]["engine_status"] for x in seen} == {"VERIFICATION_PENDING"}
    assert {x[1].holds["T"].rule_ids for x in seen} == {("A1",)}  # 동점 코호트의 기준을 사유로 지목


def test_closed_cohort_branch_does_not_hold_the_open_path(tmp_path):
    bad = lab("A1", "hemoglobin", "99", cohort="A")
    status, v, cohort = tied(tmp_path, ("A", "B"), rules=(bad, AGE_B))
    assert (status, cohort, v.holds) == ("CRITERIA_MATCH", "B", {})


def test_single_conflict_free_tie_stays_confirmed(tmp_path):
    status, v, _ = tied(tmp_path, ("B", "C"), raw="", rules=(AGE_B, EXTRA_C))
    assert status == "CRITERIA_MATCH" and v.holds == {}


def test_uncertain_tie_status_is_order_free_in_the_engine(tmp_path):
    a, b = rule("A1", cohort="A"), rule("B1", underspecified=True, cohort="B")
    out = []
    for order in (("A", "B"), ("B", "A")):
        with Env(tmp_path / "".join(order)):
            out.append(layers(run_once(session({"T": [AGE, a, b]}, cohorts={"T": order})))["T"])
    assert out[0]["status"] == out[1]["status"] == "INFO_MISSING"
    assert out[0]["pending"] == out[1]["pending"] == ["missing", "physician_review"]


def test_fewer_unknowns_is_not_a_tie(tmp_path):
    a, b1, b2 = rule("A1", cohort="A"), rule("B1", underspecified=True, cohort="B"), rule("B2", cohort="B")
    for order in (("A", "B"), ("B", "A")):
        with Env(tmp_path / "".join(order)):
            x = layers(run_once(session({"T": [AGE, a, b1, b2]}, cohorts={"T": order})))["T"]
        assert (x["cohort"], x["status"], x["pending"]) == ("A", "INFO_MISSING", ["missing"])


def test_web_progress_keeps_interim_status_off_the_confirmed_label():
    rec = web._client_record({"agent": "matcher", "event": "done", "eligibility": "ELIGIBLE", "match_status": "CRITERIA_MATCH"})
    assert rec["match_status"] != "CRITERIA_MATCH"
