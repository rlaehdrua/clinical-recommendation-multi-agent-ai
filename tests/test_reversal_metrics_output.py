"""역전 지표 출력의 식별자: 파일과 기본 dict에는 원시 run_id·환자 별칭·시험 ID가 남지 않음 (합성 값)."""

from __future__ import annotations

import json

import pytest

import test_reversal_metrics as T
from ctrec.engine import audit
from ctrec.engine import audit_diagnostics as ad
from ctrec.engine import reversal_metrics as rm
from ctrec.engine.contracts import PairStatus

RUN, ALIAS, TRIAL = "RUN-SYNSECRET1", "PA-SYNSECRET2", "NCT-SYNSECRET3"


def summary() -> rm.ReversalSummary:
    e = rm.ReversalEvent(run_id=RUN, patient_alias=ALIAS, trial_id=TRIAL, before=PairStatus.CONFIRMED_ELIGIBLE,
                         after=PairStatus.CONFIRMED_INELIGIBLE, anchor_step=1, step=3, via=("VERIFICATION_PENDING",),
                         new_evidence=True, verified=True)
    return rm.ReversalSummary(runs=1, pairs_observed=1, confirmed_eligible_to_ineligible=1, events=[e])


def test_default_dict_has_no_raw_identifiers():
    d = rm.ReversalSummary.to_dict(summary())
    text = json.dumps(d, ensure_ascii=False, default=str)
    assert "SYNSECRET" not in text
    event = d["events"][0]
    assert not {"run_id", "patient_alias", "trial_id"} & set(event)
    assert event["direction"] == "eligible_to_ineligible" and event["step"] == 3 and event["path_kind"] == "verification_only"


def test_same_pair_keeps_the_same_reference():
    a, b = summary().to_dict()["events"][0], summary().to_dict()["events"][0]
    assert a["pair_ref"] == b["pair_ref"] and len(a["pair_ref"]) == 12


def test_identifiers_only_on_explicit_request():
    event = summary().to_dict(identifiers=True)["events"][0]
    assert (event["run_id"], event["patient_alias"], event["trial_id"]) == (RUN, ALIAS, TRIAL)


def test_metrics_file_has_no_raw_identifiers_or_folder_names(tmp_path):
    out = tmp_path / "metrics.json"
    rm.write_metrics(summary(), out, [f"{RUN}:no_v2_records", f"{RUN}:records_unreadable", "other:no_v2_records"])
    text = out.read_text(encoding="utf-8")
    assert "SYNSECRET" not in text
    assert json.loads(text)["load_problems"] == ["run-1:no_v2_records", "run-1:records_unreadable", "run-2:no_v2_records"]


def test_a_line_too_deep_to_parse_is_skipped(tmp_path):
    from ctrec.engine import audit
    lines = ['{"ok": 1}', "[" * 200000]
    (tmp_path / audit.RECORDS_FILE).write_text("\n".join(lines), encoding="utf-8")
    assert rm._raw_record_items(tmp_path) == [(1, {"ok": 1})]


# --- 값 검사 경로: inspect_run_checked가 뺀 기록이 지표로 돌아오지 않음 ---

class Finds:
    """합성 scanner: SECRET을 찾음."""

    def scan(self, text: str) -> list[str]:
        return ["original_id"] if SECRET in text else []


class Broken:
    def scan(self, text: str) -> list[str]:
        raise RuntimeError(f"입력 포함 {text}")


SECRET = "SYNSECRETVALUE"
CE, CI, VP = PairStatus.CONFIRMED_ELIGIBLE, PairStatus.CONFIRMED_INELIGIBLE, PairStatus.VERIFICATION_PENDING


def lines(statuses=(CE, VP, CI), secret_steps=()) -> list[dict]:
    out = [json.loads(r.model_dump_json()) for r in T._chain(list(statuses))]
    for rec in out:
        if rec["step"] in secret_steps:
            for p in rec["pairs"]:
                p["pair"]["trial_id"] = SECRET
    return out


def write_run(d, objs) -> None:
    d.mkdir()
    (d / audit.RECORDS_FILE).write_text("\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")


def test_a_record_the_scanner_withholds_stays_out_of_metrics(tmp_path):
    d = tmp_path / "run"
    objs = lines()
    write_run(d, lines(secret_steps={r["step"] for r in objs}))
    assert SECRET in str(rm.load_run_inputs(d)[0])  # 검사 없는 경로는 기존과 같음

    s, problems = rm.compute_for_runs([d], scanner=Finds())
    assert s.events == [] and s.confirmed_eligible_to_ineligible == 0 and s.pairs_observed == 0
    assert SECRET not in json.dumps(s.to_dict(identifiers=True), default=str)
    out = tmp_path / "m.json"
    rm.write_metrics(s, out, problems)
    text = out.read_text(encoding="utf-8")
    assert SECRET not in text and "sensitive_record:original_id" in text


