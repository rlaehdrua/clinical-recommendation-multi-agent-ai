"""WP5 정책 엔진 기반: 예산 초과 0, 문서 중복 청구 0, 결정성, 실패 뒤 사실 생성 금지, 정답 격리,
환자 1명 동등성, 임상 판단 자동 확정 금지, 안전 행동 보존, 배분 관측 (합성 데이터, 실제 Matcher + 오프라인 LLM 대역)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.agents import matcher  # noqa: E402
from ctrec.decision_policy import candidates  # noqa: E402
from ctrec.decision_state import build_state  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine.bundles import SourceCatalog, bundle, charge_key  # noqa: E402
from ctrec.engine.cohort_state import CohortState, OpsState  # noqa: E402
from ctrec.engine.cost import BudgetExceeded, EffortLedger  # noqa: E402
from ctrec.engine.fairness import allocation_report, gini  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy, plan_and_charge, plan_cohort  # noqa: E402
from ctrec.eval_hygiene import AliasMap  # noqa: E402

import decision_fixtures as d  # noqa: E402
from test_offline import _rule  # noqa: E402

PLT = _rule(rule_id="I6", text="Platelets >= 100 x10^9/L", category="lab", field="lab:platelets", operator=">=",
            value="100", unit="x10^9/L")
AM = AliasMap(salt="policy-tests")
PA, PB, PC, PD = (AM.alias(x) for x in ("SYN-PA", "SYN-PB", "SYN-PC", "SYN-PD"))
ENGINE_DIR = Path(__file__).resolve().parents[1] / "src" / "ctrec" / "engine"


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)


def _state_missing(pid="SYN-PA"):
    """ECOG·EGFR 정보 없음 (질문 대상), 두 시험."""
    s = d.store(("age", 60, {"unit": "years"}), patient=pid)
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, d.EGFR]), "T2": d.trial("T2", [d.AGE, d.ECOG])}, s, patient=pid)
    return build_state(v)


def _state_docs(pid="SYN-PB"):
    """검사 수치 두 개가 자기 보고·출처 미상 -> 같은 검사지(lab_report)로 확인할 문서 요청 두 건."""
    s = d.store(("age", 60, {"unit": "years"}),
                ("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-09-01"}),
                ("lab:platelets", 150.0, {"unit": "x10^9/L", "observed_at": "2026-09-01"}), patient=pid)
    meta = {**d.record_meta("I4"), **d.record_meta("I6")}
    v = d.view({"T1": d.trial("T1", [d.AGE, d.HB, PLT], meta=meta)}, s, source_text="합성 검사 수치 60세",
               input_source="unknown", patient=pid)
    return build_state(v)


def _state_conflict(pid="SYN-PC"):
    s = d.store(("age", 60, {"unit": "years"}), ("ecog", 1, {}), ("ecog", 3, {}), patient=pid)
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, d.EGFR])}, s, patient=pid)
    return build_state(v)


def _state_expert(pid="SYN-PD"):
    s = d.store(("age", 60, {"unit": "years"}), ("ecog", 1, {}), patient=pid)
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, d.VAGUE])}, s, patient=pid)
    return build_state(v)


def _cohort():
    return CohortState.from_decision_states({PA: _state_missing(), PB: _state_docs(), PC: _state_conflict(),
                                             PD: _state_expert()})


def _budget(m, **kw):
    return C.EffortBudget(coordinator_minutes=m, clinician_minutes=kw.pop("clin", 60), **kw)


# ------------------------------------------------------------ 코호트 상태
def test_cohort_state_derives_without_changing_judgement():
    states = {PA: _state_missing(), PB: _state_docs()}
    before = {a: s.model_dump_json() for a, s in states.items()}
    co = CohortState.from_decision_states(states)
    assert {a: s.model_dump_json() for a, s in states.items()} == before  # 원본 불변
    for p in co.pairs():
        legacy = states[p.pair.patient_alias].trials[p.pair.trial_id]
        assert p.eligibility == legacy.eligibility  # 판정 값 복사만
        assert {c.rule_id: c.state for c in p.criteria} == {
            c.rule_id: ("UNKNOWN" if c.status == "NEEDS_FULL_EVALUATION" else c.status) for c in legacy.criteria}
    assert co.pair(PA, "T1").status == C.PairStatus.INFO_MISSING
    assert co.pair(PB, "T1").status == C.PairStatus.VERIFICATION_PENDING
    assert co.patients[PA].legacy_copy().patient_id == PA  # 사본에도 원래 ID 없음
    with pytest.raises(ValueError):
        CohortState.from_decision_states({"SYN-PA": _state_missing()})  # 원래 ID 키 거부


def test_cohort_state_is_read_only():
    co = _cohort()
    with pytest.raises(TypeError):
        co.patients[PA] = None  # type: ignore[index]
    c = co.pair(PA, "T1").criteria[0]
    with pytest.raises(Exception):
        c.state = "MET"  # type: ignore[misc]
    copy = co.patients[PA].legacy_copy()
    copy.trials["T1"].eligibility = "ELIGIBLE"
    assert co.patients[PA].legacy_copy().trials["T1"].eligibility != "ELIGIBLE"


# ------------------------------------------------------------ 환자 1명 동등성
@pytest.mark.parametrize("factory", [_state_missing, _state_docs, _state_conflict, _state_expert])
def test_single_patient_order_equals_legacy_policy(factory):
    st = factory()
    legacy, _ = candidates(st)
    co = CohortState.from_decision_states({PA: st})
    pol = LegacyAdapterPolicy()
    eng = pol.propose(co)
    assert [a.fingerprint for a in eng] == [a.fingerprint for a in legacy]
    assert [a.action_type for a in eng] == [C.LEGACY_ACTION_TYPES[a.action_type] for a in legacy]
    plan = pol.select(eng, _budget(10_000, clin=10_000), cohort=co)  # 예산 무제한: 실행 행동 순서 그대로
    executable = [a.fingerprint for a in legacy if a.action_type not in ("DEFER", "FINALIZE")]
    assert [a.fingerprint for a in plan.selected] == executable
    assert plan.skipped == () and plan.safety_unfunded == ()


# ------------------------------------------------------------ 예산
@pytest.mark.parametrize("minutes", [0, 1, 3, 5, 8, 9, 12, 20, 33, 60, 500])
def test_budget_never_exceeded(minutes):
    co = _cohort()
    pol = LegacyAdapterPolicy()
    for floor in (0, min(2, minutes)):
        for clin in (0, 10, 100):
            b = C.EffortBudget(coordinator_minutes=minutes, clinician_minutes=clin, per_patient_floor_minutes=floor)
            plan, led = plan_and_charge(pol, co, b)
            assert plan.total_cost.coordinator_minutes <= minutes + 1e-9
            assert plan.total_cost.clinician_minutes <= clin + 1e-9
            assert led.total == plan.total_cost
            ids = [a.action_id for a in plan.selected] + [s.action_id for s in plan.skipped] + list(plan.safety_unfunded)
            assert len(ids) == len(set(ids))  # 각 행동은 한 곳에만


def test_per_patient_cap_and_ledger_rejects_overcharge():
    co = _cohort()
    pol = LegacyAdapterPolicy()
    b = C.EffortBudget(coordinator_minutes=100, clinician_minutes=100, per_patient_cap_minutes=6)
    plan, led = plan_and_charge(pol, co, b)
    assert all(m <= 6 + 1e-9 for m in led.per_patient.values())
    tiny = EffortLedger(C.EffortBudget(coordinator_minutes=1), pol.catalog)
    ask = next(a for a in pol.propose(co) if a.action_type == "ASK_PATIENT")
    with pytest.raises(BudgetExceeded):
        tiny.charge(ask)
    assert tiny.total == C.ZERO_COST  # 부분 청구 없음


# ------------------------------------------------------------ 정보원 묶음·중복 청구
def test_document_bundle_charged_once():
    co = CohortState.from_decision_states({PB: _state_docs()})
    pol = LegacyAdapterPolicy()
    docs = [a for a in pol.propose(co) if a.action_type == "REQUEST_DOCUMENT"]
    assert {a.source_id for a in docs} == {"lab_report"} and len(docs) == 2  # 두 검사 -> 같은 검사지
    lab = pol.catalog["lab_report"]
    plan, led = plan_and_charge(pol, co, _budget(1000))
    charged = [c for _, k, c in led.charges if k == charge_key(PB, "lab_report")]
    assert len(charged) == 2
    expected = lab.request_cost.coordinator_minutes + lab.per_item_minutes * 2
    assert sum(c.coordinator_minutes for c in charged) == pytest.approx(expected)  # 요청 비용은 1회
    bundles = [b for b in bundle(list(plan.selected), pol.catalog) if b.source_id == "lab_report"]
    assert len(bundles) == 1 and bundles[0].cost.coordinator_minutes == pytest.approx(expected)
    # 다음 단계에서 같은 문서·같은 기준을 다시 계획해도 추가 청구 0
    plan2, led2 = plan_and_charge(pol, co, _budget(1000), led)
    again = [c for (_, k, c) in led2.charges[len(led.charges):] if k == charge_key(PB, "lab_report")]
    assert all(c == C.ZERO_COST for c in again)


def test_same_source_different_patients_charged_separately():
    co = CohortState.from_decision_states({PB: _state_docs("SYN-PB"), PD: _state_docs("SYN-PD")})
    plan, led = plan_and_charge(LegacyAdapterPolicy(), co, _budget(1000))
    assert charge_key(PB, "lab_report") in led.requested and charge_key(PD, "lab_report") in led.requested


# ------------------------------------------------------------ 결정성
def test_same_state_same_plan():
    pol = LegacyAdapterPolicy()
    b = _budget(20, per_patient_floor_minutes=3)
    p1 = plan_cohort(pol, _cohort(), b)
    p2 = plan_cohort(LegacyAdapterPolicy(), _cohort(), b)  # 상태를 새로 만들어도 (uuid 행동 ID와 무관)
    assert p1 == p2 and p1.model_dump_json() == p2.model_dump_json()
    assert all(a.legacy_action_id is None for a in p1.selected)


# ------------------------------------------------------------ 안전 필수 행동
def test_safety_actions_first_and_never_silently_dropped():
    co = _cohort()
    pol = LegacyAdapterPolicy()
    cands = pol.propose(co)
    safety = [a for a in cands if a.safety_required]
    assert safety and all(a.patient_alias == PC and a.action_type == "VERIFY_RECORD" for a in safety)
    plan = pol.select(cands, _budget(5), cohort=co)
    assert plan.selected[0].safety_required  # 안전 확인이 먼저
    # 안전 확인에 비용이 드는 정보원으로 바꾼 카탈로그: 예산이 없으면 보고하고, 그 환자의 다른 행동을 막음
    costly = SourceCatalog(tuple(s if s.source_id != "system_check" else
                                 s.model_copy(update={"request_cost": C.EffortCost(coordinator_minutes=50)})
                                 for s in pol.catalog.sources.values()))
    pol2 = LegacyAdapterPolicy(catalog=costly)
    plan2 = pol2.select(pol2.propose(co), _budget(10), cohort=co)
    assert set(plan2.safety_unfunded) == {a.action_id for a in safety}
    assert not any(a.patient_alias == PC for a in plan2.selected)
    assert any(s.reason_code == "blocked_by_unfunded_safety" for s in plan2.skipped)
    assert any(a.patient_alias != PC for a in plan2.selected)  # 다른 환자는 계속 진행


# ------------------------------------------------------------ 실패·무응답·대체 정보원·보류
def test_failure_adds_no_facts_and_switches_to_alternative_then_holds():
    co = CohortState.from_decision_states({PB: _state_docs()})
    pol = LegacyAdapterPolicy()
    doc = next(a for a in pol.propose(co) if a.source_id == "lab_report")
    facts_before = co.patients[PB].legacy_copy().fact_ids
    crit_before = [p.criteria for p in co.pairs()]
    out = C.ActionOutcome(action_id=doc.action_id, status="failed", error_code="document_unavailable",
                          cost_charged=C.EffortCost(coordinator_minutes=8))
    co2, tr = pol.apply_outcome(co, doc, out)
    assert co2.patients[PB].legacy_copy().fact_ids == facts_before  # 사실 임의 생성 없음
    assert [p.criteria for p in co2.pairs()] == crit_before  # 판정 불변
    assert tr == ()  # 대체 정보원이 남아 있어 아직 보류 아님
    alt = [a for a in pol.propose(co2) if a.fingerprint == doc.fingerprint][0]
    assert alt.source_id == "medical_record" and alt.reason_code == "alternative_source_after_failure"
    out2 = C.ActionOutcome(action_id=alt.action_id, status="no_response", error_code="no_response")
    co3, tr3 = pol.apply_outcome(co2, alt, out2)
    held = [a for a in pol.propose(co3) if a.fingerprint == doc.fingerprint][0]
    assert held.action_type == "DEFER" and held.reason_code == "sources_exhausted" and held.source_id is None
    assert co3.pair(PB, "T1").status == C.PairStatus.BLOCKED_FAILURE
    assert [(t.before, t.after, t.authority) for t in tr3] == [
        (C.PairStatus.VERIFICATION_PENDING, C.PairStatus.BLOCKED_FAILURE, "policy")]
    plan = pol.select(pol.propose(co3), _budget(1000), cohort=co3)
    assert held in plan.held and held not in plan.selected  # 보류는 실행·청구하지 않음
    with pytest.raises(Exception):
        C.ActionOutcome(action_id=doc.action_id, status="failed", error_code="x", new_fact_ids=("F-made-up",))


def test_success_outcome_does_not_change_judgement_in_policy():
    co = CohortState.from_decision_states({PA: _state_missing()})
    pol = LegacyAdapterPolicy()
    ask = next(a for a in pol.propose(co) if a.action_type == "ASK_PATIENT")
    out = C.ActionOutcome(action_id=ask.action_id, status="succeeded", new_fact_ids=("F-new",),
                          cost_charged=C.EffortCost(coordinator_minutes=6))
    co2, tr = pol.apply_outcome(co, ask, out)
    assert tr == () and [p.status for p in co2.pairs()] == [p.status for p in co.pairs()]
    assert co2.ops[PA].attempts[ask.fingerprint] == 1


# ------------------------------------------------------------ 임상 판단
def test_expert_criteria_not_auto_confirmed():
    co = CohortState.from_decision_states({PD: _state_expert()})
    pair = co.pair(PD, "T1")
    vague = next(c for c in pair.criteria if c.rule_id == "E1")
    assert vague.adjudication == "expert" and vague.state == "UNKNOWN" and vague.decided_by == "none"
    assert pair.status == C.PairStatus.EXPERT_REVIEW
    pol = LegacyAdapterPolicy()
    review = next(a for a in pol.propose(co) if a.action_type == "CLINICIAN_REVIEW")
    assert review.source_id == "clinician_review" and review.expected_cost.clinician_minutes > 0
    out = C.ActionOutcome(action_id=review.action_id, status="succeeded",
                          cost_charged=C.EffortCost(clinician_minutes=10))
    co2, _ = pol.apply_outcome(co, review, out)
    assert next(c for c in co2.pair(PD, "T1").criteria if c.rule_id == "E1").state == "UNKNOWN"
    for target in (C.PairStatus.CONFIRMED_ELIGIBLE, C.PairStatus.CONFIRMED_INELIGIBLE):
        with pytest.raises(C.TransitionError):
            C.check_transition(pair.status, target, authority="policy")
    # 실패로 정보원이 소진돼도 임상의 판단 대기는 보류(BLOCKED)로 바뀌지 않음
    assert C.derive_pair_status("UNCERTAIN", {"physician_review"}, exhausted=True) == C.PairStatus.EXPERT_REVIEW


# ------------------------------------------------------------ 정답 격리·확률 금지
POLICY_MODULES = ["cohort_state.py", "cost.py", "bundles.py", "policy.py", "fairness.py", "contracts.py"]
FORBIDDEN_IMPORTS = {"question_eval", "eval_hygiene", "metrics", "cohort_metrics", "llm", "llm_pilot", "pipeline",
                     "web", "orchestrator", "answerers", "sim"}


@pytest.mark.parametrize("name", POLICY_MODULES)
def test_policy_modules_cannot_reach_hidden_truth_or_llm(name):
    tree = ast.parse((ENGINE_DIR / name).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
            mods |= {f"{node.module}.{a.name}" for a in node.names}
    parts = {p for m in mods for p in m.split(".")}
    assert not parts & FORBIDDEN_IMPORTS, (name, parts & FORBIDDEN_IMPORTS)
    src = (ENGINE_DIR / name).read_text(encoding="utf-8").lower()
    for word in ("random", "probab", "oracle", "hidden_fact", "label"):
        assert word not in src.replace("확률", ""), (name, word)


def test_cohort_state_has_no_truth_fields():
    co = _cohort()
    blob = repr(co) + "".join(p.model_dump_json() if hasattr(p, "model_dump_json") else repr(p) for p in co.pairs())
    for word in ("oracle", "hidden", "masked", "label", "rescue", "cleanup", "SYN-PA"):
        assert word not in blob


# ------------------------------------------------------------ 배분 관측
def test_fairness_floor_spreads_allocation_and_report_alarms():
    co = CohortState.from_decision_states({PA: _state_missing("SYN-PA"), PB: _state_missing("SYN-PB"),
                                           PC: _state_missing("SYN-PC")})
    pol = LegacyAdapterPolicy()
    no_floor, led0 = plan_and_charge(pol, co, C.EffortBudget(coordinator_minutes=12))
    rep0 = allocation_report(no_floor, co, led0)
    assert rep0["alarm"] and rep0["zero_allocation"]  # 효율 우선이면 0 배분 환자 발생 -> 경보
    floor, led1 = plan_and_charge(pol, co, C.EffortBudget(coordinator_minutes=21, per_patient_floor_minutes=6))
    rep1 = allocation_report(floor, co, led1)
    assert not rep1["alarm"] and all(r["actions"] > 0 for r in rep1["patients"].values())
    assert rep1["gini_minutes"] is not None and rep1["gini_minutes"] <= rep0["gini_minutes"]
    assert all(r["verification_opportunities"] >= 1 for r in rep1["patients"].values())


def test_gini():
    assert gini([]) is None and gini([0, 0]) == 0.0 and gini([5, 5, 5]) == 0.0
    assert gini([0, 0, 10]) == pytest.approx(2 / 3, abs=1e-6)


def test_policy_engine_protocol():
    assert isinstance(LegacyAdapterPolicy(), C.PolicyEngine)


def test_catalog_validation_and_routing():
    cat = SourceCatalog()
    assert cat.route("ASK_USER", ["ecog"]) == "patient_interview"
    assert cat.route("REQUEST_DOCUMENT", ["lab:hemoglobin"]) == "lab_report"
    assert cat.route("REQUEST_DOCUMENT", ["biomarker"], "biomarker:EGFR") == "pathology_report"
    assert cat.route("REQUEST_DOCUMENT", ["primary_diagnosis", "other_fact"]) == "medical_record"
    assert cat.route("NEEDS_REVIEW", []) == "clinician_review"
    assert cat.route("DEFER", []) is None and cat.route("FINALIZE", []) is None
    assert cat.alternatives("lab_report", frozenset()) == ["medical_record"]
    assert cat.alternatives("lab_report", frozenset({"medical_record"})) == []
    with pytest.raises(ValueError):
        SourceCatalog((C.InformationSource(source_id="a", kind="document", alternatives=("missing",)),))
    with pytest.raises(ValueError):
        SourceCatalog((C.InformationSource(source_id="a", kind="document"),) * 2)
    assert cat.digest() == SourceCatalog().digest()


# ------------------------------------------------------------ WP5: "모름"은 항목 단위 실패
def _ask(pol, co, info_key):
    return next(a for a in pol.propose(co) if a.info_key == info_key and a.action_type == "ASK_PATIENT")


def _no_answer(a):
    return C.ActionOutcome(action_id=a.action_id, status="no_response", error_code=C.NO_ANSWER,
                           cost_charged=C.EffortCost(coordinator_minutes=6))


def test_failure_scope_codes():
    assert C.failure_scope("no_response", C.NO_ANSWER) == "item"
    assert C.failure_scope("failed", C.NO_ANSWER) == "item"
    assert C.failure_scope("no_response", C.UNREACHABLE) == "source"
    assert C.failure_scope("no_response", C.DECLINED_ALL) == "source"
    assert C.failure_scope("failed", "document_unavailable") == "source"  # 모르는 코드는 기존 동작(정보원 단위)
    assert C.failure_scope("succeeded", None) is None and C.failure_scope("not_executed", None) is None


def test_no_answer_blocks_only_that_item_and_keeps_other_questions():
    """ECOG에 "모름" -> 그 항목만 문서 정보원으로, EGFR 문진은 계속. 정보원 전체·쌍 소진 없음."""
    co = CohortState.from_decision_states({PA: _state_missing()})
    pol = LegacyAdapterPolicy()
    ecog = _ask(pol, co, "ecog")
    other = [a for a in pol.propose(co) if a.action_type == "ASK_PATIENT" and a.info_key != "ecog"]
    assert other  # 다른 문진 질문이 있는 상태에서 시작
    co2, tr = pol.apply_outcome(co, ecog, _no_answer(ecog))
    assert tr == () and [p.status for p in co2.pairs()] == [p.status for p in co.pairs()]
    assert co2.ops[PA].failed_sources == frozenset()
    assert co2.ops[PA].failed_items == frozenset({("patient_interview", "ecog")})
    after = {a.fingerprint: a for a in pol.propose(co2)}
    moved = after[ecog.fingerprint]
    assert moved.action_type == "REQUEST_DOCUMENT" and moved.reason_code == "alternative_source_after_no_answer"
    assert moved.source_id == "medical_record" and moved.purpose == "eligibility"
    for a in other:  # 같은 정보원의 다른 항목은 그대로 문진
        assert after[a.fingerprint].action_type == "ASK_PATIENT" and after[a.fingerprint].source_id == "patient_interview"


def test_item_exhausted_blocks_pair_only_when_all_its_items_are_exhausted():
    """ECOG를 문진·문서 모두에서 얻지 못하면 ECOG만 필요한 T2는 소진, ECOG·EGFR가 필요한 T1은 EGFR이 남아 그대로 (Q-D2)."""
    co = CohortState.from_decision_states({PA: _state_missing()})
    pol = LegacyAdapterPolicy()
    ecog = _ask(pol, co, "ecog")
    co2, _ = pol.apply_outcome(co, ecog, _no_answer(ecog))
    doc = next(a for a in pol.propose(co2) if a.fingerprint == ecog.fingerprint)
    co3, tr = pol.apply_outcome(co2, doc, C.ActionOutcome(action_id=doc.action_id, status="no_response",
                                                          error_code=C.NO_ANSWER))
    assert co3.ops[PA].exhausted_items == frozenset({"ecog"}) and co3.ops[PA].failed_sources == frozenset()
    assert co3.pair(PA, "T2").status == C.PairStatus.BLOCKED_FAILURE
    assert co3.pair(PA, "T1").status == co.pair(PA, "T1").status  # EGFR가 아직 남음
    assert [(t.pair.trial_id, t.after, t.authority, t.reason_code) for t in tr] == [
        ("T2", C.PairStatus.BLOCKED_FAILURE, "policy", "item_sources_exhausted")]
    held = next(a for a in pol.propose(co3) if a.fingerprint == ecog.fingerprint)
    assert held.action_type == "DEFER" and held.reason_code == "item_sources_exhausted" and held.source_id is None
    assert co3.patients[PA].legacy_copy().fact_ids == co.patients[PA].legacy_copy().fact_ids  # 사실 생성 없음


@pytest.mark.parametrize("code", [C.UNREACHABLE, C.DECLINED_ALL])
def test_unreachable_or_declined_all_blocks_the_whole_source(code):
    co = CohortState.from_decision_states({PA: _state_missing()})
    pol = LegacyAdapterPolicy()
    ecog = _ask(pol, co, "ecog")
    co2, tr = pol.apply_outcome(co, ecog, C.ActionOutcome(action_id=ecog.action_id, status="no_response",
                                                          error_code=code))
    assert co2.ops[PA].failed_sources == frozenset({"patient_interview"})
    assert all(a.source_id != "patient_interview" for a in pol.propose(co2))  # 문진 전체 중단
    assert {t.reason_code for t in tr} <= {"sources_exhausted"}


def test_one_non_answer_detector_for_all_executors():
    from ctrec.decision_state import DECLINED_ANSWERS
    from ctrec.engine import answers
    from ctrec.engine import record_producer as rp
    assert C.NO_ANSWER == rp.NO_ANSWER_ERROR  # 생산기가 기록하는 코드와 같은 값
    for text in (*DECLINED_ANSWERS, "잘 모르겠어요", "기억이 안 나요", "모름.", "  ", None):
        assert answers.answer_outcome(text) == ("no_response", C.NO_ANSWER), text
    for text in ("네", "ECOG 1이에요", "아니요"):
        assert answers.answer_outcome(text) == ("succeeded", None), text


def _declined_state(info_key: str):
    """실제 Session처럼 "모름" 답이 기록된 상태: 기존 정책은 그 항목을 다시 묻지 않고 DEFER(user_declined_or_exhausted)."""
    st = _state_missing()
    for t in st.trials.values():
        for c in t.criteria:
            if c.info_key == info_key:
                c.asked, c.declined = 1, True
    return st


def test_legacy_decline_defer_becomes_document_request_after_engine_item_failure():
    """실제 Session 경로 (선우 보고): "모름" 뒤 기존 정책은 문진을 다시 제안하지 않고 보류를 냄. 엔진이 그 항목의 실패를
    기록했으면 그 보류도 대체 문서 요청으로 바꿔야 함. 실패 기록이 없으면(엔진 밖의 거부) 보류 그대로."""
    pol = LegacyAdapterPolicy()
    legacy = [a for a in candidates(_declined_state("ecog"), pol.cfg)[0] if a.info_key == "ecog"]
    assert [(a.action_type, a.reason_code) for a in legacy] == [("DEFER", "user_declined_or_exhausted")]

    plain = CohortState.from_decision_states({PA: _declined_state("ecog")})
    held = next(a for a in pol.propose(plain) if a.info_key == "ecog")
    assert (held.action_type, held.reason_code) == ("DEFER", "user_declined_or_exhausted")  # 실패 기록 없음

    ops = {PA: OpsState(failed_items=frozenset({("patient_interview", "ecog")}))}  # 엔진이 기록한 항목 실패
    co = CohortState.from_decision_states({PA: _declined_state("ecog")}, ops=ops)
    moved = next(a for a in pol.propose(co) if a.info_key == "ecog")
    assert (moved.action_type, moved.reason_code, moved.source_id) == (
        "REQUEST_DOCUMENT", "alternative_source_after_no_answer", "medical_record")
    assert moved.purpose == "eligibility" and moved.target_criteria == held.target_criteria

    co2, tr = pol.apply_outcome(co, moved, _no_answer(moved))  # 문서에서도 못 얻음 -> 그 항목만 소진
    assert co2.ops[PA].exhausted_items == frozenset({"ecog"})
    last = next(a for a in pol.propose(co2) if a.info_key == "ecog")
    assert (last.action_type, last.reason_code, last.source_id) == ("DEFER", "item_sources_exhausted", None)
    assert co2.pair(PA, "T1").status != C.PairStatus.BLOCKED_FAILURE  # EGFR가 남아 있음


def test_legacy_decline_defer_becomes_rephrase_when_enabled():
    """웹 경로: "모름" 뒤 기존 정책의 보류도 바꿔 묻기 차례면 바꿔 묻기 질문으로. 바꿔 묻기를 끈 정책은 보류 그대로."""
    from types import MappingProxyType
    from ctrec.engine.policy import REPHRASE_DONE, REPHRASE_PENDING, rephrase_key
    key = rephrase_key("patient_interview", "ecog")
    ops = {PA: OpsState(attempts=MappingProxyType({key: REPHRASE_PENDING}))}
    co = CohortState.from_decision_states({PA: _declined_state("ecog")}, ops=ops)
    asked = next(a for a in LegacyAdapterPolicy(rephrase=True).propose(co) if a.info_key == "ecog")
    assert (asked.action_type, asked.reason_code, asked.source_id) == ("ASK_PATIENT", C.REPHRASE, "patient_interview")
    assert asked.fingerprint.endswith(".rephrase")
    held = next(a for a in LegacyAdapterPolicy().propose(co) if a.info_key == "ecog")
    assert (held.action_type, held.reason_code) == ("DEFER", "user_declined_or_exhausted")
    done = CohortState.from_decision_states({PA: _declined_state("ecog")},
                                            ops={PA: OpsState(attempts=MappingProxyType({key: REPHRASE_DONE}))})
    again = next(a for a in LegacyAdapterPolicy(rephrase=True).propose(done) if a.info_key == "ecog")
    assert again.reason_code != C.REPHRASE  # 바꿔 묻기는 한 번만


# ------------------------------------------------------------ 필수 기준 0개 쌍
def _state_no_required():
    """T0 기준 0개, T1 절차 기준만(waivable), T2 정상 대조군, T3 기준 0개지만 모집 종료, T4 기준 0개 UNCERTAIN."""
    from ctrec.decision_state import CriterionState, DecisionState, TrialState
    crit = lambda tid, rid, req: CriterionState(trial_id=tid, rule_id=rid, kind="inclusion", category="clinical",  # noqa
                                                cohort=None, requirement=req, status="MET", info_key="age")
    return DecisionState(session_id="S", patient_id="SYN-PE", fact_version="v1", trials={
        "T0": TrialState(trial_id="T0", eligibility="ELIGIBLE", match_status=None, open=True),
        "T1": TrialState(trial_id="T1", eligibility="ELIGIBLE", match_status=None, open=True,
                         criteria=[crit("T1", "I1", "waivable")]),
        "T2": TrialState(trial_id="T2", eligibility="ELIGIBLE", match_status=None, open=True,
                         criteria=[crit("T2", "I1", "required")]),
        "T3": TrialState(trial_id="T3", eligibility="ELIGIBLE", match_status=None, open=False),
        "T4": TrialState(trial_id="T4", eligibility="UNCERTAIN", match_status=None, open=True)})


def test_pair_without_required_criteria_goes_to_clinician_review():
    """엔진이 NOT_EVALUATED로 둔 쌍(#14)과 필수 기준 없는 UNCERTAIN 쌍은 기존 정책이 행동을 내지 않음 -> 담당자 검토."""
    pe = AM.alias("SYN-PE")
    co = CohortState.from_decision_states({pe: _state_no_required()})
    status = {p.pair.trial_id: p.status for p in co.pairs()}
    assert status["T0"] == status["T1"] == C.PairStatus.NOT_EVALUATED and status["T4"] == C.PairStatus.INFO_MISSING
    pol = LegacyAdapterPolicy()
    reviews = [a for a in pol.propose(co) if a.reason_code == "no_required_criteria"]
    assert len(reviews) == 1
    r = reviews[0]
    assert r.action_type == "CLINICIAN_REVIEW" and r.source_id == "clinician_review" and not r.safety_required
    assert r.target_trial_ids == ("T0", "T1", "T4")  # 정상 대조군·모집 종료 시험은 제외
    assert r.expected_cost.clinician_minutes > 0
    assert [a.action_id for a in pol.propose(co)] == [a.action_id for a in pol.propose(co)]  # 결정적
    plan = pol.select(pol.propose(co), _budget(60), cohort=co)
    assert r.action_id in {a.action_id for a in plan.selected}
    co2, trans = pol.apply_outcome(co, r, C.ActionOutcome(action_id=r.action_id, status="succeeded"))
    assert trans == () and [p.status for p in co2.pairs()] == [p.status for p in co.pairs()]  # 판정·상태는 그대로


def test_no_review_added_when_every_pair_has_required_criteria():
    assert not [a for a in LegacyAdapterPolicy().propose(_cohort()) if a.reason_code == "no_required_criteria"]


# ------------------------------------------------------------ PR #20 리뷰 회귀 (선우·서연 11-07 재현)
def _state_ecog_hb(pid="SYN-PF"):
    """ECOG·Hb 정보 없음. T1은 ECOG·Hb, T2는 ECOG만 필요. Hb는 검사지(lab_report)로도 얻을 수 있음."""
    s = d.store(("age", 60, {"unit": "years"}), patient=pid)
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG, d.HB]), "T2": d.trial("T2", [d.AGE, d.ECOG])}, s, patient=pid)
    return build_state(v)


