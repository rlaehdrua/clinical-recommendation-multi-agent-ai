"""P0-C: 국가 한정 구조화 기준의 한쪽 보류 (옵션 B′, NCT06216301 I1 '≥22 years of age in the USA').

파싱 IR(Rule)에는 국가 범위 필드가 없고 환자 프로필에도 참여 국가가 없습니다. 규칙 엔진은 기준 원문(rule.text)의 국가 한정
표현을 결정론적으로 알아보고(rule_engine.country_scope), 그 기준이
- 통과이면 그대로 둡니다 (그 나라 환자이면 충족, 아니면 '해당 없음 = 통과').
- 불충족이면 holds unknown으로 보류합니다 (None이 아니므로 LLM이 나라를 짐작하지 않음).
국가 한정이 아닌 기준은 판정이 바뀌지 않습니다.

알려진 비용 (팀 결정 CL-C1, 이 테스트는 현재 동작을 기록할 뿐 정하지 않음): 17세처럼 두 갈래(I1 ≥22, I2 ≥18) 모두
불충족인 환자도 I1·I2가 모두 보류되어 이 시험에서 부적격으로 확정되지 않습니다.

세 경로를 확인합니다: CLI·웹 Session, followup 서비스, WP6 runner. LLM은 오프라인 대역만 씁니다.
국가 문장은 서술에만 있습니다 (시스템에 국가 필드가 없음).
"""

from __future__ import annotations

import json

import pytest

from ctrec import config, question_eval as qe, trial_store
from ctrec.engine import contracts as C
from ctrec.schemas import PatientProfile, Rule
from ctrec.tools import ctgov, rule_engine
from ctrec.tools.rule_engine import country_scope

NT, NP = config.ROOT / "data/nsclc/trials", config.ROOT / "data/nsclc/parsed"
TID, CTRL = "NCT06216301", "NCT06928389"  # 대조: 국가 조건 없는 '≥18' 나이 기준
REF = "2026-10-12"
FLAG = "country_scope_unknown"
COUNTRY = {"unknown": "", "USA": " She lives in the United States and will enroll at a US site.",
           "nonUSA": " She lives in South Korea and will enroll at a site in Korea."}
STORES = (config.ROOT / "data/parsed", NP)


def _text(age: int, ck: str = "nonUSA") -> str:
    return f"Synthetic NSCLC patient. {age}-year-old female with stage IV non-small cell lung cancer.{COUNTRY[ck]}"


def _store_rules():
    for st in STORES:
        for f in sorted(st.glob("*.json")):
            if f.name == "FROZEN.json":
                continue
            p = json.loads(f.read_text(encoding="utf-8"))["parsed"]
            for r in p["rules"]:
                yield p["trial_id"], Rule.model_validate(r)


def _trial(tid):
    t = ctgov.load_local(NT / f"{tid}.json")
    return t, trial_store.load(t, store_dir=NP)


def _rules(tid):
    return {r.rule_id: r for r in _trial(tid)[1].rules}


def _patient(age, **kw) -> PatientProfile:
    base = dict(patient_id="P-C", age=age, sex="female", primary_diagnosis="NSCLC", disease_stage="IV", diagnoses=[],
                biomarkers=[], labs=[], medications=[], treatment_history=[], ecog=None, other_facts=[],
                missing_or_ambiguous=[])
    return PatientProfile(**{**base, **kw})


def _rule(**kw) -> Rule:
    return Rule(**{"structured_evaluable": True, "time_window_days": None, "underspecified": False, "cohort": None, **kw})


# ------------------------------------------------------------------ 탐지
@pytest.mark.parametrize("text, hit", [
    ("≥22 years of age in the USA", "in the USA"),
    ("≥18 years of age outside of the USA.", "outside of the USA"),
    ("Enrolled at US sites only", "US sites"),
    ("Age >= 20 outside the US", "outside the US"),
    ("Age >= 20 years in Japan", "in Japan"),
    ("Aged 19 or older in the United States", "in the United States"),
    ("non-US participants: age >= 18", "non-US"),
    ("Patients in the U.S. only", "in the U.S."),
    ("within the EU", "within the EU"),
])
def test_country_scope_detects_conservative_phrases(text, hit):
    assert country_scope(text) == hit


@pytest.mark.parametrize("text", [
    "Age ≥ 18 years old at the time of randomization.", "Age >= 18", "Let us know", "in us",
    "European/Japanese Respiratory Society criteria", "Lesion in the macular region", "ECOG 0-1",
    "Hemoglobin >= 9 g/dL", "Prior therapy in Korean patients", None, "",
])
def test_country_scope_ignores_non_country_text(text):
    assert country_scope(text) is None


