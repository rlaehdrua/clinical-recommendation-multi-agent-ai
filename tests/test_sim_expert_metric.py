"""WP6 주 지표 (11-07 팀 결정 1): 판단 기준 대기 적격 GT와 EXPERT_REVIEW 도달을 확정 적격과 따로 셈. 합성 데이터만."""

from __future__ import annotations

import pytest

from ctrec.cohort import spec as cohort_spec
from ctrec.engine import contracts as C
from ctrec.engine.bundles import SourceCatalog
from ctrec.sim import demo, from_generator as FG, runner
from ctrec.sim.cohort import make_cohort
from ctrec.sim.judge import CategoricalJudge
from ctrec.sim.metrics import ELIGIBLE, PENDING_EXPERT, correct_resolved, gt_comparison

B400 = C.EffortBudget(coordinator_minutes=400, clinician_minutes=60, horizon_steps=10)


def _expert_cohort():
    """데모 + T-C에 원문에 구체 기준이 없는 임상 판단 기준(기대 여명) 하나."""
    le = demo._rule("I5", "Life expectancy >= 12 weeks", "other", structured=False).model_copy(
        update={"underspecified": True})
    base = demo.cohort()
    trials = [demo._trial(t.trial_id, [*t.parsed.rules, le] if t.trial_id == "T-C" else list(t.parsed.rules),
                          dict(t.rule_meta)) for t in base.trials]
    pats = [(p.key, list(p.public), list(base.oracle.truth[p.key]), p.trials) for p in base.patients]
    return make_cohort("demo-expert", base.reference_date, trials, pats, base.salt)


def test_correct_resolved_never_mixes_confirmed_and_pending():
    S = C.PairStatus
    assert correct_resolved(S.CONFIRMED_ELIGIBLE, ELIGIBLE) == (True, False)
    assert correct_resolved(S.EXPERT_REVIEW, PENDING_EXPERT) == (False, True)
    assert correct_resolved(S.CONFIRMED_ELIGIBLE, PENDING_EXPERT) == (False, False)  # 판단 없이 확정은 성공 아님
    assert correct_resolved(S.EXPERT_REVIEW, ELIGIBLE) == (False, False)


def test_pending_expert_label_and_metric_on_demo_with_a_judgement_criterion():
    co = _expert_cohort()
    labels = runner.rule_labels(co, SourceCatalog())
    pending = {k for k, v in labels.items() if v == PENDING_EXPERT}
    assert pending and all(k.endswith("|T-C") for k in pending)  # 판단 기준이 있는 시험만
    assert ELIGIBLE not in {v for k, v in labels.items() if k.endswith("|T-C")}  # 확정 적격 라벨과 섞이지 않음
    for pid in ("legacy_greedy", "full_dcvc"):
        r = runner.run(runner.build_spec("t", pid, co, B400, 1), co)
        m = r.metrics
        assert m["correct_expert_ready"] >= 1 and m["correct_expert_ready"] <= len(pending)
        assert m["correct_eligible_resolved"] == m["correct_confirmed_eligible"] + m["correct_expert_ready"]
        assert m["rule_gt"]["gt_eligible_pending_expert"] == len(pending)
        assert m["rule_gt"]["confirmed_eligible_gt_pending_expert"] == 0  # 엔진은 판단 기준을 확정하지 않음
        assert m["rule_gt"]["rule_gt_disagreements"] == 0
        assert not any(p.status == C.PairStatus.CONFIRMED_ELIGIBLE and p.pair.key in pending for p in r.final.pairs())
        for k in runner.PRIMARY_PARTS:
            assert sum(v[k] for v in r.per_patient.values()) == m[k]
        row = runner.compare(co, [pid], [B400], [1])[0]
        assert row["correct_eligible_resolved"] == m["correct_eligible_resolved"] and row["expert_review"] >= 1


def test_confirming_a_pending_expert_pair_counts_as_disagreement():
    co = _expert_cohort()
    labels = runner.rule_labels(co, SourceCatalog())
    r = runner.run(runner.build_spec("t", "legacy_greedy", co, B400, 1), co)
    key = next(p.pair.key for p in r.final.pairs() if p.status == C.PairStatus.CONFIRMED_ELIGIBLE)
    gt = gt_comparison(r.final, {**labels, key: PENDING_EXPERT})
    assert gt["confirmed_eligible_gt_pending_expert"] == 1 and gt["rule_gt_disagreements"] == 1


def test_expert_slots_match_the_generator_definition_when_present():
    if not hasattr(cohort_spec, "is_expert_slot"):
        pytest.skip("#22 미병합: cohort.spec에 판단 슬롯 정의 없음")
    assert FG.EXPERT_SLOTS == cohort_spec.EXPERT_SLOTS and FG.EXPERT_SLOT_PREFIX == cohort_spec.EXPERT_SLOT_PREFIX


def test_expert_rules_are_marked_and_not_judged():
    assert FG.is_expert_slot("judge.life_expectancy") and FG.is_expert_slot("cm.cardiac")
    assert not FG.is_expert_slot("lab.anc") and not FG.is_expert_slot(None)
    trials = demo.trials()
    marked = FG.mark_expert(trials, frozenset({"T-B:I4"}))
    assert next(r for r in marked[1].parsed.rules if r.rule_id == "I4").underspecified
    assert not any(r.underspecified for r in trials[1].parsed.rules)  # 원본은 그대로
    assert sum(r.underspecified for t in marked for r in t.parsed.rules) == 1
    b = cohort_spec.Binding.model_construct(trial_id="T-B", rule_id="I4", slot="judge.x", truth={"in": ["y"]},
                                            status="reviewed")
    b_key = getattr(b, "key", None)
    if b_key is None:
        pytest.skip("Binding.key 없음")
    assert b_key in CategoricalJudge([b], {}, "2026-01-01").bindings
    assert b_key not in CategoricalJudge([b], {}, "2026-01-01", expert_rules=frozenset({b_key})).bindings
