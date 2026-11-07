"""임상의 판단 기준의 안전 경계 (ctrec.expert_boundary). API 호출 없음, 값은 모두 합성.

입력 구분
- A 임상의 판단 근거 없음 + LLM 통과 / B 같은 조건 + LLM 실패 / D 문서 사실은 있지만 임상의 판단은 없음.
- 임의 reviewed 슬롯 연결(C)은 시뮬레이터 판정기(PR #20) 경로이므로 이 파일에 없습니다.
- 임상의 판단 완료(E)는 만드는 코드가 없어(decided_by="clinician"은 타입뿐) 시험하지 않습니다. 가짜 완료 상태를 만들지 않습니다.

matcher_pending 표시: 기본 Session은 보호 파일 matcher.py가 판정을 만듭니다. 승인 대기 패치
(docs/proposals/expert_boundary_matcher.patch)를 적용하기 전에는 **우회가 그대로 있다는 재현 테스트**(strict xfail)이고,
적용하면 표시가 풀려 일반 회귀 테스트가 됩니다. 이 표시가 있는 테스트의 통과는 안전성 해결의 근거가 아닙니다.
"""

import csv
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from document_support import attest_declared  # noqa: E402

from ctrec import config, expert_boundary as xb, pipeline, status  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.decision_state import SessionView, build_state  # noqa: E402
from ctrec.engine import contract_bridge  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.schemas import (CriterionAssessment, LLMAssessment, MatcherOutput, ParsedTrial, PatientProfile,  # noqa: E402
                           RecommendationDraft, RuleMeta)

from test_decisions import TRIAL  # noqa: E402
from test_offline import _rule  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PID = "PA-0123456789abcdef"
AGE = _rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18", unit="years")
IN_MATCHER = "expert_boundary" in Path(matcher.__file__).read_text(encoding="utf-8")
matcher_pending = pytest.mark.xfail(not IN_MATCHER, strict=True, raises=AssertionError,
                                    reason="기본 Session 경로는 matcher.py 패치 승인 전까지 우회가 남아 있음")


def _stored(trial_id: str, rule_id: str):
    raw = json.loads((ROOT / "data/nsclc/parsed" / f"{trial_id}.json").read_text(encoding="utf-8"))
    rule = next(r for r in ParsedTrial(**raw["parsed"]).rules if r.rule_id == rule_id)
    return rule, {k: RuleMeta(**v) for k, v in (raw.get("rule_meta") or {}).items() if k == rule_id}


class _Tracer:
    def log(self, *a, **k):
        pass


def _profile(age):
    return PatientProfile(patient_id=PID, age=age, age_value=age, age_unit="years", sex="male", primary_diagnosis=None,
                          disease_stage=None, diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[],
                          ecog=None, other_facts=[], missing_or_ambiguous=[])


def _run(monkeypatch, trial_id, rules, meta, verdicts, text, age=62, cls=Session, documents=False, **kw):
    """실제 Session 한 번 (LLM 자리만 대역). 반환: 쌍의 층별 결과."""
    parsed = ParsedTrial(trial_id=trial_id, title="t", target_population="", intervention_summary="", key_conditions=[],
                         cohorts=[], rules=[r.model_copy() for r in rules], parsing_notes="")
    parsed.rule_meta = dict(meta or {})

    def llm(model, *, system=None, user=None, **k):
        if model is RecommendationDraft:
            return RecommendationDraft(patient_id=PID, ranked=[], excluded=[], overall_comment="")
        ask = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
        return MatcherOutput(assessments=[
            LLMAssessment(rule_id=r["rule_id"], applicable=verdicts[r["rule_id"]][2] if len(verdicts[r["rule_id"]]) > 2 else True,
                          holds=verdicts[r["rule_id"]][0], evidence_type="explicit", confidence="high",
                          evidence=verdicts[r["rule_id"]][1], reasoning="stub", missing_info="") for r in ask])

    monkeypatch.setattr(config, "WRITE_REPORT", False)
    monkeypatch.setattr(matcher.llm, "structured", llm)
    monkeypatch.setattr(pipeline.trial_store, "get", lambda t: parsed)
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", lambda pid, raw, reference_date=None: _profile(age))
    s = cls(case=PatientCase(PID, text, input_source="clinical_record"), trials={trial_id: {**TRIAL, "trial_id": trial_id}},
            answerer=None, tracer=_Tracer(), **kw)
    s.profile_patient()
    if documents:
        attest_declared(s.fact_store, PID)
    s.match()
    v = SessionView.from_session(s)
    v.matched_versions = {t: v.snapshot.version for t in s.matches}
    co = CohortState.from_decision_states({PID: build_state(v)})
    m, pair = s.matches[trial_id], co.pair(PID, trial_id)
    return {"match": m, "parsed": parsed, "external": status.external_status(m), "pair": pair, "co": co, "session": s}


