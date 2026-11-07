"""WP6 참고 상한(oracle_upper)과 환자 단위 부트스트랩 신뢰구간 (설계 §4·§5). 합성 데모 코호트만, 실제 LLM 호출 없음."""

from __future__ import annotations

import pytest

from ctrec.engine import contracts as C
from ctrec.engine.baselines import policies
from ctrec.sim import demo, runner, stats
from ctrec.sim.__main__ import main
from ctrec.sim.oracle import DOCUMENT_FIRST, ORACLE_ID, ORACLE_VERSION

BUDGETS = [C.EffortBudget(coordinator_minutes=m, clinician_minutes=60, horizon_steps=10) for m in (45, 90, 400)]
SEEDS = [1, 2, 3]


@pytest.fixture(scope="module")
def rows():
    return runner.compare(demo.cohort(), ["legacy_greedy", "full_dcvc", "cheapest_first", ORACLE_ID], BUDGETS, SEEDS)


# ------------------------------------------------------------ oracle_upper
def test_oracle_is_a_sim_only_reference_not_in_the_engine_registry():
    assert ORACLE_ID not in policies()
    spec = runner.build_spec("t", ORACLE_ID, demo.cohort(), BUDGETS[0], 1)
    assert spec.policy_version == ORACLE_VERSION
    r = runner.run(spec, demo.cohort())
    assert r.manifest["reference_policy"] is True
    assert runner.run(runner.build_spec("t", "legacy_greedy", demo.cohort(), BUDGETS[0], 1),
                      demo.cohort()).manifest["reference_policy"] is False


def test_oracle_preview_matches_the_real_outcome():
    """미리 본 결과 = 실제 결과: 안전 필수가 아닌 실행 행동은 실패·"모름"이 없음 (공통 난수, 반복 없음)."""
    co = demo.cohort()
    for b in BUDGETS:
        for seed in SEEDS:
            r = runner.run(runner.build_spec("t", ORACLE_ID, co, b, seed), co)
            bad = [x for x in r.records
                   if not x.action.safety_required and x.outcome.status in ("failed", "no_response")]
            assert not bad
            assert r.metrics["audit"]["status"] == "complete"
            assert r.metrics["active_minutes"]["coordinator"] <= b.coordinator_minutes + 1e-9


def test_oracle_goes_to_documents_for_values_the_patient_cannot_know():
    co = demo.cohort()
    r = runner.run(runner.build_spec("t", ORACLE_ID, co, BUDGETS[2], 1), co)
    docs = [x.action for x in r.records if x.action.reason_code == DOCUMENT_FIRST]
    assert docs and all(a.action_type == "REQUEST_DOCUMENT" and a.purpose == "eligibility" for a in docs)
    assert any(a.info_key and a.info_key.startswith("lab:") for a in docs)


def test_oracle_is_at_least_as_good_as_every_policy_on_the_demo(rows):
    """데모에서 확인한 성질 (최적값 보장은 아님 - 탐욕 선택): 같은 설정에서 주 지표가 어떤 정책보다도 낮지 않음."""
    by = {}
    for r in rows:
        by.setdefault((r["budget_minutes"], r["seed"]), {})[r["policy_id"]] = r["correct_confirmed_eligible"]
    for key, v in by.items():
        assert v[ORACLE_ID] == max(v.values()), key
    assert any(v[ORACLE_ID] > v["legacy_greedy"] for v in by.values())  # 예산이 모자랄 때 차이가 남


def test_per_patient_results_add_up_to_the_run_metrics(rows):
    for r in rows:
        pp = r["per_patient"].values()
        assert sum(p["confirmed_eligible"] for p in pp) == r["confirmed_eligible"]
        assert sum(p["correct_confirmed_eligible"] for p in pp) == r["correct_confirmed_eligible"]
        assert sum(p["confirmed_ineligible"] for p in pp) == r["confirmed_ineligible"]
        assert sum(p["questions"] for p in pp) == r["questions"]
        assert abs(sum(p["coordinator_minutes"] for p in pp) - r["coordinator_minutes"]) < 1e-6
        assert r["correct_confirmed_eligible"] <= r["confirmed_eligible"]


# ------------------------------------------------------------ 부트스트랩
def _ci(rows, **kw):
    return {(c["budget_minutes"], c["policy_id"], c["metric"]): c for c in stats.bootstrap_ci(rows, **kw)}


def test_bootstrap_is_deterministic_and_brackets_the_estimate(rows):
    a, b = stats.bootstrap_ci(rows, n_boot=300), stats.bootstrap_ci(rows, n_boot=300)
    assert a == b
    assert stats.bootstrap_ci(rows, n_boot=300, seed=1) != a
    for c in a:
        assert c["ci_low"] <= c["estimate"] <= c["ci_high"]
        assert c["n_patients"] == 6 and c["n_cohorts"] == 1 and c["n_seeds"] == 3


def test_bootstrap_estimate_is_the_seed_average_total(rows):
    ci = _ci(rows, n_boot=200)
    for b in BUDGETS:
        m = b.coordinator_minutes
        for pid in ("legacy_greedy", ORACLE_ID):
            mine = [r["correct_confirmed_eligible"] for r in rows if r["budget_minutes"] == m and r["policy_id"] == pid]
            assert ci[(m, pid, "correct_confirmed_eligible")]["estimate"] == pytest.approx(sum(mine) / len(mine),
                                                                                         abs=1e-4)


def test_paired_difference_with_itself_is_exactly_zero(rows):
    """같은 재표본으로 짝지으므로 자기 자신과의 차이는 구간까지 0 (값이 환자마다 다른 400분에서 확인 - 짝이 어긋나면 0이 아님)."""
    ci = _ci(rows, n_boot=200)
    c = ci[(400.0, "legacy_greedy", "correct_confirmed_eligible")]
    assert c["ci_low"] < c["ci_high"]  # 환자 사이 변동이 있음
    assert c["diff"] == c["diff_low"] == c["diff_high"] == 0
    o = ci[(90.0, ORACLE_ID, "correct_confirmed_eligible")]
    assert o["diff"] > 0 and o["diff_low"] <= o["diff"] <= o["diff_high"]


def test_bootstrap_refuses_unpaired_rows(rows):
    one_seed_less = [r for r in rows if not (r["policy_id"] == "full_dcvc" and r["seed"] == 3)]
    with pytest.raises(stats.BootstrapError):
        stats.bootstrap_ci(one_seed_less, n_boot=10)
    with pytest.raises(stats.BootstrapError):
        stats.bootstrap_ci([{k: v for k, v in rows[0].items() if k != "per_patient"}], n_boot=10)
    with pytest.raises(stats.BootstrapError):
        stats.bootstrap_ci(rows, n_boot=0)


def test_bootstrap_without_reference_leaves_differences_empty(rows):
    c = stats.bootstrap_ci([r for r in rows if r["policy_id"] == ORACLE_ID], n_boot=50)[0]
    assert c["diff"] is None and c["estimate"] is not None


def test_cli_prints_oracle_and_intervals(capsys):
    assert main(["--budgets", "90", "--seeds", "1,2", "--policies", "legacy_greedy", "--oracle",
                 "--bootstrap", "100"]) == 0
    out = capsys.readouterr().out
    assert "oracle_upper\tTrue" in out and "correct_confirmed_eligible" in out
    assert "부트스트랩 100회" in out and "참고 상한" in out
