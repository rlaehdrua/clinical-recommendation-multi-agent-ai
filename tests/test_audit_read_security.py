"""WP7 DecisionRecord 조회 경로의 오류 보고 보안: 문제 목록·예외에 파일 내용이 들어가지 않고 정상 기록은 그대로인지.

합성 표지값만 사용합니다. 예외 여부만 보지 않고 표지값이 최종 view와 HTML에 없는지 확인합니다.
scanner 조회와 to_trace_checked도 여기서 검사합니다. 진단까지 합친 화면용 경로는 test_audit_inspection.py.
"""

from __future__ import annotations

import inspect
import json
import traceback
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from ctrec.engine import audit
from ctrec.engine import contracts as C
from ctrec.engine import record_producer as rp
from ctrec.eval_hygiene import LeakScanner
from ctrec.trace import Tracer

ALIAS = "PA-" + "1" * 16
ASCII_MARK = "ZZSYNTHMARKER"
KO_MARK = "합성표지환자문장"
ID_MARK = "M-SYN-ZZSYNTHMARKER"
MARKS = (ASCII_MARK, KO_MARK, ID_MARK)
FILE_SOURCE, TRACE_SOURCE = "records_file", "trace_fallback"


def _record(step=0, **kw) -> C.DecisionRecord:
    action = C.Action(action_id=f"ACT-{step}", action_type="REQUEST_DOCUMENT", patient_alias=ALIAS,
                      source_id="lab_report", target_trial_ids=("T1",), target_criteria=("T1:I4",),
                      reason_code="unverified_self_report", tier=3, fingerprint="fp", policy_version="engine-0.1",
                      expected_cost=C.EffortCost(coordinator_minutes=10))
    outcome = C.ActionOutcome(action_id=f"ACT-{step}", status="succeeded", new_fact_ids=("F-aaa",),
                              cost_charged=C.EffortCost(coordinator_minutes=10), charge_keys=(f"{ALIAS}|lab_report",))
    base = dict(run_id="RUN-1", step=step, action=action, outcome=outcome, criteria_before={"T1:I4": "UNKNOWN"},
                criteria_after={"T1:I4": "MET"}, reevaluated=["T1:I4"], criterion_evidence={"T1:I4": ("F-aaa",)},
                pair_before={"T1": "VERIFICATION_PENDING"}, pair_after={"T1": "VERIFICATION_PENDING"},
                policy_version="engine-0.1", evaluator_version="rule_engine/1", fact_version_before="v1",
                fact_version_after="v2")
    return audit.make_record(**{**base, **kw})


def _raw(step=0) -> dict:
    return json.loads(_record(step).model_dump_json())


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _write_source(run: Path, source: str, objs: list) -> None:
    """같은 객체를 기록 파일 또는 trace 폴백 이벤트로 저장."""
    if source == FILE_SOURCE:
        (run / audit.RECORDS_FILE).write_text("\n".join(_dump(o) for o in objs) + "\n", encoding="utf-8")
    else:
        (run / "trace.jsonl").write_text("\n".join(_dump({"agent": audit.TRACE_AGENT, "event": audit.TRACE_EVENT,
                                                          "record": o}) for o in objs) + "\n", encoding="utf-8")


def _final_outputs(run: Path) -> tuple[audit.LoadResult, dict, str]:
    loaded = audit.load_run_v2(run)
    view = audit.build_v2_view(loaded)
    return loaded, view, audit.render_v2_html(view)


def _assert_no_marks(loaded: audit.LoadResult, view: dict, page: str) -> None:
    outputs = {"issues": _dump(loaded.issues), "view": _dump(view), "html": page}
    leaked = sorted(name for name, text in outputs.items() if any(m in text for m in MARKS))
    assert leaked == []  # 표지값이 아니라 출력 이름만 보고
    assert LeakScanner(literals=set(MARKS)).scan(outputs["issues"] + outputs["view"] + outputs["html"]) == []


both_sources = pytest.mark.parametrize("source", [FILE_SOURCE, TRACE_SOURCE])


class FixedScanner:
    def __init__(self, result):
        self.result = result

    def scan(self, text):
        return self.result


class RaisingScanner:
    def scan(self, text):
        raise RuntimeError(f"scanner failed on {text}")


