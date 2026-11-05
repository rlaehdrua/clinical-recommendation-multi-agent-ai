"""Inspector v2 진단 연결 (WP7): 정상 DecisionRecord와 decision_record_issues.jsonl 진단을 함께 조회.

기존 `audit.load_run_v2` / `LoadResult` / `build_v2_view`는 바꾸지 않습니다. `LoadResult.issues`는 '기록 파일 로드 문제'
의미로 계속 쓰이므로(offline_records.verify_run, 기존 테스트) 진단을 그 목록에 섞지 않고 별도 키로 붙입니다.

원칙
- 누락된 DecisionRecord를 복원하지 않습니다. 차단 단계는 진단만 보여 주고 기록이 없다고 표시합니다.
- 쌍 전이 진단은 뜻이 둘이라 나눠 둡니다. 둘 다 `pair_history`·`steps[].pairs`(정상 PairTransition)에는 넣지 않습니다.
  - `diagnostics.violation_transitions`: 전이 자체가 계약을 어김 (direct_confirmed_reversal 등)
  - `diagnostics.withheld_transitions`: 전이 자체는 검사를 통과했지만 같은 단계의 다른 위반 때문에 정상 기록이 보류됨
    (withheld_pair_transition). `withheld_by`가 그 단계의 위반 코드입니다.
  - `diagnostics.blocked_transitions`는 호환용으로 남긴 합본(위 둘, 기존 키만)입니다. 새 코드는 쓰지 않습니다.
- 진단 파일은 `record_producer.RecordIssue` 모델로 다시 검증합니다(필드 형식 제한, 추가 필드 거부). 검증에 실패한 줄은
  값 없이 줄 번호·모델 필드 이름만 문제로 보고하고 버립니다(추가 필드의 키 이름도 파일 내용이므로 `<extra>`로만 표시).
- 형식 검사는 공백·한글 원문은 막지만 ID 모양 값(원래 환자 ID 등)은 통과시키므로 값을 내보내려면 scanner가 있어야 합니다.
  scanner(eval_hygiene.LeakScanner 등)에 걸린 줄은 위와 같이 제외합니다(엔진 격리 규칙 때문에 이 모듈은 eval_hygiene를
  import하지 않고 주입받음). scanner=None이면 진단 값을 보기로 내보내지 않고 file_status="not_scanned",
  audit_status="diagnostics_not_scanned"로 둡니다. 정상 기록 조회(load_run_v2)는 scanner와 무관하게 그대로입니다.
  계약은 docs/dev/wp7_offline_records.md §3.
- 진단 파일이 없으면 file_status="absent". 진단 없이 끝난 실행과 진단 기능 이전의 실행은 구분할 수 없으므로
  '정상 완료'라고 단정하지 않고 audit_status="no_blocking_diagnostics"로 둡니다.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from . import audit
from . import record_producer as rp

VIEW_KEY = "diagnostics"
FileStatus = Literal["absent", "ok", "partial", "unreadable", "not_scanned"]
_SEVERITY_ORDER = {"blocking": 0, "warning": 1}
WITHHELD_PAIR_CODE = "withheld_pair_transition"
# 위반 때문에 보류된 변화를 알리는 진단. 위반의 원인이 아니라 결과입니다 (record_producer.build_step).
_WITHHELD_CODES = frozenset({WITHHELD_PAIR_CODE, "withheld_criterion_change", "withheld_outcome"})


@dataclass
class DiagnosticsLoad:
    file_status: FileStatus
    issues: list[rp.RecordIssue] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # 줄 번호·코드·모델 필드 이름만 (값 없음)


def _fields(e: ValidationError) -> str:
    # 추가 필드의 키 이름은 파일에서 온 값이므로 그대로 쓰지 않음
    locs = sorted({"<extra>" if err["type"] == "extra_forbidden" else ".".join(str(p) for p in err["loc"]) or "<root>"
                   for err in e.errors()})
    return ",".join(locs[:8])


def load_diagnostics(run_dir: Path, run_ids: set[str] | None = None, scanner=None) -> DiagnosticsLoad:
    """실행 폴더의 진단 파일을 읽음. 실행 폴더가 아니면 audit.AuditError (load_run_v2와 같음).

    run_ids: 정상 기록의 run_id. 주면 다른 실행의 진단 줄은 제외하고 문제로 보고.
    scanner: `scan(text) -> list[종류 코드]`. None이면 파일 값을 돌려주지 않음 (file_status="not_scanned").
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise audit.AuditError("실행 폴더가 아님")
    p = run_dir / rp.ISSUES_FILE
    if not p.exists() and not p.is_symlink():
        return DiagnosticsLoad("absent")
    if p.is_symlink() or run_dir.resolve() not in p.resolve().parents:
        return DiagnosticsLoad("unreadable", problems=["issues_file_link"])
    try:
        text = p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return DiagnosticsLoad("unreadable", problems=["issues_file_not_utf8"])
    except OSError:
        return DiagnosticsLoad("unreadable", problems=["issues_file_unreadable"])
    if scanner is None:
        return DiagnosticsLoad("not_scanned", problems=["scanner_required"])

    out = DiagnosticsLoad("ok")
    seen: set[str] = set()
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            out.problems.append(f"line {n}: corrupt_json")
            continue
        if not isinstance(obj, dict):
            out.problems.append(f"line {n}: not_object")
            continue
        try:
            issue = rp.RecordIssue.model_validate(obj)
        except ValidationError as e:
            out.problems.append(f"line {n}: invalid_issue:{_fields(e)}")
            continue
        found = scanner.scan(issue.model_dump_json())
        if found:
            out.problems.append(f"line {n}: sensitive_text:{','.join(sorted(set(found)))}")
            continue
        if run_ids and issue.run_id not in run_ids:
            out.problems.append(f"line {n}: other_run")
            continue
        if issue.issue_id in seen:
            out.problems.append(f"line {n}: duplicate_issue")
            continue
        seen.add(issue.issue_id)
        out.issues.append(issue)
    if not run_ids and len({i.run_id for i in out.issues}) > 1:
        out.problems.append("multiple_run_ids")
    if out.problems:
        out.file_status = "partial"
    out.issues.sort(key=lambda i: (i.step, _SEVERITY_ORDER[i.severity], i.code, i.issue_id))
    return out


