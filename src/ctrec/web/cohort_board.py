"""연구용 코호트 현황판(/cohort)의 쌍 상태 - 엔진 계약 0.2의 7가지 PairStatus로 계산 (읽기 전용).

화면이 판정 규칙을 따로 갖지 않도록, 합성 코호트 JSON(cohort/spec.Cohort)의 **public 부분만** 읽어
engine.contracts.derive_pair_status / pending_kinds에 넘깁니다 (시뮬레이터·웹 엔진과 같은 상태 정의, M5·M14).
oracle(숨긴 값·정답·쌍 라벨)은 쓰지 않습니다 - 상태는 '정책이 지금 아는 정보'로 본 것입니다.

기준 하나의 남은 작업(gap)
- 임상의 판단형(public.expert_rules, 슬롯 judge.*, 담당의 문의 정보원 전용): 값이 있어도 쓰지 않음(자동 통과·탈락 금지)
  physician_review -> EXPERT_REVIEW
- 성립 여부를 아는 기준: none (공개 값은 생성 시 기록에서 나온 값으로 봄 -> 불충족이면 문서 검증된 불충족)
- 모르는 기준
  - 값은 공개돼 있지만 검사가 유효 기간보다 오래됨(time_window_days < lab_age_days): stale
  - 그 밖: missing

쌍의 판정 값: OR 경로(코호트)마다 공통 기준 + 그 코호트 기준을 보고, 가장 나은 경로를 씁니다 (Matcher._select_cohort와 같은 뜻).
- 모든 경로에 불통과가 있으면 INELIGIBLE, 불통과·미확인이 없는 경로가 있으면 ELIGIBLE, 그 밖에는 UNCERTAIN.
- 쌍의 gap은 적용된 경로의 gap만 씁니다 (계약: 다른 갈래의 gap은 넘기지 않음).
- 경로 정보는 기준의 cohort와 public.paths(기준이 모두 미연결인 경로까지 포함)에서 읽습니다. 없으면 코호트 구분이 없는 시험입니다.

연결된 기준만으로 ELIGIBLE이어도 전체 적격 근거는 아닙니다 (이슈 #23).
- 미연결(검수 전) 기준 public.unbound_rules가 남았거나 그 목록을 알 수 없으면 NOT_EVALUATED (충족으로 간주하지 않음).
  미연결 기준은 어느 경로 소속인지 public에 없으므로 경로와 무관하게 막습니다.
- INELIGIBLE은 그대로 둡니다: 모든 경로가 막혔으면 남은 기준이 무엇이든 적격이 될 수 없음.
- UNCERTAIN도 그대로 둡니다: 근거 부족·판단 대기는 남은 작업이 있는 상태이며 미평가와 구분합니다.
- 연결된 기준이 하나도 없는 시험도 확정 근거가 없으므로 NOT_EVALUATED (엔진과 같은 규칙, required_criteria=0).
bound_eligibility에 연결된 기준만으로 본 결과를 남깁니다 (확정 근거가 아니라 설명용).
정적 생성 데이터에는 확보 실패(BLOCKED_FAILURE)가 없지만, 같은 7가지 상태 체계로 보여 줍니다.
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


def rule_gap(rule: dict, holds: str, known: dict, expert_sources: frozenset[str],
             expert_rules: frozenset[str] = frozenset()) -> str:
    """기준 하나의 gap (contracts.GapKind 값). expert_rules = 생성기가 내보낸 판단 기준 ID (spec.is_expert_slot 정의)."""
    slots = [rule["slot"], *rule.get("extra_slots", [])]
    if rule["rule_id"] in expert_rules or any(s.startswith(EXPERT_SLOT_PREFIX) or s in expert_sources for s in slots):
        return "physician_review"  # 값이 있어도 판단 대기 (이전 형식 데이터의 judge.* 값으로 자동 통과하지 않음)
    if holds != "unknown":
        return "none"
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


def _trial_paths(rules: list[dict], declared: list[str | None]) -> list[str]:
    """OR 경로(코호트): 생성기가 선언한 경로 + 기준에 붙은 코호트. 빈 목록 = 코호트 구분 없음."""
    labels = [p for p in declared if p]
    labels += [c for c in dict.fromkeys(r.get("cohort") for r in rules) if c and c not in labels]
    return labels


def _applied_path(rows: list[dict], paths: list[str]) -> tuple[str | None, list[dict]]:
    """가장 나은 경로와 그 경로에 적용되는 기준. 순서: 불통과 없음 -> 미확인이 적음 -> 선언 순서 (Matcher._select_cohort)."""
    if not paths:
        return None, rows
    applied = {p: [x for x in rows if x["cohort"] in (None, p)] for p in paths}
    best = min(paths, key=lambda p: (any(x["passes"] is False for x in applied[p]),
                                     sum(x["passes"] is None for x in applied[p]), paths.index(p)))
    return best, applied[best]


def pair_board(public: dict, patient: dict, trial_id: str, expert_slots: frozenset[str]) -> dict:
    pid = patient["patient_id"]
    known = patient.get("known", {})
    expert_rules = frozenset(public.get("expert_rules", {}).get(trial_id, ()))
    rows = []
    for r in public["rules"][trial_id]:
        holds = public["initial_holds"].get(f"{pid}|{trial_id}|{r['rule_id']}", "unknown")
        gap = rule_gap(r, holds, known, expert_slots, expert_rules)
        rows.append({"rule_id": r["rule_id"], "gap": gap, "cohort": r.get("cohort"),
                     "passes": None if gap == "physician_review" else _passes(r["kind"], holds)})
    path, applied = _applied_path(rows, _trial_paths(public["rules"][trial_id], public.get("paths", {}).get(trial_id, [])))
    fail = any(x["passes"] is False for x in applied)
    bound = "INELIGIBLE" if fail else "UNCERTAIN" if any(x["passes"] is None for x in applied) else "ELIGIBLE"
    unbound = public.get("unbound_rules", {}).get(trial_id)  # None = 목록을 모름: 전체 기준을 봤다고 할 수 없음
    eligibility = "NOT_EVALUATED" if bound == "ELIGIBLE" and (unbound is None or unbound) else bound
    gaps = {x["gap"] for x in applied} - {"none"}
    mismatch = "confirmed" if fail else None  # 공개 값은 기록 출처 -> 불충족은 문서 검증된 불충족
    n = len(rows)  # 이 시험에서 판정에 쓰는 (슬롯에 연결된) 기준 수. 0개면 적격 확정 근거가 없음 -> NOT_EVALUATED
    status = derive_pair_status(eligibility, gaps, mismatch, required_criteria=n)
    in_path = {x["rule_id"] for x in applied}
    return {"status": status.value, "eligibility": eligibility, "bound_eligibility": bound, "path": path,
            "unbound_rules": None if unbound is None else len(unbound),
            "pending_kinds": sorted(pending_kinds(eligibility, gaps, mismatch, required_criteria=n)),
            "rules": [{"rule_id": x["rule_id"], "gap": x["gap"], "applied": x["rule_id"] in in_path} for x in rows]}


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
