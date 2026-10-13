"""Decision Loop 제어와 사실·의존성 연계 (Phase 3, B·C). 합성 데이터, 실제 Matcher + 오프라인 LLM 대역."""

from __future__ import annotations

import contextvars
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import llm
from ctrec.agents import matcher
from ctrec.decision_loop import LoopConfig, guard_reason
from ctrec.decision_state import Budget, SessionView

import decision_fixtures as d


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)


def _base(*rules_by_trial, facts=None, **kw):
    s = d.store(*(facts or [("age", 60, {"unit": "years"})]))
    trials = {f"T{i + 1}": d.trial(f"T{i + 1}", rules) for i, rules in enumerate(rules_by_trial)}
    return d.view(trials, s, **kw), s


def _cfg(**budget):
    return LoopConfig(budget=Budget(**budget))


# ------------------------------------------------------------ B. 루프 제어
def test_normal_finalize_plans_questions_without_dispatch():
    v, _ = _base([d.AGE, d.ECOG, d.EGFR], [d.AGE, d.ECOG])
    r, ctx = d.run(v)
    assert d.types(r) == [("ASK_USER", "ecog"), ("ASK_USER", "biomarker:EGFR"), ("FINALIZE", "finalize")]
    assert {a.status for a in r.actions[:2]} == {"proposed_not_dispatched"}  # 진단: 질문을 보내지 않음
    assert r.stop_reason == "awaiting_user_answers" and r.usage.llm_calls == 0
    assert r.stats["duplicates_blocked"] >= 1  # 이미 계획한 질문은 다음 반복에서 다시 만들지 않음


def test_sufficient_information():
    v, _ = _base([d.AGE, d.ECOG], facts=[("age", 60, {"unit": "years"}), ("ecog", 1, {})])
    r, _ = d.run(v)
    assert d.types(r) == [("FINALIZE", "finalize")] and r.stop_reason == "sufficient_information"


def test_no_information_change_stops():
    v, _ = _base([d.AGE, d.ECOG, d.EGFR, d.DX])
    r, _ = d.run(v, channel=d.SimChannel({}))  # 답은 오지만 사실이 생기지 않음
    assert r.stop_reason == "no_information_change"
    assert [a.action_type for a in r.actions] == ["ASK_USER", "ASK_USER"]


def test_iteration_time_cost_tool_and_question_limits():
    v, _ = _base([d.AGE, d.ECOG, d.EGFR, d.DX])
    assert d.run(v, config=_cfg(max_iterations=1))[0].stop_reason == "iteration_limit"
    assert d.run(v, config=_cfg(max_seconds=0))[0].stop_reason == "time_limit"
    assert d.run(v, config=_cfg(max_cost_units=1))[0].stop_reason == "cost_limit"
    r, _ = d.run(v, config=_cfg(max_questions=1))
    assert [a.action_type for a in r.actions].count("ASK_USER") == 1
    assert any(x["why"] == "question_budget" for x in r.rejected)
    v2, _ = _base([d.AGE, d.ECOG, d.EGFR])
    r2, _ = d.run(v2, channel=d.SimChannel({"ecog": [("ecog", 1, {})]}), config=_cfg(max_tool_calls=1))
    assert r2.stop_reason == "tool_call_limit" and r2.usage.tool_calls == 1


def test_external_input_and_review_reasons():
    s = d.store(("age", 60, {"unit": "years"}), ("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-09-01"}))
    v = d.view({"T1": d.trial("T1", [d.AGE, d.HB, d.VAGUE], meta=d.record_meta("I4"))}, s,
               source_text="합성 Hb 11.0", input_source="unknown")
    r, _ = d.run(v)
    doc = next(a for a in r.actions if a.action_type == "REQUEST_DOCUMENT")
    assert doc.status == "awaiting_external_input" and doc.outcome == {
        "requires_external_input": True, "verified": False, "document_for": ["T1:I4"]}
    assert r.stop_reason == "needs_review" and "awaiting_external_input" in r.finalize_reasons


def test_cancel_and_web_guard():
    v, _ = _base([d.AGE, d.ECOG])
    assert d.run(v, stop_check=lambda: "cancelled")[0].stop_reason == "cancelled"

    def in_cancelled_run():
        llm.set_guard(lambda: "cancelled")
        return d.run(v, stop_check=guard_reason)[0]
    r = contextvars.copy_context().run(in_cancelled_run)
    assert r.stop_reason == "cancelled" and r.actions == []
    assert guard_reason() is None  # 다른 실행에는 영향 없음


