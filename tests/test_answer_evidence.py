"""확인 질문 답변을 근거로 인정 (API 호출 없음). 실제 실행(SYN-DEMO-A, 2026-10-24)에서 발견된 사례를 재현합니다.

- 환자가 E2(EGFR ADC 치료 이력)에 "아니요"라고 답했고 모델도 'no'로 판정했지만, 근거를 의역해
  ("EGFR 표적 항체-약물 결합체(ADC) 치료 받은 적 없음 / 아니요") 인용 검증에서 unknown으로 되돌아가 '정보 부족'으로 남던 문제
- 그 기준을 겨냥한 질문에 실제로 답했으면 판정을 인정하고 근거를 문답 원문으로 바꿈. '모름', 다른 기준, 다른 시험은 그대로 검증
"""

from ctrec import decision_state
from ctrec.agents import matcher
from ctrec.schemas import MatcherOutput, QAPair
from test_offline import _profile, _rule
from test_stage05 import _la, _trial

Q = "EGFR을 표적으로 하는 항체-약물 결합체(ADC, antibody-drug conjugate; 예: patritumab deruxtecan 등) 치료를 받은 적이 있나요?"
SOURCE = ("62세 여성. 폐 선암 IVA기.\n\n[추가 확인 정보 - 라운드 1]\n"
          f"Q: {Q}\nA: 아니요\nQ: 현재 임신 중이거나 수유 중인가요?\nA: 모름")
PARAPHRASE = "EGFR 표적 항체-약물 결합체(ADC) 치료 받은 적 없음 / 아니요"


def _qa(answer="아니요", target=("T:E5",), question=Q, purpose="eligibility"):
    return QAPair(round=1, question_id="Q1", question=question, answer=answer, purpose=purpose, target=list(target))


def _run(monkeypatch, assessment, qa, source=SOURCE):
    rule = _rule(rule_id="E5", kind="exclusion", text="Prior treatment with any EGFR-targeted antibody-drug conjugate",
                 category="treatment_history", structured_evaluable=False)
    seen = {}

    def fake(*a, **k):
        seen["user"] = k.get("user", "")
        return MatcherOutput(assessments=[assessment])

    monkeypatch.setattr(matcher.llm, "structured", fake)
    m = matcher.match_trial(_profile(), _trial(rule), {"brief_summary": ""},
                            reference_date="2026-10-05", source_text=source, qa=qa)
    return m.assessments[0], seen["user"]


def test_paraphrased_quote_accepted_when_rule_was_asked_and_answered(monkeypatch):
    a, _ = _run(monkeypatch, _la(evidence=PARAPHRASE), [_qa()])
    assert a.holds == "no" and a.passes is True
    assert a.evidence == f"Q: {Q} A: 아니요"  # 근거는 모델 의역이 아니라 문답 원문
    assert a.evidence_source == "self_report"  # 답변에서 온 근거로 추적됨
    assert a.reasoning.startswith("[코드 확인: 이 기준을 묻는 확인 질문의 답변을 근거로 사용]")


def test_same_quote_without_answer_is_still_rejected(monkeypatch):
    a, _ = _run(monkeypatch, _la(evidence=PARAPHRASE), [])
    assert a.holds == "unknown" and a.passes is None


def test_declined_answer_is_not_evidence(monkeypatch):
    a, _ = _run(monkeypatch, _la(evidence=PARAPHRASE), [_qa(answer="모름")])
    assert a.holds == "unknown"


def test_answer_for_other_rule_or_trial_is_not_evidence(monkeypatch):
    for target in (("T:E4",), ("OTHER:E5",), ()):
        a, _ = _run(monkeypatch, _la(evidence=PARAPHRASE), [_qa(target=target)])
        assert a.holds == "unknown", target


def test_physician_reference_question_is_not_evidence(monkeypatch):
    a, _ = _run(monkeypatch, _la(evidence=PARAPHRASE), [_qa(purpose="physician_reference")])
    assert a.holds == "unknown"


def test_answer_must_be_in_source_text(monkeypatch):
    """문답 기록이 있어도 원문(답변 블록)에 없으면 인정하지 않음 (기록과 원문이 어긋난 경우)."""
    a, _ = _run(monkeypatch, _la(evidence=PARAPHRASE), [_qa()], source="62세 여성. 폐 선암 IVA기.")
    assert a.holds == "unknown"


def test_unknown_judgement_is_not_upgraded(monkeypatch):
    """모델이 unknown으로 둔 판정을 코드가 답변만 보고 확정하지 않음."""
    a, _ = _run(monkeypatch, _la(holds="unknown", evidence_type="absent", evidence="근거 없음", missing_info="x"), [_qa()])
    assert a.holds == "unknown"


def test_targeted_answers_shown_verbatim_in_prompt(monkeypatch):
    _, user = _run(monkeypatch, _la(evidence=f"Q: {Q} A: 아니요"), [_qa(), _qa(target=("T:E9",), question="다른 질문")])
    assert "<clarifying_answers>" in user and f"- [E5] Q: {Q} A: 아니요" in user
    assert "다른 질문" not in user  # 판정하지 않는 규칙의 문답은 넣지 않음


def test_no_answers_block_without_answers(monkeypatch):
    _, user = _run(monkeypatch, _la(evidence=PARAPHRASE), [])
    assert "<clarifying_answers>" not in user


def test_declined_answers_match_decision_state():
    assert matcher.DECLINED_ANSWERS == decision_state.DECLINED_ANSWERS
