"""P14 실제 API 검증 D4: 의사 참고용(physician_reference) 질문의 답이 판정 근거로 쓰이던 결함. 오프라인, 유료 API 없음.

웹 실제 API(ad3a222)에서 "현재 복용 중인 모든 약…"(physician_reference, 화면 배지 "의료진 참고용 · 판정에 쓰지 않음")의 서술형 답이
NCT04063540 E3·E6 충족의 근거로 인용됨. 원인: 목적 필터(eligibility_answers)는 짧은 답의 연결 검사에만 쓰이고, 긴 답은
_evidence_units()에서 목적을 보지 않았음. 또 기본 Session은 check()에 문답 기록을 넘기지 않아 목적을 알 수 없었음.
수정: Session.match가 이미 넘기는 세션 문답(qa)을 check()까지 전달하고, 판정용(eligibility)이 아닌 질문의 답 - 의사 참고용과
목적을 밝히지 않은 기록(과거 기록) - 은 짧든 길든 근거에서 뺌. 규칙 엔진의 수치 인용도 같음. 판정을 올리지 않음.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import evidence_directness as ed
from ctrec.agents import matcher
from ctrec.schemas import LabValue, LLMAssessment, MatcherOutput, ParsedTrial, QAPair

from test_offline import _profile, _rule

ROOT = Path(__file__).resolve().parent.parent
A = "NCT04063540"
SID = "S-d4"
MEDS_Q = "현재 복용 중인 모든 약(처방약, 일반의약품, 한약, 건강기능식품 포함)의 이름과 용량을 알려주세요."
LONG = "이뇨제, 칼륨 보충제, 리튬, ACE억제제나 ARB 혈압약, 사이클로스포린, 타크로리무스, QT 연장 약물은 복용하지 않습니다."
BASE = "A 44-year-old woman with migraine with aura."


def _rules() -> dict[str, dict]:
    rec = json.loads((ROOT / f"data/parsed/{A}.json").read_text(encoding="utf-8"))
    return {r["rule_id"]: r for r in (rec.get("rules") or rec["parsed"]["rules"])}


R = _rules()


def _pair(answer=LONG, question=MEDS_Q, *, purpose="physician_reference", target=(f"{A}:E3",), round_=1, qid="Q3"):
    kw = {} if purpose is None else {"purpose": purpose}  # None: 목적을 밝히지 않은 과거 기록
    return QAPair(round=round_, question_id=qid, question=question, answer=answer, target=list(target),
                  session_id=SID, **kw)


def _src(*pairs, base=BASE):
    rounds = {}
    for p in pairs:
        rounds.setdefault(p.round, []).append(f"Q: {p.question}\nA: {p.answer}")
    return base + "".join(f"\n\n[추가 확인 정보 - 라운드 {r}]\n" + "\n".join(b) for r, b in sorted(rounds.items()))


def _e3(quote, src, *, qa=None, qa_log=None, holds="no"):
    return ed.check(R["E3"]["text"], quote, src, holds=holds, key=f"{A}:E3", qa_log=qa_log, qa=qa, session_id=SID,
                    reference_date="2026-10-25")


# ------------------------------------------------------------ B 인계 재현 (repro_d4.py 그대로: qa_log로 목적 전달)
@pytest.mark.parametrize("purpose, ok, flags", [
    ("physician_reference", False, ["physician_reference_answer"]),  # 수정 전: unchecked (통과)
    ("eligibility", True, []),
])
def test_handoff_repro(purpose, ok, flags):
    p = _pair(purpose=purpose)
    c = _e3(LONG, _src(p), qa_log=[p])
    assert c.ok is ok and c.flags == flags


# ------------------------------------------------------------ 운영 경로 (Session은 qa_log 없이 qa만 넘김)
def test_reference_long_answer_blocked_via_session_qa():
    p = _pair()
    c = _e3(LONG, _src(p), qa=[p])
    assert (c.directness, c.flags) == ("unlinked", ["physician_reference_answer"])


def test_reference_short_answer_blocked():
    p = _pair("아니요", "이뇨제나 칼륨 보충제를 복용하고 있나요?")
    c = _e3(f"Q: {p.question} A: 아니요", _src(p), qa=[p], qa_log=[p])
    assert not c.ok and c.flags == ["answer_not_linked"]  # 짧은 답은 기존 연결 검사(목적 조건 포함)가 막음


def test_same_text_in_eligibility_and_reference_records_is_reference():
    """같은 라운드·질문·답이 참고용으로도 기록돼 있으면 판정용으로 보지 않음 (보수적)."""
    ref, elig = _pair(), _pair(purpose="eligibility")
    assert not _e3(LONG, _src(ref), qa=[ref, elig]).ok


# ------------------------------------------------------------ 목적을 밝히지 않은 기록 (과거 Q&A) / 기록에 없는 답
def test_purpose_unset_record_is_not_evidence():
    p = _pair(purpose=None)
    assert p.purpose == "eligibility" and ed.declared_purpose(p) == "unknown"  # 기본값으로 판정용이 되지 않음
    c = _e3(LONG, _src(p), qa=[p])
    assert not c.ok and c.flags == ["answer_purpose_unknown"]


def test_purpose_unset_short_answer_is_not_linked():
    p = _pair("아니요", "이뇨제나 칼륨 보충제를 복용하고 있나요?", purpose=None)
    assert not _e3(f"Q: {p.question} A: 아니요", _src(p), qa_log=[p]).ok
    assert ed.eligibility_answers(f"{A}:E3", [p]) == []


def test_answer_missing_from_session_record_is_not_evidence():
    other = _pair("모름", "다른 질문", purpose="eligibility")
    c = _e3(LONG, _src(_pair()), qa=[other])
    assert not c.ok and c.flags == ["answer_purpose_unknown"]


def test_other_session_record_is_not_evidence():
    p = _pair(purpose="eligibility").model_copy(update={"session_id": "S-other"})
    assert _e3(LONG, _src(p), qa=[p]).flags == ["answer_purpose_unknown"]


def test_no_record_passed_keeps_legacy_behaviour():
    """문답 기록을 넘기지 않는 호출(스크립트 등)은 목적을 검사하지 않음 (운영 Session은 항상 qa를 넘김 - 아래 matcher 테스트)."""
    assert _e3(LONG, _src(_pair())).ok


# ------------------------------------------------------------ 독립적인 기록 사실·정상 판정용 답변은 유지
def test_record_fact_still_evidence_when_reference_answer_repeats_it():
    base = f"{BASE} 이뇨제, 칼륨 보충제는 복용하지 않습니다."
    p = _pair()
    assert _e3("이뇨제, 칼륨 보충제는 복용하지 않습니다", _src(p, base=base), qa=[p]).ok


def test_mixed_quote_uses_record_part_only():
    base = f"{BASE} 이뇨제, 칼륨 보충제는 복용하지 않습니다."
    p = _pair()
    quote = f"이뇨제, 칼륨 보충제는 복용하지 않습니다 / {LONG}"
    assert _e3(quote, _src(p, base=base), qa=[p]).ok


def test_eligibility_long_answer_still_evidence():
    p = _pair(purpose="eligibility")
    assert _e3(LONG, _src(p), qa=[p]).ok


def test_record_a_line_outside_rounds_is_record():
    """처음 입력의 'A:' 줄(SOAP 평가 등)은 확인 질문 답변이 아니므로 목적 검사 대상이 아님."""
    src = f"S: 편두통\nA: {LONG}"
    assert _e3(LONG, src, qa=[]).ok


# ------------------------------------------------------------ D1·Q1 경로와의 상호작용
def test_reference_answer_cannot_establish_not_applicable():
    wocbp = "Women of childbearing potential must use effective contraception"
    p = _pair("2015년에 폐경되었습니다", "복용 중인 약과 과거 수술·병력을 알려주세요.", target=["T:I4"])
    c = ed.check(wocbp, p.answer, _src(p), applicable=False, key="T:I4", qa=[p], session_id=SID)
    assert not c.ok and c.flags == ["physician_reference_answer"]
    e = p.model_copy(update={"purpose": "eligibility"})
    assert ed.check(wocbp, e.answer, _src(e), applicable=False, key="T:I4", qa=[e], session_id=SID).ok


def test_reference_answer_cannot_satisfy_time_window():
    p = _pair("최근 3개월 동안 매달 1회 이상 있었음", "최근 3개월 동안 조짐이 얼마나 자주 있었나요?", target=[f"{A}:I2"])
    c = ed.check(R["I2"]["text"], p.answer, _src(p), holds="yes", time_window_days=90, reference_date="2026-10-25",
                 qa=[p], session_id=SID)
    assert not c.ok and c.flags == ["physician_reference_answer"]


# ------------------------------------------------------------ matcher 경로 (가짜 LLM, 결정적)
def _parsed(rule) -> ParsedTrial:
    return ParsedTrial(trial_id=A, title="t", target_population="", intervention_summary="", key_conditions=[],
                       cohorts=[], rules=[rule], parsing_notes="")


def _match(monkeypatch, pair, quote=LONG):
    rule = _rule(rule_id="E3", kind="exclusion", text=R["E3"]["text"], category="medication",
                 structured_evaluable=False)
    out = [LLMAssessment(rule_id="E3", applicable=True, holds="no", evidence_type="explicit", confidence="high",
                         evidence=quote, reasoning="scripted", missing_info=None)]
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=out))
    m = matcher.match_trial(_profile(age=44, sex="female", ecog=None), _parsed(rule), {"brief_summary": ""},
                            reference_date="2026-10-25", source_text=_src(pair), qa=[pair], session_id=SID)
    return m.assessments[0]


def test_matcher_reference_answer_not_used_for_e3(monkeypatch):
    a = _match(monkeypatch, _pair())
    assert a.passes is None and a.holds == "unknown" and "physician_reference_answer" in a.validation_flags
    assert a.llm_judgment["holds"] == "no"  # 모델 판정은 기록에 남음


def test_matcher_eligibility_answer_still_used(monkeypatch):
    a = _match(monkeypatch, _pair(purpose="eligibility", target=[f"{A}:E3"]))
    assert a.holds == "no" and "physician_reference_answer" not in a.validation_flags
    assert a.evidence_source == "self_report"


def test_rule_engine_lab_from_reference_answer_is_held():
    rule = _rule(rule_id="I1", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin",
                 operator=">=", value="9", unit="g/dL")
    ans = "최근 검사에서 혈색소 11 정도였어요"
    p = _pair(ans, "최근 받은 검사 결과가 있다면 알려주세요.", target=[])
    prof = _profile(labs=[LabValue(name="hemoglobin", value=11.0, unit="g/dL", date=None, source_quote=ans)])
    a = matcher.match_trial(prof, _parsed(rule), {"brief_summary": ""}, source_text=_src(p), qa=[p],
                            session_id=SID).assessments[0]
    assert a.method == "rule_engine" and a.passes is None and a.validation_flags == ["physician_reference_answer"]
    e = p.model_copy(update={"purpose": "eligibility"})
    b = matcher.match_trial(prof, _parsed(rule), {"brief_summary": ""}, source_text=_src(e), qa=[e],
                            session_id=SID).assessments[0]
    # 판정용 답은 기존 정책 그대로: 검사 수치 자기 보고 -> UNVERIFIED(답변상 충족 보존)
    assert b.needs_verification and b.self_reported_holds == "yes" and b.validation_flags == []
    assert b.evidence_source == "self_report"
