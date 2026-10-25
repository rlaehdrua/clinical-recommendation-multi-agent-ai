"""질문 생성 에이전트: 판정 불가(unknown) 항목을 해소하기 위한 확인 질문 생성."""

from __future__ import annotations

import contextlib
import contextvars
import json
import re

from .. import config, evidence_directness as ed, llm
from ..schemas import ClarifyingQuestion, PatientProfile, QAPair, QuestionSet, TrialMatch
from .matcher import compact_json, is_askable_unknown

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
- 답을 통일된 형식으로 받기 위해 가능하면 객관식으로 묻습니다(answer_type).
  - 있다/없다로 답할 수 있고 아래 검증 정책상 예/아니요 단답이 근거가 되는 기준이면 yes_no. 예: "현재 임신 중이거나 수유 중인가요?"
  - 답이 몇 가지 경우 중 하나면 single_choice, 여러 개를 고를 수 있으면 multi_choice로 하고 options에 선택지를 적습니다.
    선택지는 서로 겹치지 않고 가능한 답을 빠짐없이 덮도록 2~6개로 만들고, 필요하면 "해당 없음"이나 "기타(직접 입력)"를 넣습니다.
    예: "마지막 항암치료를 받은 시기는 언제인가요?" -> ["4주 이내", "4주~3개월 전", "3개월 이상 전", "항암치료를 받은 적 없음"]
    기준의 경계값(예: 4주, 28일, 3개월)이 선택지 경계와 일치하도록 나눕니다.
  - "모름" 선택지는 화면이 자동으로 붙이므로 options에 넣지 않습니다.
  - 검사 수치는 number(unit에 단위), 날짜는 date로 묻습니다.
  - free_text는 복용 약물 전체 목록처럼 선택지로 만들 수 없는 경우에만 씁니다.
- 검사 수치처럼 환자가 모를 수 있는 항목은 "최근 검사 결과지에 ~ 수치가 있다면 알려주세요"처럼 묻습니다.
- logistics_to_confirm(동의 의사, 방문 가능 여부 등 절차 기준)은 연구진이 등록 시 확인할 사항이므로 질문하지 않습니다.
- unverified_document_needed 항목은 이미 답을 받았고 진료 기록(문서)으로 확인할 항목이므로 다시 질문하지 않습니다.
- 개인 식별 정보(이름, 주민번호, 연락처, 주소 등)는 절대 묻지 않습니다.
- question_id는 Q1, Q2, ... 로 매깁니다.

답으로 판정을 확정할 수 있는 질문 (코드의 답변 검증 정책):
코드는 답이 기준의 대상과 기간을 직접 말할 때만 판정 근거로 인정합니다. 아래 형태가 아니면 답을 받아도 판정 불가로 남아 질문 예산만 씁니다.
- 예/아니요 단답(yes_no)은 임신·수유 여부, 알레르기·금기 기준에만 판정 근거가 됩니다. 그 밖의 기준은 single_choice로 묻고,
  각 선택지가 기준의 대상과 기간을 담은 완결된 문장이 되게 합니다(질문 없이 답만 읽어도 사실을 알 수 있게).
  예: "편두통 예방약을 마지막으로 복용한 시기는 언제인가요?" -> ["현재 복용 중", "1개월 이내에 중단", "1~3개월 전에 중단", "3개월 이상 전에 중단", "복용한 적 없음"]
- 기간 조건이 있는 기준("N개월 이내", "적어도 N개월 동안 사용하지 않음", "최근 N개월 동안 매달")은 질문이나 선택지에 기준과 같은 기간을 적습니다.
  "현재 ~ 중인가요?"는 "1개월 이상 사용하지 않음"을 확인하지 못하므로, 사용 여부가 아니라 중단 시점을 묻습니다.
  기간 안의 빈도는 "최근 3개월 동안 조짐이 얼마나 자주 있었나요?" -> ["최근 3개월 동안 매달 1회 이상 있었음", "최근 3개월 중 조짐이 없던 달이 있음", ...]처럼 선택지에도 기간을 적습니다.
- 선택지 하나에는 한 가지 상태만 적습니다. 서로 다른 상태를 "A거나 B"로 묶지 않습니다(예: "폐경되었거나 임신 가능성이 없음"은 쓰지 않음).
  "임신 가능성 없음" 같은 자기 평가 대신 폐경, 자궁 적출처럼 대상 집단을 정하는 사실을 묻습니다.
- 임신·수유 여부, 가임 가능 여부, 피임 방법·기간은 서로 다른 사실입니다. 임신·수유만 묻는 질문의 target에 피임 기준을 넣지 않습니다.
- target에는 이 질문의 답만으로 판정할 수 있는 기준만 적습니다. 이 질문으로 확인할 수 없는 기준은 target에서 빼고 따로 묻습니다.
- 코드는 위 형태로 확정할 수 없는 기준을 target에서 빼고, 대상이 모두 빠진 질문은 묻지 않습니다.

