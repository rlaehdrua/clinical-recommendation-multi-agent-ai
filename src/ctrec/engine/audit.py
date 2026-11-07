"""감사 계층 (WP7, I-7): 행동 단위 DecisionRecord(v2) 생성·검증·저장·로드와 Inspector v2 뷰 모델.

연결 고리: action_id -> outcome(상태·청구 비용·새 fact_id) -> 재평가된 기준(전후 판정, 재사용 사유) -> 쌍 상태 전후
-> 버전(policy / evaluator / fact snapshot / contract).

기존 기록과의 관계
- Inspector v1(inspector.py, `ctrec.inspector/v1`)과 기존 trace 이벤트는 바꾸지 않습니다. v2 기록은
  (a) 실행 폴더의 `decision_records.jsonl` 또는 (b) trace.jsonl의 새 이벤트 `engine / decision_record_v2`로 남깁니다.
- v2 로더는 v1 입력(result.json 등)을 요구하지 않으며, v2 기록이 없으면 빈 보기와 issue를 돌려줍니다.

개인정보: 기록에는 ID·코드·숫자만 넣습니다. 다만 계약이 모든 문자열 필드의 형식을 제한하지는 않으므로(facts_added,
*_version 등) 저장 전에 LeakScanner와 호출자가 준 금지 문자열(환자 원문 문장 등)로 한 번 더 검사합니다.
문제 보고(LoadResult.issues)와 로드 예외에는 파일 내용을 넣지 않습니다. 기록 값은 scanner를 줄 때만 조회 시 검사합니다.
화면·API 경로는 audit_inspection.inspect_run_checked를 씁니다. 경로별 보장은 docs/dev/wp7_audit_read_security.md.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, get_args, get_origin

from pydantic import BaseModel, ValidationError

from .contracts import (CONFIRMED, DECISION_RECORD_SCHEMA, Action, ActionOutcome, CriterionChange, DecisionRecord,
                        PairKey, PairStatus, PairTransition)

TRACE_AGENT = "engine"
TRACE_EVENT = "decision_record_v2"
RECORDS_FILE = "decision_records.jsonl"
VIEW_SCHEMA = "ctrec.inspector/v2"
SENSITIVE_RECORD = "sensitive_record"


class AuditError(ValueError):
    pass


# 검사 결과로 그대로 보고할 수 있는 종류. 그 밖의 값은 scanner가 찾은 원문일 수 있어 SCAN_OTHER로만 보고
FREE_TEXT = "free_text"
KNOWN_SCAN_KINDS = frozenset({"original_id", "label_token", "case_id_shape", "forbidden_text", FREE_TEXT})
SCAN_OTHER = "other"
# 계약이 형식을 두지 않는 문자열 필드에 들어온 서술을 찾음 (contracts.py는 바꾸지 않음). 기록 값은 ID·코드뿐이므로 공백·비ASCII는
# 서술로 봄. required_fields와 info_key만 'lab:corrected calcium' 같은 기준 항목 이름이 있어 공백을 조금 허용 (201개 사례 기록 402건 실측).
# info_key는 계약 패턴이 공백 문장도 받으므로, 공백이 있는 값은 canonical_lab이 만드는 검사 항목 모양((basis:)lab:소문자 단어 2~4개)만 허용.
# 검사명은 개방 어휘라 'lab:has severe rash'처럼 이 모양을 흉내 낸 서술은 구분하지 못함
_NOT_CODE = re.compile(r"[^\x21-\x7e]")
_FIELD_NAME = re.compile(r"[\x21-\x7e]+(?: [\x21-\x7e]+){0,3}")
_INFO_KEY = re.compile(r"[\x21-\x7e]+|(?:basis:)?lab:[a-z0-9-]+(?: [a-z0-9-]+){1,3}")
_FIELD_NAME_KEYS = {"required_fields": _FIELD_NAME, "info_key": _INFO_KEY}
_MAX_CODE_CHARS = 256


def _strings(value, key: str = ""):
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, k)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, key)


def has_free_text(record: DecisionRecord) -> bool:
    """코드·ID 모양이 아닌 문자열이 있는가. 공백 없는 ASCII 서술은 찾지 못합니다 (통과가 서술 없음을 뜻하지 않음)."""
    for key, text in _strings(record.model_dump(mode="json")):
        if len(text) > _MAX_CODE_CHARS:
            return True
        if _FIELD_NAME_KEYS[key].fullmatch(text) if key in _FIELD_NAME_KEYS else not _NOT_CODE.search(text):
            continue
        return True
    return False


class CheckedScanner:
    """scanner 결과를 믿지 않고 감쌈: 예외·잘못된 반환은 통과가 아니라 실패, 모르는 종류는 값 없이 SCAN_OTHER."""

    def __init__(self, scanner):
        if not callable(getattr(scanner, "scan", None)):
            raise AuditError("scanner가 필요함")
        self._scanner = scanner

    def scan(self, text: str) -> list[str]:
        try:
            found = self._scanner.scan(text)
        except Exception:
            found = None
        if not isinstance(found, (list, tuple)) or not all(isinstance(k, str) for k in found):
            raise AuditError("scanner 검사 실패")  # except 밖에서 올려 원래 예외(입력 포함 가능)가 남지 않게 함
        return sorted({k if k in KNOWN_SCAN_KINDS else SCAN_OTHER for k in found})


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
                fact_version_before: str | None = None, fact_version_after: str | None = None,
                pair_reasons: dict[str, str] | None = None,
                pair_pending: dict[str, Iterable[str]] | None = None) -> DecisionRecord:
    """판정 계층이 준 전후 상태로 기록을 만듦 (여기서 판정하지 않음).

    criteria_*: {"<trial>:<rule>": MET/NOT_MET/UNKNOWN/NEEDS_FULL_EVALUATION}
    reevaluated: 이번에 다시 판정한 기준 키. 나머지 중 전후 기록이 있는 기준은 재사용(reevaluated=False)으로 남김.
    pair_*: {trial_id: PairStatus}. 쌍 키의 환자는 action.patient_alias.
    pair_reasons: {trial_id: 사유 코드} (0.2). 확정에서 내려가거나 다시 확정할 때 contracts.REVERSAL_REASON_CODES 사용.
    pair_pending: {trial_id: 남은 작업 종류} (0.2, cohort_state.PairState.pending_kinds).
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
    reasons, pending = dict(pair_reasons or {}), {k: tuple(sorted(v)) for k, v in (pair_pending or {}).items()}
    pairs = [PairTransition(pair=PairKey(patient_alias=action.patient_alias, trial_id=t),
                            before=PairStatus(pb.get(t, pa.get(t))), after=PairStatus(pa.get(t, pb.get(t))),
                            authority=pair_authority, reason_code=reasons.get(t), pending_kinds=pending.get(t, ()))
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
    # 기록은 JSON 문자열이라 따옴표·줄바꿈이 있는 문장은 이스케이프된 모양으로만 찾을 수 있음
    if any(t and len(t) >= 4 and (t in data or json.dumps(t, ensure_ascii=False)[1:-1] in data) for t in forbidden_texts):
        out.append("forbidden_text")
    out.extend((scanner or LeakScanner()).scan(data))
    if has_free_text(record):
        out.append(FREE_TEXT)
    return sorted(set(out))


def record_findings(record: DecisionRecord, checked: CheckedScanner) -> list[str]:
    """조회 때 기록 하나를 검사: scanner 결과 + 자유 서술."""
    return sorted({*checked.scan(record.model_dump_json()), *([FREE_TEXT] if has_free_text(record) else [])})


def to_trace(tracer, record: DecisionRecord) -> None:
    """기존 Tracer에 새 이벤트 종류로 남김 (기존 이벤트를 바꾸지 않음).

    주의: 민감 정보를 검사하지 않고, load_run_v2가 trace.jsonl로 폴백하므로 쓴 값이 조회 결과에 나올 수 있습니다.
    새 호출자는 to_trace_checked를 씁니다.
    """
    tracer.log(TRACE_AGENT, TRACE_EVENT, record=record.model_dump(mode="json"))


def to_trace_checked(tracer, record: DecisionRecord, forbidden_texts: Iterable[str] = (), scanner=None) -> None:
    """RecordWriter와 같은 검사를 통과한 기록만 to_trace와 같은 형식으로 남김. 실패하면 쓰지 않고 AuditError (종류만)."""
    found = sensitive_findings(record, forbidden_texts, None if scanner is None else CheckedScanner(scanner))
    if found:
        raise AuditError(f"민감 정보가 있어 기록하지 않음: {', '.join(found)}")
    to_trace(tracer, record)


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


# 문제 보고에 그대로 남겨도 되는 schema_version: 같은 계열의 다른 버전 (이전 기록 구분용). 그 밖의 값은 파일 내용으로 봄
_KNOWN_SCHEMA_RE = re.compile(r"ctrec\.decision_record/v[0-9]{1,4}")
# 한 줄의 손상을 그 줄에서 끝냄. ValueError: JSONDecodeError와 숫자가 너무 긴 정수, RecursionError: 너무 깊은 중첩
_BAD_JSON_ERRORS = (ValueError, RecursionError)
# 읽기 상한. 실제 기록은 실행당 수 KB이고 trace도 수십 KB (2026-11-07 실측). 넘으면 끝까지 읽지 않고 읽기 실패로 처리
MAX_READ_BYTES = 64 * 1024 * 1024
MAX_LINE_PROBLEMS = 10_000  # 문제 목록이 파일보다 커지지 않게 함 (20MB 손상 파일 = 문제 400만 줄, 메모리 757MB 실측)


def _nested_models(tp) -> Iterable[type[BaseModel]]:
    for arg in get_args(tp):
        yield from _nested_models(arg)
    if get_origin(tp) is None and isinstance(tp, type) and issubclass(tp, BaseModel):
        yield tp


def _model_field_names(root: type[BaseModel]) -> frozenset[str]:
    """root와 그 안의 모든 모델의 필드 이름 (코드에서 온 이름만)."""
    names: set[str] = set()
    seen: set[type[BaseModel]] = set()
    todo = [root]
    while todo:
        model = todo.pop()
        if model not in seen:
            seen.add(model)
            names.update(model.model_fields)
            todo.extend(m for f in model.model_fields.values() for m in _nested_models(f.annotation))
    return frozenset(names)


_RECORD_FIELDS = _model_field_names(DecisionRecord)


def _safe_loc(err: dict) -> str:
    """필드 경로. 계약의 필드 이름과 번호만 남김: 추가 필드는 키 이름이 파일 내용이므로 <extra>, 그 밖의 이름은 <unknown>."""
    parts = [str(p) if isinstance(p, int) or p in _RECORD_FIELDS else "<unknown>" for p in err["loc"]]
    if err["type"] == "extra_forbidden":
        parts[-1] = "<extra>"
    return ".".join(parts) or "<root>"


def _issue_fields(e: ValidationError) -> str:
    return ",".join(sorted({_safe_loc(err) for err in e.errors()})[:8])


def _schema_label(ver: object) -> str:
    return ver if isinstance(ver, str) and _KNOWN_SCHEMA_RE.fullmatch(ver) else "?"


def read_limited(path: Path) -> bytes:
    """MAX_READ_BYTES까지만 읽음. 넘으면 AuditError."""
    with Path(path).open("rb") as f:
        data = f.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise AuditError("파일이 너무 큼")
    return data


def _check_problem_count(problems: list[str]) -> None:
    if len(problems) > MAX_LINE_PROBLEMS:
        raise AuditError("문제 줄이 너무 많음")


def _read_text(path: Path) -> str:
    """UTF-8로 읽음. 실패 시 UnicodeDecodeError를 내보내지 않음 (예외 객체의 .object·repr에 파일 전체가 들어 있음)."""
    data = read_limited(path)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    raise AuditError("파일이 UTF-8이 아님")  # except 밖에서 올려 원래 예외가 __context__에 남지 않게 함


def parse_records(items: Iterable[tuple[int, object]], *, scanner=None) -> LoadResult:
    """scanner를 주면 값이 걸린 기록을 제외하고 `line N: sensitive_record:<종류>`로 보고.

    제외한 기록도 무결성 검사(run_id, 역전)에는 넣습니다. 코드·step만 보고하므로 값이 나가지 않고, 경고가 사라지지 않습니다.
    """
    checked = None if scanner is None else CheckedScanner(scanner)
    out = LoadResult()
    seen: Counter = Counter()
    withheld: list[DecisionRecord] = []
    for n, obj in items:
        if not isinstance(obj, dict):
            out.issues.append(f"line {n}: not_object")
            continue
        ver = obj.get("schema_version")
        if ver != DECISION_RECORD_SCHEMA:
            out.issues.append(f"line {n}: unsupported_schema:{_schema_label(ver)}")
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
        found = record_findings(rec, checked) if checked else []
        if found:
            out.issues.append(f"line {n}: {SENSITIVE_RECORD}:{','.join(found)}")
            withheld.append(rec)
            continue
        out.records.append(rec)
    everything = out.records + withheld
    if len({r.run_id for r in everything}) > 1:
        out.issues.append("multiple_run_ids")
    out.records.sort(key=lambda r: (r.run_id, r.step))
    out.issues.extend(reversal_issues(everything))
    return out


def reversal_issues(records: Iterable[DecisionRecord]) -> list[str]:
    """확정 상태 역전 추적 검사 (0.2). 기록을 step 순서로 읽어 쌍마다 마지막 확정 값을 기억합니다.

    - 확정에서 내려갈 때(확정 -> 대기·검토·평가 실패) 사유 코드가 없으면 `unexplained_confirmed_downgrade:<step>`
    - 반대 확정(이전 확정과 다른 확정)에 도달한 기록이 새 근거 사실 없이(facts_added 비어 있음) 확정하면
      `reversal_without_new_evidence:<step>` — 반대 확정은 별도 근거 확인 이후에만 가능
    코드·step만 보고합니다(값 없음). 직접 전이(확정 -> 반대 확정)는 계약(ALLOWED_TRANSITIONS)이 이미 거부합니다.
    """
    last_confirmed: dict[str, PairStatus] = {}
    issues = []
    for r in sorted(records, key=lambda x: (x.run_id, x.step)):
        for t in r.pairs:
            key = f"{r.run_id}|{t.pair.key}"
            if t.before in CONFIRMED and t.after not in CONFIRMED and t.reason_code is None:
                issues.append(f"unexplained_confirmed_downgrade:{r.step}")
            if t.after in CONFIRMED:
                prev = last_confirmed.get(key)
                if prev is not None and prev != t.after and t.before != t.after and not r.facts_added:
                    issues.append(f"reversal_without_new_evidence:{r.step}")
                last_confirmed[key] = t.after
    return issues


def load_records(path: Path, *, scanner=None) -> LoadResult:
    """기록 파일을 줄 단위로 읽음. 손상된 줄은 issues로 보고하고 제외. 파일 전체가 UTF-8이 아니면 AuditError."""
    items, bad = [], []
    for n, line in enumerate(_read_text(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            items.append((n, json.loads(line)))
        except _BAD_JSON_ERRORS:
            bad.append(f"line {n}: corrupt_json")
            _check_problem_count(bad)
    res = parse_records(items, scanner=scanner)
    res.issues = bad + res.issues
    _check_problem_count(res.issues)
    return res


def records_from_trace(trace: list[dict], *, scanner=None) -> LoadResult:
    items = [(i, e.get("record")) for i, e in enumerate(trace, 1)
             if isinstance(e, dict) and e.get("agent") == TRACE_AGENT and e.get("event") == TRACE_EVENT]
    return parse_records(items, scanner=scanner)


def count_withheld(issues: Iterable[str]) -> int:
    """값 검사로 제외한 기록 수."""
    return sum(f": {SENSITIVE_RECORD}:" in i for i in issues)


def load_run_v2(run_dir: Path, scanner=None) -> LoadResult:
    """실행 폴더에서 v2 기록을 읽음: decision_records.jsonl 우선, 없으면 trace.jsonl의 v2 이벤트. 링크는 거부.

    폴더가 아니거나 링크이거나 파일이 UTF-8이 아니거나 읽기 상한(MAX_READ_BYTES, MAX_LINE_PROBLEMS)을 넘으면 AuditError.
    scanner=None이면 값을 검사하지 않습니다(오프라인 평가·검증용). 화면·API는 audit_inspection.inspect_run_checked를 씁니다.
    scanner 검사는 그 scanner가 아는 값만 찾으므로 통과가 안전을 뜻하지는 않습니다.
    """
    checked = None if scanner is None else CheckedScanner(scanner)
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
        return load_records(rec, scanner=checked)
    tr = inside("trace.jsonl")
    if not tr:
        return LoadResult(issues=["no_v2_records"])
    events, bad = [], []
    for n, line in enumerate(_read_text(tr).splitlines(), 1):
        try:
            events.append(json.loads(line)) if line.strip() else None
        except _BAD_JSON_ERRORS:
            bad.append(f"trace line {n}: corrupt_json")
            _check_problem_count(bad)
    res = records_from_trace(events, scanner=checked)
    res.issues = bad + res.issues + ([] if res.records or count_withheld(res.issues) else ["no_v2_records"])
    _check_problem_count(res.issues)
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
                       "changed": t.before != t.after, "reason_code": t.reason_code,
                       "pending_kinds": list(t.pending_kinds)} for t in r.pairs],
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
