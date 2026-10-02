"""질문 생성 에이전트: 판정 불가(unknown) 항목을 해소하기 위한 확인 질문 생성."""

from __future__ import annotations

from .. import config, llm
from ..schemas import PatientProfile, QAPair, QuestionSet, TrialMatch
from .matcher import compact_json

SYSTEM = """당신은 임상시험 적격성 판정에 부족한 정보를 확인하는 '질문 생성 에이전트'입니다.
판정 불가(unknown) 항목 목록을 보고, 환자 또는 보호자에게 물어볼 확인 질문을 만듭니다.

질문은 두 종류입니다(purpose).
- eligibility: unknown 항목을 해소해 적격성 판정을 확정하기 위한 질문.
- physician_reference: physician_review 항목(예: "특정 약물 사용"처럼 프로토콜 원문에 구체 목록·기준이 없는 기준)에 대해,
  의사가 전체 프로토콜과 대조할 수 있도록 관련 사실을 수집하는 질문. 판정을 바꾸지는 않지만 최종 참여 판단의 참고자료가 됩니다.
  예: "현재 복용 중인 약(처방약, 일반의약품, 한약, 건강기능식품 포함)의 이름과 용량을 모두 알려주세요."
  physician_review 항목이 있고 이전 문답에서 아직 묻지 않았다면 반드시 physician_reference 질문을 포함합니다.
  같은 종류의 정보(예: 복용 약물)는 여러 시험·규칙에 걸쳐 질문 하나로 묶습니다.

원칙:
- 여러 시험·규칙을 한 번에 해소할 수 있는 질문을 우선합니다(target에 해결 대상 '<trial_id>:<rule_id>'를 모두 나열).
- eligibility 질문을 먼저, physician_reference 질문을 뒤에 둡니다. eligibility 질문은 적격성 결과를 바꿀 가능성이 큰 질문부터 순서대로 배치합니다. 이미 다른 불충족 기준으로 INELIGIBLE이 확정된 시험만을 위한 질문은 만들지 않습니다.
- 이미 환자 정보나 이전 문답에 답이 있는 내용은 다시 묻지 않습니다.
- 기준에 적힌 항목을 직접 묻습니다(돌려 묻지 않음). 의학 용어는 쉬운 한국어로 풀고 괄호 안에 원어를 병기합니다.
  예: "EGFR을 표적으로 하는 항체-약물 결합체(ADC, antibody-drug conjugate) 치료를 받은 적이 있나요?"
- 한 질문에는 하나의 사실만 묻습니다.
- 검사 수치처럼 환자가 모를 수 있는 항목은 "최근 검사 결과지에 ~ 수치가 있다면 알려주세요"처럼 묻습니다.
- logistics_to_confirm(동의 의사, 방문 가능 여부 등 절차 기준)은 연구진이 등록 시 확인할 사항이므로 질문하지 않습니다.
- 개인 식별 정보(이름, 주민번호, 연락처, 주소 등)는 절대 묻지 않습니다.
- question_id는 Q1, Q2, ... 로 매깁니다.
"""


def generate_questions(
    profile: PatientProfile,
    matches: list[TrialMatch],
    qa_log: list[QAPair],
    max_questions: int,
) -> QuestionSet:
    previous = "\n".join(f"- Q: {qa.question} / A: {qa.answer}" for qa in qa_log) or "(없음)"
    user = f"""<patient>
{profile.model_dump_json(indent=1)}
</patient>

<previous_qa>
{previous}
</previous_qa>

<trial_match_status>
{compact_json(matches)}
</trial_match_status>

최대 {max_questions}개의 확인 질문을 생성하세요(physician_reference 질문 포함). 물어볼 필요가 없으면 빈 목록을 반환하세요."""
    qs = llm.structured(
        QuestionSet,
        system=SYSTEM,
        user=user,
        effort=config.EFFORT["question_generator"],
    )
    qs.questions = qs.questions[:max_questions]
    return qs
