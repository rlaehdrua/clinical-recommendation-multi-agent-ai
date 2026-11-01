"""감사 계층 (WP7, I-7): 행동 단위 DecisionRecord(v2) 생성·검증·저장·로드와 Inspector v2 뷰 모델.

연결 고리: action_id -> outcome(상태·청구 비용·새 fact_id) -> 재평가된 기준(전후 판정, 재사용 사유) -> 쌍 상태 전후
-> 버전(policy / evaluator / fact snapshot / contract).

기존 기록과의 관계
- Inspector v1(inspector.py, `ctrec.inspector/v1`)과 기존 trace 이벤트는 바꾸지 않습니다. v2 기록은
  (a) 실행 폴더의 `decision_records.jsonl` 또는 (b) trace.jsonl의 새 이벤트 `engine / decision_record_v2`로 남깁니다.
- v2 로더는 v1 입력(result.json 등)을 요구하지 않으며, v2 기록이 없으면 빈 보기와 issue를 돌려줍니다.

개인정보: 기록에는 ID·코드·숫자만 들어갑니다(계약이 자유 서술 필드를 두지 않음). 저장 전에 LeakScanner와
호출자가 준 금지 문자열(환자 원문 문장 등)로 한 번 더 검사합니다.
"""

from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from .contracts import (DECISION_RECORD_SCHEMA, Action, ActionOutcome, CriterionChange, DecisionRecord, PairKey,
                        PairStatus, PairTransition)

TRACE_AGENT = "engine"
TRACE_EVENT = "decision_record_v2"
RECORDS_FILE = "decision_records.jsonl"
VIEW_SCHEMA = "ctrec.inspector/v2"


class AuditError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 생성
# ---------------------------------------------------------------------------

def make_record(*, run_id: str, step: int, action: Action, outcome: ActionOutcome | None,
                criteria_before: dict[str, str] | None = None, criteria_after: dict[str, str] | None = None,
                reevaluated: Iterable[str] = (), reuse_reason: str = "dependency_unchanged",
                reevaluation_reason: str = "fact_dependency_changed",
                criterion_evidence: dict[str, tuple[str, ...]] | None = None,
                pair_before: dict[str, PairStatus | str] | None = None,
                pair_after: dict[str, PairStatus | str] | None = None,
                pair_authority: str = "adjudication", policy_version: str, evaluator_version: str | None = None,
                fact_version_before: str | None = None, fact_version_after: str | None = None) -> DecisionRecord:
    """판정 계층이 준 전후 상태로 기록을 만듦 (여기서 판정하지 않음).

    criteria_*: {"<trial>:<rule>": MET/NOT_MET/UNKNOWN/NEEDS_FULL_EVALUATION}
    reevaluated: 이번에 다시 판정한 기준 키. 나머지 중 전후 기록이 있는 기준은 재사용(reevaluated=False)으로 남김.
    pair_*: {trial_id: PairStatus}. 쌍 키의 환자는 action.patient_alias.
    """
    cb, ca = dict(criteria_before or {}), dict(criteria_after or {})
    re_set = set(reevaluated)
    unknown = re_set - set(ca)
    if unknown:
        raise AuditError(f"재평가 기준 {len(unknown)}개의 재평가 결과가 없음")
    changes = []
    for key in sorted(set(cb) | set(ca)):
        before, after = cb.get(key, "NEEDS_FULL_EVALUATION"), ca.get(key, cb.get(key, "NEEDS_FULL_EVALUATION"))
        is_re = key in re_set
        changes.append(CriterionChange(
            criterion_key=key, before=before, after=after, reevaluated=is_re,
            reason_code=reevaluation_reason if is_re else reuse_reason,
            evaluator_version=evaluator_version if is_re else None,
            evidence_fact_ids=tuple((criterion_evidence or {}).get(key, ()))))
    pb, pa = dict(pair_before or {}), dict(pair_after or {})
    pairs = [PairTransition(pair=PairKey(patient_alias=action.patient_alias, trial_id=t),
                            before=PairStatus(pb.get(t, pa.get(t))), after=PairStatus(pa.get(t, pb.get(t))),
                            authority=pair_authority)
             for t in sorted(set(pb) | set(pa))]
    return DecisionRecord(run_id=run_id, step=step, action=action, outcome=outcome,
                          facts_added=tuple(outcome.new_fact_ids) if outcome else (), criteria=tuple(changes),
                          pairs=tuple(pairs), policy_version=policy_version, evaluator_version=evaluator_version,
                          fact_version_before=fact_version_before, fact_version_after=fact_version_after)