@pytest.mark.parametrize("sink", ["issues", "view", "html"])
def test_output_check_detects_a_mark_in_each_output(sink):
    """표지값 검사 자체가 비어 있지 않은지 확인 (대조군)."""
    outputs = {"issues": [], "view": {}, "html": ""}
    outputs[sink] = {"issues": [ASCII_MARK], "view": {"k": ASCII_MARK}, "html": ASCII_MARK}[sink]
    with pytest.raises(AssertionError):
        _assert_no_marks(audit.LoadResult(issues=outputs["issues"]), outputs["view"], outputs["html"])


def test_marks_really_are_in_the_input_file(tmp_path):
    """입력에 표지값이 실제로 있어야 출력 검사가 의미를 가짐."""
    _write_source(tmp_path, FILE_SOURCE, [{**_raw(), "schema_version": ASCII_MARK, KO_MARK: 1}])
    raw = (tmp_path / audit.RECORDS_FILE).read_text(encoding="utf-8")
    assert ASCII_MARK in raw and KO_MARK in raw


# ---------------------------------------------------------------------------
# A. schema_version
# ---------------------------------------------------------------------------

@both_sources
@pytest.mark.parametrize("version", [
    ASCII_MARK, KO_MARK, ID_MARK, ASCII_MARK * 5,
    "ctrec.decision_record/v2" + ASCII_MARK,  # 알려진 접두사 뒤에 붙인 값
    ASCII_MARK + "ctrec.decision_record/v1",
    "ctrec.decision_record/v1\n" + ASCII_MARK,  # 줄바꿈 뒤에 숨긴 값
    "ctrec.decision_record/v1\n",
    "ctrec.decision_record/v" + "1" * 5,
    [ASCII_MARK], {ASCII_MARK: KO_MARK}, 7, None,
], ids=lambda v: f"{type(v).__name__}-{len(str(v))}")
def test_unexpected_schema_version_is_reported_without_its_value(tmp_path, source, version):
    _write_source(tmp_path, source, [{**_raw(), "schema_version": version}])
    loaded, view, page = _final_outputs(tmp_path)
    assert loaded.records == [] and loaded.issues[0] == "line 1: unsupported_schema:?"
    assert view["issues"] == loaded.issues and view["steps"] == []
    assert "unsupported_schema:?" in page
    _assert_no_marks(loaded, view, page)


def test_other_version_of_the_same_schema_stays_readable_in_the_report(tmp_path):
    _write_source(tmp_path, FILE_SOURCE, [{**_raw(), "schema_version": "ctrec.decision_record/v1"}])
    assert audit.load_run_v2(tmp_path).issues == ["line 1: unsupported_schema:ctrec.decision_record/v1"]


# ---------------------------------------------------------------------------
# B. 알 수 없는 JSON 키
# ---------------------------------------------------------------------------

def _extra_top(d, key):
    d[key] = 1


def _extra_action(d, key):
    d["action"][key] = 1


def _extra_outcome_cost(d, key):
    d["outcome"]["cost_charged"][key] = 1


def _extra_list_item(d, key):
    d["criteria"][0][key] = 1


def _extra_pair(d, key):
    d["pairs"][0]["pair"][key] = 1


@both_sources
@pytest.mark.parametrize("inject, field_path", [
    (_extra_top, "<extra>"), (_extra_action, "action.<extra>"),
    (_extra_outcome_cost, "outcome.cost_charged.<extra>"), (_extra_list_item, "criteria.0.<extra>"),
    (_extra_pair, "pairs.0.pair.<extra>"),
], ids=["top", "action", "nested", "list_item", "deep"])
@pytest.mark.parametrize("key", [ASCII_MARK, KO_MARK, ID_MARK, f"<b>{ASCII_MARK}</b>", f"a,b.{ASCII_MARK}"],
                         ids=["ascii", "ko", "id_shape", "markup", "separators"])
def test_unknown_key_keeps_its_location_and_drops_its_name(tmp_path, source, inject, field_path, key):
    bad = _raw()
    inject(bad, key)
    _write_source(tmp_path, source, [bad])
    loaded, view, page = _final_outputs(tmp_path)
    no_records = ["no_v2_records"] if source == TRACE_SOURCE else []
    assert loaded.records == [] and loaded.issues == [f"line 1: invalid_record:{field_path}", *no_records]
    _assert_no_marks(loaded, view, page)


