"""질문 답 -> 행동 결과 상태 (WP5). 웹·오프라인 실행기가 같은 규칙으로 "모름"을 판별하도록 하나로 모은 곳.

무응답 판별은 question_contract.non_answer 하나만 씁니다 (빈 답, "모름"·"잘 모르겠어요"·"기억이 안 나요"·"답변 거부" 등,
decision_state.DECLINED_ANSWERS를 포함하는 가장 넓은 규칙). 사실 추출 쪽(profile_facts의 보류)도 같은 함수를 쓰므로
"사실로 저장하지 않은 답"과 "no_answer로 기록한 행동"이 어긋나지 않습니다.

질문 답만으로는 연락 불가(unreachable)나 확인 거부 전체(declined_all)를 알 수 없으므로 여기서는 만들지 않습니다.
그런 결과는 실행기가 실제로 관찰했을 때만 contracts.UNREACHABLE / DECLINED_ALL로 넘깁니다.
"""

from __future__ import annotations

from .contracts import NO_ANSWER


def is_non_answer(answer: str | None) -> bool:
    from ..question_contract import non_answer  # 지연 import: 계약 모듈(question_contract)이 무거움
    return non_answer(answer)


def answer_outcome(answer: str | None) -> tuple[str, str | None]:
    """(ActionOutcome.status, error_code). 무응답이면 ("no_response", "no_answer") - 항목 단위 실패."""
    return ("no_response", NO_ANSWER) if is_non_answer(answer) else ("succeeded", None)


__all__ = ["is_non_answer", "answer_outcome"]
