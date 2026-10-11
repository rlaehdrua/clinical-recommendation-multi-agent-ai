"""팀 결정 D1–D8 반영 (API 호출 없음).

D1·D6 파싱 검증, D2 근거 수준, D7 정보 확보 순서, D8 고정 저장소, D4 질문 정책.
"""

import sys
from pathlib import Path

import pytest

from ctrec import acquisition, config, evidence, trial_store
from ctrec.agents import matcher, parser_validator
from ctrec.agents.answerers import NoAnswerer
from ctrec.agents.parser_validator import MissingCriterion, RuleCheck, ValidationOutput, ValidationReport
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import ClarifyingQuestion, LabValue, LLMAssessment, MatcherOutput, ParsedTrial, RuleMeta
from ctrec.trace import Tracer
from test_offline import _profile, _rule

CRITERIA = """Inclusion Criteria:
- Age 18 years or older
- Histologically confirmed non-small cell lung cancer
- Hemoglobin >= 9 g/dL

Exclusion Criteria:
- Untreated brain metastases
- Use of certain medications prohibited by the protocol
- Known hypersensitivity to platinum compounds or any component of the study drug formulation
"""
TRIAL = {"trial_id": "T1", "title": "t", "eligibility_criteria": CRITERIA, "overall_status": "RECRUITING",
         "minimum_age": "18 Years", "locations": [{"country": "United States"}]}


def _parsed(rules, cohorts=()) -> ParsedTrial:
    return ParsedTrial(trial_id="T1", title="t", target_population="", intervention_summary="", key_conditions=[],
                       cohorts=list(cohorts), rules=rules, parsing_notes="")


def _rules():
    return [
        _rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18"),
        _rule(rule_id="I2", text="Histologically confirmed non-small cell lung cancer", category="diagnosis",
              structured_evaluable=False),
        _rule(rule_id="I3", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin", operator=">=",
              value="9", unit="g/dL"),
        _rule(rule_id="E1", kind="exclusion", text="Untreated brain metastases", category="comorbidity",
              structured_evaluable=False),
        _rule(rule_id="E2", kind="exclusion", text="Use of certain medications prohibited by the protocol",
              category="medication", structured_evaluable=False, underspecified=True),
    ]


def _check(rid, **kw):
    base = dict(rule_id=rid, meaning_ok=True, raw_is_vague=False, category="other", structured_ok=True,
                required_evidence="self_report_ok", note="n")
    base.update(kw)
    return RuleCheck(**base)


# ------------------------------------------------------------------ D1 결정적 검사
def test_deterministic_check_demotes_unsupported_engine_field():
    p = _parsed([_rule(rule_id="I1", text="Hemoglobin >= 9 g/dL", field="hgb_level", operator=">=", value="9")])
    det = parser_validator.deterministic_check(p, TRIAL)
    assert "I1" in det and p.rules[0].field is None and not p.rules[0].structured_evaluable


def test_deterministic_check_flags_inclusion_rule_from_exclusion_section():
    p = _parsed([_rule(rule_id="I9", text="Untreated brain metastases", category="comorbidity",
                       structured_evaluable=False)])
    det = parser_validator.deterministic_check(p, TRIAL)
    assert any(m.startswith("선정/제외 방향") for m in det["I9"])


def test_coverage_gap_finds_dropped_criterion():
    p = _parsed(_rules())  # 백금 과민반응 제외 기준이 빠짐
    gaps = parser_validator.coverage_gaps(p, CRITERIA)
    assert gaps == ["[exclusion] Known hypersensitivity to platinum compounds or any component of the study drug formulation"]


def test_risk_includes_underspecified_lab_and_cross_run_difference():
    p = _parsed(_rules())
    other = _parsed(_rules())
    other.rules[3].underspecified = True  # E1이 다른 실행에서는 underspecified
    risk = parser_validator.detect_risk(p, {}, [other])
    assert "E2" in risk and "I3" in risk and "E1" in risk
    assert "I1" not in risk and "I2" not in risk


