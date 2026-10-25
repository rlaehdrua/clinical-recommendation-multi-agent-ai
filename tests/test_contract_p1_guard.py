"""P1 Q&A 안전 경계: 계약 없는 확인 답변은 확정 근거가 될 수 없음 (반례 A~E, P0 경로 C1~C5).

LLM은 결정적 대역입니다. 대역은 '최악의 경우'(질문이 기준을 묻지 않았는데도 답을 근거로 확정)를 재현하며,
기존 Matcher 검사(_enforce_quote·evidence_directness)를 통과한 확정이 경계에서 되돌려지는지 봅니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.agents import matcher
from ctrec.qa_guard import FLAG, guard_match, qa_dependent
from ctrec.schemas import LLMAssessment, MatcherOutput, PatientProfile, RuleMeta

from contract_fixtures import MATCHER_GUARDED, REF, RULES, session_text
from test_decisions import TRIAL, _parsed

PROFILE = PatientProfile(patient_id="P1", age=45, sex="female", primary_diagnosis=None, disease_stage=None,
                         diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[], ecog=1,
                         other_facts=[], missing_or_ambiguous=[])
K = "T1:E1"
PREG_ONLY = RULES["PREG"].model_copy(update={"text": "Pregnant participants"})  # 369063 E9와 같은 단일 개념 기준


def _judge(monkeypatch, rule, text, log, holds, quote, *, applicable=True, meta="self_report_ok"):
    p = _parsed([rule])
    p.rule_meta = {rule.rule_id: RuleMeta(required_evidence=meta)}
    la = LLMAssessment(rule_id=rule.rule_id, applicable=applicable, holds=holds, evidence_type="explicit",
                       confidence="high", evidence=quote, reasoning="scripted", missing_info=None)
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[la]))
    before = matcher.match_trial(PROFILE, p, TRIAL, reference_date=REF, source_text=text, input_source="unknown",
                                 qa_log=log)
    return before, guard_match(before, p, text)


def _q(q, a):
    return f"Q: {q} A: {a}"


SIDE = "최근 4주 이내 수술 부작용이 있었나요?"
CASES = {
    # 반례 A: 과거 수술 '예' + 최근 4주 수술 '부작용' '아니요' -> 최근 4주 수술 없음으로 확정하면 오류
    "A": (RULES["SURG"], [[("수술 받은 적 있나요?", "예", K), (SIDE, "아니요", K)]], _q(SIDE, "아니요")),
    "A_single": (RULES["SURG"], [[(SIDE, "아니요", K)]], _q(SIDE, "아니요")),
    # 반례 B: 과거 수술 '예' + 최근 4주 수술 '아니요' (양립 가능하지만 계약 없는 답이라 확정 근거 아님)
    "B": (RULES["SURG"], [[("수술 받은 적 있나요?", "예", K), ("최근 4주 이내 수술을 받았나요?", "아니요", K)]],
          _q("최근 4주 이내 수술을 받았나요?", "아니요")),
    # 반례 C: 항암치료 vs 항암치료 관련 입원
    "C": (RULES["CHEMO"], [[("과거에 항암치료를 받은 적이 있나요?", "예", "T1:E2"),
                            ("최근 4주 이내 항암치료 때문에 입원한 적이 있나요?", "아니요", "T1:E2")]],
          _q("최근 4주 이내 항암치료 때문에 입원한 적이 있나요?", "아니요")),
    # 반례 D: 임신 경험 vs 현재 임신 '검사'
    "D": (PREG_ONLY, [[("과거에 임신한 적이 있나요?", "예", "T1:E3"), ("현재 임신 검사를 받았나요?", "아니요", "T1:E3")]],
          _q("현재 임신 검사를 받았나요?", "아니요")),
    # 반례 E: '수술부작용' 복합 명사 (A와 결과가 같아야 함)
    "E": (RULES["SURG"], [[("수술 받은 적 있나요?", "예", K), ("최근 4주 이내 수술부작용이 있었나요?", "아니요", K)]],
          _q("최근 4주 이내 수술부작용이 있었나요?", "아니요")),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_counterexamples_never_confirm_from_uncontracted_answers(monkeypatch, name):
    rule, rounds, quote = CASES[name]
    text, log = session_text("45세 여성.", rounds)
    before, after = _judge(monkeypatch, rule, text, log, "no", quote)
    a = after.assessments[0]
    assert a.passes is None and a.holds == "unknown" and FLAG in a.validation_flags
    assert a.needs_verification and a.self_reported_holds == "no"  # 답변상 판정은 보존 (UNVERIFIED)
    assert after.eligibility == "UNCERTAIN" and after.match_status == "POTENTIAL_MATCH"
    # 기존 Matcher 검사만으로는 확정되던 경로 (P0 C1·C2 재현): 경계가 없으면 ELIGIBLE
    assert before.eligibility == ("UNCERTAIN" if MATCHER_GUARDED else "ELIGIBLE")


def test_e_compound_noun_same_outcome_as_a(monkeypatch):
    out = {}
    for name in ("A", "E"):
        rule, rounds, quote = CASES[name]
        text, log = session_text("45세 여성.", rounds)
        _, after = _judge(monkeypatch, rule, text, log, "no", quote)
        a = after.assessments[0]
        out[name] = (a.passes, a.holds, a.needs_verification, a.self_reported_holds, after.eligibility)
    assert out["A"] == out["E"]


def test_wrong_exclusion_is_also_blocked(monkeypatch):
    """잘못된 제외 방지: 계약 없는 답변으로 제외 기준 '해당'(INELIGIBLE)도 확정하지 않음."""
    q = "현재 임신 검사를 받았나요?"  # 임신 '검사' 여부를 임신 '상태'로 읽은 확정
    text, log = session_text("45세 여성.", [[(q, "예", "T1:E3")]])
    before, after = _judge(monkeypatch, PREG_ONLY, text, log, "yes", _q(q, "예"))
    assert before.eligibility == ("UNCERTAIN" if MATCHER_GUARDED else "INELIGIBLE")
    a = after.assessments[0]
    assert a.passes is None and a.self_reported_holds == "yes"
    assert after.eligibility == "UNCERTAIN" and after.match_status == "INSUFFICIENT_INFORMATION"


@pytest.mark.parametrize("target,purpose", [(K, "eligibility"), ("T1:E9", "eligibility"), ("T9:E1", "eligibility"),
                                            (K, "physician_reference")])
def test_long_free_text_answer_any_target_or_purpose_is_blocked(monkeypatch, target, purpose):
    """P0 C4: 6자 이상 긴 답은 연결·대상·목적 검사를 받지 않던 경로 (다른 기준·시험·의사 참고용 답 포함)."""
    q, ans = "최근 수술 후 문제가 있었나요?", "아니요, 수술 후 문제는 전혀 없었어요"
    text, log = session_text("45세 여성.", [[(q, ans, target)]], purpose=purpose)
    before, after = _judge(monkeypatch, RULES["SURG"], text, log, "no", "수술 후 문제는 전혀 없었어요")
    # 경계 전에는 확정. 단 의사 참고용 답은 P14 D4부터 matcher(근거 직접성 검사)가 먼저 막음 (경계 전에도 판정 불가)
    assert before.assessments[0].passes is (None if MATCHER_GUARDED or purpose == "physician_reference" else True)
    assert after.assessments[0].passes is None
    want = "physician_reference_answer" if purpose == "physician_reference" else FLAG
    assert want in after.assessments[0].validation_flags


def test_other_session_answer_is_blocked(monkeypatch):
    q = "최근 4주 이내 수술을 받았나요?"
    text, log = session_text("45세 여성.", [[(q, "아니요, 받지 않았습니다", K)]], session_id="S-other")
    _, after = _judge(monkeypatch, RULES["SURG"], text, log, "no", "아니요, 받지 않았습니다")
    assert after.assessments[0].passes is None


def test_quote_spanning_record_and_answer_is_blocked(monkeypatch):
    text, log = session_text("45세 여성. 수술 이력 없음.", [[("최근 4주 이내 수술을 받았나요?", "아니요, 받지 않았습니다", K)]])
    _, after = _judge(monkeypatch, RULES["SURG"], text, log, "no", "수술 이력 없음. / 아니요, 받지 않았습니다")
    assert after.assessments[0].passes is None


def test_initial_record_judgment_is_unchanged(monkeypatch):
    """처음 입력만으로 내린 정상 판정은 그대로 (답변이 있어도 인용이 기록에만 있으면)."""
    text, log = session_text("45세 여성. 현재 임신 중이 아니며 수유하지 않음.",
                             [[("흡연하시나요?", "아니요, 피우지 않아요", "T1:E9")]])
    before, after = _judge(monkeypatch, RULES["PREG"], text, log, "no", "현재 임신 중이 아니며 수유하지 않음")
    assert before.assessments[0].passes is True
    assert after.assessments[0] == before.assessments[0] and after.eligibility == before.eligibility


def test_no_answers_means_no_change(monkeypatch):
    text = "45세 여성. 현재 임신 중이 아니며 수유하지 않음."
    before, after = _judge(monkeypatch, RULES["PREG"], text, [], "no", "현재 임신 중이 아니며 수유하지 않음")
    assert after is before


def test_record_required_criterion_stays_unverified_not_double_flagged(monkeypatch):
    """진료 기록 필요 기준은 기존 UNVERIFIED 처리 그대로 (경계는 확정 판정만 봄)."""
    q = "최근 4주 이내 수술을 받았나요?"
    text, log = session_text("45세 여성.", [[(q, "아니요, 받지 않았습니다", K)]])
    before, after = _judge(monkeypatch, RULES["SURG"], text, log, "no", "아니요, 받지 않았습니다", meta="clinical_record")
    a = after.assessments[0]
    assert a.needs_verification and a.passes is None and FLAG not in a.validation_flags
    assert after.model_dump() == before.model_dump()


def test_not_applicable_from_answer_is_blocked(monkeypatch):
    rule = RULES["PREG"].model_copy(update={"text": "Women of childbearing potential must use contraception"})
    q = "폐경이 되셨나요?"
    text, log = session_text("52세 여성.", [[(q, "네, 폐경이 되었어요", "T1:E3")]])
    _, after = _judge(monkeypatch, rule, text, log, "unknown", "네, 폐경이 되었어요", applicable=False)
    a = after.assessments[0]
    assert a.applicable is True and a.passes is None and a.self_reported_applicable is False


def test_rule_engine_profile_field_from_answer_is_blocked():
    """P0 C5: 답변에서 나온 프로필 값(나이)으로 규칙 엔진이 확정 -> evidence_source=self_report -> 경계."""
    from ctrec.schemas import CriterionAssessment
    a = CriterionAssessment(rule_id="I1", kind="inclusion", criterion_text="Age >= 18", holds="yes", passes=True,
                            confidence="high", evidence="환자 나이 45세", reasoning="", missing_info=None,
                            method="rule_engine", evidence_source="self_report")
    assert qa_dependent(a, "환자\n\n[추가 확인 정보 - 라운드 1]\nQ: 나이?\nA: 45세")