def test_key_named_like_a_contract_field_is_still_reported_as_extra(tmp_path):
    bad = _raw()
    bad["action"]["run_id"] = "x"  # run_id는 DecisionRecord에만 있음
    _write_source(tmp_path, FILE_SOURCE, [bad])
    assert audit.load_run_v2(tmp_path).issues == ["line 1: invalid_record:action.<extra>"]


def test_many_unknown_keys_collapse_to_one_location(tmp_path):
    bad = {**_raw(), **{f"{ASCII_MARK}{i}": i for i in range(50)}}
    _write_source(tmp_path, FILE_SOURCE, [bad])
    assert audit.load_run_v2(tmp_path).issues == ["line 1: invalid_record:<extra>"]


def test_only_contract_names_pass_the_field_allowlist():
    """dict 필드가 계약에 생겨도 키가 그대로 나가지 않게 함: 허용 목록이 아닌 경로 조각은 표시하지 않음."""
    class Inner(BaseModel):
        step: int

    class WithDict(BaseModel):
        action: dict[str, Inner]

    with pytest.raises(ValidationError) as err:
        WithDict.model_validate({"action": {ASCII_MARK: {"step": "x"}}})
    assert audit._issue_fields(err.value) == "action.<unknown>.step"


def test_field_allowlist_covers_nested_models_only_from_the_contract():
    names = audit._RECORD_FIELDS
    assert {"run_id", "action_id", "coordinator_minutes", "patient_alias", "criterion_key", "pending_kinds"} <= names
    assert ASCII_MARK not in names


# ---------------------------------------------------------------------------
# 진단 정보 유지
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mutate, expected", [
    (lambda d: d.update(run_id=KO_MARK), "run_id"),
    (lambda d: d.pop("policy_version"), "policy_version"),
    (lambda d: d["action"].update(tier=99), "action.tier"),
    (lambda d: d["pairs"][0]["pair"].update(trial_id=KO_MARK), "pairs.0.pair.trial_id"),
    (lambda d: d["criteria"][0].update(reason_code=KO_MARK), "criteria.0.reason_code"),
    (lambda d: d.update(contract_version=KO_MARK), "<root>"),  # 모델 전체 검사 실패
], ids=["field", "missing", "nested", "deep", "list_item", "root"])
def test_invalid_values_keep_their_field_path_without_the_value(tmp_path, mutate, expected):
    bad = _raw()
    mutate(bad)
    _write_source(tmp_path, FILE_SOURCE, [bad])
    loaded, view, page = _final_outputs(tmp_path)
    assert loaded.issues == [f"line 1: invalid_record:{expected}"]
    _assert_no_marks(loaded, view, page)


def test_union_member_label_is_not_a_contract_name_and_is_hidden(tmp_path):
    bad = _raw()
    bad["criteria"][0]["before"] = KO_MARK
    _write_source(tmp_path, FILE_SOURCE, [bad])
    assert audit.load_run_v2(tmp_path).issues == ["line 1: invalid_record:criteria.0.before.<unknown>"]


# ---------------------------------------------------------------------------
# C. 손상된 줄
# ---------------------------------------------------------------------------

DAMAGED_LINES = {
    "broken_json": "{" + KO_MARK + ASCII_MARK,
    "plain_text": f"{KO_MARK} {ID_MARK}",
    "deep_nesting": "[" * 200000 + ASCII_MARK,
    "huge_integer": '{"step":' + "1" * 6000 + "}",
}


@pytest.mark.parametrize("name", DAMAGED_LINES)
def test_damaged_record_line_stops_at_that_line(tmp_path, name):
    good0, good2 = _record(0), _record(2)
    (tmp_path / audit.RECORDS_FILE).write_text(
        "\n".join([good0.model_dump_json(), DAMAGED_LINES[name], good2.model_dump_json()]) + "\n", encoding="utf-8")
    loaded, view, page = _final_outputs(tmp_path)
    assert loaded.records == [good0, good2] and loaded.issues == ["line 2: corrupt_json"]
    _assert_no_marks(loaded, view, page)


