"""소규모 유료 평가 준비 (Phase 4-4, dry-run 전용): 표본 구성·비용 상한·실제 호출 차단. API 호출 없음."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prepare_paid_pilot as pp  # noqa: E402
from ctrec import question_eval as qe  # noqa: E402


@pytest.fixture(scope="module")
def plan():
    return pp.plan()


def test_sample_balanced_and_covers_types(plan):
    assert plan["target_kinds"]["inclusion"] == plan["target_kinds"]["exclusion"]
    assert {"cohort", "biomarker", "confirmed", "shared"} <= set(plan["case_types"])
    assert len({c["case_id"] for c in plan["cases"]}) == plan["n_cases"]
    assert plan["cases"] == pp.plan()["cases"]  # 결정적 선택


def test_cost_estimate_marked_as_assumption_and_capped(plan):
    est, upper = plan["estimate"], plan["upper_bound_all_stages"]
    assert est["prices_confirmed"] is False and est["tokens_confirmed"] is False
    assert est["calls_per_case_per_method"]["question_generator"] == 0 and est["estimated_usd"] < upper["estimated_usd"]
    assert est["within_cap"] is True
    assert qe.estimate_cost(plan["n_cases"], 1.2, 2, cap_usd=0.5)["within_cap"] is False


def test_paid_execution_blocked(plan):
    assert plan["real_llm_blocked_offline"] is True
    assert plan["paid_gate"].startswith("closed")
