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
  미확인 사유(unverified_reason, 한 구간에 여럿이면 뒤쪽이 우선):
    withheld_by_violation : 끊김이 같은 쌍의 차단 진단으로 연결되며 withheld_pair_transition을 포함. 전이 자체는 계약에
                            맞지만 같은 단계의 다른 위반 때문에 정상 기록이 보류됨 (정상 역전 아님, 직접 위반도 아님)
    missing_record        : 진단으로 연결되지 않는 기록 누락
    conflicting_records   : 끊긴 구간에 내용이 충돌해 버린 단계가 있음 (진단이 끊김을 잇지 못할 때)
  끊김 설명 = 같은 (run_id, 환자, 시험)이고 step이 끊긴 구간 안이며, before/after가 직전 after에서 끊긴 뒤 before까지
  step 순서로 빠짐없이 이어지는 진단. 일부만 잇거나, 같은 step이라도 다른 쌍·다른 상태의 진단은 설명이 아님.
  direct_confirmed_reversal 진단은 계약 위반 쪽에서 이미 셌으므로 미확인으로도 세지 않음 (anchor는 그 위반의 after로
  옮김). withheld_pair_transition은 끊김을 분류할 뿐 이력에 넣지 않음. 같은 단계·쌍·전이로 두 진단이 겹치면 직접 위반으로
  한 번만 다룸. 끊김 자체는 원인과 무관하게 history_gaps에 세고, 보류 전이가 설명한 것은 gaps_withheld_by_violation에도 셈.
- 끊김 사이에서 같은 확정으로 돌아온 경우(적격 -> [기록 없음] -> 적격)는 그 사이 숨은 역전을 알 수 없으므로 세지 않고
  history_gaps로만 드러남.
- 중복 기록: 같은 (run_id, step) 기록이 내용까지 같으면 한 번만 씀. 내용이 다르면 둘 다 쓰지 않고 conflicting_steps로
  보고 (그 단계의 변화는 이력 끊김으로 드러남).
- 실행 범위: run_id(`RUN-<별칭>`)는 같은 사례를 다시 실행하면 같으므로 실행 폴더 사이에서 유일하지 않음.
  compute_for_runs는 폴더마다 따로 계산해 합칩니다(폴더가 다른 같은 run_id의 이력을 잇지 않음).
- pairs_observed는 **기록에 실제로 나타난** 환자×시험 쌍 수입니다. 평가 대상 전체 쌍 수가 아니므로 적격성 정확도·역전율의
  분모로 직접 쓰면 안 되고, 관찰되지 않은 쌍은 별도 평가 계층의 대상 목록과 대조해야 합니다.

계산 함수와 파일 로더의 차이
- compute_reversal_metrics는 받은 기록을 그대로 보고 같은 step의 내용을 비교합니다(동일 = 1건, 충돌 = 둘 다 제외).
- audit.parse_records는 내용 비교 없이 첫 줄만 남기고 뒤 줄을 `line N: duplicate_step:S`로 보고합니다. 이 결과만 넘기면
  충돌 기록의 첫 줄이 정상 기록처럼 계산되므로, load_run_inputs가 그 뒤 줄을 원본에서 다시 읽어 함께 넘깁니다.
  audit.py의 동작과 공개 API는 그대로이고, load_run_inputs가 돌려주는 기록에는 중복 줄이 포함될 수 있습니다.
- 진단 파일은 줄 단위로 읽습니다(record_producer.load_issues는 쓰지 않음). load_issues는 손상 시 예외를 내는 쪽이 맞는
  호출자(IssueWriter 이어 쓰기, offline_records.verify_run)가 있어 그대로 둡니다.

값 검사 (scanner, 기본 None = 기존 동작)
- 화면·API로 나갈 지표는 inspect_run_checked와 같은 scanner를 load_run_inputs·compute_for_runs에 넘깁니다. 걸린 기록이 있는
  단계는 같은 단계의 다른 줄까지 계산에서 빠지고(이력 끊김으로 드러남), 걸린 진단 줄은 버립니다. 검사 실패는 통과가 아니라
  읽기 실패로 보고합니다. scanner를 넘기지 않으면 값을 검사하지 않습니다.