def test_detector_fires_only_on_nct06216301_in_product_stores():
    """제품 저장소 2곳(data/parsed, data/nsclc/parsed)에서 국가 한정으로 잡히는 기준은 NCT06216301 I1·I2뿐.
    새 시험이 들어와 바뀌면 의도적으로 갱신하십시오 (코호트 변화)."""
    hits = {f"{tid}:{r.rule_id}" for tid, r in _store_rules() if country_scope(r.text)}
    assert hits == {f"{TID}:I1", f"{TID}:I2"}


# ------------------------------------------------------------------ 규칙 엔진 (한쪽 보류)
@pytest.mark.parametrize("age", [16, 17, 18, 19, 21, 22, 30])
@pytest.mark.parametrize("rid, bound", [("I1", 22), ("I2", 18)])
def test_country_scoped_age_rule_keeps_pass_and_holds_fail(age, rid, bound):
    rule = _rules(TID)[rid]
    a = rule_engine.evaluate(rule, _patient(age), REF)
    assert a is not None and a.method == "rule_engine"  # None이면 LLM이 나라를 짐작함
    if age >= bound:
        assert (a.holds, a.passes, a.confidence) == ("yes", True, "high") and FLAG not in a.validation_flags
    else:
        assert (a.holds, a.passes, a.confidence) == ("unknown", None, "low")
        assert FLAG in a.validation_flags and "국가" in (a.missing_info or "")


def test_passing_patient_is_not_held_at_all():
    """통과하는 환자는 보류되지 않음: 결과가 4f81460 판정(_evaluate_structured)과 완전히 같음."""
    for rid in ("I1", "I2"):
        rule = _rules(TID)[rid]
        for age in (22, 30, 45, 81):
            assert rule_engine.evaluate(rule, _patient(age), REF) == rule_engine._evaluate_structured(rule, _patient(age), REF)


def test_non_scoped_rules_are_unchanged_in_every_product_store():
    """국가 한정이 아닌 구조화 기준(나이·성별·ECOG·검사)은 수정 전 판정과 같음."""
    n = 0
    for _, rule in _store_rules():
        if not rule.structured_evaluable or country_scope(rule.text):
            continue
        for age in (0, 1, 16, 17, 18, 19, 21, 22, 30, 75, 76, 90):
            for p in (_patient(age, ecog=1), _patient(age, ecog=3)):
                assert rule_engine.evaluate(rule, p, REF) == rule_engine._evaluate_structured(rule, p, REF)
                n += 1
    assert n > 1000


@pytest.mark.parametrize("holds, want", [("yes", (("unknown", None))), ("no", ("no", True))])
def test_country_scoped_exclusion_holds_only_the_failing_side(holds, want):
    rule = _rule(rule_id="E1", kind="exclusion", text="Age >= 65 years in Japan", category="demographic",
                 field="age", operator=">=", value="65", unit="years")
    a = rule_engine.evaluate(rule, _patient(70 if holds == "yes" else 40), REF)
    assert (a.holds, a.passes) == want


def test_country_scoped_lab_rule_holds_only_the_failing_side():
    from ctrec.schemas import LabValue
    rule = _rule(rule_id="I9", kind="inclusion", text="Hemoglobin >= 10 g/dL for patients in Japan", category="lab",
                 field="lab:hemoglobin", operator=">=", value="10", unit="g/dL")
    lab = lambda v: [LabValue(name="hemoglobin", value=v, unit="g/dL", date=REF, source_quote=f"Hb {v} g/dL")]  # noqa: E731
    assert rule_engine.evaluate(rule, _patient(50, labs=lab(12.0)), REF).passes is True
    held = rule_engine.evaluate(rule, _patient(50, labs=lab(8.0)), REF)
    assert held.passes is None and FLAG in held.validation_flags


# ------------------------------------------------------------------ CLI·웹 Session 경로
@pytest.fixture()
def offline(monkeypatch):
    monkeypatch.setattr(config, "REFERENCE_DATE", REF)
    monkeypatch.delenv("CTREC_SESSION", raising=False)
    with qe.OfflineEnvironment(3):
        yield


def _session_match(age, ck):
    from ctrec.contract_session import session_class
    from ctrec.pipeline import PatientCase
    from ctrec.trace import Tracer
    trials = {t: _trial(t) for t in (TID, CTRL)}
    s = session_class()(case=PatientCase(patient_id=f"P{age}{ck}", raw_text=_text(age, ck), input_source="clinical_record"),
                        trials={t: v[0] for t, v in trials.items()}, answerer=None, tracer=Tracer(None, verbose=False))
    s.parsed.update({t: v[1] for t, v in trials.items()})
    s.profile_patient()
    s.match([TID, CTRL])
    return s.matches