def load_run_with_diagnostics(run_dir: Path, scanner=None) -> tuple[audit.LoadResult, DiagnosticsLoad]:
    """정상 기록(기존 load_run_v2 그대로)과 진단을 함께 읽음."""
    loaded = audit.load_run_v2(run_dir)
    return loaded, load_diagnostics(run_dir, {r.run_id for r in loaded.records}, scanner)


def _is_blocked_pair_transition(i: rp.RecordIssue) -> bool:
    """쌍 단위 before/after를 담은 차단 진단 (기준 단위 진단은 criterion_key가 있어 제외)."""
    return (i.severity == "blocking" and i.trial_id is not None and i.criterion_key is None
            and i.before is not None and i.after is not None)


def _withheld_by(issues: list[rp.RecordIssue]) -> list[str]:
    """보류를 일으킨 그 단계의 위반 코드."""
    return sorted({i.code for i in issues if i.severity == "blocking" and i.code not in _WITHHELD_CODES})


def build_diagnostics_view(loaded: audit.LoadResult, diag: DiagnosticsLoad) -> dict:
    """build_v2_view 결과(기존 키·값 그대로) + "diagnostics" + "audit_status"."""
    view = audit.build_v2_view(loaded)
    records = {r.step: r for r in loaded.records}
    problems = list(diag.problems)
    by_step: dict[int, list[rp.RecordIssue]] = {}
    for i in diag.issues:
        by_step.setdefault(i.step, []).append(i)

    steps = []
    for step in sorted(set(records) | set(by_step)):
        rec, iss = records.get(step), by_step.get(step, [])
        blocking = sum(i.severity == "blocking" for i in iss)
        warnings = len(iss) - blocking
        if rec is not None and blocking:
            status = "inconsistent"
            problems.append(f"blocking_step_has_record:{step}")
        elif rec is not None:
            status = "recorded_with_warnings" if warnings else "recorded"
        elif blocking:
            status = "contract_violation"
        else:
            status = "diagnostics_without_record"  # 생산기는 경고만 있는 단계에도 기록을 남김 -> 기록 누락 의심
            problems.append(f"warning_step_without_record:{step}")
        action_ids = sorted({i.action_id for i in iss} | ({rec.action.action_id} if rec else set()))
        if len(action_ids) > 1:
            problems.append(f"issue_action_mismatch:{step}")
        steps.append({"step": step, "status": status, "has_record": rec is not None, "action_ids": action_ids,
                      "blocking": blocking, "warnings": warnings, "codes": sorted({i.code for i in iss})})

    items = [{"issue_id": i.issue_id, "step": i.step, "action_id": i.action_id, "code": i.code,
              "severity": i.severity, "patient": i.patient_alias, "trial_id": i.trial_id,
              "criterion_key": i.criterion_key, "before": i.before, "after": i.after, "fact_ids": list(i.fact_ids),
              "detail": list(i.detail), "has_record": i.step in records} for i in diag.issues]
    blocked = [{"step": i.step, "action_id": i.action_id, "pair": f"{i.patient_alias}|{i.trial_id}",
                "before": i.before, "after": i.after, "code": i.code, "detail": list(i.detail)}
               for i in diag.issues if _is_blocked_pair_transition(i)]
    # withheld만 골라내고 나머지는 위반으로 둠: 모르는 코드를 '보류'로 가볍게 보이게 하지 않음
    violation_transitions = [b for b in blocked if b["code"] != WITHHELD_PAIR_CODE]
    withheld_transitions = [{**b, "withheld_by": _withheld_by(by_step[b["step"]])}
                            for b in blocked if b["code"] == WITHHELD_PAIR_CODE]
    n_block = sum(i.severity == "blocking" for i in diag.issues)
    if diag.file_status == "unreadable":
        status = "diagnostics_unreadable"
    elif diag.file_status == "not_scanned":  # 차단 진단이 있는지 모르므로 no_blocking_diagnostics로 두지 않음
        status = "diagnostics_not_scanned"
    elif n_block:
        status = "contract_violation"
    elif problems:
        status = "diagnostics_damaged"
    else:
        status = "no_blocking_diagnostics"
    view[VIEW_KEY] = {
        "file": rp.ISSUES_FILE, "file_status": diag.file_status, "problems": problems,
        "blocking": n_block, "warnings": len(diag.issues) - n_block,
        "by_code": dict(sorted(Counter(f"{i.severity}:{i.code}" for i in diag.issues).items())),
        "violation_steps": [s["step"] for s in steps if s["status"] in ("contract_violation", "inconsistent")],
        "steps": steps, "violation_transitions": violation_transitions, "withheld_transitions": withheld_transitions,
        "blocked_transitions": blocked, "items": items}
    view["audit_status"] = status
    return view