def test_source_failure_blocks_only_pairs_with_no_way_left_for_every_item():
    """연락 불가 뒤 ECOG 문서도 실패하면 ECOG만 필요한 T2만 BLOCKED. T1은 Hb 검사지 요청이 남아 그대로 (선우 결함 1)."""
    co = CohortState.from_decision_states({PA: _state_ecog_hb()})
    pol = LegacyAdapterPolicy()
    ecog = _ask(pol, co, "ecog")
    co2, tr = pol.apply_outcome(co, ecog, C.ActionOutcome(action_id=ecog.action_id, status="no_response",
                                                          error_code=C.UNREACHABLE))
    assert tr == ()  # 문서가 남아 있음
    doc = next(a for a in pol.propose(co2) if a.info_key == "ecog")
    assert (doc.action_type, doc.source_id) == ("REQUEST_DOCUMENT", "medical_record")
    co3, tr = pol.apply_outcome(co2, doc, C.ActionOutcome(action_id=doc.action_id, status="failed",
                                                          error_code="document_unavailable"))
    assert co3.ops[PA].exhausted_trials == frozenset() and co3.ops[PA].exhausted_items == frozenset({"ecog"})
    assert co3.pair(PA, "T1").status == C.PairStatus.INFO_MISSING
    assert [(t.pair.trial_id, t.after, t.reason_code) for t in tr] == [
        ("T2", C.PairStatus.BLOCKED_FAILURE, "sources_exhausted")]
    hb = next(a for a in pol.propose(co3) if a.info_key == "lab:hemoglobin")
    assert (hb.action_type, hb.source_id) == ("REQUEST_DOCUMENT", "lab_report")
    co4, tr = pol.apply_outcome(co3, hb, C.ActionOutcome(action_id=hb.action_id, status="failed",
                                                         error_code="document_unavailable"))
    assert co4.pair(PA, "T1").status == C.PairStatus.BLOCKED_FAILURE  # 마지막 항목까지 길이 끊기면 소진
    assert [(t.pair.trial_id, t.after) for t in tr] == [("T1", C.PairStatus.BLOCKED_FAILURE)]