def test_tool_failure_stops_safely():
    v, _ = _base([d.AGE, d.ECOG])
    ctx_tools = {"deterministic_reevaluation": lambda ctx, a: (_ for _ in ()).throw(RuntimeError("합성 실패 58세"))}
    from ctrec.decision_loop import LoopContext, run_loop
    ctx = LoopContext(view=v, matched_snapshots={t: v.snapshot for t in v.matches}, tools=ctx_tools)
    sim = d.SimChannel({"ecog": [("ecog", 1, {})]})
    r = run_loop(ctx, sim, stop_check=lambda: None)
    failed = r.actions[-1]
    assert r.stop_reason == "tool_failure" and failed.status == "failed"
    assert failed.outcome == {"error_type": "RuntimeError"}  # 예외 문구(환자 정보일 수 있음)는 남기지 않음


def test_declined_answer_is_not_asked_again():
    v, _ = _base([d.AGE, d.ECOG])
    sim = d.SimChannel({"ecog": "declined"})
    r, _ = d.run(v, channel=sim)
    assert sim.asked == ["ecog"] and "user_declined" in r.finalize_reasons
    assert any(a.action_type == "DEFER" and a.reason_code == "user_declined_or_exhausted" for a in r.actions)


# ------------------------------------------------------------ C. 사실·의존성 연계
def test_answer_triggers_rule_engine_reevaluation_and_llm_staleness():
    v, s = _base([d.AGE, d.ECOG, d.EGFR], [d.AGE, d.ECOG])
    before = {f.fact_id for f in s.facts(d.P)}
    sim = d.SimChannel({"ecog": [("ecog", 1, {})]})
    r, ctx = d.run(v, channel=sim)
    seq = [(a.action_type, a.reason_code) for a in r.actions]
    assert seq[0] == ("ASK_USER", "missing_required_info")
    assert seq[1] == ("RUN_TOOL", "fact_changed_reevaluate_rule_engine")
    tool = r.actions[1]
    assert sorted(tool.target_criterion_ids) == ["T1:I2", "T2:I2"] and tool.outcome == {"reevaluated": 2, "unavailable": 0}
    assert r.stats["rule_engine_reused"] == 2  # 나이 기준(필드 그대로)은 재사용
    stale = next(a for a in r.actions if a.reason_code == "llm_reevaluation_required")
    assert stale.target_criterion_ids == ["T1:I3"] and stale.action_type == "DEFER"  # LLM 판정은 다시 쓰지 않음
    ecog = ctx.overlay["T1:I2"]  # ECOG는 진료 기록이 필요한 기준 -> 자기 보고 답은 UNVERIFIED (MET 아님)
    assert ecog.passes is None and ecog.needs_verification and ecog.self_reported_holds == "yes"
    assert any(a.action_type == "REQUEST_DOCUMENT" and a.info_key == "ecog" for a in r.actions)
    assert {f.fact_id for f in s.facts(d.P)} == before  # 원래 저장소는 바뀌지 않음 (작업용 사본만)
    assert v.matches["T1"].eligibility == "UNCERTAIN"  # 기준선 판정도 그대로


def test_self_report_for_record_criterion_becomes_document_request_not_met():
    v, _ = _base([d.AGE, d.HB])
    v.parsed["T1"].rule_meta = d.record_meta("I4")
    sim = d.SimChannel({"lab:hemoglobin": [("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-10-01"})]})
    r, ctx = d.run(v, channel=sim)
    hb = ctx.overlay["T1:I4"]
    assert hb.passes is None and hb.needs_verification and hb.self_reported_holds == "yes"  # MET로 승격하지 않음
    assert any(a.action_type == "REQUEST_DOCUMENT" and a.target_criterion_ids == ["T1:I4"] for a in r.actions)


def test_conflicting_answer_never_met_and_goes_to_review():
    v, _ = _base([d.AGE, d.ECOG], facts=[("age", 60, {"unit": "years"})])
    sim = d.SimChannel({"ecog": [("ecog", 1, {}), ("ecog", 3, {})]})
    r, ctx = d.run(v, channel=sim)
    assert ctx.overlay["T1:I2"].passes is None  # 상충 사실로 MET를 만들지 않음
    seq = [a.action_type for a in r.actions]
    assert seq[0] == "ASK_USER" and seq[1] == "VERIFY"  # 안전 확인이 재계산보다 먼저
    assert seq.index("NEEDS_REVIEW") > seq.index("VERIFY") and "RUN_TOOL" in seq
    verify = r.actions[1]
    assert verify.outcome["requires_review"] and verify.outcome["document_verified"] == 0
    assert r.stop_reason == "needs_review"


