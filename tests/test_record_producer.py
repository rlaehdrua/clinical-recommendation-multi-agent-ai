"""WP7 공통 DecisionRecord 생산기 (engine.record_producer). 실제 API 호출 없음."""

from __future__ import annotations

import ast
import dataclasses
import json
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ctrec.engine import audit  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine import record_producer as rp  # noqa: E402
from ctrec.engine.cohort_state import CohortState, PairState, PatientSlice  # noqa: E402
from ctrec.fact_changes import take_snapshot  # noqa: E402
from ctrec.fact_store import FactStore  # noqa: E402

A = "PA-0123456789abcdef"
PS = C.PairStatus
RULE_EV = "legacy_decision_state:rule_engine"


# ------------------------------------------------------------ 고정 입력
def crit(tid, rid, state, ev=RULE_EV):
    return C.CriterionResult(pair=C.PairKey(patient_alias=A, trial_id=tid), rule_id=rid, state=state,
                             adjudication="rule", decided_by="rule_engine" if state != "UNKNOWN" else "none",
                             evaluator_version=ev, gap="none" if state != "UNKNOWN" else "missing")


def pair(tid, status, crits, elig="UNCERTAIN", pending=()):
    return PairState(C.PairKey(patient_alias=A, trial_id=tid), status, elig, True, tuple(crits), frozenset(),
                     frozenset(pending), None)


def cohort(*pairs):
    return CohortState(MappingProxyType({A: PatientSlice(A, tuple(pairs), "V-x", None)}), MappingProxyType({}))


def action(aid="fp1", targets=("T1:I1",), trials=("T1",), atype="ASK_PATIENT"):
    return C.Action(action_id=f"{A}:{aid}", action_type=atype, patient_alias=A, reason_code="missing_required_info",
                    tier=2, fingerprint=aid, policy_version="0.2-provisional", target_trial_ids=trials,
                    target_criteria=targets)


def asked(a, qid="Q1", ref="qa:round1:Q1"):
    return rp.AskedQuestion(question_id=qid, action_id=a.action_id, target_criteria=a.target_criteria,
                            purpose=a.purpose, source_ref=ref)


class Facts:
    """실제 FactStore로 전후 스냅샷을 만듦 (ID는 FactStore가 만든 값)."""

    def __init__(self):
        self.store = FactStore()
        self.store.add(A, "age", 61, source_type="clinical_record",
                       source_reference={"kind": "input_text", "quote": "61-year-old"})
        self.before = take_snapshot(self.store, A)

    def add_answer(self, field, value, quote, ref="qa:round1:Q1"):
        return self.store.add(A, field, value, source_type="self_report",
                              source_reference={"kind": "answer", "ref": ref, "quote": quote}).fact_id

    def after(self):
        return take_snapshot(self.store, A)


BEFORE = cohort(pair("T1", PS.INFO_MISSING, [crit("T1", "I1", "UNKNOWN"), crit("T1", "I2", "MET")]),
                pair("T2", PS.INFO_MISSING, [crit("T2", "I1", "UNKNOWN")]))
AFTER_OK = cohort(pair("T1", PS.CONFIRMED_ELIGIBLE, [crit("T1", "I1", "MET"), crit("T1", "I2", "MET")], "ELIGIBLE"),
                  pair("T2", PS.INFO_MISSING, [crit("T2", "I1", "UNKNOWN")]))


def obs(a=None, *, execution="answered", before=BEFORE, after=AFTER_OK, facts=None, **kw):
    a = a or action()
    f = facts or Facts()
    if "facts_after" not in kw:
        kw["facts_after"] = f.after()
    if execution in ("answered", "declined") and "asked" not in kw:
        kw["asked"] = asked(a)
    return rp.StepObservation(run_id="RUN-1", step=kw.pop("step", 1), action=a, execution=execution, before=before,
                              after=after, facts_before=f.before, reevaluated_trials=kw.pop("reevaluated_trials",
                                                                                            frozenset({"T1"})), **kw)


