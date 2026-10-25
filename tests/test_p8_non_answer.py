"""P8 qalink 선별 이전: 거부·모름·무응답 판별 통합 (question_contract.non_answer 하나) + E9에서 확정 근거 제외.

legacy 정책(서비스 기본)에서도 "답변하지 않겠습니다"의 '않'을 부재 진술로 읽어 확정하던 경로를 막음 (qa_guard와 무관).
"""

from __future__ import annotations

import pytest

from ctrec.decision_state import DECLINED_ANSWERS
from ctrec.evidence_directness import check
from ctrec.question_contract import QuestionContract, map_answer, non_answer, render

NON_ANSWERS = ["", "모름", "잘 모르겠어요", "모름(답변 없음)", "기억이 안 나요", "확실하지 않아요", "답변 거부", "거부합니다",
               "답변하지 않겠습니다", "대답 안 함", "응답 불가", "미응답", "말하고 싶지 않아요", "말씀드리기 어렵습니다",
               "그 부분은 답변하지 않겠습니다", "그건 말하고 싶지 않아요", "노코멘트", "I don't know", "prefer not to say",
               "declined", "N/A"]
STATEMENTS = ["예", "아니요", "BCG 치료를 거부했습니다", "수술은 받지 않았어요", "손목 수술 종류는 몰라요",
              "최근 4주 안에 큰 수술은 받지 않았어요", "임신 아니에요", "치료를 거부하고 퇴원함"]


@pytest.mark.parametrize("answer", NON_ANSWERS)
def test_non_answers(answer):
    assert non_answer(answer)


@pytest.mark.parametrize("answer", STATEMENTS)
def test_statements_are_not_non_answers(answer):
    assert not non_answer(answer)


def test_covers_decision_state_policy():
    assert all(non_answer(a) for a in DECLINED_ANSWERS)


CRITERIA = [("Major surgery within 4 weeks before first dose", "procedure"), ("Pregnant or breastfeeding", "reproductive"),
            ("Prior surgery", "procedure")]
DECLINES = ["그 부분은 답변하지 않겠습니다", "답변 거부합니다", "말하고 싶지 않아요", "모름(답변 없음)"]


@pytest.mark.parametrize("criterion,category", CRITERIA)
@pytest.mark.parametrize("answer", DECLINES)
@pytest.mark.parametrize("holds", ["yes", "no"])
def test_e9_non_answer_never_confirms_either_way(criterion, category, answer, holds):
    text = f"45세 여성.\n\n[추가 확인 정보 - 라운드 1]\nQ: 확인 질문입니다\nA: {answer}"
    c = check(criterion, answer, text, holds=holds, category=category)
    assert not c.ok and c.flags == ["non_answer"]


def test_record_refusal_statement_is_not_non_answer():
    """처음 입력(기록)의 거부 진술은 사실 진술 -> 비답변 판별 대상이 아님 (답변 줄에만 적용)."""
    text = "45세 여성. 2024년 BCG 치료를 거부함."
    assert check("Prior BCG therapy", "2024년 BCG 치료를 거부함", text, holds="no", category="treatment_history").flags \
        != ["non_answer"]


def test_contract_non_answer_records_no_fact():
    c = QuestionContract.from_dict({"contract_id": "QC-X", "version": 1, "answer_type": "yes_no",
                                    "question": "현재 임신 중인가요?",
                                    "options": [{"label": "예", "assertions": [["pregnancy/condition/current", True]]},
                                                {"label": "아니요",
                                                 "assertions": [["pregnancy/condition/current", False]]}]})
    q = render(c, "C1", ["T1:E3"])
    for a in ["그 부분은 답변하지 않겠습니다", "모름"]:
        assert map_answer(c, q, a).kind == "non_answer"
