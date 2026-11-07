"""R5: 적용되는 필수 기준이 없는 시험을 확정 적격으로 추천하지 않음 (합성 데이터, LLM·API 호출 없음).

실제 Session을 지나 Matcher 판정, 코호트 PairStatus, 추천, 검토 행동, 외부 표시를 층별로 확인합니다.
실질 기준 = 이 환자에게 적용되는 절차 외 기준. 절차 기준의 PASS와 조건부 기준의 '해당 없음'은 적격 근거가 아닙니다.
코호트(OR)는 기준 개수가 아니라 실제로 적용되는 유효 경로로 판단합니다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import test_session_fact_shadow as tsf  # noqa: E402  (Session 고정 대역)
from document_support import attest_declared  # noqa: E402

from ctrec import config, fact_compare, llm, qa_guard, question_eval as qe, trial_store  # noqa: E402
from ctrec.agents import matcher, parser_validator as pv  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.decision_state import SessionView, build_state  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy  # noqa: E402
from ctrec.eval_hygiene import AliasMap  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.schemas import CriterionAssessment, LLMAssessment, MatcherOutput, ParsedTrial, QAPair, Rule, TrialMatch  # noqa: E402
from ctrec.status import external_status, label  # noqa: E402
from ctrec.web import display  # noqa: E402

ALIAS = AliasMap(salt="r5-tests").alias("SYN-R5")
RAW = "Synthetic 62-year-old female. ECOG 1. Able to give informed consent. EGFR mutation positive."
SOURCE = "Inclusion Criteria:\n- Histologically confirmed NSCLC with measurable disease\n"
CONSENT = "Able to give informed consent"
VISITS = "Willing to attend study visits"


def trial(tid: str, source: str = SOURCE) -> dict:
    return {"trial_id": tid, "title": "synthetic", "conditions": ["NSCLC"], "phases": ["PHASE2"], "interventions": [],
            "overall_status": "RECRUITING", "eligibility_criteria": source, "sex": "ALL", "minimum_age": None,
            "maximum_age": None, "healthy_volunteers": False, "brief_summary": "", "detailed_description": ""}


def parsed(tid: str, rules=(), cohorts=()) -> ParsedTrial:
    return ParsedTrial(trial_id=tid, title="synthetic", target_population="", intervention_summary="",
                       key_conditions=[], cohorts=list(cohorts), rules=list(rules), parsing_notes="stub")


def rule(rid: str, text: str, kind="inclusion", category="consent_or_logistics", **k) -> Rule:
    base = dict(rule_id=rid, kind=kind, text=text, category=category, field=None, operator=None, value=None, unit=None,
                time_window_days=None, structured_evaluable=False, underspecified=False, cohort=None)
    return Rule(**{**base, **k})


def age(rid: str, value="19", **k) -> Rule:
    """환자는 62세: 19 이상은 충족, 70 이상은 불충족."""
    return rule(rid, f"Age >= {value} years", category="demographic", field="age", operator=">=", value=value,
                unit="years", structured_evaluable=True, **k)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PARSED_DIR", tmp_path / "parsed")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DEMO_MODE", True)
    with qe.OfflineEnvironment(1) as e:
        real = e.stub.structured
        e.answers = {}  # rule_id -> Matcher LLM 판정. 없으면 오프라인 대역의 '판정 불가'

        def structured(model_cls, *, system, user, **kw):
            if model_cls is ParsedTrial:
                return parsed(user.split("trial_id: ")[1].split("\n")[0])
            out = real(model_cls, system=system, user=user, **kw)
            if model_cls is MatcherOutput:
                out.assessments = [e.answers.get(a.rule_id, a) for a in out.assessments]
            return out

        llm.structured = structured  # OfflineEnvironment.__exit__가 원본으로 복원 (monkeypatch를 쓰면 대역이 남음)
        yield e


class Result:
    """한 시험의 층별 결과."""

    def __init__(self, tid: str, s: Session, verified: bool = False, forged: bool = False):
        if verified:
            attest_declared(s.fact_store, ALIAS)
        if forged:  # 서버 증명 없이 상태 문자열만 바꾼 경우: 문서 확인으로 치지 않음
            for f in s.fact_store.facts(ALIAS):
                s.fact_store.set_verification(f.fact_id, "document_verified", reason="증명 없음", by="test",
                                              document_reference="synthetic-doc")
        self.session = s
        self.match = s.matches[tid]
        self.rec = s.recommendation
        view = SessionView.from_session(s)
        view.matched_versions = {t: view.snapshot.version for t in s.matches}
        co = CohortState.from_decision_states({ALIAS: build_state(view)})
        self.pair = co.pair(ALIAS, tid).status
        self.actions = {(a.action_type, a.reason_code) for a in LegacyAdapterPolicy().propose(co)
                        if a.action_type != "FINALIZE"}
        self.ranked = [r.trial_id for r in self.rec.ranked]
        self.excluded = {x.trial_id: x.reason for x in self.rec.excluded}


def run(tid: str, rules=(), cohorts=(), source: str = SOURCE, input_source="clinical_record", verified=False,
        forged=False) -> Result:
    t = trial(tid, source)
    trial_store.save(t, parsed(tid, rules, cohorts), {}, pv.ValidationReport())
    s = Session(case=PatientCase(ALIAS, RAW, input_source=input_source, candidate_trials=[tid]),
                trials={tid: t}, answerer=tsf._Answerer(), tracer=tsf._Tracer())
    s.profile_patient()
    s.match()
    s.finalize()
    return Result(tid, s, verified, forged)


def assert_not_evaluated(r: Result, tid: str, code: str) -> None:
    assert (r.match.eligibility, r.match.match_status) == ("NOT_EVALUATED", "NOT_EVALUATED")
    assert r.match.evaluation_error == code
    assert r.pair == C.PairStatus.NOT_EVALUATED
    assert r.ranked == [] and r.rec.recommended == []
    assert "부적격 아님" in r.excluded[tid]
    assert label(r.match.match_status) != label("CRITERIA_MATCH")
    assert ("CLINICIAN_REVIEW", "evaluation_failed" if code not in matcher.NO_EVIDENCE_CODES else code) in r.actions


def assert_confirmed(r: Result, tid: str) -> None:
    assert (r.match.eligibility, r.match.match_status) == ("ELIGIBLE", "CRITERIA_MATCH")
    assert r.match.evaluation_error is None
    assert r.pair == C.PairStatus.CONFIRMED_ELIGIBLE
    assert r.ranked == [tid]


def said(rule_id: str, quote: str, holds="yes", applicable=True) -> LLMAssessment:
    return LLMAssessment(rule_id=rule_id, applicable=applicable, holds=holds, evidence_type="explicit", confidence="high",
                         evidence=quote, reasoning="scripted", missing_info=None)


FOR_MEN = "Men who can father a child must use birth control"
NOT_A_MAN = "62-year-old female"  # 대상 집단이 아님이 인용으로 직접 드러나야 해당 없음이 인정됨


def conditional(rid: str, **k) -> Rule:
    return rule(rid, FOR_MEN, category="reproductive", **k)


# ------------------------------------------------------------ 빈 기준 (PR #17)
@pytest.mark.parametrize("tid, source, code", [("SYN-R1", SOURCE, "criteria_parse_empty"),
                                               ("SYN-R3", "", "no_source_criteria")])
def test_empty_rules_stay_blocked(env, tid, source, code):
    assert_not_evaluated(run(tid, source=source), tid, code)


def test_lazy_compile_empty_rules_stay_blocked(env, monkeypatch):
    monkeypatch.setattr(config, "LAZY_COMPILE", True)
    t = trial("SYN-R2")
    s = Session(case=PatientCase(ALIAS, RAW, input_source="clinical_record", candidate_trials=["SYN-R2"]),
                trials={"SYN-R2": t}, answerer=tsf._Answerer(), tracer=tsf._Tracer())
    s.profile_patient()
    s.match()
    s.finalize()
    assert_not_evaluated(Result("SYN-R2", s), "SYN-R2", "criteria_parse_empty")


# ------------------------------------------------------------ R5
@pytest.mark.parametrize("rules", [[rule("I1", CONSENT)], [rule("I1", CONSENT), rule("I2", VISITS)]],
                         ids=["one", "two"])
def test_logistics_only_trial_is_not_a_confirmed_recommendation(env, rules):
    assert_not_evaluated(run("SYN-R5", rules), "SYN-R5", "no_required_criteria")


def test_logistics_only_is_not_ineligible(env):
    r = run("SYN-R5", [rule("I1", CONSENT)])
    assert r.match.eligibility != "INELIGIBLE" and r.pair != C.PairStatus.CONFIRMED_INELIGIBLE


def test_no_required_is_not_replaced_by_other_open_states(env):
    assert run("SYN-R5", [rule("I1", CONSENT)]).pair not in (C.PairStatus.INFO_MISSING, C.PairStatus.BLOCKED_FAILURE)


def test_logistics_item_stays_visible_on_the_result(env):
    """절차 기준 미확인을 없는 것으로 치지 않음: 판정 기록에 남습니다."""
    r = run("SYN-R5", [rule("I1", CONSENT)])
    assert [a.rule_id for a in r.match.assessments] == ["I1"] and r.match.assessments[0].passes is None


@pytest.mark.parametrize("rules, answers, code, words", [
    ([rule("I1", CONSENT)], {}, "no_required_criteria", "절차 기준뿐"),
    ([conditional("I1")], {"I1": said("I1", NOT_A_MAN, holds="no", applicable=False)}, "no_applicable_criteria", "적용되는 기준이 없어"),
], ids=["logistics_only", "all_not_applicable"])
def test_result_copy_does_not_call_it_an_evaluation_failure(env, rules, answers, code, words):
    """#19의 문구 구분 유지: 기준을 평가한 시험을 처리 오류처럼 '평가 실패'로 쓰지 않음. 코드는 그대로 남음."""
    env.answers.update(answers)
    out = display.redact_errors(run("SYN-TXT", rules).session.result())
    m, ex = out["matches"]["SYN-TXT"], out["recommendation"]["excluded"][0]
    assert m["evaluation_error"] == code and words in m["summary"] and words in ex["reason"]
    assert "평가 실패" not in m["summary"] and "평가 실패" not in ex["reason"] and "부적격 아님" in ex["reason"]


