"""P9 E9(evidence_directness) 통합 회귀 테스트.

- 부정 표현 회귀 사례 (P9 §5 목록): 맞는 방향은 통과, 반대 방향(잘못된 통과·잘못된 제외)은 막힘
- 한 문장 안의 긍정·부정: 있음·없음 서술 뒤의 대등 연결 어미(-고·-(으)며)도 절 경계 (기준 65eaa0f부터 있던 잘못된 통과)
- 거부·모름·확인 불가 답은 어느 쪽 확정 근거도 아님. 의료 사실과 모름이 함께 있는 답은 통째로 막지 않음
- fix/qa-evidence-link에서 옮긴 구조 연결 검사: session_id·라운드·목적(eligibility) 격리, 같은 질문의 답 수정
E9는 보조 안전장치(표기 판별)이며 의미 판정기가 아닙니다. 개념군(임신·수유, 알레르기·금기) 밖 기준은 'unchecked'로
LLM 판정을 그대로 두는 기존 범위를 유지합니다(test_non_concept_criteria_keep_existing_scope).
"""

from __future__ import annotations

import pytest

from ctrec.evidence_directness import check
from ctrec.question_contract import non_answer
from ctrec.schemas import QAPair

PREG = "Pregnant participants"
ALG = "Known allergy to iodinated contrast agents"


def qa(q: str, a: str, round_no: int = 1) -> str:
    return f"58세 여성.\n\n[추가 확인 정보 - 라운드 {round_no}]\nQ: {q}\nA: {a}"


# (이름, 기준, 인용, 원문, 맞는 holds) - 맞는 holds는 direct, 반대 holds는 확정되지 않아야 함
CASES = [
    ("산부인과(婦人)", PREG, "현재 임신 12주로 산부인과 추적 중", "45세 여성. 현재 임신 12주로 산부인과 추적 중.", "yes"),
    ("부인함(否認)", PREG, "임신 사실 부인함", "45세 여성. 임신 사실 부인함.", "no"),
    ("임신 아님", PREG, "현재 임신 아님", "45세 여성. 현재 임신 아님.", "no"),
    ("임신하지 않음", PREG, "현재 임신하지 않음", "45세 여성. 현재 임신하지 않음.", "no"),
    ("검사 음성", PREG, "소변 임신 검사 음성", "45세 여성. 소변 임신 검사 음성.", "no"),
    ("검사 양성", PREG, "소변 임신 검사 양성", "45세 여성. 소변 임신 검사 양성.", "yes"),
    ("부작용 없음", ALG, "요오드 조영제 부작용 없음", "60세 남성. 요오드 조영제 부작용 없음.", "no"),
    ("한 문장 긍정·부정(-고)", ALG, "요오드 조영제 알레르기 있음",
     "60세 남성. 페니실린 알레르기는 없고 요오드 조영제 알레르기 있음.", "yes"),
    ("한 문장 긍정·부정(-으며)", ALG, "요오드 조영제 알레르기 있음",
     "60세 남성. 페니실린 알레르기는 없으며 요오드 조영제 알레르기 있음.", "yes"),
    ("한 문장 부정·긍정(-지만)", PREG, "현재 임신 아니지만", "45세 여성. 현재 임신 아니지만 수유 중입니다.", "no"),
    ("Q:/A: 부재", ALG, "요오드 조영제 알레르기 없어요", qa("요오드 조영제 알레르기가 있나요?", "요오드 조영제 알레르기 없어요"), "no"),
    ("Q:/A: 존재", PREG, "네 임신 8주입니다", qa("현재 임신 중이신가요?", "네 임신 8주입니다"), "yes"),
    ("의료기록 원문(영문)", PREG, "β-hCG negative", "Lab 2026-10-01: serum β-hCG negative. 산부인과 협진 예정.", "no"),
    ("추가 Q&A 답변(산부인과)", PREG, "산부인과에서 임신 확인받았어요",
     qa("임신 여부를 알려 주세요", "산부인과에서 임신 확인받았어요"), "yes"),
]


@pytest.mark.parametrize("name,crit,quote,text,right", CASES, ids=[c[0] for c in CASES])
def test_negation_regressions_both_directions(name, crit, quote, text, right):
    cat = "reproductive" if crit == PREG else "allergy"
    assert check(crit, quote, text, holds=right, category=cat).directness == "direct"
    wrong = check(crit, quote, text, holds="no" if right == "yes" else "yes", category=cat)
    assert not wrong.ok and wrong.directness != "direct"


def test_negation_about_a_different_object_does_not_cover():
    text = "60세 남성. 페니실린 알레르기 없음. 요오드 조영제 사용 후 두드러기 있었음."
    c = check(ALG, "페니실린 알레르기 없음", text, holds="no", category="allergy")
    assert not c.ok and c.flags == ["object_not_matched"]


@pytest.mark.parametrize("text", ["검사 보고 없음 상태로 내원", "교통사고 이후 통증", "최고 혈압 150"])
def test_nouns_ending_in_go_are_not_clause_boundaries(text):
    """'-고' 경계는 있음·없음 서술(없·있·않·아니) 바로 뒤만: 명사 '보고'·'사고'·'최고'는 나누지 않음."""
    from ctrec.evidence_directness import _CLAUSE_BOUNDARY
    assert not [m for m in _CLAUSE_BOUNDARY.finditer(text) if m.group() in ("고", "며", "으며")]


