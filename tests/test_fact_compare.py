"""사실 기반 비교 Matcher와 기준 단위 차이 (Phase 1-3B/1-4, 체크포인트 3). 합성 데이터, API 호출 없음."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config, store_patch, trial_store
from ctrec.agents import matcher
from ctrec.fact_compare import compare_session, compare_trial, evaluate_trial
from ctrec.fact_store import FactStore
from ctrec.fact_view import FactEvidenceView
from ctrec.schemas import LLMAssessment, MatcherOutput, RuleMeta

from test_decisions import TRIAL, _parsed
from test_offline import _profile, _rule

P = "T"  # test_offline._profile의 patient_id
REF = "2026-10-12"
REC = "clinical_record"

ECOG = _rule(rule_id="I1", text="ECOG 0-1", category="performance_status", field="ecog", operator="<=", value="1")
AGE = _rule(rule_id="I2", text="Age >= 18", field="age", operator=">=", value="18", unit="years")
HB = _rule(rule_id="I3", text="Hb >= 9 g/dL", category="lab", field="lab:hemoglobin", operator=">=", value="9",
           unit="g/dL")
DX = _rule(rule_id="I4", text="NSCLC diagnosis", category="diagnosis", structured_evaluable=False)


SRC = "환자 기록 요약: NSCLC 진단 확인됨"


def _base(profile, rules, monkeypatch=None, llm=None):
    if monkeypatch is not None:
        monkeypatch.setattr(matcher.llm, "structured", llm or _unknown_llm)
    return matcher.match_trial(profile, _parsed(rules), TRIAL, reference_date=REF, source_text=SRC,
                               input_source=REC)


def _unknown_llm(model, system, user, **k):
    """기준선 LLM 대역: 요청받은 기준을 모두 판정 불가로."""
    rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
    return MatcherOutput(assessments=[LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="unknown",
                                                    evidence_type="absent", confidence="low", evidence="근거 없음",
                                                    reasoning="r", missing_info="m") for r in rules])


def _no_llm(*a, **k):
    raise AssertionError("LLM이 호출되면 안 됨")


def _store(*facts) -> FactStore:
    s = FactStore()
    for field, value, kw in facts:
        s.add(P, field, value, **{"source_type": REC, **kw})
    return s


def _compare(store, base, rules, mode="deterministic", **kw):
    view = FactEvidenceView(store, P)
    parsed = _parsed(rules)
    fact, reasons, fm = evaluate_trial(view, parsed, TRIAL, REF, mode, **kw)
    return compare_trial(base, parsed, fact, reasons, fm)


def _by_rule(result):
    return {c["rule_id"]: c for c in result["criteria"]}


# ------------------------------------------------------------ 동일 / not_compared
def test_same_results_when_facts_agree_and_llm_rules_not_compared(monkeypatch):
    base = _base(_profile(age=62, ecog=1), [ECOG, AGE], monkeypatch)
    s = _store(("age", 62, {"unit": "years"}), ("ecog", 1, {}))
    r = _compare(s, base, [ECOG, AGE])
    assert [(c["rule_id"], c["change"], c["cause"]) for c in r["criteria"]] == [("I1", "same", None), ("I2", "same", None)]
    assert "fact" not in r  # 결정적 모드는 시험 단위 판정을 만들지 않음


def test_llm_only_rule_is_not_compared_in_default_mode(monkeypatch):
    def llm(*a, **k):
        return MatcherOutput(assessments=[LLMAssessment(rule_id="I4", applicable=True, holds="yes",
                                                        evidence_type="explicit", confidence="high", evidence=SRC,
                                                        reasoning="r", missing_info=None)])
    base = _base(_profile(), [DX], monkeypatch, llm)
    monkeypatch.setattr(matcher.llm, "structured", _no_llm)  # 비교 경로는 LLM을 부르지 않음
    r = _compare(_store(("diagnosis", "NSCLC", {})), base, [DX])
    assert _by_rule(r)["I4"] == {"rule_id": "I4", "cohort": None, "baseline": "MET", "fact": None,
                                 "change": "not_compared", "cause": None, "risk": False}


# ------------------------------------------------------------ 출처·검증·충돌 때문에 달라짐
def test_met_to_unknown_due_to_self_report(monkeypatch):
    base = _base(_profile(ecog=1), [ECOG], monkeypatch)
    r = _compare(_store(("ecog", 1, {"source_type": "self_report"})), base, [ECOG])
    assert _by_rule(r)["I1"]["change"] == "met_to_unknown" and _by_rule(r)["I1"]["cause"] == "self_report_only"


def test_declared_record_strict_mode(monkeypatch):
    base = _base(_profile(ecog=1), [ECOG], monkeypatch)
    s = _store(("ecog", 1, {}))
    assert _by_rule(_compare(s, base, [ECOG]))["I1"]["change"] == "same"
    strict = _by_rule(_compare(s, base, [ECOG], require_document_verification=True))["I1"]
    assert (strict["change"], strict["cause"]) == ("met_to_unknown", "declared_unverified")


def test_conflict_changes_met_to_unknown(monkeypatch):
    base = _base(_profile(ecog=1), [ECOG], monkeypatch)  # 기준선은 최신 프로필 값(1) 하나만 봄
    r = _compare(_store(("ecog", 1, {}), ("ecog", 2, {})), base, [ECOG])
    c = _by_rule(r)["I1"]
    assert (c["change"], c["cause"], c["risk"]) == ("met_to_unknown", "conflict", False)
    view = FactEvidenceView(_store(("ecog", 1, {}), ("ecog", 2, {})), P)
    assert view.to_profile().ecog is None  # 값을 고르지 않아 규칙 엔진이 판정하지 않음


def test_real_mismatch_kept_distinct_from_missing_info(monkeypatch):
    base = _base(_profile(ecog=3), [ECOG], monkeypatch)
    r = _compare(_store(("ecog", 3, {})), base, [ECOG])
    assert _by_rule(r)["I1"]["baseline"] == _by_rule(r)["I1"]["fact"] == "NOT_MET"  # 실제 불일치는 그대로
    r = _compare(_store(("ecog", 3, {"source_type": "self_report"})), base, [ECOG])
    assert _by_rule(r)["I1"]["fact"] == "UNKNOWN"  # 근거 부족은 불일치가 아니라 판정 불가


# ------------------------------------------------------------ 더 긍정적인 방향은 위험으로 분리
def test_unknown_to_met_is_flagged_as_risk(monkeypatch):
    base = _base(_profile(labs=[]), [HB], monkeypatch)  # 기준선 프로필에 혈색소 없음 -> 판정 불가
    s = _store(("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-10-01"}))
    c = _by_rule(_compare(s, base, [HB]))["I3"]
    assert (c["change"], c["cause"], c["risk"]) == ("unknown_to_met", "input_difference", True)


def test_not_met_to_met_is_flagged_as_risk(monkeypatch):
    base = _base(_profile(age=16), [AGE], monkeypatch)
    c = _by_rule(_compare(_store(("age", 20, {"unit": "years"})), base, [AGE]))["I2"]
    assert c["change"] == "not_met_to_met" and c["risk"]


def test_session_level_summary_and_risks(monkeypatch):
    base = _base(_profile(labs=[], ecog=1), [ECOG, HB], monkeypatch)
    s = _store(("ecog", 1, {"source_type": "self_report"}),
               ("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-10-01"}))
    out = compare_session(s, P, {"T1": base}, {"T1": _parsed([ECOG, HB])}, {"T1": TRIAL}, REF)
    assert out["summary"]["changes"] == {"met_to_unknown": 1, "unknown_to_met": 1}
    assert out["summary"]["causes"] == {"self_report_only": 1, "input_difference": 1}
    assert out["risks"] == [{"trial_id": "T1", "rule_id": "I3", "change": "unknown_to_met", "cause": "input_difference"}]
    text = json.dumps(out, ensure_ascii=False)
    for value in ("11.0", "NSCLC", "hemoglobin\": 11"):  # 환자 값·원문 없음
        assert value not in text


# ------------------------------------------------------------ 평가 불가, 코호트, 시험 단위 (LLM 모드 대역)
def test_not_evaluated_baseline_stays_not_evaluated(monkeypatch):
    from ctrec.pipeline import _not_evaluated
    base = _not_evaluated(P, "NCT99999999", "COMPILATION_PENDING: 테스트")
    out = compare_session(_store(("ecog", 1, {})), P, {"NCT99999999": base}, {}, {"NCT99999999": {}}, REF)
    t = out["trials"][0]
    assert (t["status"], t["cause"], t["criteria"]) == ("not_evaluated", "not_evaluated", [])
    assert out["summary"]["not_evaluated_trials"] == 1


def _llm_yes(evidence_by_rule):
    def llm(model, system, user, **k):
        rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
        return MatcherOutput(assessments=[LLMAssessment(
            rule_id=r["rule_id"], applicable=True, holds="yes" if r["kind"] == "inclusion" else "no",
            evidence_type="explicit", confidence="high", evidence=evidence_by_rule.get(r["rule_id"], SRC),
            reasoning="r", missing_info=None) for r in rules])
    return llm


def test_llm_mode_trial_level_and_cohorts(monkeypatch):
    """LLM 모드(대역): 코호트·AND 조건을 기존 결정 규칙 그대로 적용하고 시험 단위 변화를 비교."""
    a_rule = _rule(rule_id="I5", text="Cohort A: NSCLC", category="diagnosis", structured_evaluable=False, cohort="A")
    b_rule = _rule(rule_id="I6", text="Cohort B: ECOG 0-1", category="performance_status", field="ecog",
                   operator="<=", value="1", cohort="B")
    rules = [AGE, a_rule, b_rule]
    parsed = _parsed(rules, cohorts=["A", "B"])
    monkeypatch.setattr(matcher.llm, "structured", _llm_yes({"I5": SRC}))
    base = matcher.match_trial(_profile(age=62, ecog=1), parsed, TRIAL, reference_date=REF, source_text=SRC,
                               input_source=REC)
    assert base.eligibility == "ELIGIBLE"
    s = _store(("age", 62, {"unit": "years"}), ("ecog", 1, {"source_type": "self_report"}),
               ("diagnosis", "NSCLC", {"source_reference": {"kind": "input_text", "quote": "NSCLC 진단"}}))
    monkeypatch.setattr(matcher.llm, "structured", _llm_yes({"I5": "NSCLC 진단"}))
    fact, reasons, fm = evaluate_trial(FactEvidenceView(s, P), parsed, TRIAL, REF, "llm")
    r = compare_trial(base, parsed, fact, reasons, fm)
    assert fm.matched_cohort == "A" and fm.eligibility == "ELIGIBLE"  # 코호트 B의 ECOG는 자기 보고뿐 -> 코호트 A
    assert reasons == {"I6": "self_report_only"} and r["trial_change"] == "same" and not r["trial_risk"]
    # 공통 기준(AND)이 판정 불가가 되면 시험도 판정 보류
    s2 = _store(("age", 62, {"unit": "years", "source_type": "self_report"}), ("ecog", 1, {}),
                ("diagnosis", "NSCLC", {"source_reference": {"kind": "input_text", "quote": "NSCLC 진단"}}))
    fact2, reasons2, fm2 = evaluate_trial(FactEvidenceView(s2, P), parsed, TRIAL, REF, "llm")
    assert fm2.eligibility == "ELIGIBLE"  # 나이는 자기 보고로 충분한 기준 (미검증을 불일치로 보지 않음)


def test_llm_mode_cannot_promote_ungrounded_answer(monkeypatch):
    parsed = _parsed([DX])
    monkeypatch.setattr(matcher.llm, "structured", _llm_yes({"I4": "아마 NSCLC일 것으로 보이는 환자"}))
    fact, reasons, fm = evaluate_trial(FactEvidenceView(_store(("ecog", 1, {})), P), parsed, TRIAL, REF, "llm")
    assert reasons == {"I4": "ungrounded"} and fact["I4"].passes is None and fm.eligibility == "UNCERTAIN"


def test_trial_level_upgrade_is_risk(monkeypatch):
    parsed = _parsed([HB])
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=[LLMAssessment(
        rule_id="I3", applicable=True, holds="unknown", evidence_type="absent", confidence="low", evidence="근거 없음",
        reasoning="r", missing_info="Hb")]))
    base = matcher.match_trial(_profile(labs=[]), parsed, TRIAL, reference_date=REF, source_text=SRC,
                               input_source=REC)
    assert base.eligibility == "UNCERTAIN"
    s = _store(("lab:hemoglobin", 11.0, {"unit": "g/dL", "observed_at": "2026-10-01"}))
    fact, reasons, fm = evaluate_trial(FactEvidenceView(s, P), parsed, TRIAL, REF, "llm")
    r = compare_trial(base, parsed, fact, reasons, fm)
    assert r["trial_change"] == "uncertain_to_eligible" and r["trial_risk"]


# ------------------------------------------------------------ 고정 기준표 그대로 사용, 미승인 패치 미적용
def test_frozen_trial_used_as_is_without_unapproved_patch(monkeypatch):
    trial = json.loads((config.TRIALS_DIR / "NCT06125405.json").read_text(encoding="utf-8"))
    parsed = trial_store.load(trial)
    assert parsed.rule_meta["E4"].review is None  # Phase 0-4 E4 검토 표시(미승인)는 적용되지 않음
    s = _store(("lab:platelets", 150, {"unit": "10^9/L", "observed_at": "2026-10-01"}))
    fact, reasons, _ = evaluate_trial(FactEvidenceView(s, P), parsed, trial, REF)
    assert fact["E4"] is None  # 비표준 단위 기준은 규칙 엔진이 판정하지 않음 (Phase 0-3) -> 비교하지 않음
    assert [r.rule_id for r in parsed.rules] == list(fact)  # 기준표의 rule ID 그대로
    assert store_patch.verify_frozen() == []
