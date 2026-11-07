"""생성기 코호트 -> 시뮬레이터 변환(대응표 ①②③), LLM 기준 결정적 판정기, "모름" 뒤 바꿔 묻기, 정보원 실패 뒤 문서 대체.

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
from ctrec.engine.cost import EffortLedger
from ctrec.engine.policy import REPHRASE_DONE, REPHRASE_PENDING, LegacyAdapterPolicy, rephrase_key
from ctrec.sim import demo, from_generator as FG, runner
from ctrec.sim.cohort import make_cohort
from ctrec.sim.environment import CohortEnvironment
from ctrec.sim.evaluator import make_fact
from ctrec.sim.judge import CategoricalJudge, encode
from ctrec.sim.response_model import DEFAULT_RESPONSES, ResponseModel, SourceResponse

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


# ------------------------------------------------------------ "모름" 뒤 바꿔 묻기
def _ask(pol, co, info_key, alias=None):
    return next(a for a in pol.propose(co) if a.info_key == info_key and a.action_type == "ASK_PATIENT"
                and (alias is None or a.patient_alias == alias))


def _no_answer(a):
    return C.ActionOutcome(action_id=a.action_id, status="no_response", error_code=C.NO_ANSWER)


def _start():
    return runner.run(runner.build_spec("t", "no_action", demo.cohort(), BUDGET, 0), demo.cohort()).final


def test_rephrase_is_off_by_default_and_the_item_moves_to_documents_at_once():
    co, pol = _start(), LegacyAdapterPolicy()
    ask = _ask(pol, co, "ecog")
    co2, _ = pol.apply_outcome(co, ask, _no_answer(ask))
    assert ("patient_interview", "ecog") in co2.ops[ask.patient_alias].failed_items
    nxt = next(a for a in pol.propose(co2) if a.patient_alias == ask.patient_alias and a.info_key == "ecog")
    assert nxt.action_type == "REQUEST_DOCUMENT" and nxt.reason_code == "alternative_source_after_no_answer"


def test_rephrase_asks_once_in_other_words_then_falls_back_to_documents():
    """환자가 알 수 있는 항목(ECOG): 첫 "모름" -> 같은 질문이 아니라 바꿔 묻기 행동(다른 지문·사유) 한 번 -> 또 "모름"이면 문서."""
    co, pol = _start(), LegacyAdapterPolicy(rephrase=True)
    ask = _ask(pol, co, "ecog")
    co2, _ = pol.apply_outcome(co, ask, _no_answer(ask))
    ops = co2.ops[ask.patient_alias]
    assert not ops.failed_items and ops.attempts[rephrase_key("patient_interview", "ecog")] == REPHRASE_PENDING
    again = _ask(pol, co2, "ecog", ask.patient_alias)
    assert again.reason_code == C.REPHRASE and again.source_id == ask.source_id
    assert again.fingerprint == ask.fingerprint + ".rephrase" and again.action_id != ask.action_id
    assert again.target_criteria == ask.target_criteria and again.purpose == ask.purpose
    co3, _ = pol.apply_outcome(co2, again, _no_answer(again))
    ops3 = co3.ops[ask.patient_alias]
    assert ("patient_interview", "ecog") in ops3.failed_items
    assert ops3.attempts[rephrase_key("patient_interview", "ecog")] == REPHRASE_DONE
    nxt = next(a for a in pol.propose(co3) if a.patient_alias == ask.patient_alias and a.info_key == "ecog")
    assert nxt.action_type == "REQUEST_DOCUMENT"  # 세 번째로 묻지 않음


def test_rephrase_is_not_used_for_values_the_patient_cannot_know():
    """검사 수치는 환자 정보원이 다루는 항목이 아님 -> 말을 바꿔도 소용없으므로 바로 문서."""
    co, pol = _start(), LegacyAdapterPolicy(rephrase=True)
    ask = _ask(pol, co, "lab:hemoglobin")
    co2, _ = pol.apply_outcome(co, ask, _no_answer(ask))
    assert (ask.source_id, "lab:hemoglobin") in co2.ops[ask.patient_alias].failed_items
    assert not pol.rephrasable(ask.source_id, ask.required_fields)
    assert all(a.reason_code != C.REPHRASE for a in pol.propose(co2))


def test_rephrase_is_charged_as_a_new_contact():
    """같은 항목의 재질문도 비용 0이 아님: 바꿔 묻기는 별도 청구 키로 요청 비용 + 항목 비용."""
    co, pol = _start(), LegacyAdapterPolicy(rephrase=True)
    ask = _ask(pol, co, "ecog")
    co2, _ = pol.apply_outcome(co, ask, _no_answer(ask))
    again = _ask(pol, co2, "ecog", ask.patient_alias)
    led = EffortLedger(BUDGET, pol.catalog)
    _, k1 = led.charge(ask)
    cost, k2 = led.charge(again)
    src = pol.catalog["patient_interview"]
    assert k2 == k1 + "#rephrase"
    assert cost.coordinator_minutes == src.request_cost.coordinator_minutes + src.per_item_minutes * len(
        again.target_criteria)
    assert led.charge(again)[0] == C.ZERO_COST  # 같은 바꿔 묻기는 두 번 청구하지 않음


def test_rephrase_has_its_own_answer_probability_and_cannot_reveal_unknown_values():
    co, pol = _start(), LegacyAdapterPolicy(rephrase=True)
    resp = {"patient": SourceResponse(p_unreachable=0.0, p_no_answer=1.0, p_no_answer_rephrased=0.0),
            "document": SourceResponse(p_unreachable=0.0, p_no_answer=0.0)}
    cohort = demo.cohort()
    env = CohortEnvironment(cohort, pol.catalog, ResponseModel(0, resp))
    ask = _ask(pol, co, "ecog")
    assert env.execute(ask, 0, 0, "s0").execution == "declined"
    co2, _ = pol.apply_outcome(co, ask, _no_answer(ask))
    again = _ask(pol, co2, "ecog", ask.patient_alias)
    got = env.execute(again, 0, 1, "s1")
    assert got.execution == "answered" and [f.field for f in got.facts] == ["ecog"]
    lab = _ask(pol, co, "lab:hemoglobin")
    fake = lab.model_copy(update={"reason_code": C.REPHRASE})  # 환자가 모르는 값은 바꿔 물어도 "모름"
    assert env.execute(fake, 0, 2, "s2").execution == "declined"


def test_rephrase_sensitivity_changes_the_spec_and_never_repeats_the_same_question():
    co = demo.cohort()
    resp = {**DEFAULT_RESPONSES, "patient": SourceResponse(p_unreachable=0.0, p_no_answer=1.0,
                                                            p_no_answer_rephrased=0.0)}
    off = runner.build_spec("t", "legacy_greedy", co, BUDGET, 1)
    on = runner.build_spec("t", "legacy_greedy", co, BUDGET, 1, {"rephrase": True})
    assert off.spec_hash() != on.spec_hash()
    r0, r1 = runner.run(off, co, responses=resp), runner.run(on, co, responses=resp)
    assert r0.manifest["rephrase"] is False and r1.manifest["rephrase"] is True
    assert r0.metrics["patient_burden"]["rephrased"] == 0 < r1.metrics["patient_burden"]["rephrased"]
    for r in (r0, r1):  # 같은 (환자, 지문, 유형, 정보원) 행동은 두 번 실행되지 않음
        keys = [(x.action.patient_alias, x.action.fingerprint, x.action.action_type, x.action.source_id)
                for x in r.records]
        assert len(keys) == len(set(keys))
    assert r1.metrics["active_minutes"]["coordinator"] > r0.metrics["active_minutes"]["coordinator"]
    rows = runner.compare(co, ["legacy_greedy"], [BUDGET], [1], responses=resp, rephrase=[False, True])
    assert [r["rephrase"] for r in rows] == [False, True] and rows[1]["policy_config"] == {"rephrase": True}
    assert rows[1]["rephrased"] > 0


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


def test_fact_line_writes_whole_number_age_without_decimal():
    """생성기 나이 15.0 -> "나이 15세" (환자값 근거 검사가 원문 표기와 맞춰 봄, 선우 리뷰)."""
    from ctrec.sim.evaluator import fact_line
    from ctrec.sim.cohort import SimFact
    assert fact_line(SimFact(field="age", value=15.0)) == "나이 15세"
    assert fact_line(SimFact(field="ecog", value=1.0)) == "ECOG 1"
    assert fact_line(SimFact(field="age", value=15.5)) == "나이 15.5세"


def test_fact_line_writes_the_lab_date_so_time_windows_can_be_checked():
    """기간 조건 기준의 근거 검사는 인용 줄 안의 날짜를 봄 (PR #32 리뷰: 날짜가 없어 판정이 UNKNOWN으로 되돌아가던 검사 12행)."""
    from ctrec.evidence_directness import _within
    from ctrec.sim.cohort import SimFact
    from ctrec.sim.evaluator import fact_line
    line = fact_line(SimFact(field="lab:platelets", value=150.0, unit="x10^9/L", observed_at="2026-10-01"))
    assert line == "platelets 150 x10^9/L (2026-10-01)"
    assert _within(line, 30, "2026-10-12") and not _within(line, 7, "2026-10-12")  # 7일 기준이면 오래된 검사
    assert fact_line(SimFact(field="lab:platelets", value=150.0, unit="x10^9/L")) == "platelets 150 x10^9/L"  # 날짜 모름
