"""P12: 확인 질문에 답한 기준이 '정보 부족'으로 남는 문제 (재평가 누락) - 재현, 안전한 연결, 안전성 반례.

재현 (실제 실행 SYN-DEMO-A, 팀원 보고 b11ec29): 환자가 "현재 임신 중이거나 수유 중인가요?"에 "아니요"라고 답했고 모델도
'no'로 판정했지만, 모델은 재추출한 프로필만 보므로 근거를 의역했고("임신·수유 아님") 인용 검증이 판정을 되돌림.
모델이 "Q: … A: 아니요"를 그대로 인용해도 Session이 질문 기록을 넘기지 않아 짧은 답변 연결 검사(E9)에서 막힘.

수정 (answer_verification.py): Session이 자기 세션의 질문 기록을 넘기고, 되돌려진 판정은 이 기준을 겨냥한 예/아니요 답이
같은 대상·같은 기간을 물은 것으로 코드가 검증할 때만 인정. 대상 식별(target)은 증거가 아님 - 답 내용·질문 극성·기간·
상충·처음 입력과의 상충을 모두 확인. 오프라인 결정적 대역 (실제 LLM 호출 없음).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import pipeline, qa_guard
from ctrec.agents import matcher
from ctrec.schemas import ClarifyingQuestion, LLMAssessment, MatcherOutput, QAPair
from test_offline import _profile, _rule
from test_stage05 import _trial
from test_contract_p5_session import patched  # noqa: F401  (Session 대역 fixture)

SID = "sess-1"
PREG_Q = "현재 임신 중이거나 수유 중인가요?"
PREG_RULE = dict(rule_id="E4", kind="exclusion", text="Pregnant or breastfeeding women", category="reproductive",
                 structured_evaluable=False)


def qa_text(*rounds, initial="62세 여성. 폐 선암 IVA기.") -> str:
    """rounds: [(질문, 답), ...] 라운드별 목록 -> Session.clarify와 같은 원문 형식."""
    text = initial
    for i, pairs in enumerate(rounds, 1):
        text += f"\n\n[추가 확인 정보 - 라운드 {i}]\n" + "\n".join(f"Q: {q}\nA: {a}" for q, a in pairs)
    return text


def pair(q=PREG_Q, a="아니요", *, round_no=1, qid="Q1", target=("T:E4",), sid=SID, purpose="eligibility"):
    return QAPair(round=round_no, question_id=qid, question=q, answer=a, purpose=purpose, target=list(target),
                  session_id=sid)


def llm(holds="no", evidence="임신·수유 아님 (환자 답변)", applicable=True, evidence_type="explicit"):
    return LLMAssessment(rule_id="E4", applicable=applicable, holds=holds, evidence_type=evidence_type,
                         confidence="high", evidence=evidence, reasoning="모델 판정", missing_info=None)


def judge(monkeypatch, assessment, source, qa=None, session_id=SID, rule=None, policy=None):
    if policy:
        monkeypatch.setenv(qa_guard.POLICY_ENV, policy)
    else:
        monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    r = _rule(**(rule or PREG_RULE))
    if r.rule_id != assessment.rule_id:
        assessment = assessment.model_copy(update={"rule_id": r.rule_id})
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[assessment]))
    m = matcher.match_trial(_profile(), _trial(r), {"brief_summary": ""}, reference_date="2026-10-25",
                            source_text=source, qa=qa, session_id=session_id)
    return m, m.assessments[0]


# ============================================================ 재현 (수정 전 동작 = 질문 기록 없이 호출)
def test_repro_paraphrased_answer_judgment_is_lost_without_qa_records(monkeypatch):
    src = qa_text([(PREG_Q, "아니요")])
    _, a = judge(monkeypatch, llm(), src, qa=None)
    assert a.passes is None and "quote_not_in_source" in a.validation_flags  # '정보 부족'으로 남던 상태


def test_repro_verbatim_short_answer_is_unlinked_without_qa_records(monkeypatch):
    src = qa_text([(PREG_Q, "아니요")])
    _, a = judge(monkeypatch, llm(evidence=f"Q: {PREG_Q} A: 아니요"), src, qa=None)
    assert a.passes is None and a.evidence_directness == "unlinked" and "answer_not_linked" in a.validation_flags


# ============================================================ 정상 재평가
@pytest.mark.parametrize("evidence", ["임신·수유 아님 (환자 답변)", f"Q: {PREG_Q} A: 아니요"])
def test_direct_answer_is_used_with_verbatim_provenance(monkeypatch, evidence):
    src = qa_text([(PREG_Q, "아니요")])
    m, a = judge(monkeypatch, llm(evidence=evidence), src, qa=[pair()])
    assert a.holds == "no" and a.passes is True and a.evidence_directness == "direct"
    assert a.evidence == f"Q: {PREG_Q} A: 아니요"  # 근거는 모델 문장이 아니라 문답 원문
    assert a.evidence_source == "self_report" and a.validation_flags == ["answer_verified"]
    assert "r1:Q1" in a.reasoning and a.llm_judgment["holds"] == "no"
    assert m.n_unknown == 0


def test_yes_answer_sets_presence_in_question_polarity(monkeypatch):
    q = "현재 임신 중인가요?"
    src = qa_text([(q, "예")])
    _, a = judge(monkeypatch, llm(holds="yes", evidence="임신 중"), src, qa=[pair(q, "예")],
                 rule={**PREG_RULE, "text": "Pregnant women"})
    assert a.holds == "yes" and a.passes is False  # 제외 기준 해당 -> 불충족


def test_ever_absence_covers_current_absence(monkeypatch):
    q = "임신한 적이 있나요?"
    _, a = judge(monkeypatch, llm(evidence="임신 경험 없음"), qa_text([(q, "아니요")]), qa=[pair(q)],
                 rule={**PREG_RULE, "text": "Pregnant women"})
    assert a.passes is True  # 평생 없음 -> 현재도 없음 (넓은 범위의 부재)


def test_duplicate_identical_answers_in_any_order_give_same_result(monkeypatch):
    src = qa_text([(PREG_Q, "아니요")], [(PREG_Q, "아니요")])
    p1, p2 = pair(), pair(round_no=2)
    out = {judge(monkeypatch, llm(), src, qa=log)[1].passes for log in ([p1, p2], [p2, p1], [p1, p1, p2])}
    assert out == {True}


# ============================================================ 안전성 반례 (확정하지 않음)
def _blocked(a, reason):
    assert a.passes is None and a.holds == "unknown", a
    assert f"answer_unverified:{reason}" in a.validation_flags, a.validation_flags


def test_unrelated_question_answer_is_not_used(monkeypatch):
    q = "수술 후 부작용이 있었나요?"
    _, a = judge(monkeypatch, llm(), qa_text([(q, "아니요")]), qa=[pair(q)])
    _blocked(a, "question_concept")  # 임신·수유를 묻지 않은 질문
    q2 = "최근 감기 증상이 있었나요?"
    _, a = judge(monkeypatch, llm(), qa_text([(q2, "아니요")]), qa=[pair(q2)])
    _blocked(a, "question_concept")


def test_lifetime_surgery_answer_does_not_decide_recent_surgery_criterion(monkeypatch):
    q = "수술을 받은 적이 있나요?"
    rule = dict(rule_id="E1", kind="exclusion", text="Major surgery within 4 weeks before first dose",
                category="procedure", structured_evaluable=False)
    _, a = judge(monkeypatch, llm(evidence="수술 이력 없음"), qa_text([(q, "아니요")]),
                 qa=[pair(q, target=("T:E1",))], rule=rule)
    _blocked(a, "criterion_not_verifiable")  # 개념군 밖: 질문-기준 대응을 코드로 정할 수 없음 (질문 계약 필요)


def test_recent_window_answer_does_not_decide_lifetime_allergy(monkeypatch):
    q = "최근 4주 안에 요오드 조영제 알레르기 반응이 있었나요?"
    rule = dict(rule_id="E2", kind="exclusion", text="Known allergy to iodinated contrast agents",
                category="comorbidity", structured_evaluable=False)
    _, a = judge(monkeypatch, llm(evidence="조영제 알레르기 없음"), qa_text([(q, "아니요")]),
                 qa=[pair(q, target=("T:E2",))], rule=rule)
    _blocked(a, "time_scope_mismatch")  # 최근 4주 반응 없음 != 알레르기 없음


def test_ever_yes_does_not_decide_current_pregnancy(monkeypatch):
    q = "임신한 적이 있나요?"
    _, a = judge(monkeypatch, llm(holds="yes", evidence="임신 경험 있음"), qa_text([(q, "예")]), qa=[pair(q, "예")],
                 rule={**PREG_RULE, "text": "Pregnant women"})
    _blocked(a, "time_scope_mismatch")


def test_chemo_hospitalization_answer_does_not_decide_recent_chemo(monkeypatch):
    q = "항암 치료 때문에 입원한 적이 있나요?"
    rule = dict(rule_id="E3", kind="exclusion", text="Chemotherapy within 4 weeks before enrollment",
                category="treatment_history", structured_evaluable=False)
    _, a = judge(monkeypatch, llm(evidence="최근 항암 치료 없음"), qa_text([(q, "아니요")]),
                 qa=[pair(q, target=("T:E3",))], rule=rule)
    _blocked(a, "criterion_not_verifiable")
    q2 = "임신 때문에 입원한 적이 있나요?"  # 개념군 기준이어도 다른 사건을 끼운 질문
    _, a = judge(monkeypatch, llm(), qa_text([(q2, "아니요")]), qa=[pair(q2)])
    _blocked(a, "question_form")


def test_pregnancy_test_not_done_is_not_not_pregnant(monkeypatch):
    q = "임신 검사를 받았나요?"
    _, a = judge(monkeypatch, llm(evidence="임신 아님"), qa_text([(q, "아니요")]), qa=[pair(q)])
    _blocked(a, "test_vs_condition")


@pytest.mark.parametrize("answer", ["모름", "답변하지 않겠습니다", "확인할 수 없습니다", "", "기억나지 않습니다"])
def test_unknown_declined_or_empty_answers_never_confirm(monkeypatch, answer):
    _, a = judge(monkeypatch, llm(), qa_text([(PREG_Q, answer or "모름")]), qa=[pair(a=answer)])
    assert a.passes is None and a.holds == "unknown"


def test_answer_revision_and_cross_question_contradiction_stay_for_review(monkeypatch):
    src = qa_text([(PREG_Q, "아니요")], [(PREG_Q, "예")])
    _, a = judge(monkeypatch, llm(), src, qa=[pair(), pair(a="예", round_no=2)])
    _blocked(a, "conflicting_answers")  # 마지막 답을 고르지 않음
    q2 = "현재 임신 중인가요?"
    src = qa_text([(PREG_Q, "아니요"), (q2, "예")])
    _, a = judge(monkeypatch, llm(), src, qa=[pair(), pair(q2, "예", qid="Q2")])
    _blocked(a, "conflicting_answers")


def test_answer_conflicting_with_initial_record_is_not_used(monkeypatch):
    src = qa_text([(PREG_Q, "아니요")], initial="34세 여성. 현재 임신 12주로 산부인과 추적 중.")
    _, a = judge(monkeypatch, llm(), src, qa=[pair()])
    assert a.passes is None and any(f.startswith("answer_unverified:directness:") for f in a.validation_flags)


def test_answer_for_other_criterion_trial_session_or_purpose_is_not_used(monkeypatch):
    src = qa_text([(PREG_Q, "아니요")])
    for p, sid in [(pair(target=("T:E9",)), SID), (pair(target=("OTHER:E4",)), SID), (pair(sid="sess-2"), SID),
                   (pair(sid=None), SID), (pair(purpose="physician_reference"), SID), (pair(), None)]:
        _, a = judge(monkeypatch, llm(), src, qa=[p], session_id=sid)
        assert a.passes is None, (p, sid)


def test_forged_or_mismatched_provenance_is_not_used(monkeypatch):
    src = qa_text([(PREG_Q, "모름")])  # 기록은 '아니요'인데 원문은 '모름'
    _, a = judge(monkeypatch, llm(), src, qa=[pair()])
    _blocked(a, "answer_not_in_source")
    src2 = qa_text([(PREG_Q, "아니요")])  # 라운드 1 원문인데 기록은 라운드 2
    _, a = judge(monkeypatch, llm(), src2, qa=[pair(round_no=2)])
    _blocked(a, "answer_not_in_source")


def test_judgment_opposite_to_answer_is_neither_accepted_nor_flipped(monkeypatch):
    _, a = judge(monkeypatch, llm(holds="yes", evidence="임신 중으로 보임"), qa_text([(PREG_Q, "아니요")]), qa=[pair()])
    _blocked(a, "judgment_direction")


@pytest.mark.parametrize("q,a_,reason", [
    ("요오드 조영제에 알레르기가 있나요?", "아니요", "question_concept"),  # 임신·수유 기준에 다른 개념
    ("현재 임신 중이 아니신가요?", "예", "question_form"),  # 부정 의문문
    ("현재 임신 중일 가능성이 있나요?", "아니요", "question_form"),  # 가능성(추측)
    ("임신 중이거나 수유 중인가요? 그리고 흡연하시나요?", "아니요", "question_form"),  # 여러 질문
])
def test_ambiguous_question_forms_are_not_used(monkeypatch, q, a_, reason):  # noqa: D103
    _, a = judge(monkeypatch, llm(), qa_text([(q, a_)]), qa=[pair(q, a_)])
    _blocked(a, reason)


def test_or_question_yes_does_not_decide_single_object_criterion(monkeypatch):
    _, a = judge(monkeypatch, llm(holds="yes", evidence="임신 또는 수유"), qa_text([(PREG_Q, "예")]),
                 qa=[pair(a="예")], rule={**PREG_RULE, "text": "Pregnant women"})
    _blocked(a, "object_scope")  # '예'가 임신인지 수유인지 모름


def test_negative_result_criterion_is_not_decided_from_answers(monkeypatch):
    _, a = judge(monkeypatch, llm(holds="yes", evidence="임신 아님"), qa_text([(PREG_Q, "아니요")]), qa=[pair()],
                 rule={**PREG_RULE, "kind": "inclusion", "text": "Negative serum pregnancy test"})
    assert a.passes is None


def test_not_applicable_or_unconfident_judgments_are_not_upgraded(monkeypatch):
    src = qa_text([(PREG_Q, "아니요")])
    _, a = judge(monkeypatch, llm(applicable=False), src, qa=[pair()])
    assert a.passes is not True or a.applicable is False and "answer_verified" not in a.validation_flags
    _, a = judge(monkeypatch, llm(evidence_type="inferred"), src, qa=[pair()])
    assert a.passes is None and "answer_verified" not in a.validation_flags


def test_record_required_criterion_stays_unverified(monkeypatch):
    """진료 기록이 필요한 기준: 답변(자기 보고)으로 검증돼도 확정하지 않고 UNVERIFIED + 답변상 판정 보존."""
    rule = dict(rule_id="E2", kind="exclusion", text="Known allergy to iodinated contrast agents", category="comorbidity",
                structured_evaluable=False)
    q = "요오드 조영제에 알레르기 반응을 보인 적이 있나요?"
    from ctrec.schemas import RuleMeta
    r = _rule(**rule)
    t = _trial(r)
    t.rule_meta = {"E2": RuleMeta(required_evidence="clinical_record")}
    monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(
        assessments=[llm(evidence="조영제 알레르기 없음").model_copy(update={"rule_id": "E2"})]))
    m = matcher.match_trial(_profile(), t, {"brief_summary": ""}, reference_date="2026-10-25",
                            source_text=qa_text([(q, "아니요")]), qa=[pair(q, target=("T:E2",))], session_id=SID)
    a = m.assessments[0]
    assert a.passes is None and a.needs_verification and a.self_reported_holds == "no"


def test_contract_policy_keeps_answer_based_judgment_unverified(monkeypatch):
    _, a = judge(monkeypatch, llm(), qa_text([(PREG_Q, "아니요")]), qa=[pair()], policy="contract")
    assert a.passes is None and "qa_uncontracted" in a.validation_flags  # 미승인 계약: 결정적 판정 없음


def test_existing_qa_log_api_is_unchanged(monkeypatch):
    """E9의 qa_log 연결(직접 호출용)은 그대로: qa를 주지 않으면 새 검증은 실행되지 않음."""
    src = qa_text([(PREG_Q, "아니요")])
    _, a = judge(monkeypatch, llm(), src, qa=None)
    assert "answer_verified" not in a.validation_flags and not any("answer_unverified" in f for f in a.validation_flags)


# ============================================================ Session 재평가 E2E (오프라인)
def test_session_reevaluation_uses_verified_answer_and_keeps_provenance(monkeypatch, patched):  # noqa: F811
    from test_contract_p5_session import _Answerer, _profile_from, _session, _trials

    monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    rule = _rule(**{**PREG_RULE, "rule_id": "E4"})
    trials = _trials(monkeypatch, {"T1": [rule]})
    patched["llm_questions"] = [dict(question_id="Q1", question=PREG_Q, answer_type="yes_no", target=["T1:E4"],
                                     purpose="eligibility", why="E4")]
    answered = {"done": False}

    def fake(model, system, user, **k):  # 답변 전: 정보 없음 / 답변 후: 의역한 근거로 'no' (실제 실행에서 본 양상)
        h = ("no", "임신·수유 아님") if answered["done"] else ("unknown", "근거 없음")
        return MatcherOutput(assessments=[LLMAssessment(
            rule_id="E4", applicable=True, holds=h[0], evidence_type="explicit" if answered["done"] else "absent",
            confidence="high", evidence=h[1], reasoning="mock", missing_info=None)])

    class Ans(_Answerer):
        def answer(self, questions, patient_id):
            answered["done"] = True
            return super().answer(questions, patient_id)

    monkeypatch.setattr(matcher.llm, "structured", fake)
    s = _session(pipeline.Session, trials, Ans({PREG_Q: "아니요"}))
    s.run_fixed()
    a = next(x for x in s.matches["T1"].assessments if x.rule_id == "E4")
    assert a.passes is True and a.evidence == f"Q: {PREG_Q} A: 아니요" and a.validation_flags == ["answer_verified"]
    assert f"r1:Q1" in a.reasoning and s.matches["T1"].n_unknown == 0
    assert s.qa_log[0].session_id == s.session_id and "session_id" not in s.qa_log[0].model_dump()
    done = [t for t in s.tracer.records if t["agent"] == "matcher" and t["event"] == "done"]
    assert done[-1]["assessments"][0]["passes"] is True  # trace(화면·Inspector)에도 재평가 결과


def test_session_reevaluation_does_not_confirm_unverifiable_criterion(monkeypatch, patched):
    """팀원 재현 사례 E2 (EGFR ADC 치료 이력, 개념군 밖): 답했어도 코드가 질문-기준 대응을 검증할 수 없어 미확정 유지."""
    from test_contract_p5_session import _Answerer, _profile_from, _session, _trials

    monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    q = "EGFR을 표적으로 하는 항체-약물 결합체(ADC) 치료를 받은 적이 있나요?"
    rule = _rule(rule_id="E2", kind="exclusion", text="Prior treatment with any EGFR-targeted antibody-drug conjugate",
                 category="treatment_history", structured_evaluable=False)
    trials = _trials(monkeypatch, {"T1": [rule]})
    patched["llm_questions"] = [dict(question_id="Q1", question=q, answer_type="yes_no", target=["T1:E2"],
                                     purpose="eligibility", why="E2")]
    state = {"done": False}

    def fake(model, system, user, **k):
        return MatcherOutput(assessments=[LLMAssessment(
            rule_id="E2", applicable=True, holds="no" if state["done"] else "unknown",
            evidence_type="explicit" if state["done"] else "absent", confidence="high",
            evidence="EGFR ADC 치료 받은 적 없음 / 아니요", reasoning="mock", missing_info=None)])

    class Ans(_Answerer):
        def answer(self, questions, patient_id):
            state["done"] = True
            return super().answer(questions, patient_id)

    monkeypatch.setattr(matcher.llm, "structured", fake)
    s = _session(pipeline.Session, trials, Ans({q: "아니요"}))
    s.run_fixed()
    a = s.matches["T1"].assessments[0]
    assert a.passes is None and "answer_unverified:criterion_not_verifiable" in a.validation_flags