정보 확보 단계(<acquisition_plan>):
- 각 unknown 항목('<trial_id>:<rule_id>')에 이번에 쓸 확보 방식(step)이 정해져 있습니다. 그 방식에 맞게 질문합니다.
  예: step이 "결과지의 수치와 검사일을 요청"이면 결과지를 보고 답하도록 묻고, "대략적인 수치나 의료진 설명"이면 기억나는 대로 답하도록 묻습니다.
- <acquisition_plan>에 없는 unknown 항목은 확보 단계를 모두 시도한 항목이므로 다시 묻지 않습니다.
"""

BATCHING = {
    "single": "",
    "batch": "",
    "adaptive": """
한 라운드에 묶을 질문 (적응형):
- 서로 독립적이고(한 질문의 답이 다른 질문을 불필요하게 만들지 않음) 각각 판정을 바꿀 가능성이 큰 질문만 함께 묻습니다.
- 첫 질문의 답에 따라 필요 없어질 수 있는 질문은 이번 라운드에 넣지 않고 다음 라운드로 미룹니다.
  예: 같은 시험에서 한 기준이 불충족이면 그 시험의 다른 질문은 필요 없어짐 -> 그 시험은 탈락 가능성이 가장 큰 질문 하나만.
  예: "수술을 받았나요?"의 답이 '아니오'면 "수술 후 몇 주가 지났나요?"는 필요 없음 -> 앞 질문만.
- 묶을 질문이 없으면 가장 중요한 질문 하나만 반환합니다.
""",
}


# 실행별 질문 생성기 교체 (평가 실행이 Decision Loop 계획 등을 넣을 때). 설정한 컨텍스트(와 그 사본)에서만 쓰이고
# 다른 실행은 아래 기본 생성기를 그대로 씀. 모듈 속성을 전역으로 바꾸지 않기 위한 장치
_override_var: contextvars.ContextVar = contextvars.ContextVar("ctrec_question_generator", default=None)


@contextlib.contextmanager
def use_generator(fn):
    token = _override_var.set(fn)
    try:
        yield fn
    finally:
        _override_var.reset(token)


def generate_questions(
    profile: PatientProfile,
    matches: list[TrialMatch],
    qa_log: list[QAPair],
    max_questions: int,
    acquisition_plan: dict[str, dict] | None = None,
) -> QuestionSet:
    override = _override_var.get()
    if override is not None:
        return override(profile, matches, qa_log, max_questions, acquisition_plan)
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

<acquisition_plan>
{json.dumps(acquisition_plan or {}, ensure_ascii=False, indent=1)}
</acquisition_plan>

최대 {max_questions}개의 확인 질문을 생성하세요(physician_reference 질문 포함). 물어볼 필요가 없으면 빈 목록을 반환하세요."""
    qs = llm.structured(
        QuestionSet,
        system=SYSTEM + BATCHING.get(config.QUESTION_POLICY, ""),
        user=user,
        effort=config.EFFORT["question_generator"],
    )
    # 해소할 수 없는 대상을 뺀 뒤 예산만큼 (LLM이 정한 순서 그대로, 질문 수·예산은 늘리지 않음)
    qs.questions = screen([_normalize(q) for q in qs.questions], matches)[:max_questions]
    return qs


# ---------------------------------------------------------------------------- 생성 후 코드 검사 (P14 Q1)
# 기준의 기간 조건 ("in the 3 months prior", "for at least 1 month prior", "within 28 days", "4주 이내")
_C_WINDOW = (re.compile(r"(\d+)\s*(day|week|month|year)s?\b(?!\s*(?:of\s+age|old|or\s+older))"
                        r"(?=[^.;]{0,15}?\b(?:prior|before|preceding|ago)\b)", re.I),
             re.compile(r"\b(?:within|last|past|prior|previous|preceding)\s+(?:the\s+)?(?:last\s+|past\s+)?(\d+)\s*"
                        r"(day|week|month|year)s?\b", re.I),
             re.compile(r"(\d+)\s*(일|주|개월|달|년)\s*(?:이내|안에|동안|전)"))
