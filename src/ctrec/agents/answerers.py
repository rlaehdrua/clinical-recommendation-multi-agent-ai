"""확인 질문에 대한 답변 제공자.

- InteractiveAnswerer: 터미널에서 사용자가 직접 답변 (시연용)
- SimulatedPatientAnswerer: 가상 환자의 숨겨진 상세 정보(hidden_details)를 아는 LLM이 답변 (개발·평가용)
- NoAnswerer: 답변 없음 (배치 실행 시 기본값, 질문만 기록)
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from .. import config, llm
from ..schemas import ClarifyingQuestion


class Answerer(Protocol):
    def answer(self, questions: list[ClarifyingQuestion], patient_id: str) -> dict[str, str]: ...


class NoAnswerer:
    def answer(self, questions, patient_id):
        return {q.question_id: "모름(답변 없음)" for q in questions}


class InteractiveAnswerer:
    def answer(self, questions, patient_id):
        print(f"\n=== [{patient_id}] 확인 질문 (모르면 Enter) ===")
        answers = {}
        for q in questions:
            ans = input(f"{q.question_id}. {q.question}\n   > ").strip()
            answers[q.question_id] = ans or "모름"
        return answers


class _SimAnswer(BaseModel):
    question_id: str
    answer: str


class _SimAnswers(BaseModel):
    answers: list[_SimAnswer]


SIM_SYSTEM = """당신은 임상시험 상담을 받는 가상 환자(또는 보호자) 역할입니다.
<hidden_record>는 이 환자의 전체 병력 기록이며, <hidden_record>에 있는 정보만 사용해 질문에 짧게 답합니다.
- 치료·약물·질환·시술 이력은 기록에 적힌 것이 전부입니다. 기록에 없는 치료나 질환에 대해 물으면 "없음"(받은 적 없음)으로 답합니다.
- 검사 수치, 날짜처럼 기록에 구체적인 값이 없는 항목은 "모름"이라고 답합니다.
- 추측하거나 기록에 없는 값을 지어내지 않습니다."""


class SimulatedPatientAnswerer:
    def __init__(self, hidden_details: dict[str, str]):
        self.hidden = hidden_details  # patient_id -> hidden text

    def answer(self, questions, patient_id):
        hidden = self.hidden.get(patient_id)
        if not hidden:
            return NoAnswerer().answer(questions, patient_id)
        qs = "\n".join(f"{q.question_id}. {q.question}" for q in questions)
        out = llm.structured(
            _SimAnswers,
            system=SIM_SYSTEM,
            user=f"<hidden_record>\n{hidden}\n</hidden_record>\n\n<questions>\n{qs}\n</questions>",
            effort=config.EFFORT["simulated_patient"],
            max_tokens=4000,
        )
        got = {a.question_id: a.answer for a in out.answers}
        return {q.question_id: got.get(q.question_id, "모름") for q in questions}