# ------------------------------------------------------------ 절차 기준 PASS, 조건부 전체 N/A
def test_logistics_pass_alone_is_not_confirmed(env):
    env.answers["I1"] = said("I1", "Able to give informed consent")
    r = run("SYN-PASS", [rule("I1", CONSENT)])
    assert r.match.assessments[0].passes is True  # 기준 단위 판정은 그대로, 시험 단위 확정만 막음
    assert_not_evaluated(r, "SYN-PASS", "no_required_criteria")


def test_logistics_exclusion_pass_alone_is_not_confirmed(env):
    env.answers["E1"] = said("E1", "Able to give informed consent", holds="no")
    r = run("SYN-PASSX", [rule("E1", "Participating in another interventional trial", kind="exclusion")])
    assert r.match.assessments[0].passes is True
    assert_not_evaluated(r, "SYN-PASSX", "no_required_criteria")


def test_every_conditional_not_applicable_is_not_confirmed(env):
    env.answers["I1"] = said("I1", NOT_A_MAN, holds="no", applicable=False)
    r = run("SYN-NA", [conditional("I1")])
    assert r.match.assessments[0].applicable is False
    assert_not_evaluated(r, "SYN-NA", "no_applicable_criteria")


def test_not_applicable_plus_logistics_pass_is_not_confirmed(env):
    env.answers.update(I1=said("I1", NOT_A_MAN, holds="no", applicable=False), I2=said("I2", "Able to give informed consent"))
    assert_not_evaluated(run("SYN-NA2", [conditional("I1"), rule("I2", CONSENT)]), "SYN-NA2", "no_applicable_criteria")


