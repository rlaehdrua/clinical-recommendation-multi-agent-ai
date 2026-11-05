"""연구용 코호트 현황판(/cohort)의 쌍 상태 - 엔진 계약 0.2의 7가지 PairStatus로 계산 (읽기 전용).

화면이 판정 규칙을 따로 갖지 않도록, 합성 코호트 JSON(cohort/spec.Cohort)의 **public 부분만** 읽어
engine.contracts.derive_pair_status / pending_kinds에 넘깁니다 (시뮬레이터·웹 엔진과 같은 상태 정의, M5·M14).
oracle(숨긴 값·정답·쌍 라벨)은 쓰지 않습니다 - 상태는 '정책이 지금 아는 정보'로 본 것입니다.

기준 하나의 남은 작업(gap)
- 성립 여부를 아는 기준: none (공개 값은 생성 시 기록에서 나온 값으로 봄 -> 불충족이면 문서 검증된 불충족)
- 모르는 기준
  - 임상의 판단형(슬롯 judge.* 또는 담당의 문의 정보원 전용): physician_review -> EXPERT_REVIEW
  - 값은 공개돼 있지만 검사가 유효 기간보다 오래됨(time_window_days < lab_age_days): stale
  - 그 밖: missing
쌍의 판정 값: 아는 기준 하나라도 불통과면 INELIGIBLE, 모두 알고 모두 통과면 ELIGIBLE, 그 밖에는 UNCERTAIN.
연결된 기준이 하나도 없는 시험은 확정 근거가 없으므로 NOT_EVALUATED (엔진과 같은 규칙, required_criteria=0).
정적 생성 데이터에는 확보 실패(BLOCKED_FAILURE)·평가 실패(NOT_EVALUATED)가 없지만, 같은 7가지 상태 체계로 보여 줍니다.
"""

from __future__ import annotations

from collections import Counter

from ..engine.contracts import CONTRACT_VERSION, PairStatus, derive_pair_status, pending_kinds

LAB_AGE = "lab_age_days"
EXPERT_SLOT_PREFIX = "judge."
STATUS_ORDER = [s.value for s in PairStatus]


def _passes(kind: str, holds: str) -> bool | None:
    if holds == "unknown":
        return None
    return holds == "yes" if kind == "inclusion" else holds == "no"


def rule_gap(rule: dict, holds: str, known: dict, expert_sources: frozenset[str]) -> str:
    """기준 하나의 gap (contracts.GapKind 값)."""
    if holds != "unknown":
        return "none"
    slots = [rule["slot"], *rule.get("extra_slots", [])]
    if any(s.startswith(EXPERT_SLOT_PREFIX) or s in expert_sources for s in slots):
        return "physician_review"
    window = rule.get("time_window_days")
    age = known.get(LAB_AGE)
    if window is not None and age is not None and float(age) > window and all(s in known for s in slots):
        return "stale"
    return "missing"


def _expert_slots(public: dict) -> frozenset[str]:
    """담당의·임상의 판단 정보원으로만 드러나는 슬롯 (생성기 정보원 ID든 엔진 I-5 ID든)."""
    expert = {"S_md", "clinician_review"}
    by_slot: dict[str, set[str]] = {}
    for src in public.get("sources", {}).values():
        base = src.get("base_source") or src.get("source_id")
        for s in src.get("reveals", []):
            by_slot.setdefault(s, set()).add(base)
    return frozenset(s for s, bases in by_slot.items() if bases and bases <= expert)


def pair_board(public: dict, patient: dict, trial_id: str, expert_slots: frozenset[str]) -> dict:
    pid = patient["patient_id"]
    known = patient.get("known", {})
    rules = []
    fail = unknown = False
    for r in public["rules"][trial_id]:
        holds = public["initial_holds"].get(f"{pid}|{trial_id}|{r['rule_id']}", "unknown")
        ps = _passes(r["kind"], holds)
        gap = rule_gap(r, holds, known, expert_slots)
        fail |= ps is False
        unknown |= ps is None
        rules.append({"rule_id": r["rule_id"], "gap": gap})
    eligibility = "INELIGIBLE" if fail else "UNCERTAIN" if unknown else "ELIGIBLE"
    gaps = {x["gap"] for x in rules} - {"none"}
    mismatch = "confirmed" if fail else None  # 공개 값은 기록 출처 -> 불충족은 문서 검증된 불충족
    n = len(rules)  # 이 시험에서 판정에 쓰는 (슬롯에 연결된) 기준 수. 0개면 적격 확정 근거가 없음 -> NOT_EVALUATED
    status = derive_pair_status(eligibility, gaps, mismatch, required_criteria=n)
    return {"status": status.value, "eligibility": eligibility,
            "pending_kinds": sorted(pending_kinds(eligibility, gaps, mismatch, required_criteria=n)), "rules": rules}


def board(cohort: dict) -> dict:
    """{"contract_version", "pairs": {"환자|시험": {...}}, "status_counts": {상태: 수}} - public만 사용."""
    public = cohort["public"]
    expert = _expert_slots(public)
    pairs = {f"{p['patient_id']}|{t}": pair_board(public, p, t, expert)
             for p in public["patients"] for t in p["candidate_trials"]}
    counts = Counter(v["status"] for v in pairs.values())
    return {"contract_version": CONTRACT_VERSION, "status_order": STATUS_ORDER, "pairs": pairs,
            "status_counts": {s: counts.get(s, 0) for s in STATUS_ORDER}}


__all__ = ["board", "pair_board", "rule_gap", "STATUS_ORDER"]