def test_source_failure_without_recovery_still_blocks_the_target_trials():
    """회복 제거(ablation)는 기존 동작 그대로: 대체가 없으면 대상 시험 전체 소진."""
    co = CohortState.from_decision_states({PA: _state_ecog_hb()})
    pol = LegacyAdapterPolicy()
    pol.recovery = False
    ecog = _ask(pol, co, "ecog")
    co2, _ = pol.apply_outcome(co, ecog, C.ActionOutcome(action_id=ecog.action_id, status="no_response",
                                                         error_code=C.UNREACHABLE))
    assert co2.ops[PA].exhausted_trials == frozenset({"T1", "T2"})


def test_legacy_defer_after_unreachable_becomes_document_request():
    """실제 Session 경로 (선우 결함 2): 연락 불가는 failed_sources에만 기록됨. 기존 정책의 보류도 문서 요청으로."""
    pol = LegacyAdapterPolicy()
    ops = {PA: OpsState(failed_sources=frozenset({"patient_interview"}))}
    co = CohortState.from_decision_states({PA: _declined_state("ecog")}, ops=ops)
    moved = next(a for a in pol.propose(co) if a.info_key == "ecog")
    assert (moved.action_type, moved.reason_code, moved.source_id) == (
        "REQUEST_DOCUMENT", "alternative_source_after_failure", "medical_record")
    other = CohortState.from_decision_states({PA: _declined_state("ecog")},
                                             ops={PA: OpsState(failed_sources=frozenset({"lab_report"}))})
    held = next(a for a in pol.propose(other) if a.info_key == "ecog")  # ECOG와 무관한 정보원 실패는 보류 그대로
    assert (held.action_type, held.reason_code) == ("DEFER", "user_declined_or_exhausted")


