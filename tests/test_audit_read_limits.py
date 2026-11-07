"""감사 파일 읽기 상한: 큰 파일·손상 줄이 메모리와 문제 목록을 키우지 않고, 상한 안의 기록은 그대로 읽힘 (합성 값)."""

from __future__ import annotations

import json

import pytest

import test_reversal_metrics as T
from ctrec.engine import audit
from ctrec.engine import audit_diagnostics as ad
from ctrec.engine import audit_inspection as ai
from ctrec.engine import record_producer as rp
from ctrec.engine import reversal_metrics as rm
from ctrec.eval_hygiene import LeakScanner


@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setattr(audit, "MAX_READ_BYTES", 4096)
    monkeypatch.setattr(audit, "MAX_LINE_PROBLEMS", 20)


def good_run(d):
    T._write_records(d, T._chain([T.CE, T.VP, T.CI]))
    return d


def test_a_file_over_the_limit_is_refused_without_reading_it_all(tmp_path, small_limits):
    d = tmp_path / "run"
    d.mkdir()
    (d / audit.RECORDS_FILE).write_bytes(b"x" * 5000)
    with pytest.raises(audit.AuditError):
        audit.load_run_v2(d)
    assert rm.load_run_inputs(d)[2] == ["records_unreadable"]


def test_a_file_at_the_limit_is_read(tmp_path, small_limits):
    d = tmp_path / "run"
    d.mkdir()
    (d / audit.RECORDS_FILE).write_bytes(b"\n" * 4096)
    assert audit.load_run_v2(d).records == []


def test_too_many_damaged_lines_stop_the_read(tmp_path, small_limits):
    d = tmp_path / "run"
    d.mkdir()
    (d / audit.RECORDS_FILE).write_bytes(b"{bad\n" * 21)
    assert rm.load_run_inputs(d)[2] == ["records_unreadable"]
    (d / audit.RECORDS_FILE).write_bytes(b"{bad\n" * 20)
    assert len(rm.load_run_inputs(d)[2]) == 20


def test_too_many_damaged_trace_lines_stop_the_read(tmp_path, small_limits):
    d = tmp_path / "run"
    d.mkdir()
    (d / "trace.jsonl").write_bytes(b"{bad\n" * 21)
    assert rm.load_run_inputs(d)[2] == ["records_unreadable"]


def test_too_many_duplicate_lines_stop_the_read(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "MAX_LINE_PROBLEMS", 2)
    d = tmp_path / "run"
    T._write_records(d, T._chain([T.CE, T.VP])[:1] * 4)
    assert rm.load_run_inputs(d)[2] == ["records_unreadable"]


@pytest.mark.parametrize("raw,code", [(b"{bad\n" * 21, "issues_file_too_damaged"), (b"x" * 5000, "issues_file_too_large")])
def test_diagnostics_limits_keep_the_records_usable(tmp_path, small_limits, raw, code):
    d = good_run(tmp_path / "run")
    (d / rp.ISSUES_FILE).write_bytes(raw)
    records, issues, problems = rm.load_run_inputs(d)
    assert records and issues == [] and problems == [code]
    loaded = ad.load_diagnostics(d, None, LeakScanner())
    assert loaded.file_status == "unreadable" and loaded.problems == [code]


def test_limit_problems_reach_the_metrics_file(tmp_path, small_limits):
    d = good_run(tmp_path / "run")
    (d / rp.ISSUES_FILE).write_bytes(b"x" * 5000)
    s, problems = rm.compute_for_runs([d])
    out = tmp_path / "m.json"
    rm.write_metrics(s, out, problems)
    assert json.loads(out.read_text(encoding="utf-8"))["load_problems"] == ["run-1:issues_file_too_large"]


def test_one_oversized_run_does_not_stop_the_others(tmp_path, small_limits):
    good = good_run(tmp_path / "good")
    big = tmp_path / "big"
    big.mkdir()
    (big / audit.RECORDS_FILE).write_bytes(b"x" * 5000)
    s, problems = rm.compute_for_runs([good, big])
    assert s.confirmed_eligible_to_ineligible == 1 and problems == ["big:records_unreadable"]


def test_a_writer_will_not_append_to_an_oversized_file(tmp_path, small_limits):
    p = tmp_path / audit.RECORDS_FILE
    p.write_bytes(b"x" * 5000)
    with pytest.raises(audit.AuditError):
        audit.RecordWriter(p, "RUN-1")


def non_finite_cost_run(d, value):
    d.mkdir()
    obj = json.loads(T._chain([T.CE, T.VP])[0].model_dump_json())
    obj["outcome"]["cost_charged"]["coordinator_minutes"] = value
    (d / audit.RECORDS_FILE).write_text(json.dumps(obj) + "\n", encoding="utf-8")
    return d


def test_an_infinite_cost_is_a_lookup_failure_not_a_crash(tmp_path):
    d = non_finite_cost_run(tmp_path / "run", float("inf"))
    with pytest.raises(audit.AuditError) as e:
        ai.inspect_run_checked(d, LeakScanner())
    assert e.value.__context__ is None and e.value.__cause__ is None


def test_a_nan_cost_is_already_an_invalid_line(tmp_path):
    d = non_finite_cost_run(tmp_path / "run", float("nan"))
    assert ai.inspect_run_checked(d, LeakScanner())["steps"] == []
    assert rm.load_run_inputs(d)[2] == ["line 1: invalid_record"]


def test_default_limits_leave_real_sized_runs_alone(tmp_path):
    d = good_run(tmp_path / "run")
    assert audit.MAX_READ_BYTES >= 1024 ** 2 and audit.MAX_LINE_PROBLEMS >= 1000
    assert len(audit.load_run_v2(d).records) == 2 and rm.load_run_inputs(d)[2] == []
