"""질문 가치 계산과 불충족 확정 수준 (Phase 3 보완). 합성 데이터, 실제 Matcher + 오프라인 LLM 대역.

불충족 확정 수준은 질문 순서에만 쓰며 기존 Matcher 판정(eligibility)은 바꾸지 않습니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.agents import matcher
from ctrec.decision_policy import candidates
from ctrec.decision_state import build_state

import decision_fixtures as d


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)


def _age_store(age: int | None, *, verified=True, source="clinical_record", extra=()):
    facts = ([("age", age, {"unit": "years", "source_type": source})] if age is not None else []) + list(extra)
    s = d.store(*facts)
    if verified and age is not None:
        f = s.facts(d.P, "age")[0]
        s.set_verification(f.fact_id, "document_verified", reason="합성 문서 확인", by="test", document_reference="DOC-1")
    return s


def _state(trials, s, **kw):
    v = d.view({tid: d.trial(tid, rules, **opts) for tid, (rules, opts) in trials.items()}, s, **kw)
    return v, build_state(v)


def _ask(st, key):
    return next(a for a in candidates(st)[0] if a.action_type == "ASK_USER" and a.info_key == key)


def _unchanged(v, st):
    assert {t: x.eligibility for t, x in st.trials.items()} == {t: m.eligibility for t, m in v.matches.items()
                                                                 if t in st.trials}


# 1·9. 확정적인 필수 나이 기준 불충족 (일부 시험만)
def test_confirmed_age_mismatch_excluded_from_gain():
    v, st = _state({"T1": ([d.AGE, d.ECOG], {}), "T2": ([d.ECOG], {})}, _age_store(12))
    t1 = st.trials["T1"]
    assert (t1.eligibility, t1.mismatch, t1.mismatch_basis) == ("INELIGIBLE", "confirmed", ["T1:I1"])
    assert st.trials["T2"].mismatch == "none"
    ask = _ask(st, "ecog")
    assert ask.expected_information_gain == 1 and ask.tier == 2  # T2만 결과가 바뀔 수 있음
    assert ask.score_terms["required"] == 2.0 and ask.score_terms["other"] == 0.5  # T1 기준은 '그 밖'으로만
    _unchanged(v, st)


# 2. 나이 기준 충족
def test_age_met_counts_normally():
    _, st = _state({"T1": ([d.AGE, d.ECOG], {}), "T2": ([d.ECOG], {})}, _age_store(40))
    assert {t.mismatch for t in st.trials.values()} == {"none"}
    assert _ask(st, "ecog").expected_information_gain == 2


# 3. 나이 미상
def test_age_unknown_is_not_mismatch():
    v, st = _state({"T1": ([d.AGE, d.ECOG], {})}, _age_store(None))
    t1 = st.trials["T1"]
    assert (t1.eligibility, t1.mismatch) == ("UNCERTAIN", "none")
    age = _ask(st, "age")
    assert age.tier == 2 and age.target_criterion_ids == ["T1:I1"]
    _unchanged(v, st)


# 4. 나이 사실 충돌
def test_age_conflict_never_used_as_exclusion():
    s = d.store(("age", 12, {"unit": "years"}), ("age", 40, {"unit": "years"}))
    _, st = _state({"T1": ([d.AGE, d.ECOG], {})}, s)
    age = next(c for c in st.criteria() if c.rule_id == "I1")
    assert (age.status, age.gap, st.trials["T1"].mismatch) == ("UNKNOWN", "conflict", "none")
    assert candidates(st)[0][0].action_type == "VERIFY"


def test_conflict_behind_age_mismatch_reopens_path():
    """기준선이 나이로 불충족인데 나이 사실이 상충하게 되면 그 불충족을 제외 근거로 쓰지 않음."""
    s = _age_store(12)
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG])}, s)
    s.add(d.P, "age", 40, unit="years", source_type="self_report")
    v.snapshot = d.fact_changes.take_snapshot(s, d.P)
    st = build_state(v)
    t1 = st.trials["T1"]
    assert t1.eligibility == "INELIGIBLE" and t1.mismatch == "none" and t1.open_paths == ["all"]
    first = candidates(st)[0][0]
    assert (first.action_type, first.reason_code) == ("VERIFY", "conflict_on_decided_criterion")
    assert _ask(st, "ecog").expected_information_gain == 1


# 5. OR/cohort의 다른 경로가 살아 있음
def test_other_cohort_path_alive():
    age_a = d.AGE.model_copy(update={"rule_id": "I7", "cohort": "A"})
    ecog_b = d.ECOG.model_copy(update={"rule_id": "I8", "cohort": "B"})
    _, st = _state({"T1": ([d.DX, age_a, ecog_b], {"cohorts": ["A", "B"]})}, _age_store(12))
    t1 = st.trials["T1"]
    assert t1.cohort_results["A"] == "INELIGIBLE" and t1.eligibility == "UNCERTAIN"
    assert (t1.mismatch, t1.open_paths) == ("none", ["B"])  # 시험 전체를 제외하지 않음
    assert _ask(st, "ecog").expected_information_gain == 1


# 6. 필수 기준 불충족이 있지만 질문 후보가 남음
def test_mismatched_trial_questions_kept_but_low_priority():
    v, st = _state({"T1": ([d.AGE, d.ECOG, d.EGFR], {})}, _age_store(12))
    asks = [a for a in candidates(st)[0] if a.action_type == "ASK_USER"]
    assert {a.info_key for a in asks} == {"ecog", "biomarker:EGFR"}
    assert {(a.tier, a.expected_information_gain) for a in asks} == {(6, 0)}  # 질문 하나로 불충족이 풀린다고 보지 않음
    _unchanged(v, st)


# 7. LLM 판정 기준 미평가
def test_llm_unevaluated_is_not_mismatch():
    _, st = _state({"T1": ([d.DX, d.EGFR], {})}, _age_store(40))
    t1 = st.trials["T1"]
    assert (t1.eligibility, t1.mismatch) == ("UNCERTAIN", "none")
    assert {c.status for c in t1.criteria} == {"UNKNOWN"}


# 8. 모든 후보 시험이 확정 불충족
def test_all_trials_confirmed_mismatch():
    _, st = _state({"T1": ([d.AGE, d.ECOG], {}), "T2": ([d.AGE, d.EGFR], {})}, _age_store(12))
    acts = candidates(st)[0]
    assert {t.mismatch for t in st.trials.values()} == {"confirmed"}
    assert all(a.tier >= 6 for a in acts)  # 필수 정보 질문 없음
    assert any(a.action_type == "RETRIEVE" and a.reason_code == "no_open_candidate" for a in acts)
    assert not any(a.reason_code == "exclusion_basis_unverified" for a in acts)  # 근거가 검증됨


# 근거가 문서로 검증되지 않은 불충족 (clinical_record 선언·자기 보고)
@pytest.mark.parametrize("source", ["clinical_record", "self_report"])
def test_unverified_basis_is_provisional(source):
    v, st = _state({"T1": ([d.AGE, d.ECOG], {}), "T2": ([d.ECOG], {})}, _age_store(12, verified=False, source=source))
    t1 = st.trials["T1"]
    assert t1.eligibility == "INELIGIBLE" and t1.mismatch == "provisional"  # 확정 제외 근거로 쓰지 않음
    ask = _ask(st, "ecog")
    assert ask.expected_information_gain == 1 and ask.score_terms["provisional"] == 0.5
    doc = next(a for a in candidates(st)[0] if a.reason_code == "exclusion_basis_unverified")
    assert doc.action_type == "REQUEST_DOCUMENT" and doc.target_criterion_ids == ["T1:I1"] and doc.tier == 6
    _unchanged(v, st)


# 10. 질문 가치가 낮아도 의료진 검토가 필요한 경우
def test_low_question_value_but_review_needed():
    v2, _ = _state({"T1": ([d.VAGUE], {}), "T2": ([d.AGE, d.ECOG], {})}, _age_store(12))
    r, _ = d.run(v2)
    assert any(a.action_type == "NEEDS_REVIEW" and a.reason_code == "criterion_underspecified" for a in r.actions)
    assert [a.tier for a in r.actions if a.action_type == "ASK_USER"] == [6]  # T2 확정 불충족 -> 낮은 순위
    assert r.stop_reason == "needs_review"