def test_not_applicable_with_a_met_criterion_stays_confirmed(env):
    env.answers["I2"] = said("I2", NOT_A_MAN, holds="no", applicable=False)
    assert_confirmed(run("SYN-NA3", [age("I1"), conditional("I2")]), "SYN-NA3")


def test_logistics_failure_is_a_real_failure_not_missing_evidence(env):
    """불충족 근거가 있으면 기존 판정을 따름: 근거 없음과 구분합니다."""
    env.answers["I1"] = said("I1", "Able to give informed consent", holds="no")
    r = run("SYN-FAIL", [rule("I1", CONSENT)])
    assert (r.match.eligibility, r.match.match_status, r.match.evaluation_error) == ("INELIGIBLE", "CRITERIA_MISMATCH", None)
    assert r.ranked == []


# ------------------------------------------------------------ 정상 대조군
def test_r7_clinical_met_logistics_open_is_unchanged(env):
    assert_confirmed(run("SYN-R7", [age("I1"), rule("I2", CONSENT)]), "SYN-R7")


def test_logistics_exclusion_unknown_still_asks(env):
    r = run("SYN-R6", [rule("E1", "Participating in another interventional trial", kind="exclusion")])
    assert (r.match.eligibility, r.pair) == ("UNCERTAIN", C.PairStatus.INFO_MISSING)
    assert ("ASK_PATIENT", "missing_required_info") in r.actions


