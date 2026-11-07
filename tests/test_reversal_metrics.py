"""확정 판정 역전 지표 (engine.reversal_metrics): 고정된 쌍 상태 이력으로 정의를 검증. API 호출 없음.

이 지표는 판정 상태 변경 건수이며 오판 건수가 아님. 실제 masked 오프라인 실행에서는 확정 상태가 나오지 않아 0이
예상되므로, 기능 검증은 여기의 고정 이력으로 합니다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent))  # test_record_producer의 고정 입력 재사용

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
    assert s.unverified_by_reason == {"withheld_by_violation": 0, "missing_record": 1, "conflicting_records": 0}
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


# ---------------------------------------------------------------------------
# PR #3 리뷰 ①: withheld_pair_transition으로 설명되는 이력 끊김
# ---------------------------------------------------------------------------

def _withheld(step, before, after, alias=A1, trial="T1", run="RUN-1", severity="blocking", code="withheld_pair_transition",
              **kw):
    return rp.RecordIssue(issue_id=f"{run}:{step}:{code}:{alias[-2:]}:{trial}", run_id=run, step=step,
                          action_id=f"ACT-{alias[-2:]}-{step}", code=code, severity=severity, patient_alias=alias,
                          trial_id=trial, before=before, after=after, **kw)


def _gap_history():
    """step1 CE 확정, step2 정상 기록 없음(CE->VP 보류), step3 VP->CI."""
    return [_rec(1, {"T1": (CE, CE)}), _rec(3, {"T1": (VP, CI)})]


def _no_normal_reversal(s):
    assert s.confirmed_eligible_to_ineligible == s.confirmed_ineligible_to_eligible == 0
    assert sum(sum(d.values()) for d in s.reversals_by_path.values()) == 0


def test_withheld_transition_explains_gap_separately_from_missing_record():
    s = rm.compute_reversal_metrics(_gap_history(), [_withheld(2, CE, VP)])
    _no_normal_reversal(s)
    assert s.direct_reversal_violations == 0  # 직접 위반으로도 세지 않음
    assert s.unverified_reversals == 1 and s.history_gaps == 1 and s.gaps_withheld_by_violation == 1
    assert s.unverified_by_reason == {"withheld_by_violation": 1, "missing_record": 0, "conflicting_records": 0}
    (ev,) = s.events
    assert ev.verified is False and ev.unverified_reason == "withheld_by_violation"
    assert ev.direction == "eligible_to_ineligible" and ev.via == (VP,)


def test_same_gap_without_diagnostic_stays_missing_record():
    s = rm.compute_reversal_metrics(_gap_history())
    assert s.unverified_by_reason["missing_record"] == 1 and s.gaps_withheld_by_violation == 0


def test_withheld_gap_without_reversal_is_still_counted_as_withheld_gap():
    records = [_rec(1, {"T1": (CE, CE)}), _rec(3, {"T1": (VP, CE)})]
    s = rm.compute_reversal_metrics(records, [_withheld(2, CE, VP)])
    assert s.events == [] and s.history_gaps == 1 and s.gaps_withheld_by_violation == 1


def test_withheld_transition_arriving_at_opposite_confirmed_is_not_a_normal_reversal():
    records = [_rec(1, {"T1": (CE, VP)}), _rec(3, {"T1": (CI, CI)})]
    s = rm.compute_reversal_metrics(records, [_withheld(2, VP, CI)])
    _no_normal_reversal(s)
    assert s.unverified_by_reason["withheld_by_violation"] == 1 and s.direct_reversal_violations == 0


@pytest.mark.parametrize("diagnostic", [
    _withheld(2, CE, VP, trial="T2"),  # 같은 step, 다른 시험
    _withheld(2, CE, VP, alias=A2),  # 같은 step, 다른 환자
    _withheld(2, CE, VP, run="RUN-2"),  # 같은 step, 다른 실행
    _withheld(2, CE, ER),  # after가 끊긴 before와 다름
    _withheld(2, VP, ER),  # before가 직전 after와 다름
    _withheld(1, CE, VP),  # 끊긴 구간 밖 (앞)
    _withheld(3, CE, VP),  # 끊긴 구간 밖 (뒤)
    _withheld(2, CE, VP, severity="warning"),  # 차단 진단이 아님
    _withheld(2, CE, VP, code="transition_rejected"),  # 다른 진단 코드
    _withheld(2, "NOT_A_STATE", VP),  # 쌍 상태가 아닌 값
], ids=["other_trial", "other_patient", "other_run", "wrong_after", "wrong_before", "before_gap", "after_gap", "warning",
        "other_code", "bad_status"])
def test_unrelated_diagnostic_does_not_explain_gap(diagnostic):
    s = rm.compute_reversal_metrics(_gap_history(), [diagnostic])
    assert s.unverified_by_reason == {"withheld_by_violation": 0, "missing_record": 1, "conflicting_records": 0}
    assert s.events[0].unverified_reason == "missing_record" and s.gaps_withheld_by_violation == 0


def test_criterion_level_withheld_change_does_not_explain_pair_gap():
    crit = _withheld(2, CE, VP, code="withheld_criterion_change", criterion_key="T1:I1")
    assert rm.compute_reversal_metrics(_gap_history(), [crit]).events[0].unverified_reason == "missing_record"


def test_withheld_chain_must_connect_the_whole_gap():
    records = [_rec(1, {"T1": (CE, CE)}), _rec(5, {"T1": (VP, CI)})]
    joined = rm.compute_reversal_metrics(records, [_withheld(2, CE, ER), _withheld(3, ER, VP)])
    assert joined.events[0].unverified_reason == "withheld_by_violation" and joined.gaps_withheld_by_violation == 1
    partial = rm.compute_reversal_metrics(records, [_withheld(2, CE, ER)])  # ER -> VP를 설명하는 진단이 없음
    assert partial.events[0].unverified_reason == "missing_record" and partial.gaps_withheld_by_violation == 0
    reversed_order = rm.compute_reversal_metrics(records, [_withheld(3, CE, ER), _withheld(2, ER, VP)])
    assert reversed_order.events[0].unverified_reason == "missing_record"


def test_missing_record_is_not_hidden_by_a_later_withheld_gap():
    records = [_rec(1, {"T1": (CE, VP)}), _rec(3, {"T1": (ER, ER)}), _rec(5, {"T1": (NE, CI)})]
    s = rm.compute_reversal_metrics(records, [_withheld(4, ER, NE)])  # step2 끊김은 설명되지 않음
    assert s.history_gaps == 2 and s.gaps_withheld_by_violation == 1
    assert s.events[0].unverified_reason == "missing_record"


def test_same_transition_in_direct_and_withheld_is_counted_once_as_direct():
    records = [_rec(1, {"T1": (VP, CE)}), _rec(3, {"T1": (CI, CI)})]
    s = rm.compute_reversal_metrics(records, [_withheld(2, CE, CI), _direct_issue(2, CE, CI)])
    assert s.direct_reversal_violations == 1 and s.unverified_reversals == 0 and s.events == []
    assert s.gaps_withheld_by_violation == 0 and s.history_gaps == 1


def test_direct_violation_then_withheld_is_not_double_counted():
    # 적격 -[직접 위반]-> 부적격 -[보류]-> 대기, 다시 적격: 직접 위반 1건 + 위반 뒤 부적격에서의 미확인 1건
    records = [_rec(1, {"T1": (VP, CE)}), _rec(4, {"T1": (VP, CE)})]
    s = rm.compute_reversal_metrics(records, [_direct_issue(2, CE, CI), _withheld(3, CI, VP)])
    _no_normal_reversal(s)
    assert s.direct_reversal_violations == 1 and s.history_gaps == 1 and s.gaps_withheld_by_violation == 1
    (ev,) = s.events
    assert ev.direction == "ineligible_to_eligible" and ev.unverified_reason == "withheld_by_violation"
    assert (ev.anchor_step, ev.step) == (2, 4)


def test_real_violation_step_separates_direct_and_withheld_by_pair():
    """실제 build_step 산출: T1 직접 역전 때문에 T2의 정상 정책 전이가 보류된 단계."""
    import dataclasses
    import test_record_producer as tp
    before = tp.cohort(tp.pair("T1", C.PairStatus.CONFIRMED_ELIGIBLE, [tp.crit("T1", "I1", "MET")], "ELIGIBLE"),
                       tp.pair("T2", C.PairStatus.INFO_MISSING, [tp.crit("T2", "I1", "UNKNOWN")]))
    after = tp.cohort(tp.pair("T1", C.PairStatus.CONFIRMED_INELIGIBLE, [tp.crit("T1", "I1", "NOT_MET")], "INELIGIBLE"),
                      tp.pair("T2", C.PairStatus.BLOCKED_FAILURE, [tp.crit("T2", "I1", "UNKNOWN")],
                              pending=("sources_exhausted",)))
    moved = C.PairTransition(pair=C.PairKey(patient_alias=tp.A, trial_id="T2"), before=C.PairStatus.INFO_MISSING,
                             after=C.PairStatus.BLOCKED_FAILURE, authority="policy", reason_code="sources_exhausted",
                             pending_kinds=("sources_exhausted",))
    o = tp.obs(tp.action(trials=("T1", "T2"), targets=("T1:I1", "T2:I1")), before=before, after=after,
               policy_transitions=(moved,), reevaluated_trials=frozenset({"T1"}))
    r = rp.build_step(dataclasses.replace(o, run_id="RUN-1", step=3))
    assert r.status == "contract_violation" and r.record is None
    records = [_rec(1, {"T1": (VP, CE), "T2": (CE, VP)}, alias=tp.A), _rec(2, {"T1": (CE, CE), "T2": (VP, IM)}, alias=tp.A),
               _rec(4, {"T1": (CI, CI), "T2": (BF, CI)}, alias=tp.A)]
    s = rm.compute_reversal_metrics(records, r.issues)
    _no_normal_reversal(s)
    assert s.direct_violations_by_direction == {"eligible_to_ineligible": 1, "ineligible_to_eligible": 0}
    assert s.history_gaps == 2 and s.gaps_withheld_by_violation == 1
    (ev,) = s.events  # T1은 직접 위반 쪽, T2만 보류로 미확인
    assert (ev.trial_id, ev.unverified_reason) == ("T2", "withheld_by_violation")
    assert s.unverified_reversals == 1


# ---------------------------------------------------------------------------
# PR #3 리뷰 ②: 파일 입력의 중복·충돌
# ---------------------------------------------------------------------------

def _write_records(run_dir, records, name=audit.RECORDS_FILE):
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / name).open("a", encoding="utf-8") as f:  # RecordWriter는 중복을 거부하므로 원본 줄을 직접 씀
        for r in records:
            f.write(r.model_dump_json() + "\n")


def _write_trace(run_dir, records):
    from ctrec.trace import Tracer
    tr = Tracer(run_dir / "trace.jsonl", verbose=False)
    tr.log("matcher", "match")  # 기존 이벤트가 섞여 있어도 v2 이벤트만 읽음
    for r in records:
        audit.to_trace(tr, r)


def test_file_conflict_is_not_computed_as_first_record_wins(tmp_path):
    # audit.parse_records만 믿으면 step1의 첫 줄이 살아 E->I가 정상 역전으로 세어짐
    d = tmp_path / "run"
    _write_records(d, _chain([CE, VP, CI]) + [_rec(1, {"T1": (CE, ER)})])
    assert len(audit.load_run_v2(d).records) == 2  # 기존 로더 동작은 그대로
    s, problems = rm.compute_for_runs([d])
    assert s.conflicting_steps == 1 and s.confirmed_eligible_to_ineligible == 0 and s.events == []
    assert problems == ["run:line 3: duplicate_step:1", "run:conflicting_steps:1"]


def test_file_conflict_inside_history_surfaces_as_conflicting_records(tmp_path):
    d = tmp_path / "run"
    _write_records(d, [_rec(1, {"T1": (CE, CE)}), _rec(2, {"T1": (CE, VP)}), _rec(2, {"T1": (CE, ER)}),
                       _rec(3, {"T1": (VP, CI)})])
    s, problems = rm.compute_for_runs([d])
    assert s.conflicting_steps == 1 and s.confirmed_eligible_to_ineligible == 0 and s.history_gaps == 1
    assert s.unverified_by_reason["conflicting_records"] == 1
    assert s.events[0].unverified_reason == "conflicting_records"
    assert any("duplicate_step:2" in p for p in problems) and "run:conflicting_steps:1" in problems


def test_file_identical_duplicate_counts_once_and_is_reported(tmp_path):
    d = tmp_path / "run"
    hist = _chain([CE, VP, CI])
    _write_records(d, hist + hist[:1])
    s, problems = rm.compute_for_runs([d])
    assert s.confirmed_eligible_to_ineligible == 1 and s.duplicate_records_ignored == 1 and s.conflicting_steps == 0
    assert problems == ["run:line 3: duplicate_step:1"]


def test_trace_conflict_is_handled_like_file_conflict(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    _write_trace(d, _chain([CE, VP, CI]) + [_rec(1, {"T1": (CE, ER)})])
    s, problems = rm.compute_for_runs([d])
    assert s.conflicting_steps == 1 and s.confirmed_eligible_to_ineligible == 0
    assert "run:conflicting_steps:1" in problems


def test_corrupt_record_line_keeps_other_records_and_is_reported(tmp_path):
    d = tmp_path / "run"
    hist = _chain([CE, VP, CI])
    _write_records(d, hist[:1])
    with (d / audit.RECORDS_FILE).open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    _write_records(d, hist[1:])
    s, problems = rm.compute_for_runs([d])
    assert s.confirmed_eligible_to_ineligible == 1 and problems == ["run:line 2: corrupt_json"]


# ---------------------------------------------------------------------------
# PR #3 리뷰 ③: 기록 파일 누락과 trace 대체 경로
# ---------------------------------------------------------------------------

def test_trace_fallback_gives_same_reversal_information_as_records_file(tmp_path):
    records = (_chain([CE, VP, BF, CI]) + [_rec(4, {"T1": (CI, ER)}), _rec(5, {"T1": (ER, CE)}, facts=("F-000000000001",))]
               + _chain([CI, VP], trial="T2", start=10))
    _write_records(tmp_path / "file", records)
    (tmp_path / "trace").mkdir()
    _write_trace(tmp_path / "trace", records)
    from_file, file_problems = rm.compute_for_runs([tmp_path / "file"])
    from_trace, trace_problems = rm.compute_for_runs([tmp_path / "trace"])
    assert from_file.confirmed_eligible_to_ineligible == 1 and from_file.confirmed_ineligible_to_eligible == 1
    assert from_trace.to_dict() == from_file.to_dict()
    assert file_problems == []
    assert trace_problems == ["trace:records_from_trace",
                              "trace:issues_file_absent:direct_violations_and_withheld_not_computable"]


def test_trace_without_issue_file_cannot_compute_violations_and_says_so(tmp_path):
    # 차단 단계는 trace에도 없음: 진단 파일이 없으면 끊김은 missing_record이고 위반 0은 '없음'이 아니라 '계산 불가'
    d = tmp_path / "run"
    d.mkdir()
    _write_trace(d, _gap_history())
    s, problems = rm.compute_for_runs([d])
    assert s.direct_reversal_violations == 0 and s.unverified_by_reason["missing_record"] == 1
    assert any("not_computable" in p for p in problems)


def test_trace_with_issue_file_classifies_blocked_steps(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    _write_trace(d, _gap_history())
    rp.IssueWriter(d / rp.ISSUES_FILE, "RUN-1").write(_withheld(2, CE, VP))
    s, problems = rm.compute_for_runs([d])
    assert s.unverified_by_reason["withheld_by_violation"] == 1
    assert "run:records_from_trace" in problems and not any("not_computable" in p for p in problems)


def test_records_file_takes_priority_over_trace(tmp_path):
    d = tmp_path / "run"
    _write_records(d, _chain([CE, VP, CI]))
    _write_trace(d, _chain([CI, VP, CE]))
    s, problems = rm.compute_for_runs([d])
    assert s.confirmed_eligible_to_ineligible == 1 and s.confirmed_ineligible_to_eligible == 0 and problems == []


def test_no_records_and_no_trace_is_a_problem_not_a_clean_zero(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    s, problems = rm.compute_for_runs([d])
    assert s.runs == 0 and s.events == [] and problems == ["run:no_v2_records"]


@pytest.mark.parametrize("content", ["", "\n\n"], ids=["empty", "blank_lines"])
def test_empty_records_file_is_a_problem_even_with_a_trace(tmp_path, content):
    # 기록 파일이 있으면 로더가 trace로 폴백하지 않고 문제 없이 0건을 돌려줌: 깨끗한 0건으로 두면 안 됨
    d = tmp_path / "run"
    d.mkdir()
    (d / audit.RECORDS_FILE).write_text(content, encoding="utf-8")
    (d / "trace.jsonl").write_text('{"t":0,"agent":"matcher","event":"match"}\n', encoding="utf-8")
    assert audit.load_run_v2(d).issues == []  # 기존 로더 동작은 그대로
    s, problems = rm.compute_for_runs([d])
    assert s.runs == 0 and problems == ["run:no_v2_records"]


def test_real_run_trace_shape_is_reported_as_no_v2_records(tmp_path):
    # 실제 Tracer 산출 모양: t/agent/event + 에이전트별 필드, `record` 키 없음
    from ctrec.trace import Tracer
    d = tmp_path / "run"
    tr = Tracer(d / "trace.jsonl", verbose=False)
    tr.log("patient_profiler", "profile", message="x")
    tr.log("decision", "round", round=1)
    tr.log("engine", "other_event", record={"not": "a record"})  # 같은 agent라도 v2 이벤트가 아니면 읽지 않음
    s, problems = rm.compute_for_runs([d])
    assert s.runs == 0 and s.pairs_observed == 0 and problems == ["run:no_v2_records"]


def test_legacy_run_with_trace_but_no_v2_events_is_reported(tmp_path):
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "trace.jsonl").write_text('{"t":0,"agent":"matcher","event":"match"}\nnot json\n', encoding="utf-8")
    s, problems = rm.compute_for_runs([d])
    assert s.runs == 0 and problems == ["legacy:trace line 2: corrupt_json", "legacy:no_v2_records"]


def test_missing_run_dir_is_a_problem_and_does_not_stop_other_runs(tmp_path):
    _write_records(tmp_path / "good", _chain([CE, VP, CI]))
    s, problems = rm.compute_for_runs([tmp_path / "gone", tmp_path / "good"])
    assert s.confirmed_eligible_to_ineligible == 1 and problems == ["gone:records_unreadable"]


def test_unreadable_records_file_is_a_problem_and_issues_are_still_read(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    (d / audit.RECORDS_FILE).write_bytes(b"\xff\xfe not utf-8\n")
    rp.IssueWriter(d / rp.ISSUES_FILE, "RUN-1").write(_direct_issue(2, CE, CI))
    s, problems = rm.compute_for_runs([d])
    assert problems == ["run:records_unreadable"] and s.direct_reversal_violations == 1 and s.runs == 0


# ---------------------------------------------------------------------------
# PR #3 리뷰 ④: 손상된 진단 파일 부분 복구 (record_producer.load_issues는 그대로)
# ---------------------------------------------------------------------------

SENSITIVE = "환자원문 홍길동 010-1234-5678"


def _write_issue_lines(run_dir, lines):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / rp.ISSUES_FILE).write_bytes(b"\n".join(lines) + b"\n")


def _line(issue):
    return issue.model_dump_json().encode("utf-8")


def test_corrupt_issue_line_keeps_valid_diagnostics(tmp_path):
    d = tmp_path / "run"
    _write_records(d, [_rec(1, {"T1": (VP, CE)}), _rec(3, {"T1": (CI, CI)}), _rec(5, {"T1": (VP, CI)})])
    _write_issue_lines(d, [_line(_direct_issue(2, CE, CI)), b"{broken " + SENSITIVE.encode("utf-8"),
                           _line(_withheld(4, CI, VP))])
    s, problems = rm.compute_for_runs([d])
    assert problems == ["run:issues line 2: corrupt_json"]
    assert s.direct_reversal_violations == 1 and s.gaps_withheld_by_violation == 1
    assert SENSITIVE not in str(problems)


@pytest.mark.parametrize("bad, code", [
    (b"\xff\xfe\xfd", "corrupt_json"),
    (b'["list"]', "not_object"),
    (json.dumps({"issue_id": SENSITIVE, "code": SENSITIVE}, ensure_ascii=False).encode("utf-8"), "invalid_issue"),
    (json.dumps({SENSITIVE: 1}, ensure_ascii=False).encode("utf-8"), "invalid_issue"),  # 추가 필드: 키가 값
    (b"[" * 200000, "corrupt_json"),
], ids=["not_utf8", "not_object", "invalid_values", "extra_key", "deep_nesting"])
def test_each_kind_of_damaged_issue_line_is_reported_without_its_content(tmp_path, bad, code):
    d = tmp_path / "run"
    _write_records(d, [_rec(1, {"T1": (VP, CE)}), _rec(3, {"T1": (CI, CI)})])
    _write_issue_lines(d, [bad, _line(_direct_issue(2, CE, CI))])
    s, problems = rm.compute_for_runs([d])
    assert s.direct_reversal_violations == 1 and len(problems) == 1
    assert problems[0].startswith(f"run:issues line 1: {code}")
    assert SENSITIVE not in problems[0] and "홍길동" not in problems[0]


def test_invalid_issue_reports_only_known_field_names(tmp_path):
    d = tmp_path / "run"
    bad = json.loads(_direct_issue(2, CE, CI).model_dump_json())
    bad["severity"] = SENSITIVE
    _write_issue_lines(d, [json.dumps(bad, ensure_ascii=False).encode("utf-8")])
    _, _, problems = rm.load_run_inputs(d)
    assert "issues line 1: invalid_issue:severity" in problems and SENSITIVE not in str(problems)


def test_duplicate_issue_line_is_reported_and_counted_once(tmp_path):
    d = tmp_path / "run"
    _write_issue_lines(d, [_line(_direct_issue(2, CE, CI))] * 2)
    _, issues, problems = rm.load_run_inputs(d)
    assert len(issues) == 1 and "issues line 2: duplicate_issue" in problems


def test_record_load_problems_do_not_carry_file_values(tmp_path):
    d = tmp_path / "run"
    bad_extra = json.loads(_rec(1, {"T1": (CE, VP)}).model_dump_json())
    bad_extra[SENSITIVE] = 1
    other_schema = {"schema_version": SENSITIVE}
    (d).mkdir()
    (d / audit.RECORDS_FILE).write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (bad_extra, other_schema)) + "\n",
                                        encoding="utf-8")
    assert SENSITIVE not in str(audit.load_run_v2(d).issues)  # 로더 문제 목록에도 파일 값이 없음
    _, problems = rm.compute_for_runs([d])
    assert problems == ["run:line 1: invalid_record", "run:line 2: unsupported_schema"]


def test_one_damaged_run_dir_does_not_stop_the_others(tmp_path):
    good, damaged, garbage = tmp_path / "good", tmp_path / "damaged", tmp_path / "garbage"
    _write_records(good, _chain([CE, VP, CI]))
    _write_records(damaged, _chain([CI, VP, CE]))
    _write_issue_lines(damaged, [b"\x00\xff garbage", b"{}"])
    garbage.mkdir()
    (garbage / audit.RECORDS_FILE).write_bytes(b"\xff\xff")
    (garbage / rp.ISSUES_FILE).write_bytes(b"\xff\xff")
    s, problems = rm.compute_for_runs([good, damaged, garbage])
    assert s.confirmed_eligible_to_ineligible == 1 and s.confirmed_ineligible_to_eligible == 1 and s.runs == 2
    assert problems[0] == "damaged:issues line 1: corrupt_json"
    assert problems[1].startswith("damaged:issues line 2: invalid_issue:")
    assert problems[2:] == ["garbage:records_unreadable", "garbage:issues line 1: corrupt_json"]


def test_issue_file_outside_run_dir_is_refused(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(_line(_direct_issue(2, CE, CI)) + b"\n")
    try:
        (d / rp.ISSUES_FILE).symlink_to(outside)
    except OSError:
        pytest.skip("심볼릭 링크를 만들 수 없는 환경")
    _, issues, problems = rm.load_run_inputs(d)
    assert issues == [] and "issues_file_link" in problems


def test_load_issues_contract_stays_strict(tmp_path):
    # IssueWriter가 손상 파일에 이어 쓰지 않도록 load_issues는 그대로 예외를 냄 (부분 복구는 지표 전용)
    p = tmp_path / rp.ISSUES_FILE
    p.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(ValueError):
        rp.load_issues(p)
    with pytest.raises(ValueError):
        rp.IssueWriter(p, "RUN-1")


# ---------------------------------------------------------------------------
# 출력·회귀
# ---------------------------------------------------------------------------

def test_write_metrics_keeps_load_problems_next_to_the_numbers(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    s, problems = rm.compute_for_runs([d])
    out = tmp_path / "metrics.json"
    rm.write_metrics(s, out, problems)
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["load_problems"] == ["run-1:no_v2_records"] and saved["runs"] == 0  # 폴더 이름은 파일에 남지 않음
    assert "gaps_withheld_by_violation" in saved and "withheld_by_violation" in saved["unverified_by_reason"]


def test_pairs_observed_counts_only_recorded_pairs_not_evaluation_targets():
    # 행동이 T2도 겨냥했더라도 PairTransition이 없으면 쌍으로 관찰되지 않음: 분모로 쓸 수 없는 이유
    a = _action(A1, 1, ("T1", "T2", "T3"))
    rec = C.DecisionRecord(run_id="RUN-1", step=1, action=a, outcome=None, policy_version="test-0.2",
                           pairs=(C.PairTransition(pair=C.PairKey(patient_alias=A1, trial_id="T1"), before=VP, after=CE),))
    assert rm.compute_reversal_metrics([rec]).pairs_observed == 1
    assert "pairs_observed" in rm.__doc__ and "분모" in rm.__doc__


def test_directions_paths_and_folder_scopes_still_hold_across_damaged_sibling(tmp_path):
    a, b, broken = tmp_path / "a", tmp_path / "b", tmp_path / "broken"
    _write_records(a, _chain([CE, VP, CI]) + _chain([CI, VP, IM, CE], trial="T2", start=10))
    _write_records(b, _chain([CE, NE, CI]))  # 같은 run_id·쌍이지만 폴더가 달라 이력을 잇지 않음
    _write_records(broken, _chain([CE, VP]))
    _write_issue_lines(broken, [b"{broken"])
    s, problems = rm.compute_for_runs([a, b, broken])
    assert s.confirmed_eligible_to_ineligible == 2 and s.confirmed_ineligible_to_eligible == 1
    assert s.reversals_by_path["eligible_to_ineligible"] == {"verification_only": 1, "via_info_missing": 0,
                                                             "failure_or_not_evaluated": 1}
    assert s.reversals_by_path["ineligible_to_eligible"]["via_info_missing"] == 1
    assert s.runs == 3 and s.pairs_observed == 4 and s.history_gaps == 0 and s.downgraded_pending_from_eligible == 1
    assert problems == ["broken:issues line 1: corrupt_json"]
