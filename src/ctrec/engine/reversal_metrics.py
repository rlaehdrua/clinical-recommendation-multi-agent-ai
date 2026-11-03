"""확정 판정 역전 지표 (WP7 분석, 계약 0.2 PROVISIONAL - 계약은 바꾸지 않음).

WP7 감사 기록(DecisionRecord v2)에서 (run_id, 환자 별칭, 시험) 쌍별로 시간순(step) 쌍 상태 이력을 만들고, **확정 판정이
반대 확정으로 바뀐 사건**을 셉니다. 이 수치는 판정 상태 변경 건수이며 **'기존 판정이 오판이었다'는 뜻이 아닙니다**.
어느 쪽 확정이 맞았는지는 이 기록만으로 알 수 없고, 오판 여부는 정답 대조(평가 계층)로만 정할 수 있습니다.

정의
- 확정 상태 = CONFIRMED_ELIGIBLE / CONFIRMED_INELIGIBLE 만. 나머지는 모두 중간 상태:
    검증 중간 상태    VERIFICATION_PENDING, EXPERT_REVIEW
    정보 재확보 상태  INFO_MISSING
    실패·미평가 상태  BLOCKED_FAILURE, NOT_EVALUATED
- 기준 확정(anchor) = 그 쌍에서 마지막으로 관찰된 확정 상태. 쌍 전이의 before·after 모두 관찰값으로 씀.
- 확정 역전 사건 1건 = 이력에서 anchor와 **다른** 확정 상태에 도달한 순간. 그 사이 거친 중간 상태를 via로 남김.
  같은 확정으로 돌아오면(적격 -> 대기 -> 적격) 사건 아님. 같은 상태 반복 기록은 사건을 만들지 않음.
- 경유 유형(path_kind, 사건마다 하나. 우선순위 순):
    failure_or_not_evaluated : via에 BLOCKED_FAILURE 또는 NOT_EVALUATED가 있음 (확보 실패·평가 실패를 거침.
                               **정상 검증 경유가 아님**)
    via_info_missing         : 실패·미평가는 없고 INFO_MISSING을 거침 (정보 재확보 경유)
    verification_only        : VERIFICATION_PENDING / EXPERT_REVIEW만 거침 (검증·검토 경유)
  방향별 합계(confirmed_*)는 경유 유형과 무관한 '확정 상태 변경' 수이고, 유형별 내역은 reversals_by_path에 있음.
- 확정 역전 대기(downgraded_pending) = 이력 끝에서 anchor는 확정인데 현재 상태가 중간 상태인 쌍 (쌍 단위 1건).
- 직접 역전 계약 위반 = 진단 파일의 차단 진단 `direct_confirmed_reversal` (정상 기록이 없음). 정상 역전 지표와 따로 셈.
  진단 파일에만 남은 전이(withheld_* 포함)는 이력에 넣지 않음.

근거 불충분 처리 (임의 판정하지 않음)
- 이력 끊김: 어떤 전이의 before가 같은 쌍의 직전 after와 다르면 그 사이 상태 변화가 기록에 없음. 이 끊김을 건너 반대
  확정에 도달하면 확인된 역전으로 세지 않고 unverified_reversals로 보고 (경유 유형 내역에도 넣지 않음).
  미확인 사유(unverified_reason): conflicting_records (끊긴 구간에 내용이 충돌해 버린 단계가 있음) /
  missing_record (그 외 기록 누락).
  단, 끊긴 구간 안에 그 쌍의 direct_confirmed_reversal 차단 진단이 있고 그 before/after가 끊김을 정확히 설명하면
  계약 위반 쪽에서 이미 셌으므로 미확인으로도 세지 않음 (anchor는 관찰된 현재 확정으로 옮김).
- 끊김 사이에서 같은 확정으로 돌아온 경우(적격 -> [기록 없음] -> 적격)는 그 사이 숨은 역전을 알 수 없으므로 세지 않고
  history_gaps로만 드러남.
- 중복 기록: 같은 (run_id, step) 기록이 내용까지 같으면 한 번만 씀. 내용이 다르면 둘 다 쓰지 않고 conflicting_steps로
  보고 (그 단계의 변화는 이력 끊김으로 드러남).
- 실행 범위: run_id(`RUN-<별칭>`)는 같은 사례를 다시 실행하면 같으므로 실행 폴더 사이에서 유일하지 않음.
  compute_for_runs는 폴더마다 따로 계산해 합칩니다(폴더가 다른 같은 run_id의 이력을 잇지 않음).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .contracts import CONFIRMED, DecisionRecord, PairStatus

E, I = PairStatus.CONFIRMED_ELIGIBLE, PairStatus.CONFIRMED_INELIGIBLE
DIRECT_REVERSAL_CODE = "direct_confirmed_reversal"
METRICS_VERSION = "reversal_metrics/0.2"
VERIFICATION_STATES = frozenset({PairStatus.VERIFICATION_PENDING.value, PairStatus.EXPERT_REVIEW.value})
FAILURE_STATES = frozenset({PairStatus.BLOCKED_FAILURE.value, PairStatus.NOT_EVALUATED.value})
PATH_KINDS = ("verification_only", "via_info_missing", "failure_or_not_evaluated")
UNVERIFIED_REASONS = ("missing_record", "conflicting_records")
DIRECTIONS = ("eligible_to_ineligible", "ineligible_to_eligible")


def path_kind(via: Iterable[str]) -> str:
    v = set(via)
    if v & FAILURE_STATES:
        return "failure_or_not_evaluated"
    if PairStatus.INFO_MISSING.value in v:
        return "via_info_missing"
    return "verification_only"


@dataclass(frozen=True)
class ReversalEvent:
    run_id: str
    patient_alias: str
    trial_id: str
    before: PairStatus  # 이전 확정
    after: PairStatus  # 새 확정
    anchor_step: int  # 이전 확정이 마지막으로 관찰된 단계
    step: int  # 새 확정에 도달한 단계
    via: tuple[str, ...]  # 관찰된 중간 상태 (순서대로, 연속 중복 제거)
    new_evidence: bool  # 새 확정에 도달한 기록에 facts_added가 있었는가
    verified: bool  # 이력이 끊기지 않았는가 (False면 unverified_reversals)
    unverified_reason: str | None = None  # UNVERIFIED_REASONS (verified=False일 때만)

    @property
    def direction(self) -> str:
        return "eligible_to_ineligible" if self.before == E else "ineligible_to_eligible"

    @property
    def path_kind(self) -> str:
        return path_kind(self.via)


@dataclass
class ReversalSummary:
    metrics_version: str = METRICS_VERSION
    runs: int = 0
    pairs_observed: int = 0
    confirmed_eligible_to_ineligible: int = 0  # 이력이 이어진 확정 역전 (경유 유형 무관, 내역은 reversals_by_path)
    confirmed_ineligible_to_eligible: int = 0
    reversals_by_path: dict = field(default_factory=lambda: {d: {k: 0 for k in PATH_KINDS} for d in DIRECTIONS})
    direct_reversal_violations: int = 0  # 진단 파일의 차단 진단 (정상 지표에 포함하지 않음)
    direct_violations_by_direction: dict = field(default_factory=lambda: {d: 0 for d in DIRECTIONS})
    downgraded_pending_from_eligible: int = 0  # 확정에서 내려와 아직 반대 확정에 도달하지 않은 쌍
    downgraded_pending_from_ineligible: int = 0
    unverified_reversals: int = 0  # 이력 끊김을 건너 반대 확정에 도달 (확인되지 않음)
    unverified_by_reason: dict = field(default_factory=lambda: {k: 0 for k in UNVERIFIED_REASONS})
    history_gaps: int = 0
    duplicate_records_ignored: int = 0
    conflicting_steps: int = 0
    reversals_without_new_evidence: int = 0  # 확인된 역전 중 새 확정 기록에 facts_added가 없던 것 (정보용)
    events: list[ReversalEvent] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "events"}
        d["events"] = [{"run_id": e.run_id, "patient_alias": e.patient_alias, "trial_id": e.trial_id,
                        "direction": e.direction, "anchor_step": e.anchor_step, "step": e.step, "via": list(e.via),
                        "path_kind": e.path_kind, "new_evidence": e.new_evidence, "verified": e.verified,
                        "unverified_reason": e.unverified_reason} for e in self.events]
        return d


def _dedup(records: Iterable[DecisionRecord], s: ReversalSummary,
           conflicts: set[tuple[str, int]]) -> list[DecisionRecord]:
    by_step: dict[tuple[str, int], list[DecisionRecord]] = defaultdict(list)
    for r in records:
        by_step[(r.run_id, r.step)].append(r)
    out = []
    for key in sorted(by_step):
        rs = by_step[key]
        if len({r.model_dump_json() for r in rs}) > 1:
            s.conflicting_steps += 1
            conflicts.add(key)
            continue
        s.duplicate_records_ignored += len(rs) - 1
        out.append(rs[0])
    return out


def _direct_violations(issues: Iterable, s: ReversalSummary) -> dict[tuple[str, str, str], list[tuple[int, str, str]]]:
    """(run_id, alias, trial) -> [(step, before, after)] (같은 run_id·step·trial 중복 제거)."""
    seen = set()
    out: dict[tuple[str, str, str], list[tuple[int, str, str]]] = defaultdict(list)
    for i in issues:
        if i.code != DIRECT_REVERSAL_CODE or i.severity != "blocking" or not i.trial_id:
            continue
        key = (i.run_id, i.step, i.patient_alias, i.trial_id)
        if key in seen:
            continue
        seen.add(key)
        s.direct_reversal_violations += 1
        if i.before == E.value and i.after == I.value:
            s.direct_violations_by_direction["eligible_to_ineligible"] += 1
        elif i.before == I.value and i.after == E.value:
            s.direct_violations_by_direction["ineligible_to_eligible"] += 1
        out[(i.run_id, i.patient_alias, i.trial_id)].append((i.step, i.before, i.after))
    return out


def compute_reversal_metrics(records: Iterable[DecisionRecord], issues: Iterable = ()) -> ReversalSummary:
    """records: DecisionRecord. issues: record_producer.RecordIssue (차단 직접 역전 집계용).

    넘긴 기록 전체를 한 실행 범위로 보고 run_id로 이력을 나눕니다. 서로 다른 실행 폴더는 compute_for_runs를 쓰세요.
    """
    s = ReversalSummary()
    conflicts: set[tuple[str, int]] = set()
    recs = _dedup(records, s, conflicts)
    violations = _direct_violations(issues, s)
    s.runs = len({r.run_id for r in recs})

    history: dict[tuple[str, str, str], list[tuple[int, object, bool]]] = defaultdict(list)
    for r in recs:  # (run_id, step) 순서
        for t in r.pairs:
            history[(r.run_id, t.pair.patient_alias, t.pair.trial_id)].append((r.step, t, bool(r.facts_added)))
    s.pairs_observed = len(history)

    for (run_id, alias, tid), items in sorted(history.items()):
        anchor: PairStatus | None = None
        anchor_step = -1
        last: PairStatus | None = None
        last_step = -1
        via: list[str] = []
        broken: str | None = None  # anchor 이후 이력 끊김 사유 (None = 이어짐)

        def observe(status: PairStatus, step: int, new_evidence: bool):
            nonlocal anchor, anchor_step, via, broken
            if status in CONFIRMED:
                if anchor is not None and status != anchor:
                    ev = ReversalEvent(run_id, alias, tid, anchor, status, anchor_step, step, tuple(via),
                                       new_evidence, broken is None, broken)
                    s.events.append(ev)
                    if broken is not None:
                        s.unverified_reversals += 1
                        s.unverified_by_reason[broken] += 1
                    else:
                        if anchor == E:
                            s.confirmed_eligible_to_ineligible += 1
                        else:
                            s.confirmed_ineligible_to_eligible += 1
                        s.reversals_by_path[ev.direction][ev.path_kind] += 1
                        if not new_evidence:
                            s.reversals_without_new_evidence += 1
                anchor, anchor_step, via, broken = status, step, [], None
            elif anchor is not None and (not via or via[-1] != status.value):
                via.append(status.value)

        for step, t, new_evidence in items:
            if last is not None and t.before != last:  # 이력 끊김
                s.history_gaps += 1
                explained = any(last_step < vs < step and vb == last.value and va == t.before.value
                                for vs, vb, va in violations.get((run_id, alias, tid), ()))
                if explained:  # 차단 진단이 끊김을 설명: 위반 쪽에서 셈. anchor만 관찰값으로 옮김
                    anchor, anchor_step, via, broken = t.before, step, [], None
                else:
                    broken = ("conflicting_records" if any(r == run_id and last_step < cs < step
                                                           for r, cs in conflicts) else "missing_record")
                    observe(t.before, step, False)
            elif last is None:
                observe(t.before, step, False)
            observe(t.after, step, new_evidence)
            last, last_step = t.after, step

        if anchor is not None and last is not None and last not in CONFIRMED:
            if anchor == E:
                s.downgraded_pending_from_eligible += 1
            else:
                s.downgraded_pending_from_ineligible += 1
    return s


def merge_summaries(parts: Iterable[ReversalSummary]) -> ReversalSummary:
    """서로 독립인 실행(폴더)별 결과를 합침. 이력은 잇지 않고 수만 더함."""
    out = ReversalSummary()
    for p in parts:
        for k, v in p.__dict__.items():
            if k == "metrics_version":
                continue
            if k == "events":
                out.events.extend(v)
            elif isinstance(v, dict):
                cur = getattr(out, k)
                for kk, vv in v.items():
                    if isinstance(vv, dict):
                        for k3, v3 in vv.items():
                            cur[kk][k3] += v3
                    else:
                        cur[kk] += vv
            else:
                setattr(out, k, getattr(out, k) + v)
    return out


def load_run_inputs(run_dir: Path) -> tuple[list[DecisionRecord], list, list[str]]:
    """실행 폴더의 기록·진단을 읽음 (audit.load_run_v2 / record_producer.load_issues). 반환: (기록, 진단, 로드 문제)."""
    from . import audit
    from . import record_producer as rp
    run_dir = Path(run_dir)
    loaded = audit.load_run_v2(run_dir) if (run_dir / audit.RECORDS_FILE).exists() else audit.LoadResult()
    ip = run_dir / rp.ISSUES_FILE
    issues = rp.load_issues(ip) if ip.exists() else []
    return loaded.records, issues, list(loaded.issues)


def compute_for_runs(run_dirs: Iterable[Path]) -> tuple[ReversalSummary, list[str]]:
    """실행 폴더마다 따로 계산해 합침 (run_id가 폴더 사이에서 유일하지 않으므로 폴더가 실행 범위)."""
    parts, problems = [], []
    for d in run_dirs:
        r, i, p = load_run_inputs(d)
        parts.append(compute_reversal_metrics(r, i))
        problems += [f"{Path(d).name}:{x}" for x in p]
    return merge_summaries(parts), problems


def write_metrics(summary: ReversalSummary, path: Path) -> None:
    Path(path).write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")


__all__ = ["ReversalEvent", "ReversalSummary", "compute_reversal_metrics", "compute_for_runs", "load_run_inputs",
           "merge_summaries", "path_kind", "write_metrics", "METRICS_VERSION", "PATH_KINDS", "UNVERIFIED_REASONS"]
