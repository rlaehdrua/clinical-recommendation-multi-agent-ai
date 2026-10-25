"""P14 실제 API 검증 D1: '대상 집단 아님'(applicable=False) 판정이 근거 대상·극성 검사를 받지 않던 우회. 오프라인, 유료 API 없음.

실제 API(e8a6928, CLI 2회차, SYN-S005-03 x NCT04063540 I4)에서 묶인 선택지 "폐경되었거나 임신 가능성이 없음"을 고른 답으로
I4(가임 여성 피임 기준)가 '해당 없음' 통과됨. 원인: check()의 not applicable 분기는 개념 정규식(임신|폐경 ...)이 근거 절에 맞기만
하면 direct로 통과시켰음. 수정: 임신·수유 개념군은 대상 집단 밖임을 정하는 사실(폐경·자궁 적출 등)을 근거가 긍정으로, 이미 일어난
일로, 다른 상태와 묶지 않고 말할 때만 '해당 없음'을 인정 (판정을 올리지 않고, 걸리면 unknown으로 되돌림).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import evidence_directness as ed
from ctrec.agents import matcher
from ctrec.schemas import LLMAssessment, MatcherOutput, ParsedTrial, QAPair

from test_offline import _profile, _rule

ROOT = Path(__file__).resolve().parent.parent
SID = "S-d1"
WOCBP = "Women of childbearing potential must use effective contraception"


def _i4() -> str:
    rec = json.loads((ROOT / "data/parsed/NCT04063540.json").read_text(encoding="utf-8"))
    rules = rec.get("rules") or rec["parsed"]["rules"]
    return next(r["text"] for r in rules if r["rule_id"] == "I4")


def _s005_03() -> str:
    cases = json.loads((ROOT / "data/patients/synthetic/cases_S005-S006.json").read_text(encoding="utf-8"))
    cases = cases if isinstance(cases, list) else cases.get("topics", cases.get("cases", []))
    return next(c for c in cases if c["num"] == "SYN-S005-03")["title"]


def _qa(question: str, answer: str, base: str = "44세 여성.", key: str = "T:I4"):
    qa = [QAPair(round=1, question_id="Q1", question=question, answer=answer, target=[key], session_id=SID)]
    return f"{base}\n\n[추가 확인 정보 - 라운드 1]\nQ: {question}\nA: {answer}", qa


def _na(criterion: str, quote: str, source: str, qa=None, key: str = "T:I4") -> ed.Check:
    return ed.check(criterion, quote, source, holds="yes", applicable=False, key=key, qa_log=qa, session_id=SID,
                    reference_date="2026-10-25")


# ------------------------------------------------------------ B 보고서 재현 (handoff 문서의 repro_d1.py 그대로)
REPRO_Q = "폐경이 되었거나, 임신 가능성이 없는 상태인가요? 그렇지 않다면 신뢰할 수 있는 피임 방법을 사용하고 있나요?"


@pytest.mark.parametrize("answer, applicable, directness, flags", [
    ("임신 가능하며 신뢰할 수 있는 피임법을 3개월 이상 사용 중임", True, "object_mismatch", ["object_not_matched"]),
    ("폐경되었거나 임신 가능성이 없음", False, "hedged", ["combined_states"]),  # 수정 전: direct (우회)
    ("폐경되었거나 임신 가능성이 없음", True, "indirect", ["polarity_mismatch"]),
])
def test_handoff_repro(answer, applicable, directness, flags):
    key = "NCT04063540:I4"
    qa = [QAPair(round=1, question_id="Q2", question=REPRO_Q, answer=answer, target=[key], session_id=SID)]
    src = f"{_s005_03()}\n\n[추가 확인 정보 - 라운드 1]\nQ: {REPRO_Q}\nA: {answer}"
    c = ed.check(_i4(), answer, src, holds="yes", applicable=applicable, key=key, qa_log=qa, session_id=SID,
                 reference_date="2026-10-25")
    assert (c.directness, c.flags, c.concept) == (directness, flags, "pregnancy_lactation")
    assert applicable or not c.ok


# ------------------------------------------------------------ 근거 없는 '대상 집단 아님'
@pytest.mark.parametrize("answer", [
    "임신 가능성이 없음",  # 자기 평가: 대상 집단을 정하는 사실이 아님
    "현재 임신하지 않았음",  # 임신 상태 != 가임 가능성
    "2019년 난관결찰술을 받았음",  # 난관결찰은 일반적인 WOCBP 정의에서 제외 사유가 아님
    "임신 계획이 없음",
])
def test_not_applicable_without_population_fact_is_blocked(answer):
    src, qa = _qa("임신 가능성에 대해 알려 주세요.", answer)
    c = _na(WOCBP, answer, src, qa)
    assert not c.ok and c.directness == "indirect" and c.flags == ["population_exclusion_not_stated"]


# ------------------------------------------------------------ 올바른 직접 근거 (기존 정상 판정 유지)
@pytest.mark.parametrize("quote, source", [
    ("postmenopausal since 2015", "62-year-old woman, postmenopausal since 2015."),
    ("2015년에 폐경됨", "62세 여성. 2015년에 폐경됨."),
    ("2020년 자궁 적출술을 받음", "48세 여성. 2020년 자궁 적출술을 받음."),
    ("s/p total hysterectomy 2018", "51-year-old woman, s/p total hysterectomy 2018."),
    ("bilateral oophorectomy in 2019", "50-year-old woman, bilateral oophorectomy in 2019."),
])
def test_population_fact_record_is_direct(quote, source):
    c = _na(WOCBP, quote, source)
    assert c.ok and c.directness == "direct" and c.flags == []


def test_long_answer_with_population_fact_is_direct():
    src, qa = _qa("폐경 여부를 알려 주세요.", "2018년에 폐경되었음")
    assert _na(WOCBP, "2018년에 폐경되었음", src, qa).directness == "direct"


def test_linked_yes_to_menopause_question_is_direct():
    src, qa = _qa("폐경이 되었나요?", "예")
    assert _na(WOCBP, "Q: 폐경이 되었나요? A: 예", src, qa).directness == "direct"


# ------------------------------------------------------------ 대상(의미) 불일치·극성 불일치·시간 조건 불일치
@pytest.mark.parametrize("quote, source", [
    ("44세 여성, 폐경 전", "44세 여성, 폐경 전. 편두통."),  # 폐경 '전'은 가임 대상 집단 안
    ("premenopausal woman", "44-year-old premenopausal woman."),
    ("perimenopausal", "49-year-old perimenopausal woman."),
])
def test_premenopausal_is_not_population_exclusion(quote, source):
    c = _na(WOCBP, quote, source)
    assert not c.ok and c.flags == ["population_exclusion_not_stated"]


@pytest.mark.parametrize("question, answer", [
    ("폐경 전인가요?", "예"),  # 질문 대상 불일치 (폐경 전 = 대상 집단 안)
    ("폐경이 되었나요?", "아니요"),  # 극성 불일치
    ("폐경이 되지 않았나요?", "아니요"),  # 부정형 질문: 예/아니요의 뜻을 정하지 않음
])
def test_linked_answer_target_or_polarity_mismatch(question, answer):
    src, qa = _qa(question, answer)
    c = _na(WOCBP, f"Q: {question} A: {answer}", src, qa)
    assert not c.ok


def test_negated_population_fact_is_blocked():
    src, qa = _qa("폐경 여부를 알려 주세요.", "아직 폐경되지 않았음")
    assert not _na(WOCBP, "아직 폐경되지 않았음", src, qa).ok


@pytest.mark.parametrize("answer", ["내년에 자궁 적출술을 받을 예정임", "scheduled for hysterectomy next month"])
def test_planned_population_fact_is_blocked(answer):
    """시간 조건: 앞으로 받을 수술은 지금 대상 집단 밖이라는 근거가 아님."""
    src, qa = _qa("자궁 적출술을 받았나요?", answer)
    assert not _na(WOCBP, answer, src, qa).ok


@pytest.mark.parametrize("answer", ["폐경 또는 피임 중", "자궁 적출술을 받았거나 폐경됨"])
def test_combined_states_are_held(answer):
    """서로 다른 상태를 묶은 답은 어느 쪽인지 모름 (두 상태가 모두 제외 사유여도 보수적으로 보류)."""
    src, qa = _qa("다음 중 해당하는 것을 고르세요.", answer)
    c = _na(WOCBP, answer, src, qa)
    assert (c.directness, c.flags) == ("hedged", ["combined_states"])


def test_unlinked_short_answer_still_blocked():
    src, _ = _qa("폐경이 되었나요?", "예")
    assert _na(WOCBP, "Q: 폐경이 되었나요? A: 예", src, qa=None).directness == "unlinked"


# ------------------------------------------------------------ matcher 경로 (가짜 LLM, 결정적)
def _parsed(text: str) -> ParsedTrial:
    rule = _rule(rule_id="I4", kind="inclusion", text=text, category="reproductive", structured_evaluable=False)
    return ParsedTrial(trial_id="T", title="t", target_population="", intervention_summary="", key_conditions=[],
                       cohorts=[], rules=[rule], parsing_notes="")


def _match(monkeypatch, text: str, source: str, quote: str, qa=None):
    out = MatcherOutput(assessments=[LLMAssessment(
        rule_id="I4", applicable=False, holds="yes", evidence_type="explicit", confidence="high", evidence=quote,
        reasoning="환자가 폐경되었거나 임신 가능성이 없다고 명시되어 있어 가임기 여성 대상 조건에 해당하지 않음",
        missing_info=None)])
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=out.assessments))
    m = matcher.match_trial(_profile(age=44, sex="female", ecog=None), _parsed(text), {"brief_summary": ""},
                            reference_date="2026-10-25", source_text=source, qa=qa, session_id=SID)
    return m.assessments[0]


def test_matcher_bundled_choice_answer_is_not_auto_passed(monkeypatch):
    answer = "폐경되었거나 임신 가능성이 없음"
    src, qa = _qa(REPRO_Q, answer, base=_s005_03(), key="T:I4")
    a = _match(monkeypatch, _i4(), src, answer, qa)
    assert a.passes is None and a.applicable is True and a.holds == "unknown"
    assert a.validation_flags == ["combined_states"] and a.evidence_directness == "hedged"
    assert a.llm_judgment["applicable"] is False  # 모델 판정은 기록에 남음 (의미 변경 없음)


def test_matcher_menopause_record_still_passes(monkeypatch):
    a = _match(monkeypatch, WOCBP, "62세 여성. 2015년에 폐경됨.", "2015년에 폐경됨")
    assert a.passes is True and a.applicable is False and a.evidence_directness == "direct" and a.validation_flags == []


# ------------------------------------------------------------ 대상 집단을 정하지 않는 기준 (D1 후속)
@pytest.mark.parametrize("criterion", ["Pregnant or lactating patient", "Pregnant or breastfeeding women"])
def test_population_less_criterion_cannot_be_not_applicable(criterion):
    """"Pregnant or lactating"은 모든 환자에게 적용: 폐경 기록으로 '해당 없음' 통과하지 않음 ('폐경 -> 임신 아님'은 임상 추론).
    수정 전: 개념어(폐경)가 있어 direct로 통과."""
    c = _na(criterion, "postmenopausal since 2015", "62-year-old woman, postmenopausal since 2015.", key="T:E9")
    assert not c.ok and c.flags == ["no_target_population"]


@pytest.mark.parametrize("criterion", [WOCBP, "Females of reproductive potential must have a negative pregnancy test",
                                       "가임 여성은 피임해야 함", "I4",
                                       "Women who can become pregnant must use an approved birth control method"])
def test_population_criteria_still_accept_population_fact(criterion):
    criterion = _i4() if criterion == "I4" else criterion
    assert _na(criterion, "postmenopausal since 2015", "62-year-old woman, postmenopausal since 2015.").ok


# ------------------------------------------------------------ 독립 리뷰가 찾은 남은 구멍 (수정 전에도 통과하던 우회)
@pytest.mark.parametrize("quote", ["pre menopausal woman", "menopausal transition", "approaching menopause",
                                   "폐경기 증상(안면홍조)이 있음", "폐경기에 접어들고 있음", "폐경이 가까움", "조기 폐경 위험"])
def test_not_yet_outside_population_is_blocked(quote):
    assert not _na(WOCBP, quote, f"47세 여성. {quote}.").ok


def test_potential_substring_does_not_define_population():
    crit = "Pregnant or breastfeeding women or any condition that could potentially interfere with the study"
    c = _na(crit, "postmenopausal since 2015", "62-year-old woman, postmenopausal since 2015.", key="T:E9")
    assert not c.ok and c.flags == ["no_target_population"]


@pytest.mark.parametrize("quote", ["2015년 자궁 및 양쪽 난소 절제술", "양측 난소난관절제술을 받음"])
def test_korean_bilateral_oophorectomy_is_direct(quote):
    assert _na(WOCBP, quote, f"55세 여성. {quote}.").directness == "direct"


def test_criterion_exempting_postmenopausal_defines_population():
    crit = "Female patients must have a negative serum pregnancy test unless postmenopausal or surgically sterile"
    assert _na(crit, "postmenopausal since 2015", "62-year-old woman, postmenopausal since 2015.").ok
