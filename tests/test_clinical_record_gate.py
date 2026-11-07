"""진료 기록 필요 기준의 문서 게이트: 입력 유형별 회귀와 위조 차단 (합성 데이터, LLM·API 호출 없음).

기본 Session과 ContractSession을 실제로 실행합니다. 위조 입력은 운영 기본 권한(NoDocumentAuthority) 아래에서 봅니다.
"""

from __future__ import annotations

import contextvars
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from document_support import AUTHORITY, attest, attest_declared, documented  # noqa: E402
from test_decisions import TRIAL, _parsed  # noqa: E402
from test_offline import _rule  # noqa: E402

from ctrec import config, document_authority, evidence_basis as eb, pipeline, status  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.decision_state import SessionView, build_state  # noqa: E402
from ctrec.document_authority import NoDocumentAuthority, ServerAttestation  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.fact_store import FactStore, FactStoreError  # noqa: E402
from ctrec.fact_view import FactEvidenceView, document_coverage, tier  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.schemas import LabValue, LLMAssessment, MatcherOutput, PatientProfile, RuleMeta  # noqa: E402

PID = "PA-0123456789abcdef"
HB = _rule(rule_id="I3", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin", operator=">=", value="9", unit="g/dL")
HB_HIGH = HB.model_copy(update={"text": "Hemoglobin >= 15 g/dL", "value": "15"})
AGE = _rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18", unit="years")
EGFR = _rule(rule_id="I4", text="EGFR mutation positive", category="biomarker", structured_evaluable=False)
JUDGE = _rule(rule_id="I5", text="Adequate organ function in the investigator's judgment", category="comorbidity",
              structured_evaluable=False, underspecified=True)
RECORD = {"I3": RuleMeta(required_evidence="clinical_record"), "I4": RuleMeta(required_evidence="clinical_record")}
SELF_OK = {"I1": RuleMeta(required_evidence="self_report_ok")}
TEXT = "62세 여성. Hb 12 g/dL"
BOTH = [Session, ContractSession]


class Tracer:
    def log(self, *a, **k):
        pass


def hb(value=12.0, quote="Hb 12 g/dL"):
    return LabValue(name="hemoglobin", value=value, unit="g/dL", date=None, source_quote=quote)


def profile(labs=None):
    return PatientProfile(patient_id=PID, age=62, age_value=62, age_unit="years", sex="female", primary_diagnosis=None, disease_stage=None,
                          diagnoses=[], biomarkers=[], labs=[hb()] if labs is None else labs, medications=[], treatment_history=[], ecog=None,
                          other_facts=[], missing_or_ambiguous=[])


def _unknown(model, *, system=None, user=None, **k):
    rs = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
    return MatcherOutput(assessments=[LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent", confidence="low",
                                                    evidence="근거 없음", reasoning="stub", missing_info="확인 필요") for r in rs])


@pytest.fixture
def run(monkeypatch):
    def go(rules, source="clinical_record", *, meta=RECORD, text=TEXT, prof=None, cls=Session, llm=_unknown, prepare=None):
        p = _parsed([r.model_copy() for r in rules])
        p.rule_meta = dict(meta)
        monkeypatch.setattr(matcher.llm, "structured", llm)
        monkeypatch.setattr(config, "WRITE_REPORT", False)
        monkeypatch.setattr(pipeline.trial_store, "get", lambda t: p)
        monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", lambda pid, raw, reference_date=None: prof or profile())
        s = cls(case=PatientCase(PID, text, input_source=source), trials={"T1": dict(TRIAL)}, answerer=None, tracer=Tracer())
        s.profile_patient()
        if prepare:
            prepare(s)
        s.match()
        return s
    return go


def pair(s, overlay=None):
    v = SessionView.from_session(s)
    v.matched_versions = {t: v.snapshot.version for t in s.matches}
    return CohortState.from_decision_states({PID: build_state(v, overlay)}).pair(PID, "T1")


def documents(s):
    attest_declared(s.fact_store, PID)


# ------------------------------------------------------------ 운영 기본값과 위조
@pytest.fixture
def production_authority():
    with document_authority.use(NoDocumentAuthority()):
        yield


def test_the_default_authority_trusts_nothing():
    assert isinstance(contextvars.Context().run(document_authority.current), NoDocumentAuthority)


def test_no_production_code_installs_an_authority():
    """운영 권한이 생기기 전에는 시뮬레이터 말고 document_authority.use를 부르는 코드가 없어야 함."""
    src = Path(__file__).parent.parent / "src" / "ctrec"
    users = [p.relative_to(src).as_posix() for p in src.rglob("*.py") if p.name != "document_authority.py"
             and re.search(r"document_authority\.use\(", p.read_text(encoding="utf-8"))]
    assert users in ([], ["sim/evaluator.py"])


