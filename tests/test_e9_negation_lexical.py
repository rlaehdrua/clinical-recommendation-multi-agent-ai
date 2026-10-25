"""E9(evidence_directness) 부정어 판별 회귀 테스트 (P8).

P7 발견: 부정 표현을 부분 문자열로 찾아 '산부인과'의 '부인'(婦人)을 부정어(否認)로 읽음. 결과는 양방향이었음
- 정상 판정 손실: 기록 "현재 임신 12주로 산부인과 추적 중" + 판정 '임신 중'(holds=yes) -> polarity_mismatch로 판정 불가
- 잘못된 통과: 같은 기록 + 잘못된 판정 '임신 아님'(holds=no) -> 극성이 '없음'과 맞는다고 보아 direct(통과)
수정: 한국어 부정 표현을 어절 단위로 판별 (문법 부정 형태소 / 굳은 '~없이' 부사 / 한자어 부정 명사 + 어미).
E9는 보조 안전장치이며 의미 판정기가 아닙니다. 이 테스트는 표기 판별만 확인합니다.
"""

from __future__ import annotations

import pytest

from ctrec.evidence_directness import _clause, _negated, check, polarity

NEGATIVE = [  # 실제 부정 표현 (붙여 쓴 기록·환자 진술·의료진 서술)
    "임신 아님", "임신아님", "이상없음", "특이 소견 없음", "알레르기 없어요", "수술 받지 않았어요", "임신이 아니에요",
    "흡연 부인함", "약물 알레르기 부인", "흡연력은 부인하였음", "HBsAg 음성", "임신 반응 검사 음성으로 확인",
    "검사 결과 음성입니다", "안 받았어요", "못 받았어요", "denies smoking", "no prior surgery", "환자는 '임신 아니다'라고 함",
]
NOT_NEGATIVE = [  # 부정 글자열이 다른 낱말의 일부 (婦人, 音聲, 굳은 부사)
    "산부인과 추적 중", "부인과 진료 중", "부인암 수술력 있음", "산부인과적 검사 시행", "부인께서 동반 내원",
    "음성인식 기록 제출", "음성메시지로 전달", "끊임없이 통증 호소", "틀림없이 임신 중", "변함없이 임신 유지 중",
    "빠짐없이 복용 중", "안정 상태", "안과 진료 중",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_real_negations_are_detected(text):
    assert _negated(text) and polarity(text) == "negative"


@pytest.mark.parametrize("text", NOT_NEGATIVE)
def test_negation_substrings_inside_other_words_are_not_negation(text):
    assert not _negated(text) and polarity(text) == "positive"


def test_standalone_eumseong_stays_negation_known_ambiguity():
    """띄어 쓴 '음성'(陰性/音聲)은 표기로 구분할 수 없어 이전처럼 부정으로 둠 (확정 판정을 막는 쪽: 알려진 한계)."""
    assert _negated("음성 녹음 파일 제출")


PREG = "Pregnant participants"
TEXT = "45세 여성. 현재 임신 12주로 산부인과 추적 중."
QUOTE = "현재 임신 12주로 산부인과 추적 중"


def test_gynecology_word_keeps_correct_pregnancy_judgment():
    assert check(PREG, QUOTE, TEXT, holds="yes").directness == "direct"


def test_gynecology_word_no_longer_lets_wrong_absence_pass():
    """수정 전에는 '임신 아님'(holds=no)이 direct로 통과했음 (잘못된 통과 방향)."""
    c = check(PREG, QUOTE, TEXT, holds="no")
    assert not c.ok and "polarity_mismatch" in c.flags


@pytest.mark.parametrize("text,quote,holds,ok", [
    ("변함없이 임신 유지 중.", "변함없이 임신 유지 중", "yes", True),
    ("변함없이 임신 유지 중.", "변함없이 임신 유지 중", "no", False),
    ("현재 임신 아님(산부인과 확인).", "현재 임신 아님(산부인과 확인)", "no", True),
    ("현재 임신 아님(산부인과 확인).", "현재 임신 아님(산부인과 확인)", "yes", False),
])
def test_lexical_words_and_real_negation_in_same_record(text, quote, holds, ok):
    assert check(PREG, quote, text, holds=holds).ok is ok


def test_mixed_polarity_sentence_is_split_by_clause():
    """긍정·부정 공존: 절 단위로 극성을 봄. 수유 기준은 '수유 중' 절, 임신 기준은 '임신은 아니지만' 절."""
    text = "임신은 아니지만 현재 수유 중입니다."
    assert polarity(_clause(text, text.index("수유"), text.index("수유") + 2)) == "positive"
    assert polarity(_clause(text, 0, 2)) == "negative"
    assert check("Breastfeeding participants", "현재 수유 중입니다", text, holds="yes").ok
    assert check(PREG, "임신은 아니지만", text, holds="no").ok


def test_different_concepts_in_one_record():
    text = "현재 임신 12주로 산부인과 추적 중, 조영제 알레르기 없음."
    assert check(PREG, QUOTE, text, holds="yes").directness == "direct"
    assert check("Known allergy to contrast agents", "조영제 알레르기 없음", text, holds="no").ok


def test_patient_statement_and_clinician_narrative_clauses():
    """인용된 환자 진술과 의료진 서술이 한 문장에 있으면 절로 나뉘고 각각의 극성을 가짐 (상충 판단은 기존 _conflicts)."""
    s = "환자는 흡연을 부인하였으나 보호자는 매일 흡연한다고 함"
    lo = s.index("보호자")
    assert polarity(_clause(s, 0, 3)) == "negative"
    assert polarity(_clause(s, lo, lo + 3)) == "positive"
