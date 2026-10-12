"""값싼 확정 제외(cheap hard filter, 아키텍처 §26–27): 후보 추림(LLM) 전에 명백히 맞지 않는 시험을 코드로 뺍니다.

명확한 불일치만 씁니다. 하나라도 확실하지 않으면 남깁니다(정보 부족 ≠ 부적격).
- 모집 상태: 모집이 끝난 시험 (config.HARD_FILTER_STATUS가 켜져 있을 때)
- 나이: CT.gov 구조화 필드(minimum_age / maximum_age)와 환자 나이 구간이 겹치지 않음
- 성별: 시험이 한 성별 전용(MALE/FEMALE)인데 환자 성별이 확실히 다름

건강 지원자(healthy volunteers) 불일치는 CT.gov 필드가 '건강인도 받는다'는 뜻이라 환자를 확실히 뺄 근거가 되지 않아 쓰지 않습니다.
세부 기준(아형, 바이오마커, 치료 이력 등)은 여기서 판단하지 않습니다.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from . import config
from .agents.recommender import is_open, status_of
from .schemas import PatientProfile
from .tools.rule_engine import _AGE_DAYS, _age_unit


class HardFiltered(BaseModel):
    trial_id: str
    kind: Literal["status", "age", "sex"]
    reason: str


_CTGOV_AGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*$")


def _trial_age_days(s: str | None) -> tuple[float, float] | None:
    """'18 Years' -> (값, 단위의 일수). 해석할 수 없으면 None (그 경계는 쓰지 않음)."""
    m = _CTGOV_AGE.match(s or "")
    if not m:
        return None
    unit = _age_unit(m.group(2))
    return (float(m.group(1)), _AGE_DAYS[unit]) if unit else None


def _patient_age_days(p: PatientProfile) -> tuple[float, float] | None:
    """환자 나이 구간 [lo, hi) (일). 만 나이 v는 'v 이상 v+1 미만'."""
    if p.age_value is not None and p.age_unit:
        v, unit = p.age_value, p.age_unit
    elif p.age is not None:
        v, unit = float(p.age), "years"
    else:
        return None
    return v * _AGE_DAYS[unit], (v + 1) * _AGE_DAYS[unit]


def check(trial: dict, profile: PatientProfile) -> HardFiltered | None:
    tid = trial["trial_id"]
    if config.HARD_FILTER_STATUS and not is_open(trial):
        return HardFiltered(trial_id=tid, kind="status", reason=f"모집 종료({status_of(trial)})")

    age = _patient_age_days(profile)
    if age is not None:
        lo, hi = age
        mn, mx = _trial_age_days(trial.get("minimum_age")), _trial_age_days(trial.get("maximum_age"))
        if mn and hi <= mn[0] * mn[1]:
            return HardFiltered(trial_id=tid, kind="age",
                                reason=f"나이 하한 {trial['minimum_age']} 미만 (환자 나이 확정 구간 기준)")
        # 상한 'N Years'는 N세까지 포함 -> N+1세가 되는 날부터 벗어남
        if mx and lo >= (mx[0] + 1) * mx[1]:
            return HardFiltered(trial_id=tid, kind="age",
                                reason=f"나이 상한 {trial['maximum_age']} 초과 (환자 나이 확정 구간 기준)")

    sex = (trial.get("sex") or "ALL").upper()
    if sex in ("MALE", "FEMALE") and profile.sex in ("male", "female") and profile.sex.upper() != sex:
        return HardFiltered(trial_id=tid, kind="sex", reason=f"{sex} 전용 시험 (환자 성별 {profile.sex})")
    return None


def apply(pool: dict[str, dict], profile: PatientProfile) -> tuple[dict[str, dict], list[HardFiltered]]:
    """반환: (남은 시험, 뺀 시험과 사유)."""
    kept, removed = {}, []
    for tid, t in pool.items():
        r = check(t, profile)
        if r is None:
            kept[tid] = t
        else:
            removed.append(r)
    return kept, removed