@pytest.mark.parametrize("name", DAMAGED_LINES)
def test_damaged_trace_line_stops_at_that_line(tmp_path, name):
    event = _dump({"agent": audit.TRACE_AGENT, "event": audit.TRACE_EVENT, "record": _raw()})
    (tmp_path / "trace.jsonl").write_text(f"{DAMAGED_LINES[name]}\n{event}\n", encoding="utf-8")
    loaded, view, page = _final_outputs(tmp_path)
    assert len(loaded.records) == 1 and loaded.issues == ["trace line 1: corrupt_json"]
    _assert_no_marks(loaded, view, page)


@pytest.mark.parametrize("line", [_dump(KO_MARK), _dump([ID_MARK]), "7"], ids=["string", "list", "number"])
def test_non_object_line_reports_only_its_kind(tmp_path, line):
    (tmp_path / audit.RECORDS_FILE).write_text(line + "\n", encoding="utf-8")
    loaded, view, page = _final_outputs(tmp_path)
    assert loaded.issues == ["line 1: not_object"]
    _assert_no_marks(loaded, view, page)


@pytest.mark.parametrize("name", [audit.RECORDS_FILE, "trace.jsonl"])
def test_non_utf8_file_error_carries_no_file_content(tmp_path, name):
    (tmp_path / name).write_bytes(f"{KO_MARK} {ASCII_MARK}".encode("utf-8") + b"\xff\n")
    with pytest.raises(audit.AuditError) as err:
        audit.load_run_v2(tmp_path)
    e = err.value
    rendered = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    assert not any(m in text for m in MARKS for text in (str(e), repr(e), rendered))
    assert e.__context__ is None and e.__cause__ is None  # UnicodeDecodeError.object에 파일 전체가 있음
    assert isinstance(e, ValueError)  # 이전 UnicodeDecodeError를 ValueError로 잡던 호출자 호환


def test_writer_refuses_a_non_utf8_records_file_and_leaves_it(tmp_path):
    p = tmp_path / audit.RECORDS_FILE
    data = KO_MARK.encode("utf-8") + b"\xff\n"
    p.write_bytes(data)
    with pytest.raises(audit.AuditError):
        audit.RecordWriter(p, "RUN-1")
    assert p.read_bytes() == data


# ---------------------------------------------------------------------------
# 정상 기록 호환
# ---------------------------------------------------------------------------

def test_normal_records_load_unchanged_from_both_sources(tmp_path):
    records = [_record(0), _record(1)]
    a, b = tmp_path / "file", tmp_path / "trace"
    a.mkdir()
    b.mkdir()
    writer = audit.RecordWriter(a / audit.RECORDS_FILE, "RUN-1")
    tracer = Tracer(b / "trace.jsonl", verbose=False)
    for r in records:
        writer.write(r)
        audit.to_trace(tracer, r)
    for run in (a, b):
        loaded, view, page = _final_outputs(run)
        assert loaded.issues == [] and loaded.records == records
        assert view["issues"] == [] and "<li>없음</li>" in page
    assert _final_outputs(a)[1:] == _final_outputs(b)[1:]


def test_inspector_v2_view_and_html_shape_is_unchanged(tmp_path):
    audit.RecordWriter(tmp_path / audit.RECORDS_FILE, "RUN-1").write(_record())
    _, view, page = _final_outputs(tmp_path)
    assert list(view) == ["schema", "run_ids", "steps", "patients", "pair_history", "fact_origin", "issues"]
    assert view["schema"] == audit.VIEW_SCHEMA and view["run_ids"] == ["RUN-1"]
    assert view["steps"][0]["versions"] == {"policy": "engine-0.1", "evaluator": "rule_engine/1", "fact_before": "v1",
                                            "fact_after": "v2", "contract": C.CONTRACT_VERSION}
    assert view["patients"][ALIAS]["coordinator_minutes"] == 10.0 and view["fact_origin"] == {"F-aaa": "ACT-0"}
    assert audit.chain(view, "ACT-0")["criteria_changed"] == ["T1:I4"]
    assert "<h2>기록 문제</h2>" in page and page.endswith("</body></html>") and "ACT-0" in page


