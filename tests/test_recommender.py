"""추천 에이전트의 모집 상태 처리와 '추천 없음' 사유 (API 호출 없음)."""

import pytest

from ctrec.agents import recommender
from ctrec.schemas import PatientProfile, RankedTrial, RecommendationDraft, TrialMatch


def _match(tid, elig):
    return TrialMatch(patient_id="P", trial_id=tid, eligibility=elig, assessments=[],
                      n_pass=0, n_fail=0, n_unknown=0, summary=f"{elig} 요약")


def _profile():
    return PatientProfile(patient_id="P", age=1, sex="male", primary_diagnosis=None, disease_stage=None,
                          diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[],
                          ecog=None, other_facts=[], missing_or_ambiguous=[])


@pytest.fixture
def no_llm(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("추천할 시험이 없으면 LLM을 호출하지 않아야 함")
    monkeypatch.setattr(recommender.llm, "structured", fail)


def test_all_trials_closed_but_eligible(no_llm):
    trials = {"A": {"overall_status": "COMPLETED"}, "B": {"overall_status": "COMPLETED"}}
    rec = recommender.recommend(_profile(), [_match("A", "ELIGIBLE"), _match("B", "INELIGIBLE")], trials)
    assert rec.ranked == []
    assert rec.no_recommendation_reason.startswith("현재 진행 중인 적절한 임상시험이 없습니다.")
    assert "모두 모집이 종료" in rec.no_recommendation_reason and "A" in rec.no_recommendation_reason
    reasons = {e.trial_id: e.reason for e in rec.excluded}
    assert "모집 종료" in reasons["A"] and "적격 기준은 충족" in reasons["A"]


def test_open_ineligible_and_closed_eligible(no_llm):
    trials = {"A": {"overall_status": "RECRUITING"}, "B": {"overall_status": "TERMINATED"}}
    rec = recommender.recommend(_profile(), [_match("A", "INELIGIBLE"), _match("B", "ELIGIBLE")], trials)
    assert "기준을 충족하거나 판정이 보류된 시험(B 적격·조기 종료(TERMINATED))은 모집이 종료" in rec.no_recommendation_reason


def test_all_open_ineligible(no_llm):
    trials = {"A": {"overall_status": "RECRUITING"}}
    rec = recommender.recommend(_profile(), [_match("A", "INELIGIBLE")], trials)
    assert "모집 중인 후보 시험이 모두 선정/제외 기준에 맞지 않았습니다" in rec.no_recommendation_reason


def test_llm_cannot_rank_closed_trial():
    trials = {"A": {"overall_status": "RECRUITING"}, "B": {"overall_status": "COMPLETED"}}
    matches = [_match("A", "UNCERTAIN"), _match("B", "ELIGIBLE")]
    draft = RecommendationDraft(patient_id="P", overall_comment="", excluded=[], ranked=[
        RankedTrial(rank=1, trial_id="B", eligibility="ELIGIBLE", rationale="", key_matches=[], key_concerns=[], next_steps=[]),
        RankedTrial(rank=2, trial_id="A", eligibility="UNCERTAIN", rationale="", key_matches=[], key_concerns=[], next_steps=[]),
    ])
    rec = recommender._validate(draft, "P", matches, trials)
    assert [r.trial_id for r in rec.ranked] == ["A"] and rec.ranked[0].rank == 1
    assert rec.no_recommendation_reason is None
    assert any(e.trial_id == "B" and "모집 종료" in e.reason for e in rec.excluded)


def test_physician_notes_for_underspecified_medication_rule():
    from ctrec.schemas import CriterionAssessment, Fact, QAPair
    a = CriterionAssessment(rule_id="E2", kind="exclusion", criterion_text="Use of certain medications",
                            holds="unknown", passes=None, confidence="low", evidence="", reasoning="",
                            missing_info="현재 복용 약물 전체", method="llm", category="medication",
                            underspecified=True)
    m = TrialMatch(patient_id="P", trial_id="A", eligibility="ELIGIBLE", assessments=[a],
                   n_pass=0, n_fail=0, n_unknown=1, summary="ELIGIBLE")
    prof = _profile().model_copy(update={"medications": [Fact(description="candesartan 8 mg", date=None, source_quote="")]})
    qa = [QAPair(round=1, question_id="Q1", question="복용 중인 약을 모두 알려주세요", answer="칸데사르탄, 비타민D",
                 purpose="physician_reference", target=["A:E2"])]
    draft = RecommendationDraft(patient_id="P", overall_comment="", excluded=[], ranked=[
        RankedTrial(rank=1, trial_id="A", eligibility="ELIGIBLE", rationale="", key_matches=[], key_concerns=[], next_steps=[]),
    ])
    rec = recommender._validate(draft, "P", [m], {"A": {"overall_status": "RECRUITING"}}, prof, qa)
    note = rec.ranked[0].physician_review[0]
    assert "Use of certain medications" in note and "칸데사르탄, 비타민D" in note and "candesartan 8 mg" in note
    assert "담당 의사" in note
    assert any("의사" in s for s in rec.ranked[0].next_steps)

    from ctrec.agents.explainer import _physician_section
    assert "의사 확인 필요 사항" in _physician_section(rec)


def _rt(tid, rank, elig):
    return RankedTrial(rank=rank, trial_id=tid, eligibility=elig, rationale="", key_matches=[], key_concerns=[], next_steps=[])


def test_single_top_recommendation():
    trials = {t: {"overall_status": "RECRUITING"} for t in "ABC"}
    matches = [_match("A", "UNCERTAIN"), _match("B", "ELIGIBLE"), _match("C", "UNCERTAIN")]
    draft = RecommendationDraft(patient_id="P", overall_comment="", excluded=[], selection_reason="B가 유일하게 적격",
                                ranked=[_rt("B", 1, "ELIGIBLE"), _rt("A", 2, "UNCERTAIN"), _rt("C", 3, "UNCERTAIN")])
    rec = recommender._validate(draft, "P", matches, trials)
    assert [r.trial_id for r in rec.recommended] == ["B"]
    assert [(r.trial_id, r.rank) for r in rec.ranked] == [("B", 1), ("A", 2), ("C", 3)]
    assert rec.selection_reason == "B가 유일하게 적격"


def test_tie_shown_together_only_within_same_eligibility():
    trials = {t: {"overall_status": "RECRUITING"} for t in "ABC"}
    matches = [_match("A", "ELIGIBLE"), _match("B", "ELIGIBLE"), _match("C", "UNCERTAIN")]
    # LLM이 A·B·C를 모두 공동 1순위로 줘도, 적격(A·B)만 공동 1순위이고 판정 보류(C)는 뒤로
    draft = RecommendationDraft(patient_id="P", overall_comment="", excluded=[],
                                ranked=[_rt("A", 1, "ELIGIBLE"), _rt("B", 1, "ELIGIBLE"), _rt("C", 1, "UNCERTAIN")])
    rec = recommender._validate(draft, "P", matches, trials)
    assert sorted(r.trial_id for r in rec.recommended) == ["A", "B"]
    assert {r.trial_id: r.rank for r in rec.ranked} == {"A": 1, "B": 1, "C": 3}