def test_required_criterion_with_missing_information_still_asks(env):
    r = run("SYN-R8", [rule("I1", "Adequate renal function", category="lab")])
    assert (r.match.eligibility, r.match.match_status) == ("UNCERTAIN", "INSUFFICIENT_INFORMATION")
    assert r.pair == C.PairStatus.INFO_MISSING and ("ASK_PATIENT", "missing_required_info") in r.actions


def test_clinician_only_criterion_stays_with_the_clinician(env):
    r = run("SYN-R9", [rule("I1", "Adequate organ function", category="lab", underspecified=True)])
    assert (r.match.eligibility, r.match.match_status) == ("UNCERTAIN", "NEEDS_REVIEW")
    assert r.pair == C.PairStatus.EXPERT_REVIEW
    assert any(a[0] == "CLINICIAN_REVIEW" for a in r.actions)


def test_document_required_criterion_stays_unverified(env):
    env.answers["I1"] = said("I1", "EGFR mutation positive")
    r = run("SYN-DOC", [rule("I1", "EGFR mutation positive", category="biomarker")], input_source="self_report")
    assert (r.match.eligibility, r.match.match_status) == ("UNCERTAIN", "POTENTIAL_MATCH")
    assert r.match.assessments[0].needs_verification and r.pair == C.PairStatus.VERIFICATION_PENDING
    assert any(a[0] == "REQUEST_DOCUMENT" for a in r.actions) and r.ranked == ["SYN-DOC"]


def test_document_verified_ineligibility_stays_confirmed(env):
    r = run("SYN-CI", [age("I1", "70")], verified=True)
    assert (r.match.eligibility, r.match.match_status) == ("INELIGIBLE", "CRITERIA_MISMATCH")
    assert r.pair == C.PairStatus.CONFIRMED_INELIGIBLE and r.ranked == []


def test_status_string_alone_does_not_confirm_ineligibility(env):
    r = run("SYN-CI", [age("I1", "70")], forged=True)
    assert r.match.eligibility == "INELIGIBLE" and r.pair == C.PairStatus.VERIFICATION_PENDING


@pytest.mark.parametrize("rules", [[age("I1", "70")], [age("I1", "70"), rule("I2", CONSENT)]], ids=["alone", "logistics"])
def test_ineligible_is_not_promoted(env, rules):
    r = run("SYN-R10", rules)
    assert (r.match.eligibility, r.match.match_status) == ("INELIGIBLE", "CRITERIA_MISMATCH")
    assert r.ranked == [] and r.pair == C.PairStatus.VERIFICATION_PENDING


# ------------------------------------------------------------ OR 구조: 개수가 아니라 유효 경로
@pytest.mark.parametrize("rules, cohorts", [
    ([rule("A1", CONSENT, cohort="A"), age("B1", cohort="B")], ["A", "B"]),
    ([age("B1", cohort="B"), rule("A1", CONSENT, cohort="A")], ["B", "A"]),
], ids=["logistics_declared_first", "clinical_declared_first"])
def test_valid_path_wins_regardless_of_declaration_order(env, rules, cohorts):
    r = run("SYN-OR1", rules, cohorts)
    assert_confirmed(r, "SYN-OR1")
    assert r.match.matched_cohort == "B"


def test_open_path_keeps_the_question_flow(env):
    r = run("SYN-OR2", [rule("A1", CONSENT, cohort="A"), rule("B1", "Adequate renal function", category="lab", cohort="B")],
            ["A", "B"])
    assert (r.match.eligibility, r.match.matched_cohort) == ("UNCERTAIN", "B")
    assert r.pair == C.PairStatus.INFO_MISSING and ("ASK_PATIENT", "missing_required_info") in r.actions