기록 원천 (load_run_inputs). 어떤 경우든 폴더 하나의 실패가 다른 폴더의 집계를 막지 않음
- decision_records.jsonl 우선, 없으면 audit.load_run_v2의 기존 폴백으로 trace.jsonl의 v2 이벤트.
- 둘 다 없거나, trace에 v2 이벤트가 없거나, 기록 파일이 비어 있으면 `no_v2_records`: **0건 실행으로 단정하지 않음**.
  현재 cli·웹·render_inspector가 남기는 실제 trace에는 v2 이벤트가 없으므로 모두 여기 해당합니다(역전 지표 미검증).
- trace 이벤트는 DecisionRecord 전체이므로 정상 역전·경유 유형 계산은 파일과 같습니다. 그러나 차단 단계는 진단 파일에만
  남으므로, 진단 파일 없이 trace만 있으면 직접 역전 위반·withheld_by_violation은 계산할 수 없고 그 끊김은
  missing_record로 분류됩니다 (`issues_file_absent:...` 문제로 표시).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import groupby
from operator import attrgetter
from pathlib import Path
from typing import Iterable, NamedTuple

from pydantic import ValidationError

from .contracts import CONFIRMED, DecisionRecord, PairStatus

E, I = PairStatus.CONFIRMED_ELIGIBLE, PairStatus.CONFIRMED_INELIGIBLE
DIRECT_REVERSAL_CODE = "direct_confirmed_reversal"
WITHHELD_TRANSITION_CODE = "withheld_pair_transition"
METRICS_VERSION = "reversal_metrics/0.2"
VERIFICATION_STATES = frozenset({PairStatus.VERIFICATION_PENDING.value, PairStatus.EXPERT_REVIEW.value})
FAILURE_STATES = frozenset({PairStatus.BLOCKED_FAILURE.value, PairStatus.NOT_EVALUATED.value})
PATH_KINDS = ("verification_only", "via_info_missing", "failure_or_not_evaluated")
UNVERIFIED_REASONS = ("withheld_by_violation", "missing_record", "conflicting_records")  # 뒤쪽이 우선
DIRECTIONS = ("eligible_to_ineligible", "ineligible_to_eligible")
_STATUS_VALUES = frozenset(st.value for st in PairStatus)
_DUPLICATE_LINE = re.compile(r"^line (\d+): duplicate_step:\d+$")
_SENSITIVE_LINE = re.compile(r"^line (\d+): sensitive_record:[a-z_,]+$")
_VALUE_BEARING_ISSUE = re.compile(r"^(line \d+: (?:unsupported_schema|invalid_record)):.*$")  # 파일 내용이 섞일 수 있음
MAX_STEP = 10 ** 18 - 1  # 이보다 큰 step은 실행기가 만들 수 없는 값이므로 파일 내용으로 보고 숫자를 옮기지 않음
OUT_OF_RANGE = "<out_of_range>"
_LONG_NUMBER = re.compile(r"\d{19,}")
_STEP = rf"(?:\d{{1,18}}|{OUT_OF_RANGE})"
# write_metrics가 파일에 남기는 문제 코드 전체. 폴더 이름은 콜론을 포함해 무엇이든 될 수 있으므로 코드를 끝에서 정확히 맞춤
_KNOWN_PROBLEM = re.compile(
    r"^(?P<folder>.*):(?P<code>no_v2_records|records_unreadable|records_from_trace|multiple_run_ids"
    r"|issues_file_link|issues_file_unreadable|issues_file_too_large|issues_file_too_damaged"
    r"|issues_file_absent:direct_violations_and_withheld_not_computable"
    r"|(?:conflicting_steps|step_out_of_range):\d{1,18}"
    rf"|(?:unexplained_confirmed_downgrade|reversal_without_new_evidence):{_STEP}"
    r"|trace line \d+: corrupt_json"
    r"|issues line \d+: (?:corrupt_json|not_object|duplicate_issue|invalid_issue:[A-Za-z_<>,.0-9]*"
    r"|sensitive_text:[a-z_,]+)"
    rf"|line \d+: (?:corrupt_json|not_object|invalid_record|unsupported_schema|duplicate_step:{_STEP}"
    r"|sensitive_record:[a-z_,]+))$", re.DOTALL)