def forged_status(s):
    for f in s.fact_store.facts(PID):
        f.verification_status = "document_verified"


def free_text_reference(s):
    for f in s.fact_store.facts(PID):
        s.fact_store.set_verification(f.fact_id, "document_verified", reason="r", by="b", document_reference="아무 글자")


def self_made_attestation(s):
    for f in s.fact_store.facts(PID):
        forged = ServerAttestation(document_id="DOC", sha256="0" * 64, fact_digest=document_authority.fact_digest(f), verifier="me", signature="sig")
        s.fact_store.set_verification(f.fact_id, "document_verified", reason="r", by="b", document_reference="DOC")
        f.verification_history[-1].attestation = forged


FORGERIES = {"declared": None, "status_string": forged_status, "free_text_reference": free_text_reference, "self_made_attestation": self_made_attestation}


@pytest.mark.parametrize("cls", BOTH)
@pytest.mark.parametrize("name", FORGERIES)
def test_nothing_short_of_the_authority_confirms(run, production_authority, cls, name):
    s = run([HB], cls=cls, prepare=FORGERIES[name])
    m = s.matches["T1"]
    assert (m.eligibility, m.match_status) == ("UNCERTAIN", "POTENTIAL_MATCH")
    assert pair(s).status.value == "VERIFICATION_PENDING"
    a = m.assessments[0]
    assert a.passes is None and a.needs_verification and eb.HELD_FLAG in a.validation_flags


@pytest.mark.parametrize("name", FORGERIES)
def test_forgery_does_not_confirm_a_mismatch_either(run, production_authority, name):
    s = run([HB_HIGH], prepare=FORGERIES[name])
    assert s.matches["T1"].eligibility == "INELIGIBLE" and pair(s).status.value == "VERIFICATION_PENDING"


def test_authentic_attestation_does_not_work_under_the_production_authority(run):
    """대조군 권한의 증명을 가져와도 운영 기본 권한에서는 무효."""
    s = run([HB], prepare=documents)
    assert s.matches["T1"].eligibility == "ELIGIBLE"
    with document_authority.use(NoDocumentAuthority()):
        s.match()
        assert s.matches["T1"].eligibility == "UNCERTAIN" and pair(s).status.value == "VERIFICATION_PENDING"


def test_attestation_is_bound_to_the_value():
    store = FactStore()
    good = store.add(PID, "lab:hemoglobin", 12.0, unit="g/dL", source_type="clinical_record")
    other = store.add(PID, "lab:hemoglobin", 8.0, unit="g/dL", source_type="clinical_record")
    att = AUTHORITY.issue(good)
    with pytest.raises(FactStoreError):
        store.attest(other.fact_id, att, AUTHORITY, reason="r", by="b")
    attest(store, good.fact_id)
    assert tier(good) == "verified"
    store.set_verification(other.fact_id, "document_verified", reason="r", by="b", document_reference=att.document_id)
    other.verification_history[-1].attestation = att  # 다른 값으로 옮긴 증명
    assert tier(other) == "declared_record"


def test_attest_refuses_what_the_authority_refuses():
    store = FactStore()
    f = store.add(PID, "lab:hemoglobin", 12.0, unit="g/dL", source_type="clinical_record")
    with pytest.raises(FactStoreError):
        store.attest(f.fact_id, AUTHORITY.issue(f), NoDocumentAuthority(), reason="r", by="b")
    assert f.verification_status == "unknown" and f.verification_history == []


def test_attestation_survives_a_store_round_trip(tmp_path):
    store = FactStore(root=tmp_path)
    f = store.add(PID, "lab:hemoglobin", 12.0, unit="g/dL", source_type="clinical_record")
    attest(store, f.fact_id)
    store.save()
    again = FactStore.load(tmp_path)
    assert tier(again.facts(PID)[0]) == "verified"
    with document_authority.use(NoDocumentAuthority()):
        assert tier(again.facts(PID)[0]) == "declared_record"


def test_a_later_status_change_ends_the_verification():
    store = FactStore()
    f = store.add(PID, "lab:hemoglobin", 12.0, unit="g/dL", source_type="clinical_record")
    attest(store, f.fact_id)
    store.set_verification(f.fact_id, "unverified", reason="문서 철회", by="b")
    assert tier(f) == "declared_record"


