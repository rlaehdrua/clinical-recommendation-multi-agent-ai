"""평가 충실도 (Phase 4-3): 평가 경로의 정책이 실제 Decision Loop를 그대로 쓰고 동점 순서만 바꾸는지. 오프라인."""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bench_incremental import load_frozen  # noqa: E402
from build_validation_set import SUPPLEMENT  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402
from eval_question_fidelity import VALIDATION, _canon_ids, compare  # noqa: E402

from ctrec import decision_loop, question_eval as qe, question_priority as qp, store_patch  # noqa: E402
from ctrec.decision_state import build_state, SessionView  # noqa: E402

PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}
STALE_CASE = "V-NCT07040306-I2-prov-NCT06125405"  # Phase 4-2 방식이 오래된 판정으로 질문한 사례
CONFIRMED_CASE = "S-NCT06727604-E4-confirmed-NCT07516951"


def _case(path, cid):
    return next((c, g) for c, g in qe.load_cases(PARSED, path, only=[cid]))


def _run(path, cid, **kw):
    case, gt = _case(path, cid)
    with qe.OfflineEnvironment(3):
        return qe.run_method(kw.pop("method", "candidate"), case, gt, TRIALS, 2, 3, **kw)


@pytest.mark.parametrize("path, cid", [(VALIDATION, STALE_CASE), (qe.DEFAULT_CASES, "M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1"),
                                       (SUPPLEMENT, CONFIRMED_CASE)])
def test_policy_a_path_equals_real_loop(path, cid):
    plain = _run(path, cid)
    real = _run(path, cid, record_candidates=True)
    a = _run(path, cid, method="A_current", tie_policy="A_current", record_candidates=True)
    assert compare(plain, real) == []  # 기록용 포장은 동작을 바꾸지 않음
    assert compare(real, a) == []  # 후보·단계·점수·제외 사유·선택 순서·예산·종료·사실 버전·질문 모두 같음
    assert real.candidate_log and real.candidate_log == a.candidate_log


def test_stale_matcher_judgment_blocked_in_real_path():
    real = _run(VALIDATION, STALE_CASE)
    legacy = _run(VALIDATION, STALE_CASE, method="A_legacy",
                  factories={"A_legacy": qp.generator_factory("A_current")})
    stale = {k for d in real.diagnostics for a in d.get("actions", [])
             if a["reason_code"] == "llm_reevaluation_required" for k in a["target_criterion_ids"]}
    assert stale  # 사실이 바뀐 뒤 다시 매칭되지 않은 시험의 LLM 판정은 재판정 필요로 보류
    real_targets = {k for q in real.questions for k in q["target_criterion_ids"]}
    legacy_targets = {k for q in legacy.questions for k in q["target_criterion_ids"]}
    assert not real_targets & stale and legacy_targets & stale  # 예전 평가기는 오래된 판정으로 질문했음


def test_fact_version_changes_between_rounds_and_tracker_keeps_old_snapshot():
    real = _run(VALIDATION, STALE_CASE)
    versions = [d["state"]["fact_version"] for d in real.diagnostics]
    assert len(versions) >= 2 and len(set(versions)) >= 2  # 답변 뒤 재매칭 시점의 사실 버전이 바뀜
    assert real.diagnostics[-1]["stats"]["llm_stale"] > 0


def test_fingerprint_duplicates_budget_and_round1_termination_same_across_policies():
    case, gt = _case(VALIDATION, STALE_CASE)
    runs = {}
    with qe.OfflineEnvironment(3):
        for p in qp.POLICIES:
            runs[p] = qe.run_method(p, case, gt, TRIALS, 2, 3, tie_policy=p, record_candidates=True)
    per_round = defaultdict(dict)
    for p, r in runs.items():
        keys = [k for q in r.questions for k in q["target_criterion_ids"]]
        assert len(keys) == len(set(keys))  # 중복 질문 없음
        for q in r.questions:
            per_round[q["round_id"]].setdefault(p, 0)
            per_round[q["round_id"]][p] += 1
        assert max(per_round[1].values()) <= 3
    assert len(set(per_round[1].values())) == 1  # 라운드 1: 모든 정책이 같은 수의 질문 (예산 동일)
    r1 = {p: r.diagnostics[0]["stop_reason"] for p, r in runs.items()}
    assert len(set(r1.values())) == 1  # 라운드 1 종료 사유 동일