UNRECOGNIZED_PROBLEM = "unrecognized_problem"


def path_kind(via: Iterable[str]) -> str:
    v = set(via)
    if v & FAILURE_STATES:
        return "failure_or_not_evaluated"
    if PairStatus.INFO_MISSING.value in v:
        return "via_info_missing"
    return "verification_only"


@dataclass(frozen=True)
class ReversalEvent:
    run_id: str = field(repr=False)  # 식별자는 repr·로그로 나가지 않음
    patient_alias: str = field(repr=False)
    trial_id: str = field(repr=False)
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
    pairs_observed: int = 0  # 기록에 나타난 쌍 수. 평가 대상 전체가 아니므로 정확도·역전율의 분모로 쓰지 말 것
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
    gaps_withheld_by_violation: int = 0  # history_gaps 중 보류 전이 진단으로 연결된 끊김 (역전 도달 여부와 무관)
    duplicate_records_ignored: int = 0
    conflicting_steps: int = 0
    reversals_without_new_evidence: int = 0  # 확인된 역전 중 새 확정 기록에 facts_added가 없던 것 (정보용)
    events: list[ReversalEvent] = field(default_factory=list)

    def to_dict(self, *, identifiers: bool = False) -> dict:
        """기본 출력에는 원시 식별자가 없음: 사건은 pair_ref(같은 쌍이면 같은 값)로만 구분합니다.

        identifiers=True는 분석자가 원본 실행 폴더를 함께 볼 때만 씁니다. pair_ref는 구분용이며 비식별화를 보장하지 않습니다."""
        d = {k: v for k, v in self.__dict__.items() if k != "events"}
        d["events"] = [{**_event_identity(e, identifiers),
                        "direction": e.direction, "anchor_step": e.anchor_step, "step": e.step, "via": list(e.via),
                        "path_kind": e.path_kind, "new_evidence": e.new_evidence, "verified": e.verified,
                        "unverified_reason": e.unverified_reason} for e in self.events]
        return d


def _event_identity(e: ReversalEvent, identifiers: bool) -> dict:
    if identifiers:
        return {"run_id": e.run_id, "patient_alias": e.patient_alias, "trial_id": e.trial_id}
    return {"pair_ref": hashlib.sha256("\0".join((e.run_id, e.patient_alias, e.trial_id)).encode("utf-8")).hexdigest()[:12]}


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


class _GapLink(NamedTuple):
    """끊긴 구간을 이을 수 있는 차단 진단 하나 (정상 기록 아님)."""
    step: int
    before: str
    after: str
    code: str


def _gap_links(issues: Iterable, s: ReversalSummary) -> dict[tuple[str, str, str], list[_GapLink]]:
    """직접 역전 위반을 세고, 끊김 설명에 쓸 진단을 쌍별로 모음. 같은 (단계, 전이)는 직접 위반을 남김."""
    issues = list(issues)
    seen = set()
    links: dict[tuple[str, str, str], dict[tuple, _GapLink]] = defaultdict(dict)
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
        else:
            continue  # 방향이 없는 진단은 위반 수에만 남고 끊김을 설명하지 못함
        links[(i.run_id, i.patient_alias, i.trial_id)][(i.step, i.before, i.after)] = _GapLink(
            i.step, i.before, i.after, i.code)
    for i in issues:
        if (i.code == WITHHELD_TRANSITION_CODE and i.severity == "blocking" and i.trial_id and not i.criterion_key
                and i.before in _STATUS_VALUES and i.after in _STATUS_VALUES and i.before != i.after):
            links[(i.run_id, i.patient_alias, i.trial_id)].setdefault(
                (i.step, i.before, i.after), _GapLink(i.step, i.before, i.after, i.code))
    return {k: sorted(v.values()) for k, v in links.items()}


