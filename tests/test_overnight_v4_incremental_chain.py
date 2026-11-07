"""야간 V4: 연속 사실 변경에서 부분 재평가 == 전체 재평가, 무관한 기준 재사용, 영향 범위 추적. 합성 데이터, API 호출 없음.

test_incremental.py는 변경 1건씩 비교합니다. 여기서는 부분 재평가 결과를 다음 변경의 입력으로 이어 쓰고(체인)
매 단계 전체 재평가와 비교합니다. 시험 2개 중 변경과 무관한 시험의 기준은 모두 재사용되어야 합니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from document_support import attest_declared  # noqa: E402

from ctrec.agents import matcher  # noqa: E402
from ctrec.incremental import compare_states, evaluate_full, update  # noqa: E402

from incremental_fixtures import (AGE, DX, ECOG, EGFR, HB, P, PDL1, POLICY, FakeLLM, base_facts, snap,  # noqa: E402
                                  store as declared_store, trial)


@pytest.fixture(autouse=True)
def no_paid_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("유료 LLM이 호출되면 안 됨")
    monkeypatch.setattr(matcher.llm, "structured", boom)


def _store():
    s = declared_store(*base_facts())
    attest_declared(s, P)
    return s


# 규칙 엔진 기준만 있는 시험 2개: T1(나이·ECOG), T2(Hb). LLM 기준이 없어야 재사용이 의미 있음
PARSED_RULES = {"T1": trial([AGE, ECOG], tid="T1"), "T2": trial([HB.model_copy(update={"rule_id": "I1"})], tid="T2")}
# LLM 기준이 섞인 시험 (LLM 기준은 모든 변경에 다시 판정)
PARSED_MIXED = {"T1": trial([AGE, ECOG, HB, EGFR, PDL1, DX], tid="T1"), "T2": trial([AGE, HB], tid="T2")}

STEPS = [
    ("ecog_worse", lambda s: s.add(P, "ecog", 2, source_type="clinical_record")),
    ("hb_low_newer", lambda s: s.add(P, "lab:hemoglobin", 8.1, unit="g/dL", observed_at="2026-10-05",
                                     source_type="clinical_record")),
    ("egfr_pos", lambda s: s.add(P, "biomarker", "EGFR exon 19 deletion positive", source_type="clinical_record")),
    ("hb_recovered", lambda s: s.add(P, "lab:hemoglobin", 10.4, unit="g/dL", observed_at="2026-10-10",
                                     source_type="clinical_record")),
    ("age_dup", lambda s: s.add(P, "age", 62, unit="years", source_type="clinical_record")),
]


@pytest.mark.parametrize("parsed", [PARSED_RULES, PARSED_MIXED], ids=["rules_only", "mixed_llm"])
def test_chained_incremental_equals_full_each_step(parsed):
    llm = FakeLLM()
    s = _store()
    prev_snap = snap(s)
    state = evaluate_full(prev_snap, parsed, POLICY, llm)
    for name, change in STEPS:
        change(s)
        attest_declared(s, P)
        new_snap = snap(s)
        r = update(state, prev_snap, new_snap, parsed, POLICY, llm)
        full = evaluate_full(new_snap, parsed, POLICY, llm)
        eq = compare_states(r.state, full)
        assert eq["status"] == "equivalent", (name, eq)
        for tid in parsed:
            a = r.state.trials[tid].model_dump(include={"eligibility", "match_status", "matched_cohort",
                                                        "cohort_results"})
            b = full.trials[tid].model_dump(include={"eligibility", "match_status", "matched_cohort",
                                                     "cohort_results"})
            assert a == b, (name, tid)
            for rid, c in full.trials[tid].criteria.items():
                assert r.state.trials[tid].criteria[rid].state == c.state, (name, tid, rid)
        state, prev_snap = r.state, new_snap  # 부분 재평가 결과를 다음 단계 입력으로


def test_unrelated_trial_criteria_reused_and_impact_tracked():
    s = _store()
    before = snap(s)
    st0 = evaluate_full(before, PARSED_RULES, POLICY)
    s.add(P, "ecog", 2, source_type="clinical_record")
    attest_declared(s, P)
    r = update(st0, before, snap(s), PARSED_RULES, POLICY)
    t1 = {rid: c.status for rid, c in r.state.trials["T1"].criteria.items()}
    t2 = {rid: c.status for rid, c in r.state.trials["T2"].criteria.items()}
    assert t1 == {"I1": "reused", "I2": "evaluated"}  # 나이는 ECOG 변경과 무관
    assert t2 == {"I1": "reused"}  # Hb 시험은 전부 재사용
    assert set(r.changes.changed) == {"ecog"}  # 변경 항목 추적
    assert r.stats["reevaluated"] == 1 and r.stats["reused"] == 2
    # 재사용 기준의 근거 버전은 원래 계산 버전 그대로, 유효 확인 버전만 새 스냅샷
    old = st0.trials["T2"].criteria["I1"]
    new = r.state.trials["T2"].criteria["I1"]
    assert new.provenance == old.provenance and new.validated_version == r.state.snapshot_version


def test_bundled_change_many_fields_at_once_equals_full():
    """문서 1건이 여러 항목을 한 번에 바꾸는 경우 (ECOG·Hb·나이 동시)."""
    llm = FakeLLM()
    s = _store()
    before = snap(s)
    st0 = evaluate_full(before, PARSED_MIXED, POLICY, llm)
    s.add(P, "ecog", 0, source_type="clinical_record")
    s.add(P, "lab:hemoglobin", 7.5, unit="g/dL", observed_at="2026-10-08", source_type="clinical_record")
    s.add(P, "age", 63, unit="years", source_type="clinical_record")
    attest_declared(s, P)
    r = update(st0, before, snap(s), PARSED_MIXED, POLICY, llm)
    assert {"ecog", "lab:hemoglobin", "age"} <= set(r.changes.changed)
    assert compare_states(r.state, evaluate_full(snap(s), PARSED_MIXED, POLICY, llm))["status"] == "equivalent"
    # T2(나이·Hb)는 두 기준 모두 다시 판정 (재사용하면 안 됨)
    assert {c.status for c in r.state.trials["T2"].criteria.values()} == {"evaluated"}
