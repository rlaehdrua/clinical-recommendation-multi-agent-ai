"""Decision Policy와 Decision State (Phase 3, A). 합성 데이터, 실제 Matcher + 오프라인 LLM 대역."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.agents import matcher
from ctrec.decision_policy import candidates, validate
from ctrec.decision_state import build_state, narrowable

import decision_fixtures as d


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)


def _cands(v, **kw):
    return candidates(build_state(v, **kw))[0]


def test_required_missing_before_optional_cohort():
    b_only = d.EGFR.model_copy(update={"rule_id": "I7", "cohort": "B"})
    a_only = d.DX.model_copy(update={"rule_id": "I8", "cohort": "A"})
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, a_only, b_only], cohorts=["A", "B"])},
               d.store(("age", 60, {"unit": "years"})))
    st = build_state(v)
    chosen = st.trials["T1"].matched_cohort
    other = "I7" if chosen == "A" else "I8"
    req = {c.rule_id: c.requirement for c in st.criteria()}
    assert req["I2"] == "required" and req[other] == "alternative_cohort"  # OR(코호트) 구조 보존
    tiers = {a.target_criterion_ids[0]: a.tier for a in candidates(st)[0] if a.action_type == "ASK_USER"}
    assert tiers["T1:I2"] == 2 and tiers[f"T1:{other}"] == 6  # 공통·선택된 코호트 필수 기준이 먼저


def test_info_needed_by_more_trials_ranks_first():
    s = d.store(("age", 60, {"unit": "years"}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, d.EGFR]), "T2": d.trial("T2", [d.AGE, d.ECOG]),
                "T3": d.trial("T3", [d.AGE, d.ECOG])}, s)
    asks = [a for a in _cands(v) if a.action_type == "ASK_USER"]
    assert [a.info_key for a in asks] == ["ecog", "biomarker:EGFR"]
    assert asks[0].expected_information_gain == 3 and asks[1].expected_information_gain == 1
    assert sum(asks[0].score_terms.values()) == pytest.approx(asks[0].value_score)  # 설명 가능한 점수


def test_conflict_prioritized_as_verify():
    s = d.store(("age", 60, {"unit": "years"}), ("ecog", 1, {}), ("ecog", 3, {}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, d.EGFR])}, s)
    st = build_state(v)
    ecog = next(c for c in st.criteria() if c.rule_id == "I2")
    assert (ecog.status, ecog.gap) == ("UNKNOWN", "conflict") and st.conflicting_fields == {"ecog": "contradictory"}
    first = candidates(st)[0][0]
    assert (first.action_type, first.reason_code, first.tier) == ("VERIFY", "fact_conflict", 1)
    assert not any(a.action_type == "ASK_USER" and a.info_key == "ecog" for a in candidates(st)[0])


def test_conflict_behind_decided_criterion_flagged():
    """기준선이 MET인데 근거 항목에 나중에 상충 사실이 생기면 안전 확인 (MET를 그대로 믿지 않음)."""
    s = d.store(("age", 60, {"unit": "years"}), ("ecog", 1, {}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG])}, s)
    s.add(d.P, "ecog", 3, source_type="self_report")
    v.snapshot = d.fact_changes.take_snapshot(s, d.P)
    st = build_state(v)  # 재평가 없이 기준선 판정 + 현재 사실로 상충 표시만 확인
    ecog = next(c for c in st.criteria() if c.rule_id == "I2")
    assert ecog.status == "MET" and ecog.conflict_on_decided
    assert candidates(st)[0][0].reason_code == "conflict_on_decided_criterion"


def test_unverified_self_report_requests_document_not_verified():
    s = d.store(("age", 60, {"unit": "years"}), ("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-09-01"}))
    p = d.trial("T1", [d.AGE, d.HB], meta=d.record_meta("I4"))
    v = d.view({"T1": p}, s, source_text="합성 Hb 11.0 g/dL", input_source="unknown")
    st = build_state(v)
    hb = next(c for c in st.criteria() if c.rule_id == "I4")
    assert (hb.status, hb.gap) == ("UNKNOWN", "unverified")  # 판정은 UNKNOWN 그대로 (MET으로 올리지 않음)
    doc = next(a for a in candidates(st)[0] if a.action_type == "REQUEST_DOCUMENT")
    assert doc.tier == 3 and doc.target_criterion_ids == ["T1:I4"]
    # clinical_record 선언 사실도 문서 검증 전이면 미검증 목록에 있음
    assert set(st.unverified_fact_ids) == {f.fact_id for f in s.facts(d.P)}


def test_resolved_info_not_asked():
    s = d.store(("age", 60, {"unit": "years"}), ("ecog", 1, {}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, d.EGFR])}, s)
    assert [a.info_key for a in _cands(v) if a.action_type == "ASK_USER"] == ["biomarker:EGFR"]


def test_declined_answer_defers_without_reasking():
    s = d.store(("age", 60, {"unit": "years"}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG])}, s, qa=[d.qa("Q1", "모름", "T1:I2")])
    acts = _cands(v)
    assert not any(a.action_type == "ASK_USER" for a in acts)
    defer = next(a for a in acts if a.action_type == "DEFER")
    assert defer.reason_code == "user_declined_or_exhausted" and defer.target_criterion_ids == ["T1:I2"]


def test_answered_but_unresolved_reparse_once():
    s = d.store(("age", 60, {"unit": "years"}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG])}, s, qa=[d.qa("Q1", "활동은 괜찮아요", "T1:I2")])
    acts = _cands(v)
    assert [a.action_type for a in acts if a.info_key == "ecog"] == ["REPARSE"]


def test_not_evaluated_preserved_and_needs_review():
    s = d.store(("age", 60, {"unit": "years"}))
    v = d.view({"T1": d.trial("T1", [d.AGE]), "T2": d.trial("T2", [d.AGE])}, s, not_evaluated={"T2"})
    st = build_state(v)
    assert st.not_evaluated_trials == ["T2"] and "T2" not in st.trials  # MET/NOT_MET으로 바꾸지 않음
    review = next(a for a in candidates(st)[0] if a.reason_code == "evaluation_failed")
    assert review.action_type == "NEEDS_REVIEW" and review.target_trial_ids == ["T2"]


def test_moot_trial_info_is_low_priority_and_states_unchanged():
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG]), "T2": d.trial("T2", [d.ECOG])},
               d.store(("age", 12, {"unit": "years"})))  # T1은 나이 기준 불충족
    st = build_state(v)
    assert st.trials["T1"].eligibility == "INELIGIBLE" and st.trials["T2"].eligibility == "UNCERTAIN"
    ask = next(a for a in candidates(st)[0] if a.info_key == "ecog")
    assert ask.expected_information_gain == 1  # 이미 불충족인 T1은 세지 않음
    for tid, t in st.trials.items():  # Decision State는 기준선 판정을 바꾸지 않음
        assert t.eligibility == v.matches[tid].eligibility


def test_physician_review_and_waivable_logistics():
    s = d.store(("age", 60, {"unit": "years"}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.VAGUE, d.CONSENT])}, s)
    st = build_state(v)
    gaps = {c.rule_id: (c.gap, c.requirement) for c in st.criteria()}
    assert gaps["E1"] == ("physician_review", "required") and gaps["I9"] == ("none", "waivable")
    acts = candidates(st)[0]
    assert any(a.reason_code == "criterion_underspecified" and a.action_type == "NEEDS_REVIEW" for a in acts)
    assert not any(a.action_type == "ASK_USER" for a in acts)  # 절차 기준·원문 미상 기준은 묻지 않음


def test_validate_blocks_unsafe_actions():
    s = d.store(("age", 60, {"unit": "years"}), ("ecog", 1, {}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, d.EGFR])}, s)
    st = build_state(v)
    ask = next(a for a in candidates(st)[0] if a.action_type == "ASK_USER")
    assert validate(ask.model_copy(update={"target_criterion_ids": ["T1:I2"]}), st) == "target_not_missing"
    tool = ask.model_copy(update={"action_type": "RUN_TOOL", "tool": "shell"})
    assert validate(tool, st) == "tool_not_allowed"
    assert validate(ask.model_copy(update={"prerequisites": ["A-none"]}), st) == "prerequisite_missing"


def test_evaluation_records_and_narrowing():
    s = d.store(("age", 60, {"unit": "years"}), ("ecog", 1, {}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, d.EGFR])}, s)
    by = {c.rule_id: c for c in build_state(v).criteria()}
    assert by["I2"].record.engine == "rule_engine" and by["I2"].record.used_fields == ["ecog"]
    assert narrowable(by["I2"].record) and by["I2"].evidence_refs  # 근거 사실 ID 연결
    assert by["I3"].record.engine == "llm" and by["I3"].record.used_fields is None and not narrowable(by["I3"].record)
    assert not narrowable(None)  # 기록이 없으면 좁힐 수 없음