# ------------------------------------------------------------ 1. 정상 응답
def test_answer_produces_normal_record():
    f = Facts()
    fid = f.add_answer("ecog", 1, "ECOG 1")
    r = rp.build_step(obs(facts=f))
    assert r.status == "recorded" and r.issues == ()
    rec = r.record
    assert rec.outcome.action_id == rec.action.action_id and rec.outcome.status == "succeeded"
    assert rec.facts_added == (fid,) and rec.outcome.new_fact_ids == (fid,)
    ch = {c.criterion_key: c for c in rec.criteria}
    assert (ch["T1:I1"].before, ch["T1:I1"].after, ch["T1:I1"].reevaluated) == ("UNKNOWN", "MET", True)
    assert ch["T1:I1"].evaluator_version == RULE_EV and rec.evaluator_version == RULE_EV
    tr = {p.pair.trial_id: p for p in rec.pairs}
    assert (tr["T1"].before, tr["T1"].after, tr["T1"].authority) == (PS.INFO_MISSING, PS.CONFIRMED_ELIGIBLE,
                                                                     "adjudication")
    assert rec.fact_version_before == f.before.version and rec.fact_version_after == f.after().version
    assert rec.fact_version_before != rec.fact_version_after
    assert rec.policy_version == "0.2-provisional"


def test_cost_and_charge_keys_are_the_observed_ones():
    cost = C.EffortCost(coordinator_minutes=4.0, patient_wait_minutes=30.0)
    r = rp.build_step(obs(cost=cost, charge_keys=(f"{A}|patient_interview",), latency_minutes=12.0))
    assert r.record.outcome.cost_charged == cost and r.record.outcome.charge_keys == (f"{A}|patient_interview",)
    assert r.record.outcome.latency_minutes == 12.0
    assert rp.build_step(obs()).record.outcome.cost_charged == C.ZERO_COST  # 관찰하지 않은 비용은 0 그대로


# ------------------------------------------------------------ 2. 무응답
def test_declined_answer_is_no_response_and_marker_fact_is_kept_as_issue():
    f = Facts()
    marker = f.add_answer("other", "모름", "모름")  # 오프라인 대역이 무응답 문장을 사실로 만드는 실제 현상
    r = rp.build_step(obs(execution="declined", facts=f, after=BEFORE))
    rec = r.record
    assert rec.outcome.status == "no_response" and rec.outcome.error_code == "no_answer"
    assert rec.facts_added == () and rec.outcome.new_fact_ids == ()
    assert r.status == "recorded_with_warnings"
    assert [(i.code, i.fact_ids) for i in r.issues] == [("decline_marker_fact", (marker,))]
    assert rec.fact_version_after == f.after().version  # 실제 스냅샷 버전은 그대로 (숨기지 않음)
    with pytest.raises(rp.ObservationError, match="facts_linked_to_unanswered_action"):
        rp.build_step(obs(execution="declined", facts=f, after=BEFORE, explicit_fact_links=(marker,)))


# ------------------------------------------------------------ 3. 실행되지 않은 행동
def test_unexecuted_action_is_distinguished_from_execution():
    a = action("fp2", atype="REQUEST_DOCUMENT")
    r = rp.build_step(obs(a, execution="not_executed", after=BEFORE, reevaluated_trials=frozenset()))
    assert r.status == "recorded" and r.record.outcome.status == "not_executed"
    assert r.record.criteria == () and r.record.pairs == () and r.record.facts_added == ()
    p = rp.build_step(obs(a, execution="proposed", after=BEFORE, reevaluated_trials=frozenset()))
    assert p.record.outcome is None  # 제안·선택만
    with pytest.raises(rp.ObservationError, match="unexecuted_action_charged"):
        rp.build_step(obs(a, execution="not_executed", after=BEFORE, reevaluated_trials=frozenset(),
                          cost=C.EffortCost(coordinator_minutes=1.0)))
    with pytest.raises(rp.ObservationError, match="asked_but_not_executed"):
        rp.build_step(obs(execution="not_executed", after=BEFORE, asked=asked(action())))
    # 실행되지 않았는데 상태가 바뀌었다고 관찰되면 정상 기록으로 만들지 않음
    b = rp.build_step(obs(a, execution="not_executed", reevaluated_trials=frozenset()))
    assert b.status == "contract_violation" and b.record is None


