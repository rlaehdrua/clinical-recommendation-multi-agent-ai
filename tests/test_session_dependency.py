"""Session의 의존 그래프·부분 재평가 진단 (Phase 2, 체크포인트 5). 기본 off, 켜도 판정·질문·추천은 그대로.

Phase 1-3A의 Session 대역(프로필·매칭 LLM·질문·추천)을 그대로 씀. API 호출 없음.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import incremental, store_patch
from ctrec.agents import matcher

import test_session_fact_shadow as harness
from test_session_fact_shadow import _outputs, _run, calls  # noqa: F401 - calls는 pytest fixture


def _reset(calls):
    calls["questions"].clear()
    calls["recommend"].clear()


def _count_llm(monkeypatch):
    n = {"calls": 0}

    def counted(*a, **k):
        n["calls"] += 1
        return harness._fake_matcher_llm(*a, **k)
    monkeypatch.setattr(matcher.llm, "structured", counted)
    return n


def test_off_by_default(calls):
    s = _run()
    assert s.dependency_diagnostic is False and s.dependency_diagnostics == [] and s._dependency_tracker is None
    assert "dependency_diagnostic" not in s.result()
    assert not any(r["agent"] == "dependency" for r in s.tracer.records)


def test_on_keeps_results_and_llm_calls_identical(calls, monkeypatch):
    n = _count_llm(monkeypatch)
    off = _outputs(_run(), calls)
    off_calls = n["calls"]
    _reset(calls)
    n["calls"] = 0
    s = _run(dependency_diagnostic=True)
    assert _outputs(s, calls) == off  # 판정·질문 입력·추천 입력 동일
    assert n["calls"] == off_calls  # 진단이 LLM을 더 부르지 않음


def test_on_records_initial_and_incremental_rounds(calls):
    s = _run(dependency_diagnostic=True)
    first, second = s.dependency_diagnostics
    assert (first["round"], first["status"], first["mode"]) == (0, "ok", "initial")
    assert (second["round"], second["status"], second["mode"]) == (1, "ok", "incremental")
    assert second["changes"]["changed_fields"] == ["biomarker"]  # 답변으로 EGFR 사실이 생김
    assert second["impact"]["affected_trials"] == ["T1"] and second["stats"]["affected_criteria"] == 1
    assert second["stats"]["reused"] == 2  # 나이·혈색소 기준은 재사용
    assert second["equivalence"]["status"] == "equivalent" and second["base_for_next"] == "incremental"
    scope = second["equivalence"]["llm_judgments"]  # LLM 기준의 같음은 Matcher가 이번에 낸 출력 기준 (실제 재판정 아님)
    assert scope["scope"] == "assessor_outputs_only" and scope["sources"] == ["baseline_matcher"]
    assert s.result()["dependency_diagnostic"] == s.dependency_diagnostics


def test_failure_isolated(calls, monkeypatch):
    off = _outputs(_run(), calls)
    _reset(calls)

    def boom(*a, **k):
        raise ValueError("민감할 수 있는 메시지 11.2")
    monkeypatch.setattr(incremental.DependencyTracker, "observe", boom)
    s = _run(dependency_diagnostic=True)
    assert _outputs(s, calls) == off
    assert [e["status"] for e in s.dependency_diagnostics] == ["error", "error"]
    assert s.dependency_diagnostics[0] == {"round": 0, "status": "error", "error_type": "ValueError"}
    assert s.errors == []


def test_shadow_off_skips(calls):
    s = _run(dependency_diagnostic=True, fact_shadow=False)
    assert {e["status"] for e in s.dependency_diagnostics} == {"skipped"}


def test_works_together_with_fact_comparison(calls):
    off = _outputs(_run(), calls)
    _reset(calls)
    s = _run(dependency_diagnostic=True, fact_compare="deterministic")
    assert _outputs(s, calls) == off
    assert s.fact_comparison["status"] == "ok" and s.dependency_diagnostics[-1]["status"] == "ok"


def test_patients_and_sessions_isolated(calls):
    a = _run(pid="A1", dependency_diagnostic=True)
    b = _run(pid="B2", dependency_diagnostic=True)
    assert a._dependency_tracker is not b._dependency_tracker
    assert (a._dependency_tracker.patient_id, b._dependency_tracker.patient_id) == ("A1", "B2")
    assert {f.patient_id for f in a._dependency_tracker.snapshot.facts} == {"A1"}
    assert {f.patient_id for f in b._dependency_tracker.snapshot.facts} == {"B2"}


def test_no_files_no_patient_values_frozen_intact(calls, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _run(dependency_diagnostic=True)
    assert list(tmp_path.iterdir()) == []
    text = json.dumps(s.dependency_diagnostics, ensure_ascii=False)
    for value in ("EGFR 양성", "EGFR positive", "11.2", "female", "62세", "비소세포폐암"):
        assert value not in text
    assert store_patch.verify_frozen() == []