def test_legacy_reparse_becomes_rephrase_when_pending():
    """"잘 모르겠어요"처럼 기존 목록 밖의 무응답은 기존 정책이 재추출(REPARSE)을 냄 -> 바꿔 묻기 차례면 바꿔 묻기로."""
    from types import MappingProxyType
    from ctrec.engine.policy import REPHRASE_PENDING, rephrase_key
    st = _state_missing()
    for t in st.trials.values():
        for c in t.criteria:
            if c.info_key == "ecog":
                c.asked, c.declined = 1, False  # 답은 있으나 반영 안 됨
    pol = LegacyAdapterPolicy(rephrase=True)
    legacy = [a for a in candidates(st, pol.cfg)[0] if a.info_key == "ecog"]
    assert [(a.action_type, a.reason_code) for a in legacy] == [("REPARSE", "answered_not_reflected")]
    ops = {PA: OpsState(attempts=MappingProxyType({rephrase_key("patient_interview", "ecog"): REPHRASE_PENDING}))}
    co = CohortState.from_decision_states({PA: st}, ops=ops)
    asked = next(a for a in pol.propose(co) if a.info_key == "ecog")
    assert (asked.action_type, asked.reason_code, asked.source_id) == ("ASK_PATIENT", C.REPHRASE, "patient_interview")


@pytest.mark.parametrize("key", ["lab:absolute neutrophil count", "lab:total bilirubin", "lab:creatinine clearance"])
def test_info_key_with_spaces_is_kept(key):
    """NSCLC 검사명에는 공백이 있음 (서연 리뷰). info_key가 빠지면 항목 키가 지문으로 바뀌어 시험 사이 공유·소진이 끊김."""
    from ctrec.engine.policy import _INFO_KEY
    assert _INFO_KEY.match(key)
    a = C.Action(action_id="PA-0123456789abcdef:x", action_type="REQUEST_DOCUMENT",
                 patient_alias="PA-0123456789abcdef", source_id="lab_report", reason_code="x", fingerprint="x", tier=3, policy_version="t",
                 info_key=key)
    assert a.info_key == key
    for bad in (" " + key, key + " ", "lab:a\nb"):
        assert not _INFO_KEY.match(bad)