@pytest.mark.parametrize("age, ck", [(18, "nonUSA"), (19, "nonUSA"), (19, "USA"), (19, "unknown"), (21, "nonUSA")])
def test_session_does_not_exclude_18_to_21_on_country_scoped_rule(offline, age, ck):
    m = _session_match(age, ck)[TID]
    a = {x.rule_id: x for x in m.assessments}
    assert m.eligibility != "INELIGIBLE" and m.match_status != "CRITERIA_MISMATCH"
    assert a["I1"].method == "rule_engine" and a["I1"].passes is None and FLAG in a["I1"].validation_flags
    assert a["I2"].passes is True and "I1" not in (m.summary or "").split("불충족:")[-1]


def test_session_22_still_passes_both_age_rules(offline):
    m = _session_match(22, "nonUSA")[TID]
    a = {x.rule_id: x for x in m.assessments}
    assert (a["I1"].passes, a["I2"].passes) == (True, True)
    assert not any(FLAG in x.validation_flags for x in m.assessments)


def test_session_17_is_held_not_excluded_known_cost_cl_c1(offline):
    """CL-C1 (팀 결정 대기): 두 갈래 모두 불충족인 17세도 보류. 대조 시험(국가 조건 없음)은 그대로 부적격."""
    ms = _session_match(17, "nonUSA")
    a = {x.rule_id: x for x in ms[TID].assessments}
    assert (a["I1"].passes, a["I2"].passes) == (None, None) and ms[TID].eligibility != "INELIGIBLE"
    assert ms[CTRL].eligibility == "INELIGIBLE"


# ------------------------------------------------------------------ followup 서비스 경로
@pytest.mark.parametrize("age", [18, 19, 21])
def test_followup_intake_and_age_answer_never_confirm_ineligible(offline, tmp_path, age):
    from ctrec.followup.service import FollowupService, TrialCatalog
    from ctrec.followup.store import CaseStore
    t, p = _trial(TID)
    svc = FollowupService(CaseStore(tmp_path / "fu"), TrialCatalog(name="nsclc-c", trials={TID: t}, parsed={TID: p}))
    d = svc.create_case(patient_text=_text(age), trial_ids=[TID], input_source="self_report",
                        patient_token_digest="0" * 64, synthetic=True)
    pair = d["current"]["pairs"][TID]
    assert pair["status"] != C.PairStatus.CONFIRMED_INELIGIBLE.value and pair["mismatch"] == "none"
    # 수정 전에는 I1을 겨냥한 나이 문서 요청(REQUEST_DOCUMENT age)이 나왔고, 검증하면 CONFIRMED_INELIGIBLE이 됐음
    assert not [x for x in d["current"]["proposals"] if x.get("info_key") == "age" and x["action_type"] == "REQUEST_DOCUMENT"]
    ask = [x for x in d["current"]["proposals"] if x.get("info_key") == "age" and x["action_type"] == "ASK_PATIENT"]
    if ask:  # 나이를 다시 물어도(info_key가 age인 한계, 후속 과제) 답이 확정 부적격을 만들지 않음
        res = svc.create_requests(d["case_id"], [ask[0]["proposal_id"]], staff_id="coord-1")
        r = svc.respond(d["case_id"], (res["created"] or res["existing"])[0], answer=f"I am {age} years old.", unknown=False)
        assert r.get("case", r)["current"]["pairs"][TID]["status"] != C.PairStatus.CONFIRMED_INELIGIBLE.value


# ------------------------------------------------------------------ WP6 runner 경로
@pytest.mark.parametrize("age", [18, 19, 21])
def test_wp6_single_patient_18_to_21_never_confirmed_ineligible(age):
    from ctrec.sim import runner
    from ctrec.sim.cohort import SimFact, make_cohort
    from ctrec.sim.from_generator import load_trials
    st = load_trials([TID], NP, NT)
    pub = [SimFact(field="age", value=age, unit="years"), SimFact(field="sex", value="female")]
    co = make_cohort("c-country", REF, st, [("K", pub, pub + [SimFact(field="ecog", value=0)])], "c-salt")
    r = runner.run(runner.build_spec("c", "full_dcvc", co, C.EffortBudget(
        coordinator_minutes=1000, clinician_minutes=600, horizon_steps=40), 1), co)
    assert r.final.pair(co.aliases()["K"], TID).status != C.PairStatus.CONFIRMED_INELIGIBLE
