"""N2 환자값 근거 + 임상의 판단 경계 결합 검증 (보호 파일 matcher.py가 둘을 함께 적용하는 상태). API 호출 없음, 값은 모두 합성.

LLM 자리는 고정 대역입니다. 대역이 답을 고정한 최악의 경우이며, 실제 LLM의 발생 빈도를 재지 않습니다.
임상의 판단 완료 입력은 만드는 코드가 없어 시험하지 않습니다 (가짜 완료 상태를 만들지 않음).

판정 이름: MET = 기준 통과(passes True), NOT_MET = 불통과(passes False), NA = LLM의 '해당 없음'.
"""

import ast
import csv
import inspect
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import expert_boundary as xb, value_grounding as vg  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.pipeline import Session  # noqa: E402
from ctrec.schemas import LLMAssessment, MatcherOutput, ParsedTrial, PatientProfile, QAPair  # noqa: E402

from test_decisions import TRIAL  # noqa: E402
from test_expert_boundary import AGE, PID, ROOT, STORED, _assert_expert_review, _run, _stored, _verdict  # noqa: E402
from test_offline import _rule  # noqa: E402

CLASSES = [Session, ContractSession]
SRC = ROOT / "src" / "ctrec"


def _llm_verdict(rule, good, bad, outcome):
    if outcome == "NA":
        return ("unknown", good, False)
    return _verdict(rule, good, bad, ok=outcome == "MET")


def _text(age, good, bad, outcome):
    return f"{age}세 남성. {bad if outcome == 'NOT_MET' else good}."


def _layers(x, rule_id):
    a = next(a for a in x["match"].assessments if a.rule_id == rule_id)
    return {"eligibility": x["match"].eligibility, "external": x["external"], "pair": x["pair"].status.value,
            "holds": a.holds, "passes": a.passes, "applicable": a.applicable, "underspecified": a.underspecified,
            "needs_verification": a.needs_verification, "criteria": sorted((c.rule_id, c.state, c.adjudication, c.gap)
                                                                           for c in x["pair"].criteria)}


# ---------------------------------------------------------------------------
# 세 가지 LLM 판정 모두 확정되지 않음, 두 Session 일치
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("outcome", ["MET", "NOT_MET", "NA"])
@pytest.mark.parametrize("tid, rid, good, bad", STORED)
def test_llm_cannot_settle_a_clinician_rule(monkeypatch, cls, outcome, tid, rid, good, bad):
    rule, meta = _stored(tid, rid)
    x = _run(monkeypatch, tid, [AGE, rule], meta, {rid: _llm_verdict(rule, good, bad, outcome)}, _text(62, good, bad, outcome), cls=cls)
    _assert_expert_review(x, rid)
    a = next(a for a in x["match"].assessments if a.rule_id == rid)
    assert xb.FLAG in a.validation_flags and a.llm_judgment  # LLM이 낸 값은 확정이 아니라 감사용으로만 남음


@pytest.mark.parametrize("outcome", ["MET", "NOT_MET", "NA"])
@pytest.mark.parametrize("tid, rid, good, bad", STORED)
def test_session_and_contract_session_agree(monkeypatch, outcome, tid, rid, good, bad):
    rule, meta = _stored(tid, rid)
    verdicts = {rid: _llm_verdict(rule, good, bad, outcome)}
    text = _text(62, good, bad, outcome)
    a, b = (_layers(_run(monkeypatch, tid, [AGE, rule], meta, verdicts, text, cls=c), rid) for c in CLASSES)
    assert a == b


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("outcome", ["MET", "NOT_MET", "NA"])
def test_registered_inclusion_rule_with_reference_to_curative_options(monkeypatch, cls, outcome):
    """목록의 나머지 기준 NCT07680790:I2 (검수 대기, 판단 슬롯 dx.disease_setting)."""
    rule, meta = _stored("NCT07680790", "I2")
    good, bad = "근치 수술 불가 4기 재발", "근치 수술 가능"
    x = _run(monkeypatch, "NCT07680790", [AGE, rule], meta, {"I2": _llm_verdict(rule, good, bad, outcome)},
             _text(62, good, bad, outcome), cls=cls)
    _assert_expert_review(x, "I2")