def _explain_gap(links: Iterable[_GapLink], start: str, end: str, after_step: int,
                 before_step: int) -> tuple[_GapLink, ...] | None:
    """(after_step, before_step) 구간의 진단으로 start -> end를 step 순서대로 빠짐없이 이을 수 있으면 그 경로."""
    paths: dict[str, tuple[_GapLink, ...]] = {start: ()}
    inside = (x for x in links if after_step < x.step < before_step)
    for _, group in groupby(inside, key=attrgetter("step")):
        reached = dict(paths)
        for x in group:  # 한 단계에서는 한 전이만 일어남
            if x.before in paths:
                reached.setdefault(x.after, paths[x.before] + (x,))
        paths = reached
    return paths.get(end)


def compute_reversal_metrics(records: Iterable[DecisionRecord], issues: Iterable = ()) -> ReversalSummary:
    """records: DecisionRecord. issues: record_producer.RecordIssue (차단 직접 역전 집계용).

    넘긴 기록 전체를 한 실행 범위로 보고 run_id로 이력을 나눕니다. 서로 다른 실행 폴더는 compute_for_runs를 쓰세요.
    """
    s = ReversalSummary()
    conflicts: set[tuple[str, int]] = set()
    recs = _dedup(records, s, conflicts)
    links = _gap_links(issues, s)
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

        def mark_broken(reason: str):
            nonlocal broken
            broken = max(filter(None, (broken, reason)), key=UNVERIFIED_REASONS.index)

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
                path = _explain_gap(links.get((run_id, alias, tid), ()), last.value, t.before.value, last_step, step)
                if path is None:
                    mark_broken("conflicting_records" if any(r == run_id and last_step < cs < step
                                                             for r, cs in conflicts) else "missing_record")
                else:
                    if any(x.code == WITHHELD_TRANSITION_CODE for x in path):
                        s.gaps_withheld_by_violation += 1
                    for x in path:
                        if x.code == DIRECT_REVERSAL_CODE:  # 위반 쪽에서 셈: anchor만 옮김
                            anchor, anchor_step, via, broken = PairStatus(x.after), x.step, [], None
                        else:
                            mark_broken("withheld_by_violation")
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


def _raw_record_items(run_dir: Path) -> list[tuple[int, object]]:
    """audit가 parse_records에 넘기는 것과 같은 (번호, 객체) 목록. 번호는 audit 문제 메시지의 line N과 일치."""
    from . import audit

    def objects(path: Path):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                if line.strip():
                    yield n, json.loads(line)
            except (ValueError, RecursionError):  # 깨진 줄, 초장 정수, 깊은 중첩: 그 줄만 건너뜀
                pass

    records = run_dir / audit.RECORDS_FILE
    if records.exists():
        return list(objects(records))
    events = [obj for _, obj in objects(run_dir / "trace.jsonl")]
    return [(n, e.get("record")) for n, e in enumerate(events, 1)
            if isinstance(e, dict) and e.get("agent") == audit.TRACE_AGENT and e.get("event") == audit.TRACE_EVENT]


