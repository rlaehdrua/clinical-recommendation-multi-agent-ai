"""날짜가 다른 검사값, 상충 자료, 확인 답변 뒤 재평가에서 근거가 어떻게 연결되는가 (합성 데이터, LLM 호출 없음).

C2는 알려진 불일치입니다. 표시 코드는 이 파일의 범위가 아니므로 xfail(strict)로 관찰만 합니다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_decisions import TRIAL, _parsed  # noqa: E402
from test_p0_grounding_attacks import block, pair  # noqa: E402
from test_p0_lab_semantics_session import ranked  # noqa: E402
from test_p0_session_grounding import (HB, OPEN, PID, Q_AGE, Q_HB, Q_SEX, SELF_OK, Tracer, confirmed, hb, held, prof, run)  # noqa: E402,F401

from ctrec import config, pipeline, status, value_grounding as vg  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.schemas import ClarifyingQuestion, LabValue, LLMAssessment, MatcherOutput, QuestionSet  # noqa: E402
from ctrec.web import display  # noqa: E402

U = "unlocated"
OLD, NEW = "2026-01-01", "2026-03-01"


def lab(value, day, quote=None):
    return LabValue(name="hemoglobin", value=value, unit="g/dL", date=day, source_quote=quote or f"Hb {value:g} g/dL")


LOW_THEN_HIGH = f"Hb 8 g/dL ({OLD}). Hb 12 g/dL ({NEW})."
HIGH_THEN_LOW = f"Hb 12 g/dL ({OLD}). Hb 8 g/dL ({NEW})."


# ------------------------------------------------------------ 다른 세션의 문답
def test_other_session_pair_with_the_same_answer_text_is_not_evidence():
    src = block(("흡연량은 몇 갑년인가요?", "62"))
    assert vg.ground("age", 62, src, key="T1:I1", qa=[pair(Q_AGE, "62", session="OTHER")]) == U
    src = block(("혈소판은요?", "12"))
    assert vg.ground_lab("hemoglobin", 12.0, "12", src, key="T1:I3", qa=[pair(Q_HB, "12", "T1:I3", session="OTHER")]) == U
    src = block(("다른 항목은요?", "F"))
    assert vg.ground("sex", "female", src, qa=[pair(Q_SEX, "F", session="OTHER")]) == U


def test_this_sessions_pair_still_counts():
    assert vg.ground("age", 62, block((Q_AGE, "62")), key="T1:I1", qa=[pair(Q_AGE, "62")]) is None


# ------------------------------------------------------------ 날짜
@pytest.mark.parametrize("text,day,expected", [
    (HIGH_THEN_LOW, NEW, U),  # 옛 값에 새 날짜를 붙임
    (HIGH_THEN_LOW, OLD, None),
    ("Hb 12 g/dL (2026.03.01)", NEW, None),  # 표기만 다름
    ("Hb 12 g/dL", NEW, None),  # 원문에 날짜 없음: 비교할 수 없음
    ("3월 1일 Hb 12 g/dL", NEW, None),  # 연도 없는 날짜: 비교하지 않음
    ("Hb 12 g/dL (2026-03-01)", "날짜 미상", None),
    ("Hb 12 g/dL 2026-01-01 2026-03-01", NEW, None)])  # 절에 날짜가 둘: 어느 쪽인지 알 수 없음
def test_lab_date_must_match_the_date_written_with_the_value(text, day, expected):
    quote = "Hb 12 g/dL"
    assert vg.ground_lab("hemoglobin", 12.0, quote, text, unit="g/dL", known={"12", "8"}, stated_date=day) == expected


def test_swapped_dates_do_not_pick_the_old_value_as_latest(run):
    held(run(HIGH_THEN_LOW, [HB], prof(labs=[lab(12.0, NEW), lab(8.0, OLD)]), meta=SELF_OK), "VERIFICATION_PENDING")


def test_latest_failing_value_rules_out_with_its_real_date(run):
    o = run(HIGH_THEN_LOW, [HB], prof(labs=[lab(12.0, OLD), lab(8.0, NEW)]), meta=SELF_OK)
    assert (o.match.eligibility, o.status) == ("INELIGIBLE", "VERIFICATION_PENDING")


@pytest.mark.parametrize("text,value", [(LOW_THEN_HIGH, 12.0), (HIGH_THEN_LOW, 12.0)])
def test_copying_one_of_two_dated_values_is_held(run, text, value):
    held(run(text, [HB], prof(labs=[lab(value, NEW)]), meta=SELF_OK), "INFO_MISSING")


@pytest.mark.parametrize("text,labs", [
    ("Hb 8. Hb 12.", [lab(8.0, None), lab(12.0, None)]),  # 날짜 없음
    (f"Hb 8. Hb 12 ({NEW}).", [lab(8.0, None), lab(12.0, NEW)]),  # 한쪽만 날짜
    (f"{NEW} Hb 8. {NEW} Hb 12.", [lab(8.0, NEW), lab(12.0, NEW)])])  # 같은 날짜
def test_values_that_cannot_be_ordered_are_held(run, text, labs):
    held(run(text, [HB], prof(labs=labs), meta=SELF_OK), "VERIFICATION_PENDING")


# ------------------------------------------------------------ 확인 답변 뒤 재평가
def test_answer_that_differs_from_the_record_is_held(run):
    o = run("Hb 8 g/dL", [HB], prof(), prof(labs=[lab(12.0, None, "오늘 Hb 12 g/dL")]), "오늘 Hb 12 g/dL", meta=SELF_OK, question=Q_HB, target="T1:I3")
    held(o, "INFO_MISSING")


def test_newer_dated_answer_is_not_treated_as_replacing_the_record(run):
    """답변이 더 최근이어도 기록과 다르면 사람이 확인: 답변이 기록을 대체하는 규칙은 정해진 적이 없음."""
    labs = [lab(8.0, OLD), lab(12.0, NEW, f"{NEW} Hb 12 g/dL")]
    o = run(f"{OLD} Hb 8 g/dL", [HB], prof(), prof(labs=labs), f"{NEW} Hb 12 g/dL", meta=SELF_OK, question=Q_HB, target="T1:I3")
    held(o)


def test_value_invented_after_a_non_answer_stays_held(run):
    held(run("폐암 환자.", [HB], prof(labs=[hb()]), prof(labs=[hb()]), "모름", meta=SELF_OK, question=Q_HB, target="T1:I3"), "INFO_MISSING")


def test_clear_answer_confirms_after_reevaluation(run):
    confirmed(run("폐암 환자.", [HB], prof(), prof(labs=[hb(12.0, "Hb 12 g/dL")]), "Hb 12 g/dL", meta=SELF_OK, question=Q_HB, target="T1:I3"))


def two_trials(monkeypatch, rules_a, rules_b, before, after, answer):
    """T1은 첫 평가에서 끝나고 T2만 질문 대상이 되는 세션."""
    parsed = {"T1": _parsed([r.model_copy() for r in rules_a]), "T2": _parsed([r.model_copy() for r in rules_b])}
    parsed["T2"].trial_id = "T2"
    for p in parsed.values():
        p.rule_meta = dict(SELF_OK)

    def stub(model, *, system=None, user=None, **k):
        rs = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
        return MatcherOutput(assessments=[LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent", confidence="low",
                                                        evidence="근거 없음", reasoning="stub", missing_info="확인 필요") for r in rs])

    def questions(*a, **k):
        return QuestionSet(questions=[ClarifyingQuestion(question_id="Q1", question=Q_HB, answer_type="free_text", target=["T2:I9"], purpose="eligibility", why="x")])

    class Ans:
        def answer(self, qs, pid):
            return {q.question_id: answer for q in qs}

    monkeypatch.setattr(matcher.llm, "structured", stub)
    monkeypatch.setattr(config, "WRITE_REPORT", False)
    monkeypatch.setattr(pipeline.trial_store, "get", lambda t: parsed[t["trial_id"]])
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", lambda pid, raw, reference_date=None: after if "[추가 확인 정보" in raw else before)
    monkeypatch.setattr(pipeline.question_generator, "generate_questions", questions)
    t2 = {**TRIAL, "trial_id": "T2"}
    s = Session(case=PatientCase(PID, "Hb 12 g/dL", input_source="unknown"), trials={"T1": dict(TRIAL), "T2": t2}, answerer=Ans(), tracer=Tracer())
    s.profile_patient()
    s.match()
    s.clarify()
    return s


def test_conflicting_answer_reaches_a_trial_already_confirmed(monkeypatch):
    """재평가는 판정 불가 시험만이 아니라 적격 후보 전체를 다시 봄 (Session.open_candidates)."""
    s = two_trials(monkeypatch, [HB], [HB, OPEN], prof(labs=[hb()]), prof(labs=[hb(8.0, "오늘 Hb 8 g/dL")]), "오늘 Hb 8 g/dL")
    assert s.qa_log and {t: m.eligibility for t, m in s.matches.items()} == {"T1": "UNCERTAIN", "T2": "UNCERTAIN"}


# ------------------------------------------------------------ C2: 엔진은 확인 대기, 화면은 공개 기준 충족
def c2_case(run):
    return run(LOW_THEN_HIGH, [HB], prof(labs=[lab(8.0, OLD), lab(12.0, NEW)]), meta=SELF_OK)


def test_c2_layers_as_they_are_now(run, monkeypatch):
    """값 연결은 맞음: 최신 값이 원문에 있고 날짜도 같음. 어긋남은 근거 층이 아니라 표시 층입니다."""
    o = c2_case(run)
    assert o.status == "VERIFICATION_PENDING" and "VERIFY_RECORD" in o.actions
    assert o.a.passes is True and not o.a.validation_flags


@pytest.mark.xfail(strict=True, reason="C2: 추천·표시가 PairStatus를 읽지 않음. 수정은 N5 소관 (status.py, recommender.py, web/display.py). 고쳐지면 이 표시를 지울 것")
def test_c2_pending_pair_is_not_shown_as_a_criteria_match(run, monkeypatch):
    o = c2_case(run)
    rec = ranked(o, monkeypatch)
    assert o.status == "VERIFICATION_PENDING"
    assert status.external_status(o.match) != "CRITERIA_MATCH" and rec.ranked[0].match_status != "CRITERIA_MATCH"


@pytest.mark.xfail(strict=True, reason="C2: 화면 묶음에도 확인 대기 표시가 없음. 값 상충 기준은 충족으로만 묶임. 수정은 N5 소관")
def test_c2_web_view_tells_the_reader_to_check_the_record(run, monkeypatch):
    o = c2_case(run)
    ranked(o, monkeypatch)
    view = display.build(o.session)["trials"]["T1"]
    assert view["groups"]["I3"] != display.MET or view["record_checks"]
