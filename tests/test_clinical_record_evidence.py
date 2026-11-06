"""출처 선언과 문서 검증의 구분 (합성 데이터, LLM·API 호출 없음).

xfail(strict)은 알려진 위험입니다. 정책이 정해져 패치가 들어가면 XPASS로 실패하므로 그때 표시를 지웁니다.
통과 테스트는 어느 대안을 고르더라도 보존해야 하는 동작입니다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_decisions import TRIAL, _parsed  # noqa: E402
from test_offline import _rule  # noqa: E402

from ctrec import config, evidence_basis as eb, pipeline  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.decision_state import SessionView, build_state  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.contract_bridge import evidence_ref  # noqa: E402
from ctrec.engine.contracts import CriterionResult, EvidenceRef, PairKey  # noqa: E402
from ctrec.fact_store import FactStore, FactStoreError  # noqa: E402
from ctrec.fact_view import tier  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.schemas import ClarifyingQuestion, LabValue, LLMAssessment, MatcherOutput, PatientProfile, QuestionSet, RuleMeta  # noqa: E402

PID = "PA-0123456789abcdef"
HB = _rule(rule_id="I3", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin", operator=">=", value="9", unit="g/dL")
AGE = _rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18", unit="years")
JUDGE = _rule(rule_id="I5", text="Adequate organ function in the investigator's judgment", category="comorbidity", structured_evaluable=False, underspecified=True)
RECORD = {"I3": RuleMeta(required_evidence="clinical_record")}
TEXT = "62세 여성. Hb 12 g/dL"


class Tracer:
    def log(self, *a, **k):
        pass


def profile(age=62, labs=True):
    lab = [LabValue(name="hemoglobin", value=12.0, unit="g/dL", date=None, source_quote="Hb 12 g/dL")] if labs else []
    return PatientProfile(patient_id=PID, age=age, age_value=age, age_unit="years", sex="female", primary_diagnosis=None, disease_stage=None, diagnoses=[],
                          biomarkers=[], labs=lab, medications=[], treatment_history=[], ecog=None, other_facts=[], missing_or_ambiguous=[])


@pytest.fixture
def session(monkeypatch):
    def make(rules, input_source, text=TEXT, prof=None, after=None, answer=None, meta=RECORD):
        p = _parsed([r.model_copy() for r in rules])
        p.rule_meta = dict(meta)

        def stub(model, *, system=None, user=None, **k):
            rs = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
            return MatcherOutput(assessments=[LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent", confidence="low",
                                                            evidence="근거 없음", reasoning="stub", missing_info="확인 필요") for r in rs])

        def questions(*a, **k):
            return QuestionSet(questions=[ClarifyingQuestion(question_id="Q1", question="혈색소 수치를 알려주세요", answer_type="free_text", target=["T1:I3"],
                                                             purpose="eligibility", why="x")])

        class Ans:
            def answer(self, qs, pid):
                return {q.question_id: answer for q in qs}

        monkeypatch.setattr(matcher.llm, "structured", stub)
        monkeypatch.setattr(config, "WRITE_REPORT", False)
        monkeypatch.setattr(pipeline.trial_store, "get", lambda t: p)
        first = prof or profile()
        monkeypatch.setattr(pipeline.patient_profiler, "profile_patient",
                            lambda pid, raw, reference_date=None: after if after is not None and "[추가 확인 정보" in raw else first)
        monkeypatch.setattr(pipeline.question_generator, "generate_questions", questions)
        s = Session(case=PatientCase(PID, text, input_source=input_source), trials={"T1": dict(TRIAL)}, answerer=Ans(), tracer=Tracer())
        s.profile_patient()
        s.match()
        return s, p
    return make


def pair_status(s):
    v = SessionView.from_session(s)
    v.matched_versions = {t: v.snapshot.version for t in s.matches}
    return CohortState.from_decision_states({PID: build_state(v)}).pair(PID, "T1").status.value


def fact(input_source="clinical_record"):
    store = FactStore()
    return store, store.add(PID, "lab:hemoglobin", "12", source_type=input_source)


# ------------------------------------------------------------ 위험: 문서를 본 적 없는 선언
@pytest.mark.xfail(strict=True, reason="clinical_record 선언만으로 적격 확정. 정책 결정 대기 (R2 대안 A/B/C)")
def test_declaration_alone_does_not_confirm_eligibility(session):
    s, _ = session([HB], "clinical_record")
    assert pair_status(s) != "CONFIRMED_ELIGIBLE"


def test_the_declared_path_is_what_the_helper_reports(session):
    s, p = session([HB], "clinical_record")
    assert eb.unverified_record_passes(s.matches["T1"], p, s.fact_store, PID) == ["I3"]


def test_hold_keeps_the_stated_value_and_routes_to_a_document_request(session):
    s, p = session([HB], "clinical_record")
    m = eb.replace_unverified(s.matches["T1"], p, s.fact_store, PID)
    a = m.assessments[0]
    assert (m.eligibility, m.match_status) == ("UNCERTAIN", "POTENTIAL_MATCH") and a.passes is None and a.needs_verification
    assert a.self_reported_holds == "yes" and eb.HELD_FLAG in a.validation_flags


def test_mark_keeps_the_verdict(session):
    s, p = session([HB], "clinical_record")
    m = eb.replace_unverified(s.matches["T1"], p, s.fact_store, PID, change=eb.mark)
    assert m.eligibility == "ELIGIBLE" and m.assessments[0].passes is True and eb.HELD_FLAG in m.assessments[0].validation_flags


def test_helper_is_not_wired_into_any_runtime_path():
    src = Path(__file__).parent.parent / "src" / "ctrec"
    users = [p.name for p in src.rglob("*.py") if p.name != "evidence_basis.py" and "evidence_basis" in p.read_text(encoding="utf-8")]
    assert users == []


def test_helper_clears_once_a_document_reference_is_recorded(session):
    s, p = session([HB], "clinical_record")
    for f in s.fact_store.facts(PID):
        s.fact_store.set_verification(f.fact_id, "document_verified", reason="합성 결과지", by="test", document_reference="SYN-DOC-1")
    s.match()
    assert eb.unverified_record_passes(s.matches["T1"], p, s.fact_store, PID) == []


def test_web_free_text_cannot_declare_a_record(session):
    s, _ = session([HB], "unknown")
    assert pair_status(s) == "VERIFICATION_PENDING"


# ------------------------------------------------------------ verified인데 문서 참조가 없는 경우
@pytest.mark.parametrize("reference", ["", "   ", None])
def test_store_refuses_verified_without_a_reference(reference):
    store, f = fact()
    with pytest.raises(FactStoreError):
        store.set_verification(f.fact_id, "document_verified", reason="r", by="b", document_reference=reference)


def test_a_status_string_with_no_history_is_not_document_backed():
    _, f = fact()
    f.verification_status = "document_verified"
    assert not eb.document_backed(f) and eb.fact_basis(f) == "declared_record"


def test_tier_requires_a_verification_history():
    _, f = fact()
    f.verification_status = "document_verified"
    assert tier(f) == "declared_record"
    store, g = fact()
    store.set_verification(g.fact_id, "document_verified", reason="r", by="b", document_reference="SYN-DOC-1")
    assert tier(g) == "verified"


def test_forged_status_cannot_rule_out_or_clear_the_mismatch(session):
    high = HB.model_copy(update={"text": "Hemoglobin >= 15 g/dL", "value": "15"})
    s, _ = session([high], "clinical_record")
    for f in s.fact_store.facts(PID):
        f.verification_status = "document_verified"
    s.match()
    assert pair_status(s) == "VERIFICATION_PENDING"


@pytest.mark.xfail(strict=True, reason="evidence_satisfied 검증이 evidence_refs가 있을 때만 돎: 근거 없이 clinical_record 충족을 기록할 수 있음 (cohort_state.criterion_result가 이 경로)")
def test_required_record_cannot_be_satisfied_with_no_refs():
    with pytest.raises(ValueError):
        CriterionResult(pair=PairKey(patient_alias=PID, trial_id="T1"), rule_id="I3", state="MET", adjudication="rule", decided_by="rule_engine",
                        required_evidence="clinical_record", evidence_satisfied=True, evaluator_version="x")


@pytest.mark.xfail(strict=True, reason="EvidenceRef.verified는 상태 문자열뿐이고 문서 참조 필드가 없음: 계약 변경 필요 (대안 B)")
def test_evidence_ref_says_which_document_verified_it():
    assert "document_reference" in EvidenceRef.model_fields


def test_free_text_reference_is_accepted_and_proves_nothing():
    """현재 계약의 한계를 고정: 참조 문자열은 문서가 있다는 증거가 아님. 서버 검증이 생기면 이 테스트를 바꿉니다."""
    store, f = fact()
    store.set_verification(f.fact_id, "document_verified", reason="r", by="b", document_reference="아무 글자")
    assert eb.document_backed(f) and tier(f) == "verified"


def test_contract_fact_is_not_promoted_to_record_before_verification():
    class Prop:
        key = "stage_iv"

    class F:
        fact_id, proposition, source, verification_status, contract_id, contract_version = "F1", Prop, "clinical_record", "unknown", None, None

    assert evidence_ref(F).source_type == "self_report" and not evidence_ref(F).verified


# ------------------------------------------------------------ 어느 대안이든 보존
def test_valid_self_report_criterion_still_confirms(session):
    s, _ = session([AGE], "self_report", text="62세 여성", prof=profile(labs=False), meta={"I1": RuleMeta(required_evidence="self_report_ok")})
    assert pair_status(s) == "CONFIRMED_ELIGIBLE"
    assert eb.unverified_record_passes(s.matches["T1"], s.parsed["T1"], s.fact_store, PID) == []


def test_verified_synthetic_document_still_confirms(session):
    s, _ = session([HB], "clinical_record")
    for f in s.fact_store.facts(PID):
        s.fact_store.set_verification(f.fact_id, "document_verified", reason="합성 결과지", by="test", document_reference="SYN-DOC-1")
    s.match()
    assert pair_status(s) == "CONFIRMED_ELIGIBLE"


@pytest.mark.parametrize("source", ["clinical_record", "unknown", "self_report"])
def test_explicit_unknown_stays_unresolved(session, source):
    s, _ = session([HB], source, text="폐암 환자", prof=profile(labs=False))
    assert s.matches["T1"].eligibility == "UNCERTAIN" and s.matches["T1"].assessments[0].passes is None
    assert pair_status(s) not in ("CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE")


@pytest.mark.parametrize("source", ["clinical_record", "unknown"])
def test_clinician_only_criterion_stays_held(session, source):
    s, _ = session([JUDGE], source, meta={})
    assert s.matches["T1"].eligibility != "ELIGIBLE" and pair_status(s) == "EXPERT_REVIEW"
    assert eb.criterion_basis(s.matches["T1"].assessments[0], []) == "clinician"


def test_answer_to_a_record_criterion_stays_a_patient_report(session):
    after = profile()
    s, _ = session([HB], "unknown", text="62세 여성", prof=profile(labs=False), after=after, answer="Hb 12 g/dL")
    s.clarify()
    assert s.matches["T1"].eligibility == "UNCERTAIN" and s.matches["T1"].assessments[0].needs_verification
