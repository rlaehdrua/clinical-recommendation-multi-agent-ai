"""결정론적 규칙 엔진 (LLM 호출 없음).

나이·성별·ECOG·수치형 검사값처럼 기계적으로 비교 가능한 규칙은 여기서 먼저 판정해
LLM의 산술 오류를 막고, 판정 근거를 재현 가능하게 남깁니다.
판정할 수 없으면 None을 반환해 추론·매칭 에이전트(LLM)에게 넘깁니다.
"""

from __future__ import annotations

import operator as op
import re

from ..schemas import CriterionAssessment, PatientProfile, Rule

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


def _norm_unit(u: str | None) -> str:
    return _norm(u or "").replace("µ", "u").replace("μ", "u")


def _to_float(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


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


def evaluate(rule: Rule, patient: PatientProfile) -> CriterionAssessment | None:
    if not rule.structured_evaluable or not rule.field or not rule.operator:
        return None
    field = _norm(rule.field)

    if field == "age":
        target = _to_float(rule.value)
        if patient.age is None or target is None or rule.operator not in _OPS:
            return None
        holds = _OPS[rule.operator](patient.age, target)
        return _result(rule, "yes" if holds else "no", f"환자 나이 {patient.age}세",
                       f"{patient.age} {rule.operator} {rule.value} -> {holds}")

    if field == "sex":
        if patient.sex == "unknown" or not rule.value or rule.operator not in ("==", "!="):
            return None
        want = _norm(rule.value)
        if want in ("all", "any"):
            return _result(rule, "yes", f"환자 성별 {patient.sex}", "성별 제한 없음")
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
        # 날짜가 있는 경우 가장 최근 값 사용
        lab = sorted(candidates, key=lambda x: x.date or "")[-1]
        if rule.unit and lab.unit and _norm_unit(rule.unit) != _norm_unit(lab.unit):
            return None  # 단위 변환은 LLM에 위임
        holds = _OPS[rule.operator](lab.value, target)
        return _result(rule, "yes" if holds else "no",
                       f"{lab.name} {lab.value} {lab.unit or ''} ({lab.date or '날짜 미상'}) - \"{lab.source_quote}\"",
                       f"{lab.value} {rule.operator} {rule.value} -> {holds}")

    return None
