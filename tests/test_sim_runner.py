"""WP6 실행기 (설계 §9 테스트 계획): 결정성·seed, 실행 전 검사, 실패·무응답, 예산, LLM 0건, oracle 격리, 공통 난수,
규칙 GT 대비 안전, 웹·시뮬레이터 같은 정책 함수, 문서 확인, 기록 계약. 합성 데모 코호트(sim.demo)만 씁니다."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ctrec import llm
from ctrec.engine import audit
from ctrec.engine import contracts as C
from ctrec.engine.baselines import make_policy, policies
from ctrec.engine.bundles import SourceCatalog
from ctrec.engine.experiment import ExperimentSpecError, with_catalog_hash
from ctrec.sim import demo, runner
from ctrec.sim.cohort import make_cohort
from ctrec.sim.environment import CohortEnvironment
from ctrec.sim.evaluator import OfflineLLMRequest, OfflineMatcherClient
from ctrec.sim.response_model import ResponseModel, SourceResponse

SRC = Path(__file__).resolve().parents[1] / "src" / "ctrec"
ALL = sorted(policies())
WIDE = C.EffortBudget(coordinator_minutes=400, clinician_minutes=60, horizon_steps=10)
TIGHT = C.EffortBudget(coordinator_minutes=45, clinician_minutes=60, horizon_steps=10)


@pytest.fixture(scope="module")
def cohort():
    return demo.cohort()


def _run(cohort, pid="legacy_greedy", budget=WIDE, seed=1, **kw):
    return runner.run(runner.build_spec("t", pid, cohort, budget, seed, kw.pop("cfg", None)), cohort, **kw)


@pytest.fixture(scope="module")
def wide_runs(cohort):
    return {pid: _run(cohort, pid) for pid in ALL}


# ------------------------------------------------------------ 결정성·seed
@pytest.mark.parametrize("pid", ["legacy_greedy", "full_dcvc", "random"])
def test_same_spec_twice_same_result_hash(cohort, pid):
    a, b = _run(cohort, pid, TIGHT), _run(cohort, pid, TIGHT)
    assert a.result_hash == b.result_hash
    assert [r.model_dump(mode="json") for r in a.records] == [r.model_dump(mode="json") for r in b.records]
    assert a.manifest["spec_hash"] == b.manifest["spec_hash"]


def test_seed_changes_responses(cohort):
    outs = {seed: [(r.action.action_id, r.outcome.status) for r in _run(cohort, seed=seed).records]
            for seed in range(4)}
    assert len({tuple(v) for v in outs.values()}) > 1


def test_experiment_id_is_not_part_of_the_spec_hash(cohort):
    a = runner.build_spec("a", "legacy_greedy", cohort, TIGHT, 1)
    b = runner.build_spec("b", "legacy_greedy", cohort, TIGHT, 1)
    assert a.spec_hash() == b.spec_hash()
    assert runner.run(a, cohort).result_hash == runner.run(b, cohort).result_hash


# ------------------------------------------------------------ 실행 전 검사
def test_catalog_hash_missing_or_different_is_rejected(cohort):
    spec = runner.build_spec("t", "legacy_greedy", cohort, TIGHT, 1)
    no_hash = spec.model_copy(update={"data_version": spec.data_version.model_copy(
        update={"source_catalog_hash": None})})
    with pytest.raises(ExperimentSpecError, match="source_catalog_hash_missing"):
        runner.run(no_hash, cohort)
    other = SourceCatalog(tuple(s.model_copy(update={"per_item_minutes": 9.0}) if s.source_id == "lab_report" else s
                                for s in SourceCatalog().sources.values()))
    with pytest.raises(ExperimentSpecError, match="source_catalog_hash_mismatch"):
        runner.run(spec, cohort, other)
    assert runner.run(with_catalog_hash(spec, other), cohort, other).metrics["pairs"] == 18


def test_cohort_store_and_policy_version_must_match(cohort):
    spec = runner.build_spec("t", "legacy_greedy", cohort, TIGHT, 1)
    other = demo.cohort(salt="another-salt")  # 별칭이 바뀌면 다른 코호트
    with pytest.raises(ExperimentSpecError, match="cohort_hash_mismatch"):
        runner.run(spec, other)
    with pytest.raises(ExperimentSpecError, match="policy_version_mismatch"):
        runner.run(spec.model_copy(update={"policy_version": "9.9"}), cohort)
    trials = demo.trials()[:2]
    small = make_cohort(cohort.cohort_id, cohort.reference_date, trials,
                        [(p.key, list(p.public), list(cohort.oracle.truth[p.key])) for p in cohort.patients],
                        cohort.salt)
    bad = spec.model_copy(update={"data_version": spec.data_version.model_copy(
        update={"cohort_hash": small.digest()})})
    with pytest.raises(ExperimentSpecError, match="store_hash_mismatch"):
        runner.run(bad, small)


# ------------------------------------------------------------ 실패·무응답·예산
def test_failures_add_no_facts_but_are_charged(wide_runs):
    seen = set()
    for r in wide_runs.values():
        for rec in r.records:
            o = rec.outcome
            if o.status in ("failed", "no_response"):
                seen.add(o.error_code)
                assert o.new_fact_ids == () and rec.facts_added == ()
                assert o.error_code in (C.NO_ANSWER, C.UNREACHABLE)
            if o.status == "not_executed":
                assert o.cost_charged == C.ZERO_COST
    assert seen == {C.NO_ANSWER, C.UNREACHABLE}  # 데모 seed에서 두 실패 범위가 모두 나옴
    charged_failures = [rec for r in wide_runs.values() for rec in r.records
                        if rec.outcome.status in ("failed", "no_response") and rec.outcome.cost_charged != C.ZERO_COST]
    assert charged_failures  # 실패·무응답도 요청 작업 비용을 청구 (M9)


@pytest.mark.parametrize("pid", ALL)
@pytest.mark.parametrize("minutes", [0, 10, 45, 120])
def test_budget_never_exceeded_and_charges_add_up(cohort, pid, minutes):
    r = _run(cohort, pid, C.EffortBudget(coordinator_minutes=minutes, clinician_minutes=20, horizon_steps=6))
    total = sum((rec.outcome.cost_charged.coordinator_minutes for rec in r.records), 0.0)
    assert r.metrics["active_minutes"]["coordinator"] == pytest.approx(total)
    assert total <= minutes + 1e-9
    assert sum(rec.outcome.cost_charged.clinician_minutes for rec in r.records) <= 20 + 1e-9
    assert r.metrics["llm_calls"] == 0


def test_no_action_executes_nothing(wide_runs):
    r = wide_runs["no_action"]
    assert r.records == [] and r.metrics["status_final"] == r.metrics["status_initial"]
    assert r.metrics["termination"] == "no_selectable_action"


# ------------------------------------------------------------ LLM 0건
def test_offline_run_never_reaches_a_real_llm_client(cohort, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real LLM client used")
    monkeypatch.setattr(llm, "client", boom)
    r = _run(cohort, "full_dcvc", TIGHT)
    assert r.manifest["llm"]["real_calls"] == 0 and r.manifest["llm"]["offline_matcher_requests"] > 0


def test_offline_client_only_answers_the_matcher():
    c = OfflineMatcherClient()
    with pytest.raises(OfflineLLMRequest):
        c.messages.create(system=[{"type": "text", "text": "other agent"}], messages=[{"role": "user",
                                                                                    "content": "x"}])
    assert c.requests == 0


# ------------------------------------------------------------ oracle 격리
def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out |= {a.name for a in node.names} | ({node.module} if getattr(node, "module", None) else set())
    return out


def test_policies_and_evaluator_never_touch_the_oracle():
    banned = {"oracle", "truth", "OraclePart", "rule_labels"}
    files = [*sorted((SRC / "engine").glob("*.py")), SRC / "sim" / "evaluator.py"]
    for f in files:
        hit = _names(f) & banned
        assert not hit, f"{f.name}: {sorted(hit)}"
    for f in sorted((SRC / "engine").glob("*.py")):
        assert not any(n and n.startswith(("ctrec.sim", "..sim", "sim")) for n in _names(f) if isinstance(n, str)
                       and "sim" in n.split(".")), f.name


def test_policy_view_starts_from_public_facts_only(cohort, wide_runs):
    """정책이 처음 받는 상태의 사실 = public 사실뿐 (정답 사실 수와 다름), 원래 환자 키는 없음."""
    r = runner.run(runner.build_spec("t", "no_action", cohort, TIGHT, 1), cohort)
    for p in cohort.patients:
        alias = cohort.aliases()[p.key]
        st = r.final.patients[alias].legacy_copy()
        assert len(st.fact_ids) == len(p.public) < len(cohort.oracle.truth[p.key])
        assert p.key not in st.model_dump_json()
    for rec in wide_runs["full_dcvc"].records:
        assert not any(p.key in rec.model_dump_json() for p in cohort.patients)


def test_sim_package_defines_no_policy_engine():
    """M14: 정책은 engine/에만 (sim/에는 propose·select를 가진 클래스가 없음 - oracle_upper가 생기면 허용 목록)."""
    for f in sorted((SRC / "sim").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                meths = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
                assert not {"propose", "select"} <= meths, f"{f.name}:{node.name}"


# ------------------------------------------------------------ 공통 난수
def test_common_random_numbers_do_not_depend_on_order(cohort):
    cat = SourceCatalog()
    env = CohortEnvironment(cohort, cat, ResponseModel(7))
    pol = make_policy("legacy_greedy")
    first = runner.run(runner.build_spec("t", "no_action", cohort, TIGHT, 7), cohort).final
    acts = [a for a in pol.propose(first) if a.source_id and a.action_type not in C.NON_EXECUTING]
    forward = [env.execute(a, 0, 1, "sim:1") for a in acts]
    backward = [env.execute(a, 0, 1, "sim:1") for a in reversed(acts)][::-1]
    assert [(x.execution, x.error_code, x.latency_minutes) for x in forward] == [
        (x.execution, x.error_code, x.latency_minutes) for x in backward]
    other = CohortEnvironment(cohort, cat, ResponseModel(7, {"patient": SourceResponse(p_unreachable=1.0,
                                                                                         p_no_answer=0.0)}))
    ask = next(a for a in acts if a.action_type == "ASK_PATIENT")
    assert other.execute(ask, 0, 1, "sim:1").error_code == C.UNREACHABLE  # 정보원 단위 실패


# ------------------------------------------------------------ 규칙 GT 대비 안전 (일관성 검사)
@pytest.mark.parametrize("pid", ALL)
def test_no_system_confirmation_against_the_rule_gt(wide_runs, pid):
    gt = wide_runs[pid].metrics["rule_gt"]
    assert gt["confirmed_eligible_gt_ineligible"] == 0 and gt["rule_gt_disagreements"] == 0
    assert gt["kind"] == "rule_gt_circular"


def test_wide_budget_confirms_pairs_and_llm_criteria_stay_unconfirmed(wide_runs):
    m = wide_runs["legacy_greedy"].metrics
    assert m["system_confirmed_eligible"] > 0 and m["system_confirmed_ineligible"] > 0
    final = wide_runs["legacy_greedy"].final
    # T-B에는 LLM 기준(EGFR)이 있어 오프라인 규칙 범위에서는 확정 적격이 될 수 없음
    assert not [p for p in final.pairs() if p.pair.trial_id == "T-B" and p.status == C.PairStatus.CONFIRMED_ELIGIBLE]
    assert wide_runs["legacy_greedy"].manifest["evaluator"]["scope"] == runner.EVALUATOR_SCOPE


def test_document_check_verifies_the_existing_fact(wide_runs, cohort):
    """SYN-04는 나이 80(기록 선언)이 T-C 상한을 넘음 -> 문서 확인이 기존 사실을 검증 상태로 바꿔 확정 부적격."""
    r = wide_runs["legacy_greedy"]
    alias = cohort.aliases()["SYN-04"]
    assert r.final.pair(alias, "T-C").status == C.PairStatus.CONFIRMED_INELIGIBLE
    st = r.final.patients[alias].legacy_copy()
    assert len(st.fact_ids) == len(set(st.fact_ids))
    docs = [rec for rec in r.records if rec.action.patient_alias == alias and rec.action.action_type
            == "REQUEST_DOCUMENT" and rec.action.info_key == "basis:age"]
    assert docs and docs[0].outcome.status == "succeeded" and docs[0].facts_added == ()  # 새 사실 없이 검증만


# ------------------------------------------------------------ 웹·시뮬레이터 같은 정책 함수 (M14)
@pytest.mark.parametrize("pid", ["legacy_greedy", "full_dcvc", "cheapest_first"])
def test_first_round_plan_equals_the_shared_policy_plan(cohort, pid):
    r = _run(cohort, pid, TIGHT)
    start = runner.run(runner.build_spec("t", "no_action", cohort, TIGHT, 1), cohort).final
    pol = make_policy(pid)
    plan = pol.select(pol.propose(start), TIGHT, None, start)
    n = len(plan.selected)
    assert [rec.action.action_id for rec in r.records[:n]] == [a.action_id for a in plan.selected]


# ------------------------------------------------------------ 기록·manifest
@pytest.mark.parametrize("pid", ALL)
def test_records_are_complete_and_round_trip(wide_runs, pid, tmp_path):
    r = wide_runs[pid]
    assert r.metrics["audit"]["status"] == "complete" and r.metrics["audit"]["blocking"] == 0
    path = tmp_path / "decision_records.jsonl"
    w = audit.RecordWriter(path, r.manifest["run_id"])
    for rec in r.records:
        w.write(rec)
    if not r.records:  # no_action: 실행한 행동이 없으면 기록 파일도 없음
        assert not path.exists()
        return
    loaded = audit.load_records(path)
    assert not loaded.issues and len(loaded.records) == len(r.records)


def test_manifest_states_the_claim_limits(wide_runs):
    m = wide_runs["full_dcvc"].manifest
    assert m["claims"] == runner.CLAIMS
    assert m["catalog_claims"]["allowed_claims"] == "relative_policy_comparison_only"
    assert m["response_model"]["basis"] == "assumed"
    assert m["spec"]["data_version"]["source_catalog_hash"] == SourceCatalog().digest()
    assert m["result_hash"] == wide_runs["full_dcvc"].result_hash
    assert "environment" in m and "commit" in m["environment"]


def test_compare_rows_share_the_same_conditions(cohort):
    rows = runner.compare(cohort, ["legacy_greedy", "full_dcvc", "random"], [TIGHT], [1, 2])
    assert len(rows) == 6
    assert {(r["policy_id"], r["seed"]) for r in rows} == {(p, s) for p in ("legacy_greedy", "full_dcvc", "random")
                                                           for s in (1, 2)}
    assert all(r["coordinator_minutes"] <= TIGHT.coordinator_minutes + 1e-9 for r in rows)