def sensitive_findings(record: DecisionRecord, forbidden_texts: Iterable[str] = (), scanner=None) -> list[str]:
    """기록 JSON에 금지 문자열(환자 원문 문장, 원래 ID 등)·정답 토큰이 있으면 종류 목록."""
    from ..eval_hygiene import LeakScanner
    data = record.model_dump_json()
    out = []
    if any(t and len(t) >= 4 and t in data for t in forbidden_texts):
        out.append("forbidden_text")
    out.extend((scanner or LeakScanner()).scan(data))
    return sorted(set(out))


def to_trace(tracer, record: DecisionRecord) -> None:
    """기존 Tracer에 새 이벤트 종류로 남김 (기존 이벤트를 바꾸지 않음)."""
    tracer.log(TRACE_AGENT, TRACE_EVENT, record=record.model_dump(mode="json"))


class RecordWriter:
    """decision_records.jsonl에 한 줄씩 추가. 같은 run_id·step 중복과 민감 정보를 거부."""

    def __init__(self, path: Path, run_id: str, forbidden_texts: Iterable[str] = (), scanner=None):
        self.path = Path(path)
        self.run_id = run_id
        self.forbidden = [t for t in forbidden_texts if t]
        self.scanner = scanner
        self._steps: set[int] = set()
        if self.path.is_symlink():
            raise AuditError("기록 파일이 링크임")
        if self.path.exists():
            loaded = load_records(self.path)
            if loaded.issues:
                raise AuditError("기존 기록 파일에 문제가 있어 이어 쓰지 않음")
            if any(r.run_id != run_id for r in loaded.records):
                raise AuditError("다른 실행의 기록 파일에 이어 쓰지 않음")
            self._steps = {r.step for r in loaded.records}

    def write(self, record: DecisionRecord) -> None:
        if record.run_id != self.run_id:
            raise AuditError("run_id가 다름")
        if record.step in self._steps:
            raise AuditError(f"step {record.step} 기록이 이미 있음")
        found = sensitive_findings(record, self.forbidden, self.scanner)
        if found:
            raise AuditError(f"민감 정보가 있어 기록하지 않음: {', '.join(found)}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")
        self._steps.add(record.step)


# ---------------------------------------------------------------------------
# 로드
# ---------------------------------------------------------------------------

@dataclass
class LoadResult:
    records: list[DecisionRecord] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)  # 코드·줄 번호·필드 경로만 (값 없음)


def _issue_fields(e: ValidationError) -> str:
    locs = sorted({".".join(str(p) for p in err["loc"]) or "<root>" for err in e.errors()})
    return ",".join(locs[:8])


def parse_records(items: Iterable[tuple[int, object]]) -> LoadResult:
    out = LoadResult()
    seen: Counter = Counter()
    for n, obj in items:
        if not isinstance(obj, dict):
            out.issues.append(f"line {n}: not_object")
            continue
        ver = obj.get("schema_version")
        if ver != DECISION_RECORD_SCHEMA:
            out.issues.append(f"line {n}: unsupported_schema:{ver if isinstance(ver, str) and len(ver) < 64 else '?'}")
            continue
        try:
            rec = DecisionRecord.model_validate(obj)
        except ValidationError as e:
            out.issues.append(f"line {n}: invalid_record:{_issue_fields(e)}")
            continue
        seen[(rec.run_id, rec.step)] += 1
        if seen[(rec.run_id, rec.step)] > 1:
            out.issues.append(f"line {n}: duplicate_step:{rec.step}")
            continue
        out.records.append(rec)
    if len({r.run_id for r in out.records}) > 1:
        out.issues.append("multiple_run_ids")
    out.records.sort(key=lambda r: (r.run_id, r.step))
    return out


