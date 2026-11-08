"""현황판 EXPERT_REVIEW와 미연결 기준 (docs/proposals/decision5_evidence.md '확인 필요 (현황판 담당)', 선택지 (b)).

재현 (1f04229): 결정 5 뒤 기본 데이터의 EXPERT_REVIEW 쌍은 모두 미연결(검수 전) 기준이 남은 NCT06530004·NCT06125405의
쌍인데, 상태별 총계(EXPERT_REVIEW)만 보면 '판단 기준 외 확인 완료'로 읽힘. 쌍마다 unbound_rules 수는 있지만 총계에서
'미연결 남음'과 '판단 기준만 남음'을 나눌 수 없었음.

계약 (b, 판정 불변): 상태는 그대로, 쌍마다 unbound_remaining(미연결 기준이 남았거나 목록을 모름), 현황판에
expert_review_split {"unbound_remaining", "expert_only"} (합 = EXPERT_REVIEW 수). 화면은 총계를 나눠 보여 줌.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.web import cohort_board as cb  # noqa: E402

from test_cohort_board import STATIC, _cohort, _default_cohorts, _rule  # noqa: E402


def _expert_pair(unbound):
    return _cohort([_rule("I1", "age"), _rule("E9", "judge.x", kind="exclusion")], {"I1": "yes", "E9": "unknown"},
                   {"age": 60}, unbound=unbound)


def test_pair_and_board_split_expert_review_by_unbound():
    with_ub, without = cb.board(_expert_pair(["E3"])), cb.board(_expert_pair([]))
    for b, remaining in ((with_ub, True), (without, False)):
        p = b["pairs"]["P001|T1"]
        assert p["status"] == "EXPERT_REVIEW"  # 상태는 그대로 (선택지 b)
        assert p["unbound_remaining"] is remaining
        assert b["expert_review_split"] == {"unbound_remaining": int(remaining), "expert_only": int(not remaining)}
    unknown = _expert_pair([])
    del unknown["public"]["unbound_rules"]["T1"]  # 목록을 모르면 전체 기준을 봤다고 할 수 없음
    assert cb.board(unknown)["pairs"]["P001|T1"]["unbound_remaining"] is True
    other = cb.board(_cohort([_rule("I1", "age")], {"I1": "yes"}, {"age": 60}, unbound=["E9"]))
    assert other["expert_review_split"] == {"unbound_remaining": 0, "expert_only": 0}  # EXPERT_REVIEW 쌍만 셈


def test_default_data_expert_review_is_all_unbound_remaining():
    total = {"er": 0, "ub": 0}
    for d in _default_cohorts():
        before = copy.deepcopy(d)
        b = cb.board(d)
        split = b["expert_review_split"]
        assert split["unbound_remaining"] + split["expert_only"] == b["status_counts"]["EXPERT_REVIEW"]
        for k, v in b["pairs"].items():
            t = k.split("|")[1]
            assert v["unbound_remaining"] == bool(d["public"]["unbound_rules"].get(t, None) is None
                                                  or d["public"]["unbound_rules"][t])
        assert d == before
        total["er"] += b["status_counts"]["EXPERT_REVIEW"]
        total["ub"] += split["unbound_remaining"]
    assert total["er"] > 0 and total["ub"] == total["er"]  # decision5_evidence.md: 183쌍 모두 미연결 남음


def test_board_page_shows_the_split():
    js = (STATIC / "cohort.js").read_text(encoding="utf-8")
    assert "expert_review_split" in js and "미연결 남음" in js and "판단 기준만 남음" in js
    assert "unbound_remaining" in js