@pytest.mark.parametrize("cls", CLASSES)
def test_a_targeted_yes_answer_on_a_later_round_is_held_too(monkeypatch, cls):
    """라운드가 바뀌어 다시 판정해도 같음: 환자의 '예'는 임상의 판단이 아님."""
    rule, meta = _stored("NCT07098338", "E10")
    x = _run(monkeypatch, "NCT07098338", [AGE, rule], meta, {"E10": ("no", "심장 질환 없음")}, "62세 남성. 심장 질환 없음.", cls=cls)
    s = x["session"]
    s.qa_log.append(QAPair(round=1, question_id="q1", question="심장 질환이 없습니까?", answer="예", purpose="eligibility",
                           target=["NCT07098338:E10"], session_id=s.session_id))
    s.rounds += 1
    s.match()
    m = s.matches["NCT07098338"]
    assert m.eligibility == "UNCERTAIN" and xb.violations(m, x["parsed"]) == []


# ---------------------------------------------------------------------------
# 별개의 결정적 부적격은 유지
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("outcome", ["MET", "NOT_MET", "NA"])
@pytest.mark.parametrize("tid, rid, good, bad", STORED)
def test_independent_engine_failure_stays_ineligible(monkeypatch, cls, outcome, tid, rid, good, bad):
    rule, meta = _stored(tid, rid)
    x = _run(monkeypatch, tid, [AGE, rule], meta, {rid: _llm_verdict(rule, good, bad, outcome)}, _text(10, good, bad, outcome),
             age=10, cls=cls)
    assert x["match"].eligibility == "INELIGIBLE" and x["external"] == "CRITERIA_MISMATCH"
    # 문서 검증 전 불충족은 계약상 VERIFICATION_PENDING (b2100d8과 같음). 문서 검증된 불충족은 WP6 경로에서 시험: tests/test_expert_boundary_wp6.py
    control = _run(monkeypatch, tid, [AGE], None, {}, _text(10, good, bad, outcome), age=10, cls=cls)
    assert x["pair"].status == control["pair"].status and x["pair"].status.value == "VERIFICATION_PENDING"
    assert xb.violations(x["match"], x["parsed"]) == []  # 부적격이어도 임상의 판단 기준 자체는 미확정
    assert next(a for a in x["match"].assessments if a.rule_id == "I1").passes is False


@pytest.mark.parametrize("cls", CLASSES)
def test_independent_ordinary_criterion_failure_stays_ineligible(monkeypatch, cls):
    """규칙 엔진이 아닌 일반 LLM 기준의 인용된 불충족도 유지 (임상의 판단 기준과 무관)."""
    rule, meta = _stored("NCT07098338", "I5")
    dx = _rule(rule_id="I2", text="Histologically confirmed NSCLC", category="diagnosis", structured_evaluable=False)
    x = _run(monkeypatch, "NCT07098338", [AGE, dx, rule], meta, {"I2": ("no", "조직검사상 NSCLC 아님"), "I5": ("yes", "기대 여명 12주 이상")},
             "62세 남성. 조직검사상 NSCLC 아님. 기대 여명 12주 이상.", cls=cls)
    assert x["match"].eligibility == "INELIGIBLE" and x["external"] == "CRITERIA_MISMATCH"


@pytest.mark.parametrize("cohort_of_failure, eligibility", [("B", "UNCERTAIN"), (None, "INELIGIBLE")])
def test_held_rule_keeps_an_or_path_open_unless_the_failure_is_common(monkeypatch, cohort_of_failure, eligibility):
    """OR 시험: 임상의 판단 기준의 LLM 불충족으로 경로를 닫지 않음. 공통 기준의 불충족은 모든 경로를 닫음."""
    held = _rule(rule_id="A1", text="Adequate organ function", category="comorbidity", structured_evaluable=False,
                 underspecified=True, cohort="A")
    other = _rule(rule_id="B1", text="NSCLC", category="diagnosis", structured_evaluable=False, cohort=cohort_of_failure)
    parsed = ParsedTrial(trial_id="T1", title="t", target_population="", intervention_summary="", key_conditions=[],
                         cohorts=["A", "B"], rules=[held, other], parsing_notes="")
    said = {"A1": ("no", "장기 기능 불량"), "B1": ("no", "NSCLC 아님")}

    def llm(model, *, system=None, user=None, **k):
        ask = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
        return MatcherOutput(assessments=[LLMAssessment(rule_id=r["rule_id"], applicable=True, holds=said[r["rule_id"]][0],
                                                        evidence_type="explicit", confidence="high", evidence=said[r["rule_id"]][1],
                                                        reasoning="stub", missing_info="") for r in ask])

    monkeypatch.setattr(matcher.llm, "structured", llm)
    profile = PatientProfile(patient_id=PID, age=62, age_value=62, age_unit="years", sex="male", primary_diagnosis=None,
                             disease_stage=None, diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[],
                             ecog=None, other_facts=[], missing_or_ambiguous=[])
    m = matcher.match_trial(profile, parsed, {**TRIAL, "trial_id": "T1"}, source_text="장기 기능 불량. NSCLC 아님.",
                            input_source="clinical_record")
    assert m.eligibility == eligibility


