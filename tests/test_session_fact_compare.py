"""Session의 사실 기반 비교 (Phase 1-3B/1-4, 체크포인트 4). 기본 off, 켜도 판정·질문·추천은 기준선 그대로.

Phase 1-3A의 Session 대역(프로필·매칭 LLM·질문·추천)을 그대로 씀. API 호출 없음.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import fact_compare, store_patch

from test_session_fact_shadow import _outputs, _run, calls  # noqa: F401 - calls는 pytest fixture


def _reset(calls):
    calls["questions"].clear()
    calls["recommend"].clear()


def test_off_by_default_and_result_unchanged(calls):
    s = _run()
    assert s.fact_compare == "off" and s.fact_comparison is None
    assert "fact_comparison" not in s.result()  # 기본 결과 형식은 Phase 1-3A와 같음 (웹/API 응답 호환)
    assert not any(r["agent"] == "fact_compare" for r in s.tracer.records)


def test_deterministic_comparison_keeps_results_identical(calls):
    off = _outputs(_run(), calls)
    _reset(calls)
    s = _run(fact_compare="deterministic")
    assert _outputs(s, calls) == off
    c = s.fact_comparison
    assert c["status"] == "ok" and c["mode"] == "deterministic" and c["risks"] == []
    [trial] = c["trials"]
    by = {x["rule_id"]: x for x in trial["criteria"]}
    assert by["I1"]["change"] == "same" and by["I3"]["change"] == "same"  # 나이, 혈색소(선언된 기록)
    assert by["I2"]["change"] == "not_compared"  # LLM 기준은 기본 모드에서 비교하지 않음
    assert s.result()["fact_comparison"] == c


def test_llm_comparison_opt_in_keeps_results_identical(calls):
    off = _outputs(_run(), calls)
    _reset(calls)
    s = _run(fact_compare="llm")
    assert _outputs(s, calls) == off
    [trial] = s.fact_comparison["trials"]
    by = {x["rule_id"]: x for x in trial["criteria"]}
    # 답변에서만 나온 EGFR: 기준선도 사실 기반도 UNVERIFIED(판정 불가)
    assert (by["I2"]["baseline"], by["I2"]["fact"], by["I2"]["cause"]) == ("UNKNOWN", "UNKNOWN", "self_report_only")
    assert trial["trial_change"] == "same" and not trial["trial_risk"]


def test_strict_mode_reports_declared_records(calls):
    s = _run(fact_compare="deterministic", fact_compare_strict=True)
    by = {x["rule_id"]: x for x in s.fact_comparison["trials"][0]["criteria"]}
    assert (by["I3"]["change"], by["I3"]["cause"]) == ("met_to_unknown", "declared_unverified")
    assert s.fact_comparison["require_document_verification"] is True


def test_comparison_failure_isolated(calls, monkeypatch):
    off = _outputs(_run(), calls)
    _reset(calls)

    def boom(*a, **k):
        raise ValueError("민감할 수 있는 메시지 11.2")

    monkeypatch.setattr(fact_compare, "compare_session", boom)
    s = _run(fact_compare="deterministic")
    assert _outputs(s, calls) == off
    assert s.fact_comparison == {"status": "error", "error_type": "ValueError"}  # 실패를 성공으로 표시하지 않음
    assert s.errors == []


def test_invalid_mode_and_shadow_off(calls):
    s = _run(fact_compare="sometimes")
    assert s.fact_comparison == {"status": "error", "error_type": "InvalidMode"}
    t = _run(fact_compare="deterministic", fact_shadow=False)
    assert t.fact_comparison["status"] == "skipped"


def test_patients_and_sessions_separated(calls):
    a, b = _run(pid="A1", fact_compare="deterministic"), _run(pid="B2", fact_compare="deterministic")
    assert a.fact_store.patients() == ["A1"] and b.fact_store.patients() == ["B2"]
    assert a.fact_comparison["status"] == b.fact_comparison["status"] == "ok"


def test_no_files_no_patient_values_frozen_intact(calls, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _run(fact_compare="llm")
    assert list(tmp_path.iterdir()) == []
    text = json.dumps(s.fact_comparison, ensure_ascii=False)
    for value in ("EGFR 양성", "11.2", "female", "62세"):
        assert value not in text
    assert store_patch.verify_frozen() == []
