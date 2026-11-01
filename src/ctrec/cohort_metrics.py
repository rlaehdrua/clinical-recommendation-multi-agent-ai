"""코호트 지표 (WP1 / M3): 누락 예측을 분모에 넣는 적격성 지표. metrics.py는 바꾸지 않습니다(하위 호환).

감사 P1-6 (docs/audit/01 §6-10): metrics.eligibility_metrics는 (환자, 시험) 내부 조인이라 예측이 없는 쌍이
분모에서 빠지고 감점되지 않습니다. 실행 실패로 결과가 빠진 환자가 많을수록 정확도가 좋아 보일 수 있습니다.

여기서는
- 정답에 있는 모든 쌍을 분모로 하고, 예측이 없는 쌍은 NOT_EVALUATED(판정 못 함 = 오답, 부적격 아님)로 채웁니다.
- 정답에 없는 예측(평가 대상 밖)은 세지 않고 개수만 보고합니다.
- 기존 지표 이름은 그대로 두고 cohort_ 접두사 결과를 따로 돌려줍니다. 기존 baseline_gate와 의미가 섞이지 않게
  cohort_baseline_gate를 따로 둡니다.
- confirmed_correct_pairs(M3 목적 함수 후보): 정답 ELIGIBLE이고 시스템이 ELIGIBLE로 **확정**한 쌍 수. 확정 여부를
  따로 주면(confirmed) 그 쌍만 셉니다. 오확정(정답이 ELIGIBLE이 아닌데 ELIGIBLE 확정)은 별도로 셉니다.
"""

from __future__ import annotations

from .metrics import BASELINE_TOLERANCE, eligibility_metrics

MISSING_FILL = "NOT_EVALUATED"


def fill_missing(gold: dict, pred: dict) -> tuple[dict, list, list]:
    """(정답 쌍 전체에 대한 예측, 누락 쌍, 정답 밖 예측 쌍)."""
    missing = sorted(k for k in gold if k not in pred)
    extra = sorted(k for k in pred if k not in gold)
    full = {k: pred.get(k, MISSING_FILL) for k in gold}
    return full, missing, extra


def cohort_eligibility_metrics(gold: dict, pred: dict, text_only: dict | None = None,
                               confirmed: set | None = None) -> dict:
    full, missing, extra = fill_missing(gold, pred)
    m = eligibility_metrics(gold, full, text_only)
    m["missing_predictions"] = len(missing)  # 기존 함수는 채운 뒤라 0이 되므로 다시 기록
    m["missing_rate"] = len(missing) / len(gold) if gold else None
    m["extra_predictions"] = len(extra)
    m["patients_with_missing"] = len({p for p, _ in missing})
    ok = [k for k in gold if gold[k] == "ELIGIBLE" and full[k] == "ELIGIBLE" and (confirmed is None or k in confirmed)]
    wrong = [k for k in gold if gold[k] != "ELIGIBLE" and full[k] == "ELIGIBLE" and (confirmed is None or k in confirmed)]
    m["confirmed_correct_pairs"] = len(ok)
    m["confirmed_wrong_pairs"] = len(wrong)
    m["gold_eligible_pairs"] = sum(v == "ELIGIBLE" for v in gold.values())
    m["denominator"] = "all_gold_pairs"
    return m


def cohort_baseline_gate(current: dict, baseline: dict, tolerance: float = BASELINE_TOLERANCE) -> tuple[bool, str]:
    """누락을 감점한 정확도로 비교. 정답 쌍 수가 다르면 비교하지 않음(같은 평가셋이 아님)."""
    if current.get("denominator") != "all_gold_pairs" or baseline.get("denominator") != "all_gold_pairs":
        return False, "cohort_eligibility_metrics 결과끼리만 비교할 수 있음"
    if current.get("pairs") != baseline.get("pairs"):
        return False, f"정답 쌍 수가 다름 ({current.get('pairs')} vs {baseline.get('pairs')})"
    cur, base = current.get("accuracy"), baseline.get("accuracy")
    if cur is None or base is None:
        return False, "정확도가 없어 기준선과 비교할 수 없음"
    diff = cur - base
    ok = diff >= -tolerance - 1e-9 and current.get("confirmed_wrong_pairs", 0) <= baseline.get("confirmed_wrong_pairs", 0)
    return ok, (f"정확도(누락 포함) {cur:.3f} (기준선 {base:.3f}, 차이 {diff * 100:+.1f}%p), 오확정 "
                f"{current.get('confirmed_wrong_pairs')} (기준선 {baseline.get('confirmed_wrong_pairs')}) "
                f"-> {'통과' if ok else '불합격'}")
