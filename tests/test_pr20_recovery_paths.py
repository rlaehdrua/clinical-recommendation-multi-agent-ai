"""PR #20 복구 경로의 나머지 검증 (합성 데이터, LLM 호출 없음). test_pr20_review_regressions.py가 다루지 않는 경로.

- 앞부분: 실제 Session으로 failed_sources·UNREACHABLE·BLOCKED_FAILURE·DEFER·대체 문서 요청 경로 확인 (통과해야 함)
- R1·R2: H에서 strict xfail로 고정했던 결함. 수정 뒤 표시를 지워 통과 테스트로 전환 (기대값은 그대로).
  재현 입력·호출 경로: docs/review/board_sim_diff/recovery_residual.md, 수정 내용: docs/review/pr20_recovery_fix.md
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.agents import matcher  # noqa: E402
from ctrec.decision_state import build_state  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.engine.dcvc import FullDCVCPolicy  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy  # noqa: E402
from ctrec.eval_hygiene import AliasMap  # noqa: E402

import decision_fixtures as d  # noqa: E402
from test_offline import _rule as offline_rule  # noqa: E402
from test_pr20_review_regressions import (ECOG, HB, KEEP_LOOP_OPEN, LAB_REPORT, PATIENT, RECORD, ROUNDS, TID,  # noqa: E402,F401
                                          _rule, _unreachable, requests, run, status)

EXHAUSTED = ("sources_exhausted", "item_sources_exhausted")
MED = "medication_list"
ANC = _rule("I2", "ANC >= 1.5", "lab", field="lab:absolute neutrophil count", operator=">=", value="1.5",
            structured_evaluable=True)


# ------------------------------------------------------------ 실제 Session: failed_sources·UNREACHABLE
def test_unreachable_is_source_scoped_and_requests_a_document_with_failure_reason(run):
    r = run([ECOG, HB, *KEEP_LOOP_OPEN], _unreachable(LegacyAdapterPolicy, {PATIENT}))
    ops = r.ops[r.alias]
    assert ops.failed_sources == {PATIENT}
    assert ops.exhausted_trials == frozenset()  # 시험 전체를 막지 않음
    doc = next(a.action for a in r.asked if a.action.action_type == "REQUEST_DOCUMENT" and a.action.info_key == "ecog")
    assert doc.source_id == RECORD and doc.reason_code == "alternative_source_after_failure"


def test_failed_source_is_not_asked_after_the_round_it_failed(run):
    r = run([ECOG, HB, *KEEP_LOOP_OPEN], _unreachable(LegacyAdapterPolicy, {PATIENT}))
    first = min(a.round for a in r.asked if a.action.source_id == PATIENT)
    assert [a for a in r.asked if a.action.source_id == PATIENT and a.round > first] == []


# ------------------------------------------------------------ 실제 Session: BLOCKED_FAILURE·DEFER
def test_all_sources_unreachable_blocks_by_item_not_by_whole_trial(run):
    r = run([ECOG, *KEEP_LOOP_OPEN], _unreachable(LegacyAdapterPolicy, {PATIENT, RECORD, LAB_REPORT}))
    ops = r.ops[r.alias]
    assert status(r) == C.PairStatus.BLOCKED_FAILURE
    assert ops.exhausted_trials == frozenset() and "ecog" in ops.exhausted_items


def test_blocked_pair_leaves_only_exhausted_defer_for_its_items(run):
    r = run([ECOG, *KEEP_LOOP_OPEN], _unreachable(LegacyAdapterPolicy, {PATIENT, RECORD, LAB_REPORT}))
    acts = [a for a in r.policy.propose(r.cohort()) if a.info_key and a.info_key != "finalize"]
    assert acts and all(a.action_type == "DEFER" and a.reason_code in EXHAUSTED for a in acts)


def test_blocked_pair_is_dropped_from_full_dcvc_candidates(run):
    r = run([ECOG, *KEEP_LOOP_OPEN], _unreachable(FullDCVCPolicy, {PATIENT, RECORD, LAB_REPORT}))
    assert status(r) == C.PairStatus.BLOCKED_FAILURE
    assert not [a for a in r.policy.propose(r.cohort())
                if a.action_type not in C.NON_EXECUTING and TID in a.target_trial_ids]


def test_ablation_blocks_the_whole_trial_and_requests_no_document(run):
    class NoRecovery(LegacyAdapterPolicy):
        recovery = False
    r = run([ECOG, HB, *KEEP_LOOP_OPEN], _unreachable(NoRecovery, {PATIENT}))
    assert TID in r.ops[r.alias].exhausted_trials
    assert status(r) == C.PairStatus.BLOCKED_FAILURE
    assert requests(r, "ecog") == []


# ------------------------------------------------------------ 실제 Session: 대체 문서 요청
def test_spaced_lab_key_links_the_question_to_its_document(run):
    """검사 이름에 공백이 있어도 질문과 문서 요청이 같은 항목으로 이어짐 (aca2033 리뷰 항목)."""
    r = run([ECOG, ANC, *KEEP_LOOP_OPEN], LegacyAdapterPolicy())
    assert requests(r, "lab:absolute neutrophil count")[:1] == [LAB_REPORT]


def test_rephrase_falls_back_to_a_document_after_second_no_answer(run):
    """바꿔 묻기도 "모름"이면 그 뒤 문서 요청이 실제 Session에서 실행됨 (R2 전에는 계획에만 남고 끝남)."""
    r = run([ECOG, HB, *KEEP_LOOP_OPEN], LegacyAdapterPolicy(rephrase=True))
    ecog = [a.action for a in r.asked if a.action.info_key == "ecog"]
    assert [a.reason_code for a in ecog if a.source_id == PATIENT][-1] == C.REPHRASE
    assert requests(r, "ecog") == [RECORD]


# ------------------------------------------------------------ R1: 답한 문서가 대체로 남아 소진되지 않던 결함
AM = AliasMap(salt="recovery-residual")
PA = AM.alias("SYN-RESIDUAL")
TACRO = offline_rule(rule_id="I9", text="No prior tacrolimus", category="medication", field=None, operator=None,
                     value=None, unit=None)
ITEM = "criterion:T1:I9"
UNREACHABLE = ("failed", C.UNREACHABLE)
NO_ANSWER = ("no_response", C.NO_ANSWER)
ANSWERED = ("succeeded", None)


@pytest.fixture
def stub_llm(monkeypatch):
    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)


def _cohort():
    s = d.store(("age", 60, {"unit": "years"}), patient="SYN-RESIDUAL")
    return CohortState.from_decision_states(
        {PA: build_state(d.view({"T1": d.trial("T1", [TACRO])}, s, patient="SYN-RESIDUAL"))})


def _drive(outcome_of):
    """그 항목의 행동을 정책이 내는 대로 실행. outcome_of(정보원) -> (상태, 코드). 반환: (정책, 코호트, 실행한 정보원)."""
    pol, co, sources = LegacyAdapterPolicy(), _cohort(), []
    for _ in range(6):
        acts = [a for a in pol.propose(co) if a.info_key == ITEM and a.action_type not in C.NON_EXECUTING
                and a.source_id not in sources]
        if not acts:
            break
        a = acts[0]
        sources.append(a.source_id)
        st, code = outcome_of(a.source_id)
        co, _ = pol.apply_outcome(co, a, C.ActionOutcome(action_id=a.action_id, status=st, error_code=code))
    return pol, co, sources


def _defers(pol, co):
    return {a.reason_code for a in pol.propose(co) if a.info_key == ITEM and a.action_type == "DEFER"}


def test_control_no_answer_from_every_document_blocks_the_pair(stub_llm):
    pol, co, sources = _drive(lambda s: UNREACHABLE if s == PATIENT else NO_ANSWER)
    assert sources == [PATIENT, MED, RECORD]
    assert co.pair(PA, "T1").status == C.PairStatus.BLOCKED_FAILURE


def test_tried_document_is_not_an_alternative_when_the_last_one_has_no_answer(stub_llm):
    """환자 연락 불가 -> 복약 목록은 답했지만 gap이 남음 -> 진료 기록은 "모름": 정책 스스로 소진 보류를 내는 상태."""
    pol, co, sources = _drive(lambda s: {PATIENT: UNREACHABLE, MED: ANSWERED}.get(s, NO_ANSWER))
    assert sources == [PATIENT, MED, RECORD] and _defers(pol, co) & set(EXHAUSTED)  # 시나리오 전제
    assert co.pair(PA, "T1").status == C.PairStatus.BLOCKED_FAILURE


def test_last_document_answered_with_gap_left_is_exhausted(stub_llm):
    pol, co, sources = _drive(lambda s: UNREACHABLE if s == PATIENT else ANSWERED)
    assert sources == [PATIENT, MED, RECORD] and _defers(pol, co) & set(EXHAUSTED)  # 시나리오 전제
    assert co.pair(PA, "T1").status == C.PairStatus.BLOCKED_FAILURE