# ------------------------------------------------------------------ D1·D6 검증 적용
def test_apply_checks_rules():
    p = _parsed(_rules())
    risk = {"E2": ["underspecified"], "E1": ["x"], "I3": ["x"], "I2": ["x"]}
    out = ValidationOutput(checks=[
        _check("E2", raw_is_vague=False, category="medication"),          # D6: 원문은 구체적 -> 재분류
        _check("E1", raw_is_vague=True, category="comorbidity"),          # 애매해 보여도 켜지 않음
        _check("I3", structured_ok=False, category="lab"),                # 규칙 엔진 필드 제거
        _check("I2", meaning_ok=False, category="consent_or_logistics"),  # 의미 다름 -> review, 절차 분류는 거부
        _check("ZZ"),                                                     # 요청하지 않은 ID는 무시
    ], missing=[
        MissingCriterion(kind="exclusion", category="comorbidity",
                         text="Known hypersensitivity to platinum compounds or any component of the study drug formulation"),
        MissingCriterion(kind="exclusion", category="comorbidity", text="Allergy to platinum (paraphrased, not in source)"),
    ])
    report = ValidationReport()
    meta = parser_validator.apply_checks(p, TRIAL, risk, out, report)
    by = {r.rule_id: r for r in p.rules}
    assert by["E2"].underspecified is False and meta["E2"].underspecified_state == "reparsed_concrete"
    assert by["E1"].underspecified is False and meta["E1"].underspecified_state == "vague_suspected"
    assert by["I3"].field is None and not by["I3"].structured_evaluable
    assert meta["I2"].review and by["I2"].category == "diagnosis"
    assert report.llm_checked == ["E1", "E2", "I2", "I3"]
    assert [r.rule_id for r in p.rules][-1] == "E3" and meta["E3"].added_by_validator
    assert len(report.added_rules) == 1  # 원문에 없는 문장은 추가하지 않음
    # D2: 근거 수준은 유형 규칙, 충돌하면 엄격한 쪽
    assert meta["I3"].required_evidence == "clinical_record"
    assert meta["I1"].required_evidence == "self_report_ok"


def test_missing_validation_for_direction_mismatch_goes_to_review():
    p = _parsed(_rules())
    meta = parser_validator.apply_checks(p, TRIAL, {"I2": ["선정/제외 방향: ..."]},
                                         ValidationOutput(checks=[], missing=[]), ValidationReport())
    assert meta["I2"].review


# ------------------------------------------------------------------ D2 근거 수준
def test_required_evidence_policy():
    assert evidence.required_evidence(_rule(category="biomarker")) == ("clinical_record", None)
    assert evidence.required_evidence(_rule(category="other"), "self_report_ok") == ("self_report_ok", None)
    assert evidence.required_evidence(_rule(category="other")) == ("clinical_record", None)
    level, conflict = evidence.required_evidence(_rule(category="medication"), "clinical_record")
    assert level == "clinical_record" and conflict


def _lab_trial_match(monkeypatch, quote: str, source: str):
    rule = _rule(rule_id="I1", text="EGFR mutation positive", category="biomarker", structured_evaluable=False)
    la = LLMAssessment(rule_id="I1", applicable=True, holds="yes", evidence_type="explicit", confidence="high",
                       evidence=quote, reasoning="r", missing_info=None)
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[la]))
    return matcher.match_trial(_profile(), _parsed([rule]), TRIAL, source_text=source)


def test_self_reported_biomarker_needs_record_check(monkeypatch):
    src = "62세 여성 폐암.\n\n[추가 확인 정보 - 라운드 1]\nQ: EGFR 검사 결과?\nA: 의사가 EGFR 변이 양성이라고 했어요"
    m = _lab_trial_match(monkeypatch, "의사가 EGFR 변이 양성이라고 했어요", src)
    a = m.assessments[0]
    assert a.passes is True and a.evidence_source == "self_report" and a.needs_verification
    assert m.eligibility == "ELIGIBLE" and "기록 확인 필요" in m.summary


def test_record_biomarker_does_not_need_check(monkeypatch):
    m = _lab_trial_match(monkeypatch, "EGFR exon 19 deletion positive", "Pathology: EGFR exon 19 deletion positive.")
    assert m.assessments[0].evidence_source == "record" and not m.assessments[0].needs_verification


def test_rule_engine_lab_from_answer_is_self_report():
    rule = _rule(rule_id="I1", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin",
                 operator=">=", value="9", unit="g/dL")
    p = _profile(labs=[LabValue(name="hemoglobin", value=11.0, unit="g/dL", date=None, source_quote="혈색소 11 정도였어요")])
    src = "기록\n\n[추가 확인 정보 - 라운드 1]\nQ: 혈색소?\nA: 혈색소 11 정도였어요"
    m = matcher.match_trial(p, _parsed([rule]), TRIAL, source_text=src)
    assert m.assessments[0].method == "rule_engine" and m.assessments[0].needs_verification


# ------------------------------------------------------------------ D1 review(UNSUPPORTED) 규칙
def test_review_rule_is_not_auto_decided(monkeypatch):
    rules = [_rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18"),
             _rule(rule_id="I2", text="x", category="diagnosis", structured_evaluable=False)]
    p = _parsed(rules)
    p.rule_meta = {"I2": RuleMeta(review="원문과 뜻이 다름")}
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: pytest.fail("review 규칙은 LLM에 보내지 않음"))
    m = matcher.match_trial(_profile(), p, TRIAL)
    a = m.assessments[1]
    assert a.passes is None and a.underspecified and matcher.needs_physician_review(a)
    assert m.eligibility == "UNCERTAIN"


