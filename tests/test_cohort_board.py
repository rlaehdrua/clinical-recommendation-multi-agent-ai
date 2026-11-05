"""연구용 코호트 현황판의 쌍 상태 (web/cohort_board.py): 엔진 계약 0.2의 7가지 PairStatus, public만 사용 (API 호출 없음)."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.engine.contracts import PairStatus, derive_pair_status  # noqa: E402
from ctrec.web import cohort_board as cb  # noqa: E402

from test_cohort_generator import _cohorts, _web_client  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "src" / "ctrec" / "web" / "static"


def _rule(rid, slot, kind="inclusion", window=None, extra=()):
    return {"rule_id": rid, "kind": kind, "slot": slot, "extra_slots": list(extra), "time_window_days": window,
            "required_evidence": "clinical_record"}


def _cohort(rules, holds, known, sources=None):
    """손으로 만든 코호트 JSON: 환자 P001, 시험 T1."""
    return {"public": {
        "trials": ["T1"], "rules": {"T1": rules}, "unbound_rules": {"T1": []}, "enrollment_remaining": {"T1": None},
        "sources": sources or {}, "budgets_min": [30],
        "patients": [{"patient_id": "P001", "candidate_trials": ["T1"], "known": known, "unknown_slots": []}],
        "initial_holds": {f"P001|T1|{rid}": h for rid, h in holds.items()}},
        "oracle": {"hidden": {}, "truth": {}, "pair_label": {}}}


def _status(c):
    return cb.board(c)["pairs"]["P001|T1"]


def test_all_known_and_passing_is_confirmed_eligible():
    c = _cohort([_rule("I1", "age"), _rule("E1", "tx.egfr_tki", kind="exclusion")], {"I1": "yes", "E1": "no"},
                {"age": 60, "tx.egfr_tki": "no"})
    assert _status(c)["status"] == "CONFIRMED_ELIGIBLE" and _status(c)["pending_kinds"] == []


def test_one_known_failing_rule_confirms_ineligible_even_with_unknowns():
    c = _cohort([_rule("I1", "age"), _rule("I2", "lab.hemoglobin")], {"I1": "no", "I2": "unknown"}, {"age": 10})
    s = _status(c)
    assert s["status"] == "CONFIRMED_INELIGIBLE" and s["eligibility"] == "INELIGIBLE"


def test_unknown_rule_is_info_missing():
    c = _cohort([_rule("I1", "age"), _rule("I2", "lab.hemoglobin")], {"I1": "yes", "I2": "unknown"}, {"age": 60})
    s = _status(c)
    assert s["status"] == "INFO_MISSING" and [r["gap"] for r in s["rules"]] == ["none", "missing"]


def test_stale_lab_is_info_missing_with_stale_gap():
    c = _cohort([_rule("I1", "lab.hemoglobin", window=7)], {"I1": "unknown"}, {"lab.hemoglobin": 13.2, "lab_age_days": 16})
    s = _status(c)
    assert s["status"] == "INFO_MISSING" and s["rules"][0]["gap"] == "stale" and "stale" in s["pending_kinds"]
    fresh = _cohort([_rule("I1", "lab.hemoglobin", window=7)], {"I1": "unknown"}, {"lab_age_days": 3})
    assert _status(fresh)["rules"][0]["gap"] == "missing"  # 값이 없으면 오래됨이 아니라 정보 없음


@pytest.mark.parametrize("sources", [None, {"S_md": {"source_id": "S_md", "base_source": "S_md", "reveals": ["cm.x"]}},
                                     {"cr": {"source_id": "cr", "base_source": "clinician_review", "reveals": ["cm.x"]}}])
def test_clinician_judgement_rule_is_expert_review(sources):
    slot = "judge.investigator_unsuitable" if sources is None else "cm.x"
    c = _cohort([_rule("I1", "age"), _rule("E9", slot, kind="exclusion")], {"I1": "yes", "E9": "unknown"}, {"age": 60},
                sources)
    s = _status(c)
    assert s["status"] == "EXPERT_REVIEW" and s["rules"][1]["gap"] == "physician_review"


def test_missing_info_outranks_expert_review():
    c = _cohort([_rule("I1", "lab.hemoglobin"), _rule("E9", "judge.x", kind="exclusion")],
                {"I1": "unknown", "E9": "unknown"}, {})
    assert _status(c)["status"] == "INFO_MISSING"  # 계약 우선순위: 정보 없음 > 확인 필요 > 임상의 판단


def test_trial_without_any_bound_rule_is_not_confirmed():
    """연결된 기준이 0개인 시험: 전부 통과처럼 보여도 확정 근거가 없으므로 NOT_EVALUATED (엔진 #14와 같은 규칙)."""
    s = _status(_cohort([], {}, {"age": 60}))
    assert s["eligibility"] == "ELIGIBLE" and s["status"] == "NOT_EVALUATED" and s["pending_kinds"] == []


def test_board_uses_engine_contract_and_only_public_data():
    for c in _cohorts():
        data = json.loads(json.dumps(c.model_dump(mode="json")))
        b = cb.board(data)
        assert b["status_order"] == [s.value for s in PairStatus]
        assert sum(b["status_counts"].values()) == len(b["pairs"]) == sum(len(p["candidate_trials"])
                                                                          for p in data["public"]["patients"])
        for v in b["pairs"].values():
            gaps = {r["gap"] for r in v["rules"]} - {"none"}
            expect = derive_pair_status(v["eligibility"], gaps, "confirmed" if v["eligibility"] == "INELIGIBLE" else None,
                                        required_criteria=len(v["rules"]))
            assert v["status"] == expect.value
        garbled = copy.deepcopy(data)
        garbled["oracle"] = {"hidden": {}, "truth": {}, "pair_label": {}}
        assert cb.board(garbled) == b  # 정답을 지워도 상태가 같음 = public만 사용


def test_web_cohort_api_returns_board(monkeypatch, tmp_path):
    cohorts = _cohorts()
    client = _web_client(monkeypatch, tmp_path, cohorts)
    one = client.get(f"/api/cohorts/{cohorts[0].cohort_id}").json()
    assert set(one["board"]["status_counts"]) == {s.value for s in PairStatus}
    assert one["board"]["contract_version"].startswith("dcvc.engine.contracts/")
    assert set(one["board"]["pairs"]) == {f"{p['patient_id']}|{t}" for p in one["public"]["patients"]
                                          for t in p["candidate_trials"]}


def test_board_page_has_no_own_judgement_rule_and_labels_all_statuses():
    js = (STATIC / "cohort.js").read_text(encoding="utf-8")
    assert "c.board.pairs" in js and "status_counts" in js
    for s in PairStatus:
        assert f"{s.value}:" in js  # STATUS_UI에 7가지 모두
    assert "미확정 · " not in js  # 예전 3상태 라벨 제거