def test_a_withheld_step_cannot_be_rescued_by_a_clean_duplicate_line(tmp_path):
    clean = lines()
    dirty = lines(secret_steps={clean[1]["step"]})[1]
    as_text = {**dirty, "step": str(dirty["step"])}  # 계약 검증이 숫자로 바꾸는 표기
    for order, name in (((dirty, clean[1]), "dirty_first"), ((clean[1], dirty), "clean_first"),
                        ((as_text, clean[1]), "dirty_first_as_text")):
        d = tmp_path / name
        write_run(d, [clean[0], *order, *clean[2:]])
        recs, _, problems = rm.load_run_inputs(d, scanner=Finds())
        assert clean[1]["step"] not in {r.step for r in recs}, name
        assert SECRET not in str(recs) and any("sensitive_record:original_id" in p for p in problems), name


def test_withholding_a_step_leaves_a_gap_not_a_confirmed_reversal(tmp_path):
    d = tmp_path / "run"
    statuses = (CE, VP, PairStatus.EXPERT_REVIEW, CI)
    clean = lines(statuses)
    write_run(d, [clean[0], {**clean[1], "policy_version": SECRET}, clean[2]])
    assert rm.compute_for_runs([d])[0].confirmed_eligible_to_ineligible == 1  # 검사 전에는 확인된 역전
    s, _ = rm.compute_for_runs([d], scanner=Finds())
    assert s.confirmed_eligible_to_ineligible == 0 and s.history_gaps == 1
    assert s.unverified_reversals == 1 and s.unverified_by_reason["missing_record"] == 1


def test_a_sensitive_diagnostic_line_is_dropped_and_reported(tmp_path):
    d = tmp_path / "run"
    write_run(d, lines())
    issue = T._direct_issue(2, CE, CI)
    bad = json.loads(issue.model_dump_json())
    bad["trial_id"] = SECRET
    T._write_issue_lines(d, [T._line(issue), json.dumps(bad).encode("utf-8")])
    _, issues, problems = rm.load_run_inputs(d, scanner=Finds())
    assert len(issues) == 1 and SECRET not in str(issues)
    assert "issues line 2: sensitive_text:original_id" in problems


def test_a_failing_scanner_is_a_read_failure_not_a_pass(tmp_path):
    d = tmp_path / "run"
    write_run(d, lines(secret_steps={1}))
    T._write_issue_lines(d, [T._line(T._direct_issue(2, CE, CI))])
    recs, issues, problems = rm.load_run_inputs(d, scanner=Broken())
    assert recs == [] and issues == []
    assert problems == ["records_unreadable", "issues_file_unreadable"] and SECRET not in str(problems)


def test_an_object_without_scan_is_refused_not_ignored(tmp_path):
    with pytest.raises(audit.AuditError):
        rm.compute_for_runs([tmp_path], scanner=object())


def test_clean_runs_compute_the_same_with_and_without_a_scanner(tmp_path):
    d = tmp_path / "run"
    write_run(d, lines())
    assert rm.compute_for_runs([d], scanner=Finds()) == rm.compute_for_runs([d])


# --- 파일로 나가는 문제 목록 ---

@pytest.mark.parametrize("folder", ["PA:SYNSECRET", "pa:synsecret", "line 1: corrupt_json:SYNSECRET"])
def test_folder_names_with_colons_do_not_survive(tmp_path, folder):
    out = tmp_path / "m.json"
    rm.write_metrics(rm.ReversalSummary(), out, [f"{folder}:no_v2_records", f"{folder}:line 2: duplicate_step:1"])
    assert json.loads(out.read_text(encoding="utf-8"))["load_problems"] == ["run-1:no_v2_records", "run-1:line 2: duplicate_step:1"]


@pytest.mark.parametrize("problem", ["SYNSECRET", "run:SYNSECRET", "run:line 1: invalid_record:SYNSECRET", "a:\nSYNSECRET"])
def test_unknown_problem_text_is_replaced_but_still_counted(tmp_path, problem):
    out = tmp_path / "m.json"
    rm.write_metrics(rm.ReversalSummary(), out, [problem])
    assert json.loads(out.read_text(encoding="utf-8"))["load_problems"] == [rm.UNRECOGNIZED_PROBLEM]


