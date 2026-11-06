"""보안 적용 경로: 자유 서술 탐지, 내보내기 전용 지표, 실제 실행기 연결, CLI 콘솔 출력 (합성 값만, 값 자체를 출력하지 않음)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import test_reversal_metrics as T
from ctrec.engine import audit
from ctrec.engine import audit_inspection as ai
from ctrec.engine import reversal_metrics as rm
from ctrec.eval_hygiene import LeakScanner

ROOT = Path(__file__).resolve().parents[1]
FREE = "환자는 어제 흉통을 호소함"
ENGLISH = "patient reports chest pain"


def record(step=1, **update):
    return T._chain([T.CE, T.VP], start=step)[0].model_copy(update=update)


def run_dir(d: Path, records) -> Path:
    T._write_records(d, records)
    return d


# --- 자유 서술 ---

@pytest.mark.parametrize("field", ["facts_added", "policy_version", "evaluator_version", "fact_version_before", "fact_version_after"])
@pytest.mark.parametrize("text", [FREE, ENGLISH], ids=["korean", "english"])
def test_free_text_in_unformatted_fields_is_found(field, text):
    value = (text,) if field == "facts_added" else text
    assert audit.has_free_text(record(**{field: value}))


@pytest.mark.parametrize("value", ["F-1a2b3c", "rule_engine/1", "V-12", "legacy_decision_state:llm", "PA-0123456789abcdef|lab_report",
                                   "biomarker:EGFR", "0.2-provisional"])
def test_real_value_shapes_are_not_free_text(value):
    assert not audit.has_free_text(record(policy_version=value))


def test_a_spaced_criterion_field_name_is_allowed_only_in_required_fields():
    action = record().action
    ok = action.model_copy(update={"required_fields": ("lab:corrected calcium",)})
    assert not audit.has_free_text(record(action=ok))
    assert audit.has_free_text(record(policy_version="lab:corrected calcium"))
    long_text = action.model_copy(update={"required_fields": ("one two three four five six",)})
    assert audit.has_free_text(record(action=long_text))


def test_an_overlong_code_is_free_text():
    assert audit.has_free_text(record(policy_version="x" * 257)) and not audit.has_free_text(record(policy_version="x" * 256))


def test_a_spaceless_ascii_note_is_not_detected():
    """남은 위험 고정: 공백 없는 ASCII 서술은 이 검사로 찾지 못함. 통과를 안전으로 읽으면 안 됨."""
    assert not audit.has_free_text(record(policy_version="chest-pain-yesterday"))


def test_the_writer_refuses_free_text(tmp_path):
    p = tmp_path / audit.RECORDS_FILE
    rec = record(facts_added=(FREE,))
    with pytest.raises(audit.AuditError) as e:
        audit.RecordWriter(p, rec.run_id).write(rec)
    assert FREE not in str(e.value) and not p.exists()


def test_the_trace_writer_refuses_free_text():
    class Tracer:
        logged = []

        def log(self, *a, **k):
            self.logged.append(k)
    rec = record(policy_version=FREE)
    with pytest.raises(audit.AuditError):
        audit.to_trace_checked(Tracer(), rec)
    assert Tracer.logged == []


def test_the_checked_view_withholds_old_free_text_records_without_deleting_them(tmp_path):
    d = run_dir(tmp_path / "run", [record(policy_version=FREE)])
    view = ai.inspect_run_checked(d, LeakScanner())
    assert FREE not in json.dumps(view, ensure_ascii=False, default=str)
    assert view["value_scan"]["status"] == "withheld" and view["audit_status"] == audit_status_withheld()
    assert len(audit.load_run_v2(d).records) == 1  # 파일과 검사 없는 오프라인 읽기는 그대로


def audit_status_withheld() -> str:
    return ai.STATUS_WITHHELD


def test_free_text_has_no_path_into_the_metrics_file_even_unscanned(tmp_path):
    d = run_dir(tmp_path / "run", [record(step=i + 1, policy_version=FREE) for i in range(2)])
    s, problems = rm.compute_for_runs([d])
    out = tmp_path / "m.json"
    rm.write_metrics(s, out, problems)
    assert FREE not in out.read_text(encoding="utf-8") and FREE not in str(s.to_dict(identifiers=True))


def test_a_duplicate_line_with_free_text_withholds_the_step_in_metrics(tmp_path):
    clean = record()
    d = run_dir(tmp_path / "run", [clean, clean.model_copy(update={"policy_version": FREE})])
    records, _, problems = rm.load_run_inputs(d, LeakScanner())
    assert records == [] and "line 2: sensitive_record:free_text" in problems


def test_records_that_read_before_still_read_the_same(tmp_path):
    d = run_dir(tmp_path / "run", T._chain([T.CE, T.VP, T.CI]))
    before = audit.load_run_v2(d)
    assert before.issues == [] and len(before.records) == 2
    view = ai.inspect_run_checked(d, LeakScanner())
    assert view["value_scan"]["status"] == "no_findings" and len(view["steps"]) == 2
    assert rm.compute_for_runs([d])[0].confirmed_eligible_to_ineligible == 1


# --- 내보내기 전용 경로 ---

class Finds:
    def scan(self, text: str) -> list[str]:
        return ["original_id"] if "SYNSECRET" in text else []


class Broken:
    def scan(self, text: str) -> list[str]:
        raise RuntimeError(text)


def test_export_refuses_to_run_without_a_scanner(tmp_path):
    d = run_dir(tmp_path / "run", T._chain([T.CE, T.VP, T.CI]))
    with pytest.raises(audit.AuditError):
        rm.export_metrics([d], None)


def test_export_has_no_identifiers_and_keeps_the_numbers(tmp_path):
    d = run_dir(tmp_path / "RUN-SYNFOLDER", T._chain([T.CE, T.VP, T.CI], run="RUN-SYNRUN", trial="SYNTRIAL"))
    data = rm.export_metrics([d], LeakScanner())
    text = json.dumps(data, ensure_ascii=False)
    assert "SYNRUN" not in text and "SYNTRIAL" not in text and T.A1 not in text and "SYNFOLDER" not in text
    assert data["confirmed_eligible_to_ineligible"] == 1 and data["value_scan"]["status"] == "no_findings"
    assert data["value_scan"]["limit"] == ai.SCAN_LIMIT


def test_export_pair_ref_cannot_be_recomputed_from_candidates(tmp_path):
    d = run_dir(tmp_path / "run", T._chain([T.CE, T.VP, T.CI]))
    public = rm.compute_for_runs([d])[0].to_dict()["events"][0]["pair_ref"]
    first, second = (rm.export_metrics([d], LeakScanner())["events"][0]["pair_ref"] for _ in range(2))
    assert len(first) == len(public) == 12 and first != public and first != second


def test_export_keeps_a_pair_together_inside_one_output(tmp_path):
    d = run_dir(tmp_path / "run", T._chain([T.CE, T.VP, T.CI, T.VP, T.CE]))
    refs = {e["pair_ref"] for e in rm.export_metrics([d], LeakScanner())["events"]}
    assert len(refs) == 1


@pytest.mark.parametrize("scanner,status", [(Finds(), "withheld"), (Broken(), "incomplete")], ids=["withheld", "scan_failed"])
def test_export_status_never_reads_as_clean_after_a_problem(tmp_path, scanner, status):
    d = run_dir(tmp_path / "run", [record(policy_version="SYNSECRET")])
    data = rm.export_metrics([d], scanner)
    assert data["value_scan"]["status"] == status and "SYNSECRET" not in json.dumps(data)


def test_metrics_file_says_whether_it_was_scanned(tmp_path):
    d = run_dir(tmp_path / "run", T._chain([T.CE, T.VP, T.CI]))
    for scanner, status in ((None, "not_scanned"), (LeakScanner(), "no_findings")):
        s, problems = rm.compute_for_runs([d], scanner)
        out = tmp_path / "m.json"
        rm.write_metrics(s, out, problems)
        assert json.loads(out.read_text(encoding="utf-8"))["value_scan"]["status"] == status


def test_to_dict_keys_and_public_pair_ref_are_unchanged(tmp_path):
    d = run_dir(tmp_path / "run", T._chain([T.CE, T.VP, T.CI]))
    data = rm.compute_for_runs([d])[0].to_dict()
    assert "values_scanned" not in data and "value_scan" not in data
    assert set(data["events"][0]) == {"pair_ref", "direction", "anchor_step", "step", "via", "path_kind", "new_evidence",
                                      "verified", "unverified_reason"}


# --- 실제 경로 연결 고정 ---

def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", getattr(n.func, "id", None)) == name]


def test_no_operational_code_reads_metrics_without_a_scanner():
    """src·scripts·eval에서 지표를 읽는 호출은 export_metrics뿐이어야 함 (검사 없는 경로가 새로 생기면 실패)."""
    unchecked = []
    for base in ("src", "scripts", "eval"):
        for f in (ROOT / base).rglob("*.py"):
            if f.name == "reversal_metrics.py":
                continue
            for name in ("compute_for_runs", "load_run_inputs", "compute_reversal_metrics", "write_metrics"):
                unchecked += [f"{f.name}:{c.lineno}:{name}" for c in _calls(f, name)]
    assert unchecked == []
