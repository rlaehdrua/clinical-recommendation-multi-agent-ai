"""Session -> 환자 사실 저장소 병행 기록 (Phase 1-3A). API 호출 없음 (프로필·매칭 LLM·질문·추천은 대역).

병행 기록은 판정·질문·추천에 쓰이지 않으므로, 켜고 끈 실행의 판정·질문 입력·추천 입력이 같아야 합니다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config, pipeline, profile_facts
from ctrec.agents import matcher
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import (ClarifyingQuestion, Fact, LabValue, LLMAssessment, MatcherOutput, PatientProfile,
                           QuestionSet, Recommendation)

from test_decisions import TRIAL, _parsed
from test_offline import _rule

RAW = "62세 여성 비소세포폐암 4기. 2026-09-01 Hb 11.2 g/dL. ECOG 1."
ANSWER_QUOTE = "EGFR 양성이라고 들었어요"
RULES = [
    _rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18", unit="years"),
    _rule(rule_id="I2", text="EGFR mutation positive", category="biomarker", structured_evaluable=False),
    _rule(rule_id="I3", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin", operator=">=",
          value="9", unit="g/dL"),
]


class _Tracer:
    def __init__(self):
        self.records = []

    def log(self, agent, event, **data):
        self.records.append({"agent": agent, "event": event, **data})


class _Answerer:
    def answer(self, questions, patient_id):
        return {q.question_id: ANSWER_QUOTE for q in questions}


def _fake_profile(ecog_after_answer=1):
    def profile(patient_id, raw_text, reference_date=None):
        answered = ANSWER_QUOTE in raw_text
        return PatientProfile(
            patient_id=patient_id, age=62, age_value=62, age_unit="years", sex="female",
            primary_diagnosis="non-small cell lung cancer", disease_stage="IV", diagnoses=[],
            biomarkers=[Fact(description="EGFR positive", date=None, source_quote=ANSWER_QUOTE)] if answered else [],
            labs=[LabValue(name="Hb", value=11.2, unit="g/dL", date="2026-09-01", source_quote="Hb 11.2 g/dL")],
            medications=[], treatment_history=[], ecog=ecog_after_answer if answered else 1, other_facts=[],
            missing_or_ambiguous=[] if answered else ["EGFR"],
        )
    return profile


def _fake_matcher_llm(model, system, user, **k):
    """EGFR 규칙: 프로필에 바이오마커가 있으면 그 인용으로 '해당', 없으면 판정 불가."""
    patient = json.loads(user.split("<patient>")[1].split("</patient>")[0])
    rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
    out = []
    for r in rules:
        if patient["biomarkers"]:
            out.append(LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="yes", evidence_type="explicit",
                                     confidence="high", evidence=patient["biomarkers"][0]["source_quote"],
                                     reasoning="mock", missing_info=None))
        else:
            out.append(LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent",
                                     confidence="low", evidence="근거 없음", reasoning="mock", missing_info="EGFR"))
    return MatcherOutput(assessments=out)


@pytest.fixture()
def calls(monkeypatch):
    """질문 생성·추천 에이전트가 받은 입력을 기록 (병행 기록 전후 비교용)."""
    captured = {"questions": [], "recommend": []}

    def gen(profile, matches, qa_log, n, acquisition_plan=None):
        captured["questions"].append((profile.model_dump(), [m.model_dump() for m in matches]))
        return QuestionSet(questions=[ClarifyingQuestion(
            question_id="Q1", question="EGFR 검사 결과를 아시나요?", answer_type="free_text", target=["T1:I2"],
            purpose="eligibility", why="I2")])

    def rec(profile, matches, trials, qa_log=None):
        captured["recommend"].append((profile.model_dump(), [m.model_dump() for m in matches],
                                      [q.model_dump() for q in qa_log or []]))
        return Recommendation(patient_id=profile.patient_id, ranked=[], excluded=[], overall_comment="mock")

    monkeypatch.setattr(pipeline.question_generator, "generate_questions", gen)
    monkeypatch.setattr(pipeline.recommender, "recommend", rec)
    monkeypatch.setattr(pipeline.trial_store, "get", lambda trial: _parsed([r.model_copy() for r in RULES]))
    monkeypatch.setattr(matcher.llm, "structured", _fake_matcher_llm)
    monkeypatch.setattr(config, "WRITE_REPORT", False)
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _fake_profile())
    return captured


def _session(pid="P001", **kw) -> Session:
    return Session(case=PatientCase(pid, RAW, input_source="clinical_record"), trials={"T1": dict(TRIAL)},
                   answerer=_Answerer(), tracer=_Tracer(), **kw)


def _run(**kw) -> Session:
    s = _session(**kw)
    s.run_fixed()
    return s


# ------------------------------------------------------------ 1-5. 기록, 중복 없음, 답변 반영, 출처 보존
def test_first_profile_recorded(calls):
    s = _session()
    s.profile_patient()
    facts = s.fact_store.facts("P001")
    assert {f.field for f in facts} >= {"age", "sex", "ecog", "lab:hemoglobin"}
    assert all(f.session_id == s.session_id for f in facts)
    assert s.fact_shadow_log == [{"round": 0, "session_id": s.session_id, "status": "ok", "added": len(facts),
                                  "duplicates": 0, "skipped": 0, "rejected": 0, "rejected_kinds": []}]


def test_reprocessing_same_profile_adds_nothing(calls):
    s = _session()
    s.profile_patient()
    n = len(s.fact_store.facts("P001"))
    s.profile_patient()
    assert len(s.fact_store.facts("P001")) == n
    assert s.fact_shadow_log[1]["added"] == 0 and s.fact_shadow_log[1]["duplicates"] == n


def test_answer_adds_self_report_fact_and_keeps_original_sources(calls):
    s = _run()
    assert s.rounds == 1 and s.qa_log[0].answer == ANSWER_QUOTE
    [egfr] = s.fact_store.facts("P001", "biomarker")
    assert (egfr.source_type, egfr.source_reference.kind, egfr.source_reference.quote) == \
        ("self_report", "answer", ANSWER_QUOTE)
    [hb] = s.fact_store.facts("P001", "lab:hemoglobin")  # 다시 추출돼도 새 사실이 아니고, 출처도 그대로
    assert (hb.source_type, hb.source_reference.kind, hb.source_reference.ref) == \
        ("clinical_record", "input_text", "raw_text:initial")
    assert [e["round"] for e in s.fact_shadow_log] == [0, 1]
    assert s.fact_shadow_log[1]["added"] == 1 and s.fact_shadow_log[1]["duplicates"] > 0
    for f in s.fact_store.facts("P001"):  # 처음 원문 정보가 답변 출처로 바뀌지 않음
        assert not (f.field != "biomarker" and f.source_type == "self_report")
        assert f.verification_status == "unknown"


def test_conflicting_value_after_answer_is_kept(calls, monkeypatch):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _fake_profile(ecog_after_answer=2))
    s = _run()
    ecog = s.fact_store.facts("P001", "ecog")
    assert [f.value for f in ecog] == [1, 2] and ecog[1].conflict_status == "contradictory"
    assert s.profile.ecog == 2  # 기존 파이프라인은 그대로 최신 프로필 사용 (사실 저장소가 바꾸지 않음)


# ------------------------------------------------------------ 7-8. 세션·환자 분리, 파일 생성 없음
def test_sessions_and_patients_do_not_mix(calls):
    a, b, a2 = _run(pid="A1"), _run(pid="B2"), _run(pid="A1")
    assert a.fact_store is not b.fact_store and a.session_id != a2.session_id
    assert a.fact_store.patients() == ["A1"] and b.fact_store.patients() == ["B2"]
    assert {f.session_id for f in a2.fact_store.facts("A1")} == {a2.session_id}


def test_default_run_writes_no_files(calls, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _run()
    assert s.fact_store.root is None and list(tmp_path.iterdir()) == []
    assert all("saved" not in e for e in s.fact_shadow_log)


def test_explicit_dir_option_saves_only_there(calls, tmp_path):
    s = _run(fact_store_dir=tmp_path / "facts")
    assert sorted(p.name for p in (tmp_path / "facts").iterdir()) == ["P001.json"]
    assert all(e["saved"] for e in s.fact_shadow_log)


# ------------------------------------------------------------ 9-10. 판정·질문·추천 불변
def _outputs(s: Session, calls: dict) -> dict:
    return {"matches": {t: m.model_dump() for t, m in s.matches.items()}, "qa": [q.model_dump() for q in s.qa_log],
            "history": s.match_history, "questions_in": calls["questions"], "recommend_in": calls["recommend"],
            "profile": s.profile.model_dump()}


def test_results_identical_with_and_without_shadow(calls):
    on = _outputs(_run(), calls)
    calls["questions"].clear()
    calls["recommend"].clear()
    off_session = _run(fact_shadow=False)
    off = _outputs(off_session, calls)
    assert on == off
    assert off_session.fact_store.patients() == [] and off_session.fact_shadow_log == []
    assert on["matches"]["T1"]["eligibility"] == "UNCERTAIN"  # 답변 기반 EGFR는 UNVERIFIED (기존 동작)


def test_shadow_failure_does_not_change_results(calls, monkeypatch):
    baseline = _outputs(_run(fact_shadow=False), calls)
    calls["questions"].clear()
    calls["recommend"].clear()

    def boom(*a, **k):
        raise RuntimeError("민감 정보가 들어 있을 수 있는 메시지 62세")

    monkeypatch.setattr(profile_facts, "ingest_profile", boom)
    s = _run()
    assert _outputs(s, calls) == baseline
    assert [e["status"] for e in s.fact_shadow_log] == ["error", "error"]
    assert s.fact_shadow_log[0]["error_type"] == "RuntimeError" and "62세" not in json.dumps(s.fact_shadow_log)
    assert s.errors == []  # 추천 파이프라인의 오류 목록과 섞지 않음


def test_partial_record_is_reported(calls, monkeypatch):
    real = profile_facts.ingest_profile

    def half(store, profile, **kw):
        store.add(profile.patient_id, "ecog", 1, session_id=kw.get("session_id"))
        raise OSError("disk")

    monkeypatch.setattr(profile_facts, "ingest_profile", half)
    s = _session()
    s.profile_patient()
    assert s.fact_shadow_log[0]["status"] == "error" and s.fact_shadow_log[0]["added_before_error"] == 1
    monkeypatch.setattr(profile_facts, "ingest_profile", real)


def test_invalid_patient_id_and_rejected_items(calls, monkeypatch):
    s = _session(pid="환자 1")  # 저장소가 받지 않는 ID: 기록만 실패, 파이프라인은 진행
    s.run_fixed()
    assert s.fact_shadow_log[0]["status"] == "error" and s.matches
    bad = _fake_profile()

    def with_bad_lab(patient_id, raw_text, reference_date=None):
        p = bad(patient_id, raw_text, reference_date)
        p.labs.append(LabValue(name="β2-microglobulin", value=2.1, unit="mg/L", date=None, source_quote="b2m"))
        return p

    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", with_bad_lab)
    t = _session()
    t.profile_patient()
    e = t.fact_shadow_log[0]
    assert e["status"] == "partial" and e["rejected"] == 1 and e["rejected_kinds"] == ["lab"]  # 성공으로 표시하지 않음


def test_shadow_status_separate_and_without_patient_values(calls):
    s = _run()
    result = s.result()
    assert result["fact_shadow"] == s.fact_shadow_log and "fact_store" not in result
    logged = [r for r in s.tracer.records if r["agent"] == "fact_store"]
    assert [r["event"] for r in logged] == ["ok", "ok"]
    text = json.dumps(logged + s.fact_shadow_log, ensure_ascii=False)
    for value in ("EGFR", "11.2", "female", "비소세포", ANSWER_QUOTE):
        assert value not in text
