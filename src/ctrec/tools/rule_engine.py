"""결정론적 규칙 엔진 (LLM 호출 없음).

나이·성별·ECOG·수치형 검사값처럼 기계적으로 비교 가능한 규칙은 여기서 먼저 판정해
LLM의 산술 오류를 막고, 판정 근거를 재현 가능하게 남깁니다.
판정할 수 없으면 None을 반환해 추론·매칭 에이전트(LLM)에게 넘깁니다.
"""

from __future__ import annotations

import operator as op
import re
from datetime import date, datetime

from ..schemas import CriterionAssessment, LabValue, PatientProfile, Rule

_OPS = {">=": op.ge, "<=": op.le, ">": op.gt, "<": op.lt, "==": op.eq, "!=": op.ne}

# 검사명 동의어 -> 표준명
LAB_ALIASES = {
    "hemoglobin": {"hemoglobin", "hgb", "hb", "haemoglobin"},
    "platelets": {"platelets", "platelet count", "plt"},
    "absolute neutrophil count": {"absolute neutrophil count", "anc", "neutrophils"},
    "white blood cell count": {"white blood cell count", "wbc"},
    "creatinine": {"creatinine", "serum creatinine", "cr"},
    "creatinine clearance": {"creatinine clearance", "crcl"},
    "egfr (renal)": {"egfr (renal)", "estimated glomerular filtration rate", "gfr"},
    "total bilirubin": {"total bilirubin", "bilirubin", "tbil"},
    "ast": {"ast", "aspartate aminotransferase", "sgot"},
    "alt": {"alt", "alanine aminotransferase", "sgpt"},
    "hba1c": {"hba1c", "hemoglobin a1c", "glycated hemoglobin", "a1c"},
    "ldl cholesterol": {"ldl cholesterol", "ldl", "ldl-c"},
    "inr": {"inr"},
    "albumin": {"albumin", "serum albumin"},
    "lvef": {"lvef", "left ventricular ejection fraction", "ejection fraction"},
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def canonical_lab(name: str) -> str:
    n = _norm(name)
    for canon, aliases in LAB_ALIASES.items():
        if n in aliases:
            return canon
    return n


# 표기만 다른 같은 단위 (cells/µL = /mm³ 등)
_UNIT_EQUIV = {
    "/ul": {"/ul", "cells/ul", "/mm3", "cells/mm3"},
    "x10^9/l": {"x10^9/l", "10^9/l", "x10e9/l", "10e9/l", "x10^3/ul", "10^3/ul", "k/ul"},
    "%": {"%", "percent"},
}


def _norm_unit(u: str | None) -> str:
    n = _norm(u or "").replace("µ", "u").replace("μ", "u").replace("×", "x").replace("mm³", "mm3").replace("³", "^3").replace("⁹", "^9")
    n = n.replace(" ", "").replace("*", "x")
    for canon, aliases in _UNIT_EQUIV.items():
        if n in aliases:
            return canon
    return n


def _to_float(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_date(s: str | None) -> date | None:
    """ISO 형식(YYYY-MM-DD, YYYY-MM, YYYY)만 인정. 그 외 표기는 None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _latest(labs: list[LabValue]) -> LabValue | None:
    """가장 최근 검사값. 값이 하나면 그대로, 여러 개면 모든 날짜를 해석할 수 있을 때만 최신값.

    (날짜 문자열을 그대로 정렬하면 'Aug 2026' 같은 표기에서 엉뚱한 값을 고를 수 있음)
    """
    if len(labs) == 1:
        return labs[0]
    if len({lab.value for lab in labs}) == 1:
        return labs[0]  # 값이 모두 같으면 어느 것을 골라도 같은 판정
    dated = [(_parse_date(lab.date), lab) for lab in labs]
    if any(d is None for d, _ in dated):
        return None
    dates = [d for d, _ in dated]
    latest = max(dates)
    if dates.count(latest) > 1:
        return None  # 같은 날짜에 다른 값 -> 확정 불가
    return next(lab for d, lab in dated if d == latest)


def _result(rule: Rule, holds: str, evidence: str, reasoning: str) -> CriterionAssessment:
    return CriterionAssessment(
        rule_id=rule.rule_id,
        kind=rule.kind,
        criterion_text=rule.text,
        holds=holds,  # type: ignore[arg-type]
        passes=passes(rule.kind, holds),
        confidence="high",
        evidence=evidence,
        reasoning=reasoning,
        missing_info=None,
        method="rule_engine",
        category=rule.category,
        underspecified=rule.underspecified,
        cohort=rule.cohort,
    )


def passes(kind: str, holds: str) -> bool | None:
    if holds == "unknown":
        return None
    return (holds == "yes") if kind == "inclusion" else (holds == "no")


# 나이 단위 -> 일. 만 나이는 '그 단위로 v 이상 v+1 미만'인 구간입니다.
_AGE_DAYS = {"years": 365.25, "months": 365.25 / 12, "weeks": 7.0, "days": 1.0}
_AGE_UNIT_ALIASES = {
    "years": {"y", "yr", "yrs", "year", "years", "세", "살", "년"},
    "months": {"m", "mo", "mos", "month", "months", "개월", "달"},
    "weeks": {"w", "wk", "wks", "week", "weeks", "주"},
    "days": {"d", "day", "days", "일"},
}


def _age_unit(u: str | None) -> str | None:
    """규칙 단위 정규화. 없으면 년(years), 알 수 없는 표기면 None."""
    n = _norm(u or "")
    if not n:
        return "years"
    for canon, aliases in _AGE_UNIT_ALIASES.items():
        if n in aliases:
            return canon
    return None


def _evaluate_age(rule: Rule, patient: PatientProfile) -> CriterionAssessment | None:
    """나이 비교. 같은 단위의 정수 기준은 관례대로 만 나이를 그대로 비교하고,
    단위가 다르거나 기준이 소수(예: 0.038년)이면 환자 나이 구간 [v, v+1)을 일 단위로 바꿔 비교합니다.
    구간 안에서 결과가 갈리면(예: 0세 vs '12주 미만') 확정하지 않고 None을 반환합니다.
    """
    target = _to_float(rule.value)
    rule_unit = _age_unit(rule.unit)
    if target is None or rule.operator not in _OPS or rule_unit is None:
        return None
    if patient.age_value is not None and patient.age_unit:
        value, unit = patient.age_value, patient.age_unit
    elif patient.age is not None:
        value, unit = float(patient.age), "years"
    else:
        return None
    shown = f"{value:g} {unit}"
    if unit == rule_unit and float(target).is_integer() and float(value).is_integer():
        holds = _OPS[rule.operator](value, target)
        return _result(rule, "yes" if holds else "no", f"환자 나이 {shown}",
                       f"{value:g} {rule.operator} {target:g} ({unit}) -> {holds}")
    lo, hi = value * _AGE_DAYS[unit], (value + 1) * _AGE_DAYS[unit]  # [lo, hi)
    t = target * _AGE_DAYS[rule_unit]
    cmp = _OPS[rule.operator]
    if rule.operator in ("==", "!="):
        return None
    # 구간 양 끝(hi는 포함되지 않으므로 아주 조금 안쪽)에서 결과가 같을 때만 확정
    at_lo, at_hi = cmp(lo, t), cmp(hi - 1e-6, t)
    if at_lo != at_hi:
        return None
    return _result(rule, "yes" if at_lo else "no", f"환자 나이 {shown}",
                   f"{shown} = {lo:.0f}~{hi:.0f}일, 기준 {target:g} {rule_unit} = {t:.0f}일 -> {at_lo}")


def evaluate(rule: Rule, patient: PatientProfile) -> CriterionAssessment | None:
    if not rule.structured_evaluable or not rule.field or not rule.operator:
        return None
    field = _norm(rule.field)

    if field == "age":
        return _evaluate_age(rule, patient)

    if field == "sex":
        if not rule.value or rule.operator not in ("==", "!="):
            return None
        want = _norm(rule.value)
        if want in ("all", "any") and rule.operator == "==":
            return _result(rule, "yes", f"환자 성별 {patient.sex}", "성별 제한 없음 (환자 성별과 무관하게 충족)")
        if patient.sex == "unknown":
            return None
        holds = (patient.sex == want) if rule.operator == "==" else (patient.sex != want)
        return _result(rule, "yes" if holds else "no", f"환자 성별 {patient.sex}",
                       f"sex {rule.operator} {want} -> {holds}")

    if field == "ecog":
        target = _to_float(rule.value)
        if patient.ecog is None or target is None or rule.operator not in _OPS:
            return None
        holds = _OPS[rule.operator](patient.ecog, target)
        return _result(rule, "yes" if holds else "no", f"ECOG {patient.ecog}",
                       f"{patient.ecog} {rule.operator} {rule.value} -> {holds}")

    if field.startswith("lab:"):
        target = _to_float(rule.value)
        if target is None or rule.operator not in _OPS or rule.time_window_days:
            # 시간 조건이 붙은 검사는 날짜 해석이 필요하므로 LLM에 위임
            return None
        want = canonical_lab(field[4:])
        candidates = [lab for lab in patient.labs if canonical_lab(lab.name) == want and lab.value is not None]
        if not candidates:
            return None
        lab = _latest(candidates)
        if lab is None:
            return None  # 같은 검사가 여러 번인데 최신 값을 날짜로 확정할 수 없음 -> LLM에 위임
        if (rule.unit or lab.unit) and _norm_unit(rule.unit) != _norm_unit(lab.unit):
            return None  # 단위가 다르거나 한쪽만 적혀 있으면 숫자만 비교하지 않음 (예: Hb 100 g/L vs 9 g/dL)
        holds = _OPS[rule.operator](lab.value, target)
        return _result(rule, "yes" if holds else "no",
                       f"{lab.name} {lab.value} {lab.unit or ''} ({lab.date or '날짜 미상'}) - \"{lab.source_quote}\"",
                       f"{lab.value} {rule.operator} {rule.value} -> {holds}")

    return None