def _reread_lines(run_dir: Path, load_issues: Iterable[str], checked) -> tuple[list[DecisionRecord], set, list[str]]:
    """audit가 duplicate_step으로 뺀 줄을 다시 읽음. 동일·충돌 판단은 compute_reversal_metrics._dedup이 함.

    반환: (쓸 수 있는 중복 줄, 값 검사에 걸린 (run_id, step), 중복 줄에서 새로 걸린 문제). audit은 중복 줄을 검사하지 않으므로
    여기서 검사합니다. 걸린 단계는 어느 줄이 맞는지 알 수 없으므로 같은 단계의 다른 줄까지 호출자가 뺍니다.
    """
    issues = list(load_issues)
    lines = {int(m.group(1)) for m in map(_DUPLICATE_LINE.match, issues) if m}
    sensitive = {int(m.group(1)) for m in map(_SENSITIVE_LINE.match, issues) if m}
    if not lines | sensitive:
        return [], set(), []
    items = [(n, DecisionRecord.model_validate(o)) for n, o in _raw_record_items(run_dir) if n in lines | sensitive]
    withheld = {(r.run_id, r.step) for n, r in items if n in sensitive}  # 검증된 값: 파일의 "3"과 3이 같은 단계
    kept, problems = [], []
    for n, rec in items:
        if n not in lines:
            continue
        found = checked.scan(rec.model_dump_json()) if checked else []
        if found:
            problems.append(f"line {n}: sensitive_record:{','.join(found)}")
            withheld.add((rec.run_id, rec.step))
        else:
            kept.append(rec)
    return kept, withheld, problems


def _in_range(records: list[DecisionRecord]) -> tuple[list[DecisionRecord], list[str]]:
    kept = [r for r in records if r.step <= MAX_STEP]
    return kept, ([f"step_out_of_range:{len(records) - len(kept)}"] if len(kept) != len(records) else [])


def _issue_field_names(e: ValidationError) -> str:
    """검증 실패 필드. 추가 필드는 키 자체가 파일 내용일 수 있으므로 이름을 옮기지 않음."""
    from . import record_producer as rp
    return ",".join(sorted({str(err["loc"][0]) if err["loc"] and err["loc"][0] in rp.RecordIssue.model_fields
                            else "<unknown>" for err in e.errors()}))


def _load_issues_partial(run_dir: Path, checked=None) -> tuple[list, list[str]]:
    """진단 파일을 줄 단위로 읽음: 유효한 줄은 살리고 손상된 줄은 번호·코드·필드 이름으로만 보고 (값·원문 없음)."""
    from . import audit
    from . import record_producer as rp
    p = run_dir / rp.ISSUES_FILE
    if not p.exists() and not p.is_symlink():
        return [], []
    if run_dir.is_symlink() or p.is_symlink() or run_dir.resolve() not in p.resolve().parents:
        return [], ["issues_file_link"]
    try:
        data = audit.read_limited(p)
    except audit.AuditError:
        return [], ["issues_file_too_large"]
    except OSError:
        return [], ["issues_file_unreadable"]
    issues, problems, seen = [], [], set()
    for n, raw in enumerate(data.splitlines(), 1):  # 바이트로 나눠 잘못된 UTF-8도 그 줄에서 끝나게 함
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # UnicodeDecodeError, JSONDecodeError 모두 ValueError
            problems.append(f"issues line {n}: corrupt_json")
            continue
        if not isinstance(obj, dict):
            problems.append(f"issues line {n}: not_object")
            continue
        try:
            issue = rp.RecordIssue.model_validate(obj)
        except ValidationError as e:
            problems.append(f"issues line {n}: invalid_issue:{_issue_field_names(e)}")
            continue
        found = checked.scan(issue.model_dump_json()) if checked else []
        if found:
            problems.append(f"issues line {n}: sensitive_text:{','.join(found)}")
            continue
        if issue.issue_id in seen:
            problems.append(f"issues line {n}: duplicate_issue")
            continue
        seen.add(issue.issue_id)
        issues.append(issue)
    if len(problems) > audit.MAX_LINE_PROBLEMS:
        return [], ["issues_file_too_damaged"]
    return issues, problems