def _assert_expert_review(x, rule_id):
    assert x["pair"].status.value == "EXPERT_REVIEW"
    assert x["match"].eligibility == "UNCERTAIN" and x["external"] == "NEEDS_REVIEW"
    c = next(c for c in x["pair"].criteria if c.rule_id == rule_id)
    assert c.state == "UNKNOWN" and c.adjudication == "expert" and c.gap == "physician_review"
    assert xb.violations(x["match"], x["parsed"]) == []


# 플래그 있는 기준 2개, 플래그 없이 슬롯 후보표로 분류된 기준 2개 (judge.life_expectancy, cm.cardiac)
STORED = [("NCT07680790", "E3", "심장 질환 없음", "심장 질환 있음"), ("NCT07098338", "I4", "장기 기능 양호", "장기 기능 불량"),
          ("NCT07098338", "I5", "기대 여명 12주 이상", "기대 여명 4주"), ("NCT07098338", "E10", "심장 질환 없음", "심장 질환 있음")]


def _verdict(rule, good, bad, *, ok):
    passing = "no" if rule.kind == "exclusion" else "yes"
    failing = "yes" if rule.kind == "exclusion" else "no"
    return (passing, good) if ok else (failing, bad)


@pytest.mark.parametrize("tid, rid, good, bad", STORED)
@pytest.mark.parametrize("case", ["A", "B", "D"])
@matcher_pending
def test_default_session_keeps_clinician_rule_open(monkeypatch, tid, rid, good, bad, case):
    rule, meta = _stored(tid, rid)
    v = _verdict(rule, good, bad, ok=case != "B")
    text = {"A": f"62세 남성. {good}.", "B": f"62세 남성. {bad}.", "D": f"[퇴원요약 2026-09-01] 62세 남성. {good}."}[case]
    _assert_expert_review(_run(monkeypatch, tid, [AGE, rule], meta, {rid: v}, text), rid)


@matcher_pending
def test_default_session_not_applicable_ruling_is_held(monkeypatch):
    """'해당 없음'도 판정: LLM의 N/A로 임상의 판단 기준을 통과시키지 않음 (정책 승인 항목)."""
    rule, meta = _stored("NCT07680790", "E3")
    x = _run(monkeypatch, "NCT07680790", [AGE, rule], meta, {"E3": ("unknown", "심장 질환 없음", False)}, "62세 남성. 심장 질환 없음.")
    _assert_expert_review(x, "E3")


@pytest.mark.parametrize("tid, rid, good, bad", STORED)
@pytest.mark.parametrize("case", ["A", "B", "D"])
def test_contract_session_keeps_clinician_rule_open(monkeypatch, tid, rid, good, bad, case):
    """비보호 경로의 실제 해결: CTREC_SESSION=contract에서는 matcher 패치 없이도 막힘."""
    rule, meta = _stored(tid, rid)
    v = _verdict(rule, good, bad, ok=case != "B")
    text = {"A": f"62세 남성. {good}.", "B": f"62세 남성. {bad}.", "D": f"[퇴원요약 2026-09-01] 62세 남성. {good}."}[case]
    _assert_expert_review(_run(monkeypatch, tid, [AGE, rule], meta, {rid: v}, text, cls=ContractSession), rid)


def test_clinician_review_action_is_proposed_after_the_hold(monkeypatch):
    rule, meta = _stored("NCT07098338", "I5")
    x = _run(monkeypatch, "NCT07098338", [AGE, rule], meta, {"I5": ("yes", "기대 여명 12주 이상")}, "62세 남성. 기대 여명 12주 이상.",
             cls=ContractSession)
    acts = {(a.action_type, a.reason_code) for a in LegacyAdapterPolicy().propose(x["co"])}
    assert ("CLINICIAN_REVIEW", "criterion_underspecified") in acts


