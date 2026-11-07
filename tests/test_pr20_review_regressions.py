"""PR #20 리뷰 회귀 테스트: 정보원 실패의 범위와 실제 Session의 연락 불가 뒤 문서 전환 (합성 데이터, LLM 호출 없음).

실제 Session에는 연락 불가를 만드는 실행기가 없으므로(현재 sim/environment.py만 생성) 정책의 apply_outcome에서
지정한 정보원의 결과를 UNREACHABLE로 바꿔 주입합니다. 나머지는 실제 Session, 정책 엔진 실행기, 정책 코드 그대로입니다.

- 결함 1: 항목 하나의 정보원이 끝났을 뿐인데, 다른 필수 항목의 문서가 남은 시험 전체가 BLOCKED_FAILURE가 됨
- 결함 2: 환자에게 이미 물은 항목이 연락 불가로 끝나면 문서로 넘어가지도, 최종 소진되지도 않음
대조 테스트는 수정 전에도 통과하며, 시나리오가 다른 이유로 실패하는 것이 아님을 보입니다.
"""

from __future__ import annotations

import pytest

from ctrec import config, llm, question_eval as qe, trial_store
from ctrec.agents import parser_validator as pv
from ctrec.engine import contracts as C
from ctrec.engine.dcvc import FullDCVCPolicy
from ctrec.engine.policy import LegacyAdapterPolicy
from ctrec.eval_hygiene import AliasMap
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import ParsedTrial, Rule
from ctrec.web import engine_runner as er

TID = "T1"
ROUNDS = 14
PATIENT, RECORD, LAB_REPORT = "patient_interview", "medical_record", "lab_report"


def _rule(rid, text, category, **k):
    base = dict(rule_id=rid, kind="inclusion", text=text, category=category, field=None, operator=None, value=None,
                unit=None, time_window_days=None, structured_evaluable=False, underspecified=False, cohort=None)
    return Rule(**{**base, **k})


def _lab(rid, name):
    return _rule(rid, f"{name} >= 1", "lab", field=f"lab:{name}", operator=">=", value="1", structured_evaluable=True)


EGFR = _rule("I0", "EGFR sensitizing mutation", "biomarker")
ECOG = _rule("I1", "ECOG performance status <= 2", "performance_status", field="ecog", operator="<=", value="2",
             structured_evaluable=True)
HB = _rule("I2", "Hemoglobin >= 9 g/dL", "lab", field="lab:hemoglobin", operator=">=", value="9", unit="g/dL",
           structured_evaluable=True)
# Session 루프는 열린 항목이 있어야 이어짐. 문서 전환이 실행될 라운드를 주기 위한 항목
KEEP_LOOP_OPEN = [_lab(f"I{i}", n) for i, n in enumerate(("platelets", "albumin", "creatinine", "sodium"), 3)]


def _trial():
    return {"trial_id": TID, "title": "synthetic", "conditions": ["NSCLC"], "phases": ["PHASE2"], "interventions": [],
            "overall_status": "RECRUITING", "eligibility_criteria": "Inclusion Criteria:\n- synthetic\n", "sex": "ALL",
            "minimum_age": None, "maximum_age": None, "healthy_volunteers": False, "brief_summary": "",
            "detailed_description": ""}


def _parsed(rules=()):
    return ParsedTrial(trial_id=TID, title="synthetic", target_population="", intervention_summary="",
                       key_conditions=[], cohorts=[], rules=list(rules), parsing_notes="stub")


class _Tracer:
    def log(self, *a, **k):
        pass


class _Answerer:
    def answer(self, questions, patient_id):
        return {q.question_id: "모름" for q in questions}


def _unreachable(base, sources):
    class Policy(base):
        def apply_outcome(self, cohort, action, outcome):
            if action.source_id in sources:
                outcome = C.ActionOutcome(action_id=action.action_id, status="failed", error_code=C.UNREACHABLE)
            return super().apply_outcome(cohort, action, outcome)
    return Policy()