def load_run_inputs(run_dir: Path, scanner=None) -> tuple[list[DecisionRecord], list, list[str]]:
    """실행 폴더의 기록·진단을 읽음. 반환: (기록, 진단, 로드 문제). 폴더 단위 실패도 예외가 아니라 문제로 돌려줌.

    기록에는 duplicate_step으로 빠진 줄도 포함됩니다(모듈 설명의 '계산 함수와 파일 로더의 차이').
    scanner를 주면 걸린 기록·진단을 뺍니다(모듈 설명의 '값 검사'). 검사가 실패한 파일은 읽지 못한 것으로 보고합니다.
    """
    from . import audit
    from . import record_producer as rp
    run_dir = Path(run_dir)
    checked = None if scanner is None else audit.CheckedScanner(scanner)
    try:
        loaded = audit.load_run_v2(run_dir, scanner=checked)
        extra, withheld, extra_problems = _reread_lines(run_dir, loaded.issues, checked)
        records = [r for r in loaded.records + extra if (r.run_id, r.step) not in withheld]
    except (ValueError, OSError):  # AuditError·ValidationError·UnicodeDecodeError 포함. 메시지·값은 옮기지 않음
        loaded, records, extra_problems = audit.LoadResult(issues=["records_unreadable"]), [], []
    records, range_problems = _in_range(records)
    problems = [_LONG_NUMBER.sub(OUT_OF_RANGE, _VALUE_BEARING_ISSUE.sub(r"\1", x)) for x in loaded.issues]
    problems += extra_problems + range_problems
    if not records and not problems:  # 빈 기록 파일: 로더는 문제 없이 0건을 돌려주고 trace로 폴백하지 않음
        problems.append("no_v2_records")
    try:
        issues, issue_problems = _load_issues_partial(run_dir, checked)
    except audit.AuditError:
        issues, issue_problems = [], ["issues_file_unreadable"]
    if records and not (run_dir / audit.RECORDS_FILE).exists():
        problems.append("records_from_trace")
        if not (run_dir / rp.ISSUES_FILE).exists():
            problems.append("issues_file_absent:direct_violations_and_withheld_not_computable")
    return records, issues, problems + issue_problems


def compute_for_runs(run_dirs: Iterable[Path], scanner=None) -> tuple[ReversalSummary, list[str]]:
    """실행 폴더마다 따로 계산해 합침 (run_id가 폴더 사이에서 유일하지 않으므로 폴더가 실행 범위).

    문제가 있는 폴더는 읽을 수 있는 만큼만 계산하고 문제를 `<폴더 이름>:<코드>`로 돌려줍니다. 수치만 보지 말고 함께 볼 것.
    폴더 이름이 들어 있으므로 이 목록은 write_metrics로만 내보냅니다. scanner는 load_run_inputs와 같습니다.
    """
    parts, problems = [], []
    for d in run_dirs:
        r, i, p = load_run_inputs(d, scanner)
        part = compute_reversal_metrics(r, i)
        parts.append(part)
        if part.conflicting_steps:
            p = p + [f"conflicting_steps:{part.conflicting_steps}"]
        problems += [f"{Path(d).name}:{x}" for x in p]
    return merge_summaries(parts), problems


def write_metrics(summary: ReversalSummary, path: Path, problems: Iterable[str] = ()) -> None:
    """지표 JSON 저장. compute_for_runs의 문제는 load_problems로 함께 남겨 손상·누락이 파일에서 사라지지 않게 함."""
    folders: dict[str, str] = {}  # 폴더 이름에 run_id·별칭이 들어갈 수 있어 순번으로 바꿈

    def anonymous(problem: str) -> str:
        m = _KNOWN_PROBLEM.match(str(problem))
        if m is None:  # 형식을 모르면 어디까지가 값인지 알 수 없음: 있었다는 사실만 남김
            return UNRECOGNIZED_PROBLEM
        return f"{folders.setdefault(m['folder'], f'run-{len(folders) + 1}')}:{m['code']}"

    data = {**summary.to_dict(), "load_problems": [anonymous(p) for p in problems]}
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


__all__ = ["ReversalEvent", "ReversalSummary", "compute_reversal_metrics", "compute_for_runs", "load_run_inputs",
           "merge_summaries", "path_kind", "write_metrics", "METRICS_VERSION", "PATH_KINDS", "UNVERIFIED_REASONS",
           "UNRECOGNIZED_PROBLEM"]
