"""파싱 검증기가 올바른 규칙 엔진 필드를 지우지 않는지 (Phase 0-2).

재현 사례 (data/parsed, 2026-10-11 컴파일):
- NCT06878144 "Patients must be 18-100 years old" -> I1 age >= 18, I2 age <= 100 (같은 원문 문장을 나눈 규칙)
- NCT07679893 "40-80 years old ..." -> I1 age >= 40, I2 age <= 80
I1만 '다른 파싱 실행과 다름(engine)'으로 위험 항목이 되어 검증 LLM에 혼자 보내졌고, LLM은 상한이 빠졌다며
structured_ok=false로 판정 -> 하한 필드가 지워지고 unit만 남음.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.agents import criteria_parser, parser_validator
from ctrec.agents.parser_validator import RuleCheck, ValidationOutput, ValidationReport
from ctrec.schemas import MatcherOutput, ParsedTrial

from test_offline import _rule

AGE_TEXT = "Patients must be 18-100 years old"
TRIAL = {"trial_id": "T1", "title": "t", "minimum_age": "18 Years", "maximum_age": "100 Years",
         "eligibility_criteria": f"Inclusion Criteria:\n* {AGE_TEXT}\n* Platelet count equal to 125,000-500,000/microL.\n"
                                 "* Hemoglobin >= 9 g/dL\n\nExclusion Criteria:\n* Pregnancy\n"}
PLT_TEXT = "Platelet count equal to 125,000-500,000/microL."


def _age_rules(lo="18", unit="years"):
    return [_rule(rule_id="I1", text=AGE_TEXT, field="age", operator=">=", value=lo, unit=unit),
            _rule(rule_id="I2", text=AGE_TEXT, field="age", operator="<=", value="100", unit=unit)]


def _parsed(rules) -> ParsedTrial:
    return ParsedTrial(trial_id="T1", title="t", target_population="", intervention_summary="", key_conditions=[],
                       cohorts=[], rules=rules, parsing_notes="")


def _check(rid, **kw) -> RuleCheck:
    base = dict(rule_id=rid, meaning_ok=True, raw_is_vague=False, category="demographic", structured_ok=True,
                required_evidence="self_report_ok", note="n")
    base.update(kw)
    return RuleCheck(**base)


def _apply(rules, checks, risk=None):
    p = _parsed(rules)
    report = ValidationReport()
    risk = risk or {c.rule_id: ["다른 파싱 실행과 다름(engine)"] for c in checks}
    meta = parser_validator.apply_checks(p, TRIAL, risk, ValidationOutput(checks=checks, missing=[]), report)
    return {r.rule_id: r for r in p.rules}, meta, report


# ------------------------------------------------------------ 재현: 범위 하한이 지워짐
def test_range_lower_bound_kept_when_llm_saw_it_alone():
    """하한·상한을 나눈 규칙에서 하한만 보고 '상한 누락'이라 판정해도 원문과 맞는 하한 필드는 지우지 않음."""
    by, meta, report = _apply(_age_rules(), [_check("I1", structured_ok=False, note="상한 100세를 누락")])
    i1 = by["I1"]
    assert (i1.field, i1.operator, i1.value, i1.unit) == ("age", ">=", "18", "years") and i1.structured_evaluable
    assert not any("I1: 규칙 엔진 필드 제거" in c for c in report.changes)
    assert any(n.startswith("I1:") and "I2" in n for n in report.notes)  # 적용하지 않은 판단은 기록
    assert meta["I1"].review is None


def test_range_upper_bound_kept_symmetrically():
    by, _, _ = _apply(_age_rules(), [_check("I2", structured_ok=False, note="하한 18세를 누락")])
    assert (by["I2"].field, by["I2"].operator, by["I2"].value) == ("age", "<=", "100")


def test_lab_range_split_kept():
    rules = [_rule(rule_id="I3", text=PLT_TEXT, category="lab", field="lab:platelets", operator=">=", value="125000",
                   unit="/microL"),
             _rule(rule_id="I4", text=PLT_TEXT, category="lab", field="lab:platelets", operator="<=", value="500000",
                   unit="/microL")]
    by, _, _ = _apply(rules, [_check("I3", structured_ok=False, category="lab", note="상한 누락")])
    assert by["I3"].field == "lab:platelets" and by["I3"].value == "125000"


def test_cross_run_comparison_uses_all_rules_with_same_text():
    """다른 실행도 같은 문장을 하한·상한으로 나눴으면 위험 항목이 아님 (같은 문장 규칙 중 마지막 것과만 비교하던 문제)."""
    p = _parsed(_age_rules())
    other = _parsed(_age_rules())
    risk = parser_validator.detect_risk(p, {}, [other])
    assert "I1" not in risk and "I2" not in risk


def test_cross_run_difference_still_detected():
    p = _parsed(_age_rules())
    other = _parsed(_age_rules(lo="21"))
    risk = parser_validator.detect_risk(p, {}, [other])
    assert any("다른 파싱 실행과 다름" in m for m in risk.get("I1", []))


def test_llm_sees_rules_split_from_same_sentence(monkeypatch):
    """검증 LLM에 같은 원문 문장을 나눈 다른 규칙을 함께 보여 줌 (I1만 보고 상한 누락으로 오판하지 않도록)."""
    seen = {}

    def fake(model, system, user, **k):
        seen["user"] = user
        return ValidationOutput(checks=[], missing=[])

    monkeypatch.setattr(parser_validator.llm, "structured", fake)
    p = _parsed(_age_rules())
    parser_validator._llm_validate(p, TRIAL, {"I1": ["다른 파싱 실행과 다름(engine)"]}, [])
    payload = json.loads(seen["user"].split("<rules_to_check>")[1].split("</rules_to_check>")[0])
    assert [r["rule_id"] for r in payload] == ["I1"]
    assert payload[0]["same_sentence_rules"] == [{"rule_id": "I2", "field": "age", "operator": "<=", "value": "100",
                                                  "unit": "years"}]


# ------------------------------------------------------------ 실제로 잘못된 필드는 지우되 unit도 함께
def test_wrong_value_in_range_is_removed_with_unit():
    """원문 범위와 값이 다르면(18-100인데 21) 범위 보호를 받지 않고 지움. unit도 함께 비워 필드·단위 불일치를 남기지 않음."""
    by, meta, report = _apply(_age_rules(lo="21"), [_check("I1", structured_ok=False, note="하한이 원문과 다름")])
    i1 = by["I1"]
    assert (i1.field, i1.operator, i1.value, i1.unit) == (None, None, None, None) and not i1.structured_evaluable
    assert any("I1: 규칙 엔진 필드 제거" in c for c in report.changes)
    assert by["I2"].field == "age"  # 문제없는 짝 규칙은 그대로


def test_single_bound_from_range_without_partner_is_removed():
    """범위 문장인데 한쪽 경계 규칙만 있으면 LLM 판정(상한 누락)이 맞음 -> 지우고 LLM이 원문으로 판정."""
    by, _, _ = _apply(_age_rules()[:1], [_check("I1", structured_ok=False, note="상한 누락")])
    assert by["I1"].field is None and by["I1"].unit is None


def test_wrong_unit_in_range_is_removed():
    by, _, _ = _apply(_age_rules(unit="months"), [_check("I1", structured_ok=False, note="단위가 다름")])
    assert by["I1"].field is None and by["I1"].unit is None


def test_deterministic_demotion_clears_unit():
    p = _parsed([_rule(rule_id="I1", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin",
                       operator="in", value="9", unit="g/dL")])  # 규칙 엔진이 처리하지 않는 연산자
    issues = parser_validator.deterministic_check(p, TRIAL)
    r = p.rules[0]
    assert issues["I1"] and (r.field, r.operator, r.value, r.unit) == (None, None, None, None)


def test_integrity_demotion_clears_unit():
    p = _parsed([_rule(rule_id="I1", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin",
                       operator=">=", value="10", unit="g/dL")])
    assert criteria_parser.check_integrity(p, TRIAL)
    r = p.rules[0]
    assert (r.field, r.operator, r.value, r.unit) == (None, None, None, None)