@pytest.mark.parametrize("cls", [Session, ContractSession])
def test_independent_ineligible_stays_confirmed_in_decision(monkeypatch, cls):
    """대조군: 나이 불충족은 임상의 판단과 무관 -> 임상의 판단 기준이 남아도 부적격."""
    rule, meta = _stored("NCT07098338", "I5")
    x = _run(monkeypatch, "NCT07098338", [AGE, rule], meta, {"I5": ("yes", "기대 여명 12주 이상")}, "10세 남성. 기대 여명 12주 이상.",
             age=10, cls=cls)
    assert x["match"].eligibility == "INELIGIBLE" and x["external"] == "CRITERIA_MISMATCH"


@pytest.mark.parametrize("cls", [Session, ContractSession])
@pytest.mark.parametrize("age, eligibility", [(62, "ELIGIBLE"), (10, "INELIGIBLE")])
def test_ordinary_rules_keep_their_decisions(monkeypatch, cls, age, eligibility):
    """대조군: 수치 규칙과 일반 LLM 기준(임상 판단 아님)의 판정은 그대로."""
    dx = _rule(rule_id="I2", text="Histologically confirmed NSCLC", category="diagnosis", structured_evaluable=False)
    x = _run(monkeypatch, "T1", [AGE, dx], {"I2": RuleMeta(required_evidence="self_report_ok")}, {"I2": ("yes", "조직검사로 확인된 NSCLC")},
             f"{age}세 남성. 조직검사로 확인된 NSCLC.", age=age, cls=cls)  # 문서 게이트가 아닌 임상의 경계만 보는 대조
    assert x["match"].eligibility == eligibility
    assert all(xb.FLAG not in a.validation_flags for a in x["match"].assessments)
    assert next(a for a in x["match"].assessments if a.rule_id == "I2").passes is True


# ---------------------------------------------------------------------------
# 경계 함수 (순수)
# ---------------------------------------------------------------------------

def _assessment(rule, **kw):
    base = dict(rule_id=rule.rule_id, kind=rule.kind, criterion_text=rule.text, holds="yes", passes=True, confidence="high",
                evidence="q", reasoning="r", missing_info=None, method="llm", category=rule.category,
                underspecified=rule.underspecified, cohort=rule.cohort)
    return CriterionAssessment(**{**base, **kw})


VAGUE = _rule(rule_id="I9", text="Adequate organ function", category="comorbidity", structured_evaluable=False, underspecified=True)
PLAIN = _rule(rule_id="I2", text="NSCLC", category="diagnosis", structured_evaluable=False)


@pytest.mark.parametrize("method", ["llm", "rule_engine", "contract"])
@pytest.mark.parametrize("holds, passes", [("yes", True), ("no", False)])
def test_enforce_holds_every_decider(method, holds, passes):
    a = xb.enforce("T1", VAGUE, _assessment(VAGUE, method=method, holds=holds, passes=passes,
                                            llm_judgment={"holds": holds}))
    assert (a.holds, a.passes, a.underspecified) == ("unknown", None, True) and matcher.needs_physician_review(a)
    assert xb.FLAG in a.validation_flags and a.llm_judgment == {"holds": holds}  # 판정기 값은 감사용으로 남음


def test_enforce_holds_not_applicable_and_unverified():
    na = xb.enforce("T1", VAGUE, _assessment(VAGUE, holds="unknown", passes=True, applicable=False))
    assert na.passes is None and na.applicable is True
    un = xb.enforce("T1", VAGUE, _assessment(VAGUE, holds="unknown", passes=None, needs_verification=True, self_reported_holds="yes"))
    assert not un.needs_verification and un.self_reported_holds is None and matcher.needs_physician_review(un)


def test_enforce_is_identity_for_ordinary_rules_and_idempotent():
    a = _assessment(PLAIN)
    assert xb.enforce("T1", PLAIN, a) is a
    once = xb.enforce("T1", VAGUE, _assessment(VAGUE))
    assert xb.enforce("T1", VAGUE, once) is once


def test_registered_rule_needs_its_own_trial():
    rule = _rule(rule_id="I5", text="Life expectancy >= 12 weeks", category="other", structured_evaluable=False)
    assert xb.clinician_reason("NCT07098338", rule) == "registered" and xb.clinician_reason("T1", rule) is None