@pytest.mark.parametrize("crit,quote,text", [
    ("Positive hepatitis B surface antigen", "B형 간염 없음", "45세 여성. B형 간염 없음."),
    ("Prior surgery", "수술 받지 않음", "60세 남성. 수술 받지 않음."),
])
def test_non_concept_criteria_keep_existing_scope(crit, quote, text):
    """개념군 밖 기준은 E9가 극성을 판정하지 않음(기존 범위, 'unchecked'). 확정 근거 검증은 질문 계약·IR 경로가 맡음 -
    legacy에서 남는 위험으로 보고서 K절에 기록."""
    for h in ("yes", "no"):
        assert check(crit, quote, text, holds=h).directness == "unchecked"


# ---------------------------------------------------------------------------- 거부·모름·확인 불가
P9_NON_ANSWERS = ["답변하지 않겠습니다", "답변 거부합니다", "모르겠습니다", "기억나지 않습니다", "확인할 수 없습니다",
                  "해당 질문에 답하고 싶지 않습니다", "답하고 싶지 않아요", "답변을 원하지 않습니다", "확인이 어렵습니다",
                  "알 수 없습니다", "대답하기 싫어요", "그 질문에는 답변 안 함", "날짜는 확인할 수 없어요"]


@pytest.mark.parametrize("answer", P9_NON_ANSWERS)
def test_p9_non_answers_detected(answer):
    assert non_answer(answer)


@pytest.mark.parametrize("answer", P9_NON_ANSWERS)
@pytest.mark.parametrize("crit,cat", [(PREG, "reproductive"), (ALG, "allergy")])
@pytest.mark.parametrize("holds", ["yes", "no"])
def test_p9_non_answers_never_confirm(answer, crit, cat, holds):
    text = qa("현재 임신 중이신가요?" if crit == PREG else "요오드 조영제 알레르기가 있나요?", answer)
    c = check(crit, answer, text, holds=holds, category=cat)
    assert not c.ok and c.flags == ["non_answer"]


@pytest.mark.parametrize("answer", ["수술은 받았어요. 날짜는 기억나지 않습니다", "임신 중이에요. 몇 주인지는 모르겠어요",
                                    "치료를 거부했습니다", "알 수 없는 원인의 두드러기가 있었어요"])
def test_fact_with_unknown_part_is_not_non_answer(answer):
    assert not non_answer(answer)


def test_mixed_fact_and_unknown_answer_confirms_the_stated_fact():
    """'임신 중이에요. 몇 주인지는 모르겠어요': 임신은 확정 근거, 모르는 부분(주수)은 그 문장에만 해당."""
    text = qa("현재 임신 중이신가요?", "임신 중이에요. 몇 주인지는 모르겠어요")
    assert check(PREG, "임신 중이에요", text, holds="yes", category="reproductive").directness == "direct"
    assert not check(PREG, "임신 중이에요", text, holds="no", category="reproductive").ok
    assert check(PREG, "몇 주인지는 모르겠어요", text, holds="no", category="reproductive").flags == ["hedged_quote"]


def test_hedged_absence_is_not_confirmed():
    text = qa("현재 임신 중이신가요?", "잘 모르겠지만 아닌 것 같아요")
    assert not check(PREG, "잘 모르겠지만 아닌 것 같아요", text, holds="no", category="reproductive").ok


# ---------------------------------------------------------------------------- qalink 구조 연결 검사
KEY = "T1:E1"
Q = "현재 임신 중이신가요?"


def pair(answer="예", *, round_no=1, sid="s1", purpose="eligibility", target=(KEY,), question=Q):
    return QAPair(round=round_no, question_id="Q1", question=question, answer=answer, purpose=purpose,
                  target=list(target), session_id=sid)


def short(log, *, session_id="s1", text=None, quote=f"Q: {Q} A: 예"):
    return check(PREG, quote, text or qa(Q, "예"), holds="yes", category="reproductive", key=KEY, qa_log=log,
                 session_id=session_id)


def test_linked_short_answer_confirms_only_in_own_session():
    assert short([pair()]).directness == "direct"
    assert short([pair()], session_id="s2").flags == ["answer_not_linked"]  # 다른 세션 기록
    assert short([pair(sid=None)]).flags == ["answer_not_linked"]  # session_id 없는 기록도 제외


def test_linked_short_answer_requires_same_round_and_eligibility_purpose():
    assert short([pair(round_no=2)]).flags == ["answer_not_linked"]  # 라운드마다 Q1부터 다시 매겨짐
    assert short([pair(purpose="physician_reference")]).flags == ["answer_not_linked"]
    assert short([pair(target=("T1:E9",))]).flags == ["answer_not_linked"]


def test_unlinked_short_answer_never_confirms():
    assert short(None, session_id=None).flags == ["answer_not_linked"]  # legacy 기본: qa_log를 넘기지 않음


def test_answer_revision_on_same_question_blocks_both_answers():
    text = qa(Q, "예") + "\n\n[추가 확인 정보 - 라운드 2]\nQ: " + Q + "\nA: 아니요"
    log = [pair("예"), pair("아니요", round_no=2)]
    for quote, holds in ((f"Q: {Q} A: 예", "yes"), (f"Q: {Q} A: 아니요", "no")):
        c = check(PREG, quote, text, holds=holds, category="reproductive", key=KEY, qa_log=log, session_id="s1")
        assert not c.ok and c.flags == ["conflicting_answers"]


def test_non_answer_is_not_counted_as_revision():
    text = qa(Q, "모름") + "\n\n[추가 확인 정보 - 라운드 2]\nQ: " + Q + "\nA: 예"
    log = [pair("모름"), pair("예", round_no=2)]
    c = check(PREG, f"Q: {Q} A: 예", text, holds="yes", category="reproductive", key=KEY, qa_log=log, session_id="s1")
    assert c.directness == "direct"


def test_other_session_revision_does_not_leak():
    log = [pair("예"), pair("아니요", round_no=2, sid="s2")]
    assert short(log).directness == "direct"
