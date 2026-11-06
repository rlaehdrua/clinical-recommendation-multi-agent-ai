"""E-1: 평가할 기준이 없는 시험이 근거 없이 CONFIRMED_ELIGIBLE이 되어 추천되지 않음 (합성 데이터, LLM·API 호출 없음).

docs/dev/e1_empty_criteria_safety.md. 절차 기준만 남은 시험(R5)은 E-1 범위 밖이라 현재 동작을 층별로 기록만 합니다.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import test_session_fact_shadow as tsf  # noqa: E402  (Session 고정 대역)

from ctrec import config, fact_compare, llm, question_eval as qe, trial_store  # noqa: E402
from ctrec.agents import matcher, parser_validator as pv  # noqa: E402
from ctrec.decision_state import SessionView, build_state  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy  # noqa: E402
from ctrec.eval_hygiene import AliasMap  # noqa: E402
from ctrec.fact_store import FactStore  # noqa: E402
from ctrec.fact_view import FactEvidenceView  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.schemas import CriterionAssessment, ParsedTrial, Rule  # noqa: E402
from ctrec.web import display  # noqa: E402

ALIAS = AliasMap(salt="e1-tests").alias("SYN-E1")
RAW = "Synthetic 62-year-old male. ECOG 1."
SOURCE = ("Inclusion Criteria:\n- Histologically confirmed NSCLC with measurable disease\n"
          "- Adequate organ function within 14 days\n\nExclusion Criteria:\n- Active brain metastases requiring steroids\n")
NEVER = (C.PairStatus.CONFIRMED_ELIGIBLE, C.PairStatus.CONFIRMED_INELIGIBLE)


def trial(tid, criteria=SOURCE, **kw):
    return {"trial_id": tid, "title": "synthetic", "conditions": ["NSCLC"], "phases": ["PHASE2"], "interventions": [],
            "overall_status": "RECRUITING", "eligibility_criteria": criteria, "sex": "ALL", "minimum_age": None,
            "maximum_age": None, "healthy_volunteers": False, "brief_summary": "", "detailed_description": "", **kw}


def parsed(tid, rules=()):
    return ParsedTrial(trial_id=tid, title="synthetic", target_population="", intervention_summary="", key_conditions=[],
                       cohorts=[], rules=list(rules), parsing_notes="")


def rule(rid, text, kind="inclusion", category="consent_or_logistics", **kw):
    base = dict(rule_id=rid, kind=kind, text=text, category=category, field=None, operator=None, value=None, unit=None,
                time_window_days=None, structured_evaluable=False, underspecified=False, cohort=None)
    return Rule(**{**base, **kw})


AGE_RULE = rule("I1", "Age >= 19 years", category="demographic", field="age", operator=">=", value="19", unit="years",
                structured_evaluable=True)
CONSENT = rule("I2", "Able to give informed consent")


def profile():
    return qe.stub_profiler(ALIAS, RAW)


def assessment(passes):
    return CriterionAssessment(rule_id="I1", kind="inclusion", criterion_text="x", holds="yes", passes=passes,
                               confidence="high", evidence="age: 62", reasoning="r", missing_info="",
                               method="rule_engine", category="demographic")


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """오프라인 대역 + 임시 저장소. 파서는 빈 규칙을 돌려주고, 그 밖의 LLM 요청은 오프라인 대역이 처리."""
    monkeypatch.setattr(config, "PARSED_DIR", tmp_path / "parsed")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DEMO_MODE", True)
    with qe.OfflineEnvironment(1) as e:
        real = e.stub.structured

        def structured(model_cls, *, system, user, **kw):
            if model_cls is ParsedTrial:
                return parsed(user.split("trial_id: ")[1].split("\n")[0])
            return real(model_cls, system=system, user=user, **kw)

        # monkeypatch를 쓰면 안 됨: with가 끝난 뒤 되돌리므로 오프라인 대역이 전역에 남아 비용·미터링 테스트를 우회함
        llm.structured = structured  # OfflineEnvironment.__exit__가 원본으로 복원
        yield e


@pytest.fixture(autouse=True, scope="module")
def _llm_restored():
    """이 파일의 대역이 다음 테스트 파일로 새지 않음."""
    original = llm.structured
    yield
    assert llm.structured is original


def run_session(t, **kw) -> Session:
    s = Session(case=PatientCase(ALIAS, RAW, input_source="clinical_record", candidate_trials=[t["trial_id"]]),
                trials={t["trial_id"]: t}, answerer=tsf._Answerer(), tracer=tsf._Tracer(), **kw)
    s.profile_patient()
    s.match()
    s.finalize()
    return s


def cohort_of(s: Session) -> CohortState:
    view = SessionView.from_session(s)
    view.matched_versions = {t: view.snapshot.version for t in s.matches}
    return CohortState.from_decision_states({ALIAS: build_state(view)})


def store(t, p):
    """사전 컴파일과 같은 저장 경로 (LLM 없이 결정적 검사만)."""
    p2, meta, report = pv.validate(p, t, use_llm=False)
    trial_store.save(t, p2, meta, report)
    return report


def assert_blocked(s: Session, t, code):
    tid = t["trial_id"]
    m = s.matches[tid]
    assert m.eligibility == "NOT_EVALUATED" and m.match_status == "NOT_EVALUATED"
    assert m.evaluation_error == code
    pair = cohort_of(s).pair(ALIAS, tid)
    assert pair.status == C.PairStatus.NOT_EVALUATED and pair.status not in NEVER
    assert s.recommendation.ranked == [] and s.recommendation.recommended == []  # 추천 1순위로 나가지 않음
    assert [x.trial_id for x in s.recommendation.excluded] == [tid]
    assert "부적격 아님" in s.recommendation.excluded[0].reason


# ------------------------------------------------------------ decide / match_trial
def test_decide_empty_is_uncertain_not_eligible_or_ineligible():
    assert matcher.decide([]) == "UNCERTAIN"


def test_decide_keeps_existing_outcomes():
    assert matcher.decide([assessment(True)]) == "ELIGIBLE"
    assert matcher.decide([assessment(False)]) == "INELIGIBLE"
    assert matcher.decide([assessment(None)]) == "UNCERTAIN"


@pytest.mark.parametrize("t,code", [
    (trial("T1"), "criteria_parse_empty"),
    (trial("T1", minimum_age="18 Years"), "criteria_parse_empty"),
    (trial("T1", criteria="", maximum_age="75 Years"), "criteria_parse_empty"),  # 파서는 구조화 나이도 규칙으로 만듦
    (trial("T1", criteria="", sex="FEMALE"), "criteria_parse_empty"),
    (trial("T1", criteria=""), "no_source_criteria"),
    (trial("T1", criteria="  \n "), "no_source_criteria"),
    (trial("T1", criteria=None, sex=None), "no_source_criteria"),
])
def test_empty_rules_are_not_evaluated_with_cause_code(t, code, monkeypatch):
    def no_llm(*a, **k):
        raise AssertionError("규칙이 없으면 LLM을 부르지 않음")

    monkeypatch.setattr(matcher.llm, "structured", no_llm)
    m = matcher.match_trial(profile(), parsed("T1"), t)
    assert (m.eligibility, m.match_status, m.evaluation_error) == ("NOT_EVALUATED", "NOT_EVALUATED", code)
    assert m.assessments == [] and (m.n_pass, m.n_fail, m.n_unknown) == (0, 0, 0)


def test_evaluation_error_and_summary_carry_no_source_text():
    secret = "SYNTHETIC-CRITERION-TEXT-MARKER"
    m = matcher.match_trial(profile(), parsed("T1"), trial("T1", criteria=secret))
    assert secret not in m.model_dump_json() and "62" not in m.summary


def test_trial_with_rules_is_evaluated_as_before():
    m = matcher.match_trial(profile(), parsed("T1", [AGE_RULE]), trial("T1"))
    assert m.eligibility == "ELIGIBLE" and m.evaluation_error is None and m.n_pass == 1


# ------------------------------------------------------------ 실제 Session (R1~R3)
def test_r1_parser_empty_through_compile_path_is_blocked(env):
    t = trial("SYN-R1")
    report = store(t, parsed("SYN-R1"))
    assert report.coverage_gaps  # 검증기는 누락을 감지하지만 저장을 막지 않음 (이번 수정 범위 밖)
    assert_blocked(run_session(t), t, "criteria_parse_empty")


def test_r2_lazy_compile_path_is_blocked(env, monkeypatch):
    monkeypatch.setattr(config, "LAZY_COMPILE", True)
    t = trial("SYN-R2")
    assert trial_store.read_record("SYN-R2") is None  # 저장소를 거치지 않는 경로
    s = run_session(t)
    assert s.parsed["SYN-R2"].rules == []
    assert_blocked(s, t, "criteria_parse_empty")


def test_r3_no_source_criteria_is_blocked(env):
    t = trial("SYN-R3", criteria="")
    assert store(t, parsed("SYN-R3")).coverage_gaps == []
    assert_blocked(run_session(t), t, "no_source_criteria")


def test_web_display_shows_cause_code_only(env):
    t = trial("SYN-R1")
    store(t, parsed("SYN-R1"))
    m = display.redact_errors(run_session(t).result())["matches"]["SYN-R1"]
    assert m["evaluation_error"] == "criteria_parse_empty"
    assert m["summary"] == "NOT_EVALUATED: 기준 해석 결과가 비어 판정하지 못함(부적격 아님) - criteria_parse_empty"


def test_clinician_review_uses_the_existing_not_evaluated_path(env):
    t = trial("SYN-R1")
    store(t, parsed("SYN-R1"))
    co = cohort_of(run_session(t))
    assert co.patients[ALIAS].legacy_copy().not_evaluated_trials == ["SYN-R1"]
    review = [a for a in LegacyAdapterPolicy().propose(co) if a.action_type == "CLINICIAN_REVIEW"]
    assert [(a.reason_code, a.target_trial_ids) for a in review] == [("evaluation_failed", ("SYN-R1",))]


@pytest.mark.parametrize("mode", ["deterministic", "llm"])
def test_fact_compare_handles_empty_criteria(env, mode):
    t = trial("SYN-R1")
    store(t, parsed("SYN-R1"))
    s = run_session(t, fact_compare=mode)
    assert s.fact_comparison["status"] == "ok"
    row = s.fact_comparison["trials"][0]
    assert row["status"] == "not_evaluated" and row["criteria"] == []
    assert row["baseline"] == {"eligibility": "NOT_EVALUATED", "match_status": "NOT_EVALUATED"}


def test_fact_compare_rebuild_on_empty_rules_does_not_confirm(env):
    """Session을 거치지 않고 빈 기준을 직접 넘겨도 사실 기반 재계산(decide)이 ELIGIBLE을 만들지 않음."""
    view = FactEvidenceView(FactStore(), ALIAS)
    assert fact_compare.evaluate_trial(view, parsed("T1"), trial("T1"), "2026-10-01", "deterministic")[2] is None
    _, _, m = fact_compare.evaluate_trial(view, parsed("T1"), trial("T1"), "2026-10-01", "llm")
    assert m.eligibility == "UNCERTAIN" and m.match_status != "CRITERIA_MATCH"


def test_empty_criteria_is_never_ineligible(env):
    for t, code in ((trial("A"), "criteria_parse_empty"), (trial("B", criteria=""), "no_source_criteria")):
        m = matcher.match_trial(profile(), parsed(t["trial_id"]), t)
        assert m.eligibility != "INELIGIBLE" and m.match_status != "CRITERIA_MISMATCH" and m.evaluation_error == code


# ------------------------------------------------------------ 대조군 (정상 판정 유지)
def test_r7_clinical_met_with_logistics_unknown_stays_eligible(env):
    """B안 정책 미결정: 현재 동작 그대로 유지되는지만 고정."""
    t = trial("SYN-R7")
    trial_store.save(t, parsed("SYN-R7", [AGE_RULE, CONSENT]), {}, pv.ValidationReport())
    s = run_session(t)
    m = s.matches["SYN-R7"]
    assert (m.eligibility, m.match_status, m.evaluation_error) == ("ELIGIBLE", "CRITERIA_MATCH", None)
    assert cohort_of(s).pair(ALIAS, "SYN-R7").status == C.PairStatus.CONFIRMED_ELIGIBLE
    assert [r.trial_id for r in s.recommendation.ranked] == ["SYN-R7"]


def test_logistics_exclusion_unknown_still_blocks(env):
    t = trial("SYN-R6")
    trial_store.save(t, parsed("SYN-R6", [rule("E1", "Participating in another interventional trial", kind="exclusion")]),
                     {}, pv.ValidationReport())
    assert run_session(t).matches["SYN-R6"].eligibility == "UNCERTAIN"


def logistics_only_session() -> Session:
    t = trial("SYN-R5")
    trial_store.save(t, parsed("SYN-R5", [CONSENT]), {}, pv.ValidationReport())
    return run_session(t)


def test_known_risk_r5_session_layer_stays_eligible_and_ranked(env):
    """알려진 잔여 위험 (R5): 규칙이 있으므로 E-1의 차단 대상이 아니고, Session 판정과 추천은 그대로입니다.

    바람직한 동작을 고정하는 테스트가 아닙니다. 팀 결정으로 동작을 바꾸면 이 테스트도 함께 바꿉니다."""
    s = logistics_only_session()
    m = s.matches["SYN-R5"]
    assert (m.eligibility, m.match_status, m.n_pass, m.evaluation_error) == ("ELIGIBLE", "CRITERIA_MATCH", 0, None)
    assert [r.trial_id for r in s.recommendation.ranked] == ["SYN-R5"]


def test_known_risk_r5_pair_status_belongs_to_the_contract_layer(env):
    """R5의 PairStatus는 Session 판정과 다른 층: PR #14의 필수 기준 0개 차단이 있으면 NOT_EVALUATED.

    PR #14 병합 뒤에는 분기를 없애고 NOT_EVALUATED만 남깁니다."""
    has_gate = "required_criteria" in inspect.signature(C.derive_pair_status).parameters
    expected = C.PairStatus.NOT_EVALUATED if has_gate else C.PairStatus.CONFIRMED_ELIGIBLE
    assert cohort_of(logistics_only_session()).pair(ALIAS, "SYN-R5").status == expected


# ------------------------------------------------------------ 잠긴 기준표
def _frozen_records():
    root = Path(__file__).resolve().parents[1] / "data" / "parsed"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.glob("*.json")) if p.name != "FROZEN.json"]


def test_every_frozen_trial_path_has_substantive_criteria():
    """잠긴 기준표는 이 수정의 영향을 받지 않음: 모든 평가 경로에 절차 외 기준이 있음."""
    records = _frozen_records()
    assert records
    for rec in records:
        p = rec["parsed"]
        rules = p["rules"]
        labels = {r["cohort"] for r in rules if r.get("cohort")}
        paths = [[r for r in rules if r.get("cohort") in (None, c)] for c in labels] or [rules]
        for sub in paths:
            assert any(r["category"] != matcher.LOGISTICS for r in sub), rec["trial_id"]