# ------------------------------------------------------------ 4. 질문-행동 불일치
@pytest.mark.parametrize("field,value,code", [
    ("action_id", f"{A}:other", "question_action_id_mismatch"),
    ("target_criteria", ("T1:I2",), "question_targets_mismatch"),
    ("purpose", "physician_reference", "question_purpose_mismatch")])
def test_question_action_mismatch_is_explicit_error(field, value, code):
    a = action()
    q = asked(a).model_copy(update={field: value})
    with pytest.raises(rp.ObservationError, match=code):
        rp.build_step(obs(a, asked=q))
    with pytest.raises(rp.ObservationError, match="asked_question_required"):
        rp.build_step(obs(a, asked=None))


# ------------------------------------------------------------ 5. 스냅샷에 없는 사실 ID
@pytest.mark.parametrize("bad", ["F6", "F-000000000000", "M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1"])
def test_fact_ids_must_come_from_snapshot_diff(bad):
    f = Facts()
    f.add_answer("ecog", 1, "ECOG 1")
    with pytest.raises(rp.ObservationError, match="linked_fact_not_in_snapshot_diff"):
        rp.build_step(obs(facts=f, explicit_fact_links=(bad,)))
    old = f.before.facts[0].fact_id  # 이전부터 있던 사실도 이 행동의 결과가 아님
    with pytest.raises(rp.ObservationError, match="linked_fact_not_in_snapshot_diff"):
        rp.build_step(obs(facts=f, explicit_fact_links=(old,)))


def test_removed_fact_blocks_record():
    f = Facts()
    empty = take_snapshot(FactStore(), A)
    r = rp.build_step(obs(facts=f, facts_after=empty))
    assert r.status == "contract_violation" and r.record is None
    assert r.issues[0].code == "fact_missing_after" and r.issues[0].fact_ids == (f.before.facts[0].fact_id,)


# ------------------------------------------------------------ 6. 귀속 불가 사실
def test_quote_match_alone_does_not_attribute_and_unattributed_fact_is_kept():
    f = Facts()
    by_ref = f.add_answer("ecog", 1, "ECOG 1")  # 질문 출처 ref가 정확히 맞음 -> 귀속
    generic = f.add_answer("smoker", True, "smoker", ref="raw_text:answers")  # 현재 profile_facts 형식: 질문 불명
    other_q = f.add_answer("egfr", "positive", "EGFR positive", ref="qa:round1:Q2")  # 다른 질문
    reextracted = f.store.add(A, "sex", "female", source_type="clinical_record",
                              source_reference={"kind": "input_text", "quote": "woman"}).fact_id
    r = rp.build_step(obs(facts=f))
    assert r.record.facts_added == (by_ref,)
    un = {i.fact_ids[0]: i for i in r.issues if i.code == "unattributed_fact"}
    assert set(un) == {generic, other_q, reextracted}
    assert un[reextracted].detail == ("source_kind:input_text",) and all(i.severity == "warning" for i in un.values())
    # 실행기가 명시적으로 연결하면 그 사실만 추가 귀속
    r2 = rp.build_step(obs(facts=f, explicit_fact_links=(generic,)))
    assert set(r2.record.facts_added) == {by_ref, generic}


