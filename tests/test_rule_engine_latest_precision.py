"""같은 검사 값이 여러 개일 때 월·연 단위 날짜로 최신값을 추정하지 않음 (감사 V5 F-04).

같은 파일의 원칙(_exact_date: 임의로 1일·말일을 가정하지 않음)을 최신값 고르기에도 적용합니다.
"""

from __future__ import annotations

from ctrec.schemas import LabValue, PatientProfile, Rule
from ctrec.tools import rule_engine

PLT = Rule(rule_id="I3", kind="inclusion", text="Platelets >= 100 x10^9/L", category="lab", field="lab:platelets",
           operator=">=", value="100", unit="x10^9/L", time_window_days=None, structured_evaluable=True,
           underspecified=False, cohort=None)


def prof(*labs: LabValue) -> PatientProfile:
    return PatientProfile(patient_id="X", age=60, sex="male", primary_diagnosis=None, disease_stage=None, diagnoses=[],
                          biomarkers=[], labs=list(labs), medications=[], treatment_history=[], ecog=1, other_facts=[],
                          missing_or_ambiguous=[])


def plt(value: float, date: str) -> LabValue:
    return LabValue(name="platelets", value=value, unit="x10^9/L", date=date, source_quote=f"{date}: platelets {value:g}")


def test_month_only_value_that_may_be_later_is_not_ignored():
    # "2026-10"의 60은 10월 6~31일일 수 있어 2026-10-05의 150보다 나중일 수 있음 -> 확정하지 않음
    r = rule_engine.evaluate(PLT, prof(plt(60, "2026-10"), plt(150, "2026-10-05")))
    assert r is not None and r.holds == "unknown" and r.passes is None


def test_month_only_ambiguity_with_the_same_result_still_decides():
    r = rule_engine.evaluate(PLT, prof(plt(120, "2026-10"), plt(150, "2026-10-05")))
    assert r is not None and r.holds == "yes"


def test_month_precision_that_is_clearly_earlier_still_picks_the_later_day():
    r = rule_engine.evaluate(PLT, prof(plt(60, "2026-09"), plt(150, "2026-10-05")))
    assert r is not None and r.holds == "yes" and "2026-10-05" in r.evidence


def test_year_only_value_that_may_be_later_is_not_ignored():
    r = rule_engine.evaluate(PLT, prof(plt(150, "2026-03-01"), plt(60, "2026")))
    assert r is not None and r.holds == "unknown" and r.passes is None


def test_exact_dates_keep_existing_behaviour():
    assert rule_engine.evaluate(PLT, prof(plt(60, "2026-09-01"), plt(150, "2026-10-05"))).holds == "yes"
    assert rule_engine.evaluate(PLT, prof(plt(150, "2026-09-01"), plt(60, "2026-10-05"))).holds == "no"
    # 같은 날 다른 값, 해석할 수 없는 날짜: 기존대로 규칙 엔진이 정하지 않음 (LLM 위임)
    assert rule_engine.evaluate(PLT, prof(plt(60, "2026-10-05"), plt(150, "2026-10-05"))) is None
    assert rule_engine.evaluate(PLT, prof(plt(60, "Oct 2026"), plt(150, "2026-10-05"))) is None
