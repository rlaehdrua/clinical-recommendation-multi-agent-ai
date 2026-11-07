"""실행 지표 (설계 §4, #13 §9-4 최소 집합). PairStatus 기반이며 정답 라벨은 지표 계산에서만 씁니다.

- 시스템 확정 적격·부적격 수는 **시스템이 확정한 것**이지 증거로 확인된 적격이 아닙니다.
- 규칙 GT: 같은 오프라인 평가기에 정답 사실 전체(문서 검증된 기록)를 넣어 확정된 쌍만 라벨로 씀. 같은 규칙 엔진이 정답과 판정을
  모두 만드므로 순환적이며, 불일치는 **구현 일관성 검사**입니다. False Eligible이나 임상 정확도로 쓰지 않습니다 (#13 §0.3).
  LLM 기준이 남은 쌍은 라벨 없음(unlabeled)이며 분모에서 뺍니다.
- 판단 기준 대기 적격(eligible_pending_expert, 11-07 팀 결정 1): 정답 사실을 모두 넣어도 임상의 판단 기준만 남는 쌍(규칙 GT
  상태 EXPERT_REVIEW - 판단 기준 외 필수 기준은 모두 통과). 엔진은 이 쌍을 확정하지 않고 담당의 검토로 보내므로, 시스템이
  EXPERT_REVIEW에 도달하면 "판단 기준 외 확인 완료"로 셉니다. 확정 적격(eligible)과 섞지 않고 따로 보고합니다.
- 시간 두 축(능동·경과)을 따로 보고하고 더하지 않습니다 (설계 §3.2.1). 가정 비용이면 분 단위는 정책 간 상대 비교 전용.
"""

from __future__ import annotations

from collections import Counter

from ..engine.cohort_state import CohortState
from ..engine.contracts import REPHRASE, PairStatus
from ..engine.fairness import gini

ELIGIBLE, INELIGIBLE, UNLABELED = "eligible", "ineligible", "unlabeled"
PENDING_EXPERT = "eligible_pending_expert"
GT_KIND = "rule_gt_circular"


def r6(x: float) -> float:
    return round(float(x), 6)


def label_of(status: PairStatus) -> str:
    return {PairStatus.CONFIRMED_ELIGIBLE: ELIGIBLE, PairStatus.CONFIRMED_INELIGIBLE: INELIGIBLE,
            PairStatus.EXPERT_REVIEW: PENDING_EXPERT}.get(status, UNLABELED)


def correct_resolved(status: PairStatus, label: str) -> tuple[bool, bool]:
    """(정답 확정 적격, 정답 판단 기준 외 확인 완료). 주 지표 = 둘의 합 (따로도 보고)."""
    return (status == PairStatus.CONFIRMED_ELIGIBLE and label == ELIGIBLE,
            status == PairStatus.EXPERT_REVIEW and label == PENDING_EXPERT)


def status_counts(co: CohortState) -> dict[str, int]:
    c = Counter(p.status.value for p in co.pairs())
    return {s.value: c.get(s.value, 0) for s in PairStatus}