# ------------------------------------------------------------ 7. 직접 역전
def test_direct_reversal_is_blocked_with_original_states():
    before = cohort(pair("T1", PS.CONFIRMED_ELIGIBLE, [crit("T1", "I1", "MET")], "ELIGIBLE"))
    after = cohort(pair("T1", PS.CONFIRMED_INELIGIBLE, [crit("T1", "I1", "NOT_MET")], "INELIGIBLE"))
    f = Facts()
    fid = f.add_answer("ecog", 3, "ECOG 3")
    r = rp.build_step(obs(facts=f, before=before, after=after))
    assert r.status == "contract_violation" and r.record is None
    (i,) = [x for x in r.blocking if x.code == "direct_confirmed_reversal"]
    assert (i.code, i.trial_id, i.before, i.after) == ("direct_confirmed_reversal", "T1", "CONFIRMED_ELIGIBLE",
                                                       "CONFIRMED_INELIGIBLE")
    # 보류된 단계의 다른 실제 변화도 진단으로 남음 (기준 변화, 결과·귀속 사실)
    withheld = {(x.code, x.criterion_key, x.fact_ids) for x in r.blocking if x.code.startswith("withheld_")}
    assert withheld == {("withheld_criterion_change", "T1:I1", ()), ("withheld_outcome", None, (fid,))}
    assert (i.run_id, i.step, i.action_id, i.fact_ids) == ("RUN-1", 1, f"{A}:fp1", (fid,))
    st = rp.run_status([rp.build_step(obs()), r])
    assert not st.complete and st.status == "incomplete_contract_violation" and st.violation_steps == (1,)
    assert st.records == 1
    with pytest.raises(C.TransitionError):  # 계약 검사는 그대로 (우회하지 않음)
        C.check_transition(PS.CONFIRMED_ELIGIBLE, PS.CONFIRMED_INELIGIBLE, authority="adjudication")


def test_confirmed_downgrade_reason_rules():
    before = cohort(pair("T1", PS.CONFIRMED_ELIGIBLE, [crit("T1", "I1", "MET")], "ELIGIBLE"))
    vp = cohort(pair("T1", PS.VERIFICATION_PENDING, [crit("T1", "I1", "MET")], "ELIGIBLE", ("conflict",)))
    r = rp.build_step(obs(before=before, after=vp))
    assert r.status == "recorded_with_warnings" and r.issues[0].code == "unexplained_confirmed_downgrade"
    assert audit.reversal_issues([r.record]) == ["unexplained_confirmed_downgrade:1"]  # 로드 때도 잡힘
    ok = rp.build_step(obs(before=before, after=vp, pair_reasons={"T1": "contradicting_evidence"}))
    assert ok.status == "recorded" and ok.record.pairs[0].reason_code == "contradicting_evidence"
    ne = cohort(pair("T1", PS.NOT_EVALUATED, [], "NOT_EVALUATED"))
    e = rp.build_step(obs(before=before, after=ne))
    assert e.status == "recorded" and e.record.pairs[0].reason_code == "evaluation_failed"


# ------------------------------------------------------------ 8. 재평가 범위
def test_not_reevaluated_criteria_are_not_marked_reevaluated():
    r = rp.build_step(obs())
    ch = {c.criterion_key: c for c in r.record.criteria}
    assert ch["T2:I1"].reevaluated is False and ch["T2:I1"].reason_code == "not_reevaluated"
    assert ch["T2:I1"].evaluator_version is None and ch["T1:I2"].reevaluated is True
    narrow = rp.build_step(obs(reevaluated_trials=frozenset(), reevaluated_criteria=frozenset({"T1:I1"})))
    nch = {c.criterion_key: c for c in narrow.record.criteria}
    assert nch["T1:I1"].reevaluated and not nch["T1:I2"].reevaluated
    # 재평가하지 않았다는데 판정이 바뀌면 계약상 표현 불가 -> 차단
    b = rp.build_step(obs(reevaluated_trials=frozenset()))
    assert b.status == "contract_violation" and b.issues[0].code == "criterion_changed_without_reevaluation"
    assert (b.issues[0].criterion_key, b.issues[0].before, b.issues[0].after) == ("T1:I1", "UNKNOWN", "MET")
    with pytest.raises(rp.ObservationError, match="reevaluated_scope_not_in_after_state"):
        rp.build_step(obs(reevaluated_trials=frozenset({"T9"})))