def load_records(path: Path) -> LoadResult:
    items, bad = [], []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            items.append((n, json.loads(line)))
        except json.JSONDecodeError:
            bad.append(f"line {n}: corrupt_json")
    res = parse_records(items)
    res.issues = bad + res.issues
    return res


def records_from_trace(trace: list[dict]) -> LoadResult:
    items = [(i, e.get("record")) for i, e in enumerate(trace, 1)
             if isinstance(e, dict) and e.get("agent") == TRACE_AGENT and e.get("event") == TRACE_EVENT]
    return parse_records(items)


def load_run_v2(run_dir: Path) -> LoadResult:
    """실행 폴더에서 v2 기록을 읽음: decision_records.jsonl 우선, 없으면 trace.jsonl의 v2 이벤트. 링크는 거부."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise AuditError("실행 폴더가 아님")
    base = run_dir.resolve()

    def inside(name: str) -> Path | None:
        p = run_dir / name
        if not p.exists():
            return None
        if p.is_symlink() or base not in p.resolve().parents:
            raise AuditError(f"{name}: 실행 폴더 밖을 가리킴")
        return p

    rec = inside(RECORDS_FILE)
    if rec:
        return load_records(rec)
    tr = inside("trace.jsonl")
    if not tr:
        return LoadResult(issues=["no_v2_records"])
    events, bad = [], []
    for n, line in enumerate(tr.read_text(encoding="utf-8").splitlines(), 1):
        try:
            events.append(json.loads(line)) if line.strip() else None
        except json.JSONDecodeError:
            bad.append(f"trace line {n}: corrupt_json")
    res = records_from_trace(events)
    res.issues = bad + res.issues + ([] if res.records else ["no_v2_records"])
    return res


# ---------------------------------------------------------------------------
# Inspector v2 뷰 모델
# ---------------------------------------------------------------------------

def build_v2_view(loaded: LoadResult) -> dict:
    steps, by_patient = [], defaultdict(Counter)
    pair_history: dict[str, list[dict]] = defaultdict(list)
    fact_origin: dict[str, str] = {}
    for r in loaded.records:
        a, o = r.action, r.outcome
        cost = o.cost_charged if o else None
        p = by_patient[a.patient_alias]
        p["actions"] += 1
        if o:
            p[f"outcome_{o.status}"] += 1
            p["coordinator_minutes_x100"] += round(cost.coordinator_minutes * 100)
            p["clinician_minutes_x100"] += round(cost.clinician_minutes * 100)
            p["facts_added"] += len(r.facts_added)
        for fid in r.facts_added:
            fact_origin.setdefault(fid, a.action_id)
        for t in r.pairs:
            pair_history[t.pair.key].append({"step": r.step, "action_id": a.action_id, "before": t.before.value,
                                             "after": t.after.value, "authority": t.authority})
        steps.append({
            "step": r.step, "action_id": a.action_id, "action_type": a.action_type, "patient": a.patient_alias,
            "source_id": a.source_id, "reason_code": a.reason_code, "tier": a.tier,
            "safety_required": a.safety_required, "targets": list(a.target_criteria),
            "expected_minutes": a.expected_cost.coordinator_minutes,
            "outcome": None if o is None else {
                "status": o.status, "error_code": o.error_code, "coordinator_minutes": cost.coordinator_minutes,
                "clinician_minutes": cost.clinician_minutes, "llm_calls": cost.llm_calls,
                "patient_wait_minutes": cost.patient_wait_minutes, "charge_keys": list(o.charge_keys)},
            "facts_added": list(r.facts_added),
            "criteria": [{"key": c.criterion_key, "before": c.before, "after": c.after, "reevaluated": c.reevaluated,
                          "reason_code": c.reason_code, "changed": c.before != c.after,
                          "evidence_fact_ids": list(c.evidence_fact_ids)} for c in r.criteria],
            "pairs": [{"pair": t.pair.key, "before": t.before.value, "after": t.after.value,
                       "changed": t.before != t.after} for t in r.pairs],
            "versions": {"policy": r.policy_version, "evaluator": r.evaluator_version,
                         "fact_before": r.fact_version_before, "fact_after": r.fact_version_after,
                         "contract": r.contract_version},
        })
    patients = {k: {"actions": v["actions"], "facts_added": v["facts_added"],
                    "coordinator_minutes": v["coordinator_minutes_x100"] / 100,
                    "clinician_minutes": v["clinician_minutes_x100"] / 100,
                    "failed_or_no_response": v["outcome_failed"] + v["outcome_no_response"],
                    "partial": v["outcome_partial"], "succeeded": v["outcome_succeeded"]}
                for k, v in sorted(by_patient.items())}
    return {"schema": VIEW_SCHEMA, "run_ids": sorted({r.run_id for r in loaded.records}), "steps": steps,
            "patients": patients, "pair_history": dict(pair_history), "fact_origin": fact_origin,
            "issues": list(loaded.issues)}


def chain(view: dict, action_id: str) -> dict | None:
    """행동 하나의 연결: 행동 -> 결과 -> 새 사실 -> 바뀐 기준 -> 바뀐 쌍 상태."""
    s = next((x for x in view["steps"] if x["action_id"] == action_id), None)
    if s is None:
        return None
    return {"action_id": action_id, "outcome": (s["outcome"] or {}).get("status"), "facts": s["facts_added"],
            "criteria_changed": [c["key"] for c in s["criteria"] if c["changed"]],
            "criteria_reused": [c["key"] for c in s["criteria"] if not c["reevaluated"]],
            "pairs_changed": [(p["pair"], p["before"], p["after"]) for p in s["pairs"] if p["changed"]]}


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def render_v2_html(view: dict) -> str:
    """읽기 전용 정적 HTML (외부 스크립트 없음, 모든 값 이스케이프)."""
    rows = []
    for s in view["steps"]:
        o = s["outcome"] or {}
        crit = "<br>".join(f"{_e(c['key'])}: {_e(c['before'])}→{_e(c['after'])}"
                           f"{'' if c['reevaluated'] else ' (재사용)'}" for c in s["criteria"])
        pairs = "<br>".join(f"{_e(p['pair'].split('|')[1])}: {_e(p['before'])}→{_e(p['after'])}" for p in s["pairs"])
        rows.append(f"<tr><td>{_e(s['step'])}</td><td>{_e(s['action_type'])}<br><small>{_e(s['action_id'])}"
                    f"</small></td><td>{_e(s['patient'])}</td><td>{_e(s['source_id'])}</td>"
                    f"<td>{_e(o.get('status', '결과 없음'))}{' / ' + _e(o['error_code']) if o.get('error_code') else ''}"
                    f"</td><td>{_e(o.get('coordinator_minutes'))}</td><td>{_e(', '.join(s['facts_added']))}</td>"
                    f"<td>{crit}</td><td>{pairs}</td></tr>")
    issues = "".join(f"<li>{_e(i)}</li>" for i in view["issues"]) or "<li>없음</li>"
    head = ("<tr><th>단계</th><th>행동</th><th>환자 별칭</th><th>정보원</th><th>결과</th><th>코디네이터 분</th>"
            "<th>새 사실</th><th>기준 전→후</th><th>쌍 상태 전→후</th></tr>")
    return ("<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<title>Decision Records v2</title><style>body{font-family:sans-serif;margin:16px}"
            "table{border-collapse:collapse}td,th{border:1px solid #999;padding:4px;vertical-align:top}</style>"
            f"</head><body><h1>행동 단위 감사 기록 (v2)</h1><p>실행: {_e(', '.join(view['run_ids']))}</p>"
            f"<table>{head}{''.join(rows)}</table><h2>기록 문제</h2><ul>{issues}</ul></body></html>")