# ---------------------------------------------------------------------------
# N2 근거 검사와 공존
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", CLASSES)
def test_n2_hold_on_an_ordinary_rule_survives_next_to_a_clinician_rule(monkeypatch, cls):
    """경계가 N2의 판정 보류를 덮어쓰지 않음: 나이가 원문에 없으면 임상의 판단 기준과 별개로 보류된 채 남음."""
    rule, meta = _stored("NCT07098338", "E10")
    x = _run(monkeypatch, "NCT07098338", [AGE, rule], meta, {"E10": ("no", "심장 질환 없음")}, "남성 환자. 심장 질환 없음.", cls=cls)
    age = next(a for a in x["match"].assessments if a.rule_id == "I1")
    assert age.passes is None and "structured_unlocated" in age.validation_flags
    assert x["match"].eligibility == "UNCERTAIN" and xb.violations(x["match"], x["parsed"]) == []


def test_boundary_keeps_n2_flags_on_a_rule_it_holds():
    rule = _rule(rule_id="I9", text="Adequate organ function", category="comorbidity", structured_evaluable=False, underspecified=True)
    from ctrec.schemas import CriterionAssessment
    a = CriterionAssessment(rule_id="I9", kind="inclusion", criterion_text=rule.text, holds="unknown", passes=None, confidence="low",
                            evidence="q", reasoning="r", missing_info=None, method="llm", category=rule.category,
                            evidence_directness="unlocated", validation_flags=["structured_unlocated"], underspecified=False)
    held = xb.enforce("T1", rule, a)
    assert held.evidence_directness == "unlocated" and held.validation_flags == ["structured_unlocated", xb.FLAG]


@pytest.mark.parametrize("text, grounded", [("나이 62.", True), ("나이 62. ECOG 1.", True), ("Age: 62.", True), ("age 62.", True),
                                            ("나이 62.5세", False), ("나이 62.5", False), ("나이 162", False)])
def test_sentence_period_after_age_is_not_a_decimal(text, grounded):
    pattern = vg._value_pattern("age", "62", "years")
    assert bool(pattern.search(text)) is grounded


# ---------------------------------------------------------------------------
# 임상의 판단 입력 계약 없음: 가짜 완료 상태를 만들지 않음
# ---------------------------------------------------------------------------

def test_boundary_has_no_clinician_passed_argument():
    assert list(inspect.signature(xb.enforce).parameters) == ["trial_id", "rule", "a"]
    assert list(inspect.signature(xb.enforce_all).parameters) == ["parsed", "assessments"]


def test_nothing_in_src_produces_a_clinician_decision():
    """decided_by="clinician"을 값으로 넘기거나 대입하는 코드가 없음. 입력 계약이 생기면 이 테스트를 그때 고칩니다."""
    found = []
    for f in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            value = node.value if isinstance(node, (ast.keyword, ast.Assign, ast.AnnAssign)) else None
            if isinstance(value, ast.Constant) and value.value == "clinician":
                name = node.arg if isinstance(node, ast.keyword) else getattr((node.targets[0] if isinstance(node, ast.Assign)
                                                                               else node.target), "id", None)
                if name in ("decided_by", "by"):
                    found.append(f"{f.name}:{node.lineno}")
    assert found == []


# ---------------------------------------------------------------------------
# 목록의 출처와 검수 상태
# ---------------------------------------------------------------------------

def _candidates():
    with open(ROOT / xb.CANDIDATE_TABLE, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r["adjudication_candidate"] == "expert"]


def test_every_registered_rule_names_its_source_and_stays_unreviewed():
    """검수 기록 없이 reviewed로 바꾸지 못함: 후보표의 상태가 '검수 필요'인 동안 목록도 unreviewed."""
    assert xb.EXPERT_RULE_SOURCES.keys() == xb.EXPERT_RULES.keys()
    rows = {(r["trial_id"], r["slot_id"]): r for r in _candidates()}
    for key, src in xb.EXPERT_RULE_SOURCES.items():
        trial_id = key.split(":")[0]
        assert src["slot"] == xb.EXPERT_RULES[key] and src["source"] == xb.CANDIDATE_TABLE
        assert rows[(trial_id, src["slot"])]["review_status"] == "검수 필요"
        assert src["review_status"] == "unreviewed"


def test_review_status_does_not_change_the_decision():
    """상태 필드는 표시용: 판정 분류는 목록 소속만 봄."""
    assert "EXPERT_RULE_SOURCES" not in inspect.getsource(xb.clinician_reason)
    assert "EXPERT_RULE_SOURCES" not in inspect.getsource(xb.enforce)
