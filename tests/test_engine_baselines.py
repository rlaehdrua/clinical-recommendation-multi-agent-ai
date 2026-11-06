"""기준선 정책(engine/baselines.py)과 제안 정책 초안(engine/dcvc.py): 같은 후보·같은 안전/예산 규칙에서 순서만 다름.

합성 코호트(test_engine_policy의 4명: 정보 없음·문서 확인·상충·임상의 판단)와 오프라인 Matcher 대역만 씁니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine.baselines import (CheapestFirstPolicy, NoActionPolicy, PatientOrderPolicy,  # noqa: E402
                                    RandomPolicy, make_policy, policies)
from ctrec.engine.bundles import SourceCatalog  # noqa: E402
from ctrec.engine.cohort_state import CohortState, OpsState  # noqa: E402
from ctrec.engine.cost import EffortLedger  # noqa: E402
from ctrec.engine.dcvc import FullDCVCPolicy  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy  # noqa: E402

from test_engine_policy import PA, PB, PC, _budget, _cohort, _no_answer, _state_missing, stub_llm  # noqa: E402,F401

ALL = sorted(policies())
BUDGETS = [0, 5, 12, 30, 60, 240]


def _plan(pid, co, minutes, **kw):
    pol = make_policy(pid, **kw)
    b = _budget(minutes)
    return pol, pol.select(pol.propose(co), b, EffortLedger(b, pol.catalog), co)


def _charge_all(pol, plan, minutes):
    led = EffortLedger(_budget(minutes), pol.catalog)
    for a in plan.selected:
        led.charge(a)  # 예산 초과면 BudgetExceeded
    return led


@pytest.mark.parametrize("pid", ALL)
def test_every_policy_is_a_policy_engine_with_the_shared_rules(pid):
    co = _cohort()
    pol = make_policy(pid)
    assert isinstance(pol, C.PolicyEngine)
    for m in BUDGETS:
        _, plan = _plan(pid, co, m)
        led = _charge_all(pol, plan, m)
        assert led.total.coordinator_minutes <= m + 1e-9  # 예산 초과 0
        assert len({a.action_id for a in plan.selected}) == len(plan.selected)  # 중복 선택 0
        assert plan.policy_id == pol.policy_id


@pytest.mark.parametrize("pid", ALL)
def test_same_state_same_plan(pid):
    co = _cohort()
    for m in (12, 60):
        a = _plan(pid, co, m)[1]
        b = _plan(pid, _cohort(), m)[1]
        assert [x.action_id for x in a.selected] == [x.action_id for x in b.selected]


def test_all_policies_share_the_dcv_candidates():
    """기준선은 같은 후보 목록에서 고르는 방식만 다름. full_dcvc는 결정 영향 필터로 그 부분집합."""
    co = _cohort()
    base = {a.action_id for a in LegacyAdapterPolicy().propose(co)}
    for pid in ALL:
        got = {a.action_id for a in make_policy(pid).propose(co)}
        assert got <= base and (pid == "full_dcvc" or got == base), pid
    assert {a.action_id for a in FullDCVCPolicy(dcv_impact=False).propose(co)} == base


@pytest.mark.parametrize("pid", [p for p in ALL if p != "no_action"])
def test_safety_actions_first_and_unfunded_blocks_that_patient(pid):
    co = _cohort()
    pol, plan = _plan(pid, co, 240)
    safety = [a for a in plan.selected if a.safety_required]
    assert safety and plan.selected[:len(safety)] == tuple(safety)  # 안전 필수가 맨 앞
    # 안전 확인에 비용이 드는 카탈로그: 예산이 모자라면 보고하고 그 환자의 다른 행동을 막음 (test_engine_policy와 같은 방식)
    costly = SourceCatalog(tuple(s if s.source_id != "system_check" else
                                 s.model_copy(update={"request_cost": C.EffortCost(coordinator_minutes=50)})
                                 for s in pol.catalog.sources.values()))
    pol2 = make_policy(pid, catalog=costly)
    poor = pol2.select(pol2.propose(co), _budget(10), EffortLedger(_budget(10), costly), co)
    assert poor.safety_unfunded and not [a for a in poor.selected if a.patient_alias == PC]
    assert all(s.reason_code in ("over_budget", "blocked_by_unfunded_safety", "duplicate") for s in poor.skipped)


def test_no_action_executes_nothing_and_reports_it():
    co = _cohort()
    pol, plan = _plan("no_action", co, 240)
    assert plan.selected == () and plan.total_cost == C.ZERO_COST
    executable = {a.action_id for a in pol.propose(co) if a.action_type not in C.NON_EXECUTING}
    assert {s.action_id for s in plan.skipped} == executable and {s.reason_code for s in plan.skipped} == {
        "no_action_policy"}


def test_random_is_reproducible_per_seed_and_differs_across_seeds():
    co = _cohort()
    orders = {}
    for seed in range(6):
        pol = RandomPolicy(seed=seed)
        cands = [a for a in pol.propose(co) if a.action_type not in C.NON_EXECUTING and not a.safety_required]
        orders[seed] = [a.action_id for a in sorted(cands, key=pol.order_key)]
        assert orders[seed] == [a.action_id for a in sorted(reversed(cands), key=RandomPolicy(seed=seed).order_key)]
    assert len({tuple(v) for v in orders.values()}) > 1


def test_patient_order_finishes_one_patient_before_the_next():
    co = _cohort()
    _, plan = _plan("patient_order", co, 240)
    rest = [a.patient_alias for a in plan.selected if not a.safety_required]
    assert rest == sorted(rest)  # 별칭 순서로 묶여 있음
    assert PatientOrderPolicy().floor_minutes(_budget(60, per_patient_floor_minutes=10)) == 0.0


def test_cheapest_first_orders_by_expected_cost():
    co = _cohort()
    _, plan = _plan("cheapest_first", co, 240)
    costs = [a.expected_cost.coordinator_minutes for a in plan.selected if not a.safety_required]
    assert costs == sorted(costs)


def test_make_policy_rejects_unknown_and_passes_switches():
    with pytest.raises(ValueError):
        make_policy("oracle_upper")  # 정답을 보는 정책은 엔진에 두지 않음 (sim/ 전용)
    pol = make_policy("full_dcvc", bundles=False, recovery=False)
    assert pol.switches == {"dcv_impact": True, "bundles": False, "fairness_floor": True, "recovery": False}
    assert make_policy("random", seed=3).seed == 3


# ------------------------------------------------------------ full_dcvc 초안
def test_dcv_impact_drops_actions_whose_pairs_are_all_closed():
    co = _cohort()
    pol = FullDCVCPolicy()
    kept = {a.action_id for a in pol.propose(co)}
    for a in LegacyAdapterPolicy().propose(co):
        if a.action_id not in kept:
            assert not a.safety_required and a.action_type not in C.NON_EXECUTING
            assert not pol.open_targets(co, a)  # 움직일 수 있는 열린 쌍이 없음


def test_full_dcvc_prefers_actions_closer_to_confirmation_per_minute():
    co = _cohort()
    pol, plan = _plan("full_dcvc", co, 240)
    led = EffortLedger(_budget(240), pol.catalog)
    for a in plan.selected:
        if a.safety_required:
            led.charge(a)
    rest = [a for a in plan.selected if not a.safety_required]
    assert rest and pol.impact(co, rest[0]) > 0
    first_score = pol.score(co, rest[0], led)
    for a in pol.propose(co):
        if not a.safety_required and a.action_type not in C.NON_EXECUTING and a.action_id not in {
                x.action_id for x in plan.selected if x.safety_required}:
            assert pol.score(co, a, led) <= first_score + 1e-9  # 처음 고른 행동의 점수가 가장 큼


def test_bundles_switch_changes_only_the_score_cost():
    co = _cohort()
    pol_on, pol_off = FullDCVCPolicy(), FullDCVCPolicy(bundles=False)
    led = EffortLedger(_budget(240), pol_on.catalog)
    docs = [a for a in pol_on.propose(co) if a.patient_alias == PB and a.action_type == "REQUEST_DOCUMENT"]
    assert len(docs) >= 2
    led.charge(docs[0])  # 같은 검사지를 이미 요청 -> 두 번째는 추가 항목 비용만
    assert pol_on.cost_minutes(docs[1], led) < pol_off.cost_minutes(docs[1], led)


def test_fairness_floor_switch():
    b = _budget(60, per_patient_floor_minutes=10)
    assert FullDCVCPolicy().floor_minutes(b) == 10 and FullDCVCPolicy(fairness_floor=False).floor_minutes(b) == 0


def test_recovery_switch_holds_instead_of_alternative_source():
    """회복 제거: "모름" 뒤 대체 문서를 찾지 않고 그 항목을 바로 보류·소진. 회복을 켜면 문서 요청."""
    co = CohortState.from_decision_states({PA: _state_missing()})
    for recovery, expect in ((True, "REQUEST_DOCUMENT"), (False, "DEFER")):
        pol = FullDCVCPolicy(recovery=recovery)
        ecog = next(a for a in pol.propose(co) if a.info_key == "ecog" and a.action_type == "ASK_PATIENT")
        co2, _ = pol.apply_outcome(co, ecog, _no_answer(ecog))
        nxt = next(a for a in pol.propose(co2) if a.fingerprint == ecog.fingerprint)
        assert nxt.action_type == expect, recovery
        assert (co2.ops[PA].exhausted_items == frozenset({"ecog"})) == (not recovery)


def test_policies_never_change_judgement_or_facts():
    co = _cohort()
    before = [(p.pair, p.status, p.eligibility) for p in co.pairs()]
    for pid in ALL:
        pol, plan = _plan(pid, co, 60)
        for a in plan.selected[:3]:
            co2, _ = pol.apply_outcome(co, a, C.ActionOutcome(action_id=a.action_id, status="succeeded"))
            assert [(p.pair, p.eligibility) for p in co2.pairs()] == [(p, e) for p, _, e in before]
    assert [(p.pair, p.status, p.eligibility) for p in co.pairs()] == before
    assert OpsState() == OpsState()  # 운영 상태 기본값 비교 가능 (정책 간 공통 시작점)