# ------------------------------------------------------------ 입력 유형별 (Session·ContractSession 공통)
# ContractSession은 self_report 선언 입력을 답변 의존으로 보고 보류함 (qa_guard.qa_dependent, 이번 변경과 무관). 선언 없는 입력으로 대조
@pytest.mark.parametrize("cls, source", [(Session, "self_report"), (ContractSession, "unknown")])
def test_normal_self_report_still_confirms(run, cls, source):
    s = run([AGE], source, cls=cls, meta=SELF_OK, text="62세 여성")
    assert s.matches["T1"].eligibility == "ELIGIBLE" and pair(s).status.value == "CONFIRMED_ELIGIBLE"


@pytest.mark.parametrize("cls", BOTH)
def test_verified_fact_confirms(run, cls):
    s = run([HB], cls=cls, prepare=documents)
    assert s.matches["T1"].match_status == "CRITERIA_MATCH" and pair(s).status.value == "CONFIRMED_ELIGIBLE"
    c = pair(s).criteria[0]
    assert c.evidence_satisfied and c.evidence_refs and all(r.verified for r in c.evidence_refs)


@pytest.mark.parametrize("cls", BOTH)
def test_verified_mismatch_is_a_confirmed_ineligible(run, cls):
    s = run([HB_HIGH], cls=cls, prepare=documents)
    assert s.matches["T1"].eligibility == "INELIGIBLE" and pair(s).status.value == "CONFIRMED_INELIGIBLE"


@pytest.mark.parametrize("cls", BOTH)
def test_declared_mismatch_stays_ineligible_and_provisional(run, cls):
    s = run([HB_HIGH], cls=cls)
    m = s.matches["T1"]
    assert (m.eligibility, m.match_status) == ("INELIGIBLE", "CRITERIA_MISMATCH") and m.assessments[0].passes is False
    assert pair(s).status.value == "VERIFICATION_PENDING"


@pytest.mark.parametrize("cls", BOTH)
@pytest.mark.parametrize("source", ["clinical_record", "self_report", "unknown"])
def test_unknown_value_stays_unresolved(run, cls, source):
    s = run([HB], source, cls=cls, text="폐암 환자", prof=profile(labs=[]))
    assert s.matches["T1"].eligibility == "UNCERTAIN" and s.matches["T1"].assessments[0].passes is None
    assert pair(s).status.value not in ("CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE")


@pytest.mark.parametrize("cls", BOTH)
def test_conflicting_values_stay_open_even_with_a_document(run, cls):
    def conflict(s):
        documents(s)
        s.fact_store.add(PID, "lab:hemoglobin", 8.0, unit="g/dL", source_type="self_report",
                         source_reference={"kind": "answer", "quote": "Hb 8"})
    s = run([HB], cls=cls, prepare=conflict)
    assert s.matches["T1"].eligibility == "UNCERTAIN" and s.matches["T1"].assessments[0].passes is None
    assert pair(s).status.value not in ("CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE")


@pytest.mark.parametrize("cls", BOTH)
@pytest.mark.parametrize("prepare", [None, documents], ids=["declared", "documented"])
def test_clinician_rule_needs_a_clinician_whatever_the_document(run, cls, prepare):
    def yes(model, *, system=None, user=None, **k):
        return MatcherOutput(assessments=[LLMAssessment(rule_id="I5", applicable=True, holds="yes", evidence_type="explicit", confidence="high",
                                                        evidence="Hb 12 g/dL", reasoning="stub", missing_info="")])
    s = run([JUDGE], cls=cls, meta={"I5": RuleMeta(required_evidence="clinical_record")}, llm=yes, prepare=prepare)
    assert s.matches["T1"].eligibility != "ELIGIBLE" and pair(s).status.value == "EXPERT_REVIEW"
    a = s.matches["T1"].assessments[0]
    assert a.underspecified and not a.needs_verification  # 문서 요청으로 끝나는 기준이 아님


def test_the_recommender_does_not_call_a_declared_value_a_patient_report(run):
    from ctrec.agents.recommender import verification_notes
    s = run([HB])
    [note] = verification_notes(s.matches["T1"])
    assert "선언된 기록상" in note and "환자 답변상" not in note


# ------------------------------------------------------------ 근거 범위
def _llm_yes(quote):
    def stub(model, *, system=None, user=None, **k):
        rs = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
        return MatcherOutput(assessments=[LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="yes", evidence_type="explicit", confidence="high",
                                                        evidence=quote, reasoning="stub", missing_info="") for r in rs])
    return stub


