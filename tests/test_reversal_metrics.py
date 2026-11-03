"""확정 판정 역전 지표 (engine.reversal_metrics): 고정된 쌍 상태 이력으로 정의를 검증. API 호출 없음.

이 지표는 판정 상태 변경 건수이며 오판 건수가 아님. 실제 masked 오프라인 실행에서는 확정 상태가 나오지 않아 0이
예상되므로, 기능 검증은 여기의 고정 이력으로 합니다.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ctrec.engine import audit
from ctrec.engine import contracts as C
from ctrec.engine import record_producer as rp
from ctrec.engine import reversal_metrics as rm

A1 = "PA-" + "1" * 16
A2 = "PA-" + "2" * 16
CE, CI = "CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE"
VP, ER, IM, BF, NE = "VERIFICATION_PENDING", "EXPERT_REVIEW", "INFO_MISSING", "BLOCKED_FAILURE", "NOT_EVALUATED"


def _action(alias, step, trials):
    return C.Action(action_id=f"ACT-{alias[-2:]}-{step}", action_type="REQUEST_DOCUMENT", patient_alias=alias,
                    source_id="lab_report", target_trial_ids=tuple(trials), reason_code="test", tier=3,
                    fingerprint=f"fp-{step}", policy_version="test-0.2")


def _rec(step, transitions, alias=A1, run="RUN-1", facts=()):
    """transitions: {trial_id: (before, after)} -> 정상 DecisionRecord (계약 검증 통과해야 함)."""
    a = _action(alias, step, transitions)
    out = C.ActionOutcome(action_id=a.action_id, status="succeeded", new_fact_ids=tuple(facts))
    pairs = tuple(C.PairTransition(pair=C.PairKey(patient_alias=alias, trial_id=t), before=b, after=af,
                                   reason_code="contradicting_evidence" if b in (CE, CI) and af != b else None)
                  for t, (b, af) in sorted(transitions.items()))
    return C.DecisionRecord(run_id=run, step=step, action=a, outcome=out, facts_added=tuple(facts), pairs=pairs,
                            policy_version="test-0.2")


def _chain(statuses, trial="T1", alias=A1, run="RUN-1", start=1):
    """상태 순서 [s0, s1, ...] -> 단계별 전이 기록 (s0->s1, s1->s2, ...)."""
    return [_rec(start + i, {trial: (b, a)}, alias, run) for i, (b, a) in enumerate(zip(statuses, statuses[1:]))]


def _direct_issue(step, before, after, alias=A1, trial="T1", run="RUN-1"):
    return rp.RecordIssue(issue_id=f"{run}:{step}:direct_confirmed_reversal:{trial}", run_id=run, step=step,
                          action_id=f"ACT-{alias[-2:]}-{step}", code="direct_confirmed_reversal", severity="blocking",
                          patient_alias=alias, trial_id=trial, before=before, after=after)


# 1
def test_eligible_to_pending_to_ineligible_counts_once():
    s = rm.compute_reversal_metrics(_chain([CE, VP, CI]))
    assert s.confirmed_eligible_to_ineligible == 1 and s.confirmed_ineligible_to_eligible == 0
    assert s.unverified_reversals == 0 and s.direct_reversal_violations == 0
    assert s.downgraded_pending_from_eligible == 0
    (ev,) = s.events
    assert ev.direction == "eligible_to_ineligible" and ev.via == (VP,) and ev.verified
    assert (ev.anchor_step, ev.step) == (1, 2)


# 2
def test_ineligible_to_pending_to_eligible_counts_once():
    s = rm.compute_reversal_metrics(_chain([CI, VP, CE]))
    assert s.confirmed_ineligible_to_eligible == 1 and s.confirmed_eligible_to_ineligible == 0


def test_expert_review_and_not_evaluated_are_intermediate_states():
    s = rm.compute_reversal_metrics(_chain([CE, ER, NE, CI]))
    assert s.confirmed_eligible_to_ineligible == 1 and s.events[0].via == (ER, NE)
    assert s.events[0].path_kind == "failure_or_not_evaluated"  # 평가 실패를 거침: 정상 검증 경유 아님


# 경유 유형: 실패·미평가 상태를 거친 역전은 검증 경유로 분류하지 않음
@pytest.mark.parametrize("path, kind", [
    ([CE, VP, CI], "verification_only"),
    ([CE, ER, VP, CI], "verification_only"),
    ([CE, VP, BF, CI], "failure_or_not_evaluated"),
    ([CE, NE, CI], "failure_or_not_evaluated"),
    ([CE, VP, IM, BF, VP, CI], "failure_or_not_evaluated"),  # 실패가 정보 재확보보다 우선
    ([CE, VP, IM, CI], "via_info_missing"),
])
def test_path_kind_separates_verification_from_failure_and_not_evaluated(path, kind):
    s = rm.compute_reversal_metrics(_chain(path))
    (ev,) = s.events
    assert ev.path_kind == kind and ev.verified
    assert s.confirmed_eligible_to_ineligible == 1  # 방향별 합계는 경유 유형과 무관한 상태 변경 수
    assert s.reversals_by_path["eligible_to_ineligible"] == {k: int(k == kind) for k in rm.PATH_KINDS}
    assert sum(s.reversals_by_path["ineligible_to_eligible"].values()) == 0


def test_failure_path_from_ineligible_is_classified_too():
    s = rm.compute_reversal_metrics(_chain([CI, VP, BF, CE]))
    assert s.reversals_by_path["ineligible_to_eligible"]["failure_or_not_evaluated"] == 1
    assert s.reversals_by_path["ineligible_to_eligible"]["verification_only"] == 0


# 3
def test_eligible_to_pending_only_is_not_a_reversal_but_pending():
    s = rm.compute_reversal_metrics(_chain([CE, VP]))
    assert s.confirmed_eligible_to_ineligible == s.confirmed_ineligible_to_eligible == 0 and s.events == []
    assert s.downgraded_pending_from_eligible == 1 and s.downgraded_pending_from_ineligible == 0


def test_return_to_same_confirmed_is_not_a_reversal():
    s = rm.compute_reversal_metrics(_chain([CE, VP, CE]))
    assert s.events == [] and s.downgraded_pending_from_eligible == 0


# 4
def test_eligible_to_eligible_is_zero():
    s = rm.compute_reversal_metrics(_chain([CE, CE, CE]))
    assert s.events == [] and s.downgraded_pending_from_eligible == 0 and s.history_gaps == 0


@pytest.mark.parametrize("st", [NE, BF, IM])
def test_non_confirmed_states_are_never_counted_as_confirmed(st):
    # 확정이 없는 이력: 아무리 오가도 역전 아님
    s = rm.compute_reversal_metrics(_chain([IM, st, VP, st]))
    assert s.events == [] and s.downgraded_pending_from_eligible == s.downgraded_pending_from_ineligible == 0


# 5
def test_direct_reversal_is_rejected_by_contract_and_counted_only_as_violation():
    with pytest.raises(ValidationError):  # 정상 기록으로는 표현 불가 (계약 0.2 그대로)
        _rec(2, {"T1": (CE, CI)})
    # 실제 실행 모양: step1 정상, step2 차단(진단만), step3 정상 (before가 끊김을 설명)
    records = [_rec(1, {"T1": (VP, CE)}), _rec(3, {"T1": (CI, CI)})]
    s = rm.compute_reversal_metrics(records, [_direct_issue(2, CE, CI)])
    assert s.direct_reversal_violations == 1
    assert s.direct_violations_by_direction == {"eligible_to_ineligible": 1, "ineligible_to_eligible": 0}
    assert s.confirmed_eligible_to_ineligible == 0 and s.unverified_reversals == 0 and s.events == []
    assert s.history_gaps == 1


def test_withheld_transitions_in_issue_file_do_not_enter_normal_metric():
    withheld = rp.RecordIssue(issue_id="RUN-1:2:withheld_pair_transition:T1", run_id="RUN-1", step=2,
                              action_id="ACT-11-2", code="withheld_pair_transition", severity="blocking",
                              patient_alias=A1, trial_id="T1", before=CE, after=VP)
    s = rm.compute_reversal_metrics(_chain([CE, VP, CI]) [:1], [withheld])
    assert s.events == [] and s.direct_reversal_violations == 0


def test_gap_without_explanation_is_reported_unverified_not_confirmed():
    # step1 CE->CE 관찰, step2 기록 없음, step3 VP->CI : CE에서 VP로 간 경로가 기록에 없음
    records = [_rec(1, {"T1": (CE, CE)}), _rec(3, {"T1": (VP, CI)})]
    s = rm.compute_reversal_metrics(records)
    assert s.confirmed_eligible_to_ineligible == 0 and s.unverified_reversals == 1 and s.history_gaps == 1
    assert s.events[0].verified is False and s.events[0].unverified_reason == "missing_record"
    assert s.unverified_by_reason == {"missing_record": 1, "conflicting_records": 0}
    assert sum(sum(d.values()) for d in s.reversals_by_path.values()) == 0  # 미확인은 경유 유형 내역에도 없음


def test_gap_over_conflicting_step_is_reported_with_its_reason():
    records = [_rec(1, {"T1": (CE, CE)}), _rec(2, {"T1": (CE, VP)}), _rec(2, {"T1": (CE, ER)}),
               _rec(3, {"T1": (VP, CI)})]
    s = rm.compute_reversal_metrics(records)
    assert s.conflicting_steps == 1 and s.unverified_reversals == 1
    assert s.events[0].unverified_reason == "conflicting_records" and s.confirmed_eligible_to_ineligible == 0


def test_gap_returning_to_same_confirmed_is_not_counted_but_visible_as_gap():
    s = rm.compute_reversal_metrics([_rec(1, {"T1": (CE, CE)}), _rec(5, {"T1": (CE, CE)}),
                                     _rec(6, {"T1": (VP, CE)})])
    assert s.events == [] and s.history_gaps == 1


# 6
def test_repeated_state_records_and_duplicate_records_are_not_double_counted():
    hist = _chain([CE, VP, VP, VP, CI, CI])
    s = rm.compute_reversal_metrics(hist + hist[:3])  # 같은 기록 재입력
    assert s.confirmed_eligible_to_ineligible == 1 and len(s.events) == 1
    assert s.duplicate_records_ignored == 3 and s.conflicting_steps == 0
    assert s.events[0].via == (VP,)


def test_conflicting_records_for_same_step_are_dropped_and_surface_as_gap():
    a = _chain([CE, VP, CI])
    other = _rec(1, {"T1": (CE, ER)})  # 같은 run·step, 다른 내용
    s = rm.compute_reversal_metrics(a + [other])
    assert s.conflicting_steps == 1
    assert s.confirmed_eligible_to_ineligible == 0 and s.unverified_reversals == 0  # step1을 버려 anchor 관찰 없음


def test_two_reversals_in_one_history_count_twice():
    s = rm.compute_reversal_metrics(_chain([CE, VP, CI, ER, CE]))
    assert s.confirmed_eligible_to_ineligible == 1 and s.confirmed_ineligible_to_eligible == 1


# 7
def test_pairs_patients_and_runs_are_counted_separately():
    recs = (_chain([CE, VP, CI], trial="T1") + _chain([CI, VP, CE], trial="T2", start=10)
            + _chain([CE, VP, CI], alias=A2, start=20) + _chain([CE, VP], run="RUN-2")
            + _chain([VP, CI], run="RUN-3"))  # RUN-3은 이전 확정이 없으므로 역전 아님
    s = rm.compute_reversal_metrics(recs)
    assert s.runs == 3 and s.pairs_observed == 5 and s.conflicting_steps == 0
    assert s.confirmed_eligible_to_ineligible == 2 and s.confirmed_ineligible_to_eligible == 1
    assert s.downgraded_pending_from_eligible == 1
    assert {(e.run_id, e.patient_alias, e.trial_id) for e in s.events} == {
        ("RUN-1", A1, "T1"), ("RUN-1", A1, "T2"), ("RUN-1", A2, "T1")}


def test_same_pair_in_different_runs_does_not_link_histories():
    s = rm.compute_reversal_metrics(_chain([CE, VP], run="RUN-1") + _chain([VP, CI], run="RUN-2"))
    assert s.events == [] and s.downgraded_pending_from_eligible == 1


def test_one_record_with_several_trials():
    recs = [_rec(1, {"T1": (CE, VP), "T2": (CI, VP)}), _rec(2, {"T1": (VP, CI), "T2": (VP, CI)})]
    s = rm.compute_reversal_metrics(recs)
    assert s.confirmed_eligible_to_ineligible == 1 and s.confirmed_ineligible_to_eligible == 0


def test_new_evidence_flag_is_informational():
    recs = [_rec(1, {"T1": (CE, VP)}), _rec(2, {"T1": (VP, CI)}, facts=("F-000000000001",))]
    s = rm.compute_reversal_metrics(recs)
    assert s.events[0].new_evidence and s.reversals_without_new_evidence == 0
    s2 = rm.compute_reversal_metrics(_chain([CE, VP, CI]))
    assert s2.reversals_without_new_evidence == 1


def test_repeat_run_dirs_with_same_run_id_are_separate_scopes(tmp_path):
    # run_id = RUN-<별칭>은 같은 사례를 다시 실행하면 같음. 폴더가 다르면 이력을 잇거나 충돌로 버리지 않음
    for n, facts in ((1, ("F-000000000001",)), (2, ("F-000000000002",))):
        w = audit.RecordWriter(tmp_path / f"r{n}" / audit.RECORDS_FILE, "RUN-1")
        w.write(_rec(1, {"T1": (CE, VP)}))
        w.write(_rec(2, {"T1": (VP, CI)}, facts=facts))  # 실행마다 fact_id가 다름 (FactStore uuid)
    s, problems = rm.compute_for_runs([tmp_path / "r1", tmp_path / "r2"])
    assert problems == [] and s.conflicting_steps == 0 and s.history_gaps == 0
    assert s.confirmed_eligible_to_ineligible == 2 and s.runs == 2 and s.pairs_observed == 2
    assert s.reversals_by_path["eligible_to_ineligible"]["verification_only"] == 2


def test_files_round_trip_through_writers(tmp_path):
    d = tmp_path / "run"
    w = audit.RecordWriter(d / audit.RECORDS_FILE, "RUN-1")
    for r in [_rec(1, {"T1": (VP, CE)}), _rec(3, {"T1": (CI, VP)}), _rec(4, {"T1": (VP, CE)})]:
        w.write(r)
    rp.IssueWriter(d / rp.ISSUES_FILE, "RUN-1").write(_direct_issue(2, CE, CI))
    s, problems = rm.compute_for_runs([d])
    assert problems == []
    assert s.direct_reversal_violations == 1 and s.confirmed_ineligible_to_eligible == 1
    assert s.confirmed_eligible_to_ineligible == 0
    d2 = s.to_dict()
    assert d2["events"][0]["direction"] == "ineligible_to_eligible" and "misjudg" not in str(d2).lower()
