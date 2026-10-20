"""질문 우선순위 동점 처리 정책 A–E와 검증셋 (Phase 4-2, 평가 전용). 오프라인, API 호출 없음."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

import build_validation_set as bvs  # noqa: E402
from ctrec import question_eval as qe, question_priority as qp, store_patch  # noqa: E402
from ctrec.decision_state import Action  # noqa: E402

PARSED = load_frozen()
ALL_TRIALS = load_trials()
TRIALS = {t: ALL_TRIALS[t] for t in PARSED}


def _act(key, tier=2, score=4.0, info=None):
    return Action(action_id=f"A-{key}", action_type="ASK_USER", reason_code="missing_required_info",
                  human_readable_reason="r", target_criterion_ids=[key], priority=tier + 0.03 + 1 / (2 + score),
                  tier=tier, value_score=score, info_key=info or f"criterion:{key}")


def _session(tid="T", rules=("I1", "I2", "I10", "E1", "E2", "E10")):
    p = SimpleNamespace(rules=[SimpleNamespace(rule_id=r, kind="inclusion" if r[0] == "I" else "exclusion",
                                               category="other", field=None, structured_evaluable=False, operator=None)
                               for r in rules], rule_meta={})
    return SimpleNamespace(case=SimpleNamespace(candidate_trials=[tid]), trials={tid: {}}, parsed={tid: p})


def _ids(order):
    return [a.target_criterion_ids[0].split(":")[1] for a in order]


TIED = [_act(f"T:{r}") for r in ("I1", "I2", "I10", "E1", "E2", "E10")]


# ------------------------------------------------------------ 정책별 동점 처리
def test_current_policy_reproduces_string_sort_problem():
    assert _ids(qp.order_asks(TIED, _session(), "A_current")) == ["E1", "E10", "E2", "I1", "I10", "I2"]


def test_natural_order_fixes_numbers_but_not_kind():
    assert _ids(qp.order_asks(TIED, _session(), "C_natural")) == ["E1", "E2", "E10", "I1", "I2", "I10"]


def test_protocol_order_preserved():
    s = _session(rules=("I10", "I1", "E2", "I2", "E1", "E10"))  # 기준표 순서 그대로
    assert _ids(qp.order_asks(TIED, s, "B_protocol")) == ["I10", "I1", "E2", "I2", "E1", "E10"]


def test_kind_balance_alternates():
    assert _ids(qp.order_asks(TIED, _session(), "D_kind_balance")) == ["I1", "E1", "I2", "E2", "I10", "E10"]


def test_ties_only_tier_and_score_preserved():
    mixed = [_act("T:E1", tier=6, score=9), _act("T:I2", tier=2, score=1), _act("T:I1", tier=2, score=5),
             _act("T:E2", tier=1, score=0)]
    for p in qp.POLICIES:
        assert _ids(qp.order_asks(mixed, _session(), p)) == ["E2", "I1", "I2", "E1"], p  # 단계 -> 점수는 모든 정책이 같음


def test_info_value_policy_uses_predefined_factors():
    s = _session(rules=("I1", "I2", "I3"))
    s.parsed["T"].rules[1].category = "performance_status"  # 부담 낮음
    s.parsed["T"].rules[2].field, s.parsed["T"].rules[2].structured_evaluable, s.parsed["T"].rules[2].operator = \
        "ecog", True, "<="  # 구조화 기준
    acts = [_act(f"T:{r}") for r in ("I1", "I2", "I3")]
    assert _ids(qp.order_asks(acts, s, "E_info_value")) == ["I3", "I2", "I1"]


def test_policies_are_deterministic():
    for p in qp.POLICIES:
        assert [a.action_id for a in qp.order_asks(list(TIED), _session(), p)] == \
               [a.action_id for a in qp.order_asks(list(reversed(TIED)), _session(), p)], p  # 입력 순서와 무관


# ------------------------------------------------------------ 검증셋
def test_validation_set_separated_and_reproducible():
    cases, summary = bvs.build()
    again, _ = bvs.build()
    assert cases == again  # 같은 seed -> 같은 검증셋
    dev = qe.load_cases(PARSED)
    dev_ids = {g.case_id for _, g in dev}
    dev_gt = {k for _, g in dev for k in g.exact_keys() | g.inferred_keys()}
    assert all(c["split"] == "validation" and c["synthetic"] for c in cases)
    assert not dev_ids & {c["patient_id"] for c in cases}
    assert summary["dev_gt_reused"] == 0 and not dev_gt & {m["rule_key"] for c in cases for m in c["target_meta"]}
    for c in cases:
        assert all(h["text"] not in c["text"] for h in c["hidden_facts"])
    kinds = summary["target_kinds"]
    assert abs(kinds["inclusion"] - kinds["exclusion"]) <= 10  # 선정·제외 균형
    assert {"single", "cohort", "shared", "provisional"} <= set(summary["case_types"])
    stored = json.loads((bvs.OUT / "validation_cases.json").read_text(encoding="utf-8"))
    assert stored == cases  # 저장된 검증셋이 생성기와 같음


def test_validation_masked_criteria_map_exactly():
    cases = qe.load_cases(PARSED, bvs.OUT / "validation_cases.json")
    assert {t.status for _, g in cases for t in g.targets} == {"EXACT"}


# ------------------------------------------------------------ 평가: 경로 보존·누출·불변
SUBSET_VAL = None


@pytest.fixture(scope="module")
def val_subset():
    cases = qe.load_cases(PARSED, bvs.OUT / "validation_cases.json")
    pick = ([g.case_id for _, g in cases if g.case_type == "cohort"][:2]
            + [g.case_id for _, g in cases if g.case_type == "provisional"][:3])
    factories = {p: qp.generator_factory(p) for p in qp.POLICIES}
    return pick, qe.run_evaluation(PARSED, TRIALS, only=pick, rounds=2, budget=3,
                                   cases_path=bvs.OUT / "validation_cases.json",
                                   methods=("candidate",) + qp.POLICIES, factories=factories)


def test_policies_keep_paths_and_safety(val_subset):
    _, r = val_subset
    assert r["round1_matcher_identical"]
    for m, a in r["methods"].items():
        assert a["failed"] == 0 and set(a["safety"].values()) <= {0}, m
        assert a["irrelevant_rate"]["numerator"] == 0  # 열린 코호트 경로의 질문은 불필요로 세지 않음
    groups = {}
    for q in r["question_records"]:
        g = groups.setdefault((q["case_id"], q["method"], q["round_id"]), {"rel": [], "prov": []})
        if q["relevance"].get("relevant"):
            g["rel"].append(q["priority"])
        elif q["relevance"].get("provisional_mismatch"):
            g["prov"].append(q["priority"])
    for g in groups.values():  # 잠정 불충족 시험 질문이 관련 질문보다 앞서지 않음 (예산 3에서는 대개 나오지도 않음)
        if g["rel"] and g["prov"]:
            assert max(g["rel"]) < min(g["prov"])


def test_provisional_questions_kept_but_after_relevant():
    """예산을 크게 주면 잠정 불충족 시험의 질문도 지워지지 않고 남되, 모든 정책에서 관련 질문 뒤에 옴."""
    cases = qe.load_cases(PARSED, bvs.OUT / "validation_cases.json")
    pick = [g.case_id for _, g in cases if g.case_type == "provisional"][:2]
    factories = {p: qp.generator_factory(p) for p in qp.POLICIES}
    r = qe.run_evaluation(PARSED, TRIALS, only=pick, rounds=1, budget=40, cases_path=bvs.OUT / "validation_cases.json",
                          methods=qp.POLICIES, factories=factories)
    for m in qp.POLICIES:
        qs = [q for q in r["question_records"] if q["method"] == m]
        rel = [q["priority"] for q in qs if q["relevance"].get("relevant")]
        prov = [q["priority"] for q in qs if q["relevance"].get("provisional_mismatch")]
        assert rel and prov, m  # 잠정 불충족은 지워지지 않음 (확정 제외 아님)
        for cid in pick:
            rel_c = [q["priority"] for q in qs if q["case_id"] == cid and q["relevance"].get("relevant")]
            prov_c = [q["priority"] for q in qs if q["case_id"] == cid and q["relevance"].get("provisional_mismatch")]
            if rel_c and prov_c:
                assert max(rel_c) < min(prov_c), m


def test_policies_never_see_ground_truth(monkeypatch, val_subset):
    pick, _ = val_subset
    cases = {g.case_id: g for _, g in qe.load_cases(PARSED, bvs.OUT / "validation_cases.json", only=pick)}
    seen = []
    real = qp.order_asks

    def spy(asks, session, policy):
        blob = session.case.raw_text
        g = cases[session.case.patient_id]
        seen.append(any(t in blob for _, t in g._hidden_texts))
        assert not hasattr(session.case, "oracle_facts") or session.case.oracle_facts is None
        return real(asks, session, policy)
    monkeypatch.setattr(qp, "order_asks", spy)
    factories = {p: qp.generator_factory(p) for p in qp.POLICIES}
    qe.run_evaluation(PARSED, TRIALS, only=pick[:2], rounds=1, budget=3,
                      cases_path=bvs.OUT / "validation_cases.json", methods=qp.POLICIES, factories=factories)
    assert seen and not any(seen)  # 라운드 1 정책 입력에는 숨긴 사실이 없음


def test_frozen_store_unchanged():
    assert store_patch.verify_frozen() == []
