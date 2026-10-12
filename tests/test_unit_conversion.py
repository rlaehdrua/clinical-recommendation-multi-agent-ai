"""규칙 엔진 검사 단위: 표기만 다른 같은 단위와 환산이 필요한 세포 수 단위 (Phase 0-3).

- 같은 단위(표기만 다름): /uL = /µL = /microL = cells/microliter = /mm^3 = /mm3, 10^9/L = x10^9/L = 10*9/L = 10^3/µL
- 환산(세포 수 검사만): 1 x10^9/L = 1,000 /µL, 1 x10^12/L = 1,000 x10^9/L, G/L(기가) = 10^9/L
- 알 수 없거나 호환되지 않는 단위, 판단할 수 없는 'g/L'(그램인지 기가인지)는 판정하지 않음(None -> LLM)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.schemas import LabValue
from ctrec.tools import rule_engine
from ctrec.tools.rule_engine import _norm_unit

from test_offline import _profile, _rule


def _lab_rule(name="platelets", op=">=", value="100", unit="10^9/L", kind="inclusion"):
    return _rule(rule_id="I1", kind=kind, text="t", category="lab", field=f"lab:{name}", operator=op,
                 value=value, unit=unit)


def _patient(value, unit, name="platelets"):
    return _profile(labs=[LabValue(name=name, value=value, unit=unit, date="2026-09-01", source_quote="q")])


def _holds(rule, value, unit, name="platelets"):
    r = rule_engine.evaluate(rule, _patient(value, unit, name))
    return None if r is None else r.holds


# ------------------------------------------------------------ 1. 같은 단위 별칭
@pytest.mark.parametrize("unit", ["/uL", "/µL", "/μL", "/microL", "cells/microL", "cells/microliter", "/mm^3", "/mm3",
                                  "/mm³", "cells/mm3", "cells/µL"])
def test_per_microliter_aliases(unit):
    assert _norm_unit(unit) == _norm_unit("/ul")


@pytest.mark.parametrize("unit", ["x10^9/L", "10^9/L", "10*9/L", "x 10*9/L", "×10⁹/L", "10⁹/L", "10^3/µL", "K/uL"])
def test_giga_per_liter_aliases(unit):
    assert _norm_unit(unit) == _norm_unit("10^9/L")


def test_per_microliter_alias_compares_without_conversion():
    assert _holds(_lab_rule(value="100000", unit="cells/microliter"), 150000, "/µL") == "yes"
    assert _holds(_lab_rule(value="125000", unit="/microL"), 120000, "/mm^3") == "no"


# ------------------------------------------------------------ 2. /µL <-> 10^9/L 양방향 환산
def test_per_microliter_patient_against_giga_rule():
    """150,000 /µL = 150 x10^9/L."""
    assert _holds(_lab_rule(op=">=", value="150", unit="10^9/L"), 150000, "/µL") == "yes"
    assert _holds(_lab_rule(op=">=", value="151", unit="10^9/L"), 150000, "/µL") == "no"


def test_giga_patient_against_per_microliter_rule():
    """150 x10^9/L = 150,000 /µL."""
    assert _holds(_lab_rule(op=">=", value="100000", unit="cells/microliter"), 150, "x10^9/L") == "yes"
    assert _holds(_lab_rule(op=">=", value="100000", unit="/microL"), 99, "10^9/L") == "no"


def test_conversion_is_shown_in_reasoning():
    r = rule_engine.evaluate(_lab_rule(op=">=", value="150", unit="10^9/L"), _patient(150000, "/µL"))
    assert "150000" in r.evidence and "150 10^9/L" in r.reasoning


# ------------------------------------------------------------ 3. G/L = 10^9/L
def test_giga_per_liter_G_is_10_9_per_liter():
    assert _holds(_lab_rule(op=">=", value="150", unit="10^9/L"), 150, "G/L") == "yes"
    assert _holds(_lab_rule(op=">=", value="150000", unit="/µL"), 150, "G/L") == "yes"
    assert _holds(_lab_rule(op=">=", value="150", unit="G/L"), 149, "10^9/L") == "no"


def test_lowercase_g_per_liter_is_not_assumed_giga():
    """'g/L'은 그램/리터일 수 있으므로 세포 수 검사에서 기가로 추측하지 않음."""
    assert _holds(_lab_rule(op=">=", value="150", unit="10^9/L"), 150, "g/L") is None


def test_g_per_liter_on_mass_analyte_is_not_giga():
    """혈색소 g/L은 질량 농도. 10^9/L과 같다고 보지 않음 (항목명과 단위를 함께 확인)."""
    assert _holds(_lab_rule(name="hemoglobin", op=">=", value="100", unit="10^9/L"), 120, "G/L", "hemoglobin") is None


# ------------------------------------------------------------ 4. 10^12/L 환산계수 (10^9/L의 1,000배)
def test_tera_per_liter_factor():
    assert rule_engine._count_per_ul("10^12/L") == 1000 * rule_engine._count_per_ul("10^9/L") == 1e6
    assert _holds(_lab_rule(op=">=", value="150", unit="10^9/L"), 0.15, "10^12/L") == "yes"
    assert _holds(_lab_rule(op=">=", value="150", unit="10^9/L"), 0.149, "10^12/L") == "no"
    assert _holds(_lab_rule(name="white blood cell count", op="<=", value="3.0", unit="10^9/L"), 0.003, "10^12/L",
                  "white blood cell count") == "yes"
    assert _holds(_lab_rule(name="white blood cell count", op="<=", value="3.0", unit="10^9/L"), 0.0031, "10^12/L",
                  "white blood cell count") == "no"


def test_count_rule_in_nonstandard_unit_is_not_auto_judged():
    """혈소판 기준이 표준 보고 단위가 아닌 10^12/L로 적혀 있으면(NCT06125405 E4 유형) 정정하지 않고 판정하지 않음.

    단위대로 환산하면 '< 100 x10^12/L'(= 100,000 x10^9/L)에 모든 환자가 해당하게 되므로, 환자 단위와 관계없이 LLM에 넘김.
    """
    rule = _lab_rule(op="<", value="100", unit="10^12/L", kind="exclusion")
    for value, unit in [(150, "10^9/L"), (150000, "/µL"), (0.15, "10^12/L")]:
        assert _holds(rule, value, unit) is None


# ------------------------------------------------------------ 5. 단위 불일치·미지원 단위
@pytest.mark.parametrize("unit", ["mg/dL", "%", "cells", "thousand", "/L", "mmol/L", None])
def test_incompatible_or_unknown_units_defer(unit):
    assert _holds(_lab_rule(op=">=", value="100", unit="10^9/L"), 150, unit) is None


def test_count_conversion_only_for_count_analytes():
    """세포 수가 아닌 검사에는 /µL <-> 10^9/L 환산을 쓰지 않음."""
    assert _holds(_lab_rule(name="hemoglobin", op=">=", value="9", unit="10^9/L"), 9000, "/µL", "hemoglobin") is None


# ------------------------------------------------------------ 6. 경계값과 비교 방향 (환산 후에도 유지)
@pytest.mark.parametrize("op,expected", [(">=", "yes"), (">", "no"), ("<=", "yes"), ("<", "no")])
def test_boundary_after_conversion(op, expected):
    assert _holds(_lab_rule(op=op, value="1.5", unit="10^9/L", name="absolute neutrophil count"), 1500, "cells/µL",
                  "absolute neutrophil count") == expected


@pytest.mark.parametrize("op,expected", [(">=", "yes"), (">", "no"), ("<=", "yes"), ("<", "no")])
def test_boundary_reverse_conversion(op, expected):
    assert _holds(_lab_rule(op=op, value="1000", unit="cells/microliter", name="absolute neutrophil count"), 1.0,
                  "10^9/L", "absolute neutrophil count") == expected


def test_boundary_with_float_rounding():
    """0.1 x10^9/L = 100 /µL: 부동소수점 오차로 경계 판정이 뒤집히지 않음."""
    assert _holds(_lab_rule(op=">=", value="100", unit="/µL", name="white blood cell count"), 0.1, "10^9/L",
                  "white blood cell count") == "yes"
    assert _holds(_lab_rule(op="<", value="0.3", unit="10^9/L", name="white blood cell count"), 300, "/µL",
                  "white blood cell count") == "no"


# ------------------------------------------------------------ 7. 기존 정상 판정 유지
def test_existing_same_unit_and_mass_behavior_kept():
    assert _holds(_lab_rule(name="hemoglobin", op=">=", value="9.0", unit="g/dL"), 10.0, "g/dL", "hemoglobin") == "yes"
    assert _holds(_lab_rule(name="hemoglobin", op=">=", value="90", unit="g/L"), 10.0, "g/dL", "hemoglobin") is None
    assert _holds(_lab_rule(op=">=", value="100", unit="x10^9/L"), 150, "10³/µL") == "yes"
    assert _holds(_lab_rule(name="creatinine clearance", op=">=", value="60", unit="ml/min"), 72, "mL/min",
                  "creatinine clearance") == "yes"


# ------------------------------------------------------------ 기준 단위 정책: 판정 가능 여부가 환자 단위에 따라 달라지지 않음
def _frozen_rule(trial_id: str, rule_id: str):
    import json

    from ctrec import trial_store
    trial = json.loads((Path(__file__).parent.parent / "data" / "trials" / f"{trial_id}.json").read_text(encoding="utf-8"))
    return next(r for r in trial_store.load(trial).rules if r.rule_id == rule_id)


@pytest.mark.parametrize("value,unit", [(0.15, "10^12/L"), (150, "10^9/L"), (150000, "/µL"), (90, "10^12/L")])
def test_nct06125405_e4_not_judged_for_any_patient_unit(value, unit):
    """NCT06125405 E4 '혈소판 < 100 x10^12/L': 기준값을 정정하지 않고, 환자 값의 단위와 관계없이 규칙 엔진이 판정하지 않음.

    10^12/L은 혈소판·백혈구의 표준 보고 단위가 아니므로(적혈구 단위) 원문 확인이 필요한 기준으로 보고 LLM에 넘김.
    """
    rule = _frozen_rule("NCT06125405", "E4")
    assert (rule.field, rule.operator, rule.value, rule.unit) == ("lab:platelets", "<", "100", "10^12/L")  # 원문 그대로
    assert _holds(rule, value, unit) is None


COUNT_RULES = [("NCT00575185", "I11"), ("NCT00575185", "I12"), ("NCT06125405", "E2")]
EQUIVALENT = {"/ul": [(1.0, "/µL"), (1.0, "cells/microL"), (1e-3, "10^9/L"), (1e-3, "G/L"), (1e-6, "10^12/L")],
              "x10^9/l": [(1e3, "/µL"), (1e3, "/mm^3"), (1.0, "10^9/L"), (1.0, "G/L"), (1e-3, "10^12/L")]}


@pytest.mark.parametrize("trial_id,rule_id", COUNT_RULES)
@pytest.mark.parametrize("ratio", [0.9, 1.0, 1.1])
def test_same_criterion_same_verdict_for_every_patient_unit(trial_id, rule_id, ratio):
    """실제 기준표의 세포 수 기준: 같은 값을 어떤 단위로 적어도 판정이 같음 (판정 가능 여부 포함)."""
    rule = _frozen_rule(trial_id, rule_id)
    name = rule.field[4:]
    family = rule_engine._norm_unit(rule.unit)
    verdicts = {_holds(rule, float(rule.value) * ratio * factor, unit, name) for factor, unit in EQUIVALENT[family]}
    assert len(verdicts) == 1 and None not in verdicts


@pytest.mark.parametrize("value", ["1", "100", "1000000"])
def test_standard_unit_criterion_not_blocked_by_threshold_size(value):
    """표준 단위로 적힌 기준은 기준값 크기로 막지 않음 (임의의 생리적 범위를 쓰지 않음)."""
    rule = _lab_rule(op=">=", value=value, unit="/µL")
    assert _holds(rule, float(value), "/µL") == "yes"
    assert _holds(rule, float(value) / 1000, "10^9/L") == "yes"
