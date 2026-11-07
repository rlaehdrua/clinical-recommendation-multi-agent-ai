"""R5 P3(필수 기준 0개는 NOT_EVALUATED)와 D+E(값 근거, 임상의, 문서, 출력 경계)가 한 실행에서 함께 걸리는가.

합성 값, LLM·API 호출 없음. 절차 기준이 붙은 시험에서 R5가 다른 경계의 보류를 덮지 않고,
다른 경계가 R5의 제외를 되살리지 않는지 봅니다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import MappingProxyType

import pytest

pytest.importorskip("ctrec.sim.evaluator")

sys.path.insert(0, str(Path(__file__).parent))

import night_consistency_support as ncs  # noqa: E402
from document_support import AUTHORITY  # noqa: E402
from night_consistency_support import AGE, ALIAS, CONSENT, cohort_of, parsed, trial  # noqa: E402
from test_abc_safety_e2e import (HB, OLDER, REF, VAGUE, VAGUE_YES, PassAll, every_output, outcome, run, sim_facts,  # noqa: E402
                                 wp6)
from test_de_safety_e2e import GOOD, NOT_VALUES  # noqa: E402

from ctrec import conservative_view as cv, document_authority  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.document_authority import NoDocumentAuthority  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.contracts import PairStatus  # noqa: E402
from ctrec.sim.cohort import SimTrial  # noqa: E402
from ctrec.sim.evaluator import OfflineEvaluator, make_fact  # noqa: E402
from ctrec.web import display  # noqa: E402

NOT_EVALUATED = ("NOT_EVALUATED", "NOT_EVALUATED", "NOT_EVALUATED")
HELD_VALUE = ("UNCERTAIN", "INFO_MISSING", "INSUFFICIENT_INFORMATION")
HELD_DOC = ("UNCERTAIN", "VERIFICATION_PENDING", "POTENTIAL_MATCH")
HELD_EXPERT = ("UNCERTAIN", "EXPERT_REVIEW", "NEEDS_REVIEW")
CONFIRMED = ("ELIGIBLE", "CONFIRMED_ELIGIBLE", "CRITERIA_MATCH")
LLM_SAYS_CONSENT = {**VAGUE_YES, "CONSENT": ("yes", True, "62-year-old")}


@pytest.fixture(autouse=True)
def production():
    with document_authority.use(NoDocumentAuthority()):
        yield


@pytest.fixture(params=["session", "contract"])
def any_session(request, monkeypatch):
    if request.param == "contract":
        monkeypatch.setattr(ncs, "Session", ContractSession)
    return request.param


def self_report(tmp_path, trials, raw, **kw):
    return run(tmp_path, trials, raw, documented=False, gate=False, source="self_report", **kw)


# ------------------------------------------------------------ 절차 기준이 붙어도 각 경계가 그대로
@pytest.mark.parametrize("rules, raw, gate, want", [
    pytest.param([CONSENT], GOOD, True, NOT_EVALUATED, id="logistics_only"),
    pytest.param([CONSENT], NOT_VALUES["pending"], True, NOT_EVALUATED, id="logistics_only_non_value"),
    pytest.param([CONSENT, HB], GOOD, True, HELD_DOC, id="logistics_plus_document_value"),
    pytest.param([CONSENT, HB], NOT_VALUES["other_person"], False, HELD_VALUE, id="logistics_plus_non_value"),
    pytest.param([CONSENT, VAGUE], GOOD, True, HELD_EXPERT, id="logistics_plus_expert"),
    pytest.param([CONSENT, AGE], GOOD, False, CONFIRMED, id="logistics_plus_self_report"),
])
def test_one_gate_never_replaces_the_other(tmp_path, any_session, rules, raw, gate, want):
    if any_session == "contract" and want == CONFIRMED:
        want = HELD_DOC  # ContractSession은 계약 없는 답변의 확정을 qa_uncontracted로 낮춤 (D+E에서도 같음, R5와 무관)
    with run(tmp_path / "w", {"T": rules}, raw, documented=False, gate=gate, verdicts=LLM_SAYS_CONSENT,
             source="clinical_record" if gate else "self_report") as r:
        assert outcome(r.s) == want
        code = r.s.matches["T"].evaluation_error
    assert (code == "no_required_criteria") == (want == NOT_EVALUATED)


def test_every_gate_in_one_session_and_the_same_on_every_output(tmp_path):
    trials = {"NR": [CONSENT], "NR2": [CONSENT.model_copy(update={"rule_id": "CONSENT2"})], "DOC": [CONSENT, HB, AGE],
              "EXPERT": [CONSENT, VAGUE, AGE], "OUT": [CONSENT, OLDER, AGE]}
    with run(tmp_path / "w", trials, GOOD, documented=False) as r:
        got = {t: outcome(r.s, t) for t in trials}
        outputs = every_output(r.s, tmp_path / "out")
        ranked = {x.trial_id for x in r.s.recommendation.ranked}
        kinds = {t: display.exclusion_kind(r.s.matches[t], r.s.trials[t], ranked) for t in trials}
    assert got["NR"] == got["NR2"] == NOT_EVALUATED
    assert got["DOC"] == HELD_DOC and got["EXPERT"] == HELD_EXPERT
    assert got["OUT"][0] == "INELIGIBLE" and got["OUT"][2] == "CRITERIA_MISMATCH"
    assert kinds["NR"] == kinds["NR2"] == "needs_review"
    want = {"DOC": "POTENTIAL_MATCH", "EXPERT": "NEEDS_REVIEW"}
    assert all(v == want for v in outputs.values()), outputs


# ------------------------------------------------------------ 어떤 증거도 필수 기준 0개를 적격으로 만들지 못함
def test_a_document_or_llm_yes_does_not_turn_no_required_into_eligible(tmp_path):
    with document_authority.use(AUTHORITY):
        with run(tmp_path / "w", {"T": [CONSENT]}, GOOD, documented=True, verdicts=LLM_SAYS_CONSENT) as r:
            assert outcome(r.s) == NOT_EVALUATED
            assert r.s.recommendation.ranked == []


def test_a_document_releases_the_substantive_gate_beside_logistics(tmp_path):
    with document_authority.use(AUTHORITY):
        with run(tmp_path / "w", {"T": [CONSENT, HB, AGE]}, GOOD, documented=True, verdicts=LLM_SAYS_CONSENT) as r:
            assert outcome(r.s) == CONFIRMED


def test_a_no_required_trial_stays_out_of_a_ranked_list_with_confirmed_controls(tmp_path):
    with self_report(tmp_path / "w", {"OK": [CONSENT, AGE], "NR": [CONSENT]}, GOOD) as r:
        ok, nr = outcome(r.s, "OK"), outcome(r.s, "NR")
        outputs = every_output(r.s, tmp_path / "out")
    assert ok == CONFIRMED and nr == NOT_EVALUATED
    assert all(v == {"OK": "CRITERIA_MATCH"} for v in outputs.values()), outputs


# ------------------------------------------------------------ 코호트 경로
@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")], ids=["logistics_first", "clinical_first"])
@pytest.mark.parametrize("raw, gate, want", [
    pytest.param(NOT_VALUES["pending"], False, HELD_VALUE, id="non_value"),
    pytest.param(GOOD, True, HELD_DOC, id="document"),
])
def test_a_held_substantive_path_is_kept_over_a_logistics_path(tmp_path, order, raw, gate, want):
    rules = [CONSENT.model_copy(update={"cohort": "A"}), HB.model_copy(update={"cohort": "B"})]
    with run(tmp_path / "w", {"T": rules}, raw, documented=False, gate=gate, cohorts={"T": list(order)},
             source="clinical_record" if gate else "self_report") as r:
        assert outcome(r.s) == want
        assert r.s.matches["T"].evaluation_error is None and r.s.matches["T"].matched_cohort == "B"


# ------------------------------------------------------------ WP6
def wp6_state(rules, facts, attested):
    """필수 기준 0개는 state.trials에 없고 not_evaluated_trials로 가므로 상태 전체를 봄."""
    t = SimTrial(parsed("T", [x.model_copy() for x in rules]), trial("T"), MappingProxyType({}))
    made = [make_fact(ALIAS, f, seq=i, origin=f"src{i}", source_type="clinical_record", verified=attested,
                      ref_kind="document", ref=None) for i, f in enumerate(facts)]
    return OfflineEvaluator((t,), REF, PassAll()).evaluate(ALIAS, made)[0]


def test_wp6_applies_no_required_with_the_gates():
    facts = sim_facts("Hb 10 g/dL on 2026-09-01.")
    for attested in (False, True):
        state = wp6_state([CONSENT], facts, attested)
        assert state.no_required_trials == {"T": "no_required_criteria"} and state.trials == {}
        assert CohortState.from_decision_states({ALIAS: state}).pair(ALIAS, "T").status == PairStatus.NOT_EVALUATED
    assert wp6([CONSENT, HB, AGE], facts, False) == ("UNCERTAIN", "VERIFICATION_PENDING")
    assert wp6([CONSENT, HB, AGE], facts, True) == ("ELIGIBLE", "CONFIRMED_ELIGIBLE")
    assert wp6([CONSENT, VAGUE, AGE], facts, True) == ("UNCERTAIN", "EXPERT_REVIEW")
    assert wp6([CONSENT, OLDER, AGE], facts, False)[0] == "INELIGIBLE"


# ------------------------------------------------------------ 출력 경계와 엔진 코드
def test_the_output_boundary_has_nothing_to_lower_on_no_required(tmp_path):
    with run(tmp_path, {"T": [CONSENT]}, GOOD, documented=False) as r:
        v = cv.build(r.s)
        pair = cohort_of(r.s).pair(ALIAS, "T")
    assert v.holds == {} and pair.review_code == "no_required_criteria"
    assert v.engine["T"]["unresolved"] is None
