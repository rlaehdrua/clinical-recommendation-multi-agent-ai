"""근거 출처 판별 (D2, 아키텍처 §18–19): 진료 기록으로 확인된 근거만 record.

- 최초 입력(환자 원문)은 선언된 출처(input_source)를 따름: clinical_record로 선언된 입력만 record,
  self_report면 self_report, 선언이 없으면 unknown (문자열이라는 이유로 기록으로 보지 않음)
- 확인 질문 답변에서만 확인되는 값은 self_report
- 출처를 확인할 수 없으면 unknown -> 검증된 기록으로 인정하지 않음 (진료 기록이 필요한 기준이면 UNVERIFIED)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.agents import matcher
from ctrec.pipeline import PatientCase, Session, load_patients
from ctrec.schemas import LabValue, LLMAssessment, MatcherOutput

from test_decisions import TRIAL, _parsed
from test_offline import _profile, _rule

ECOG_RULE = _rule(rule_id="I1", text="ECOG performance status 0-1", category="performance_status",
                  field="ecog", operator="<=", value="1")
HB_RULE = _rule(rule_id="I1", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin",
                operator=">=", value="9", unit="g/dL")
AGE_RULE = _rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18", unit="years")
EGFR_RULE = _rule(rule_id="I1", text="EGFR mutation positive", category="biomarker", structured_evaluable=False)
RECORD = "clinical_record"


def _answers(q: str, a: str) -> str:
    return f"\n\n[추가 확인 정보 - 라운드 1]\nQ: {q}\nA: {a}"


# ------------------------------------------------------------ 최초 입력의 출처 (확인 질문 전)
def _egfr_initial_match(monkeypatch, input_source: str | None):
    src = "62세 여성 폐암. 조직검사에서 EGFR exon 19 deletion 양성."
    la = LLMAssessment(rule_id="I1", applicable=True, holds="yes", evidence_type="explicit", confidence="high",
                       evidence="EGFR exon 19 deletion 양성", reasoning="r", missing_info=None)
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[la]))
    kw = {} if input_source is None else {"input_source": input_source}
    return matcher.match_trial(_profile(), _parsed([EGFR_RULE]), TRIAL, source_text=src, **kw)


def test_initial_input_declared_clinical_record_is_record(monkeypatch):
    """출처가 진료 기록으로 선언된 최초 입력의 근거는 record -> 판정 확정."""
    a = _egfr_initial_match(monkeypatch, RECORD).assessments[0]
    assert a.evidence_source == "record" and a.passes is True and not a.needs_verification


def test_initial_input_self_report_is_unverified(monkeypatch):
    """최초 입력이 환자 자기 보고면 질문 전이라도 기록으로 보지 않음 -> UNVERIFIED, 답변 값 보존."""
    m = _egfr_initial_match(monkeypatch, "self_report")
    a = m.assessments[0]
    assert a.evidence_source == "self_report"
    assert a.needs_verification and a.passes is None and a.self_reported_holds == "yes"
    assert m.eligibility == "UNCERTAIN" and m.match_status == "POTENTIAL_MATCH"


@pytest.mark.parametrize("input_source", [None, "unknown"])
def test_initial_input_unknown_source_is_not_record(monkeypatch, input_source):
    """출처를 밝히지 않은 최초 입력(기본값)은 unknown -> 기록으로 승격하지 않고, 자기 보고 값으로 꾸미지도 않음."""
    m = _egfr_initial_match(monkeypatch, input_source)
    a = m.assessments[0]
    assert a.evidence_source == "unknown"
    assert a.needs_verification and a.passes is None and a.self_reported_holds is None
    assert m.eligibility == "UNCERTAIN" and m.match_status == "INSUFFICIENT_INFORMATION"


def test_initial_unknown_source_keeps_self_report_ok_verdict():
    """자기 보고로 충분한 기준(나이)은 최초 입력 출처가 unknown이어도 판정이 그대로."""
    m = matcher.match_trial(_profile(age=62), _parsed([AGE_RULE]), TRIAL, source_text="62세 여성 폐암.")
    a = m.assessments[0]
    assert a.evidence_source == "unknown" and a.passes is True and m.eligibility == "ELIGIBLE"


# ------------------------------------------------------------ 입력 경로: 환자 파일·Session
def test_load_patients_reads_input_source_and_defaults_to_unknown(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps([{"patient_id": "A", "text": "기록", "input_source": "clinical_record"},
                             {"patient_id": "B", "text": "자기 소개", "input_source": "self_report"},
                             {"patient_id": "C", "text": "출처 미상"},
                             {"patient_id": "D", "age": 62, "input_source": "clinical_record"}]), encoding="utf-8")
    cases = {c.patient_id: c for c in load_patients(f)}
    assert [cases[k].input_source for k in "ABCD"] == ["clinical_record", "self_report", "unknown", "clinical_record"]
    assert "input_source" not in cases["D"].raw_text  # 출처 표시는 메타데이터 - LLM 입력 원문에 넣지 않음
    t = tmp_path / "free.txt"
    t.write_text("자유 서술", encoding="utf-8")
    assert load_patients(t)[0].input_source == "unknown"
    assert PatientCase("P", "x").input_source == "unknown"


def test_load_patients_rejects_invalid_input_source(tmp_path):
    """확인되지 않은 값(예: document_verified)을 임의로 받아들이지 않음."""
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"patient_id": "A", "text": "x", "input_source": "document_verified"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_patients(f)


def test_session_passes_input_source_to_matcher(monkeypatch):
    seen = []

    def fake_match(*a, **k):
        seen.append(k.get("input_source"))
        raise RuntimeError("stop")

    monkeypatch.setattr(matcher, "match_trial", fake_match)
    for src in ("self_report", "clinical_record"):
        s = Session(case=PatientCase("P", "x", input_source=src), trials={"T1": TRIAL}, answerer=None,
                    tracer=_NullTracer())
        s.profile = _profile()
        s.parsed["T1"] = _parsed([AGE_RULE])
        s.match(["T1"])
    assert seen == ["self_report", "clinical_record"]


class _NullTracer:
    def log(self, *a, **k):
        pass


# ------------------------------------------------------------ 규칙 엔진: 프로필 구조화 필드(인용 없음)
def test_ecog_from_answer_only_is_unverified():
    """기록에 없는 ECOG를 답변으로만 얻었으면 self_report -> UNVERIFIED (이전에는 무조건 record)."""
    src = "62세 여성 폐암 4기." + _answers("일상생활 활동 정도는?", "ECOG 1이라고 들었어요")
    m = matcher.match_trial(_profile(ecog=1), _parsed([ECOG_RULE]), TRIAL, source_text=src, input_source=RECORD)
    a = m.assessments[0]
    assert a.method == "rule_engine"
    assert a.evidence_source == "self_report"
    assert a.needs_verification and a.passes is None and a.self_reported_holds == "yes"
    assert m.eligibility == "UNCERTAIN" and m.match_status == "POTENTIAL_MATCH"


def test_ecog_in_record_stays_record_after_answers():
    """진료 기록에 ECOG가 있으면 다른 질문에 답한 뒤에도 record (기존 정상 판정 유지)."""
    src = "62세 여성 폐암 4기. ECOG PS 1." + _answers("흡연력?", "없어요")
    m = matcher.match_trial(_profile(ecog=1), _parsed([ECOG_RULE]), TRIAL, source_text=src, input_source=RECORD)
    a = m.assessments[0]
    assert a.evidence_source == "record" and not a.needs_verification and a.passes is True


def test_profile_field_without_answers_follows_initial_source():
    """확인 질문 전에는 최초 입력이 유일한 출처 -> 진료 기록으로 선언됐으면 record (기존 동작 유지)."""
    m = matcher.match_trial(_profile(ecog=1), _parsed([ECOG_RULE]), TRIAL, source_text="62세 여성 폐암 4기, 활동도 1.",
                            input_source=RECORD)
    a = m.assessments[0]
    assert a.evidence_source == "record" and not a.needs_verification and a.passes is True


def test_ecog_in_self_reported_initial_input_is_unverified():
    """최초 입력이 자기 보고면 그 안의 ECOG 값도 self_report (답변이 있어도 마찬가지)."""
    src = "제가 직접 적습니다. 62세 여성 폐암, ECOG 1이라고 들었어요." + _answers("흡연력?", "없어요")
    m = matcher.match_trial(_profile(ecog=1), _parsed([ECOG_RULE]), TRIAL, source_text=src, input_source="self_report")
    a = m.assessments[0]
    assert a.evidence_source == "self_report" and a.needs_verification and a.passes is None


def test_ecog_found_nowhere_after_answers_is_unknown():
    """답변이 있는데 ECOG 값이 기록·답변 어디에서도 확인되지 않으면 unknown -> 기록으로 인정하지 않음."""
    src = "62세 여성 폐암 4기." + _answers("흡연력?", "없어요")
    m = matcher.match_trial(_profile(ecog=1), _parsed([ECOG_RULE]), TRIAL, source_text=src, input_source=RECORD)
    a = m.assessments[0]
    assert a.evidence_source == "unknown"
    assert a.needs_verification and a.passes is None
    # 답변이 아니므로 자기 보고 값으로 꾸미지 않음
    assert a.self_reported_holds is None
    assert m.eligibility == "UNCERTAIN" and m.match_status == "INSUFFICIENT_INFORMATION"


def test_self_report_ok_rule_with_unknown_source_keeps_verdict():
    """자기 보고로 충분한 기준(나이)은 출처가 unknown이어도 판정이 바뀌지 않음."""
    src = "폐암 4기 환자." + _answers("흡연력?", "없어요")
    m = matcher.match_trial(_profile(age=62), _parsed([AGE_RULE]), TRIAL, source_text=src, input_source=RECORD)
    a = m.assessments[0]
    assert a.method == "rule_engine" and a.evidence_source == "unknown"
    assert a.passes is True and not a.needs_verification and m.eligibility == "ELIGIBLE"


# ------------------------------------------------------------ 규칙 엔진: lab (프로필 인용 포함)
def test_lab_quote_found_nowhere_after_answers_is_unknown():
    """lab 인용이 기록·답변 어디에도 없으면 record가 아님 (이전에는 기본값 record)."""
    p = _profile(labs=[LabValue(name="hemoglobin", value=11.0, unit="g/dL", date=None, source_quote="Hb eleven")])
    src = "62세 여성 폐암." + _answers("흡연력?", "없어요")
    m = matcher.match_trial(p, _parsed([HB_RULE]), TRIAL, source_text=src, input_source=RECORD)
    a = m.assessments[0]
    assert a.method == "rule_engine" and a.evidence_source == "unknown"
    assert a.needs_verification and a.passes is None and a.self_reported_holds is None


def test_lab_quote_in_record_stays_record():
    p = _profile(labs=[LabValue(name="hemoglobin", value=11.0, unit="g/dL", date=None, source_quote="Hb 11.0 g/dL")])
    src = "62세 여성 폐암. Hb 11.0 g/dL." + _answers("흡연력?", "없어요")
    m = matcher.match_trial(p, _parsed([HB_RULE]), TRIAL, source_text=src, input_source=RECORD)
    a = m.assessments[0]
    assert a.evidence_source == "record" and a.passes is True and not a.needs_verification


# ------------------------------------------------------------ LLM 판정: 프로필 필드 참조 인용 ("ecog: 1")
def _llm_ecog_match(monkeypatch, src: str):
    rule = _rule(rule_id="I1", text="ECOG performance status 0-1", category="performance_status",
                 structured_evaluable=False)
    la = LLMAssessment(rule_id="I1", applicable=True, holds="yes", evidence_type="explicit", confidence="high",
                       evidence="ecog: 1", reasoning="r", missing_info=None)
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[la]))
    return matcher.match_trial(_profile(ecog=1), _parsed([rule]), TRIAL, source_text=src, input_source=RECORD)


def test_llm_field_ref_from_answer_is_unverified(monkeypatch):
    """'ecog: 1' 같은 필드 참조는 값이 답변에서만 확인되면 self_report (이전에는 무조건 record)."""
    m = _llm_ecog_match(monkeypatch, "62세 여성 폐암 4기." + _answers("일상생활 활동 정도는?", "ECOG 1이래요"))
    a = m.assessments[0]
    assert a.evidence_source == "self_report" and a.needs_verification and a.passes is None


def test_llm_field_ref_in_record_stays_record(monkeypatch):
    m = _llm_ecog_match(monkeypatch, "62세 여성 폐암 4기. ECOG 1." + _answers("흡연력?", "없어요"))
    a = m.assessments[0]
    assert a.evidence_source == "record" and a.passes is True and not a.needs_verification