# ------------------------------------------------------------ 9. 정답·원래 ID 차단
def test_observation_has_no_truth_inputs_and_rejects_original_ids():
    names = {f.name for f in dataclasses.fields(rp.StepObservation)}
    for word in ("gt", "truth", "answerer", "hidden", "masked", "case_id", "patient_id"):
        assert not any(word in n for n in names), word
    with pytest.raises(ValueError):  # 원래 케이스 ID는 Action 별칭 형식에서 거부
        C.Action(action_id="X:fp", action_type="ASK_PATIENT", patient_alias="M-SYN-DEMO-A-rescue-1",
                 reason_code="r", tier=2, fingerprint="fp", policy_version="p")
    store = FactStore()
    store.add("M-SYN-DEMO-A-rescue-1", "age", 61)
    with pytest.raises(rp.ObservationError, match="facts_after_patient_mismatch"):
        rp.build_step(obs(facts_after=take_snapshot(store, "M-SYN-DEMO-A-rescue-1")))


def test_records_and_issues_contain_only_ids_and_codes(tmp_path):
    f = Facts()
    f.add_answer("ecog", 1, "ECOG performance status is 1")
    f.add_answer("smoker", True, "former smoker since 1990", ref="raw_text:answers")
    r = rp.build_step(obs(facts=f))
    blob = r.record.model_dump_json() + "".join(i.model_dump_json() for i in r.issues)
    for text in ("ECOG performance", "former smoker", "61-year-old", "M-SYN", "rescue", "F6\""):
        assert text not in blob
    w = rp.IssueWriter(tmp_path / rp.ISSUES_FILE, "RUN-1", forbidden_texts=["former smoker"])
    for i in r.issues:
        w.write(i)
    bad = r.issues[0].model_copy(update={"issue_id": "RUN-1:1:x:M-SYN-DEMO"})
    with pytest.raises(rp.ObservationError):
        rp.IssueWriter(tmp_path / "other.jsonl", "RUN-1", scanner=type("S", (), {"scan": lambda s, t: ["x"] if "M-SYN"
                                                                                  in t else []})()).write(bad)


# ------------------------------------------------------------ 10. 결정성
def test_same_observation_same_output():
    f = Facts()
    f.add_answer("ecog", 1, "ECOG 1")
    f.add_answer("smoker", True, "smoker", ref="raw_text:answers")
    o = obs(facts=f)
    a, b = rp.build_step(o), rp.build_step(o)
    assert a.record.model_dump_json() == b.record.model_dump_json()
    assert [i.model_dump_json() for i in a.issues] == [i.model_dump_json() for i in b.issues]


def test_withheld_step_keeps_other_valid_transitions_as_issues():
    """위반 단계여도 다른 시험의 정상 전이(예: 정책 소진 BLOCKED_FAILURE)가 감사에서 빠지지 않음."""
    before = cohort(pair("T1", PS.CONFIRMED_ELIGIBLE, [crit("T1", "I1", "MET")], "ELIGIBLE"),
                    pair("T2", PS.INFO_MISSING, [crit("T2", "I1", "UNKNOWN")]))
    after = cohort(pair("T1", PS.CONFIRMED_INELIGIBLE, [crit("T1", "I1", "NOT_MET")], "INELIGIBLE"),
                   pair("T2", PS.BLOCKED_FAILURE, [crit("T2", "I1", "UNKNOWN")], pending=("sources_exhausted",)))
    pt = C.PairTransition(pair=C.PairKey(patient_alias=A, trial_id="T2"), before=PS.INFO_MISSING,
                          after=PS.BLOCKED_FAILURE, authority="policy", reason_code="sources_exhausted",
                          pending_kinds=("sources_exhausted",))
    r = rp.build_step(obs(action(trials=("T1", "T2"), targets=("T1:I1", "T2:I1")), before=before, after=after,
                          policy_transitions=(pt,), reevaluated_trials=frozenset({"T1"})))
    assert r.status == "contract_violation" and r.record is None
    w = [i for i in r.issues if i.code == "withheld_pair_transition"]
    assert [(i.trial_id, i.before, i.after, i.detail) for i in w] == [
        ("T2", "INFO_MISSING", "BLOCKED_FAILURE", ("authority:policy", "reason:sources_exhausted"))]


