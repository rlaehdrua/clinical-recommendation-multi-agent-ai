"""한 쌍의 네 층이 서로 맞는가: Matcher 판정 · 엔진 PairStatus · Session 추천 · 정책 검토 행동 (합성 시험, LLM·API 없음).

합성 시험 R1(파싱 결과 빔)·R3(원문에 기준 없음)·R5(절차 기준만)·R6(절차 제외 기준 미확인)·R7(임상 기준 충족, 절차 미확인)은
tests/test_empty_criteria.py(E-1)의 대역을 그대로 씁니다.

일치 규칙 (불일치 코드)
- confirmed_not_match: PairStatus가 CONFIRMED_ELIGIBLE인데 외부 상태가 CRITERIA_MATCH가 아님
- match_not_confirmed: 외부 상태가 CRITERIA_MATCH인데 PairStatus가 확정 적격이 아님 (R5 - 알려진 불일치, 팀 결정 대기)
- ranked_closed: 추천 목록에 있는데 PairStatus가 확정 부적격·평가 안 됨
- unreviewed: PairStatus가 NOT_EVALUATED인 모집 중 쌍에 담당자 검토 행동이 없음
- no_action: 열린 쌍(정보 없음·확인 대기·임상의 판단)에 아무 행동도 없음
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_empty_criteria import (ALIAS, AGE_RULE, CONSENT, _llm_restored, cohort_of, env, parsed, rule,  # noqa: E402,F401
                                 run_session, store, trial)

from ctrec import status as status_mod, trial_store  # noqa: E402
from ctrec.agents import parser_validator as pv  # noqa: E402
from ctrec.engine import contracts as C  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy  # noqa: E402

OPEN = (C.PairStatus.INFO_MISSING, C.PairStatus.VERIFICATION_PENDING, C.PairStatus.EXPERT_REVIEW)


def layers(s, tid):
    co = cohort_of(s)
    pair = co.pair(ALIAS, tid)
    acts = [a for a in LegacyAdapterPolicy().propose(co) if tid in a.target_trial_ids]
    return {"eligibility": s.matches[tid].eligibility, "external": status_mod.external_status(s.matches[tid]),
            "status": pair.status, "open": pair.open, "ranked": tid in [r.trial_id for r in s.recommendation.ranked],
            "reviews": sorted(a.reason_code for a in acts if a.action_type == "CLINICIAN_REVIEW"), "actions": acts}


def mismatches(x) -> set[str]:
    out = set()
    if x["status"] == C.PairStatus.CONFIRMED_ELIGIBLE and x["external"] != "CRITERIA_MATCH":
        out.add("confirmed_not_match")
    if x["external"] == "CRITERIA_MATCH" and x["status"] != C.PairStatus.CONFIRMED_ELIGIBLE:
        out.add("match_not_confirmed")
    if x["ranked"] and x["status"] in (C.PairStatus.CONFIRMED_INELIGIBLE, C.PairStatus.NOT_EVALUATED):
        out.add("ranked_closed")
    if x["status"] == C.PairStatus.NOT_EVALUATED and x["open"] and not x["reviews"]:
        out.add("unreviewed")
    if x["status"] in OPEN and x["open"] and not x["actions"]:
        out.add("no_action")
    return out


def _session(tid, rules=None, criteria=None):
    t = trial(tid) if criteria is None else trial(tid, criteria=criteria)
    if rules is None:
        store(t, parsed(tid))  # 파서가 빈 규칙을 돌려준 경우와 같은 저장 경로
    else:
        trial_store.save(t, parsed(tid, rules), {}, pv.ValidationReport())
    return run_session(t)


def test_r1_empty_parse_all_layers_agree(env):
    x = layers(_session("SYN-R1"), "SYN-R1")
    assert (x["eligibility"], x["external"], x["status"], x["ranked"]) == (
        "NOT_EVALUATED", "NOT_EVALUATED", C.PairStatus.NOT_EVALUATED, False)
    assert x["reviews"] == ["evaluation_failed"]  # 기존 검토 한 건만 (no_required_criteria와 중복되지 않음)
    assert mismatches(x) == set()


def test_r3_no_source_criteria_all_layers_agree(env):
    x = layers(_session("SYN-R3", criteria=""), "SYN-R3")
    assert x["status"] == C.PairStatus.NOT_EVALUATED and not x["ranked"] and x["reviews"] == ["evaluation_failed"]
    assert mismatches(x) == set()


def test_r7_confirmed_eligible_all_layers_agree(env):
    x = layers(_session("SYN-R7", [AGE_RULE, CONSENT]), "SYN-R7")
    assert (x["external"], x["status"], x["ranked"]) == ("CRITERIA_MATCH", C.PairStatus.CONFIRMED_ELIGIBLE, True)
    assert x["reviews"] == [] and mismatches(x) == set()


def test_r6_logistics_exclusion_unknown_has_an_action(env):
    x = layers(_session("SYN-R6", [rule("E1", "Participating in another interventional trial", kind="exclusion")]),
               "SYN-R6")
    assert x["eligibility"] == "UNCERTAIN" and x["status"] != C.PairStatus.CONFIRMED_ELIGIBLE
    assert mismatches(x) == set()


def test_r5_known_mismatch_is_reviewed(env):
    """R5 (절차 기준만): Session은 CRITERIA_MATCH·추천 1순위, 엔진은 NOT_EVALUATED - 팀 결정(P1/P2/P3) 전까지 남은 불일치.

    엔진 쪽은 담당자 검토(no_required_criteria)를 내므로 확인 없이 방치되지는 않음. 결정이 나면 기대값을 바꿉니다."""
    x = layers(_session("SYN-R5", [CONSENT]), "SYN-R5")
    assert (x["external"], x["status"], x["ranked"]) == ("CRITERIA_MATCH", C.PairStatus.NOT_EVALUATED, True)
    assert x["reviews"] == ["no_required_criteria"]
    assert mismatches(x) == {"match_not_confirmed", "ranked_closed"}


@pytest.mark.parametrize("bad, code", [
    ({"status": C.PairStatus.CONFIRMED_ELIGIBLE, "external": "INSUFFICIENT_INFORMATION"}, "confirmed_not_match"),
    ({"status": C.PairStatus.NOT_EVALUATED, "reviews": []}, "unreviewed"),
    ({"status": C.PairStatus.INFO_MISSING, "external": "INSUFFICIENT_INFORMATION", "actions": []}, "no_action"),
    ({"status": C.PairStatus.CONFIRMED_INELIGIBLE, "external": "CRITERIA_MISMATCH", "ranked": True}, "ranked_closed"),
])
def test_mismatch_rules_detect_each_case(bad, code):
    base = {"eligibility": "ELIGIBLE", "external": "CRITERIA_MATCH", "status": C.PairStatus.CONFIRMED_ELIGIBLE,
            "open": True, "ranked": False, "reviews": ["x"], "actions": [object()]}
    assert code in mismatches({**base, **bad})


def test_display_separates_no_criteria_from_evaluation_failure(env):
    """기준이 없어서 판정하지 못한 시험은 '평가 실패'(처리 오류)와 다른 문구. 판정 값·원인 코드는 그대로."""
    from ctrec.web import display
    out = display.redact_errors(_session("SYN-R3", criteria="").result())
    m, ex = out["matches"]["SYN-R3"], out["recommendation"]["excluded"][0]
    assert m["eligibility"] == "NOT_EVALUATED" and m["evaluation_error"] == "no_source_criteria"
    assert m["summary"].startswith("NOT_EVALUATED: 시험에 평가할 기준이 없어 판정하지 못함(부적격 아님)")
    assert "평가 실패" not in m["summary"] and "평가 실패" not in ex["reason"] and "부적격 아님" in ex["reason"]
    failed = {"eligibility": "NOT_EVALUATED", "evaluation_error": "적격성 판정 실패: RuntimeError('x')"}
    assert "평가 실패로" in display.redact_errors({"matches": {"T": failed}})["matches"]["T"]["summary"]