def inspect_run(run_dir: Path, scanner=None) -> dict:
    """정상 기록 + 진단 보기. scanner가 없으면 정상 기록만 보이고 진단 값은 내보내지 않음 (모듈 설명 참고)."""
    return build_diagnostics_view(*load_run_with_diagnostics(run_dir, scanner))


# ---------------------------------------------------------------------------
# HTML (audit.render_v2_html 결과에 진단 절을 덧붙임)
# ---------------------------------------------------------------------------

def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _split_transitions(d: dict) -> tuple[list[dict], list[dict]]:
    """(위반 전이, 보류 전이). 합본만 있는 이전 형식의 보기도 코드로 나눠 행이 사라지지 않게 함."""
    if "violation_transitions" in d:
        return d["violation_transitions"], d["withheld_transitions"]
    old = d.get("blocked_transitions", [])
    return [b for b in old if b["code"] != WITHHELD_PAIR_CODE], [b for b in old if b["code"] == WITHHELD_PAIR_CODE]


def render_diagnostics_html(view: dict) -> str:
    base = audit.render_v2_html(view)
    d = view.get(VIEW_KEY)
    if d is None:
        return base
    step_rows = "".join(
        f"<tr><td>{_e(s['step'])}</td><td>{_e(s['status'])}</td><td>{'있음' if s['has_record'] else '없음'}</td>"
        f"<td>{_e(s['blocking'])}</td><td>{_e(s['warnings'])}</td><td>{_e(', '.join(s['codes']))}</td></tr>"
        for s in d["steps"])
    violations, withheld = _split_transitions(d)
    violation_rows = "".join(
        f"<tr><td>{_e(b['step'])}</td><td>{_e(b['pair'].split('|')[1])}</td><td>{_e(b['before'])}→{_e(b['after'])}"
        f"</td><td>{_e(b['code'])}</td></tr>" for b in violations)
    withheld_rows = "".join(
        f"<tr><td>{_e(b['step'])}</td><td>{_e(b['pair'].split('|')[1])}</td><td>{_e(b['before'])}→{_e(b['after'])}"
        f"</td><td>{_e(', '.join(b['detail']))}</td><td>{_e(', '.join(b.get('withheld_by') or ['확인되지 않음']))}</td></tr>"
        for b in withheld)
    item_rows = "".join(
        f"<tr><td>{_e(i['step'])}</td><td>{_e(i['severity'])}</td><td>{_e(i['code'])}</td>"
        f"<td>{_e(i['trial_id'] or i['criterion_key'])}</td><td>{_e(i['before'])}→{_e(i['after'])}</td>"
        f"<td>{_e(', '.join(i['fact_ids']))}</td><td>{_e(', '.join(i['detail']))}</td></tr>" for i in d["items"])
    probs = "".join(f"<li>{_e(p)}</li>" for p in d["problems"]) or "<li>없음</li>"
    section = (
        f"<h2>진단 ({_e(d['file'])})</h2><p>감사 상태: <b>{_e(view['audit_status'])}</b> · 파일: {_e(d['file_status'])}"
        f" · 차단 {_e(d['blocking'])} · 경고 {_e(d['warnings'])}</p>"
        "<table><tr><th>단계</th><th>상태</th><th>정상 기록</th><th>차단</th><th>경고</th><th>진단 코드</th></tr>"
        f"{step_rows}</table>"
        "<h3>계약 위반 전이 (전이 자체가 계약을 어김, 정상 기록 없음)</h3>"
        "<table><tr><th>단계</th><th>시험</th><th>전→후</th><th>코드</th></tr>"
        f"{violation_rows or '<tr><td colspan=4>없음</td></tr>'}</table>"
        "<h3>보류된 전이 (전이 자체는 위반이 아니나 같은 단계의 다른 위반 때문에 정상 기록이 보류됨)</h3>"
        "<table><tr><th>단계</th><th>시험</th><th>전→후</th><th>보조 코드</th><th>보류 원인</th></tr>"
        f"{withheld_rows or '<tr><td colspan=5>없음</td></tr>'}</table>"
        "<h3>진단 항목</h3><table><tr><th>단계</th><th>심각도</th><th>코드</th><th>대상</th><th>전→후</th>"
        f"<th>사실 ID</th><th>보조 코드</th></tr>{item_rows}</table>"
        f"<h3>진단 파일 문제</h3><ul>{probs}</ul>")
    tail = "</body></html>"
    if not base.endswith(tail):  # render_v2_html 형식이 바뀌면 덧붙이지 않고 그대로 (조용히 깨지지 않게 테스트가 확인)
        return base
    return base[: -len(tail)] + section + tail


__all__ = ["DiagnosticsLoad", "load_diagnostics", "load_run_with_diagnostics", "build_diagnostics_view",
           "inspect_run", "render_diagnostics_html", "VIEW_KEY"]
