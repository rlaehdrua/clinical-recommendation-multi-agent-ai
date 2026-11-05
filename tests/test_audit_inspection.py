"""화면·API용 검사 조회 (engine.audit_inspection): 정상 기록과 진단에 같은 검사, scanner 없으면 값 없음.

audit_diagnostics(PR #5)가 있어야 실행됩니다. reversal_metrics(PR #3) 호환 검사도 그 모듈이 있을 때만 실행됩니다.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

import pytest

ad = pytest.importorskip("ctrec.engine.audit_diagnostics")

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import test_audit_read_security as sec  # noqa: E402
from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import offline_records as orc  # noqa: E402
from ctrec.engine import audit  # noqa: E402
from ctrec.engine import audit_inspection as ai  # noqa: E402
from ctrec.engine import record_producer as rp  # noqa: E402
from ctrec.eval_hygiene import AliasMap, LeakScanner, load_cases_hygienic  # noqa: E402
from ctrec.trace import Tracer  # noqa: E402

MARKS = sec.MARKS


def _no_marks(view: dict, page: str) -> None:
    leaked = [n for n, t in {"view": json.dumps(view, ensure_ascii=False), "html": page}.items()
              if any(m in t for m in MARKS)]
    assert leaked == []


def _write(run: Path, *records, issues=()):
    if records:
        (run / audit.RECORDS_FILE).write_text("".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8")
    if issues:
        (run / rp.ISSUES_FILE).write_text("".join(i + "\n" for i in issues), encoding="utf-8")


def _both(run: Path, scanner) -> tuple[dict, str]:
    view = ai.inspect_run_checked(run, scanner)
    return view, ai.render_inspection_html(view)


def test_scanner_is_required_and_has_no_default():
    assert inspect.signature(ai.inspect_run_checked).parameters["scanner"].default is inspect.Parameter.empty


@pytest.mark.parametrize("scanner", [None, object(), sec.RaisingScanner(), sec.FixedScanner(None)],
                         ids=["missing", "no_scan_method", "raises", "returns_none"])
def test_no_view_without_a_working_scanner(tmp_path, scanner):
    _write(tmp_path, sec._record(policy_version=sec.KO_MARK))
    with pytest.raises(audit.AuditError) as err:
        ai.inspect_run_checked(tmp_path, scanner)
    assert sec.KO_MARK not in str(err.value) and sec.KO_MARK not in repr(err.value) and err.value.__context__ is None


def test_scanner_failing_only_on_diagnostics_gives_no_view(tmp_path):
    class FailsOnIssue:
        def scan(self, text):
            if "transition_rejected" in text:
                raise RuntimeError(text)
            return []

    _write(tmp_path, sec._record(), issues=[sec._issue()])
    with pytest.raises(audit.AuditError) as err:
        ai.inspect_run_checked(tmp_path, FailsOnIssue())
    assert err.value.__context__ is None


def test_html_refuses_a_view_that_skipped_the_check(tmp_path):
    _write(tmp_path, sec._record())
    for unchecked in (audit.build_v2_view(audit.load_run_v2(tmp_path)), ad.inspect_run(tmp_path, LeakScanner())):
        with pytest.raises(audit.AuditError):
            ai.render_inspection_html(unchecked)


def test_same_policy_for_records_and_diagnostics(tmp_path):
    scanner = LeakScanner(literals={sec.ID_MARK})
    _write(tmp_path, sec._record(0), sec._record(1, policy_version=sec.ID_MARK),
           issues=[sec._issue(), sec._issue(issue_id="RUN-1:9:y", trial_id=sec.ID_MARK)])
    view, page = _both(tmp_path, scanner)
    assert [s["step"] for s in view["steps"]] == [0]
    assert view["issues"] == ["line 2: sensitive_record:case_id_shape,original_id"]
    assert view["diagnostics"]["problems"][0] == "line 2: sensitive_text:case_id_shape,original_id"
    assert view[ai.SCAN_KEY] == {"status": "withheld", "limit": ai.SCAN_LIMIT, "withheld_records": 1,
                                 "withheld_diagnostics": 1}
    assert [i["issue_id"] for i in view["diagnostics"]["items"]] == ["RUN-1:9:x"]
    _no_marks(view, page)


def test_trace_fallback_is_checked_too(tmp_path):
    audit.to_trace(Tracer(tmp_path / "trace.jsonl", verbose=False), sec._record(policy_version=sec.ID_MARK))
    view, page = _both(tmp_path, LeakScanner())
    assert view["steps"] == [] and view[ai.SCAN_KEY]["withheld_records"] == 1
    assert view["audit_status"] == ai.STATUS_WITHHELD
    _no_marks(view, page)


def test_unchecked_inspect_run_shows_what_the_checked_path_hides(tmp_path):
    """같은 입력에서 차이를 확인: inspect_run은 정상 기록 값을 검사하지 않으므로 화면 경로로 쓰면 안 됨."""
    _write(tmp_path, sec._record(policy_version=sec.ID_MARK))
    assert sec.ID_MARK in json.dumps(ad.inspect_run(tmp_path, LeakScanner()), ensure_ascii=False)
    assert sec.ID_MARK not in json.dumps(ai.inspect_run_checked(tmp_path, LeakScanner()), ensure_ascii=False)


def test_withheld_record_is_not_shown_as_a_clean_run(tmp_path):
    _write(tmp_path, sec._record(policy_version=sec.ID_MARK))
    view, page = _both(tmp_path, LeakScanner())
    assert view["audit_status"] == ai.STATUS_WITHHELD and view[ai.SCAN_KEY]["status"] == "withheld"
    assert ai.STATUS_WITHHELD in page and "제외한 기록 1" in page


def test_withheld_record_does_not_hide_a_blocking_status(tmp_path):
    _write(tmp_path, sec._record(policy_version=sec.ID_MARK), issues=[sec._issue()])
    view, _ = _both(tmp_path, LeakScanner())
    assert view["audit_status"] == "contract_violation" and view[ai.SCAN_KEY]["withheld_records"] == 1


def test_false_positive_in_a_normal_run_is_visible(tmp_path):
    _write(tmp_path, sec._record(policy_version="engine-cleanup"))
    view, _ = _both(tmp_path, LeakScanner())
    assert view["steps"] == [] and view["issues"] == ["line 1: sensitive_record:label_token"]
    assert view["audit_status"] == ai.STATUS_WITHHELD


def test_clean_run_keeps_the_existing_view_and_only_adds_the_scan_block(tmp_path):
    _write(tmp_path, sec._record())
    view, page = _both(tmp_path, LeakScanner())
    expected = ad.inspect_run(tmp_path, LeakScanner())
    assert view.pop(ai.SCAN_KEY) == {"status": "no_findings", "limit": ai.SCAN_LIMIT, "withheld_records": 0,
                                     "withheld_diagnostics": 0}
    assert view == expected and view["audit_status"] == "no_blocking_diagnostics"
    assert "안전을 보장하지 않습니다" in page and page.endswith("</body></html>")


def test_scanner_unaware_of_an_identifier_does_not_hide_it(tmp_path):
    """한계 고정: 검사 범위 표시가 있어도 scanner가 모르는 값은 보입니다."""
    _write(tmp_path, sec._record(policy_version=sec.KO_MARK))
    view = ai.inspect_run_checked(tmp_path, LeakScanner())
    assert view[ai.SCAN_KEY]["limit"] == ai.SCAN_LIMIT and sec.KO_MARK in json.dumps(view, ensure_ascii=False)


def test_module_respects_engine_isolation():
    src = Path(ai.__file__).read_text(encoding="utf-8")
    assert "eval_hygiene" not in re.sub(r'""".*?"""', "", src, flags=re.S)


def test_offline_run_passes_the_checked_view_unchanged(tmp_path):
    """오프라인 실행이 쓴 정상 기록은 같은 scanner의 조회 검사를 그대로 통과."""
    parsed, all_trials = load_frozen(), load_trials()
    case = "M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1"
    amap = AliasMap(salt="t")
    amap.alias(case)
    hcs, _, scanner = load_cases_hygienic(parsed, only=[case], alias_map=amap)
    result = orc.record_case(hcs[0], {t: all_trials[t] for t in parsed}, scanner, tmp_path)
    assert result.complete
    plain = audit.load_run_v2(tmp_path)
    view = ai.inspect_run_checked(tmp_path, scanner)
    assert plain.issues == [] and len(view["steps"]) == len(plain.records) > 0
    assert view[ai.SCAN_KEY]["status"] == "no_findings"


def test_reversal_metrics_code_parsing_still_fits():
    rm = pytest.importorskip("ctrec.engine.reversal_metrics")
    assert rm._VALUE_BEARING_ISSUE.sub(r"\1", "line 3: unsupported_schema:?") == "line 3: unsupported_schema"
    assert rm._VALUE_BEARING_ISSUE.sub(r"\1", "line 3: invalid_record:action.<extra>") == "line 3: invalid_record"
    assert rm._DUPLICATE_LINE.match("line 4: duplicate_step:0")


def test_diagnostics_withheld_alone_is_not_a_clean_status(tmp_path):
    _write(tmp_path, sec._record(), issues=[sec._issue(trial_id=sec.ID_MARK)])
    view, page = _both(tmp_path, LeakScanner())
    assert view["audit_status"] == "diagnostics_damaged" and view[ai.SCAN_KEY]["status"] == "withheld"
    _no_marks(view, page)


def test_all_records_withheld_is_not_an_empty_clean_run(tmp_path):
    _write(tmp_path, sec._record(0, policy_version=sec.ID_MARK), sec._record(1, policy_version=sec.ID_MARK))
    view, page = _both(tmp_path, LeakScanner())
    assert view["steps"] == [] and view[ai.SCAN_KEY]["withheld_records"] == 2
    assert view["audit_status"] == ai.STATUS_WITHHELD and "no_v2_records" not in view["issues"]
    _no_marks(view, page)


def test_withheld_record_in_a_reversal_warns_by_step_only(tmp_path):
    down = sec._record(1, policy_version=sec.ID_MARK, pair_before={"T1": "CONFIRMED_ELIGIBLE"},
                       pair_after={"T1": "VERIFICATION_PENDING"})
    _write(tmp_path, down)
    view, page = _both(tmp_path, LeakScanner())
    assert "unexplained_confirmed_downgrade:1" in view["issues"]
    _no_marks(view, page)


def test_reversal_metrics_run_on_safe_loader_messages(tmp_path):
    rm = pytest.importorskip("ctrec.engine.reversal_metrics")
    run = tmp_path / "run"
    run.mkdir()
    records = [sec._record(1, pair_before={"T1": "VERIFICATION_PENDING"}, pair_after={"T1": "CONFIRMED_ELIGIBLE"}),
               sec._record(2, pair_before={"T1": "CONFIRMED_ELIGIBLE"}, pair_after={"T1": "VERIFICATION_PENDING"},
                           pair_reasons={"T1": "contradicting_evidence"}),
               sec._record(3, pair_before={"T1": "VERIFICATION_PENDING"}, pair_after={"T1": "CONFIRMED_INELIGIBLE"})]
    lines = [r.model_dump_json() for r in records]
    lines.insert(1, json.dumps({**sec._raw(), sec.KO_MARK: 1}, ensure_ascii=False))
    lines.insert(2, json.dumps({"schema_version": sec.KO_MARK}, ensure_ascii=False))
    (run / audit.RECORDS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary, problems = rm.compute_for_runs([run])
    assert summary.confirmed_eligible_to_ineligible == 1
    assert problems == ["run:line 2: invalid_record", "run:line 3: unsupported_schema"]
    assert sec.KO_MARK not in json.dumps(summary.to_dict(), ensure_ascii=False) + str(problems)