def test_decline_rule_matches_decision_state():
    from ctrec.decision_state import _declined
    for text in ("모름", " 모름 ", "Unknown", "UNKNOWN", "", "거부", "ECOG 1", "모름. 기억 안 남"):
        assert rp.is_declined(text) == _declined(text), text


# ------------------------------------------------------------ 정책 전이·저장·로드
def test_policy_transition_kept_with_its_authority():
    before = cohort(pair("T1", PS.INFO_MISSING, [crit("T1", "I1", "UNKNOWN")]))
    after = cohort(pair("T1", PS.BLOCKED_FAILURE, [crit("T1", "I1", "UNKNOWN")], pending=("sources_exhausted",)))
    pt = C.PairTransition(pair=C.PairKey(patient_alias=A, trial_id="T1"), before=PS.INFO_MISSING,
                          after=PS.BLOCKED_FAILURE, authority="policy", reason_code="sources_exhausted",
                          pending_kinds=("sources_exhausted",))
    r = rp.build_step(obs(execution="declined", before=before, after=after, policy_transitions=(pt,),
                          reevaluated_trials=frozenset()))
    assert r.status == "recorded" and r.record.pairs == (pt,)
    wrong = pt.model_copy(update={"after": PS.VERIFICATION_PENDING})
    assert rp.build_step(obs(execution="declined", before=before, after=after, policy_transitions=(wrong,),
                             reevaluated_trials=frozenset())).status == "contract_violation"


def test_write_and_load_with_existing_audit_tools(tmp_path):
    f = Facts()
    f.add_answer("smoker", True, "smoker", ref="raw_text:answers")
    ok = rp.build_step(obs(facts=f))
    before = cohort(pair("T1", PS.CONFIRMED_ELIGIBLE, [crit("T1", "I1", "MET")], "ELIGIBLE"))
    after = cohort(pair("T1", PS.CONFIRMED_INELIGIBLE, [crit("T1", "I1", "NOT_MET")], "INELIGIBLE"))
    blocked = rp.build_step(obs(step=2, before=before, after=after))
    records = audit.RecordWriter(tmp_path / audit.RECORDS_FILE, "RUN-1")
    issues = rp.IssueWriter(tmp_path / rp.ISSUES_FILE, "RUN-1")
    for res in (ok, blocked):
        rp.write_step(res, records, issues)
    loaded = audit.load_run_v2(tmp_path)
    assert loaded.issues == [] and [r.step for r in loaded.records] == [1]  # 차단 단계는 정상 기록에 없음
    saved = rp.load_issues(tmp_path / rp.ISSUES_FILE)
    assert {(i.step, i.code, i.severity) for i in saved} == {(1, "unattributed_fact", "warning"),
                                                             (2, "direct_confirmed_reversal", "blocking"),
                                                             (2, "withheld_criterion_change", "blocking"),
                                                             (2, "withheld_outcome", "blocking")}
    assert not rp.run_status([ok, blocked]).complete


# ------------------------------------------------------------ 엔진 경계
def test_producer_module_respects_engine_isolation():
    src = Path(rp.__file__).read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    parts = {p for m in mods for p in m.split(".")}
    assert not parts & {"question_eval", "eval_hygiene", "metrics", "llm", "llm_pilot", "pipeline", "web",
                        "orchestrator", "answerers", "sim", "contract_session"}
    for word in ("random", "probab", "oracle", "hidden_fact", "label", "uuid", "time.time"):
        assert word not in src.lower(), word


