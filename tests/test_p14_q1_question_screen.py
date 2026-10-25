"""P14 실제 API 검증 Q1: 답을 받아도 코드 정책상 기준을 확정할 수 없는 질문에 질문 예산을 쓰던 문제. 오프라인, 유료 API 없음.

실제 API(ad3a222, CLI 3회차, SYN-S005-03, 예산 3)의 질문 3개 중 E9 하나만 해소됨:
- Q1 yes_no "현재 예방약 복용?" -> I3(1개월 이상 미사용)·07616128:I6: 예/아니요 단답은 임신·수유·알레르기 개념군만 검증되고
  (answer_verification, 기본 Session은 matcher에 qa_log를 넘기지 않음) 중단 시점도 묻지 않음
- Q2 yes_no "임신·수유 중?" -> I4(피임)·E9: I4는 피임을 묻지 않음
- Q3 yes_no "최근 3개월 매달 조짐?" -> I2: 예/아니요 단답 + 기간 안 '있음'은 근거에 날짜가 있어야만 통과(time_window_unverified)

수정: (1) 질문 생성 프롬프트에 답변 검증 정책을 명시, (2) 생성 후 코드 검사로 답이 확정할 수 없는 대상 기준을 빼고 대상이 모두 빠진
질문은 버린 뒤 예산만큼 고름, (3) 답변이 말한 상대 기간("최근 3개월 동안")이 기준 기간 안이면 기간 조건 확인으로 인정.
가짜 LLM 응답(고정)으로 결정적으로 재현합니다. 실제 Claude의 질문 품질 개선은 실제 API 재검증(B) 전까지 확정이 아님.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import evidence_directness as ed
from ctrec.agents import question_generator as qg
from ctrec.schemas import ClarifyingQuestion, CriterionAssessment, QAPair, QuestionSet, TrialMatch

from test_recommender import _profile

ROOT = Path(__file__).resolve().parent.parent
A, B = "NCT04063540", "NCT07616128"


def _rules(tid: str) -> dict[str, dict]:
    rec = json.loads((ROOT / f"data/parsed/{tid}.json").read_text(encoding="utf-8"))
    return {r["rule_id"]: r for r in (rec.get("rules") or rec["parsed"]["rules"])}


RA, RB = _rules(A), _rules(B)


def _unknown(rule: dict, **kw) -> CriterionAssessment:
    return CriterionAssessment(rule_id=rule["rule_id"], kind=rule["kind"], criterion_text=rule["text"], holds="unknown",
                               passes=None, confidence="low", evidence="근거 없음", reasoning="", missing_info="확인 필요",
                               method="llm", category=rule["category"], **kw)


def _matches() -> list[TrialMatch]:
    a = [_unknown(RA[r]) for r in ("I2", "I3", "I4", "E9")]
    a.append(_unknown(RA["E1"]).model_copy(update={"holds": "no", "passes": True}))  # 이미 통과: 질문 대상 아님
    b = [_unknown(RB["I6"]), _unknown(RB["I5"], underspecified=True)]
    return [TrialMatch(patient_id="P", trial_id=A, eligibility="UNCERTAIN", assessments=a, n_pass=1, n_fail=0,
                       n_unknown=4, summary=""),
            TrialMatch(patient_id="P", trial_id=B, eligibility="UNCERTAIN", assessments=b, n_pass=0, n_fail=0,
                       n_unknown=2, summary="")]


def _q(qid, question, target, answer_type="yes_no", options=(), purpose="eligibility"):
    return ClarifyingQuestion(question_id=qid, question=question, answer_type=answer_type, options=list(options),
                              target=list(target), purpose=purpose, why="")


def _generate(monkeypatch, questions, max_questions=3):
    seen = {}

    def fake(cls, *, system, user, effort):
        seen["system"], seen["user"] = system, user
        return QuestionSet(questions=[q.model_copy(deep=True) for q in questions])
    monkeypatch.setattr(qg.llm, "structured", fake)
    out = qg.generate_questions(_profile(), _matches(), [], max_questions)
    return out.questions, seen


# 실제 API 3회차 질문 (handoff 문서 표 그대로)
RUN3 = [_q("Q1", "현재 편두통 예방약(예: 프로프라놀롤)을 복용하고 있나요?", [f"{B}:I6", f"{A}:I3"]),
        _q("Q2", "현재 임신 중이거나 수유 중인가요?", [f"{A}:I4", f"{A}:E9"]),
        _q("Q3", "최근 3개월 동안 매달 적어도 한 번 이상 조짐이 있었나요?", [f"{A}:I2"])]
STOP_TIME = _q("Q1", "편두통 예방약을 마지막으로 복용한 시기는 언제인가요?", [f"{A}:I3", f"{B}:I6"], "single_choice",
               ["현재 복용 중", "1개월 이내에 중단", "1~3개월 전에 중단", "3개월 이상 전에 중단", "복용한 적 없음"])
AURA_3M = _q("Q3", "최근 3개월 동안 조짐(aura)이 얼마나 자주 있었나요?", [f"{A}:I2"], "single_choice",
             ["최근 3개월 동안 매달 1회 이상 있었음", "최근 3개월 중 조짐이 없던 달이 있음", "최근 3개월 동안 없었음"])


def _targets(qs):
    return {q.question_id: q.target for q in qs}


# ------------------------------------------------------------ 생성 후 코드 검사
def test_run3_questions_are_screened(monkeypatch):
    qs, _ = _generate(monkeypatch, RUN3)
    assert _targets(qs) == {"Q2": [f"{A}:E9"]}  # Q1·Q3: 예/아니요 단답으로 확정할 수 없음, Q2: I4(피임)는 묻지 않음


def test_stop_time_choice_question_keeps_both_targets(monkeypatch):
    """예방약 중단 시점이 필요한 기준: 경계(1개월)에 맞춘 선택형이면 I3·I6 모두 유지 (4회차 형태)."""
    qs, _ = _generate(monkeypatch, [STOP_TIME])
    assert _targets(qs) == {"Q1": [f"{A}:I3", f"{B}:I6"]}


def test_current_use_choice_without_stop_time_drops_windowed_target(monkeypatch):
    q = _q("Q1", "현재 편두통 예방약을 사용하고 있나요?", [f"{A}:I3", f"{B}:I6"], "single_choice",
           ["현재 사용 중", "현재 사용하지 않음"])
    qs, _ = _generate(monkeypatch, [q])
    assert _targets(qs) == {"Q1": [f"{B}:I6"]}  # I6(현재 미사용)은 확인됨, I3(1개월 이상 미사용)은 기간을 묻지 않음


def test_aura_frequency_needs_the_three_month_window(monkeypatch):
    qs, _ = _generate(monkeypatch, [AURA_3M])
    assert _targets(qs) == {"Q3": [f"{A}:I2"]}
    no_window = _q("Q3", "조짐(aura)이 얼마나 자주 있나요?", [f"{A}:I2"], "single_choice", ["매달 1회 이상", "가끔", "없음"])
    qs, _ = _generate(monkeypatch, [no_window])
    assert qs == []


def test_contraception_and_pregnancy_are_different_facts(monkeypatch):
    contra = _q("Q2", "임신 가능성이 있는 경우, 신뢰할 수 있는 피임법을 얼마나 오래 사용했나요?", [f"{A}:I4", f"{A}:E9"],
                "single_choice", ["3개월 이상 사용 중", "3개월 미만 사용 중", "피임하지 않음", "폐경 또는 자궁 적출로 해당 없음"])
    qs, _ = _generate(monkeypatch, [contra])
    assert _targets(qs) == {"Q2": [f"{A}:I4"]}  # 임신·수유 여부(E9)는 묻지 않음


@pytest.mark.parametrize("question, answer_type", [("폐경이 되었나요?", "yes_no"),
                                                   ("자궁 적출술을 받은 적이 있나요?", "yes_no")])
def test_population_question_kept_for_i4(monkeypatch, question, answer_type):
    """프롬프트가 권하는 대상 집단 질문: '해당 없음' 근거는 기간 조건을 보지 않으므로 I4 대상 유지 (리뷰 지적)."""
    qs, _ = _generate(monkeypatch, [_q("Q1", question, [f"{A}:I4"], answer_type)])
    assert _targets(qs) == {"Q1": [f"{A}:I4"]}


def test_window_wider_than_presence_criterion_is_dropped():
    """'있음' 기준(4주 이내 수술)에 '1개월 이내' 선택지: 답의 기간이 기준보다 넓어 검증에서 보류됨 -> 대상 제외."""
    q = _q("Q1", "최근 1개월 이내에 큰 수술을 받았나요?", ["T:X"], "single_choice", ["1개월 이내에 받음", "받지 않음"])
    assert qg.unresolvable(q, "Major surgery within 4 weeks prior to enrollment") == "time_window_not_asked"
    wide = _q("Q1", "최근 3개월 동안 예방약을 복용한 적이 있나요?", ["T:X"], "single_choice",
              ["최근 3개월 동안 복용한 적 없음", "복용한 적 있음"])
    assert qg.unresolvable(wide, RA["I3"]["text"]) is None  # '없음' 기준은 더 넓은 기간이 덮음


@pytest.mark.parametrize("criterion, q", [
    ("LDH (lactate dehydrogenase) <= 2.5 x ULN", _q("Q1", "LDH 수치를 알려주세요", ["T:X"], "number")),
    ("Current use of estrogen-containing medication",
     _q("Q1", "경구 피임약 등 에스트로겐이 들어 있는 약을 현재 복용 중인가요?", ["T:X"], "single_choice",
        ["현재 복용 중", "복용하지 않음"])),
])
def test_review_overscreening_cases_are_kept(criterion, q):
    assert qg.unresolvable(q, criterion) is None


def test_partial_targets_keep_only_matching(monkeypatch):
    qs, _ = _generate(monkeypatch, [RUN3[1]])
    assert _targets(qs) == {"Q2": [f"{A}:E9"]}


def test_d1_bundled_question_keeps_only_contraception_target(monkeypatch):
    """실제 API 2회차 Q2 (D1 사례): 피임을 묻는 질문은 E9(임신·수유 여부)를 정하지 않음. 묶인 선택지 답은 D1 검사가 보류."""
    q = _q("Q2", "폐경이 되었거나, 임신 가능성이 없는 상태인가요? 그렇지 않다면 신뢰할 수 있는 피임 방법을 3개월 이상 "
           "사용하고 있나요?", [f"{A}:I4", f"{A}:E9"], "single_choice",
           ["폐경되었거나 임신 가능성이 없음", "임신 가능성이 있고 신뢰할 수 있는 피임을 하고 있음",
            "임신 가능성이 있으나 피임을 하지 않음", "현재 임신 중이거나 수유 중임"])
    qs, _ = _generate(monkeypatch, [q])
    assert _targets(qs) == {"Q2": [f"{A}:I4"]}


def test_targets_must_be_open_unknown_criteria(monkeypatch):
    q = _q("Q1", "현재 임신 중이거나 수유 중인가요?", [f"{A}:E9", f"{A}:E1", "NCT00000000:E9", "04063540:E9"])
    qs, _ = _generate(monkeypatch, [q])
    assert _targets(qs) == {"Q1": [f"{A}:E9"]}


def test_budget_prefers_resolvable_questions(monkeypatch):
    """LLM이 예산보다 많이 돌려줘도 해소할 수 없는 질문을 버린 뒤 순서대로 예산만큼 고름 (예산·질문 수를 늘리지 않음)."""
    qs, _ = _generate(monkeypatch, [RUN3[0], RUN3[2], STOP_TIME, AURA_3M, RUN3[1]], max_questions=2)
    assert [q.question for q in qs] == [STOP_TIME.question, AURA_3M.question]


def test_no_extra_questions_when_all_resolvable(monkeypatch):
    qs, _ = _generate(monkeypatch, [STOP_TIME, AURA_3M, RUN3[1]], max_questions=3)
    assert len(qs) == 3


def test_physician_reference_and_untargeted_questions_untouched(monkeypatch):
    ref = _q("Q4", "현재 복용 중인 약의 이름과 용량을 모두 알려주세요.", [f"{B}:I5"], "free_text",
             purpose="physician_reference")
    untargeted = _q("Q5", "EGFR 검사 결과를 아시나요?", [])
    qs, _ = _generate(monkeypatch, [ref, untargeted])
    assert _targets(qs) == {"Q4": [f"{B}:I5"], "Q5": []}


@pytest.mark.parametrize("text, days", [
    (RA["I2"]["text"], {90}), (RA["I3"]["text"], {30}), (RA["I4"]["text"], {90}), (RB["I6"]["text"], set()),
    ("No rituximab or obinutuzumab in prior 6 months.", {180}),
    ("Those who have participated in other clinical trials within three months prior to the enrollment", {90}),
    ("Idiopathic urticaria within the past year.", {365}),
    ("Age 18 years or older", set()), ("최근 4주 이내 수술", {28}),
    ("Age 18 years or older with prior platinum-based chemotherapy", set()),
    ("aged at least 18 years before signing consent", set()),
])
def test_criterion_windows(text, days):
    assert qg.criterion_windows(text) == days


def test_prompt_states_answer_verification_policy(monkeypatch):
    _, seen = _generate(monkeypatch, [])
    s = seen["system"]
    for phrase in ("예/아니요 단답", "기간", "중단 시점", "묶지", "피임", "target에는"):
        assert phrase in s, phrase
    assert "<trial_match_status>" in seen["user"]  # 입력 스키마는 그대로


# ------------------------------------------------------------ 답변 후 해소 / 보류 (evidence_directness 시간 검사)
SID = "S-q1"


def _answer(question: str, answer: str, base: str = "44세 여성, 조짐 편두통."):
    return f"{base}\n\n[추가 확인 정보 - 라운드 1]\nQ: {question}\nA: {answer}"


def _i2(quote: str, src: str) -> ed.Check:
    return ed.check(RA["I2"]["text"], quote, src, holds="yes", category="diagnosis", time_window_days=90,
                    reference_date="2026-10-25", key=f"{A}:I2")


def test_answer_with_window_resolves_aura_criterion():
    ans = "최근 3개월 동안 매달 1회 이상 있었음"
    assert _i2(ans, _answer(AURA_3M.question, ans)).ok  # 수정 전: time_unverified


@pytest.mark.parametrize("question, answer", [
    ("조짐이 얼마나 자주 있나요?", "매달 1회 이상 조짐이 있었음"),  # 기간을 어디에도 말하지 않음
    # 질문의 기간은 쓰지 않음: 답이 기간을 직접 말해야 함 (답이 다른 때를 말할 수 있음 - 독립 리뷰 지적)
    ("최근 3개월 동안 조짐이 얼마나 자주 있었나요?", "매달 1회 이상 조짐이 있었음"),
    ("최근 6개월 동안 조짐이 얼마나 자주 있었나요?", "최근 6개월 동안 매달 1회 이상 있었음"),  # 기준(3개월)보다 넓은 기간
    ("최근 3개월 동안 조짐이 얼마나 자주 있었나요?", "최근 3개월 동안 없었음"),  # '있음' 주장에 부정 답
    ("조짐이 있었던 시기를 알려 주세요.", "6개월간 매달 조짐이 있었음"),  # 기간 '길이'는 최근임을 말하지 않음
    ("최근 3개월 동안 조짐이 있었나요?", "2026-05에 마지막으로 있었음"),  # 날짜가 있으면 날짜로 판단 (기간 밖)
    ("최근 3개월 동안 조짐이 얼마나 자주 있었나요?", "1년 전에 마지막으로 있었음"),  # 답이 다른 시점을 말함
    ("최근 3개월 동안 조짐이 얼마나 자주 있었나요?", "작년에 매달 있었음"),
])
def test_answer_still_held_when_time_not_established(question, answer):
    c = _i2(answer, _answer(question, answer))
    assert not c.ok and c.flags == ["time_window_unverified"]


SURGERY = "Major surgery within 28 days prior to enrollment"
SURGERY_Q = "최근 4주 이내에 큰 수술을 받은 적이 있나요?"


@pytest.mark.parametrize("question, answer", [
    (SURGERY_Q, "네, 고등학생 때 맹장 수술을 받았어요"),
    (SURGERY_Q, "네, 두 달 전에 수술을 받았어요"),
    (SURGERY_Q, "네, 어렸을 때 수술을 받았어요"),
    (SURGERY_Q, "수술을 받았어요, 지난달 초에요"),
    (SURGERY_Q, "네, 다음 주에 수술 받을 예정이에요"),
    ("수술 이력을 알려 주세요.", "2주 이내에 수술을 받을 예정입니다"),  # 앞으로의 일
    ("수술 이력을 알려 주세요.", "항암 종료 후 2주 이내에 수술을 받았습니다"),  # 다른 사건 기준의 기간
    ("최근 4주 이내가 아니라 그 이전에 수술을 받았나요?", "네, 수술을 받았어요"),
    ("2026년 10월 1일 이전에 마지막으로 수술을 받으셨나요?", "네, 수술을 받았어요"),  # 질문의 날짜는 답이 아님
    ("수술 이력을 알려 주세요.", "4주 이내에 수술 받았고 어렸을 때도 받음"),  # 다른 시점을 함께 말함
])
def test_adversarial_answers_do_not_satisfy_time_window(question, answer):
    """독립 리뷰가 찾은 반례: 수정 전(ad3a222)과 같이 time_unverified로 남아야 함 (판정을 올리지 않음)."""
    c = ed.check(SURGERY, answer, _answer(question, answer), holds="yes", reference_date="2026-10-25")
    assert not c.ok and c.flags == ["time_window_unverified"]


def test_surgery_answer_within_window_is_accepted():
    ans = "4주 이내에 큰 수술을 받았음"
    assert ed.check(SURGERY, ans, _answer("수술 시기를 알려 주세요.", ans), holds="yes", reference_date="2026-10-25").ok


def test_record_relative_window_is_not_accepted():
    """처음 입력(기록)의 '최근 3개월'은 작성 시점을 모름: 라운드 머리말 아래 답변에만 상대 기간을 인정."""
    src = "44세 여성. 최근 3개월 동안 매달 1회 이상 조짐 있음."
    assert _i2("최근 3개월 동안 매달 1회 이상 조짐 있음", src).flags == ["time_window_unverified"]
    soap = "S: 조짐 편두통 (2024년 외래 기록)\nO: 신경학적 검사 정상\nA: 최근 3개월 동안 매달 1회 이상 조짐 있음"
    assert _i2("최근 3개월 동안 매달 1회 이상 조짐 있음", soap).flags == ["time_window_unverified"]


def test_stop_time_answer_resolves_negated_criterion():
    """I3(1개월 이상 미사용, 부정형 기준): 중단 시점을 담은 선택지 답은 기존 경로대로 근거가 됨."""
    ans = "3개월 이상 전에 중단"
    c = ed.check(RA["I3"]["text"], ans, _answer(STOP_TIME.question, ans), holds="yes", category="treatment_history",
                 time_window_days=30, reference_date="2026-10-25")
    assert c.ok


def test_yes_no_answer_to_non_concept_criterion_stays_unlinked():
    """기본 Session(qa_log 없음)에서 예/아니요 단답은 개념군 밖 기준의 근거가 되지 않음 (P12 정책 유지)."""
    src = _answer(RUN3[0].question, "아니요")
    c = ed.check(RB["I6"]["text"], f"Q: {RUN3[0].question} A: 아니요", src, holds="yes", key=f"{B}:I6")
    assert c.directness == "unlinked"
