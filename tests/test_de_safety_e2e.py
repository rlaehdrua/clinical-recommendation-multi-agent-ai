"""환자값 근거(E)가 A+B+C와 한 실행 안에서 맞물리는가. 합성 값, LLM·API 호출 없음.

같은 환자 문장을 실제 Session.run_fixed에 넣고 Fact Store, 판정, 쌍 상태, 추천, 저장 파일, 웹 표시, CLI, 오케스트레이터를 함께 봅니다.
ContractSession과 갱신 경로는 test_value_context.py가 봅니다. WP6는 프로필러를 거치지 않으므로(구조화 사실 입력) 원문 표현이 닿는 지점이 없어,
여기서는 값이 사실로 올라왔을 때와 사실이 없을 때의 경계만 Session과 비교합니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("ctrec.sim.evaluator")

sys.path.insert(0, str(Path(__file__).parent))

import test_abc_safety_e2e as abc  # noqa: E402
import test_night2_status_boundary as night  # noqa: E402
from document_support import AUTHORITY  # noqa: E402
from night_consistency_support import AGE, ALIAS, cohort_of  # noqa: E402
from test_abc_safety_e2e import HB, HB_HIGH, HISTORY, OLDER, VAGUE, every_output, outcome, run, wp6  # noqa: E402

from ctrec import document_authority  # noqa: E402
from ctrec.document_authority import NoDocumentAuthority  # noqa: E402
from ctrec.schemas import RuleMeta  # noqa: E402
from ctrec.sim.cohort import SimFact  # noqa: E402


GOOD = "Hb 10 g/dL on 2026-09-01."
NOT_VALUES = {
    "not_done": "CBC 미시행. Hb 10 g/dL on 2026-09-01.",
    "pending": "Hb 10 g/dL on 2026-09-01 result pending.",
    "invalid": "Hb 10 g/dL on 2026-09-01. Specimen hemolyzed, result voided.",
    "other_person": "Donor Hb 10 g/dL on 2026-09-01.",
    "population": "Control arm median Hb 10 g/dL on 2026-09-01.",
    "before_value": "Specimen never collected. Hb 10 g/dL on 2026-09-01.",
    "value_as_subject": "Hb 10 g/dL on 2026-09-01 belongs to the bed next door.",
}
HOLD = ("UNCERTAIN", "INFO_MISSING", "INSUFFICIENT_INFORMATION")


def gate_free(tmp_path, raw, rules=(HB, AGE), **kw):
    """self_report_ok 기준으로 실행: 문서 게이트가 닿지 않아 값 근거만 판정을 막음."""
    return run(tmp_path, {"T": list(rules)}, raw, documented=False, gate=False, source="self_report", **kw)


@pytest.fixture(autouse=True)
def production():
    with document_authority.use(NoDocumentAuthority()):
        yield


@pytest.mark.parametrize("name", NOT_VALUES)
def test_a_non_value_is_held_on_every_output_and_is_not_a_fact(tmp_path, name):
    with gate_free(tmp_path / "w", NOT_VALUES[name]) as r:
        got = outcome(r.s)
        outputs = every_output(r.s, tmp_path / "out")
        facts = r.s.fact_store.facts(r.s.case.patient_id, "lab:hemoglobin")
    assert got == HOLD and facts == []
    assert all(v == {"T": "INSUFFICIENT_INFORMATION"} for v in outputs.values()), outputs


@pytest.mark.parametrize("name", NOT_VALUES)
def test_a_non_value_on_a_failed_threshold_does_not_exclude(tmp_path, name):
    with gate_free(tmp_path, NOT_VALUES[name], rules=(HB_HIGH, AGE)) as r:
        assert r.s.matches["T"].eligibility != "INELIGIBLE"


def test_the_same_value_stated_plainly_is_confirmed_and_ranked(tmp_path):
    with gate_free(tmp_path / "w", GOOD) as r:
        got = outcome(r.s)
        outputs = every_output(r.s, tmp_path / "out")
        facts = r.s.fact_store.facts(r.s.case.patient_id, "lab:hemoglobin")
    assert got == ("ELIGIBLE", "CONFIRMED_ELIGIBLE", "CRITERIA_MATCH") and len(facts) == 1
    assert all(v == {"T": "CRITERIA_MATCH"} for v in outputs.values()), outputs


def test_a_plainly_failed_value_still_excludes(tmp_path):
    with gate_free(tmp_path, GOOD, rules=(HB_HIGH, AGE)) as r:
        assert r.s.matches["T"].eligibility == "INELIGIBLE"


@pytest.mark.parametrize("name", NOT_VALUES)
def test_a_non_value_never_reaches_the_document_gate(tmp_path, name):
    """문서 필수 기준: 합성 문서를 붙여도 측정값이 아닌 수치는 확정되지 않음. 대조군은 확정됨."""
    with document_authority.use(AUTHORITY):
        with run(tmp_path / "bad", {"T": [HB, AGE]}, NOT_VALUES[name], documented=True) as r:
            bad = outcome(r.s)
        with run(tmp_path / "good", {"T": [HB, AGE]}, GOOD, documented=True) as r:
            good = outcome(r.s)
    assert bad[1] != "CONFIRMED_ELIGIBLE" and bad[0] != "ELIGIBLE"
    assert good == ("ELIGIBLE", "CONFIRMED_ELIGIBLE", "CRITERIA_MATCH")


def test_all_boundaries_together_in_one_session(tmp_path):
    trials = {"VALUE": [HB, AGE], "DOC": [HB, AGE], "EXPERT": [VAGUE, AGE], "OUT": [OLDER, AGE], "ASK": [HISTORY, AGE]}
    raw = NOT_VALUES["other_person"] + " " + GOOD.replace("10", "11")
    with night.Run(tmp_path, trials, raw, documented=False, verdicts=abc.VAGUE_YES, rounds=1, source="clinical_record") as r:
        got = {t: outcome(r.s, t) for t in trials}
        ranked = {x.trial_id for x in r.s.recommendation.ranked}
    assert got["DOC"][1] != "CONFIRMED_ELIGIBLE"
    assert got["EXPERT"][1] == "EXPERT_REVIEW"
    assert got["OUT"][0] == "INELIGIBLE" and "OUT" not in ranked


def test_wp6_without_the_fact_matches_session_with_a_withheld_value(tmp_path):
    """Session이 값을 사실로 올리지 않은 상태와 WP6의 '사실 없음'이 같은 보류여야 함."""
    with gate_free(tmp_path, NOT_VALUES["invalid"]) as r:
        session_status = cohort_of(r.s).pair(ALIAS, "T").status.value
    sim = wp6([HB.model_copy(), AGE], [SimFact(field="age", value=62, unit="years")], False)
    assert session_status == "INFO_MISSING" and sim[0] == "UNCERTAIN" and sim[1] in ("INFO_MISSING", "VERIFICATION_PENDING")