def test_provenance_and_session_isolation():
    v, _ = _base([d.AGE, d.ECOG], facts=[("age", 60, {"unit": "years"}), ("ecog", 1, {})])
    other_store = d.store(("age", 30, {"unit": "years"}), patient="SYN-OTHER")
    assert all(f.patient_id == d.P for f in v.snapshot.facts)
    assert not set(f.fact_id for f in other_store.facts("SYN-OTHER")) & set(f.fact_id for f in v.snapshot.facts)
    from ctrec.decision_state import build_state
    st = build_state(v)
    ids = {f.fact_id for f in v.snapshot.facts}
    for c in st.criteria():
        assert set(c.evidence_refs) <= ids  # 근거는 이 환자의 사실 ID만
    with pytest.raises(ValueError):
        d.fact_changes.make_snapshot(d.P, other_store.facts("SYN-OTHER"))


def test_session_view_copies_do_not_touch_session(monkeypatch):
    """SessionView.from_session은 Session을 바꾸지 않는 사본."""
    from test_session_fact_shadow import _session
    import test_session_fact_shadow as harness
    from ctrec import config, pipeline
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", harness._fake_profile())
    monkeypatch.setattr(pipeline.trial_store, "get", lambda trial: harness._parsed([r.model_copy() for r in harness.RULES]))
    monkeypatch.setattr(matcher.llm, "structured", harness._fake_matcher_llm)
    monkeypatch.setattr(config, "WRITE_REPORT", False)
    s = _session()
    s.profile_patient()
    s.match()
    v = SessionView.from_session(s)
    v.matches["T1"].eligibility = "ELIGIBLE"
    v.qa_log.append(d.qa("X", "합성"))
    assert s.matches["T1"].eligibility != "ELIGIBLE" and s.qa_log == []


# ------------------------------------------------------------ F. 실제 고정 기준표 시나리오 (읽기만)
def test_frozen_store_scenarios(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import decision_scenarios
    from ctrec import store_patch

    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)  # run_all이 자체 대역으로 바꾸고 되돌림
    rows = decision_scenarios.run_all()
    assert len(rows) == 10
    allowed = {"sufficient_information", "needs_review", "awaiting_external_input", "awaiting_user_answers",
               "user_declined", "no_valid_action", "no_information_change", "repeat_risk", "iteration_limit",
               "tool_call_limit", "cost_limit", "time_limit", "cancelled", "timeout", "tool_failure"}
    for r in rows:
        parts = [r] if "stop_reason" in r else [r["cancel"], r["budget"]]
        for p in parts:
            assert p["stop_reason"] in allowed and p["decision_llm_calls"] == 0
            assert p["stop_reason"] != "iteration_limit"
        assert r["baseline_unchanged"]  # 기존 Matcher 판정 그대로
    by = {r["scenario"][:2].strip(): r for r in rows}
    assert by["3"]["actions"][:2] == ["VERIFY:fact_conflict:ecog", "NEEDS_REVIEW:conflict_requires_review:ecog"]
    assert any(a.startswith("REQUEST_DOCUMENT") for a in by["2"]["actions"]) and by["2"]["reevaluated"] >= 1
    assert by["2"]["llm_stale"] > 0  # 사실이 바뀌면 LLM 판정은 다시 쓰지 않음
    assert all(not a.startswith("ASK_USER") or "ecog" not in a for a in by["5"]["actions"])  # '모름' 뒤 재질문 없음
    # 8세: 실제 기준표에는 나이 조건을 충족하는 시험 5개가 있어 그 시험의 질문이 남고(기대값 수정),
    # 나이로 불충족인 21개는 근거가 문서로 검증되지 않아 잠정 불충족 -> 근거 문서 확인 요청 하나로 묶임
    assert any(a == "REQUEST_DOCUMENT:exclusion_basis_unverified:basis:age" for a in by["8"]["actions"])
    assert by["8"]["questions"] == 5 and by["8"]["stop_reason"] == "needs_review"
    assert by["9"]["stop_reason"] == "no_information_change"
    assert by["10"]["cancel"]["stop_reason"] == "cancelled" and by["10"]["budget"]["stop_reason"] == "cost_limit"
    assert store_patch.verify_frozen() == []
