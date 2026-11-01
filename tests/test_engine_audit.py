"""WP7 감사 기록 v2: 생성·저장·재로딩, 행동→결과→재평가 연결, 실패·부분·무응답, 버전·손상, 민감정보, v1 무변경."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ctrec import inspector
from ctrec.engine import audit
from ctrec.engine import contracts as C
from ctrec.trace import Tracer

A1 = "PA-" + "1" * 16
A2 = "PA-" + "2" * 16


def _action(aid="ACT-1", patient=A1, atype="REQUEST_DOCUMENT", targets=("T1:I4",), trials=("T1",)):
    return C.Action(action_id=aid, action_type=atype, patient_alias=patient, source_id="lab_report",
                    target_trial_ids=trials, target_criteria=targets, reason_code="unverified_self_report", tier=3,
                    fingerprint="fp-" + aid, policy_version="engine-0.1",
                    expected_cost=C.EffortCost(coordinator_minutes=10))


def _ok(aid="ACT-1", facts=("F-aaa",), status="succeeded", minutes=10.0, error=None):
    return C.ActionOutcome(action_id=aid, status=status, new_fact_ids=facts, error_code=error,
                           cost_charged=C.EffortCost(coordinator_minutes=minutes), charge_keys=(f"{A1}|lab_report",))


def _rec(step=0, aid="ACT-1", outcome="ok", **kw):
    out = _ok(aid) if outcome == "ok" else outcome
    base = dict(run_id="RUN-1", step=step, action=_action(aid), outcome=out,
                criteria_before={"T1:I4": "UNKNOWN", "T1:I1": "MET"}, criteria_after={"T1:I4": "MET"},
                reevaluated=["T1:I4"], criterion_evidence={"T1:I4": ("F-aaa",)},
                pair_before={"T1": "VERIFICATION_PENDING"}, pair_after={"T1": "CONFIRMED_ELIGIBLE"},
                policy_version="engine-0.1", evaluator_version="rule_engine/1", fact_version_before="v1",
                fact_version_after="v2")
    base.update(kw)
    return audit.make_record(**base)


def test_make_record_links_action_outcome_criteria_pairs():
    r = _rec()
    assert r.outcome.action_id == r.action.action_id and r.facts_added == ("F-aaa",)
    by = {c.criterion_key: c for c in r.criteria}
    assert by["T1:I4"].reevaluated and by["T1:I4"].after == "MET" and by["T1:I4"].evidence_fact_ids == ("F-aaa",)
    assert not by["T1:I1"].reevaluated and by["T1:I1"].before == by["T1:I1"].after == "MET"  # 재사용
    assert by["T1:I1"].reason_code == "dependency_unchanged"
    assert r.pairs[0].before == C.PairStatus.VERIFICATION_PENDING and r.pairs[0].after == C.PairStatus.CONFIRMED_ELIGIBLE
    view = audit.build_v2_view(audit.LoadResult([r]))
    ch = audit.chain(view, "ACT-1")
    assert ch == {"action_id": "ACT-1", "outcome": "succeeded", "facts": ["F-aaa"], "criteria_changed": ["T1:I4"],
                  "criteria_reused": ["T1:I1"], "pairs_changed": [(f"{A1}|T1", "VERIFICATION_PENDING",
                                                                   "CONFIRMED_ELIGIBLE")]}
    assert view["fact_origin"] == {"F-aaa": "ACT-1"} and audit.chain(view, "nope") is None


def test_reused_criterion_cannot_change_and_reevaluation_needs_result():
    with pytest.raises(ValidationError):
        _rec(criteria_after={"T1:I4": "MET", "T1:I1": "NOT_MET"})  # I1은 재평가하지 않았는데 바뀜
    with pytest.raises(audit.AuditError):
        _rec(reevaluated=["T1:I4", "T1:I9"])


@pytest.mark.parametrize("status,facts,error,pair_after", [
    ("failed", (), "document_unavailable", "BLOCKED_FAILURE"),
    ("no_response", (), "patient_no_response", "INFO_MISSING"),
    ("partial", ("F-p1",), None, "INFO_MISSING"),
])
def test_failure_partial_no_response_records(tmp_path, status, facts, error, pair_after):
    out = _ok(status=status, facts=facts, error=error, minutes=4.0)
    reeval = ["T1:I4"] if facts else []
    r = _rec(outcome=out, criteria_after={"T1:I4": "UNKNOWN"} if facts else {}, reevaluated=reeval,
             criterion_evidence={}, pair_before={"T1": "INFO_MISSING"}, pair_after={"T1": pair_after},
             pair_authority="policy" if pair_after == "BLOCKED_FAILURE" else "adjudication")
    assert r.facts_added == facts
    w = audit.RecordWriter(tmp_path / audit.RECORDS_FILE, "RUN-1")
    w.write(r)
    loaded = audit.load_run_v2(tmp_path)
    assert loaded.issues == [] and loaded.records == [r]
    p = audit.build_v2_view(loaded)["patients"][A1]
    assert p["coordinator_minutes"] == 4.0  # 실패·무응답도 들인 시간은 기록
    assert p["failed_or_no_response"] == (status in ("failed", "no_response")) and p["partial"] == (status == "partial")


def test_failed_action_cannot_record_new_facts():
    with pytest.raises(ValidationError):
        _ok(status="failed", facts=("F-x",), error="timeout")


def test_save_reload_roundtrip_and_writer_guards(tmp_path):
    path = tmp_path / audit.RECORDS_FILE
    w = audit.RecordWriter(path, "RUN-1")
    r0, r1 = _rec(0), _rec(1, aid="ACT-2", outcome=_ok("ACT-2"), pair_before={"T1": "CONFIRMED_ELIGIBLE"},
                            pair_after={"T1": "CONFIRMED_ELIGIBLE"})
    w.write(r0)
    w.write(r1)
    with pytest.raises(audit.AuditError):
        w.write(r0)  # 같은 step
    with pytest.raises(audit.AuditError):
        w.write(r0.model_copy(update={"run_id": "RUN-2"}))
    again = audit.load_records(path)
    assert again.issues == [] and again.records == [r0, r1]
    w2 = audit.RecordWriter(path, "RUN-1")  # 이어 쓰기: 기존 step 기억
    with pytest.raises(audit.AuditError):
        w2.write(r1)
    with pytest.raises(audit.AuditError):
        audit.RecordWriter(path, "RUN-OTHER")


def test_version_mismatch_and_corrupt_lines(tmp_path):
    good = _rec(0)
    lines = [good.model_dump_json(), "{not json",
             json.dumps({**good.model_dump(mode="json"), "schema_version": "ctrec.decision_record/v1", "step": 1}),
             json.dumps({**good.model_dump(mode="json"), "step": 2, "action": {"action_id": "X"}}),
             json.dumps({k: v for k, v in good.model_dump(mode="json").items() if k != "policy_version"} | {"step": 3}),
             good.model_dump_json(), "[]", ""]
    p = tmp_path / audit.RECORDS_FILE
    p.write_text("\n".join(lines), encoding="utf-8")
    res = audit.load_records(p)
    assert res.records == [good]
    joined = " | ".join(res.issues)
    assert "line 2: corrupt_json" in joined and "unsupported_schema:ctrec.decision_record/v1" in joined
    assert "line 4: invalid_record:" in joined and "action." in joined
    assert "line 5: invalid_record:policy_version" in joined and "line 6: duplicate_step:0" in joined
    assert "line 7: not_object" in joined
    assert "UNKNOWN" not in joined and "F-aaa" not in joined  # 문제 보고에 값이 없음
    with pytest.raises(audit.AuditError):
        audit.RecordWriter(p, "RUN-1")  # 손상된 파일에 이어 쓰지 않음


def test_trace_event_added_without_changing_existing(tmp_path):
    tr = Tracer(tmp_path / "trace.jsonl", verbose=False)
    tr.log("matcher", "match", trial_id="T1")  # 기존 이벤트
    audit.to_trace(tr, _rec(0))
    events = [json.loads(x) for x in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(e["agent"], e["event"]) for e in events] == [("matcher", "match"), ("engine", "decision_record_v2")]
    res = audit.load_run_v2(tmp_path)
    assert res.issues == [] and res.records[0].action.action_id == "ACT-1"


def test_missing_v2_records_and_symlink_rejection(tmp_path):
    assert audit.load_run_v2(tmp_path).issues == ["no_v2_records"]
    (tmp_path / "trace.jsonl").write_text('{"agent":"x","event":"y"}\n{bad\n', encoding="utf-8")
    res = audit.load_run_v2(tmp_path)
    assert res.records == [] and "trace line 2: corrupt_json" in res.issues and "no_v2_records" in res.issues
    with pytest.raises(audit.AuditError):
        audit.load_run_v2(tmp_path / "missing")
    outside = tmp_path.parent / (tmp_path.name + "_out.jsonl")
    outside.write_text("", encoding="utf-8")
    try:
        (tmp_path / audit.RECORDS_FILE).symlink_to(outside)
    except OSError:
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없음")
    with pytest.raises(audit.AuditError):
        audit.load_run_v2(tmp_path)


def test_sensitive_text_never_written(tmp_path):
    raw = "The patient is a 62-year-old woman with EGFR exon 19 deletion."
    w = audit.RecordWriter(tmp_path / audit.RECORDS_FILE, "RUN-1", forbidden_texts=[raw])
    w.write(_rec(0))  # ID·코드만 있는 기록은 통과
    leaky = _rec(1).model_copy(update={"run_id": "RUN-1"})
    # 계약상 자유 서술 필드가 없으므로 원문은 형식 검사에서 먼저 막힘
    with pytest.raises(ValidationError):
        C.Action(**{**_action().model_dump(), "reason_code": raw})
    # 원래 케이스 ID를 run_id에 넣는 실수도 차단
    bad = leaky.model_copy(update={"run_id": "M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1"})
    w_bad = audit.RecordWriter(tmp_path / "other.jsonl", "M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1")
    with pytest.raises(audit.AuditError):
        w_bad.write(bad)
    assert not (tmp_path / "other.jsonl").exists()
    text = (tmp_path / audit.RECORDS_FILE).read_text(encoding="utf-8")
    assert raw not in text and "EGFR" not in text


def test_cross_patient_pairs_rejected():
    with pytest.raises(ValidationError):
        C.DecisionRecord(run_id="R", step=0, action=_action(), policy_version="p",
                         pairs=(C.PairTransition(pair=C.PairKey(patient_alias=A2, trial_id="T1"),
                                                 before="INFO_MISSING", after="INFO_MISSING"),))


def test_html_render_escapes_and_has_no_scripts():
    r = _rec(0)
    view = audit.build_v2_view(audit.LoadResult([r], issues=["<script>alert(1)</script>"]))
    out = audit.render_v2_html(view)
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "ACT-1" in out and "CONFIRMED_ELIGIBLE" in out and view["schema"] == "ctrec.inspector/v2"


def test_inspector_v1_untouched():
    assert inspector.SCHEMA == "ctrec.inspector/v1"
    assert not hasattr(inspector, "load_run_v2")  # v2는 별도 모듈