def test_crlf_and_blank_lines_keep_line_numbers(tmp_path):
    body = f"{_record(0).model_dump_json()}\r\n\r\n{{bad\r\n{_record(1).model_dump_json()}\r\n"
    (tmp_path / audit.RECORDS_FILE).write_bytes(body.encode("utf-8"))
    loaded = audit.load_run_v2(tmp_path)
    assert [r.step for r in loaded.records] == [0, 1] and loaded.issues == ["line 3: corrupt_json"]


def test_good_record_next_to_bad_ones_survives_and_issue_order_is_kept(tmp_path):
    good = _record(0)
    (tmp_path / audit.RECORDS_FILE).write_text("\n".join([
        _dump({"schema_version": ASCII_MARK}), good.model_dump_json(), "{bad", _dump({**_raw(1), KO_MARK: 1})]),
        encoding="utf-8")
    loaded, view, page = _final_outputs(tmp_path)
    assert loaded.records == [good]
    assert loaded.issues == ["line 3: corrupt_json", "line 1: unsupported_schema:?", "line 4: invalid_record:<extra>"]
    assert view["run_ids"] == ["RUN-1"] and len(view["steps"]) == 1
    _assert_no_marks(loaded, view, page)


def test_public_read_api_signatures_are_unchanged():
    def sig(f):
        return list(inspect.signature(f).parameters)

    assert sig(audit.to_trace) == ["tracer", "record"]
    # scanner는 기본값 None인 추가 인자이고, 기존 호출 형태는 그대로
    assert sig(audit.load_run_v2) == ["run_dir", "scanner"] and sig(audit.load_records) == ["path", "scanner"]
    assert sig(audit.parse_records) == ["items", "scanner"] and sig(audit.records_from_trace) == ["trace", "scanner"]
    for f in (audit.load_run_v2, audit.load_records, audit.parse_records, audit.records_from_trace):
        assert inspect.signature(f).parameters["scanner"].default is None
    assert sig(audit.build_v2_view) == ["loaded"] and sig(audit.render_v2_html) == ["view"]
    assert list(audit.LoadResult.__dataclass_fields__) == ["records", "issues"]


# ---------------------------------------------------------------------------
# 정상 기록과 진단 구분
# ---------------------------------------------------------------------------

def _issue(**kw) -> str:
    base = dict(issue_id="RUN-1:9:x", run_id="RUN-1", step=9, action_id="ACT-9", code="transition_rejected",
                severity="blocking", patient_alias=ALIAS, trial_id="T9", before="INFO_MISSING", after="MET")
    return rp.RecordIssue(**{**base, **kw}).model_dump_json()


def test_diagnostics_file_does_not_enter_records_or_load_issues(tmp_path):
    audit.RecordWriter(tmp_path / audit.RECORDS_FILE, "RUN-1").write(_record())
    (tmp_path / rp.ISSUES_FILE).write_text(_issue(trial_id=ID_MARK) + "\n{" + KO_MARK + "\n", encoding="utf-8")
    loaded, view, page = _final_outputs(tmp_path)
    assert [r.step for r in loaded.records] == [0] and loaded.issues == []  # 진단은 '기록 파일 문제'가 아님
    assert [s["step"] for s in view["steps"]] == [0]
    _assert_no_marks(loaded, view, page)


def test_diagnostics_file_is_not_a_record_source_when_records_are_absent(tmp_path):
    event = _dump({"agent": audit.TRACE_AGENT, "event": audit.TRACE_EVENT, "record": _raw()})  # 폴백이면 읽힐 모양
    (tmp_path / rp.ISSUES_FILE).write_text(f"{_issue()}\n{event}\n", encoding="utf-8")
    loaded = audit.load_run_v2(tmp_path)
    assert loaded.records == [] and loaded.issues == ["no_v2_records"]


def test_diagnostic_line_read_as_a_record_is_rejected_without_values(tmp_path):
    p = tmp_path / "wrong.jsonl"
    p.write_text(_issue(trial_id=ID_MARK) + "\n", encoding="utf-8")
    loaded = audit.load_records(p)
    assert loaded.records == [] and loaded.issues == ["line 1: unsupported_schema:?"]


def test_diagnostic_line_with_a_valid_schema_cannot_pass_as_a_record(tmp_path):
    p = tmp_path / "wrong.jsonl"
    forged = {**json.loads(_issue(trial_id=ID_MARK)), "schema_version": C.DECISION_RECORD_SCHEMA}
    p.write_text(_dump(forged) + "\n", encoding="utf-8")
    loaded = audit.load_records(p)
    assert loaded.records == [] and loaded.issues[0].startswith("line 1: invalid_record:")
    assert not any(m in loaded.issues[0] for m in MARKS)