def test_failed_path_does_not_hide_behind_a_logistics_only_path(env):
    r = run("SYN-OR3", [rule("A1", CONSENT, cohort="A"), age("B1", "70", cohort="B")], ["A", "B"])
    assert_not_evaluated(r, "SYN-OR3", "no_required_criteria")


def test_common_required_criterion_counts_for_every_cohort(env):
    r = run("SYN-OR4", [age("I1"), rule("A1", CONSENT, cohort="A"), rule("B1", VISITS, cohort="B")], ["A", "B"])
    assert_confirmed(r, "SYN-OR4")


def test_ordinary_or_structure_is_unchanged(env):
    r = run("SYN-OR5", [age("A1", cohort="A"), age("B1", "70", cohort="B")], ["A", "B"])
    assert_confirmed(r, "SYN-OR5")
    assert r.match.matched_cohort == "A"


@pytest.mark.parametrize("rules", [
    [rule("A1", CONSENT, cohort="A"), age("B1", cohort="B")],
    [rule("A1", CONSENT, cohort="A"), rule("B1", "Adequate renal function", category="lab", cohort="B")],
    [rule("A1", CONSENT, cohort="A"), age("B1", "70", cohort="B")],
    [age("I1"), rule("A1", CONSENT, cohort="A"), rule("B1", VISITS, cohort="B")],
    [age("A1", cohort="A"), age("B1", "70", cohort="B")],
], ids=["OR1", "OR2", "OR3", "OR4", "OR5"])
def test_declaration_order_does_not_change_the_verdict(env, rules):
    def verdict(cohorts):
        r = run("SYN-ORD", rules, cohorts)
        return r.match.eligibility, r.match.match_status, r.pair, r.ranked, r.actions
    assert verdict(["A", "B"]) == verdict(["B", "A"])


# ------------------------------------------------------------ 재계산 경로 (match_trial을 거치지 않음)
def test_rebuild_paths_do_not_confirm_without_substantive_criteria():
    only = parsed("SYN-RB", [rule("I1", CONSENT)])
    passed = assessment(holds="yes", passes=True, confidence="high", evidence="consent")
    rebuilt = fact_compare._rebuild("P", only, [passed])
    assert rebuilt.eligibility == "UNCERTAIN"
    was_eligible = rebuilt.model_copy(update={"eligibility": "ELIGIBLE"})
    again = qa_guard.recompose(was_eligible, only, {"I1": passed})  # match_trial과 같은 규칙
    assert (again.eligibility, again.match_status, again.evaluation_error) == (
        "NOT_EVALUATED", "NOT_EVALUATED", "no_required_criteria")


# ------------------------------------------------------------ Q&A 경계·계약 평가기: NOT_EVALUATED라고 건너뛰지 않음
def judged(rules, *rows, cohorts=()) -> tuple[ParsedTrial, TrialMatch]:
    """기준별 판정이 있는 NOT_EVALUATED 시험 (matcher를 거친 것과 같은 모양)."""
    p, all_rows = parsed("SYN-G", rules, cohorts), list(rows)
    chosen, cohort, results = matcher._select_cohort(all_rows, list(cohorts))
    code = matcher.no_evidence_code(chosen)
    assert code is not None
    return p, TrialMatch(patient_id="P", trial_id="SYN-G", eligibility="NOT_EVALUATED", assessments=chosen, n_pass=0,
                         n_fail=0, n_unknown=0, summary="s", matched_cohort=cohort, cohort_results=results,
                         evaluation_error=code)


ANSWER = "환자\n\n[추가 확인 정보 - 라운드 1]\nQ: 동의 가능하신가요?\nA: 네, 동의할 수 있어요"
FROM_ANSWER = dict(holds="yes", passes=True, confidence="high", evidence="네, 동의할 수 있어요", evidence_source="self_report")


def test_guard_still_reaches_a_not_evaluated_trial_with_assessments():
    """계약 없는 답변에 기댄 절차 PASS는 NOT_EVALUATED여도 되돌림: 확인할 항목이 생겼으므로 평가 실패가 아님."""
    p, m = judged([rule("I1", CONSENT)], assessment(**FROM_ANSWER))
    g = qa_guard.guard_match(m, p, ANSWER)
    a = g.assessments[0]
    assert a.passes is None and a.needs_verification and qa_guard.FLAG in a.validation_flags
    assert (g.eligibility, g.evaluation_error) == ("UNCERTAIN", None) and g.match_status == external_status(g)