_SPELLED = {w: str(i) for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve".split(), 1)}
_Q_TIME = re.compile(r"(\d+)\s*(일|주|개월|달|년|days?|weeks?|months?|years?)", re.I)
_CONTRACEPTION = re.compile(r"contracept|birth\s+control|피임", re.I)
# 질문이 피임(방법·여부)을 묻는가. 피임약을 약물로 묻는 질문("경구 피임약 등 에스트로겐 약")은 아님
_ASKS_CONTRACEPTION = re.compile(r"피임(?!약)|contraception|contraceptive\s+method|birth\s+control(?!\s+pill)", re.I)


def _days(n: str, unit: str) -> int:
    u = unit.lower()
    return int(n) * ed._UNIT_DAYS[u if u in ed._UNIT_DAYS else u.rstrip("s")]


def criterion_windows(text: str) -> set[int]:
    """기준 원문의 기간 조건(일). 영어 수사("three months")·"the past year"도 숫자로 읽음. 못 찾으면 빈 집합(검사 안 함)."""
    text = re.sub(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
                  lambda m: _SPELLED[m.group(1).lower()], text, flags=re.I)
    text = re.sub(r"\b(?:the\s+)?(past|last|prior|previous)\s+(day|week|month|year)\b", r"\1 1 \2", text, flags=re.I)
    # 나이("aged at least 18 years before consent")는 기간 조건이 아님
    return {_days(m.group(1), m.group(2)) for p in _C_WINDOW for m in p.finditer(text)
            if not re.search(r"\bage", text[max(0, m.start() - 15):m.start()], re.I)}


def unresolvable(q: ClarifyingQuestion, criterion: str) -> str | None:
    """이 질문의 답으로 이 기준을 확정할 수 없는 이유 (코드가 확인할 수 있는 것만). 확인할 수 없으면 None(그대로 둠).

    판정 근거 검증(evidence_directness·answer_verification)과 같은 정책: 예/아니요 단답은 E9 개념군 기준에만 근거가 되고,
    기간 조건은 질문·선택지에 같은 기간이 있어야 하며, 피임 기준은 피임(또는 대상 집단을 정하는 사실)을 물어야 함.
    질문의 의미를 넓혀 대상을 더하지 않고, 빼기만 합니다.
    """
    asked = " ".join([q.question, *q.options])
    concept = next((c for c in ed.CONCEPTS if c.criterion.search(criterion)), None)
    if q.answer_type == "yes_no" and concept is None:
        return "yes_no_not_verifiable"  # 기본 Session은 개념군 밖 기준의 예/아니요 단답을 근거로 쓰지 않음 (P12)
    if (concept and concept.population_exclusion and ed._POPULATION_CLAUSE.search(criterion)
            and concept.population_exclusion.search(q.question)):
        return None  # 대상 집단을 정하는 사실(폐경·자궁 적출)을 묻는 질문: '해당 없음' 근거는 기간 조건을 보지 않음 (D1 검사)
    if _CONTRACEPTION.search(criterion):
        if not _CONTRACEPTION.search(asked):
            return "contraception_not_asked"
    elif _ASKS_CONTRACEPTION.search(q.question):
        return "different_fact_asked"  # 피임을 묻는 질문은 임신·수유 여부 같은 다른 기준을 정하지 않음
    elif q.answer_type == "yes_no" and concept and not concept.direct.search(asked):
        return "concept_not_asked"  # answer_verification도 질문에 개념어가 없으면 예/아니요 답을 쓰지 않음
    windows = criterion_windows(criterion)
    if windows and q.answer_type != "date":
        stated = {_days(n, u) for n, u in _Q_TIME.findall(asked)}
        # '있음' 기준: 답의 기간이 기준 기간 안이어야 확인됨 (evidence_directness._answer_within) -> 기준보다 넓은 기간은 아님.
        # '없음' 기준("적어도 1개월 동안 사용하지 않음"): 기준 기간을 덮는 더 넓은 기간도 됨
        absence = bool(ed._CRITERION_NEGATION.search(criterion))
        if not any(w - max(1, w // 10) <= s and (absence or s <= w) for s in stated for w in windows):
            return "time_window_not_asked"
    return None


def screen(questions: list[ClarifyingQuestion], matches: list[TrialMatch]) -> list[ClarifyingQuestion]:
    """적격성 질문의 target에서 지금 판정 불가인 기준이 아니거나 답으로 확정할 수 없는 기준을 빼고, 대상이 모두 빠진 질문은 버림.

    의사 참고용 질문과 처음부터 target이 없는 질문은 그대로 둡니다(판정 근거로 쓰지 않거나 대상을 확인할 수 없음).
    """
    criteria = {f"{m.trial_id}:{a.rule_id}": a.criterion_text for m in matches for a in m.assessments
                if is_askable_unknown(a)}
    out = []
    for q in questions:
        if q.purpose != "eligibility" or not q.target:
            out.append(q)
            continue
        keep = [t for t in q.target if t in criteria and unresolvable(q, criteria[t]) is None]
        if keep:
            q.target = keep
            out.append(q)
    return out


UNKNOWN_OPTIONS = {"모름", "잘 모름", "모르겠음", "기억 안 남"}


def _normalize(q):
    """선택지 정리: '모름'은 화면이 붙이므로 빼고, 선택지가 2개 미만인 객관식은 주관식으로 되돌림."""
    if q.answer_type in ("single_choice", "multi_choice"):
        opts = list(dict.fromkeys(o.strip() for o in q.options if o.strip() and o.strip() not in UNKNOWN_OPTIONS))
        q.options = opts[:8]
        if len(q.options) < 2:
            q.answer_type, q.options = "free_text", []
    else:
        q.options = []
    if q.answer_type != "number":
        q.unit = None
    return q
