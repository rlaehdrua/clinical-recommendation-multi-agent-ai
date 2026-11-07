"""OfflineEvaluator(WP6)가 Session과 같은 문서 요건을 지나는가 (합성 데이터, LLM·API 호출 없음).

같은 값·같은 규칙을 실제 Session과 OfflineEvaluator에 넣어 층별 결과를 비교합니다. PR #20이 없는 트리에서는 건너뜁니다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import MappingProxyType

import pytest

pytest.importorskip("ctrec.sim.evaluator")

sys.path.insert(0, str(Path(__file__).parent))

from test_clinical_record_gate import AGE, HB, HB_HIGH, JUDGE, PID, RECORD, SELF_OK, documents, pair, profile, run  # noqa: E402,F401
from test_decisions import TRIAL, _parsed  # noqa: E402

from ctrec import document_authority  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.document_authority import NoDocumentAuthority  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.eval_hygiene import AliasMap  # noqa: E402
from ctrec.fact_store import VerificationEvent, document_backed  # noqa: E402
from ctrec.fact_view import tier  # noqa: E402
from ctrec.schemas import RuleMeta  # noqa: E402
from ctrec.sim.cohort import SimFact, SimTrial  # noqa: E402
from ctrec.sim.evaluator import SIM_AUTHORITY, OfflineEvaluator, make_fact, verify_fact  # noqa: E402

REF = "2026-10-12"
ALIAS = AliasMap(salt="document-gate").alias("probe")
HB_FACT = SimFact(field="lab:hemoglobin", value=12.0, unit="g/dL")
AGE_FACT = SimFact(field="age", value=62, unit="years")
EXPERT_META = {"I5": RuleMeta(required_evidence="clinical_record")}


def sim_trial(rule, meta):
    return SimTrial(_parsed([rule.model_copy()]).model_copy(update={"trial_id": "T1"}), dict(TRIAL, trial_id="T1"),
                    MappingProxyType(dict(meta)))


def sim_fact(f, i=0, *, verified=False, source="clinical_record", kind="document"):
    return make_fact(ALIAS, f, seq=i, origin=f"src{i}", source_type=source, verified=verified, ref_kind=kind, ref=None)


def sim_pair(rule, meta, facts):
    state, _ = OfflineEvaluator((sim_trial(rule, meta),), REF).evaluate(ALIAS, facts)
    return state.trials["T1"].eligibility, CohortState.from_decision_states({ALIAS: state}).pairs()[0].status.value


def conflict_store(s):
    documents(s)
    s.fact_store.add(PID, "lab:hemoglobin", 8.0, unit="g/dL", source_type="self_report", source_reference={"kind": "answer", "quote": "Hb 8"})


# 이름: (규칙, 메타, 시뮬레이터 사실, Session 입력 출처, Session 준비, Session 프로필, 기대 판정, 기대 상태)
CASES = {
    "declared": (HB, RECORD, [HB_FACT], "clinical_record", None, None, "UNCERTAIN", "VERIFICATION_PENDING"),
    "documented": (HB, RECORD, [(HB_FACT, True)], "clinical_record", documents, None, "ELIGIBLE", "CONFIRMED_ELIGIBLE"),
    "declared_mismatch": (HB_HIGH, RECORD, [HB_FACT], "clinical_record", None, None, "INELIGIBLE", "VERIFICATION_PENDING"),
    "documented_mismatch": (HB_HIGH, RECORD, [(HB_FACT, True)], "clinical_record", documents, None, "INELIGIBLE", "CONFIRMED_INELIGIBLE"),
    "self_report_rule": (AGE, SELF_OK, [AGE_FACT], "clinical_record", None, None, "ELIGIBLE", "CONFIRMED_ELIGIBLE"),
    "unknown": (HB, RECORD, [AGE_FACT], "clinical_record", None, profile(labs=[]), "UNCERTAIN", None),
}


@pytest.mark.parametrize("name", CASES)
def test_session_and_simulator_agree(run, name):  # noqa: F811
    rule, meta, facts, source, prepare, prof, eligibility, status = CASES[name]
    s = run([rule], source, meta=meta, prepare=prepare, prof=prof)
    made = [sim_fact(f[0], i, verified=True) if isinstance(f, tuple) else sim_fact(f, i) for i, f in enumerate(facts)]
    got = sim_pair(rule, meta, made)
    assert got == (s.matches["T1"].eligibility, pair(s).status.value)
    assert got[0] == eligibility and (status is None or got[1] == status)


def test_conflicting_values_hold_on_both_paths(run):  # noqa: F811
    s = run([HB], prepare=conflict_store)
    facts = [sim_fact(HB_FACT, 0, verified=True), sim_fact(SimFact(field="lab:hemoglobin", value=8.0, unit="g/dL"), 1, source="self_report", kind="answer")]
    got = sim_pair(HB, RECORD, facts)
    assert got[0] == s.matches["T1"].eligibility == "UNCERTAIN"
    assert got[1] not in ("CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE")


def test_a_verified_status_with_no_attestation_does_not_confirm_in_the_simulator():
    plain = sim_fact(HB_FACT)
    forged = plain.model_copy(update={"verification_status": "document_verified", "verification_history": [VerificationEvent(
        from_status="unknown", to_status="document_verified", reason="r", by="b", document_reference="DOC", at="2000-01-01T00:00:00")]})
    assert sim_pair(HB, RECORD, [forged]) == ("UNCERTAIN", "VERIFICATION_PENDING")


def test_an_attestation_moved_to_another_value_does_not_confirm():
    good = sim_fact(HB_FACT, verified=True)
    moved = good.model_copy(update={"value": 15.0})
    assert sim_pair(HB, RECORD, [moved]) == ("UNCERTAIN", "VERIFICATION_PENDING")


def test_simulated_document_request_is_what_confirms():
    fact = sim_fact(HB_FACT)
    assert sim_pair(HB, RECORD, [fact])[1] == "VERIFICATION_PENDING"
    assert sim_pair(HB, RECORD, [verify_fact(fact, "S_chart")]) == ("ELIGIBLE", "CONFIRMED_ELIGIBLE")


def test_the_simulator_authority_does_not_leak_out_of_the_evaluator():
    verified = sim_fact(HB_FACT, verified=True)
    with document_authority.use(NoDocumentAuthority()):
        sim_pair(HB, RECORD, [verified])
        assert isinstance(document_authority.current(), NoDocumentAuthority)
        assert not document_backed(verified) and tier(verified) == "declared_record"
    with document_authority.use(SIM_AUTHORITY):
        assert document_backed(verified)


def test_the_evaluator_passes_the_shared_check_to_the_matcher(monkeypatch):
    real, seen = matcher.match_trial, []

    def spy(*a, **k):
        seen.append(k.get("document_check"))
        return real(*a, **k)

    monkeypatch.setattr(matcher, "match_trial", spy)
    sim_pair(HB, RECORD, [sim_fact(HB_FACT)])
    assert seen and all(callable(c) for c in seen)


def test_a_clinician_rule_is_not_settled_by_a_simulated_document():
    from ctrec.schemas import LLMAssessment

    class PassAll:
        judge_id, include_draft = "pass_all", False

        def digest(self):
            return "pass_all"

        def assess(self, trial_id, rule, facts):
            return LLMAssessment(rule_id=rule.rule_id, applicable=True, holds="yes", evidence_type="explicit", confidence="high",
                                 evidence="hemoglobin 12 g/dL", reasoning="stub", missing_info=None)

    state, _ = OfflineEvaluator((sim_trial(JUDGE, EXPERT_META),), REF, PassAll()).evaluate(ALIAS, [sim_fact(HB_FACT, verified=True)])
    assert CohortState.from_decision_states({ALIAS: state}).pairs()[0].status.value == "EXPERT_REVIEW"