# ---------------------------------------------------------------------------
# 쓰기 검사 유지
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw, scanner, forbidden, kind", [
    ({"pair_after": {ID_MARK: "VERIFICATION_PENDING"}}, None, (), "case_id_shape"),
    ({"policy_version": ASCII_MARK}, LeakScanner(literals={ASCII_MARK}), (), "original_id"),
    ({"policy_version": KO_MARK}, None, (KO_MARK,), "forbidden_text"),
], ids=["id_shape", "literal", "forbidden_text"])
def test_writer_checks_still_refuse_without_quoting_the_value(tmp_path, kw, scanner, forbidden, kind):
    p = tmp_path / audit.RECORDS_FILE
    w = audit.RecordWriter(p, "RUN-1", forbidden_texts=forbidden, scanner=scanner)
    with pytest.raises(audit.AuditError) as err:
        w.write(_record(**kw))
    assert kind in str(err.value) and not any(m in str(err.value) for m in MARKS) and not p.exists()


@pytest.mark.parametrize("line", [_dump({"schema_version": ASCII_MARK}), "{bad", _dump({**_raw(), KO_MARK: 1})],
                         ids=["schema", "corrupt", "extra_key"])
def test_writer_does_not_append_to_a_file_with_problems(tmp_path, line):
    p = tmp_path / audit.RECORDS_FILE
    p.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(audit.AuditError) as err:
        audit.RecordWriter(p, "RUN-1")
    assert not any(m in str(err.value) for m in MARKS)
    assert p.read_text(encoding="utf-8") == line + "\n"


def test_trace_event_shape_is_unchanged(tmp_path):
    audit.to_trace(Tracer(tmp_path / "trace.jsonl", verbose=False), _record())
    event = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8"))
    assert (event["agent"], event["event"]) == (audit.TRACE_AGENT, audit.TRACE_EVENT)
    assert event["record"] == json.loads(_record().model_dump_json())


# ---------------------------------------------------------------------------
# to_trace_checked
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw, scanner, forbidden, kind", [
    ({"pair_after": {ID_MARK: "VERIFICATION_PENDING"}}, None, (), "case_id_shape"),
    ({"policy_version": ASCII_MARK}, LeakScanner(literals={ASCII_MARK}), (), "original_id"),
    ({"policy_version": KO_MARK}, None, (KO_MARK,), "forbidden_text"),
], ids=["id_shape", "literal", "forbidden_text"])
def test_checked_trace_refuses_and_writes_nothing(tmp_path, kw, scanner, forbidden, kind):
    path = tmp_path / "trace.jsonl"
    tracer = Tracer(path, verbose=False)
    with pytest.raises(audit.AuditError) as err:
        audit.to_trace_checked(tracer, _record(**kw), forbidden, scanner)
    assert kind in str(err.value) and not any(m in str(err.value) for m in MARKS)
    assert path.read_text(encoding="utf-8") == ""  # Tracer가 빈 파일은 만듦
    assert audit.load_run_v2(tmp_path).issues == ["no_v2_records"]


