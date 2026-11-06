"""슬롯 값 -> 기준 성립 여부(holds). 생성기의 정답 계산과 검증기가 같은 함수를 씁니다.

- engine     : 기존 결정론적 규칙 엔진(tools/rule_engine.evaluate)을 그대로 호출 (나이·성별·ECOG·검사 수치).
               엔진 호출에서는 시간 조건을 떼고 값만 비교합니다. 시간 조건은 holds3(check_fresh=True)가 따로 봅니다:
               마지막 검사(lab_age_days)가 기준의 유효 기간보다 오래되면 값이 있어도 '미확인' (새 결과지 확인 필요).
               정답(oracle)은 검사값이 그사이 변하지 않았다고 가정하고 계산합니다.
- categorical: 슬롯 사전에서 사람이 검수한 값 집합 (in / not_in).
- also/combine: 추가 조건을 all(모두) / any(하나라도)로 묶음. 모르는 조건이 있으면 3값 논리
               (all: 하나라도 불성립이면 불성립, any: 하나라도 성립이면 성립, 그 밖에는 미확인).
엔진이 판정하지 못하면(None) 정답을 만들 수 없으므로 IndeterminateTruth를 냅니다.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..schemas import Holds, LabValue, ParsedTrial, PatientProfile, Rule
from ..tools import rule_engine
from .spec import LAB_AGE, Binding, Cond, SlotSpec

LAB_PREFIX = "lab."


class IndeterminateTruth(ValueError):
    pass


def slot_for_field(field: str | None) -> str | None:
    """규칙 엔진 필드 -> 슬롯 ID. lab:<이름>은 엔진의 표준 검사명으로 맞춥니다 (hgb, hb -> lab.hemoglobin)."""
    if not field:
        return None
    f = field.strip().lower()
    if f in ("age", "sex", "ecog"):
        return f
    if f.startswith("lab:"):
        return LAB_PREFIX + rule_engine.canonical_lab(f[4:])
    return None


def lab_date(reference_date: str) -> str:
    return (date.fromisoformat(reference_date) - timedelta(days=1)).isoformat()


def profile_from(values: dict[str, float | str], slots: dict[str, SlotSpec], reference_date: str,
                 patient_id: str = "synthetic") -> PatientProfile:
    # 범주형 검사 슬롯(예: lab.hbv_status)은 규칙 엔진이 다루지 않으므로 수치 슬롯만 검사값으로 넣음
    labs = [LabValue(name=sid[len(LAB_PREFIX):], value=float(v), unit=slots[sid].unit, date=lab_date(reference_date),
                     source_quote="synthetic")
            for sid, v in values.items() if sid.startswith(LAB_PREFIX) and slots[sid].kind == "numeric"]
    age = values.get("age")
    return PatientProfile(
        patient_id=patient_id, age=int(age) if age is not None else None, sex=str(values.get("sex", "unknown")),  # type: ignore[arg-type]
        primary_diagnosis=None, disease_stage=None, diagnoses=[], biomarkers=[], labs=labs, medications=[],
        treatment_history=[], ecog=int(values["ecog"]) if "ecog" in values else None, other_facts=[],
        missing_or_ambiguous=[],
    )


def _cond(c: Cond, values: dict[str, float | str]) -> Holds:
    if c.slot not in values:
        return "unknown"
    inside = str(values[c.slot]) in c.values
    return "yes" if inside == (c.op == "in") else "no"


def _engine(binding: Binding, rule: Rule | None, values: dict[str, float | str], slots: dict[str, SlotSpec],
            reference_date: str, check_fresh: bool) -> Holds:
    if binding.slot not in values:
        return "unknown"
    if check_fresh and binding.time_window_days is not None and float(values.get(LAB_AGE, 0)) > binding.time_window_days:
        return "unknown"  # 값은 있지만 유효 기간이 지난 검사
    if rule is None:
        raise IndeterminateTruth(f"{binding.key}: 원본 기준 없음")
    update: dict = {"time_window_days": None}
    if binding.rule_override:
        update.update(binding.rule_override, structured_evaluable=True)
    plain = rule.model_copy(update=update)
    a = rule_engine.evaluate(plain, profile_from(values, slots, reference_date))
    if a is None or a.holds == "unknown":
        raise IndeterminateTruth(f"{binding.key}: 규칙 엔진이 {binding.slot}={values[binding.slot]!r}을 판정하지 못함")
    return a.holds


def combine3(parts: list[Holds], mode: str) -> Holds:
    decisive, other = ("no", "yes") if mode == "all" else ("yes", "no")
    if decisive in parts:
        return decisive  # type: ignore[return-value]
    return "unknown" if "unknown" in parts else other  # type: ignore[return-value]


def holds3(binding: Binding, rule: Rule | None, values: dict[str, float | str], slots: dict[str, SlotSpec],
           reference_date: str, check_fresh: bool = False) -> Holds:
    """알려진 값(values)만으로 본 기준 성립 여부: yes / no / unknown."""
    if binding.truth == "categorical":
        main = _cond(Cond(slot=binding.slot, op=binding.op, values=binding.values), values)
    else:
        main = _engine(binding, rule, values, slots, reference_date, check_fresh)
    if not binding.also:
        return main
    return combine3([main, *(_cond(c, values) for c in binding.also)], binding.combine)


def rule_holds(binding: Binding, rule: Rule | None, values: dict[str, float | str], slots: dict[str, SlotSpec],
               reference_date: str) -> Holds:
    """정답 계산용: 필요한 값이 모두 있어야 함 (모르면 IndeterminateTruth)."""
    h = holds3(binding, rule, values, slots, reference_date)
    if h == "unknown":
        missing = [s for s in binding.slots_used() if s not in values]
        raise IndeterminateTruth(f"{binding.key}: 슬롯 {', '.join(missing) or binding.slot} 값 없음")
    return h


def passes(kind: str, holds: Holds) -> bool | None:
    return rule_engine.passes(kind, holds)


def rule_index(parsed: dict[str, ParsedTrial]) -> dict[str, Rule]:
    return {f"{tid}:{r.rule_id}": r for tid, p in parsed.items() for r in p.rules}