def gt_comparison(final: CohortState, labels: dict[str, str]) -> dict:
    """labels: "<별칭>:<시험>" -> eligible / ineligible / unlabeled."""
    labeled = {k: v for k, v in labels.items() if v != UNLABELED}
    out = Counter()
    for p in final.pairs():
        lab = labels.get(p.pair.key, UNLABELED)
        if lab == UNLABELED:
            continue
        if p.status == PairStatus.CONFIRMED_ELIGIBLE:
            if lab == PENDING_EXPERT:  # 판단 기준을 판단 없이 확정 - 엔진 안전 경계 위반
                out["confirmed_eligible_gt_pending_expert"] += 1
            else:
                out["confirmed_eligible_gt_eligible" if lab == ELIGIBLE else "confirmed_eligible_gt_ineligible"] += 1
        elif p.status == PairStatus.EXPERT_REVIEW and lab != UNLABELED:
            out[f"expert_review_gt_{lab}"] += 1
        elif p.status == PairStatus.CONFIRMED_INELIGIBLE:
            out["confirmed_ineligible_gt_ineligible" if lab == INELIGIBLE else "confirmed_ineligible_gt_eligible"] += 1
        else:
            out[f"unconfirmed_gt_{lab}"] += 1
    keys = ("confirmed_eligible_gt_eligible", "confirmed_eligible_gt_ineligible", "confirmed_ineligible_gt_ineligible",
            "confirmed_ineligible_gt_eligible", "unconfirmed_gt_eligible", "unconfirmed_gt_ineligible",
            "confirmed_eligible_gt_pending_expert", "expert_review_gt_eligible_pending_expert",
            "expert_review_gt_ineligible", "expert_review_gt_eligible", "unconfirmed_gt_eligible_pending_expert")
    return {"kind": GT_KIND, "labeled_pairs": len(labeled), "unlabeled_pairs": len(labels) - len(labeled),
            "gt_eligible": sum(v == ELIGIBLE for v in labeled.values()),
            "gt_ineligible": sum(v == INELIGIBLE for v in labeled.values()),
            "gt_eligible_pending_expert": sum(v == PENDING_EXPERT for v in labeled.values()),
            **{k: out.get(k, 0) for k in keys},
            # 일관성 검사: 확정이 정답과 반대 + 판단 기준을 판단 없이 확정 + 정답 부적격인데 판단 기준만 남았다고 봄
            "rule_gt_disagreements": out.get("confirmed_eligible_gt_ineligible", 0)
            + out.get("confirmed_ineligible_gt_eligible", 0) + out.get("confirmed_eligible_gt_pending_expert", 0)
            + out.get("expert_review_gt_ineligible", 0)}


def summarize(*, initial: CohortState, final: CohortState, labels: dict[str, str], steps: list[dict],
              per_patient_minutes: dict[str, float], total_cost, elapsed_minutes: float, rounds: int,
              progress: list[int], termination: str, repeat_suppressed: int, safety_unfunded: int,
              audit: dict) -> dict:
    """steps: 실행기 단계 요약 [{action_type, execution, note, patient_alias, error_code}]."""
    n = len(final.pairs())
    fin = status_counts(final)
    confirmed = fin[PairStatus.CONFIRMED_ELIGIBLE.value] + fin[PairStatus.CONFIRMED_INELIGIBLE.value]
    executed = [s for s in steps if s["execution"] != "not_executed"]
    asks = Counter(s["patient_alias"] for s in executed if s["action_type"] == "ASK_PATIENT")
    aliases = final.aliases()
    return {
        "pairs": n,
        "status_initial": status_counts(initial),
        "status_final": fin,
        "system_confirmed_eligible": fin[PairStatus.CONFIRMED_ELIGIBLE.value],
        "system_confirmed_ineligible": fin[PairStatus.CONFIRMED_INELIGIBLE.value],
        "system_confirmed_share": r6(confirmed / n) if n else 0.0,
        "rule_gt": gt_comparison(final, labels),
        "actions_executed": dict(sorted(Counter(s["action_type"] for s in executed).items())),
        "executions": dict(sorted(Counter(s["execution"] for s in steps).items())),
        "not_executed_reasons": dict(sorted(Counter(s["note"] or "none" for s in steps
                                                    if s["execution"] == "not_executed").items())),
        "failure_codes": dict(sorted(Counter(s["error_code"] for s in steps if s["error_code"]).items())),
        "repeat_suppressed": repeat_suppressed,
        "safety_unfunded": safety_unfunded,
        "active_minutes": {"coordinator": r6(total_cost.coordinator_minutes),
                           "clinician": r6(total_cost.clinician_minutes)},
        "llm_calls": total_cost.llm_calls,
        "elapsed_minutes": r6(elapsed_minutes),
        "patient_burden": {"questions": sum(asks.values()),
                           "max_questions_per_patient": max(asks.values(), default=0),
                           "rephrased": sum(s.get("reason_code") == REPHRASE and s["execution"] != "not_executed"
                                            for s in steps),
                           "no_answer": sum(s["error_code"] == "no_answer" for s in steps)},
        "allocation_gini": r6(gini([per_patient_minutes.get(a, 0.0) for a in aliases]) or 0.0),
        "rounds": rounds,
        "confirmed_by_round": progress,
        "termination": termination,
        "audit": audit,
    }


__all__ = ["summarize", "status_counts", "gt_comparison", "label_of", "correct_resolved", "ELIGIBLE", "INELIGIBLE",
           "UNLABELED", "PENDING_EXPERT", "GT_KIND"]
