"""환자 단위 부트스트랩 신뢰구간 (설계 §4 "반복"). 비교표 행(runner.compare)의 환자별 결과(per_patient)를 씁니다.

- 단위 = 환자 (코호트 ID, 별칭). 같은 환자의 seed별 결과는 평균해 한 값으로 둠 (seed = 응답 운 반복).
  생성기 replicate가 여러 개면 코호트가 달라 환자 단위가 늘어납니다.
- 정책 간 차이는 **짝지은 부트스트랩**: 같은 재표본(같은 환자 집합)으로 기준 정책과의 차이를 계산. 모든 정책이 같은 환자·같은
  seed로 돌았는지 확인하고, 다르면 거부합니다 (짝이 맞지 않는 비교 금지).
- 추정치 = 코호트당 합계 (환자별 값의 합 / 코호트 수), 구간 = 백분위 구간. 난수는 seed로 고정 (같은 입력 -> 같은 구간).
- 한계: 예산은 코호트 전체가 나눠 쓰므로 환자 결과가 서로 독립이 아닙니다. 환자 재표본은 이 결합을 무시하므로 구간이 실제보다
  좁을 수 있습니다. 합성 코호트·순환적 규칙 GT·가정 비용 위의 **정책 간 상대 비교**에만 씁니다.
"""

from __future__ import annotations

import random
from collections import defaultdict

DEFAULT_METRICS = ("correct_eligible_resolved", "correct_confirmed_eligible", "correct_expert_ready",
                   "confirmed_eligible", "confirmed_ineligible", "coordinator_minutes",
                   "questions")
SETTING = ("rephrase", "budget_minutes")


class BootstrapError(ValueError):
    pass


def _r(x: float) -> float:
    return round(float(x), 4)


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def patient_values(rows: list[dict], metric: str) -> tuple[dict[str, dict[tuple, float]], list[int]]:
    """한 설정의 행 -> ({정책: {(코호트, 별칭): seed 평균}}, seed 목록). 정책마다 같은 환자·seed인지 검사."""
    acc: dict[str, dict[tuple, list[float]]] = defaultdict(lambda: defaultdict(list))
    seeds: dict[str, set] = defaultdict(set)
    for r in rows:
        if "per_patient" not in r:
            raise BootstrapError("행에 per_patient가 없음 (runner.compare 결과를 넘겨야 함)")
        seeds[r["policy_id"]].add((r["cohort_id"], r["seed"]))
        for alias, v in r["per_patient"].items():
            acc[r["policy_id"]][(r["cohort_id"], alias)].append(float(v[metric]))
    if len({frozenset(s) for s in seeds.values()}) > 1:
        raise BootstrapError("정책마다 (코호트, seed) 조합이 다름 - 짝지은 비교 불가")
    if len({frozenset(u) for u in acc.values()}) > 1:
        raise BootstrapError("정책마다 환자 집합이 다름 - 짝지은 비교 불가")
    vals = {pid: {u: sum(xs) / len(xs) for u, xs in units.items()} for pid, units in acc.items()}
    return vals, sorted({s for _, s in next(iter(seeds.values()))}) if seeds else []


def bootstrap_ci(rows: list[dict], metrics: tuple[str, ...] = DEFAULT_METRICS, reference: str = "legacy_greedy",
                 n_boot: int = 2000, level: float = 0.95, seed: int = 0) -> list[dict]:
    """설정(바꿔 묻기, 예산)·정책·지표마다 추정치·구간과 기준 정책 대비 차이·구간.

    reference가 그 설정에 없으면 차이 칸은 None."""
    if n_boot < 1 or not 0 < level < 1:
        raise BootstrapError("n_boot >= 1, 0 < level < 1")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in SETTING)].append(r)
    a = (1 - level) / 2
    out = []
    for setting in sorted(groups):
        for metric in metrics:
            vals, seeds = patient_values(groups[setting], metric)
            units = sorted(next(iter(vals.values())))
            n_cohorts = len({c for c, _ in units})
            rng = random.Random(f"{seed}|{setting}|{metric}")
            draws = [[rng.randrange(len(units)) for _ in units] for _ in range(n_boot)]

            def total(pid: str, idx) -> float:
                v = vals[pid]
                return sum(v[units[i]] for i in idx) / n_cohorts

            full = range(len(units))
            for pid in sorted(vals):
                boot = [total(pid, d) for d in draws]
                row = {"rephrase": setting[0], "budget_minutes": setting[1], "policy_id": pid, "metric": metric,
                       "estimate": _r(total(pid, full)), "ci_low": _r(_quantile(boot, a)),
                       "ci_high": _r(_quantile(boot, 1 - a)), "reference": reference, "diff": None,
                       "diff_low": None, "diff_high": None, "n_patients": len(units), "n_cohorts": n_cohorts,
                       "n_seeds": len(seeds), "n_boot": n_boot, "level": level}
                if reference in vals:
                    diffs = [total(pid, d) - total(reference, d) for d in draws]
                    row.update(diff=_r(total(pid, full) - total(reference, full)), diff_low=_r(_quantile(diffs, a)),
                               diff_high=_r(_quantile(diffs, 1 - a)))
                out.append(row)
    return out


__all__ = ["bootstrap_ci", "patient_values", "BootstrapError", "DEFAULT_METRICS"]