# ------------------------------------------------------------ 실제 오프라인 Session 관찰 (연기 테스트)
def test_smoke_real_offline_session_observations(tmp_path):
    """실제 Session(오프라인 대역, 별칭)에서 관찰을 만들어 생산기에 넣음. 라운드당 질문 1개로 인과를 분명히 함."""
    from bench_incremental import load_frozen
    from decision_scenarios import load_trials

    from ctrec import question_eval as qe
    from ctrec.decision_state import SessionView, build_state
    from ctrec.engine.policy import LegacyAdapterPolicy
    from ctrec.engine.question_bridge import Wording, render_action, renderable
    from ctrec.eval_hygiene import AliasMap, load_cases_hygienic, run_method_hygienic
    from ctrec.schemas import QuestionSet

    parsed = load_frozen()
    allt = load_trials()
    trials = {t: allt[t] for t in parsed}
    case_id = "M-SYN-DEMO-A-DEMO-LUNG-001-rescue2-3"
    hcs, _, scanner = load_cases_hygienic(parsed, only=[case_id], alias_map=AliasMap(salt="t"))
    pol, budget, rounds, ref = LegacyAdapterPolicy(), C.EffortBudget(coordinator_minutes=1e5), [], {}

    def state(s):
        v = SessionView.from_session(s)
        v.matched_versions = {t: v.snapshot.version for t in s.matches}
        return v.snapshot, CohortState.from_decision_states({s.case.patient_id: build_state(v)})

    def factory(r):
        ref.update(r=r)

        def gen(profile, matches, qa_log, n, acquisition_plan=None):
            s = r["session"]
            snap, co = state(s)
            plan = pol.select(pol.propose(co), budget, cohort=co)
            a = next((x for x in plan.selected if renderable(x)), None)
            if a is None:
                return QuestionSet(questions=[])
            q = render_action(a, "Q1", wording=lambda x: Wording(question=f"[engine] {x.info_key or 'item'}",
                                                                  answer_type="free_text"))
            rounds.append({"round": s.rounds, "snap": snap, "co": co, "action": a, "q": q,
                           "rematch": frozenset(m.trial_id for m in matches)})
            return QuestionSet(questions=[q])
        return gen

    with qe.OfflineEnvironment(1):
        run, guard = run_method_hygienic("engine_v2", hcs[0], trials, 2, 1, scanner, factories={"engine_v2": factory})
    s = ref["r"]["session"]
    assert run.status == "ok" and sum(guard.leaks.values()) == 0 and rounds
    final = state(s)
    results = []
    for i, rd in enumerate(rounds):
        nxt = rounds[i + 1] if i + 1 < len(rounds) else {"snap": final[0], "co": final[1]}
        qa = next(p for p in s.qa_log if p.round == rd["round"] and p.question_id == rd["q"].question_id)
        a = rd["action"]
        o = rp.StepObservation(
            run_id="RUN-SMOKE", step=i + 1, action=a,
            execution="declined" if rp.is_declined(qa.answer) else "answered",
            asked=rp.AskedQuestion(question_id=qa.question_id, action_id=a.action_id,
                                   target_criteria=tuple(qa.target), purpose=qa.purpose),
            before=rd["co"], after=nxt["co"], facts_before=rd["snap"], facts_after=nxt["snap"],
            reevaluated_trials=rd["rematch"])
        results.append(rp.build_step(o))
    records = audit.RecordWriter(tmp_path / audit.RECORDS_FILE, "RUN-SMOKE", scanner=scanner)
    issues = rp.IssueWriter(tmp_path / rp.ISSUES_FILE, "RUN-SMOKE", scanner=scanner)
    for res in results:
        rp.write_step(res, records, issues)
    loaded = audit.load_run_v2(tmp_path)
    assert loaded.issues == [] and len(loaded.records) == len(results)
    assert rp.run_status(results).complete
    # 평가기 쪽 검사: 정답 사실 ID·원문, 원래 케이스 ID가 기록·진단에 없음
    blob = (tmp_path / audit.RECORDS_FILE).read_text(encoding="utf-8")
    if (tmp_path / rp.ISSUES_FILE).exists():
        blob += (tmp_path / rp.ISSUES_FILE).read_text(encoding="utf-8")
    hidden_ids = set(hcs[0].gt.masked_fact_ids)
    assert not any(f'"{h}"' in blob for h in hidden_ids)
    assert not any(t in blob for _, t in hcs[0].gt._hidden_texts) and case_id not in blob
    real_ids = {f.fact_id for f in final[0].facts}
    assert {x for r in loaded.records for x in r.facts_added} <= real_ids
