"""Q&A 판정 정책 이전 테스트 (P8): 기존 정책과 계약 정책을 같은 입력으로 나란히 검증.

서비스 기본 정책은 바꾸지 않습니다(qa_guard.policy: CTREC_QA_POLICY 미설정 = legacy). 각 테스트가 정책을 직접 고릅니다.

1. Legacy Regression          : legacy 정책의 현재 동작을 기록 (기존 테스트 4건의 입력 + 새 사례 2건)
2. Contract Policy Regression : contract 정책에서 계약 없는 답변에 기댄 확정은 UNVERIFIED (답변상 판정은 보존)
3. Migration Comparison       : 두 정책의 차이표. 사례 종류별로 의미가 다름
   - A (질문이 기준과 의미상 다름)          : legacy의 확정은 근거 없는 확정 -> contract에서 막는 것이 안전성 개선
   - B (질문이 기준을 정확히 물었으나 계약 없음): legacy의 확정은 대체로 옳음 -> contract에서 막히는 것은 계약 도입 전의
                                             기능 제한(정상 판정 손실). 승인된 계약이 있으면 결정적으로 회복됨
   - B-free (질문은 넓고 답이 기준을 직접 진술) : 자유 서술은 계약 선택지가 아니므로 B와 같이 기능 제한

LLM은 결정적 대역이며 API를 부르지 않습니다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import pipeline, qa_guard
from ctrec.contract_session import ContractSession
from ctrec.schemas import CriterionAssessment

from contract_fixtures import QC_SURGERY, RULES
from test_contract_p5_session import _Answerer, _contract_env, _profile_from, _session, _trials, patched  # noqa: F401
from test_evidence_directness import B_TEXT, LIG
from test_evidence_directness import SURG as WRIST
from test_evidence_directness import TFCC as WRIST_TFCC
from test_evidence_directness import _match as _e9_match
from test_evidence_directness_counterexamples import SURG_Q, SURGERY, _judge, _log, _qa
from test_stage05 import SOURCE as HEP_SOURCE
from test_stage05 import _la, _run as _hep_run

pytestmark = pytest.mark.skipif("qa_guard.policy()" not in __import__("inspect").getsource(
    __import__("ctrec.agents.matcher", fromlist=["match_trial"]).match_trial),
    reason="matcher 정책 선택 패치(03 v2)가 적용된 환경에서만 의미가 있음")

SIDE_EFFECT_Q = "최근 4주 이내 수술 부작용이 있었나요?"
SIDE_EFFECT_A = "아니요, 부작용은 전혀 없었어요"
EXACT_Q = "최근 4주(28일) 안에 큰 수술을 받은 적이 있나요?"
EXACT_A = "아니요, 최근 4주 안에 큰 수술은 받지 않았어요"


@dataclass(frozen=True)
class Case:
    case_id: str
    kind: str  # A / B / B-free
    source: str  # 기존 테스트 이름 또는 '신규'
    question: str
    answer: str
    criterion: str
    run: Callable[[pytest.MonkeyPatch], CriterionAssessment]
    legacy: tuple  # (holds, passes)
    meaning: str


def _l1(mp):
    return _e9_match(mp, [WRIST, WRIST_TFCC, LIG], [("I1", "yes", "explicit", "손목 수술은 했는데"),
                                                     ("I2", "no", "explicit", "TFCC인지 인대재건술인지는 몰라요"),
                                                     ("E1", "no", "explicit", "인대재건술인지는 몰라요")], B_TEXT)["I1"]


def _l2(mp):
    return _judge(mp, SURGERY, SURG_Q, "yes", "Q: 수술 받은 적 있나요? A: 예", required="self_report_ok",
                  qa_log=_log())[1]


def _l3(mp):
    return _judge(mp, SURGERY, "45세 남성." + _qa("수술 받은 적 있나요?", "아니요"), "no",
                  "Q: 수술 받은 적 있나요? A: 아니요", required="self_report_ok", qa_log=_log(answer="아니요"))[1]


def _l4(mp):
    source = HEP_SOURCE + "\n\n[추가 확인 정보 - 라운드 1]\nQ: 간염 치료 중인가요?\nA: 간염 치료 받은 적 없어요"
    return _hep_run(mp, _la(evidence="간염 치료 받은 적 없어요"), source=source).assessments[0]


def _n1(mp):
    return _judge(mp, RULES["SURG"], "45세 남성." + _qa(SIDE_EFFECT_Q, SIDE_EFFECT_A), "no", SIDE_EFFECT_A,
                  required="self_report_ok")[1]


def _n2(mp):
    return _judge(mp, RULES["SURG"], "45세 남성." + _qa(EXACT_Q, EXACT_A), "no", EXACT_A, required="self_report_ok")[1]


CASES = [
    Case("L1", "B-free", "test_evidence_directness::test_clause_scope_keeps_certain_clause_and_blocks_unknown_clause",
         "수술 받은 적 있나요?", "손목 수술은 했는데 TFCC인지 인대재건술인지는 몰라요.", "Prior wrist surgery (포함)",
         _l1, ("yes", True), "답이 기준(손목 수술)을 직접 진술. 판정은 옳을 가능성이 높음 -> 정상 판정 손실"),
    Case("L2", "B", "test_evidence_directness_counterexamples::test_short_answer_needs_question_target_link",
         "수술 받은 적 있나요?", "예", "Prior surgery (포함)",
         _l2, ("yes", True), "질문이 기준을 그대로 물음 -> 정상 판정 손실 (계약 승인 시 회복)"),
    Case("L3", "B", "test_evidence_directness_counterexamples::test_linked_answer_polarity_must_match (두 번째 단언)",
         "수술 받은 적 있나요?", "아니요", "Prior surgery (포함)",
         _l3, ("no", False), "질문이 기준을 그대로 물음 -> 정상 제외 손실 (계약 승인 시 회복)"),
    Case("L4", "A", "test_stage05::test_quote_check_uses_clarification_answers",
         "간염 치료 중인가요?", "간염 치료 받은 적 없어요", "Active hepatitis B or C (제외)",
         _l4, ("no", True), "치료 이력 답으로 활동성 감염 부재를 확정 -> 막는 것이 안전성 개선"),
    Case("N1", "A", "신규 (P0 반례 A)", SIDE_EFFECT_Q, SIDE_EFFECT_A, "Major surgery within 4 weeks before first dose (제외)",
         _n1, ("no", True), "수술 부작용 질문의 답으로 수술 여부를 확정 (잘못된 통과) -> 막는 것이 안전성 개선"),
    Case("N2", "B", "신규", EXACT_Q, EXACT_A, "Major surgery within 4 weeks before first dose (제외)",
         _n2, ("no", True), "기간·대상을 정확히 물음. 단 기준점(첫 투여 전 vs 답변 시점)은 IR anchor_policy 승인으로만 "
                            "같다고 봄 -> 정상 판정 손실 (계약+IR 승인 시 회복)"),
]
IDS = [c.case_id for c in CASES]


def _with_policy(mp, policy: str, case: Case) -> CriterionAssessment:
    mp.setenv(qa_guard.POLICY_ENV, policy)
    assert qa_guard.policy() == policy
    return case.run(mp)


# ------------------------------------------------------------ 1. Legacy Regression
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_legacy_policy_regression(monkeypatch, case):
    a = _with_policy(monkeypatch, "legacy", case)
    assert (a.holds, a.passes) == case.legacy
    assert qa_guard.FLAG not in a.validation_flags


def test_default_policy_is_legacy(monkeypatch):
    monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    assert qa_guard.policy() == "legacy"
    for v, want in [("legacy", "legacy"), ("contract", "contract"), ("CONTRACT ", "contract"), ("contrct", "contract")]:
        monkeypatch.setenv(qa_guard.POLICY_ENV, v)
        assert qa_guard.policy() == want  # 알 수 없는 값은 엄격한 쪽


# ------------------------------------------------------------ 2. Contract Policy Regression
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_contract_policy_regression(monkeypatch, case):
    a = _with_policy(monkeypatch, "contract", case)
    assert a.passes is None and a.holds == "unknown" and a.needs_verification
    assert qa_guard.FLAG in a.validation_flags
    assert a.self_reported_holds == case.legacy[0]  # 답변상 판정은 지우지 않고 보존 (기록·재질문·의료진 확인용)


# ------------------------------------------------------------ 3. Migration Comparison
def test_migration_comparison_table(monkeypatch):
    rows = []
    for c in CASES:
        leg = _with_policy(monkeypatch, "legacy", c)
        con = _with_policy(monkeypatch, "contract", c)
        rows.append((c.case_id, c.kind, leg.passes, con.passes))
    assert rows == [("L1", "B-free", True, None), ("L2", "B", True, None), ("L3", "B", False, None),
                    ("L4", "A", True, None), ("N1", "A", True, None), ("N2", "B", True, None)]
    # 계약 정책은 확정을 더하지 않음 (단조성): legacy가 미확정인 것을 contract가 확정하는 사례 없음
    assert all(con is None or con == leg for _, _, leg, con in rows)


def test_case_b_is_recovered_by_approved_contract(monkeypatch, patched, tmp_path):
    """사례 B(정확한 질문, 계약 없음)는 기능 제한: 같은 기준을 승인된 질문 계약 + IR로 물으면 결정적으로 확정."""
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [RULES["SURG"], RULES["PREG"]]})
    monkeypatch.setenv(qa_guard.POLICY_ENV, "contract")
    ans = _Answerer({QC_SURGERY["question"]: "수술을 받은 적 없음"})
    s = _session(ContractSession, trials, ans, **_contract_env(tmp_path))
    s.run_fixed()
    a = next(x for x in s.matches["T1"].assessments if x.rule_id == "E1")
    assert a.method == "contract" and a.passes is True and qa_guard.FLAG not in a.validation_flags


def test_case_b_without_approval_stays_unverified(monkeypatch, patched, tmp_path):
    """같은 계약이라도 승인 전이면 계약 질문을 하지 않고, 비계약 정확 질문의 답은 확정 근거가 아님."""
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": [RULES["SURG"]]})
    monkeypatch.setenv(qa_guard.POLICY_ENV, "contract")
    patched["llm_questions"] = [dict(question_id="Q1", question=EXACT_Q, answer_type="yes_no", target=["T1:E1"],
                                     purpose="eligibility", why="E1")]
    from ctrec.agents import matcher
    from test_contract_p5_session import _matcher
    ans = _Answerer({EXACT_Q: EXACT_A})
    monkeypatch.setattr(matcher.llm, "structured", lambda model, system, user, **k: _matcher(
        {"E1": ("no", EXACT_A)} if ans.seen else {})(model, system, user))
    s = _session(ContractSession, trials, ans, **_contract_env(tmp_path, approve=False))
    s.run_fixed()
    a = s.matches["T1"].assessments[0]
    assert not [q for q in ans.seen if q.question_id.startswith("C")]
    assert a.passes is None and a.self_reported_holds == "no" and s.matches["T1"].eligibility == "UNCERTAIN"