def test_decided_rule_engine_rule_keeps_numeric_path():
    """규칙 엔진 수치 기준은 분류 대상이 아니면 그대로 (단순 수치 기준과 임상 판단 기준 분리)."""
    hb = _rule(rule_id="I3", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin", operator=">=", value="9")
    a = _assessment(hb, method="rule_engine")
    assert xb.enforce("NCT07098338", hb, a) is a


def test_violations_finds_decided_clinician_rule():
    p = ParsedTrial(trial_id="T1", title="t", target_population="", intervention_summary="", key_conditions=[], cohorts=[],
                    rules=[VAGUE, PLAIN], parsing_notes="")
    from ctrec.schemas import TrialMatch
    m = TrialMatch(patient_id=PID, trial_id="T1", eligibility="ELIGIBLE", assessments=[_assessment(VAGUE), _assessment(PLAIN)],
                   n_pass=2, n_fail=0, n_unknown=0, summary="s")
    assert xb.violations(m, p) == ["I9"]
    held = m.model_copy(update={"assessments": xb.enforce_all(p, m.assessments)})
    assert xb.violations(held, p) == []


def test_bridge_classifies_registered_rule_as_expert():
    rule = _rule(rule_id="I5", text="Life expectancy >= 12 weeks", category="other", structured_evaluable=False)
    assert contract_bridge.provisional_adjudication(rule, "NCT07098338") == "expert"
    assert contract_bridge.provisional_adjudication(rule, "T1") == "fact_llm"
    assert contract_bridge.provisional_adjudication(VAGUE) == "expert"


# ---------------------------------------------------------------------------
# 분류 범위: 슬롯 후보표와 대조 (파싱되지 않은 시험은 막을 규칙 자체가 없음)
# ---------------------------------------------------------------------------

def _norm(s):
    return re.sub(r"\W+", " ", s or "").strip().lower()


def _stored_rules():
    out = {}
    for f in sorted((ROOT / "data/nsclc/parsed").glob("*.json")):
        if f.name != "FROZEN.json":
            out[f.stem] = ParsedTrial(**json.loads(f.read_text(encoding="utf-8"))["parsed"]).rules
    return out


def test_every_stored_expert_candidate_is_classified():
    stored = _stored_rules()
    rows = [r for r in csv.DictReader(open(ROOT / "data/nsclc/cohort/slot_candidates.csv", encoding="utf-8-sig"))
            if r["adjudication_candidate"] == "expert"]
    missing, not_stored = [], set()
    for r in rows:
        q = _norm(r["quote"])
        hits = [x for x in stored.get(r["trial_id"], []) if q[:40] in _norm(x.text) or _norm(x.text)[:40] in q]
        if not hits:
            not_stored.add((r["trial_id"], r["slot_id"]))
        missing += [f"{r['trial_id']}:{x.rule_id}" for x in hits if not xb.clinician_reason(r["trial_id"], x)]
    assert missing == []
    # 파싱 저장본이 없는 2개 시험의 후보는 분류 밖 (재파싱 뒤 다시 대조)
    assert not_stored == {("NCT05255302", "tx.recent_radiotherapy"), ("NCT05255302", "other.tumor_tissue"),
                          ("NCT05255302", "judge.life_expectancy"), ("NCT05255302", "judge.investigator_unsuitable"),
                          ("NCT04892953", "judge.life_expectancy"), ("NCT04892953", "judge.investigator_unsuitable")}


def test_registered_rules_still_point_at_the_same_text():
    """기준 ID가 재파싱으로 밀리면 목록이 엉뚱한 기준을 가리키고 진짜 기준이 빠짐 -> 저장 규칙 원문으로 고정."""
    stored = {f"{t}:{r.rule_id}": r for t, rules in _stored_rules().items() for r in rules}
    words = {"judge.life_expectancy": "life expectancy", "cm.cardiac": "cardiac", "dx.disease_setting": "metastatic"}
    for key, slot in xb.EXPERT_RULES.items():
        assert words[slot] in stored[key].text.lower(), key
        assert not stored[key].underspecified, key  # 플래그가 있으면 목록에 둘 필요 없음


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="분류 신호 밖: 임상 판단 표현이 있어도 플래그·목록에 없으면 경계가 동작하지 않음")
def test_known_gap_unclassified_clinical_wording():
    """남은 우회의 표시. 'clinically significant' 기준이 임상 판단인지는 임상 검수가 정합니다 (휴리스틱으로 막지 않음)."""
    rule = next(r for r in _stored_rules()["NCT07027514"] if r.rule_id == "E14")
    assert "clinically significant" in rule.text.lower()
    assert xb.clinician_reason("NCT07027514", rule)