@pytest.fixture
def run(tmp_path):
    with qe.OfflineEnvironment(1) as env:
        env._set(config, "DEMO_MODE", True)
        env._set(config, "PARSED_DIR", tmp_path / "parsed")
        env._set(config, "CACHE_DIR", tmp_path / "cache")
        real = env.stub.structured

        def structured(model_cls, *, system, user, **k):
            if model_cls is ParsedTrial:
                return _parsed()
            if model_cls is pv.ValidationOutput:
                return pv.ValidationOutput(checks=[], missing=[])
            return real(model_cls, system=system, user=user, **k)
        llm.structured = structured  # OfflineEnvironment가 종료할 때 복원 (monkeypatch를 겹치지 않음)

        def execute(rules, policy):
            trial = _trial()
            trial_store.save(trial, _parsed(rules), {}, pv.ValidationReport())
            session = Session(case=PatientCase(AliasMap(salt="pr20").alias("SYN-PR20"), "Synthetic 62-year-old male.",
                                               input_source="unknown", candidate_trials=[TID]),
                              trials={TID: trial}, answerer=_Answerer(), tracer=_Tracer(), max_rounds=ROUNDS)
            with er.activate(session, policy=policy) as runner:
                session.run_fixed()
            return runner
        yield execute


def requests(runner, key):
    return [a.action.source_id for a in runner.asked
            if a.action.action_type == "REQUEST_DOCUMENT" and a.action.info_key == key]


def status(runner):
    co = runner.cohort()
    return co.pair(runner.alias, TID).status


def blocked_while_requesting(runner):
    """그 라운드에 문서를 요청하는 시험이 이미 BLOCKED_FAILURE로 집계된 라운드 번호."""
    return [p["round"] for p in runner.plans
            if p["pairs"].get("BLOCKED_FAILURE") and any(x["action_type"] == "REQUEST_DOCUMENT" for x in p["asked"])]


# ------------------------------------------------------------ 결함 1: 정보원 실패가 시험 전체를 막음
def test_defect1_trial_stays_open_while_a_document_for_another_item_remains(run):
    """ECOG의 마지막 문서가 연락 불가여도 Hb는 검사지로 확인할 수 있음 -> 그 요청이 나가는 동안 시험은 BLOCKED가 아님."""
    r = run([EGFR, ECOG, HB], _unreachable(LegacyAdapterPolicy, {PATIENT, RECORD}))
    assert requests(r, "ecog") == [RECORD] and requests(r, "lab:hemoglobin")[:1] == [LAB_REPORT]  # 시나리오 전제
    assert blocked_while_requesting(r) == []


def test_defect1_full_dcvc_still_requests_the_remaining_document(run):
    """BLOCKED 쌍은 full_dcvc의 후보에서 빠지므로, 조기 BLOCKED가 남은 문서 요청 자체를 없앰."""
    r = run([EGFR, ECOG, HB], _unreachable(FullDCVCPolicy, {PATIENT, RECORD}))
    assert requests(r, "ecog") == [RECORD]  # 시나리오 전제
    assert requests(r, "lab:hemoglobin")[:1] == [LAB_REPORT]


# ------------------------------------------------------------ 결함 2: 실제 Session의 연락 불가 뒤 문서 전환
def test_defect2_asked_item_moves_to_a_document_after_unreachable(run):
    """환자에게 물은 ECOG가 연락 불가 -> 문서가 남아 있으므로 항목 실패("모름")와 같이 문서를 요청해야 함."""
    r = run([ECOG, HB, *KEEP_LOOP_OPEN], _unreachable(LegacyAdapterPolicy, {PATIENT}))
    assert r.asked[0].action.info_key == "ecog"  # 시나리오 전제: 환자에게 먼저 물은 항목
    assert requests(r, "ecog") == [RECORD]


def test_defect2_unreachable_then_document_failure_is_finally_exhausted(run):
    """recovery=True: 연락 불가 -> 문서 요청 -> 문서도 "모름" -> 그 항목이 소진되고 시험은 BLOCKED_FAILURE."""
    r = run([ECOG, *KEEP_LOOP_OPEN], _unreachable(LegacyAdapterPolicy, {PATIENT}))
    assert "ecog" in r.ops[r.alias].exhausted_items
    assert status(r) == C.PairStatus.BLOCKED_FAILURE


# ------------------------------------------------------------ 대조: 수정 전에도 통과
def test_control_no_answer_moves_the_same_item_to_a_document(run):
    r = run([ECOG, HB, *KEEP_LOOP_OPEN], LegacyAdapterPolicy())
    assert r.asked[0].action.info_key == "ecog" and requests(r, "ecog") == [RECORD]
