"""생성기 코호트 -> 시뮬레이터 변환(대응표 ①②③), LLM 기준 결정적 판정기, "모름" 재질문 횟수, 정보원 실패 뒤 문서 대체.

생성기 예시(신증후군 소아, data/cohort)와 NSCLC 슬롯 후보(data/nsclc/cohort)만 씁니다. 실제 LLM 호출 없음."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

from ctrec.cohort import generate as G
from ctrec.engine import contracts as C
from ctrec.engine.bundles import SourceCatalog
from ctrec.engine.cohort_state import CohortState
from ctrec.engine.policy import LegacyAdapterPolicy, no_answer_key
from ctrec.sim import demo, from_generator as FG, runner
from ctrec.sim.cohort import make_cohort
from ctrec.sim.environment import CohortEnvironment
from ctrec.sim.evaluator import make_fact
from ctrec.sim.judge import CategoricalJudge, encode
from ctrec.sim.response_model import ResponseModel

ROOT = Path(__file__).resolve().parents[1]
NSCLC_MAP = ROOT / "data" / "nsclc" / "cohort" / "source_map.json"
EXAMPLE_MAP = ROOT / "data" / "cohort" / "source_map.json"
FIELD_RE = re.compile(r"^[a-z0-9_:() ./+\-]+$")
BUDGET = C.EffortBudget(coordinator_minutes=600, clinician_minutes=100, horizon_steps=8)


@pytest.fixture(scope="module")
def gen():
    cfg, sd, cat, parsed = G.load_inputs("data/cohort/generator_config.json")
    sc = next(s for s in cfg.scenarios if s.scenario_id == "neph-k3-catalog-source")
    sc = sc.model_copy(update={"n_patients": 6})
    return G.generate_cohort(cfg, sd, cat, parsed, sc, 0, G.config_hash(cfg, sd, cat)), sd


@pytest.fixture(scope="module")
def sim(gen):
    gc, sd = gen
    return FG.to_sim_cohort(gc, sd, FG.load_source_map(EXAMPLE_MAP))


# ------------------------------------------------------------ 대응표
def test_nsclc_map_covers_every_candidate_slot_exactly():
    rows = list(csv.DictReader(open(ROOT / "data/nsclc/cohort/slot_candidates.csv", encoding="utf-8-sig")))
    smap = FG.load_source_map(NSCLC_MAP)
    assert set(smap.slots) == {r["slot_id"] for r in rows} and len(smap.slots) == 55
    engine = set(SourceCatalog().sources)
    assert set(smap.sources.values()) <= engine
    for sid, m in smap.slots.items():
        assert all(FIELD_RE.match(f) and "*" not in f for f in m.fields), sid  # 정확한 항목 (패턴 아님)
        assert m.sources and set(m.sources) <= engine, sid
        if not m.fields:
            assert sid.startswith("judge.") or m.status == "unresolved", sid
    assert smap.status == "draft"


def test_example_map_covers_the_generator_dictionary(gen):
    _, sd = gen
    smap = FG.load_source_map(EXAMPLE_MAP)
    assert set(sd.slots) - set(smap.core_slots) <= set(smap.slots)
    cat = json.loads((ROOT / "data/cohort/source_catalog.json").read_text(encoding="utf-8"))
    assert set(cat["sources"]) <= set(smap.sources)


def test_conversion_rejects_unmapped_slots_and_sources(gen):
    gc, sd = gen
    smap = FG.load_source_map(EXAMPLE_MAP)
    some = sorted({s for p in gc.public.patients for s in p.known} - set(smap.core_slots))[0]
    with pytest.raises(FG.ConversionError, match="대응표 ②"):
        FG.to_sim_cohort(gc, sd, smap.model_copy(update={"slots": {k: v for k, v in smap.slots.items() if k != some}}))
    with pytest.raises(FG.ConversionError, match="대응표 ①"):
        FG.to_sim_cohort(gc, sd, smap.model_copy(update={"sources": {}}))


# ------------------------------------------------------------ 변환
def test_public_facts_are_known_slots_and_truth_adds_hidden(gen, sim):
    gc, sd = gen
    smap = FG.load_source_map(EXAMPLE_MAP)
    for p, sp in zip(gc.public.patients, sim.patients):
        assert sp.key == p.patient_id and sp.trials == tuple(p.candidate_trials)
        pub = {f.slot for f in sp.public}
        truth = {f.slot for f in sim.oracle.truth[sp.key]}
        expect = {s for s in p.known if s not in smap.core_slots or s in ("age", "sex")} - {"lab_age_days"}
        expect = {s for s in expect if smap.slots.get(s) is None or smap.slots[s].fields}
        assert pub == expect
        hidden = gc.oracle.hidden.get(p.patient_id, {})
        assert truth == pub | {s for s in hidden if smap.slots[s].fields}
        for s, v in hidden.items():  # 숨긴 값은 정답에만
            assert not any(f.slot == s for f in sp.public)
        for f in sp.public:
            if f.field.startswith("lab:"):
                age = int(float(p.known["lab_age_days"]))
                assert f.observed_at < gc.reference_date and f.observed_at is not None and age >= 0
            if f.field not in ("age", "sex", "ecog") and not (f.field.startswith("lab:") and isinstance(f.value, float)):
                assert f.value == encode(f.slot, p.known[f.slot])  # 값 표기 ③


def test_environment_reveals_only_the_criterion_slots(sim):
    cat = SourceCatalog()
    env = CohortEnvironment(sim, cat, ResponseModel(0, {k: v.model_copy(update={"p_unreachable": 0.0,
                                                                                  "p_no_answer": 0.0})
                                                        for k, v in ResponseModel(0).responses.items()}))
    start = runner.run(runner.build_spec("t", "no_action", sim, BUDGET, 0), sim).final
    pol = LegacyAdapterPolicy()
    seen = 0
    for a in pol.propose(start):
        if a.action_type not in ("ASK_PATIENT", "REQUEST_DOCUMENT") or not a.target_criteria:
            continue
        slots = {s for k in a.target_criteria for s in sim.criterion_slots.get(k, ())}
        res = env.execute(a, 0, 1, "sim:1")
        for f in res.facts:
            seen += 1
            assert f.source_reference.quote and (not slots or any(s in str(f.value) or f.field.startswith(("lab:", "age",
                                                                                                          "ecog"))
                                                                    for s in slots))
        if slots:
            got = {sim_slot for sim_slot in slots}
            revealed = [x for x in env.revealable(a.patient_alias, a.source_id, a)]
            assert {x.slot for x in revealed} <= got
    assert seen > 0


def test_generator_cohort_runs_deterministically_and_cleanly(sim):
    a = runner.run(runner.build_spec("t", "full_dcvc", sim, BUDGET, 3), sim)
    b = runner.run(runner.build_spec("t", "full_dcvc", sim, BUDGET, 3), sim)
    assert a.result_hash == b.result_hash
    assert a.metrics["audit"]["status"] == "complete" and a.metrics["rule_gt"]["rule_gt_disagreements"] == 0
    assert a.manifest["llm"]["real_calls"] == 0
    assert a.manifest["evaluator"]["scope"] == runner.JUDGE_SCOPE and not a.manifest["evaluator"]["judge_include_draft"]
    assert sum(p.trials is not None for p in sim.patients) == len(sim.patients)
    assert a.metrics["pairs"] == sum(len(p.trials) for p in sim.patients)  # 후보 시험만 평가


# ------------------------------------------------------------ 판정기
def test_judge_reads_slot_values_from_facts_only(gen):
    _, sd = gen
    b = next(x for x in sd.bindings if x.status in ("auto", "reviewed") and x.truth == "categorical" and not x.also)
    judge = CategoricalJudge(sd.bindings, sd.slots, "2026-10-12")
    rule = G.load_parsed([b.trial_id])[b.trial_id]
    rule = next(r for r in rule.rules if r.rule_id == b.rule_id)
    alias = "PA-" + "0" * 16
    inside, outside = b.values[0], next(v for v in sd.slots[b.slot].values if v not in b.values)
    fact = FG.slot_facts(b.slot, inside, sd, FG.load_source_map(EXAMPLE_MAP), None, "2026-10-12", 1)[0]
    pf = make_fact(alias, fact, seq=0, origin="t", source_type="clinical_record", verified=True, ref_kind="document",
                   ref=None)
    yes = "yes" if b.op == "in" else "no"
    assert judge.assess(b.trial_id, rule, [pf]).holds == yes
    other = make_fact(alias, fact.model_copy(update={"value": encode(b.slot, outside)}), seq=1, origin="u",
                      source_type="clinical_record", verified=True, ref_kind="document", ref=None)
    assert judge.assess(b.trial_id, rule, [other]).holds == ("no" if yes == "yes" else "yes")
    assert judge.assess(b.trial_id, rule, []).holds == "unknown"  # 값 없음
    assert judge.assess(b.trial_id, rule, [pf, other]).holds == "unknown"  # 상충 -> 모름
    a = judge.assess(b.trial_id, rule, [pf])
    assert a.evidence == pf.source_reference.quote and a.evidence_type == "explicit"


def test_judge_skips_draft_bindings_unless_asked(gen):
    _, sd = gen
    drafts = [b for b in sd.bindings if b.status == "draft" and b.slot and b.truth]
    assert drafts
    assert not set(b.key for b in drafts) & set(CategoricalJudge(sd.bindings, sd.slots, "2026-10-12").bindings)
    with_draft = CategoricalJudge(sd.bindings, sd.slots, "2026-10-12", include_draft=True)
    assert set(b.key for b in drafts) <= set(with_draft.bindings)
    assert with_draft.digest() != CategoricalJudge(sd.bindings, sd.slots, "2026-10-12").digest()


def test_judge_lets_llm_criteria_be_decided(sim):
    """판정기가 있으면 LLM 기준 일부가 확정 판정(MET/NOT_MET)을 받고, 없으면 전부 판정 불가."""
    without = make_cohort(sim.cohort_id, sim.reference_date, list(sim.trials),
                          [(p.key, list(p.public), list(sim.oracle.truth[p.key]), p.trials) for p in sim.patients],
                          sim.salt, dict(sim.criterion_slots), None)

    def llm_decided(co):
        r = runner.run(runner.build_spec("t", "no_action", co, BUDGET, 0), co)
        return sum(c.status in ("MET", "NOT_MET") for a in r.final.aliases()
                   for t in r.final.patients[a].legacy_copy().trials.values() for c in t.criteria
                   if c.record and c.record.engine == "llm")
    assert llm_decided(without) == 0
    assert llm_decided(sim) > 0


# ------------------------------------------------------------ "모름" 재질문 횟수
def test_max_item_attempts_default_marks_the_item_failed_at_once():
    co = runner.run(runner.build_spec("t", "no_action", demo.cohort(), BUDGET, 0), demo.cohort()).final
    for n, failed_after_first in ((1, True), (2, False)):
        pol = LegacyAdapterPolicy(max_item_attempts=n)
        ask = next(a for a in pol.propose(co) if a.action_type == "ASK_PATIENT" and a.info_key == "ecog")
        same = lambda c: next(a for a in pol.propose(c) if a.action_id == ask.action_id)  # noqa: E731
        out = C.ActionOutcome(action_id=ask.action_id, status="no_response", error_code=C.NO_ANSWER)
        co2, _ = pol.apply_outcome(co, ask, out)
        ops = co2.ops[ask.patient_alias]
        assert bool(ops.failed_items) == failed_after_first
        assert ops.attempts[no_answer_key(ask.source_id, "ecog")] == 1
        if n == 2:
            again = same(co2)
            assert again.action_type == "ASK_PATIENT"  # 한 번 더 물음
            co3, _ = pol.apply_outcome(co2, again, out.model_copy(update={"action_id": again.action_id}))
            nxt = same(co3)
            assert co3.ops[ask.patient_alias].failed_items and nxt.action_type == "REQUEST_DOCUMENT"
    with pytest.raises(ValueError):
        LegacyAdapterPolicy(max_item_attempts=0)


def test_ask_attempts_sensitivity_changes_the_spec_and_reasks():
    co = demo.cohort()
    one = runner.build_spec("t", "legacy_greedy", co, BUDGET, 1)
    two = runner.build_spec("t", "legacy_greedy", co, BUDGET, 1, {"max_item_attempts": 2})
    assert one.spec_hash() != two.spec_hash()
    r1, r2 = runner.run(one, co), runner.run(two, co)
    assert r2.manifest["max_item_attempts"] == 2 and r1.manifest["max_item_attempts"] == 1

    def reasks(r):
        seen, n = set(), 0
        for rec in r.records:
            k = (rec.action.patient_alias, rec.action.fingerprint, rec.action.action_type, rec.action.source_id)
            n += k in seen and rec.action.action_type == "ASK_PATIENT"
            seen.add(k)
        return n
    assert reasks(r1) == 0 and reasks(r2) > 0
    assert r2.metrics["patient_burden"]["questions"] > r1.metrics["patient_burden"]["questions"]
    rows = runner.compare(co, ["legacy_greedy"], [BUDGET], [1], ask_attempts=[1, 2])
    assert [r["ask_attempts"] for r in rows] == [1, 2] and rows[1]["policy_config"] == {"max_item_attempts": 2}


# ------------------------------------------------------------ 정보원 실패 뒤 문서 대체
def test_unreachable_patient_still_allows_document_requests():
    """환자 연락 불가(정보원 단위 실패)여도 검사 수치는 검사지로 확인 - 대체 정보원 목록이 없다고 보류하지 않음."""
    co = runner.run(runner.build_spec("t", "no_action", demo.cohort(), BUDGET, 0), demo.cohort()).final
    pol = LegacyAdapterPolicy()
    p05 = demo.cohort().aliases()["SYN-05"]  # ECOG·검사 수치 모두 모름
    ask = next(a for a in pol.propose(co) if a.patient_alias == p05 and a.action_type == "ASK_PATIENT"
               and a.info_key == "lab:hemoglobin")
    co2, trans = pol.apply_outcome(co, ask, C.ActionOutcome(action_id=ask.action_id, status="failed",
                                                             error_code=C.UNREACHABLE))
    assert "patient_interview" in co2.ops[ask.patient_alias].failed_sources
    assert not co2.ops[ask.patient_alias].exhausted_trials and trans == ()
    nxt = next(a for a in pol.propose(co2) if a.action_id == ask.action_id)
    assert (nxt.action_type, nxt.source_id, nxt.reason_code) == ("REQUEST_DOCUMENT", "lab_report",
                                                                 "alternative_source_after_failure")
    ecog = next(a for a in pol.propose(co2) if a.patient_alias == ask.patient_alias and a.info_key == "ecog")
    assert ecog.action_type == "REQUEST_DOCUMENT"  # 같은 환자의 다른 문진 항목도 문서로
    no_rec = LegacyAdapterPolicy()
    no_rec.recovery = False
    co3, _ = no_rec.apply_outcome(co, ask, C.ActionOutcome(action_id=ask.action_id, status="failed",
                                                            error_code=C.UNREACHABLE))
    assert set(ask.target_trial_ids) <= co3.ops[ask.patient_alias].exhausted_trials  # 회복 끔: 바로 보류
    assert isinstance(co3, CohortState)