# ------------------------------------------------------------------ D8 고정 저장소
def test_store_roundtrip_and_pending(tmp_path, monkeypatch):
    p = _parsed(_rules())
    meta = {r.rule_id: RuleMeta(required_evidence="clinical_record") for r in p.rules}
    trial_store.save(TRIAL, p, meta, ValidationReport(), tmp_path)
    loaded = trial_store.load(TRIAL, tmp_path)
    assert [r.rule_id for r in loaded.rules] == [r.rule_id for r in p.rules]
    assert loaded.rule_meta["I1"].required_evidence == "clinical_record"
    changed = {**TRIAL, "eligibility_criteria": CRITERIA + "\n- New criterion added later"}
    with pytest.raises(trial_store.CompilationPending, match="바뀜"):
        trial_store.load(changed, tmp_path)
    with pytest.raises(trial_store.CompilationPending, match="COMPILATION_PENDING"):
        trial_store.load({**TRIAL, "trial_id": "OTHER"}, tmp_path)
    # 모집 상태·기관이 바뀌어도 기준 원문이 같으면 그대로 사용
    trial_store.load({**TRIAL, "overall_status": "COMPLETED", "locations": []}, tmp_path)


def _session(trials, **kw):
    return Session(case=PatientCase(patient_id="P", raw_text="x"), trials=trials, answerer=NoAnswerer(),
                   tracer=Tracer(None, verbose=False), reference_date="2026-10-05", **kw)


def test_out_of_scope_trial_is_compilation_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PARSED_DIR", tmp_path)
    monkeypatch.setattr(config, "LAZY_COMPILE", False)
    s = _session({"NEW": {"trial_id": "NEW", "overall_status": "RECRUITING"}})
    s.profile = _profile()
    s.match()
    m = s.matches["NEW"]
    assert m.eligibility == "NOT_EVALUATED" and "COMPILATION_PENDING" in m.evaluation_error


def test_all_repository_trials_are_compiled():
    from ctrec.tools import ctgov
    for path in sorted(config.TRIALS_DIR.glob("*.json")):
        trial_store.load(ctgov.load_local(path))  # 하나라도 없거나 원문이 바뀌면 실패


def test_medoid_picks_most_typical_run():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from compile_trials import medoid
    a = _parsed([_rule(rule_id="I1", text="alpha"), _rule(rule_id="I2", text="beta")])
    b = _parsed([_rule(rule_id="I1", text="alpha"), _rule(rule_id="I2", text="beta")])
    c = _parsed([_rule(rule_id="I1", text="gamma")])
    assert medoid([c, a, b]) == 1


# ------------------------------------------------------------------ D7 정보 확보 순서
def _uncertain_match(tid="T1", category="lab"):
    from ctrec.schemas import CriterionAssessment, TrialMatch
    a = CriterionAssessment(rule_id="I3", kind="inclusion", criterion_text="Hemoglobin >= 9 g/dL", holds="unknown",
                            passes=None, confidence="low", evidence="근거 없음", reasoning="r",
                            missing_info="혈색소 수치", method="llm", category=category)
    return TrialMatch(patient_id="P", trial_id=tid, eligibility="UNCERTAIN", assessments=[a],
                      n_pass=0, n_fail=0, n_unknown=1, summary="s")


def test_acquisition_ladder_until_unresolved():
    facts: dict = {}
    m = _uncertain_match()
    acquisition.update(facts, [m])
    f = facts["T1:I3"]
    assert f.status == "open" and f.next_step() == acquisition.POLICIES["lab"].preferred
    q = ClarifyingQuestion(question_id="Q1", question="q", answer_type="number", target=["T1:I3"],
                           purpose="eligibility", why="w")
    acquisition.record_attempts(facts, [q], 1)
    assert acquisition.plan(facts)["T1:I3"]["step"] == acquisition.POLICIES["lab"].alternatives[0]
    acquisition.record_attempts(facts, [q], 2)
    acquisition.update(facts, [m])  # 모든 단계를 시도했는데 여전히 판정 불가
    assert f.status == "unresolved" and not acquisition.is_open(facts, "T1:I3") and acquisition.plan(facts) == {}
    assert "T1:I3" in acquisition.report_section(facts, {"T1"})


def test_acquisition_resolved_and_default_policy():
    facts: dict = {}
    acquisition.update(facts, [_uncertain_match(category="comorbidity")])
    assert facts["T1:I3"].steps() == [acquisition.DEFAULT_POLICY.preferred]
    acquisition.update(facts, [])
    assert facts["T1:I3"].status == "resolved"


def test_session_stops_asking_unresolved_fact():
    s = _session({"T1": TRIAL})
    s.matches = {"T1": _uncertain_match()}
    acquisition.update(s.missing_facts, s.matches.values())
    assert [m.trial_id for m in s.uncertain()] == ["T1"]
    s.missing_facts["T1:I3"].attempted_actions = ["a", "b"]
    acquisition.update(s.missing_facts, s.matches.values())
    assert s.uncertain() == []


# ------------------------------------------------------------------ D4 질문 정책
def test_single_question_policy(monkeypatch):
    monkeypatch.setattr(config, "QUESTION_POLICY", "single")
    assert _session({}).remaining_questions() == 1
    monkeypatch.setattr(config, "QUESTION_POLICY", "adaptive")
    assert _session({}).remaining_questions() == config.MAX_QUESTIONS_PER_ROUND
