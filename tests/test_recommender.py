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
    assert "모집 종료" in reasons["A"] and "공개 기준 충족" in reasons["A"]


def test_open_ineligible_and_closed_eligible(no_llm):
    trials = {"A": {"overall_status": "RECRUITING"}, "B": {"overall_status": "TERMINATED"}}
    rec = recommender.recommend(_profile(), [_match("A", "INELIGIBLE"), _match("B", "ELIGIBLE")], trials)
    assert "기준을 충족하거나 판정이 보류된 시험(B 공개 기준 충족·조기 종료(TERMINATED))은 모집이 종료" in rec.no_recommendation_reason


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


def test_display_order_ignores_llm_rank():
    """시험 간 우열 순위 제거 (아키텍처 §50): LLM이 매긴 순서는 쓰지 않고 코드가 그룹·모집 상태·후보 순서로 정함."""
    trials = {"A": {"overall_status": "NOT_YET_RECRUITING"}, "B": {"overall_status": "RECRUITING"},
              "C": {"overall_status": "RECRUITING"}}
    matches = [_match("A", "ELIGIBLE"), _match("B", "ELIGIBLE"), _match("C", "UNCERTAIN")]
    draft = RecommendationDraft(patient_id="P", overall_comment="", excluded=[], selection_reason="C가 가장 적합",
                                ranked=[_rt("C", 1, "UNCERTAIN"), _rt("A", 2, "ELIGIBLE"), _rt("B", 3, "ELIGIBLE")])
    rec = recommender._validate(draft, "P", matches, trials)
    # 같은 그룹(공개 기준 충족) 안에서는 모집 중(B)이 모집 예정(A)보다 먼저, 판정 보류(C)는 뒤 그룹
    assert [(r.trial_id, r.rank) for r in rec.ranked] == [("B", 1), ("A", 2), ("C", 3)]
    assert [r.match_status for r in rec.ranked] == ["CRITERIA_MATCH", "CRITERIA_MATCH", "INSUFFICIENT_INFORMATION"]
    # 추천 그룹 = 가장 앞 그룹 전체 (1순위 하나를 고르지 않음)
    assert [r.trial_id for r in rec.recommended] == ["B", "A"]
    assert "C가 가장 적합" not in rec.selection_reason and "우열을 뜻하지 않습니다" in rec.selection_reason


def test_same_group_keeps_candidate_order():
    trials = {t: {"overall_status": "RECRUITING"} for t in "XY"}
    matches = [_match("Y", "ELIGIBLE"), _match("X", "ELIGIBLE")]  # Y가 후보 추림에서 앞
    draft = RecommendationDraft(patient_id="P", overall_comment="", excluded=[], ranked=[_rt("X", 1, "ELIGIBLE")])
    rec = recommender._validate(draft, "P", matches, trials)
    assert [r.trial_id for r in rec.ranked] == ["Y", "X"]
    assert all(r.rank == i for i, r in enumerate(rec.ranked, start=1))


def test_demo_trial_blocked_unless_demo_mode(monkeypatch, no_llm):
    """가상 데모 시험은 판정은 하되 데모 모드에서만 추천 (아키텍처 §67)."""
    trials = {"DEMO": {"overall_status": "RECRUITING", "synthetic": True}}
    monkeypatch.setattr(recommender.config, "DEMO_MODE", False)
    rec = recommender.recommend(_profile(), [_match("DEMO", "ELIGIBLE")], trials)
    assert rec.ranked == [] and "가상 데모 시험뿐" in rec.no_recommendation_reason
    assert any(e.trial_id == "DEMO" and "가상 데모" in e.reason for e in rec.excluded)

    monkeypatch.setattr(recommender.config, "DEMO_MODE", True)
    rec = recommender._validate(RecommendationDraft(patient_id="P", overall_comment="", excluded=[], ranked=[]),
                                "P", [_match("DEMO", "ELIGIBLE")], trials)
    assert [r.trial_id for r in rec.recommended] == ["DEMO"]


def test_demo_trial_noted_when_real_trials_fail(monkeypatch, no_llm):
    monkeypatch.setattr(recommender.config, "DEMO_MODE", False)
    trials = {"DEMO": {"overall_status": "RECRUITING", "synthetic": True}, "A": {"overall_status": "RECRUITING"}}
    rec = recommender.recommend(_profile(), [_match("DEMO", "ELIGIBLE"), _match("A", "INELIGIBLE")], trials)
    assert "모집 중인 후보 시험이 모두 선정/제외 기준에 맞지 않았습니다" in rec.no_recommendation_reason
    assert "가상 데모 시험 1개(DEMO)" in rec.no_recommendation_reason


def test_demo_trial_left_out_of_screen_pool(monkeypatch):
    """후보 추림 풀에는 실제 시험만. 가상 데모 시험은 데모 모드에서만 들어감."""
    trials = {"DEMO": {"synthetic": True}, "A": {}}
    monkeypatch.setattr(recommender.config, "DEMO_MODE", False)
    assert list(recommender.screen_pool(trials)) == ["A"]
    monkeypatch.setattr(recommender.config, "DEMO_MODE", True)
    assert list(recommender.screen_pool(trials)) == ["DEMO", "A"]
