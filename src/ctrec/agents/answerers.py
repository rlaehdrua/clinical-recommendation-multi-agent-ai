"""확인 질문에 대한 답변 제공자.

- InteractiveAnswerer: 터미널에서 사용자가 직접 답변 (시연용)
- SimulatedPatientAnswerer: 가상 환자의 숨겨진 상세 정보(hidden_details)를 아는 LLM이 답변 (개발·평가용)
- FactRevealAnswerer: 사실 목록(oracle_facts)에서 질문이 직접 묻는 사실만 원문 그대로 공개 (정보 가리기 평가셋용)
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


# ---------------------------------------------------------------------------
# 사실 단위 공개 (정보 가리기 평가셋용)
# ---------------------------------------------------------------------------

class _Reveal(BaseModel):
    question_id: str
    fact_ids: list[str]


class _Reveals(BaseModel):
    reveals: list[_Reveal]


REVEAL_SYSTEM = """You map clarifying questions to a synthetic patient's recorded facts for an evaluation simulator.
For each question, choose the fact_ids whose content directly answers what the question asks (at most {k} per question).
- Choose only facts that answer the question itself. Do not choose facts that are merely related or that the question did not ask about.
- If no fact answers the question, return an empty list for it.
- Return every question_id exactly once."""


class FactRevealAnswerer:
    """질문이 직접 묻는 사실만 원문 그대로 공개하는 가상 환자.

    LLM은 '어떤 사실 ID가 질문에 답하는가'만 고르고, 답변 문장은 코드가 사실 원문으로 만듭니다.
    그래서 시뮬레이터가 묻지 않은 정보를 흘리거나 기록에 없는 값을 지어낼 수 없습니다.
    공개한 사실 ID는 reveal_log에 남아, 시스템이 숨겨진 핵심 정보를 되찾았는지 평가하는 데 쓰입니다.
    """

    def __init__(self, facts: dict[str, list[dict]], max_facts_per_answer: int = 2):
        self.facts = facts  # patient_id -> [{"fact_id", "text", ...}]
        self.k = max_facts_per_answer
        self.reveal_log: dict[str, list[dict]] = {}

    def answer(self, questions, patient_id):
        facts = {f["fact_id"]: f["text"] for f in self.facts.get(patient_id, [])}
        if not facts:
            return NoAnswerer().answer(questions, patient_id)
        fact_lines = "\n".join(f"{fid}: {text}" for fid, text in facts.items())
        qs = "\n".join(f"{q.question_id}. {q.question}" for q in questions)
        out = llm.structured(
            _Reveals,
            system=REVEAL_SYSTEM.format(k=self.k),
            user=f"<facts>\n{fact_lines}\n</facts>\n\n<questions>\n{qs}\n</questions>",
            effort=config.EFFORT["simulated_patient"],
            max_tokens=4000,
        )
        chosen = {r.question_id: [i for i in r.fact_ids if i in facts][: self.k] for r in out.reveals}
        answers, log = {}, self.reveal_log.setdefault(patient_id, [])
        for q in questions:
            ids = chosen.get(q.question_id, [])
            answers[q.question_id] = " ".join(facts[i] for i in ids) if ids else "모름 (기록에 없음)"
            log.append({"question_id": q.question_id, "question": q.question, "purpose": q.purpose, "fact_ids": ids})
        return answers

    def revealed(self, patient_id: str) -> list[dict]:
        return self.reveal_log.get(patient_id, [])


class RoutingAnswerer:
    """환자별로 다른 답변 제공자를 쓰는 조합 (사실 목록이 있으면 FactRevealAnswerer, 없으면 기존 시뮬레이터)."""

    def __init__(self, by_patient: dict[str, Answerer], default: Answerer):
        self.by_patient, self.default = by_patient, default

    def answer(self, questions, patient_id):
        return self.by_patient.get(patient_id, self.default).answer(questions, patient_id)

    def revealed(self, patient_id: str) -> list[dict]:
        a = self.by_patient.get(patient_id, self.default)
        return a.revealed(patient_id) if hasattr(a, "revealed") else []
