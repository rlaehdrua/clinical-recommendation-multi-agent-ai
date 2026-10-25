"""근거 직접성 검증 반례 (Phase 10-E9 감사 A~J와 승인 조건 1~5).

코드는 필요조건이 빠진 확정 판정을 unknown으로 되돌리기만 합니다(판정을 올리지 않음, 불충족으로 바꾸지 않음).
LLM 대역은 결정적이므로 반복 실행은 코드 경로의 결정성만 보이며 실제 Claude의 재현성을 뜻하지 않습니다.
기준은 메모리 내 합성 기준이며, Frozen Store는 읽기만 합니다(NCT06833073 I3).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import evidence_directness as ed, trial_store
from ctrec.agents import matcher
from ctrec.schemas import LLMAssessment, MatcherOutput, QAPair, RuleMeta

from test_decisions import TRIAL, _parsed
from test_offline import _profile, _rule

ROOT = Path(__file__).resolve().parent.parent
REC = "clinical_record"
REF = "2026-10-24"


def _qa(q: str, a: str) -> str:
    return f"\n\n[추가 확인 정보 - 라운드 1]\nQ: {q}\nA: {a}"


def _judge(monkeypatch, rule, text, holds, quote, *, applicable=True, required="clinical_record", qa_log=None,
           reference_date=REF):
    la = LLMAssessment(rule_id=rule.rule_id, applicable=applicable, holds=holds, evidence_type="explicit",
                       confidence="high", evidence=quote, reasoning="scripted", missing_info=None)
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[la]))
    p = _parsed([rule])
    p.rule_meta = {rule.rule_id: RuleMeta(required_evidence=required)}
    m = matcher.match_trial(_profile(), p, TRIAL, reference_date=reference_date, source_text=text, input_source=REC,
                            qa_log=qa_log)
    return m, m.assessments[0]


def R(text, category, kind="exclusion", window=None):
    return _rule(rule_id="R1", kind=kind, text=text, category=category, structured_evaluable=False,
                 time_window_days=window)


IODINE = R("Known allergy to iodinated contrast media", "comorbidity")
CONTRAST = R("Known allergy to contrast media", "comorbidity")
ADVERSE = R("History of adverse reaction to iodinated contrast media", "comorbidity")
SURGERY = R("Prior surgery", "procedure", kind="inclusion")
TFCC = R("Prior TFCC repair surgery", "procedure", kind="inclusion")
FAILED = R("Disease progression after (failure of) prior platinum-based chemotherapy", "treatment_history",
           kind="inclusion")
PREG = R("Pregnant or breastfeeding", "reproductive")
PREG_ONLY = R("Currently pregnant", "reproductive")
PREG_7D = R("Negative pregnancy test within 7 days before enrollment", "reproductive", kind="inclusion")
BRAIN = R("Untreated brain metastases", "comorbidity")
M, F = "68세 남성.\n", "52세 여성.\n"


# ------------------------------------------------------------ 1. 질문·답변 구분, 인용 위치
@pytest.mark.parametrize("rule,holds,q,a", [
    (IODINE, "no", "조영제에 알레르기가 있나요?", "없는 것 같아요"),  # J-2 (수정 전: POTENTIAL_MATCH)
    (SURGERY, "yes", "수술 받은 적 있나요?", "받은 것 같아요"),  # J-3 (수정 전: CRITERIA_MATCH)
])
def test_qa_bundle_quote_checks_the_answer(monkeypatch, rule, holds, q, a):
    m, x = _judge(monkeypatch, rule, "환자." + _qa(q, a), holds, f"Q: {q} A: {a}", required="self_report_ok")
    assert x.passes is None and x.evidence_directness == "hedged" and x.self_reported_holds is None
    assert m.match_status == "INSUFFICIENT_INFORMATION"


def test_question_condition_is_not_patient_statement(monkeypatch):
    m, x = _judge(monkeypatch, IODINE, "환자." + _qa("조영제에 알레르기가 있나요?", "네"), "yes", "조영제에 알레르기가 있나요")
    assert x.passes is None and x.evidence_directness == "question_only"


def test_multiline_record_quote_checked_per_line(monkeypatch):
    m, x = _judge(monkeypatch, SURGERY, "Wrist surgery 2020\nprobably arthroscopic", "yes",
                  "Wrist surgery 2020 probably arthroscopic", required="self_report_ok")
    assert x.passes is None and x.evidence_directness == "hedged"


def test_unlocated_quote_is_not_confirmed():
    """줄 단위 위치를 찾지 못한 인용은 통과(ok)가 아님 (이전: quote_not_located -> unchecked 통과)."""
    c = ed.check("Prior surgery", "수술 받음 완전히 다른 문장", "수술 받음.")
    assert not c.ok and c.directness == "unlocated"


# ------------------------------------------------------------ 2. 부정 범위와 추측 범위
@pytest.mark.parametrize("text,is_hedged,pol", [
    ("알레르기가 의심됩니다", True, "uncertain"),
    ("알레르기 의심 소견 없음", False, "negative"),
    ("임신 가능성이 있습니다", True, "uncertain"),
    ("임신 가능성 없음", False, "negative"),
    ("임신 가능성은 없는 것 같습니다", True, "uncertain"),
    ("no suspicious lesion", False, "negative"),
    ("suspected contrast allergy", True, "uncertain"),
    ("임신 아닙니다", False, "negative"),
    ("모르핀 복용 중", False, "positive"),
])
def test_negation_scope_vs_hedge_scope(text, is_hedged, pol):
    assert ed.hedged(_norm := ed._norm(text)) is is_hedged and ed.polarity(_norm) == pol


def test_negated_suspicion_record_is_confirmed(monkeypatch):
    """C (수정 후 1차에서 과잉 차단): '조영제 알레르기 의심 소견 없음'은 직접 부재 진술."""
    m, x = _judge(monkeypatch, CONTRAST, M + "조영제 알레르기 의심 소견 없음.", "no", "조영제 알레르기 의심 소견 없음")
    assert x.passes is True and x.evidence_directness == "direct"


def test_no_pregnancy_possibility_not_hedged_but_not_sufficient_for_all(monkeypatch):
    """G-1: '임신 가능성 없음'은 추측이 아님. 임신 기준에는 근거, '임신 또는 수유'·'7일 내 검사' 기준에는 부족."""
    _, only = _judge(monkeypatch, PREG_ONLY, F + "임신 가능성 없음.", "no", "임신 가능성 없음", required="self_report_ok")
    assert only.passes is True
    _, both = _judge(monkeypatch, PREG, F + "임신 가능성 없음.", "no", "임신 가능성 없음", required="self_report_ok")
    assert both.passes is None and both.validation_flags == ["object_not_matched"]
    _, test = _judge(monkeypatch, PREG_7D, "34세 여성.\n임신 가능성 없음.", "yes", "임신 가능성 없음",
                     required="self_report_ok")
    assert test.passes is None and test.validation_flags == ["test_not_stated"]


# ------------------------------------------------------------ 3. 대상 일치
@pytest.mark.parametrize("rule,text,holds,quote,flag", [
    (IODINE, M + "페니실린 알레르기 없음.", "no", "페니실린 알레르기 없음", "object_not_matched"),  # A
    (IODINE, M + "가돌리늄 조영제 알레르기 없음.", "no", "가돌리늄 조영제 알레르기 없음", "object_not_matched"),  # I-1
    (IODINE, M + "조영제 알레르기가 없는 것 같았지만 검사 결과 알레르기가 확인됨.", "yes", "검사 결과 알레르기가 확인됨",
     "object_not_matched"),  # 일반 조영제 확인 != 요오드 조영제
    (TFCC, "45세 남성." + _qa("수술 받은 적 있나요?", "수술은 확실히 했지만 수술 종류는 모름."), "yes", "수술은 확실히 했지만",
     "specific_term_missing"),  # E-2
])
def test_direct_record_with_other_object_is_not_evidence(monkeypatch, rule, text, holds, quote, flag):
    m, x = _judge(monkeypatch, rule, text, holds, quote)
    assert x.passes is None and x.validation_flags == [flag] and m.eligibility == "UNCERTAIN"


def test_same_or_broader_object_is_accepted(monkeypatch):
    _, broad = _judge(monkeypatch, IODINE, M + "조영제 알레르기 없음.", "no", "조영제 알레르기 없음")  # 조영제 전체 부재 ⊃ 요오드
    assert broad.passes is True
    _, same = _judge(monkeypatch, CONTRAST, M + "조영제 알레르기가 없는 것 같았지만 검사 결과 알레르기가 확인됨.", "yes",
                     "검사 결과 알레르기가 확인됨")  # D: 같은 문장의 대상(조영제), 추측 절과 확인 절은 상충이 아님
    assert same.passes is False and same.evidence_directness == "direct"


def test_old_test_result_does_not_meet_recent_requirement(monkeypatch):
    """I-2: 기간 조건(7일 이내) 밖의 검사 결과."""
    _, old = _judge(monkeypatch, PREG_7D, "34세 여성.\n2025-01-03 혈청 hCG 음성.", "yes", "2025-01-03 혈청 hCG 음성",
                    required="self_report_ok")
    assert old.passes is None and old.validation_flags == ["time_window_unverified"]
    _, recent = _judge(monkeypatch, PREG_7D, "34세 여성.\n2026-10-20 소변 임신 검사 음성.", "yes",
                       "2026-10-20 소변 임신 검사 음성", required="self_report_ok")
    assert recent.passes is True
    _, undated = _judge(monkeypatch, PREG_7D, "34세 여성.\n소변 임신 검사 음성.", "yes", "소변 임신 검사 음성",
                        required="self_report_ok")
    assert undated.passes is None and undated.validation_flags == ["time_window_unverified"]


def test_negated_criterion_time_window_applies_only_to_presence(monkeypatch):
    """부정형 기준('No rituximab ... within 6 months'): holds=yes는 '없음' 주장 -> 기간 검사 대상 아님.
    holds=no(최근 사용 있음)는 '있음' 주장 -> 날짜가 기간 안이어야 함."""
    rule = R("No rituximab was used within 6 months prior to the enrollment", "medication", kind="inclusion")
    _, none = _judge(monkeypatch, rule, "10세 남아.\n리툭시맙 투여력 없음.", "yes", "리툭시맙 투여력 없음",
                     required="self_report_ok")
    assert none.passes is True
    _, old = _judge(monkeypatch, rule, "10세 남아.\n2024-01-10 리툭시맙 투여.", "no", "2024-01-10 리툭시맙 투여",
                    required="self_report_ok")
    assert old.passes is None and old.validation_flags == ["time_window_unverified"]
    _, recent = _judge(monkeypatch, rule, "10세 남아.\n2026-09-10 리툭시맙 투여.", "no", "2026-09-10 리툭시맙 투여",
                       required="self_report_ok")
    assert recent.passes is False


def test_treatment_history_does_not_imply_failure(monkeypatch):
    """F: 치료를 받았다는 기록만으로 치료 실패·진행을 확정하지 않음."""
    _, x = _judge(monkeypatch, FAILED, "64세 남성.\n2025년 시스플라틴 항암치료를 받음.", "yes", "2025년 시스플라틴 항암치료를 받음",
                  required="self_report_ok")
    assert x.passes is None and x.validation_flags == ["outcome_not_stated"]
    _, y = _judge(monkeypatch, FAILED, "64세 남성.\n2025년 시스플라틴 항암치료 후 질병 진행.", "yes",
                  "2025년 시스플라틴 항암치료 후 질병 진행", required="self_report_ok")
    assert y.passes is True


def test_frozen_turbt_rule_time_window_read_only(monkeypatch):
    """Frozen Store의 NCT06833073 I3 (TURBT 12주 이내, time_window_days=84)를 읽기만 해서 확인."""
    trial = json.loads((ROOT / "data/trials/NCT06833073.json").read_text(encoding="utf-8"))
    full = trial_store.load(trial)
    rule = next(r for r in full.rules if r.rule_id == "I3")
    assert rule.time_window_days == 84
    one = full.model_copy(update={"rules": [rule], "cohorts": []})
    one.rule_meta = {"I3": full.rule_meta["I3"]}
    text = "68M bladder cancer.\nTURBT was performed on 2026-07-21."
    la = LLMAssessment(rule_id="I3", applicable=True, holds="yes", evidence_type="explicit", confidence="high",
                       evidence="TURBT was performed on 2026-07-21", reasoning="scripted", missing_info=None)
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[la]))
    late = matcher.match_trial(_profile(), one, trial, reference_date="2026-10-24", source_text=text, input_source=REC)
    assert late.assessments[0].validation_flags == ["time_window_unverified"]  # 95일 > 84일
    ok = matcher.match_trial(_profile(), one, trial, reference_date="2026-09-01", source_text=text, input_source=REC)
    assert ok.assessments[0].passes is True


# ------------------------------------------------------------ 4. 표현 목록은 후보 탐지에만
@pytest.mark.parametrize("rule,text,quote", [
    (ADVERSE, M + "2026-07 조영 CT 시행.", "2026-07 조영 CT 시행"),  # B
    (ADVERSE, "68M. Contrast-enhanced CT performed in 2026-07.", "Contrast-enhanced CT performed in 2026-07"),  # B2
])
def test_adverse_reaction_criteria_now_in_concept(monkeypatch, rule, text, quote):
    _, x = _judge(monkeypatch, rule, text, "no", quote)
    assert x.passes is None and x.evidence_directness == "indirect"


def test_concept_word_alone_is_not_sufficient(monkeypatch):
    """'반응'이라는 단어가 있어도 극성이 판정과 반대이면(반응 있음 -> 알레르기 없음) 확정하지 않음."""
    _, x = _judge(monkeypatch, CONTRAST, M + "조영제 투여 후 두드러기 반응.", "no", "조영제 투여 후 두드러기 반응")
    assert x.passes is None and x.validation_flags == ["polarity_mismatch"]


# ------------------------------------------------------------ 상충 기록
@pytest.mark.parametrize("rule,text,quote", [
    (CONTRAST, M + "2023 기록: 조영제 알레르기 없음.\n2026 기록: 조영제 투여 후 아나필락시스.", "2023 기록: 조영제 알레르기 없음"),
    (BRAIN, "60세 여성 폐암.\n2025-01 MRI: 뇌전이 없음.\n2026-09 MRI: 새로운 뇌전이 2개.", "2025-01 MRI: 뇌전이 없음"),
])
def test_past_and_recent_records_conflict(monkeypatch, rule, text, quote):
    """H-1(개념군), H-2(개념군 밖): 어느 쪽도 채택하지 않고 기록 확인. 부적격으로 바꾸지 않음."""
    m, x = _judge(monkeypatch, rule, text, "no", quote)
    assert x.evidence_directness == "conflicting" and x.needs_verification and x.passes is None
    assert m.eligibility == "UNCERTAIN"


def test_different_objects_are_not_conflict(monkeypatch):
    _, x = _judge(monkeypatch, BRAIN, "60세 여성 폐암.\nMRI: no brain metastases.\nCT: liver metastases.", "no",
                  "MRI: no brain metastases")
    assert x.passes is True


# ------------------------------------------------------------ 5. 객관식 짧은 답변
SURG_Q = "45세 남성." + _qa("수술 받은 적 있나요?", "예")


def _log(answer="예", target="T1:R1", qid="Q1", question="수술 받은 적 있나요?"):
    # Session이 만드는 기록처럼 목적을 밝힘 (P14 D4: 목적을 밝히지 않은 기록은 판정 근거가 아님)
    return [QAPair(round=1, question_id=qid, question=question, answer=answer, target=[target], purpose="eligibility")]


def test_short_answer_needs_question_target_link(monkeypatch):
    _, none = _judge(monkeypatch, SURGERY, SURG_Q, "yes", "Q: 수술 받은 적 있나요? A: 예", required="self_report_ok")
    assert none.passes is None and none.evidence_directness == "unlinked"
    _, linked = _judge(monkeypatch, SURGERY, SURG_Q, "yes", "Q: 수술 받은 적 있나요? A: 예", required="self_report_ok",
                       qa_log=_log())
    assert linked.passes is True and linked.evidence_source == "self_report"
    _, other = _judge(monkeypatch, SURGERY, SURG_Q, "yes", "Q: 수술 받은 적 있나요? A: 예", required="self_report_ok",
                      qa_log=_log(target="T1:R9"))
    assert other.passes is None and other.evidence_directness == "unlinked"


def test_linked_answer_polarity_must_match(monkeypatch):
    text = "45세 남성." + _qa("수술 받은 적 있나요?", "아니요")
    _, x = _judge(monkeypatch, SURGERY, text, "yes", "Q: 수술 받은 적 있나요? A: 아니요", required="self_report_ok",
                  qa_log=_log(answer="아니요"))
    assert x.passes is None and x.validation_flags == ["polarity_mismatch"]
    _, y = _judge(monkeypatch, SURGERY, text, "no", "Q: 수술 받은 적 있나요? A: 아니요", required="self_report_ok",
                  qa_log=_log(answer="아니요"))
    assert y.passes is False  # 답과 판정이 맞으면 기존대로 (선정 기준 불충족)


def test_linked_answer_for_record_criterion_stays_unverified(monkeypatch):
    text = "68세 남성." + _qa("요오드 조영제에 알레르기가 있나요?", "아니요")
    m, x = _judge(monkeypatch, IODINE, text, "no", "Q: 요오드 조영제에 알레르기가 있나요? A: 아니요",
                  qa_log=_log(answer="아니요", question="요오드 조영제에 알레르기가 있나요?"))
    assert x.needs_verification and x.self_reported_holds == "no" and m.match_status == "POTENTIAL_MATCH"


def test_session_match_without_qa_log_keeps_signature(monkeypatch):
    """현재 Session.match는 qa_log를 넘기지 않음 -> 기존 호출 그대로 동작, 짧은 답변만으로는 확정하지 않음."""
    import inspect
    assert inspect.signature(matcher.match_trial).parameters["qa_log"].default is None
    m, x = _judge(monkeypatch, SURGERY, SURG_Q, "yes", "Q: 수술 받은 적 있나요? A: 예", required="self_report_ok")
    assert x.passes is None and m.eligibility == "UNCERTAIN"


# ------------------------------------------------------------ 보존
def test_counterexample_runs_are_deterministic(monkeypatch):
    rows = [_judge(monkeypatch, IODINE, M + "가돌리늄 조영제 알레르기 없음.", "no", "가돌리늄 조영제 알레르기 없음")[1]
            .model_dump() for _ in range(3)]
    assert rows[0] == rows[1] == rows[2]
