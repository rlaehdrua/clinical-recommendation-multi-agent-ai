"""R5 P3 확정 계약: 출력 경로별 상태 일관성 (합성 데이터, LLM·API 호출 없음).

필수 기준이 없는 시험은 NOT_EVALUATED, 추천 제외, 담당자 검토. 평가 실패(criteria_parse_empty·no_source_criteria)와
사유 코드·문구로 구분합니다. 기본 Session, 웹, CLI, 저장 결과, Inspector, WP6가 같은 답을 내는지 봅니다.
층별 판정 변화는 tests/test_r5_no_required_criteria.py, 불일치 규칙은 tests/test_status_consistency.py가 다룹니다.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_r5_no_required_criteria import (ALIAS, CONSENT, NOT_A_MAN, SOURCE, Result, age, conditional, env, parsed,  # noqa: F401,E402
                                          rule, run, said, trial)
import test_record_producer as trp  # noqa: E402

from ctrec import cli, config, inspector, pipeline, question_eval as qe, trial_store  # noqa: E402
from ctrec.agents import matcher, parser_validator as pv, recommender  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine import record_producer as rp  # noqa: E402
from ctrec.sim import demo, runner  # noqa: E402
from ctrec.sim.cohort import make_cohort  # noqa: E402
from ctrec.sim.evaluator import OfflineEvaluator, make_fact  # noqa: E402
from ctrec.web import app as web_app, display  # noqa: E402

LABEL = "판정할 필수 기준 없음 · 담당자 확인 필요"
FAILED = "평가 실패"
ALL_NA = {"I1": said("I1", NOT_A_MAN, holds="no", applicable=False)}
REVIEW = [pytest.param([rule("I1", CONSENT)], {}, "no_required_criteria", id="procedural_only"),
          pytest.param([conditional("I1")], ALL_NA, "no_applicable_criteria", id="all_not_applicable")]
BROKEN = [pytest.param(SOURCE, "criteria_parse_empty", id="parse_empty"),
          pytest.param("", "no_source_criteria", id="no_source")]


def _saved(r: Result, tmp_path: Path) -> Path:
    """실제 저장 경로로 쓴 실행 폴더."""
    out = tmp_path / "run"
    r.session.save(out)
    return out


# ------------------------------------------------------------ 사유 코드 구분
def test_codes_split_review_from_failure():
    assert matcher.NO_EVIDENCE_CODES == {"no_required_criteria", "no_applicable_criteria"}
    assert not matcher.NO_EVIDENCE_CODES & {matcher.CRITERIA_PARSE_EMPTY, matcher.NO_SOURCE_CRITERIA}
    assert set(inspector._ELIG_BY_CODE) == matcher.NO_EVIDENCE_CODES  # Inspector는 matcher를 부르지 않으므로 값을 맞춰 둠
    assert matcher.NO_EVIDENCE_CODES <= C.REVERSAL_REASON_CODES


@pytest.mark.parametrize("rules, answers, code", REVIEW)
def test_no_required_is_reviewed_under_its_own_code(env, rules, answers, code):
    env.answers.update(answers)
    r = run("SYN-P3", rules)
    assert (r.match.eligibility, r.match.match_status, r.match.evaluation_error) == ("NOT_EVALUATED", "NOT_EVALUATED", code)
    assert r.ranked == [] and r.rec.recommended == []
    assert r.actions == {("CLINICIAN_REVIEW", code)}  # 검토는 한 건, evaluation_failed와 중복되지 않음


@pytest.mark.parametrize("source, code", BROKEN)
def test_real_failures_keep_evaluation_failed(env, source, code):
    r = run("SYN-E1", source=source)
    assert (r.match.eligibility, r.match.evaluation_error) == ("NOT_EVALUATED", code)
    assert r.actions == {("CLINICIAN_REVIEW", "evaluation_failed")}
    assert FAILED in r.excluded["SYN-E1"] and r.excluded["SYN-E1"].startswith("평가 실패로")


def test_mixed_trials_each_get_their_own_review(env):
    """같은 환자에 두 사유가 섞여도 각 시험이 자기 사유로만 검토됩니다."""
    from ctrec.decision_state import SessionView, build_state
    from ctrec.engine.cohort_state import CohortState
    from ctrec.engine.policy import LegacyAdapterPolicy
    from test_r5_no_required_criteria import tsf

    trials = {"SYN-A": trial("SYN-A"), "SYN-B": trial("SYN-B")}
    trial_store.save(trials["SYN-A"], parsed("SYN-A", [rule("I1", CONSENT)]), {}, pv.ValidationReport())
    trial_store.save(trials["SYN-B"], parsed("SYN-B"), {}, pv.ValidationReport())
    s = pipeline.Session(case=pipeline.PatientCase(ALIAS, "Synthetic 62-year-old female.", input_source="clinical_record",
                                                   candidate_trials=list(trials)),
                         trials=trials, answerer=tsf._Answerer(), tracer=tsf._Tracer())
    s.profile_patient()
    s.match()
    s.finalize()
    view = SessionView.from_session(s)
    view.matched_versions = {t: view.snapshot.version for t in s.matches}
    co = CohortState.from_decision_states({ALIAS: build_state(view)})
    reviews = {(a.reason_code, a.target_trial_ids) for a in LegacyAdapterPolicy().propose(co)
               if a.action_type == "CLINICIAN_REVIEW"}
    assert reviews == {("no_required_criteria", ("SYN-A",)), ("evaluation_failed", ("SYN-B",))}
    reason = s.recommendation.no_recommendation_reason
    assert "평가에 실패한 시험 1개(SYN-B)" in reason and "판정할 필수 기준이 없는 시험 1개(SYN-A)" in reason
    assert "모든 후보 임상시험을 판정하지 못해" in reason


# ------------------------------------------------------------ 정상 시험은 영향 없음
@pytest.mark.parametrize("rules, eligibility, external, ranked", [
    pytest.param([age("I1")], "ELIGIBLE", "CRITERIA_MATCH", True, id="inclusion_met"),
    pytest.param([age("I1"), rule("I2", CONSENT)], "ELIGIBLE", "CRITERIA_MATCH", True, id="inclusion_met_with_procedural"),
    pytest.param([age("I1", "70")], "INELIGIBLE", "CRITERIA_MISMATCH", False, id="inclusion_failed"),
    pytest.param([age("E1", kind="exclusion")], "INELIGIBLE", "CRITERIA_MISMATCH", False, id="exclusion_holds"),
    pytest.param([age("I1"), age("E1", "70", kind="exclusion")], "ELIGIBLE", "CRITERIA_MATCH", True, id="exclusion_clear"),
])
def test_normal_inclusion_and_exclusion_trials_do_not_change(env, rules, eligibility, external, ranked):
    r = run("SYN-OK", rules)
    assert (r.match.eligibility, r.match.match_status, r.match.evaluation_error) == (eligibility, external, None)
    assert bool(r.ranked) is ranked
    assert not {c for _, c in r.actions} & (matcher.NO_EVIDENCE_CODES | {"evaluation_failed"})


def test_procedural_failure_stays_a_mismatch_not_a_review(env):
    """절차 기준이 불충족으로 판정되면 그 판정이 근거이므로 no_required_criteria가 아닙니다 (P3 정의)."""
    env.answers["I1"] = said("I1", RAW_NO_CONSENT, holds="no")
    r = run("SYN-LF", [rule("I1", CONSENT)])
    assert r.match.eligibility == "INELIGIBLE" and r.match.evaluation_error is None and r.ranked == []


RAW_NO_CONSENT = "Able to give informed consent"


# ------------------------------------------------------------ 저장 결과·Inspector
@pytest.mark.parametrize("rules, answers, code", REVIEW)
def test_saved_result_and_inspector_show_the_review_label(env, tmp_path, rules, answers, code):
    env.answers.update(answers)
    out = _saved(run("SYN-P3", rules), tmp_path)
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    m = saved["matches"]["SYN-P3"]
    assert (m["eligibility"], m["match_status"], m["evaluation_error"]) == ("NOT_EVALUATED", "NOT_EVALUATED", code)
    assert saved["recommendation"]["ranked"] == []
    ex = saved["recommendation"]["excluded"][0]["reason"]
    assert "담당자 확인 필요" in ex and FAILED not in ex and FAILED not in saved["recommendation"]["no_recommendation_reason"]

    v = inspector.build_view(inspector.load_run(out))
    t = v["trials"][0]
    assert t["review_code"] == code and t["error"] is None
    assert (v["overview"]["n_not_evaluated"], v["overview"]["n_no_required"]) == (0, 1)
    page = inspector.render_html(v)
    assert LABEL in page and "평가 실패 0개" in page and "평가 실패:" not in page
    assert '<span class="mut">평가 실패</span>' not in page


@pytest.mark.parametrize("source, code", BROKEN)
def test_inspector_keeps_evaluation_failed_for_real_failures(env, tmp_path, source, code):
    v = inspector.build_view(inspector.load_run(_saved(run("SYN-E1", source=source), tmp_path)))
    assert v["trials"][0]["review_code"] is None and v["trials"][0]["error"] is not None
    assert (v["overview"]["n_not_evaluated"], v["overview"]["n_no_required"]) == (1, 0)
    page = inspector.render_html(v)
    assert '<span class="mut">평가 실패</span>' in page and LABEL not in page


def test_older_record_without_a_code_stays_evaluation_failed(env, tmp_path):
    """사유 코드가 없는 이전 기록은 새 문구로 추정하지 않습니다."""
    out = _saved(run("SYN-P3", [rule("I1", CONSENT)]), tmp_path)
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    saved["matches"]["SYN-P3"]["evaluation_error"] = None
    (out / "result.json").write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    page = inspector.render_html(inspector.build_view(inspector.load_run(out)))
    assert '<span class="mut">평가 실패</span>' in page and LABEL not in page


# ------------------------------------------------------------ 웹
def test_web_copy_uses_the_review_kind(env):
    r = run("SYN-P3", [rule("I1", CONSENT)])
    d = display.build(r.session)
    assert d["trials"]["SYN-P3"]["exclusion"] == "needs_review"
    assert d["trials"]["SYN-P3"]["error_type"] == "no_required_criteria"
    assert d["no_recommendation"] == {"needs_review": ["SYN-P3"]}
    ex = display.redact_errors(r.session.result())["recommendation"]["excluded"][0]["reason"]
    assert ex.endswith("담당자 확인 필요. 원인: no_required_criteria") and "재평가" not in ex


@pytest.mark.parametrize("source, code", BROKEN)
def test_web_copy_keeps_the_failure_kind(env, source, code):
    r = run("SYN-E1", source=source)
    assert display.build(r.session)["trials"]["SYN-E1"]["exclusion"] == "not_evaluated"
    assert "재평가 또는 담당자 확인 필요" in display.redact_errors(r.session.result())["recommendation"]["excluded"][0]["reason"]


def test_progress_record_carries_only_the_fixed_code():
    keep = web_app._client_record({"agent": "matcher", "event": "match", "evaluation_error": "no_required_criteria"})
    assert keep["review_code"] == "no_required_criteria" and "evaluation_error" not in keep
    raw = web_app._client_record({"agent": "matcher", "event": "match", "evaluation_error": "RuntimeError('환자 원문')"})
    assert "review_code" not in raw and "evaluation_error" not in raw


def test_screen_text_has_the_review_label():
    js = (Path(web_app.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert f'NO_REQUIRED: ["{LABEL}"' in js and "needs_review:" in js


# ------------------------------------------------------------ CLI
def test_cli_prints_the_review_reason_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PARSED_DIR", tmp_path / "parsed")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    tid = "NCT99999997"
    path = tmp_path / f"{tid}.json"
    path.write_text(json.dumps(trial(tid)), encoding="utf-8")
    patient = tmp_path / "patient_input.json"
    patient.write_text(json.dumps({"patient_id": "CLI-R5", "text": "Synthetic 62-year-old female.",
                                   "candidate_trials": [tid]}), encoding="utf-8")
    out, buf = tmp_path / "out", io.StringIO()
    argv = ["run", "--patient", str(patient), "--trials", str(path), "--mode", "fixed", "--candidates", "given",
            "--answers", "none", "--max-rounds", "1", "--out", str(out)]
    trial_store.save(trial(tid), parsed(tid, [rule("I1", CONSENT)]), {}, pv.ValidationReport())
    with qe.OfflineEnvironment(2), pipeline.question_generator.use_generator(qe.baseline_generator), redirect_stdout(buf):
        cli.main(argv)
    text = buf.getvalue()
    assert f"판정할 필수 기준이 없는 시험 1개({tid})" in text and "평가에 실패" not in text
    saved = json.loads((out / "CLI-R5" / "result.json").read_text(encoding="utf-8"))
    assert saved["matches"][tid]["evaluation_error"] == "no_required_criteria"


# ------------------------------------------------------------ WP6
def _sim_cohort():
    logistics = demo._trial("T-LOG", [demo._rule("I1", CONSENT, "consent_or_logistics", structured=False)], {})
    age_only = demo._trial("T-AGE", [demo.AGE18], {})
    fact = demo._f("age", 64, "years")
    return make_cohort("r5-p3", demo.REFERENCE_DATE, [logistics, age_only], [("SYN-P", [fact], [fact])], "r5-p3")


def test_wp6_evaluator_gives_the_session_answer():
    cohort = _sim_cohort()
    alias = "SYN-ALIAS"
    fact = make_fact(alias, demo._f("age", 64, "years"), seq=1, origin="public", source_type="clinical_record",
                     verified=True, ref_kind="input_text", ref="public:age")
    st, _ = OfflineEvaluator(cohort.trials, cohort.reference_date).evaluate(alias, [fact])
    assert st.not_evaluated_trials == ["T-LOG"] and st.no_required_trials == {"T-LOG": "no_required_criteria"}
    assert st.trials["T-AGE"].eligibility == "ELIGIBLE"


def test_wp6_run_reviews_under_the_code_and_leaves_normal_pairs():
    cohort = _sim_cohort()
    budget = C.EffortBudget(coordinator_minutes=400, clinician_minutes=60, horizon_steps=10)
    result = runner.run(runner.build_spec("r5", "legacy_greedy", cohort, budget, 1, None), cohort)
    alias = next(iter(result.final.patients))
    assert result.final.pair(alias, "T-LOG").status == C.PairStatus.NOT_EVALUATED
    assert result.final.pair(alias, "T-LOG").review_code == "no_required_criteria"
    assert result.final.pair(alias, "T-AGE").status == C.PairStatus.CONFIRMED_ELIGIBLE
    reviews = [r.action for r in result.records if r.action.target_trial_ids == ("T-LOG",)]
    assert reviews and {a.reason_code for a in reviews} == {"no_required_criteria"}


def test_audit_record_names_the_code_on_a_downgrade():
    """확정 쌍이 근거를 잃어 내려갈 때 감사 기록이 평가 실패로 적지 않습니다. 진짜 실패는 그대로."""
    before = trp.cohort(trp.pair("T1", C.PairStatus.CONFIRMED_ELIGIBLE, [trp.crit("T1", "I1", "MET")], "ELIGIBLE"))
    key = C.PairKey(patient_alias=trp.A, trial_id="T1")
    review = trp.cohort(trp.PairState(key, C.PairStatus.NOT_EVALUATED, "NOT_EVALUATED", True,
                                      review_code="no_required_criteria"))
    failed = trp.cohort(trp.PairState(key, C.PairStatus.NOT_EVALUATED, "NOT_EVALUATED", True))
    assert rp.build_step(trp.obs(before=before, after=review)).record.pairs[0].reason_code == "no_required_criteria"
    assert rp.build_step(trp.obs(before=before, after=failed)).record.pairs[0].reason_code == "evaluation_failed"


# ------------------------------------------------------------ 추천 문구
def test_recommender_exclusion_reason_is_built_from_the_code(env):
    assert "담당자 확인 필요" in run("SYN-P3", [rule("I1", CONSENT)]).excluded["SYN-P3"]
    assert recommender.NO_EVIDENCE_CODES is matcher.NO_EVIDENCE_CODES
