"""PR #20 잔여 복구 결함 수정 검증 (합성 데이터, LLM 호출 없음).

R1 문서 상태 계약: 사용·실패·남은 대체 문서를 _convert와 apply_outcome이 같은 값으로 읽음. 문서가 정말 소진됐을 때만 BLOCKED.
R2 Session 루프: 정책에 남은 대체 행동을 실제 Session이 실행하고, 한도로 못 한 행동은 기록함. 회복·반복 금지 계약 유지.
시뮬레이터 스트레스는 test_recovery_stress.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config, llm, question_eval as qe, trial_store  # noqa: E402
from ctrec.agents import matcher, parser_validator as pv  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.decision_state import build_state  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.dcvc import FullDCVCPolicy  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy, tried_key  # noqa: E402
from ctrec.eval_hygiene import AliasMap  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.schemas import ParsedTrial  # noqa: E402
from ctrec.web import engine_runner as er  # noqa: E402

import decision_fixtures as d  # noqa: E402
from test_offline import _rule as offline_rule  # noqa: E402
from test_pr20_review_regressions import ECOG, HB, KEEP_LOOP_OPEN, LAB_REPORT, PATIENT, RECORD, TID, _parsed, _rule, _trial  # noqa: E402

MED = "medication_list"
ITEM = "criterion:T1:I9"
AM = AliasMap(salt="recovery-r1-r2")
PA = AM.alias("SYN-R1R2")
TACRO = offline_rule(rule_id="I9", text="No prior tacrolimus", category="medication", field=None, operator=None,
                     value=None, unit=None)
AGE = offline_rule(rule_id="I1", text="Age >= 18", field="age", operator=">=", value="18", unit="years")
UNREACHABLE = ("failed", C.UNREACHABLE)
NO_ANSWER = ("no_response", C.NO_ANSWER)
ANSWERED = ("succeeded", None)
EXHAUSTED = ("sources_exhausted", "item_sources_exhausted")


@pytest.fixture
def stub_llm(monkeypatch):
    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)


def _cohort(rules=(TACRO,), facts=(("age", 60, {"unit": "years"}),)):
    s = d.store(*facts, patient="SYN-R1R2")
    return CohortState.from_decision_states(
        {PA: build_state(d.view({"T1": d.trial("T1", list(rules))}, s, patient="SYN-R1R2"))})


def _step(pol, co, key, outcome_of, done):
    """그 항목에 정책이 낸 실행 행동 하나를 실행. 없으면 None."""
    acts = [a for a in pol.propose(co) if a.info_key == key and a.action_type not in C.NON_EXECUTING
            and a.source_id not in done]
    if not acts:
        return None
    a = acts[0]
    done.append(a.source_id)
    st, code = outcome_of(a.source_id)
    return pol.apply_outcome(co, a, C.ActionOutcome(action_id=a.action_id, status=st, error_code=code))[0]


def _drive(outcome_of, stop_before=None, key=ITEM, co=None):
    pol, co, sources = LegacyAdapterPolicy(), co or _cohort(), []
    for _ in range(6):
        next_ = _step(pol, co, key, outcome_of, sources)
        if next_ is None:
            break
        co = next_
        if stop_before and sources[-1] == stop_before:
            break
    return pol, co, sources


def _ops(co):
    return co.ops[PA]


# ------------------------------------------------------------ R1: 상태 계약
def test_used_and_failed_documents_share_one_contract(stub_llm):
    pol, co, sources = _drive(lambda s: {PATIENT: UNREACHABLE, MED: ANSWERED}.get(s, NO_ANSWER), stop_before=MED)
    assert sources == [PATIENT, MED]
    ops = _ops(co)
    assert ops.attempts[tried_key(MED, ITEM)] == 1 and (MED, ITEM) in pol.spent_sources(ops, ITEM)  # 사용한 문서
    assert PATIENT in ops.failed_sources  # 실패한 정보원
    assert pol.remaining_documents(PATIENT, (), None, ITEM, ops) == [RECORD]  # 남은 대체 문서
    assert [a.source_id for a in pol.propose(co) if a.info_key == ITEM] == [RECORD]  # 다음 계획과 같은 값


def test_pair_is_not_blocked_while_an_alternative_document_remains(stub_llm):
    _, co, _ = _drive(lambda s: UNREACHABLE if s == PATIENT else ANSWERED, stop_before=MED)
    assert ITEM not in _ops(co).exhausted_items
    assert co.pair(PA, "T1").status == C.PairStatus.INFO_MISSING


def test_answered_document_that_resolves_nothing_else_keeps_sibling_item_open(stub_llm):
    """항목 둘 중 하나만 소진되면 쌍은 그대로 (다른 항목은 문서로 아직 얻을 수 있음)."""
    other = offline_rule(rule_id="I8", text="No prior sirolimus", category="medication", field=None, operator=None,
                         value=None, unit=None)
    co = _cohort(rules=(TACRO, other))
    _, co, _ = _drive(lambda s: UNREACHABLE if s == PATIENT else ANSWERED, co=co)
    assert ITEM in _ops(co).exhausted_items and "criterion:T1:I8" not in _ops(co).exhausted_items
    assert co.pair(PA, "T1").status == C.PairStatus.INFO_MISSING


@pytest.mark.parametrize("last", [ANSWERED, NO_ANSWER])
def test_no_source_failure_path_is_exhausted_too(stub_llm, last):
    """H 패치의 빈틈: 연락 불가 없이 환자 "모름" -> 문서 둘이 gap을 남기면 정책은 소진 보류를 내는데 쌍이 열려 있었음."""
    pol, co, sources = _drive(lambda s: {PATIENT: NO_ANSWER, MED: ANSWERED}.get(s, last))
    assert sources == [PATIENT, MED, RECORD] and _ops(co).failed_sources == frozenset()
    assert {a.reason_code for a in pol.propose(co) if a.info_key == ITEM} <= set(EXHAUSTED)
    assert co.pair(PA, "T1").status == C.PairStatus.BLOCKED_FAILURE


def test_exhausted_state_matches_the_policys_own_defer_on_every_path(stub_llm):
    """모든 정보원 결과 조합: 다음 계획이 소진 보류뿐이면 항목은 소진 상태, 실행할 행동이 남으면 소진 아님."""
    for p in (UNREACHABLE, NO_ANSWER):
        for m in (UNREACHABLE, NO_ANSWER, ANSWERED):
            for r in (UNREACHABLE, NO_ANSWER, ANSWERED):
                table = {PATIENT: p, MED: m, RECORD: r}
                pol, co, _ = _drive(lambda s: table[s])
                acts = [a for a in pol.propose(co) if a.info_key == ITEM]
                assert acts and all(a.action_type == "DEFER" and a.reason_code in EXHAUSTED for a in acts), table
                assert ITEM in _ops(co).exhausted_items and co.pair(PA, "T1").status == C.PairStatus.BLOCKED_FAILURE


def test_verified_document_answer_clears_the_item_without_blocking(stub_llm):
    """문서가 답해 값이 확인되면 항목은 열린 항목이 아니므로 소진 기록이 쌍을 막지 않음 (재평가된 코호트를 넘김)."""
    pol = LegacyAdapterPolicy()
    co = _cohort(rules=(ECOG_RULE,), facts=(("age", 60, {"unit": "years"}),))
    a = next(x for x in pol.propose(co) if x.info_key == "ecog")
    co2, _ = pol.apply_outcome(co, a, C.ActionOutcome(action_id=a.action_id, status="failed", error_code=C.UNREACHABLE))
    a2 = next(x for x in pol.propose(co2) if x.info_key == "ecog" and x.action_type == "REQUEST_DOCUMENT")
    fresh = _cohort(rules=(ECOG_RULE,), facts=(("age", 60, {"unit": "years"}), ("ecog", 1, {})))
    mid = CohortState.from_decision_states({PA: fresh.patients[PA].legacy_copy()}, dict(co2.ops))
    co3, _ = pol.apply_outcome(mid, a2, C.ActionOutcome(action_id=a2.action_id, status="succeeded"))
    assert "ecog" not in _ops(co3).exhausted_items
    assert co3.pair(PA, "T1").status != C.PairStatus.BLOCKED_FAILURE


ECOG_RULE = offline_rule(rule_id="I2", text="ECOG 0-1", category="performance_status", field="ecog", operator="<=",
                         value="1")


# ------------------------------------------------------------ R1: VERIFICATION_PENDING -> BLOCKED 경계
SELF_REPORTED_MINOR = (("age", 12, {"unit": "years", "source_type": "self_report"}),)


def test_pair_stays_pending_while_its_exclusion_basis_document_remains(stub_llm):
    """불충족 근거(나이 자기 보고)의 문서 확인이 남아 있으면 다른 항목이 다 소진돼도 BLOCKED가 아님."""
    co = _cohort(rules=(AGE, TACRO), facts=SELF_REPORTED_MINOR)
    pol, co, _ = _drive(lambda s: UNREACHABLE if s == PATIENT else NO_ANSWER, co=co)
    assert ITEM in _ops(co).exhausted_items
    basis = next(a for a in pol.propose(co) if a.reason_code == "exclusion_basis_unverified")
    assert basis.action_type == "REQUEST_DOCUMENT"
    assert co.pair(PA, "T1").status == C.PairStatus.VERIFICATION_PENDING
    # full_dcvc는 BLOCKED 쌍의 행동을 버리므로, 조기 BLOCKED였다면 이 요청이 사라짐
    assert any(a.reason_code == "exclusion_basis_unverified" for a in FullDCVCPolicy().propose(co))


def test_pair_is_blocked_once_its_exclusion_basis_document_is_spent_too(stub_llm):
    co = _cohort(rules=(AGE, TACRO), facts=SELF_REPORTED_MINOR)
    pol, co, _ = _drive(lambda s: UNREACHABLE if s == PATIENT else NO_ANSWER, co=co)
    for _ in range(4):
        basis = [a for a in pol.propose(co) if a.reason_code == "exclusion_basis_unverified"
                 and a.action_type == "REQUEST_DOCUMENT"]
        if not basis:
            break
        co, _ = pol.apply_outcome(co, basis[0], C.ActionOutcome(action_id=basis[0].action_id, status="no_response",
                                                                error_code=C.NO_ANSWER))
    p = co.pair(PA, "T1")
    assert p.status == C.PairStatus.BLOCKED_FAILURE
    assert {"exclusion_unverified", "sources_exhausted"} <= p.pending_kinds  # 미검증 근거 사유는 그대로 보존


def test_answered_documents_with_only_unverified_gap_stay_verification_pending(stub_llm):
    """값은 있고 문서 확인만 남은 항목: 요청한 문서를 다 썼어도 실패가 없으면 소진이 아님 (웹 입력은 문서 검증이 되지 않음)."""
    co = _cohort(rules=(TACRO,), facts=(("age", 60, {"unit": "years"}),
                                        ("medication", "tacrolimus", {"source_type": "self_report"})))
    pol = LegacyAdapterPolicy()
    for _ in range(4):
        acts = [a for a in pol.propose(co) if a.action_type == "REQUEST_DOCUMENT" and a.info_key
                and not _ops(co).attempts.get(tried_key(a.source_id, a.info_key))]
        if not acts:
            break
        co, _ = pol.apply_outcome(co, acts[0], C.ActionOutcome(action_id=acts[0].action_id, status="succeeded"))
    assert not _ops(co).exhausted_items
    assert co.pair(PA, "T1").status != C.PairStatus.BLOCKED_FAILURE


# ------------------------------------------------------------ R2: 실제 Session
class _Tracer:
    def __init__(self):
        self.events = []

    def log(self, *a, **k):
        self.events.append((a, k))


class _Answerer:
    def __init__(self, answer):
        self.answer_of = answer
        self.seen = []

    def answer(self, questions, patient_id):
        self.seen += [q.question for q in questions]
        return {q.question_id: self.answer_of(q) for q in questions}


@pytest.fixture
def run_session(tmp_path):
    with qe.OfflineEnvironment(1) as env:
        env._set(config, "DEMO_MODE", True)
        env._set(config, "PARSED_DIR", tmp_path / "parsed")
        env._set(config, "CACHE_DIR", tmp_path / "cache")
        real = env.stub.structured

        def structured(model_cls, *, system, user, **k):
            if model_cls is ParsedTrial:
                return _parsed()
            if model_cls is pv.ValidationOutput:
                return pv.ValidationOutput(checks=[], missing=[])
            return real(model_cls, system=system, user=user, **k)
        llm.structured = structured  # OfflineEnvironment가 종료할 때 복원

        def execute(rules, policy=None, *, answer=lambda q: "모름", rounds=14, cls=Session, budget=None, **kw):
            trial = _trial()
            trial_store.save(trial, _parsed(rules), {}, pv.ValidationReport())
            s = cls(case=PatientCase(AliasMap(salt="r2").alias("SYN-R2"), "Synthetic 62-year-old male.",
                                     input_source="unknown", candidate_trials=[TID]),
                    trials={TID: trial}, answerer=_Answerer(answer), tracer=_Tracer(), max_rounds=rounds, **kw)
            with er.activate(s, policy=policy or LegacyAdapterPolicy(), **({"budget": budget} if budget else {})) as runner:
                s.run_fixed()
            return runner
        yield execute


def _status(r):
    return r.cohort().pair(r.alias, TID).status


def _keys(r):
    return [er._repeat_key(a.action) for a in r.asked]


def test_loop_runs_the_document_after_the_last_open_item_without_a_keep_open_item(run_session):
    """H에서 KEEP_LOOP_OPEN으로 피해 가던 조건: 열린 항목이 없어도 정책의 남은 문서 요청을 실행하고 소진까지 감."""
    r = run_session([ECOG])
    assert [(a.action.action_type, a.action.source_id) for a in r.asked] == [("ASK_PATIENT", PATIENT),
                                                                             ("REQUEST_DOCUMENT", RECORD)]
    assert _status(r) == C.PairStatus.BLOCKED_FAILURE and r.unexecuted == []
    assert r.session.rounds < 14  # 할 일이 없으면 한도까지 돌지 않음


def test_rephrase_then_document_runs_in_a_real_session_for_nonstandard_no_answer(run_session):
    """기존 "모름" 목록 밖의 무응답도 같은 회복 계약: 바꿔 묻기 한 번 -> 문서 -> 소진."""
    r = run_session([ECOG], LegacyAdapterPolicy(rephrase=True), answer=lambda q: "잘 모르겠어요")
    assert [(a.action.action_type, a.action.reason_code == C.REPHRASE) for a in r.asked] == [
        ("ASK_PATIENT", False), ("ASK_PATIENT", True), ("REQUEST_DOCUMENT", False)]
    assert _status(r) == C.PairStatus.BLOCKED_FAILURE


def test_round_limit_keeps_the_limit_and_records_the_unexecuted_alternative(run_session):
    r = run_session([ECOG, HB, *KEEP_LOOP_OPEN], rounds=2)
    assert r.session.rounds == 2  # 한도는 그대로
    left = [u for u in r.unexecuted if u["recovery"]]
    assert left and all(u["action_type"] == "REQUEST_DOCUMENT" and u["stop_reason"] == "round_limit" for u in left)
    assert _status(r) != C.PairStatus.BLOCKED_FAILURE  # 문서가 남았으므로 소진이 아님
    (args, kw), = [e for e in r.session.tracer.events if e[0][:2] == ("engine", "actions_not_executed")]
    assert kw["stop_reason"] == "round_limit" and kw["count"] == len(r.unexecuted)
    assert not any(r.alias in str(e) for e in r.session.tracer.events if e[0][:2] == ("engine", "actions_not_executed"))


def test_unexecuted_alternative_is_restored_when_the_limit_allows_it(run_session):
    """2라운드에서 잘린 문서 요청은 라운드가 있으면 그대로 실행됨."""
    short = run_session([ECOG, HB, *KEEP_LOOP_OPEN], rounds=2)
    missed = {tuple(u["targets"]) for u in short.unexecuted if u["recovery"]}
    long = run_session([ECOG, HB, *KEEP_LOOP_OPEN], rounds=14)
    assert missed <= {tuple(a.action.target_criteria) for a in long.asked if a.action.action_type == "REQUEST_DOCUMENT"}


def test_question_budget_stops_the_loop_and_is_recorded(run_session):
    r = run_session([ECOG, HB], question_budget=1)
    assert len(r.session.qa_log) == 1
    assert r.unexecuted and {u["stop_reason"] for u in r.unexecuted} == {"question_budget"}


def test_coordinator_budget_is_kept_and_recorded(run_session):
    """작업시간 예산은 환자 질문 하나만 허용 -> 문서 요청은 실행되지 않고 over_budget으로 기록."""
    ask_only = LegacyAdapterPolicy().catalog["patient_interview"].request_cost.coordinator_minutes + 1
    r = run_session([ECOG], budget=C.EffortBudget(coordinator_minutes=ask_only))
    assert r.ledger.total.coordinator_minutes <= ask_only
    assert [a.action.source_id for a in r.asked] == [PATIENT]
    assert [(u["source_id"], u["stop_reason"]) for u in r.unexecuted] == [(RECORD, "over_budget")]
    assert _status(r) != C.PairStatus.BLOCKED_FAILURE


@pytest.mark.parametrize("answer", ["모름", "잘 모르겠어요", "기록이 애매합니다", ""])
@pytest.mark.parametrize("rephrase", [False, True])
def test_no_action_runs_twice_and_loop_is_finite(run_session, answer, rephrase):
    r = run_session([ECOG, HB, *KEEP_LOOP_OPEN], LegacyAdapterPolicy(rephrase=rephrase), answer=lambda q: answer)
    assert len(_keys(r)) == len(set(_keys(r)))
    assert r.session.rounds <= 14
    questions = r.session.answerer.seen
    assert len(questions) == len(set(questions))  # 바꿔 묻기를 포함해 같은 문장을 두 번 묻지 않음


def test_runner_does_not_repeat_an_action_the_policy_keeps_proposing(run_session):
    """정책이 같은 문서 요청을 계속 내도(운영 상태가 갱신되지 않는 경우) 실행기가 한 번만 실행하고 루프를 끝냄."""
    class Repeating(LegacyAdapterPolicy):
        same = None

        def propose(self, cohort, pending_reevaluation=None):
            ecog = next(a for a in super().propose(cohort, pending_reevaluation) if a.info_key == "ecog")
            self.same = self.same or ecog.model_copy(update={"action_type": "REQUEST_DOCUMENT", "source_id": RECORD})
            return [self.same]

        def apply_outcome(self, cohort, action, outcome):
            return cohort, ()
    r = run_session([ECOG], Repeating())
    assert [a.action.source_id for a in r.asked] == [RECORD]
    assert r.session.rounds == 1


def test_full_dcvc_runs_the_remaining_document_in_a_real_session(run_session):
    r = run_session([ECOG], FullDCVCPolicy())
    assert [a.action.source_id for a in r.asked] == [PATIENT, RECORD]


def test_ablation_does_not_extend_the_loop(run_session):
    class NoRecovery(LegacyAdapterPolicy):
        recovery = False
    r = run_session([ECOG], NoRecovery())
    assert [a.action.source_id for a in r.asked] == [PATIENT]


def test_contract_session_runs_the_remaining_document_too(run_session, tmp_path):
    r = run_session([ECOG], cls=ContractSession, contract_root=tmp_path / "none")
    assert [a.action.source_id for a in r.asked] == [PATIENT, RECORD]


def test_report_names_the_unexecuted_document_without_patient_values(run_session):
    r = run_session([ECOG, HB, *KEEP_LOOP_OPEN], rounds=2)
    r.session.report = "보고서"  # 평가 환경은 보고서를 만들지 않음
    r.close()
    tail = r.session.report.split("## 실행하지 못한 확인")[1]
    assert "Hemoglobin >= 9 g/dL" in tail or "ECOG performance status" in tail
    assert r.alias not in tail and "모름" not in tail


def test_activate_restores_the_session_even_on_failure(run_session):
    s = Session(case=PatientCase("SYN-X", "x", input_source="unknown"), trials={}, answerer=_Answerer(lambda q: "모름"),
                tracer=_Tracer())
    with pytest.raises(RuntimeError):
        with er.activate(s):
            assert s.uncertain.__self__.__class__ is er.EngineRunner
            raise RuntimeError("stop")
    assert "uncertain" not in vars(s) and s.uncertain() == []


def test_legacy_mode_session_is_untouched(monkeypatch):
    monkeypatch.delenv(er.ENV_POLICY_MODE, raising=False)
    s = Session(case=PatientCase("SYN-X", "x", input_source="unknown"), trials={}, answerer=_Answerer(lambda q: "모름"),
                tracer=_Tracer())
    with er.for_mode(s):
        assert "uncertain" not in vars(s)


def test_lab_item_still_requests_the_lab_report_first(run_session):
    r = run_session([HB])
    assert [a.action.source_id for a in r.asked if a.action.action_type == "REQUEST_DOCUMENT"][:1] == [LAB_REPORT]
