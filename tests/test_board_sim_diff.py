"""scripts/board_sim_diff.py: 현황판-시뮬레이터 차이 추출의 정의·결정성 (합성 코호트, LLM 호출 없음).

전체 건수(170·43)는 판정 코드가 바뀌면 달라지는 값이므로 여기서 고정하지 않습니다.
저장된 자료와의 비교는 `python scripts/board_sim_diff.py --check`로 합니다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ctrec.agents import matcher

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "board_sim_diff.py"
spec = importlib.util.spec_from_file_location("board_sim_diff", SCRIPT)
bsd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bsd)


@pytest.fixture(scope="module")
def res():
    return bsd.compute(limit=2)


def test_groups_follow_their_status_definition(res):
    assert {r["group"] for r in res["pairs"]} == {bsd.BOARD_CI_SIM_IM, bsd.BOARD_IM_SIM_CI}
    for r in res["pairs"]:
        assert bsd.GROUPS[(r["board_status"], r["sim_truth_status"])] == r["group"]


def test_cross_tab_accounts_for_every_pair_and_group_counts_match(res):
    groups = {g: sum(v for k, v in res["cross_tab"].items() if (k[0], k[1]) in bsd.GROUPS and bsd.GROUPS[(k[0], k[1])] == g)
              for g in bsd.GROUPS.values()}
    assert groups == {g: sum(r["group"] == g for r in res["pairs"]) for g in groups}
    assert sum(res["cross_tab"].values()) >= len(res["pairs"])


def test_every_pair_has_a_rule_that_drives_the_difference(res):
    for r in res["pairs"]:
        rows = [c for c in res["criteria"] if c["pair_id"] == r["pair_id"]]
        driving = [c for c in rows if c["drives_status"]]
        assert driving and r["pair_mechanism"], r["pair_id"]
        if r["group"] == bsd.BOARD_CI_SIM_IM:
            assert all(c["board_passes"] is False and c["sim_passes"] is not False for c in driving)
        else:
            assert all(c["sim_passes"] is False and c["board_passes"] is not False for c in driving)


def test_mechanism_is_blank_unless_the_row_differs_and_never_names_a_verdict(res):
    for c in res["criteria"]:
        assert bool(c["mechanism"]) == bool(c["differs"])
        assert not any(w in c["mechanism"].lower() for w in ("wrong", "false_", "misjudg", "error"))


def test_same_input_gives_the_same_bytes(res):
    assert bsd.render(res) == bsd.render(bsd.compute(limit=2))


def test_check_ignores_crlf_from_git_but_not_content(tmp_path):
    f = tmp_path / "a.csv"
    f.write_bytes(b"a,b\r\n1,2\r\n")
    assert bsd.same_file(f, "a,b\n1,2\n")
    assert not bsd.same_file(f, "a,b\n1,3\n")
    assert not bsd.same_file(tmp_path / "missing.csv", "")


def test_recording_leaves_the_matcher_untouched(res):
    assert matcher.match_trial.__name__ == "match_trial"


def test_recorder_restores_the_matcher_after_an_error():
    original = matcher.match_trial
    with pytest.raises(RuntimeError):
        with bsd.recording_matches({}, [None]):
            raise RuntimeError
    assert matcher.match_trial is original