def test_only_tie_order_changes():
    """라운드 1 첫 후보 목록: 정책마다 같은 후보 집합, 같은 우선순위 묶음, 묶음 안 순서만 다름."""
    case, gt = _case(qe.DEFAULT_CASES, "M-SYN-S003-01-NCT06125405-rescue-1")
    logs = {}
    with qe.OfflineEnvironment(3):
        for p in qp.POLICIES:
            logs[p] = qe.run_method(p, case, gt, TRIALS, 1, 3, tie_policy=p, record_candidates=True).candidate_log[0]
    base = logs["A_current"]
    for p, log in logs.items():
        assert sorted(log) == sorted(base), p  # 같은 후보·단계·점수·우선순위
        assert [x[3] for x in log] == [x[3] for x in base], p  # 우선순위 순서(묶음 경계) 동일
    assert logs["B_protocol"] != base  # 동점 순서는 실제로 바뀜


def test_kind_balance_uses_loop_history():
    from types import SimpleNamespace
    from ctrec.decision_state import Action

    def act(rid):
        return Action(action_id=rid, action_type="ASK_USER", reason_code="r", human_readable_reason="r",
                      target_criterion_ids=[f"T:{rid}"], priority=2.2, tier=2, value_score=4, info_key=f"k{rid}")
    p = SimpleNamespace(rules=[SimpleNamespace(rule_id=r, kind="inclusion" if r[0] == "I" else "exclusion",
                                               category="other", field=None, structured_evaluable=False, operator=None)
                               for r in ("I1", "I2", "E1", "E2")], rule_meta={})
    s = SimpleNamespace(case=SimpleNamespace(candidate_trials=["T"]), trials={"T": {}}, parsed={"T": p})
    group = [act(r) for r in ("I1", "I2", "E1", "E2")]
    assert qp.reorder_ties(group, s, "D_kind_balance", history=[])[0].action_id == "I1"
    assert qp.reorder_ties(group[1:], s, "D_kind_balance", history=[group[0]])[0].action_id == "E1"  # 선정을 물었으니 제외


def test_confirmed_setup_has_no_false_safety_alarm_and_no_wasted_questions():
    real = _run(SUPPLEMENT, CONFIRMED_CASE)
    assert real.safety["document_verified_facts"] == 0  # 평가용 상태 구성 항목은 승격으로 세지 않음
    assert not any(q["relevance"].get("confirmed_mismatch_only") for q in real.questions if q["round_id"] == 1)


def test_tie_order_restores_real_function_and_ids_canonicalized():
    real_fn = decision_loop.candidates
    with qp.tie_order("B_protocol", {"session": None}):
        assert decision_loop.candidates is not real_fn
    assert decision_loop.candidates is real_fn
    assert _canon_ids('["F-0123456789ab", "F-ba9876543210", "F-0123456789ab"]') == '["F#1", "F#2", "F#1"]'


def test_no_ground_truth_in_policy_inputs(monkeypatch):
    case, gt = _case(VALIDATION, STALE_CASE)
    seen = []
    real = qp.reorder_ties

    def spy(cands, session, policy, history=None):
        if not session.qa_log:  # 답변 시뮬레이터가 아무것도 공개하기 전의 정책 입력
            seen.append(any(t in session.case.raw_text for _, t in gt._hidden_texts) or bool(session.case.oracle_facts))
        return real(cands, session, policy, history)
    monkeypatch.setattr(qp, "reorder_ties", spy)
    with qe.OfflineEnvironment(3):
        qe.run_method("B_protocol", case, gt, TRIALS, 1, 3, tie_policy="B_protocol")
    assert seen and not any(seen)


def test_frozen_and_matcher_unchanged():
    case, gt = _case(VALIDATION, STALE_CASE)
    with qe.OfflineEnvironment(3):
        a = qe.run_method("candidate", case, gt, TRIALS, 2, 3)
        b = qe.run_method("B_protocol", case, gt, TRIALS, 2, 3, tie_policy="B_protocol")
    assert a.round0_matches == b.round0_matches  # 라운드 1 Matcher 판정 동일
    assert store_patch.verify_frozen() == []


def test_supplement_set_reproducible_and_flagged():
    import json

    import build_validation_set as bvs
    cases, summary = bvs.build_supplement()
    assert cases == json.loads(SUPPLEMENT.read_text(encoding="utf-8"))
    assert all(c["synthetic"] and c["clinical_validity"] == "unverified_synthetic" and c["split"] == "supplement"
               for c in cases)
    assert summary["reused_gt"] == 0 and {"cohort", "confirmed"} <= set(summary["case_types"])
    assert all(c.get("setup_verified_fields") == ["age"] for c in cases if c["case_type"] == "confirmed")
