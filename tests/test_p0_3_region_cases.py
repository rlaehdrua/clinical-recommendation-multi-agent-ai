"""P0-3 회귀: NCT06216301 국가 조건부 나이 기준 (tests/data/p0_3_region_cases.json, 서연 데이터 작성, 임상 검수 미완료).

원칙: 지역 조건이 불명확하면 확정 부적격 금지. 케이스의 기대 처리는 원문 문장 기준이며 엔진 결과를 보고 정하지 않았습니다.
현재 엔진(4f81460)이 금지 상태를 내는 케이스는 strict xfail로 고정합니다. 엔진이 고쳐지면 XPASS로 실패하므로
그때 데이터 파일의 known_defect를 false로 바꾸세요 (docs/proposals/p0_3_region_condition.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctrec.engine.cohort_state import CohortState
from ctrec.sim.cohort import SimFact, make_cohort
from ctrec.sim.evaluator import OfflineEvaluator, make_fact
from ctrec.sim.from_generator import load_trials

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((Path(__file__).parent / "data" / "p0_3_region_cases.json").read_text(encoding="utf-8"))
CASES = DATA["cases"]
TRIAL = "NCT06216301"


def test_case_file_follows_the_region_principle():
    original = json.loads((ROOT / "data" / "nsclc" / "trials" / f"{TRIAL}.json").read_text(encoding="utf-8"))
    text = original["eligibility_criteria"]
    assert DATA["clinical_review"] == "미완료"
    for c in CASES:
        for rid in ("I1", "I2"):
            assert c["protocol_reading"][rid]["quote"].rstrip(".") in text  # 원문 문장 그대로
        assert not set(c["forbidden_pair_status"]) & set(c["acceptable_pair_status"])
        if c["expected_age_outcome"] in ("NEEDS_COUNTRY", "AGE_NOT_BARRIER"):
            assert "CONFIRMED_INELIGIBLE" in c["forbidden_pair_status"], c["case_id"]
        if c["country"] == "unknown" and 18 <= c["age"] < 22:  # 국가에 따라 결과가 갈림
            assert c["expected_age_outcome"] == "NEEDS_COUNTRY"
    assert {(c["age"], c["country"]) for c in CASES} >= {(a, k) for a in (20, 25) for k in ("unknown", "USA", "non_USA")}


def _status(case: dict) -> str:
    trials = load_trials([TRIAL], ROOT / "data" / "nsclc" / "parsed", ROOT / "data" / "nsclc" / "trials")
    facts = [SimFact(field="age", value=case["age"], unit="years"), SimFact(field="sex", value="female")]
    if case["country"] != "unknown":
        facts.append(SimFact(field="other_fact", value="Resides in the USA" if case["country"] == "USA" else "Resides in Korea"))
    co = make_cohort("p03", "2026-11-07", trials, [(case["case_id"], facts, facts)], "p03")
    a = co.aliases()[case["case_id"]]
    pf = [make_fact(a, f, seq=0, origin=f"t{i}", source_type="clinical_record", verified=True, ref_kind="document", ref=None)
          for i, f in enumerate(facts)]
    st = OfflineEvaluator(co.trials, co.reference_date, None).evaluate(a, pf, (TRIAL,))[0]
    return CohortState.from_decision_states({a: st}).pair(a, TRIAL).status.value


@pytest.mark.parametrize("case", [
    pytest.param(c, id=c["case_id"], marks=pytest.mark.xfail(
        strict=True, reason="P0-3 미해결: 국가 조건 없이 파싱된 I1(≥22 in the USA)로 확정 부적격 (known_defect)"))
    if c["known_defect"] else pytest.param(c, id=c["case_id"])
    for c in CASES])
def test_engine_never_confirms_ineligible_when_region_is_unclear(case):
    s = _status(case)
    assert s not in case["forbidden_pair_status"], (case["case_id"], s)
    assert s in case["acceptable_pair_status"], (case["case_id"], s)
