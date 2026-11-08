"""P: DCV+C 공통 엔진 - 웹 Session과 시뮬레이터(WP6)가 같은 정책·같은 실행 루프(engine.loop)를 쓰는지 검증.

- 같은 객체: 시뮬레이터 runner.run과 웹 activate가 모두 EngineLoop.plan·settle을 부름 (다른 판단 코드 없음)
- 웹·시뮬레이터 일치: 웹 실행의 라운드별 상태(같은 초기 상태)를 새 EngineLoop로 재생하면 행동 선택·운영 상태 전이가 같음
  (기본 "네" / 무응답 "모름" / 무응답 뒤 문서 확인 성공). 시뮬레이터 쪽 실패(연락 불가)는 응답 모델로 따로 확인
- 증분 = 풀: 시뮬레이터의 환자 단위 재평가 결과 = 종료 사실로 다시 평가한 결과. 웹은 사실이 바뀐 뒤 판정이 오래된 시험도
  다시 매칭해 전체 재매칭과 같은 상태
- 반복 금지, 성공 확률 비사용, GT 격리, 임상의 판단 자동 확정 금지, 가정 비용 표시, 코호트 번들링·미해결 유지
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import question_eval as qe  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine import loop as L  # noqa: E402
from ctrec.engine.baselines import make_policy, policies  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.dcvc import RECORD_EVIDENCE, FullDCVCPolicy  # noqa: E402
from ctrec.eval_hygiene import AliasMap, load_cases_hygienic  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.sim import demo, nsclc_pilot, runner, suite  # noqa: E402
from ctrec.sim.evaluator import OfflineEvaluator  # noqa: E402
from ctrec.sim.response_model import DEFAULT_RESPONSES, SourceResponse  # noqa: E402
from ctrec.web import engine_runner as er  # noqa: E402

PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}
CASES = ("F-SYN-S001-01", "F-SYN-S003-01")
BUDGET = C.EffortBudget(coordinator_minutes=90, clinician_minutes=60, horizon_steps=10)


class _Tracer:
    def __init__(self):
        self.recs = []

    def log(self, agent, event, **kw):
        self.recs.append((agent, event, kw))


class _Answerer:
    def __init__(self, fn):
        self.fn = fn
        self.asked = []

    def answer(self, questions, patient_id):
        self.asked.append(list(questions))
        return {q.question_id: self.fn(q) for q in questions}


def _cases(ids):
    amap = AliasMap(salt="p-common")
    hcs, _, _ = load_cases_hygienic(PARSED, only=list(ids), alias_map=amap)
    return {h.gt.case_id: h for h in hcs}


def _session(hc, answer, rounds):
    c = hc.case
    return Session(case=PatientCase(patient_id=c.patient_id, raw_text=c.raw_text, input_source=c.input_source,
                                    candidate_trials=list(c.candidate_trials or [])),
                   trials={t: TRIALS[t] for t in hc.gt.candidate_trials}, answerer=_Answerer(answer),
                   tracer=_Tracer(), max_rounds=rounds)


def _web(answer, rounds=6, cases=CASES[:1], policy_id="full_dcvc"):
    hcs = _cases(cases)
    with qe.OfflineEnvironment(3):
        sessions = [_session(hcs[c], answer, rounds) for c in cases]
        if len(sessions) == 1:
            with er.activate(sessions[0], policy=make_policy(policy_id)) as r:
                sessions[0].run_fixed()
        else:
            r = er.run_cohort(sessions, policy=make_policy(policy_id))
    return sessions, r


YES = lambda q: "네, 해당됩니다"  # noqa: E731
NO_ANSWER = lambda q: "모름"  # noqa: E731
DOC_OK = lambda q: "기록 확인: ECOG 1, 해당 내용 기록에 있음" if q.question.startswith("[기록 확인") else "모름"  # noqa: E731
SCENARIOS = {"basic": YES, "no_response": NO_ANSWER, "document_success": DOC_OK}


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def web_run(request):
    return request.param, *_web(SCENARIOS[request.param])


# ---------------------------------------------------------------------------- 같은 객체
def test_simulator_and_web_call_the_same_loop(monkeypatch):
    calls = []
    for name in ("plan", "settle"):
        real = getattr(L.EngineLoop, name)

        def spy(self, *a, _real=real, _name=name, **k):
            calls.append((_name, type(self.policy).__name__))
            return _real(self, *a, **k)
        monkeypatch.setattr(L.EngineLoop, name, spy)
    co = demo.cohort()
    runner.run(runner.build_spec("p-same", "full_dcvc", co, BUDGET, 1), co)
    sim_calls = list(calls)
    calls.clear()
    _web(NO_ANSWER, rounds=2)
    assert {"plan", "settle"} <= {n for n, _ in sim_calls}
    assert {"plan", "settle"} <= {n for n, _ in calls}
    assert {p for _, p in sim_calls} == {p for _, p in calls} == {"FullDCVCPolicy"}  # 같은 정책 클래스
    assert er.DEFAULT_POLICY_ID == "full_dcvc" and er.policy_from_env().policy_id == "full_dcvc"


def _direct_calls(module) -> set[str]:
    """모듈 코드에서 정책의 select·apply_outcome을 직접 부르는 곳 (engine.loop를 거치지 않는 선택·정산)."""
    import ast
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("select", "apply_outcome")}


def test_sim_and_web_have_no_private_selection_code():
    assert _direct_calls(runner) == set() and _direct_calls(er) == set()  # 모두 engine.loop를 거침


# ---------------------------------------------------------------------------- 웹·시뮬레이터 일치 (재생)
def _replay(r) -> list[str]:
    """웹 실행의 라운드 기록(같은 상태·같은 결과)을 새 EngineLoop로 재생하고 웹의 선택·운영 상태와 대조. 반환: 불일치."""
    loop = L.EngineLoop(type(r.policy)(), r.budget)
    ops: dict = {}
    bad = []

    def apply(states, settled):
        nonlocal ops
        co = CohortState.from_decision_states(states, ops=dict(ops))
        for a, o in settled:
            co, _ = loop.settle(co, a, o)
        ops = dict(co.ops)
    for i, h in enumerate(r.rounds_log):
        apply(h["states"], h["settled"])
        co = CohortState.from_decision_states(h["states"], ops=dict(ops))
        plan = loop.plan(co)
        if [a.action_id for a in plan.selected] != [a.action_id for a in h["plan"].selected]:
            bad.append(f"round {i}: selected differs")
        unfunded = r.unfunded_aliases(plan)
        disp = loop.dispatch(plan, r.executable, lambda al: 0 if al in unfunded else h["capacity"].get(al, 0))
        if disp != h["dispatch"]:
            bad.append(f"round {i}: dispatch differs")
        for a in disp.not_executable:
            loop.begin(a, executed=False)
            co, _ = loop.settle_not_executed(co, a, er.NOT_EXECUTABLE_HERE)
        ops = dict(co.ops)
        for a in h["asked"]:
            loop.begin(a, executed=True)
    if r.final_log:
        apply(r.final_log["states"], r.final_log["settled"])
    if {k: v for k, v in ops.items()} != {k: v for k, v in r.ops.items()}:
        bad.append("final ops differ")
    if loop.tried != r.loop.tried or loop.ledger.total != r.loop.ledger.total:
        bad.append("tried/ledger differ")
    return bad


def test_web_rounds_replay_identically_through_the_shared_loop(web_run):
    name, sessions, r = web_run
    assert r.rounds_log, name
    assert _replay(r) == [], name


def test_scenarios_exercise_their_paths(web_run):
    name, sessions, r = web_run
    settled = [o for h in r.rounds_log for _, o in h["settled"]] + [o for _, o in r.final_log.get("settled", [])]
    asked = [a for h in r.rounds_log for a in h["asked"]]
    if name == "basic":
        assert settled and all(o.status == "succeeded" for o in settled)
    if name == "no_response":
        assert any(o.error_code == C.NO_ANSWER for o in settled)
        assert any(a.reason_code == "alternative_source_after_no_answer" or a.action_type == "REQUEST_DOCUMENT"
                   for a in asked)
    if name == "document_success":
        docs = [o for (a, o) in [(a, o) for h in r.rounds_log for a, o in h["settled"]]
                if a.action_type == "REQUEST_DOCUMENT"]
        assert docs and all(o.status == "succeeded" for o in docs)


def test_web_never_repeats_the_same_question(web_run):
    name, sessions, r = web_run
    keys = [L.repeat_key(a.action) for a in r.asked]
    assert len(keys) == len(set(keys)), name
    texts = [(q.question, tuple(q.target)) for batch in sessions[0].answerer.asked for q in batch]
    assert len(texts) == len(set(texts)), name


def test_web_unresolved_pairs_stay_unresolved_at_the_end(web_run):
    name, sessions, r = web_run
    co = r.cohort()
    for p in co.pairs():
        if p.status == C.PairStatus.CONFIRMED_ELIGIBLE:  # 확정 적격은 필수 기준 근거가 모두 있을 때만
            assert p.eligibility == "ELIGIBLE" and not p.gaps


# ---------------------------------------------------------------------------- 풀 재평가
def test_web_engine_mode_rematches_stale_trials_like_a_full_rematch():
    sessions, r = _web(YES, rounds=3)
    s = sessions[0]
    m = r.members[0]
    cur = er.fact_version(s)
    evaluated = [t for t, x in s.matches.items() if x.eligibility != "NOT_EVALUATED"]
    assert evaluated and all(m.versions.get(t) == cur for t in evaluated)  # 판정이 오래된 시험이 남지 않음
    assert not any(t.stale for t in r._state(m).trials.values())


@pytest.mark.parametrize("pid", ["full_dcvc", "patient_order"])
def test_sim_incremental_patient_reevaluation_equals_full_reevaluation(pid):
    co = demo.cohort()
    res = runner.run(runner.build_spec("p-inc", pid, co, BUDGET, 2), co)
    ev = OfflineEvaluator(co.trials, co.reference_date, co.judge)
    aliases = co.aliases()
    states = {aliases[p.key]: ev.evaluate(aliases[p.key], res.facts[aliases[p.key]], p.trials)[0] for p in co.patients}
    full = CohortState.from_decision_states(states, dict(res.final.ops))
    assert [(p.pair.key, p.status, p.eligibility) for p in full.pairs()] == \
        [(p.pair.key, p.status, p.eligibility) for p in res.final.pairs()]


# ---------------------------------------------------------------------------- 시뮬레이터 시나리오
def _sim(responses=None, pid="full_dcvc", cfg=None, co=None, budget=BUDGET):
    co = co or demo.cohort()
    return runner.run(runner.build_spec("p-sc", pid, co, budget, 1, cfg or {}), co, responses=responses)


def test_sim_failure_moves_to_documents_and_document_success_confirms():
    unreachable = {**DEFAULT_RESPONSES, "patient": SourceResponse(p_unreachable=1.0, p_no_answer=0.0)}
    r = _sim(unreachable)
    assert r.metrics["failure_codes"].get(C.UNREACHABLE)
    assert r.metrics["actions_executed"].get("REQUEST_DOCUMENT")
    no_answer = {**DEFAULT_RESPONSES, "patient": SourceResponse(p_unreachable=0.0, p_no_answer=1.0)}
    r2 = _sim(no_answer)
    assert r2.metrics["failure_codes"].get(C.NO_ANSWER)
    assert r2.metrics["actions_executed"].get("REQUEST_DOCUMENT")
    ok = {k: SourceResponse(p_unreachable=0.0, p_no_answer=0.0) for k in DEFAULT_RESPONSES}
    r3 = _sim(ok, budget=C.EffortBudget(coordinator_minutes=400, clinician_minutes=60, horizon_steps=10))
    assert r3.metrics["system_confirmed_eligible"] + r3.metrics["system_confirmed_ineligible"] > 0


def test_sim_never_executes_the_same_action_twice():
    r = _sim()
    keys = [L.repeat_key(x.action) for x in r.records if x.outcome.status != "not_executed"]
    assert len(keys) == len(set(keys))


def test_recovery_ablation_changes_behaviour_only_after_failures():
    unreachable = {**DEFAULT_RESPONSES, "patient": SourceResponse(p_unreachable=1.0, p_no_answer=0.0)}
    on, off = _sim(unreachable), _sim(unreachable, cfg={"recovery": False})
    assert on.metrics["actions_executed"].get("REQUEST_DOCUMENT", 0) > off.metrics["actions_executed"].get(
        "REQUEST_DOCUMENT", 0)


# ---------------------------------------------------------------------------- 정책 입력 경계
def test_success_probabilities_are_not_a_policy_input():
    """응답 모델(성공 확률)을 바꿔도 첫 계획은 같음 - 정책은 확률을 보지 않음."""
    co = demo.cohort()
    first = {}
    for name, resp in (("default", None), ("bad", {k: SourceResponse(p_unreachable=0.9, p_no_answer=0.9)
                                                   for k in DEFAULT_RESPONSES})):
        r = _sim(resp, co=co)
        first[name] = [x.action.action_id for x in r.records if x.step < 3]
    assert first["default"] == first["bad"]


def test_only_the_reference_oracle_sees_ground_truth():
    import inspect
    for pid, cls in policies().items():
        params = inspect.signature(cls.__init__).parameters
        assert not {"env", "labels", "oracle", "truth"} & set(params), pid
    assert "oracle_upper" not in {pid for _, pid, _ in suite.ARMS}
    src = inspect.getsource(suite.run_suite)
    assert "sc.labels" in src and "runner.run(spec, sc.cohort, catalog)" in src  # 라벨은 지표 계산에만


def test_clinician_judgement_is_never_auto_confirmed():
    r = _sim(budget=C.EffortBudget(coordinator_minutes=400, clinician_minutes=600, horizon_steps=10))
    for p in r.final.pairs():
        for c in p.criteria:
            if c.adjudication == "expert":
                assert c.state == "UNKNOWN"
    assert all(not x.outcome.new_fact_ids for x in r.records if x.action.action_type == "CLINICIAN_REVIEW")


def test_costs_are_reported_as_assumed():
    from ctrec.engine.bundles import SourceCatalog
    from ctrec.engine.experiment import catalog_claims
    claims = catalog_claims(SourceCatalog())
    assert claims["assumed_cost_sources"] and claims["allowed_claims"] == "relative_policy_comparison_only"


# ---------------------------------------------------------------------------- DCV
def test_dcv_routes_record_only_questions_to_documents():
    co = demo.cohort()
    r_on = _sim(co=co)
    r_off = _sim(co=co, cfg={"evidence_routing": False})
    assert any(x.action.reason_code == RECORD_EVIDENCE for x in r_on.records)
    assert not any(x.action.reason_code == RECORD_EVIDENCE for x in r_off.records)
    for x in r_on.records:
        if x.action.reason_code == RECORD_EVIDENCE:
            assert x.action.action_type == "REQUEST_DOCUMENT" and x.action.source_id != "patient_interview"


def test_dcv_scores_a_decisive_not_met_check_as_closing_the_pair():
    sc = nsclc_pilot.suite_cohort()
    r = runner.run(runner.build_spec("p-dec", "full_dcvc", sc.cohort, C.EffortBudget(
        coordinator_minutes=30, clinician_minutes=60, horizon_steps=10), 1), sc.cohort)
    m = suite.pair_metrics(r, sc.labels, suite.initial_open(sc.cohort))
    assert m["verified_ineligible"] >= 4 and m["system_confirmed_eligible"] == 0


# ---------------------------------------------------------------------------- 코호트 (C)
def test_cohort_runner_plans_both_patients_with_one_ledger():
    sessions, r = _web(NO_ANSWER, rounds=4, cases=CASES)
    assert len(r.members) == 2 and r.cohort_mode
    first = r.rounds_log[0]
    assert {a.patient_alias for a in first["plan"].selected} == {m.alias for m in r.members}
    keys = [k for _, k, _ in r.ledger.charges if k]
    # 같은 (환자, 정보원)의 요청 비용은 한 번: 요청 비용을 낸 청구 키는 중복 없음
    paid = [k for (_, k, c) in r.ledger.charges if k and c.coordinator_minutes > 0]
    assert keys and len(r.ledger.requested) == len(set(r.ledger.requested)) and paid
    assert _replay(r) == []
    for s in sessions:
        assert s.rounds >= 1 and s.recommendation is not None or s.report is not None


def test_one_fact_update_moves_several_pairs():
    """한 행동이 여러 시험의 같은 항목을 겨냥하면 사실 하나가 여러 쌍을 함께 움직임 (환자 단위 재평가)."""
    ok = {k: SourceResponse(p_unreachable=0.0, p_no_answer=0.0) for k in DEFAULT_RESPONSES}
    r = _sim(ok, budget=C.EffortBudget(coordinator_minutes=400, clinician_minutes=60, horizon_steps=10))
    moved = [x for x in r.records if x.facts_added and len({c.criterion_key.split(":", 1)[0] for c in x.criteria}
                                                           | {t.pair.trial_id for t in x.pairs}) > 1]
    assert moved, "사실 하나로 여러 시험의 기준·쌍이 바뀐 기록이 없음"


def test_suite_reports_all_arms_and_marks_missing_gt():
    rows = suite.run_suite([suite.SuiteCohort(demo.cohort(), None, suite.GT_NONE)], [BUDGET], [1])
    assert [r["arm"] for r in rows] == [a for a, _, _ in suite.ARMS]
    assert all(r["verified_eligible"] is None and r["false_eligible"] is None for r in rows)  # 독립 GT 없음 = 미검증
    agg = suite.aggregate(rows)
    assert {a["arm"] for a in agg} == {a for a, _, _ in suite.ARMS}
    for r in rows:
        assert r["unresolved"] == r["waiting"] + r["unresolved_no_action"]


def test_policy_end_keeps_unresolved_pairs_unresolved():
    r = _sim(budget=C.EffortBudget(coordinator_minutes=5, clinician_minutes=0, horizon_steps=2))
    assert any(p.status not in (C.PairStatus.CONFIRMED_ELIGIBLE, C.PairStatus.CONFIRMED_INELIGIBLE)
               for p in r.final.pairs())
    assert r.metrics["status_final"][C.PairStatus.CONFIRMED_ELIGIBLE.value] <= \
        r.metrics["status_initial"][C.PairStatus.CONFIRMED_ELIGIBLE.value] + r.metrics["system_confirmed_eligible"]


# ---------------------------------------------------------------------------- NSCLC 파일럿
def test_nsclc_pilot_never_confirms_eligible_offline():
    sc = nsclc_pilot.suite_cohort()
    rows = suite.run_suite([sc], [C.EffortBudget(coordinator_minutes=90, clinician_minutes=60, horizon_steps=10)], [1],
                           arms=[a for a in suite.ARMS if a[0] in ("no_action", "dcvc")])
    for r in rows:
        assert r["system_confirmed_eligible"] == 0  # LLM 기준이 오프라인에서 판정 불가 -> 확정 적격이 나오면 결함


def test_nsclc_us_only_age_rule_does_not_confirm_ineligible():
    sc = nsclc_pilot.suite_cohort()
    r = runner.run(runner.build_spec("p-us", "full_dcvc", sc.cohort, C.EffortBudget(
        coordinator_minutes=90, clinician_minutes=60, horizon_steps=10), 1), sc.cohort)
    a = sc.cohort.aliases()["SYN-NSCLC-03"]
    assert r.final.pair(a, "NCT06216301").status != C.PairStatus.CONFIRMED_INELIGIBLE