def test_checked_trace_output_matches_to_trace(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    record = _record()
    audit.to_trace(Tracer(a, verbose=False), record)
    assert audit.to_trace_checked(Tracer(b, verbose=False), record, scanner=LeakScanner()) is None

    def event(p):
        e = json.loads(p.read_text(encoding="utf-8"))
        e.pop("t")
        return e

    assert event(a) == event(b)
    (tmp_path / "trace.jsonl").write_bytes(b.read_bytes())
    assert audit.load_run_v2(tmp_path, scanner=LeakScanner()) == audit.LoadResult(records=[record])


@pytest.mark.parametrize("scanner", [RaisingScanner(), FixedScanner(None), FixedScanner("original_id"),
                                     FixedScanner([1]), object()],
                         ids=["raises", "none", "not_list", "not_text", "no_scan_method"])
def test_checked_trace_scanner_failure_is_not_success(tmp_path, scanner):
    path = tmp_path / "trace.jsonl"
    with pytest.raises(audit.AuditError) as err:
        audit.to_trace_checked(Tracer(path, verbose=False), _record(policy_version=KO_MARK), scanner=scanner)
    assert KO_MARK not in str(err.value) and KO_MARK not in repr(err.value) and err.value.__context__ is None
    assert path.read_text(encoding="utf-8") == ""  # Tracer가 빈 파일은 만듦


def test_checked_trace_does_not_quote_a_value_returned_by_the_scanner(tmp_path):
    with pytest.raises(audit.AuditError) as err:
        audit.to_trace_checked(Tracer(tmp_path / "t.jsonl", verbose=False), _record(), scanner=FixedScanner([KO_MARK]))
    assert str(err.value).endswith(audit.SCAN_OTHER) and KO_MARK not in str(err.value)


def test_unchecked_to_trace_still_writes_without_checking(tmp_path):
    """to_trace는 호환을 위해 그대로: 검사가 없으므로 새 코드에서 쓰면 안 되는 이유를 고정."""
    audit.to_trace(Tracer(tmp_path / "trace.jsonl", verbose=False), _record(policy_version=KO_MARK))
    assert KO_MARK in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "to_trace_checked" in audit.to_trace.__doc__


# ---------------------------------------------------------------------------
# load_run_v2(scanner=...)
# ---------------------------------------------------------------------------

@both_sources
def test_scanner_withholds_the_record_and_its_values(tmp_path, source):
    good = _record(0)
    bad = _record(1, policy_version=ID_MARK)
    _write_source(tmp_path, source, [json.loads(bad.model_dump_json()), json.loads(good.model_dump_json())])
    assert ID_MARK in _dump(audit.build_v2_view(audit.load_run_v2(tmp_path)))  # 검사 없으면 보임
    loaded = audit.load_run_v2(tmp_path, scanner=LeakScanner())
    view = audit.build_v2_view(loaded)
    assert loaded.records == [good] and loaded.issues == ["line 1: sensitive_record:case_id_shape"]
    _assert_no_marks(loaded, view, audit.render_v2_html(view))


@both_sources
@pytest.mark.parametrize("field", ["policy_version", "evaluator_version", "fact_version_after"])
def test_literal_scanner_finds_values_in_unpatterned_fields(tmp_path, source, field):
    _write_source(tmp_path, source, [json.loads(_record(**{field: KO_MARK}).model_dump_json())])
    loaded = audit.load_run_v2(tmp_path, scanner=LeakScanner(literals={KO_MARK}))
    view = audit.build_v2_view(loaded)
    assert loaded.records == [] and loaded.issues == ["line 1: sensitive_record:original_id"]
    _assert_no_marks(loaded, view, audit.render_v2_html(view))


def test_all_records_withheld_is_not_reported_as_missing(tmp_path):
    _write_source(tmp_path, TRACE_SOURCE, [json.loads(_record(policy_version=ID_MARK).model_dump_json())])
    assert "no_v2_records" not in audit.load_run_v2(tmp_path, scanner=LeakScanner()).issues


@pytest.mark.parametrize("scanner", [RaisingScanner(), FixedScanner(None), FixedScanner({"a": 1}), object()],
                         ids=["raises", "none", "not_list", "no_scan_method"])
def test_scanner_failure_stops_the_load_without_values(tmp_path, scanner):
    _write_source(tmp_path, FILE_SOURCE, [json.loads(_record(policy_version=KO_MARK).model_dump_json())])
    with pytest.raises(audit.AuditError) as err:
        audit.load_run_v2(tmp_path, scanner=scanner)
    rendered = "".join(traceback.format_exception(type(err.value), err.value, err.value.__traceback__))
    assert KO_MARK not in rendered and err.value.__context__ is None


def test_invalid_scanner_is_refused_even_without_records(tmp_path):
    with pytest.raises(audit.AuditError):
        audit.load_run_v2(tmp_path, scanner=object())


def test_unknown_kind_from_a_scanner_is_not_quoted(tmp_path):
    _write_source(tmp_path, FILE_SOURCE, [_raw()])
    loaded = audit.load_run_v2(tmp_path, scanner=FixedScanner([ASCII_MARK, "label_token"]))
    assert loaded.issues == ["line 1: sensitive_record:label_token,other"]


def test_false_positive_is_withheld_and_reported_not_hidden(tmp_path):
    """오탐: 정상 값이어도 값을 내보내지 않고 건수가 남음 (운영자가 scanner 설정을 고침)."""
    _write_source(tmp_path, FILE_SOURCE, [json.loads(_record(policy_version="engine-cleanup").model_dump_json())])
    loaded = audit.load_run_v2(tmp_path, scanner=LeakScanner())
    assert loaded.records == [] and loaded.issues == ["line 1: sensitive_record:label_token"]
    assert audit.count_withheld(loaded.issues) == 1


def test_withheld_record_still_counts_for_integrity_checks(tmp_path):
    down = _record(1, policy_version=ID_MARK, pair_before={"T1": "CONFIRMED_ELIGIBLE"},
                   pair_after={"T1": "VERIFICATION_PENDING"})
    other = _record(2).model_copy(update={"run_id": "RUN-2"})
    _write_source(tmp_path, FILE_SOURCE, [json.loads(r.model_dump_json()) for r in (down, other)])
    loaded = audit.load_run_v2(tmp_path, scanner=LeakScanner())
    assert [r.step for r in loaded.records] == [2]
    assert "multiple_run_ids" in loaded.issues and "unexplained_confirmed_downgrade:1" in loaded.issues


def test_scanner_that_knows_nothing_passes_free_text(tmp_path):
    """한계 고정: scanner가 모르는 값은 통과합니다. '안전'이라고 주장하면 안 되는 이유."""
    _write_source(tmp_path, FILE_SOURCE, [json.loads(_record(policy_version=KO_MARK).model_dump_json())])
    loaded = audit.load_run_v2(tmp_path, scanner=LeakScanner())
    assert len(loaded.records) == 1 and KO_MARK in _dump(audit.build_v2_view(loaded))


def test_normal_load_is_identical_with_and_without_a_scanner(tmp_path):
    audit.RecordWriter(tmp_path / audit.RECORDS_FILE, "RUN-1").write(_record())
    assert audit.load_run_v2(tmp_path, scanner=LeakScanner()) == audit.load_run_v2(tmp_path)


def test_loader_issues_for_extra_key_and_schema_keep_only_safe_details(tmp_path):
    """PR #3 test_record_load_problems_do_not_carry_file_values와 같은 합성 입력: 값은 없고 줄 번호·코드는 남음."""
    extra = {**_raw(), KO_MARK: 1}
    _write_source(tmp_path, FILE_SOURCE, [extra, {"schema_version": KO_MARK}])
    loaded, view, page = _final_outputs(tmp_path)
    assert KO_MARK not in str(loaded.issues)
    assert loaded.issues == ["line 1: invalid_record:<extra>", "line 2: unsupported_schema:?"]
    _assert_no_marks(loaded, view, page)


# --- 쓰기 검사: 기록은 JSON 문자열이므로 금지 문자열도 이스케이프된 모양까지 찾음 ---

@pytest.mark.parametrize("text", ['pain "sharp" reported', "first line\nsecond line", "tab\there", "back\\slash", "한글 \"인용\" 문장"])
def test_forbidden_text_is_found_after_json_escaping(tmp_path, text):
    from ctrec.engine import contracts as c
    from test_reversal_metrics import _chain
    rec = _chain([c.PairStatus.CONFIRMED_ELIGIBLE, c.PairStatus.VERIFICATION_PENDING])[0]
    rec = rec.model_copy(update={"facts_added": (text,)})
    assert audit.sensitive_findings(rec, [text]) == ["forbidden_text"]
    writer = audit.RecordWriter(tmp_path / audit.RECORDS_FILE, rec.run_id, forbidden_texts=[text])
    with pytest.raises(audit.AuditError):
        writer.write(rec)
    assert not (tmp_path / audit.RECORDS_FILE).exists()


def test_forbidden_text_shorter_than_the_limit_is_still_ignored():
    from ctrec.engine import contracts as c
    from test_reversal_metrics import _chain
    rec = _chain([c.PairStatus.CONFIRMED_ELIGIBLE, c.PairStatus.VERIFICATION_PENDING])[0]
    assert audit.sensitive_findings(rec.model_copy(update={"facts_added": ('"a"',)}), ['"a"']) == []
