"""독립 규칙 GT (NSCLC 축소 범위 평가셋, 4단계). '규칙 GT'이며 임상 정답이 아닙니다.

독립성: 이 모듈은 ctrec의 다른 모듈을 import하지 않습니다 (엔진 Matcher·decision_state·derive_pair_status,
공용 rule_engine, 생성기 truth, 시뮬레이터 모두). 입력은 두 가지뿐입니다.
- 검토: data/nsclc/review/criteria_verification.csv (서연 데이터 검증, 임상 검수 미완료)
- 환자 값: 생성기가 만든 합성 환자의 실제 값 (public + oracle.hidden)
엔진·모델의 예측을 보고 정답을 고치지 않습니다. 생성기 라벨과의 비교는 일관성 점검용 참고 열입니다.

규칙 수준 (기준 하나): PASS / FAIL / NOT_APPLICABLE / NEEDS_EXPERT / NOT_EVALUATED
- NEEDS_EXPERT  : 임상의 판단 기준 (검수표 gt_use=needs_expert)
- NOT_EVALUATED : 범위 안이지만 슬롯이 원문을 다 표현하지 못함(gt_use=unbound), 지역 조건이라 국가를 모름
- NOT_APPLICABLE: 적용 조건(applies_if, 예: 여성만)에 해당하지 않음. 통과로 세지 않음
- excluded(동의서·절차, 팀 결정 2)는 규칙 GT를 만들지 않고 목록으로만 남김
쌍 수준: CONFIRMED_ELIGIBLE / PENDING_EXPERT_ELIGIBLE / INELIGIBLE / NOT_EVALUATED
- 경로(OR 코호트) p의 기준 = 공통 기준 + 코호트 p 전용 기준. 모든 경로가 FAIL일 때만 INELIGIBLE
- 살아 있는 경로마다: 실질 경로가 아님(코호트 시험인데 그 코호트 전용 기준이 범위 안에 없음) -> NOT_EVALUATED,
  NOT_EVALUATED 기준이 있음 -> NOT_EVALUATED, 실질 통과 기준(인구학·절차 외 PASS)이 없음 -> NOT_EVALUATED,
  NEEDS_EXPERT가 있음 -> PENDING_EXPERT_ELIGIBLE, 그 밖 -> CONFIRMED_ELIGIBLE. 가장 나은 경로를 씀
- CONFIRMED_ELIGIBLE은 그 경로에 범위 안 판단 기준이 하나도 없을 때만. 실제 6개 시험은 모두 판단 기준이 있어 0건이 정상
- 모든 라벨은 '축소 범위 안에서'의 뜻입니다. 범위 밖 기준은 평가하지 않았으므로 전체 프로토콜 적격이 아닙니다
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

GT_VERSION = "nsclc_rule_gt/1"
GT_KIND = "independent_rule_gt_reduced_scope"  # 독립 규칙 GT, 축소 범위, 임상 검수 미완료

PASS, FAIL, NOT_APPLICABLE, NEEDS_EXPERT, NOT_EVALUATED = "PASS", "FAIL", "NOT_APPLICABLE", "NEEDS_EXPERT", "NOT_EVALUATED"
RULE_STATUSES = (PASS, FAIL, NOT_APPLICABLE, NEEDS_EXPERT, NOT_EVALUATED)
CONFIRMED_ELIGIBLE, PENDING_EXPERT_ELIGIBLE, INELIGIBLE = "CONFIRMED_ELIGIBLE", "PENDING_EXPERT_ELIGIBLE", "INELIGIBLE"
PAIR_LABELS = (CONFIRMED_ELIGIBLE, PENDING_EXPERT_ELIGIBLE, INELIGIBLE, NOT_EVALUATED)
# 시뮬레이터 runner(#20·#37 sim.metrics)의 라벨 문자열. #37 이전 runner는 eligible_pending_expert를 모름
RUNNER_LABEL = {CONFIRMED_ELIGIBLE: "eligible", PENDING_EXPERT_ELIGIBLE: "eligible_pending_expert",
                INELIGIBLE: "ineligible", NOT_EVALUATED: "unlabeled"}
NON_SUBSTANTIVE = frozenset({"demographic", "consent_or_logistics"})  # 이것만 통과하면 적격 근거가 아님
_RANK = {CONFIRMED_ELIGIBLE: 0, PENDING_EXPERT_ELIGIBLE: 1, NOT_EVALUATED: 2}
_NUM_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b,
            "==": lambda a, b: a == b}


@dataclass(frozen=True)
class Criterion:
    trial_id: str
    criterion_id: str
    kind: str  # inclusion / exclusion
    category: str
    cohort: str | None
    gt_use: str  # bindable / unbound / region_conditional / needs_expert
    slot: str | None
    op: str | None
    values: tuple[str, ...]
    applies_if: tuple[str, tuple[str, ...]] | None
    expert_slot: str | None
    verification: str  # 검수표 result (일치/모호/불일치)

    @property
    def key(self) -> str:
        return f"{self.trial_id}:{self.criterion_id}"


@dataclass
class Criteria:
    """검수표에서 읽은 범위 안 기준 + excluded 목록 + 시험별 선언 코호트."""
    rules: dict[str, list[Criterion]] = field(default_factory=dict)
    excluded: dict[str, list[str]] = field(default_factory=dict)
    out_of_scope: dict[str, list[str]] = field(default_factory=dict)
    cohorts: dict[str, list[str]] = field(default_factory=dict)


def load_criteria(path: str | Path, cohorts: dict[str, list[str]] | None = None) -> Criteria:
    """cohorts: 시험 -> 선언된 OR 코호트 (파싱본 cohorts). 없으면 검수표의 기준 코호트만 씀."""
    out = Criteria(cohorts={k: list(v) for k, v in (cohorts or {}).items()})
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            t = r["trial_id"]
            for d in (out.rules, out.excluded, out.out_of_scope):
                d.setdefault(t, [])
            if r["gt_use"] == "excluded":
                out.excluded[t].append(r["criterion_id"])
                continue
            if r["in_scope"] != "yes":
                out.out_of_scope[t].append(r["criterion_id"])
                continue
            ai = None
            if r["applies_if"]:
                s, vals = r["applies_if"].split(" in ")
                ai = (s, tuple(vals.split("|")))
            out.rules[t].append(Criterion(
                trial_id=t, criterion_id=r["criterion_id"], kind=r["kind"], category=r["category"],
                cohort=r["cohort"] or None, gt_use=r["gt_use"], slot=r["slot"] or None, op=r["op"] or None,
                values=tuple(v for v in r["values"].split("|") if v), applies_if=ai, expert_slot=r["expert_slot"] or None,
                verification=r["result"]))
    return out


def _holds(c: Criterion, v) -> bool:
    if c.op in _NUM_OPS:
        if c.slot == "sex" and c.values == ("all",):
            return True  # 성별 제한 없음
        return _NUM_OPS[c.op](float(v), float(c.values[0]))
    if c.op == "in":
        return str(v) in c.values
    if c.op == "not_in":
        return str(v) not in c.values
    raise ValueError(f"{c.key}: 알 수 없는 연산자 {c.op!r}")


def rule_status(c: Criterion, values: dict) -> tuple[str, str]:
    """(상태, 근거). 근거는 사람이 검수표·환자 값으로 다시 따라갈 수 있는 문장."""
    if c.gt_use == "needs_expert":
        return NEEDS_EXPERT, f"임상의 판단 기준 (판단 슬롯 {c.expert_slot}), 값·정답을 만들지 않음"
    if c.gt_use == "unbound":
        return NOT_EVALUATED, f"검수표 {c.verification}: 슬롯이 원문을 다 표현하지 못해 연결하지 않음"
    if c.gt_use == "region_conditional":  # 국가 조건부 하한 (예: USA 22세). 하한 이상이면 지역과 무관하게 충족
        v = values.get(c.slot)
        if v is None:
            return NOT_EVALUATED, f"{c.slot} 값 없음"
        if _holds(c, v):
            return PASS, f"{c.slot}={v} {c.op} {c.values[0]}: 지역 조건과 무관하게 충족"
        return NOT_EVALUATED, f"{c.slot}={v}: 지역 조건부 기준인데 국가를 모름 (검수표 불일치, overrides.json)"
    if c.gt_use != "bindable":
        raise ValueError(f"{c.key}: 알 수 없는 gt_use {c.gt_use!r}")
    if c.applies_if:
        s, vals = c.applies_if
        if s not in values:
            return NOT_EVALUATED, f"적용 조건 슬롯 {s} 값 없음"
        if str(values[s]) not in vals:
            return NOT_APPLICABLE, f"{s}={values[s]}: 적용 대상({'|'.join(vals)}) 아님"
    if c.slot not in values:
        return NOT_EVALUATED, f"{c.slot} 값 없음"
    h = _holds(c, values[c.slot])
    basis = f"{c.slot}={values[c.slot]} {c.op} {'|'.join(c.values)} -> {'성립' if h else '불성립'}"
    if c.kind == "inclusion":
        return (PASS if h else FAIL), f"포함 기준 {basis}"
    return (FAIL if h else PASS), f"제외 기준 {basis}"


@dataclass(frozen=True)
class RuleResult:
    criterion: Criterion
    status: str
    basis: str


@dataclass(frozen=True)
class PairResult:
    label: str
    path: str | None
    reasons: tuple[str, ...]
    rules: tuple[RuleResult, ...]
    path_labels: dict

    def count(self, status: str) -> int:
        return sum(r.status == status for r in self.rules)


def trial_paths(trial_id: str, crit: Criteria) -> list[str | None]:
    declared = [c for c in crit.cohorts.get(trial_id, []) if c]
    used = [r.cohort for r in crit.rules.get(trial_id, []) if r.cohort]
    paths = list(dict.fromkeys(declared + used))
    return paths or [None]


def _path_label(p: str | None, applied: list[RuleResult], has_cohorts: bool) -> tuple[str, tuple[str, ...]]:
    if has_cohorts and not any(r.criterion.cohort == p and r.criterion.gt_use != "needs_expert" for r in applied):
        return NOT_EVALUATED, ("non_substantive_path",)  # 이 코호트 전용 기준이 범위 안에 없음
    if any(r.status == NOT_EVALUATED for r in applied):
        return NOT_EVALUATED, ("not_evaluated_rule",)
    if not any(r.status == PASS and r.criterion.category not in NON_SUBSTANTIVE for r in applied):
        return NOT_EVALUATED, ("no_substantive_pass",)  # 해당 없음·인구학 기준만 통과는 적격 근거 아님
    if any(r.status == NEEDS_EXPERT for r in applied):
        return PENDING_EXPERT_ELIGIBLE, ("needs_expert",)
    return CONFIRMED_ELIGIBLE, ()


def pair_gt(trial_id: str, values: dict, crit: Criteria) -> PairResult:
    rules = tuple(RuleResult(c, *rule_status(c, values)) for c in crit.rules.get(trial_id, []))
    paths = trial_paths(trial_id, crit)
    has_cohorts = paths != [None]
    applied = {p: [r for r in rules if r.criterion.cohort in (None, p)] for p in paths}
    dead = {p for p in paths if any(r.status == FAIL for r in applied[p])}
    labels = {}
    for p in paths:
        labels[p] = (INELIGIBLE, ("fail",)) if p in dead else _path_label(p, applied[p], has_cohorts)
    if dead == set(paths):  # 모든 경로 불통과만 부적격 확정
        fails = sorted({r.criterion.criterion_id for p in paths for r in applied[p] if r.status == FAIL})
        return PairResult(INELIGIBLE, None, tuple(f"fail:{x}" for x in fails), rules, labels)
    best = min((p for p in paths if p not in dead), key=lambda p: (_RANK[labels[p][0]], paths.index(p)))
    return PairResult(labels[best][0], best, labels[best][1], rules, labels)


def score(final_status: dict[str, str], gt_labels: dict[str, str]) -> dict:
    """시스템 최종 쌍 상태(쌍 키 -> PairStatus 값 문자열)와 독립 GT 라벨(같은 쌍 키 -> PAIR_LABELS)을 대조.

    엔진을 import하지 않도록 상태는 문자열로 받습니다 (예: RunResult.final의 p.pair.key, p.status.value).
    주 지표 expert_ready_with_gt_pending = 판단 기준 외 확인을 모두 마치고 EXPERT_REVIEW에 도달했고 GT도
    PENDING_EXPERT_ELIGIBLE (#37 correct_expert_ready와 같은 뜻, GT만 독립). 정확도가 아닌 건수입니다."""
    keys = sorted(set(gt_labels))
    missing = [k for k in keys if k not in final_status]
    if missing:
        raise ValueError(f"시스템 상태가 없는 쌍 {len(missing)}개 (예: {missing[:3]})")
    out = {"gt_kind": GT_KIND, "gt_version": GT_VERSION, "pairs": len(keys)}
    for lab in PAIR_LABELS:
        out[f"gt_{lab.lower()}"] = sum(gt_labels[k] == lab for k in keys)
    cells = {}
    for k in keys:
        cells[(final_status[k], gt_labels[k])] = cells.get((final_status[k], gt_labels[k]), 0) + 1
    get = lambda s, g: cells.get((s, g), 0)  # noqa: E731
    out["expert_ready_with_gt_pending"] = get("EXPERT_REVIEW", PENDING_EXPERT_ELIGIBLE)
    out["confirmed_eligible_with_gt_confirmed"] = get("CONFIRMED_ELIGIBLE", CONFIRMED_ELIGIBLE)
    out["eligible_resolved_with_gt"] = out["expert_ready_with_gt_pending"] + out["confirmed_eligible_with_gt_confirmed"]
    # 안전 점검: GT가 확정 적격이 아닌데 시스템이 확정 적격 / GT가 부적격이 아닌데 시스템이 확정 부적격
    out["confirmed_eligible_gt_not_confirmed"] = sum(get("CONFIRMED_ELIGIBLE", g) for g in PAIR_LABELS if g != CONFIRMED_ELIGIBLE)
    out["confirmed_ineligible_gt_not_ineligible"] = sum(get("CONFIRMED_INELIGIBLE", g) for g in PAIR_LABELS if g != INELIGIBLE)
    out["expert_review_gt_ineligible"] = get("EXPERT_REVIEW", INELIGIBLE)
    out["confirmed_ineligible_with_gt_ineligible"] = get("CONFIRMED_INELIGIBLE", INELIGIBLE)
    out["cells"] = {f"{s}|{g}": n for (s, g), n in sorted(cells.items())}
    return out


__all__ = ["GT_VERSION", "GT_KIND", "RULE_STATUSES", "PAIR_LABELS", "RUNNER_LABEL", "Criterion", "Criteria",
           "load_criteria", "rule_status", "pair_gt", "trial_paths", "score", "PASS", "FAIL", "NOT_APPLICABLE",
           "NEEDS_EXPERT", "NOT_EVALUATED", "CONFIRMED_ELIGIBLE", "PENDING_EXPERT_ELIGIBLE", "INELIGIBLE"]