def test_every_problem_the_loader_can_make_survives_the_file_filter(tmp_path):
    damaged = tmp_path / "damaged"
    objs = lines()
    write_run(damaged, [objs[0], objs[0], {"schema_version": "x"}, *objs[1:]])
    with (damaged / audit.RECORDS_FILE).open("a", encoding="utf-8") as f:
        f.write("not json\n[1]\n")
    T._write_issue_lines(damaged, [b"bad", b"[1]", b"{}"])
    empty = tmp_path / "empty"
    empty.mkdir()
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / audit.RECORDS_FILE).write_bytes(b"\xff")
    trace_only = tmp_path / "trace_only"
    trace_only.mkdir()
    event = {"agent": audit.TRACE_AGENT, "event": audit.TRACE_EVENT, "record": objs[0]}
    (trace_only / "trace.jsonl").write_text(json.dumps(event) + "\n{\n", encoding="utf-8")
    s, problems = rm.compute_for_runs([damaged, empty, bad, trace_only], scanner=Finds())
    assert len(problems) >= 9
    out = tmp_path / "m.json"
    rm.write_metrics(s, out, problems)
    assert rm.UNRECOGNIZED_PROBLEM not in out.read_text(encoding="utf-8")


def test_repr_carries_no_identifiers():
    assert "SYNSECRET" not in repr(summary()) and "SYNSECRET" not in str(summary().events[0])


# --- 비정상 길이 입력 ---

@pytest.mark.parametrize("bad", [b'{"a": ' + b"9" * 6000 + b"}", b"[" * 200000], ids=["long_integer", "deep_nesting"])
def test_an_unparsable_record_line_costs_only_that_line(tmp_path, bad):
    good, broken, trace = tmp_path / "good", tmp_path / "broken", tmp_path / "trace"
    objs = lines()
    write_run(good, objs)
    write_run(broken, objs)
    with (broken / audit.RECORDS_FILE).open("ab") as f:
        f.write(bad + b"\n")
    trace.mkdir()
    events = [json.dumps({"agent": audit.TRACE_AGENT, "event": audit.TRACE_EVENT, "record": o}).encode("utf-8") for o in objs]
    (trace / "trace.jsonl").write_bytes(b"\n".join([events[0], bad, *events[1:]]) + b"\n")
    s, problems = rm.compute_for_runs([good, broken, trace])
    assert s.runs == 3 and s.confirmed_eligible_to_ineligible == 3
    assert f"broken:line {len(objs) + 1}: corrupt_json" in problems and "trace:trace line 2: corrupt_json" in problems


@pytest.mark.parametrize("bad", [b'{"a": ' + b"9" * 6000 + b"}", b"[" * 200000], ids=["long_integer", "deep_nesting"])
def test_an_unparsable_diagnostic_line_does_not_stop_the_checked_view(tmp_path, bad):
    d = tmp_path / "run"
    write_run(d, lines())
    T._write_issue_lines(d, [bad])
    assert rm.load_run_inputs(d)[2] == ["issues line 1: corrupt_json"]
    assert ad.load_diagnostics(d, None, Finds()).problems == ["line 1: corrupt_json"]


def test_a_huge_step_value_is_not_copied_into_output(tmp_path):
    d = tmp_path / "run"
    objs = lines()
    huge = {**objs[0], "step": 10 ** 4299}
    write_run(d, [*objs, huge, huge])
    s, problems = rm.compute_for_runs([d])
    assert all(e.step <= rm.MAX_STEP for e in s.events) and s.confirmed_eligible_to_ineligible == 1
    assert f"run:line {len(objs) + 2}: duplicate_step:<out_of_range>" in problems and "run:step_out_of_range:2" in problems
    out = tmp_path / "m.json"
    rm.write_metrics(s, out, problems)
    text = out.read_text(encoding="utf-8")
    assert len(text) < 3000 and rm.UNRECOGNIZED_PROBLEM not in text


def test_a_step_at_the_limit_is_still_ordinary_data(tmp_path):
    d = tmp_path / "run"
    write_run(d, [{**lines()[0], "step": rm.MAX_STEP}])
    recs, _, problems = rm.load_run_inputs(d)
    assert len(recs) == 1 and not any("range" in p for p in problems)


@pytest.mark.parametrize("field,value", [("run_id", "R" * 5000), ("x" * 2000, 1), ("step", float("inf")), ("step", 1e300)])
def test_oversized_field_values_stay_out_of_problems(tmp_path, field, value):
    d = tmp_path / "run"
    write_run(d, [{**lines()[0], field: value}])
    _, problems = rm.compute_for_runs([d])
    assert problems == ["run:line 1: invalid_record"]
