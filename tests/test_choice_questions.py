"""객관식 확인 질문: 선택지 정리와 자동 답변 에이전트에 보여 주는 형식."""

from ctrec.agents.answerers import _choices
from ctrec.agents.question_generator import _normalize
from ctrec.schemas import ClarifyingQuestion


def _q(answer_type, options=(), unit=None):
    return ClarifyingQuestion(question_id="Q1", question="q", answer_type=answer_type, options=list(options), unit=unit,
                              target=["T1:I1"], purpose="eligibility", why="w")


def test_unknown_option_removed_and_duplicates_dropped():
    q = _normalize(_q("single_choice", ["4주 이내", "모름", "4주 이내", " 3개월 이상 전 "]))
    assert q.answer_type == "single_choice"
    assert q.options == ["4주 이내", "3개월 이상 전"]


def test_choice_with_too_few_options_falls_back_to_free_text():
    q = _normalize(_q("multi_choice", ["해당 없음", "모름"]))
    assert q.answer_type == "free_text" and q.options == []


def test_non_choice_types_drop_options_and_unit_only_for_number():
    yn = _normalize(_q("yes_no", ["예", "아니요"], unit="kg"))
    assert yn.options == [] and yn.unit is None
    num = _normalize(_q("number", unit="mg/dL"))
    assert num.unit == "mg/dL"


def test_answerer_sees_same_choices_as_screen():
    assert _choices(_q("yes_no")) == " [선택: 예 / 아니요 / 모름]"
    assert _choices(_q("multi_choice", ["A", "B"])) == " [선택 (여러 개 선택 가능): A / B / 모름]"
    assert _choices(_q("number", unit="kg")) == " [단위: kg]"
    assert _choices(_q("free_text")) == ""


def test_old_questions_without_options_still_load():
    q = ClarifyingQuestion.model_validate({"question_id": "Q1", "question": "q", "answer_type": "yes_no", "target": [],
                                           "purpose": "eligibility", "why": "w"})
    assert q.options == [] and q.unit is None
