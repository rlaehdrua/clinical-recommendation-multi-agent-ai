"""환자값 근거 공격 사례: 근거는 이 세션의 원문과 문답에 실제로 연결되어야 함 (합성 데이터, LLM 호출 없음)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_p0_lab_semantics_session import ranked  # noqa: E402
from test_p0_session_grounding import (AGE, HB, OPEN, Q_AGE, Q_HB, Q_SEX, SELF_OK, SEX, TOO_OLD, confirmed, hb, held, prof, run)  # noqa: E402,F401

from ctrec import value_grounding as vg  # noqa: E402
from ctrec.schemas import QAPair  # noqa: E402

U = "unlocated"


def pair(question, answer, target="T1:I1", session="S1", purpose="eligibility", rnd=1):
    return QAPair(round=rnd, question_id=f"Q{rnd}", question=question, answer=answer, purpose=purpose, target=[target], session_id=session)


def block(*rows):
    return "기록." + "".join(f"\n\n[추가 확인 정보 - 라운드 {i}]\nQ: {q}\nA: {a}" for i, (q, a) in enumerate(rows, 1))


def test_bare_answer_from_a_qa_that_is_not_in_this_source_is_not_evidence():
    assert vg.ground("age", 62, block(("흡연하시나요?", "아니요")), key="T1:I1", qa=[pair(Q_AGE, "62", session="OTHER")]) == U
    assert vg.ground("sex", "female", "기록.", qa=[pair(Q_SEX, "F", session="OTHER")]) == U
    assert vg.ground_lab("hemoglobin", 12.0, "12", "기록.", key="T1:I3", qa=[pair(Q_HB, "12", "T1:I3", session="OTHER")]) == U


def test_bare_answer_in_this_source_is_evidence():
    assert vg.ground("age", 62, block((Q_AGE, "62")), key="T1:I1", qa=[pair(Q_AGE, "62")]) is None
    assert vg.ground_lab("hemoglobin", 12.0, "12", block((Q_HB, "12")), key="T1:I3", qa=[pair(Q_HB, "12", "T1:I3")]) is None


def test_two_bare_answers_that_disagree_are_a_conflict():
    src = block((Q_AGE, "62"), ("다시 나이는요?", "70"))
    qa = [pair(Q_AGE, "62"), pair("다시 나이는요?", "70", rnd=2)]
    assert vg.ground("age", 62, src, key="T1:I1", qa=qa) == "conflict"


def test_reference_purpose_bare_answer_is_not_evidence():
    assert vg.ground("age", 62, block(("참고로 나이는요?", "62")), key="T1:I1", qa=[pair("참고로 나이는요?", "62", purpose="physician_reference")]) == U


@pytest.mark.parametrize("field,value,text", [
    ("age", 62, "62세 남성, 흡연력 없음"), ("age", 62, "62세 남성 흡연력 없음"), ("age", 62, "62-year-old man who is not a smoker"), ("age", 62, "62세 남성으로 수혈 이력 있음"),
    ("sex", "male", "male, not on chemotherapy"), ("sex", "male", "남성 환자이며 흡연 안 함"), ("sex", "female", "62세 여성이며 임신 아님"), ("ecog", 1, "ECOG 1, 통증 없음")])
def test_other_content_in_the_clause_does_not_block_a_stated_value(field, value, text):
    assert vg.ground(field, value, text) is None


@pytest.mark.parametrize("answer", ["모르겠는데 62세일 수도", "글쎄요 62?", "62세 같기도 하고 아닌 것 같기도 해요"])
def test_vague_answer_gives_no_number(run, answer):
    held(run("폐암 환자.", [AGE], prof(), prof(age=62), answer, question=Q_AGE), "INFO_MISSING")


def test_certain_and_uncertain_facts_in_two_sentences(run):
    answer = "나이는 62세입니다. 혈색소는 12쯤인 것 같아요."
    o = run("폐암 환자.", [AGE, HB], prof(), prof(age=62, labs=[hb(12.0, "혈색소는 12쯤인 것 같아요")]), answer, meta=SELF_OK, question="나이와 혈색소는요?")
    by = {a.rule_id: a for a in o.match.assessments}
    assert by["I1"].passes is True and by["I3"].passes is None and o.match.eligibility == "UNCERTAIN"


def test_answer_about_another_item_gives_no_value_for_this_one(run):
    held(run("폐암 환자.", [AGE], prof(), prof(age=70), "혈색소는 12예요", question=Q_AGE), "INFO_MISSING")


def test_answer_to_another_question_is_a_real_statement_about_its_own_item(run):
    confirmed(run("폐암 환자.", [HB], prof(), prof(labs=[hb()]), "Hb 12 g/dL", meta=SELF_OK, question="흡연하시나요?", target="T1:I3"))


def test_denied_age_does_not_rule_out(run):
    held(run("80세 아님.", [TOO_OLD], prof(age=80)), "INFO_MISSING")


def test_session_held_value_is_not_recommended_as_a_match(run, monkeypatch):
    o = run("폐암 환자.", [AGE, OPEN], prof(age=60))
    assert ranked(o, monkeypatch).ranked[0].match_status == "INSUFFICIENT_INFORMATION"