def test_guard_without_a_dependent_answer_leaves_the_trial_as_it_was():
    p, m = judged([rule("I1", CONSENT)], assessment(holds="yes", passes=True, confidence="high", evidence="age: 62"))
    assert qa_guard.guard_match(m, p, "환자") is m


def test_recompose_keeps_the_gate_when_a_replacement_adds_no_substantive_criterion():
    """평가기가 절차 기준을 정해도 실질 기준은 생기지 않음: UNCERTAIN(행동 없는 쌍)으로 풀리지 않음."""
    p, m = judged([rule("I1", CONSENT)], assessment())
    done = assessment(holds="yes", passes=True, confidence="high", evidence="contract", method="contract")
    g = qa_guard.recompose(m, p, {"I1": done})
    assert (g.eligibility, g.evaluation_error) == ("NOT_EVALUATED", "no_required_criteria") and g.n_pass == 1


def test_real_evaluation_failures_are_still_skipped():
    failed = TrialMatch(patient_id="P", trial_id="SYN-G", eligibility="NOT_EVALUATED", assessments=[], n_pass=0, n_fail=0,
                        n_unknown=0, summary="s", evaluation_error="적격성 판정 실패: RuntimeError()")
    p = parsed("SYN-G", [rule("I1", CONSENT)])
    assert qa_guard.guard_match(failed, p, ANSWER) is failed
    assert qa_guard.recompose(failed, p, {"I1": assessment()}) is failed


def test_contract_session_guard_reaches_it_too():
    """ContractSession._contract_and_guard도 같은 기준으로 건너뜀 (CTREC_SESSION=contract 경로)."""
    p, m = judged([rule("I1", CONSENT)], assessment(**FROM_ANSWER))
    fake = SimpleNamespace(parsed={"SYN-G": p}, initial_text="환자", contract_provenance={}, rounds=1, tracer=tsf._Tracer(),
                           qa_log=[QAPair(round=1, question_id="Q1", question="동의 가능하신가요?", answer="네, 동의할 수 있어요")],
                           _registry=SimpleNamespace(active_irs=lambda tid: {}))
    g = ContractSession._contract_and_guard(fake, m)
    assert g.assessments[0].needs_verification and (g.eligibility, g.evaluation_error) == ("UNCERTAIN", None)


# ------------------------------------------------------------ 판정 함수 경계
def assessment(**k) -> CriterionAssessment:
    base = dict(rule_id="I1", kind="inclusion", criterion_text="x", holds="unknown", passes=None, confidence="low",
                evidence="근거 없음", reasoning="r", missing_info="", method="llm", category="consent_or_logistics")
    return CriterionAssessment(**{**base, **k})


def test_decide_never_returns_eligible_for_waivable_only_paths():
    assert matcher.decide([]) == "UNCERTAIN"
    assert matcher.decide([assessment()]) == "UNCERTAIN"
    assert matcher.decide([assessment(), assessment(rule_id="I2")]) == "UNCERTAIN"


def test_decide_needs_a_substantive_criterion():
    passed = dict(holds="yes", passes=True, confidence="high", evidence="age: 62")
    assert matcher.decide([assessment(**passed)]) == "UNCERTAIN"
    assert matcher.decide([assessment(**passed, category="lab", applicable=False)]) == "UNCERTAIN"
    assert matcher.decide([assessment(**passed, category="lab")]) == "ELIGIBLE"


@pytest.mark.parametrize("rows, code", [
    ([dict(passes=None)], "no_required_criteria"),
    ([dict(passes=True, holds="yes")], "no_required_criteria"),
    ([dict(passes=True, holds="yes", category="lab", applicable=False)], "no_applicable_criteria"),
    ([dict(passes=True, holds="yes", category="lab")], None),
    ([dict(passes=False, holds="no")], None),
    ([dict(passes=None, kind="exclusion")], None),
    ([dict(passes=None, underspecified=True)], None),
], ids=["logistics_open", "logistics_pass", "all_na", "substantive", "failed", "blocking", "clinician"])
def test_no_evidence_code_separates_the_cases(rows, code):
    assert matcher.no_evidence_code([assessment(**r) for r in rows]) == code


@pytest.fixture(autouse=True, scope="module")
def _llm_restored():
    original = llm.structured
    yield
    assert llm.structured is original