def test_an_uncited_criterion_is_not_covered_by_a_document_on_another_field(run):
    """나이 문서가 있어도, 인용이 어떤 사실과도 이어지지 않는 LLM 판정은 확정하지 않음."""
    s = run([EGFR], llm=_llm_yes("EGFR 양성"), text="EGFR 양성. Hb 12 g/dL", prepare=documents)
    assert s.matches["T1"].assessments[0].passes is None


def test_document_coverage_needs_every_value_but_one_document_per_value():
    store = FactStore()
    a = store.add(PID, "lab:hemoglobin", 12.0, unit="g/dL", source_type="clinical_record")
    store.add(PID, "lab:hemoglobin", 12.0, unit="g/dL", source_type="self_report", source_reference={"kind": "answer", "quote": "Hb 12"})
    b = store.add(PID, "lab:platelets", 200.0, unit="x10^9/L", source_type="clinical_record")
    attest(store, a.fact_id)
    v = FactEvidenceView(store, PID)
    same = v.fields["lab:hemoglobin"].facts
    assert document_coverage(same) == "verified"  # 같은 값: 문서 하나로 충분
    assert document_coverage(same + v.fields["lab:platelets"].facts) == "declared_record"  # 다른 항목은 각자 확인
    attest(store, b.fact_id)
    v = FactEvidenceView(store, PID)
    assert document_coverage(v.fields["lab:hemoglobin"].facts + v.fields["lab:platelets"].facts) == "verified"
    assert document_coverage([]) == "unknown"


def test_a_document_older_than_the_window_does_not_confirm(run):
    windowed = HB.model_copy(update={"time_window_days": 28})

    def old(s):
        for f in list(s.fact_store.facts(PID)):
            f.observed_at = "2020-01-01"
        documents(s)
    s = run([windowed], prepare=old)
    assert s.matches["T1"].eligibility == "UNCERTAIN"


# ------------------------------------------------------------ Matcher를 거치지 않은 판정
def test_match_trial_without_a_check_holds_even_with_no_source_text(monkeypatch):
    monkeypatch.setattr(matcher.llm, "structured", _unknown)
    p = _parsed([HB])
    p.rule_meta = dict(RECORD)
    m = matcher.match_trial(profile(), p, TRIAL)
    assert m.eligibility == "UNCERTAIN" and m.assessments[0].needs_verification
    m = matcher.match_trial(profile(), p, TRIAL, document_check=documented)
    assert m.eligibility == "ELIGIBLE"


def test_a_stored_eligible_result_is_not_confirmed_by_the_decision_state(run):
    """저장·주입된 ELIGIBLE이 들어와도 decision_state가 같은 요건으로 막음."""
    s = run([HB])
    s.matches["T1"] = matcher.match_trial(s.profile, s.parsed["T1"], s.trials["T1"], source_text=s.case.raw_text, input_source="clinical_record",
                                          document_check=documented)
    assert s.matches["T1"].eligibility == "ELIGIBLE"
    p = pair(s)
    assert p.status.value == "VERIFICATION_PENDING" and p.criteria[0].gap == "unverified" and not p.criteria[0].evidence_satisfied


def test_a_reevaluation_overlay_is_held_the_same_way(run):
    s = run([HB])
    met = s.matches["T1"].assessments[0].model_copy(update={"holds": "yes", "passes": True, "needs_verification": False})
    assert pair(s, {"T1:I3": met}).criteria[0].gap == "unverified"


def test_a_contract_fact_is_never_a_record_without_the_authority(production_authority):
    from ctrec.proposition_store import PropositionLedger
    from ctrec.propositions import Proposition
    store = FactStore()
    prop = Proposition.parse("surgery/event/ever")
    f = store.add(PID, "prop:" + prop.key, True, source_type="clinical_record", session_id="S",
                  source_reference={"kind": "answer", "ref": "contract:QC-X@v1#000000000000;q:r1:C1", "quote": "예"}, observed_at="2026-10-01")
    store.set_verification(f.fact_id, "document_verified", reason="r", by="b", document_reference="DOC")
    led = PropositionLedger(store)
    led.issued.add(f.fact_id)
    [got] = led.facts(PID, "S")
    assert got.source == "self_report" and got.verification_status == "unverified" and got.document_reference is None



def test_decision_state_lists_forged_facts_as_unverified(run, production_authority):
    s = run([HB], prepare=forged_status)
    st = build_state(SessionView.from_session(s))
    assert set(st.unverified_fact_ids) == {f.fact_id for f in s.fact_store.facts(PID)}


def test_external_status_follows_the_gate(run):
    s = run([HB])
    assert status.external_status(s.matches["T1"]) == "POTENTIAL_MATCH"
