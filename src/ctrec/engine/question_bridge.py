"""엔진 Action -> 화면 질문(ClarifyingQuestion) 변환 경계 (0.2, ActionRenderer 계약). 행동 선택·판정을 하지 않습니다.

규칙
1. 질문 목적(purpose)·대상(target)은 Action이 정함. 렌더러·문구 생성기(LLM 가능)는 바꿀 수 없음 (check_rendered).
2. Action에 검수된 질문 계약(question_contract_id)이 있으면 그 계약이 활성일 때 계약 문장·선택지를 그대로 씀
   (question_contract.render, LLM 없음). 활성이 아니면 실패로 보고하고 다른 문구로 바꿔 치우지 않음.
3. 계약이 없으면 문구 생성기(wording)가 문장·답 형식만 제안. 목적·대상·근거 사용 여부는 제안할 수 없음.
4. 의사 참고용(physician_reference) 질문의 답은 적격성 근거가 아님. qa_pair()가 목적을 그대로 기록하므로
   evidence_directness.eligibility_answers가 근거에서 제외합니다 (P14 D4). 질문 계약은 적격성 전용.
웹 연결(WebQuestionChannel·engine_runner)은 WP8에서 이 함수를 부르면 됩니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

from ..question_contract import render as render_contract
from ..question_contract import verify_asked
from ..schemas import ClarifyingQuestion, QAPair
from .contracts import Action

QUESTION_ACTIONS = frozenset({"ASK_PATIENT"})


class RenderError(ValueError):
    pass


@dataclass(frozen=True)
class Wording:
    """문구 생성기가 제안할 수 있는 것: 문장·답 형식·선택지·단위·설명뿐 (목적·대상 없음)."""
    question: str
    answer_type: Literal["yes_no", "single_choice", "multi_choice", "number", "date", "free_text"] = "yes_no"
    options: tuple[str, ...] = field(default_factory=tuple)
    unit: str | None = None
    why: str = ""


def renderable(action: Action) -> bool:
    """환자에게 묻는 행동인가 (환자 질문, 또는 의사 참고용 정보 수집으로 표시된 임상의 검토)."""
    return action.action_type in QUESTION_ACTIONS or (action.action_type == "CLINICIAN_REVIEW"
                                                       and action.purpose == "physician_reference")


def _contract(action: Action, contracts):
    if contracts is None:
        raise RenderError("question_contract_registry_required")
    status, _ = contracts.status(action.question_contract_id)
    if status != "active":
        raise RenderError(f"question_contract_not_active:{status}")
    return contracts.raw(action.question_contract_id)


def render_action(action: Action, question_id: str, *, contracts=None,
                  wording: Callable[[Action], Wording] | None = None) -> ClarifyingQuestion:
    if not renderable(action):
        raise RenderError("not_a_question_action")
    targets = list(action.target_criteria)
    if action.question_contract_id:
        q = render_contract(_contract(action, contracts), question_id, targets)
    else:
        if wording is None:
            raise RenderError("wording_required")
        w = wording(action)
        if not isinstance(w, Wording):
            raise RenderError("wording_must_return_Wording")
        q = ClarifyingQuestion(question_id=question_id, question=w.question, answer_type=w.answer_type,
                               options=list(w.options), unit=w.unit, target=targets, purpose=action.purpose,
                               why=w.why or action.reason_code)
    check_rendered(action, q, contracts=contracts)
    return q


def check_rendered(action: Action, q: ClarifyingQuestion, *, contracts=None) -> None:
    """화면에 보낼 질문이 Action과 맞는지 (목적·대상 불변, 계약 질문이면 계약 문장 그대로)."""
    if q.purpose != action.purpose:
        raise RenderError("purpose_changed_by_renderer")
    if list(q.target) != list(action.target_criteria):
        raise RenderError("targets_changed_by_renderer")
    if action.question_contract_id and not verify_asked(_contract(action, contracts), q):
        raise RenderError("question_contract_text_changed")


def qa_pair(action: Action, q: ClarifyingQuestion, *, round_no: int, answer: str,
            session_id: str | None = None) -> QAPair:
    """답변 기록. 목적·계약 ID는 Action에서 가져옴 (화면이 보낸 값을 믿지 않음)."""
    if q.purpose != action.purpose or list(q.target) != list(action.target_criteria):
        raise RenderError("question_does_not_match_action")
    return QAPair(round=round_no, question_id=q.question_id, question=q.question, answer=answer,
                  purpose=action.purpose, target=list(action.target_criteria), session_id=session_id,
                  contract_id=action.question_contract_id)


def attach_question_contract(action: Action, contract_id: str, contracts) -> Action:
    """검수된 활성 계약을 행동에 붙인 새 Action (검증을 다시 거침). 적격성 환자 질문에만."""
    data = action.model_dump()
    data["question_contract_id"] = contract_id
    new = Action.model_validate(data)
    _contract(new, contracts)
    return new


__all__ = ["Wording", "RenderError", "renderable", "render_action", "check_rendered", "qa_pair",
           "attach_question_contract"]
