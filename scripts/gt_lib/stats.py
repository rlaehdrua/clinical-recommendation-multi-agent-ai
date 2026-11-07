"""비율 구간·짝지은 검정·일치도. 외부 의존 없음 (scipy 없음)."""

from __future__ import annotations

import math
from collections import Counter

Z95 = 1.959963984540054
NOT_ESTIMABLE = "추정 불가"


def wilson(x: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = x / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _log_binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k), X ~ Bin(n, p)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return min(1.0, sum(math.exp(_log_binom_pmf(i, n, p)) for i in range(k + 1)))


def _bisect(f, lo: float, hi: float, iters: int = 100) -> float:
    """f(lo) > 0 >= f(hi)를 가정한 단조 감소 함수의 근."""
    for _ in range(iters):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(x: int, n: int, alpha: float = 0.05) -> tuple[float, float] | None:
    """정확 이항(Clopper-Pearson) 양측 구간."""
    if n <= 0:
        return None
    a = alpha / 2
    lo = 0.0 if x == 0 else _bisect(lambda p: a - (1 - binom_cdf(x - 1, n, p)), 0.0, 1.0)
    hi = 1.0 if x == n else _bisect(lambda p: binom_cdf(x, n, p) - a, 0.0, 1.0)
    return lo, hi


def rate(x: int, n: int) -> dict:
    """분자·분모를 그대로 남긴 비율. 분모 0이면 '추정 불가'."""
    if x < 0 or n < 0 or x > n:
        raise ValueError(f"잘못된 분자/분모 {x}/{n}")
    if n == 0:
        return {"numerator": x, "denominator": 0, "rate": None, "wilson95": None, "exact95": None,
                "status": NOT_ESTIMABLE}
    w, e = wilson(x, n), clopper_pearson(x, n)
    return {"numerator": x, "denominator": n, "rate": x / n,
            "wilson95": [round(w[0], 4), round(w[1], 4)], "exact95": [round(e[0], 4), round(e[1], 4)],
            "status": "ok"}


def mcnemar_exact(fixed: int, broken: int) -> dict:
    """짝지은 전후 비교. fixed = 전 실패·후 성공, broken = 전 성공·후 실패. 양측 정확 이항 p."""
    m = fixed + broken
    if m == 0:
        return {"fixed": 0, "broken": 0, "discordant": 0, "p_two_sided": None, "status": NOT_ESTIMABLE}
    k = min(fixed, broken)
    p = min(1.0, 2 * binom_cdf(k, m, 0.5))
    return {"fixed": fixed, "broken": broken, "discordant": m, "p_two_sided": p, "status": "ok"}


def cohen_kappa(a: list, b: list) -> dict:
    """두 라벨러의 Cohen's kappa. 기대 일치 = 1이면 (한 범주만 쓰임) '추정 불가'."""
    if len(a) != len(b):
        raise ValueError("라벨 수가 다릅니다")
    n = len(a)
    if n == 0:
        return {"n": 0, "observed": None, "expected": None, "kappa": None, "status": NOT_ESTIMABLE}
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if math.isclose(pe, 1.0):
        return {"n": n, "observed": po, "expected": pe, "kappa": None, "status": NOT_ESTIMABLE}
    return {"n": n, "observed": po, "expected": pe, "kappa": (po - pe) / (1 - pe), "status": "ok"}
