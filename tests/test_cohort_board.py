"""연구용 코호트 현황판의 쌍 상태 (web/cohort_board.py): 엔진 계약 0.2의 7가지 PairStatus, public만 사용 (API 호출 없음)."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config  # noqa: E402
from ctrec.cohort import generate as gen  # noqa: E402
from ctrec.engine.contracts import PairStatus, derive_pair_status  # noqa: E402
from ctrec.web import cohort_board as cb  # noqa: E402

from test_cohort_generator import _cohorts, _web_client  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "src" / "ctrec" / "web" / "static"


def _rule(rid, slot, kind="inclusion", window=None, extra=(), cohort=None):
    r = {"rule_id": rid, "kind": kind, "slot": slot, "extra_slots": list(extra), "time_window_days": window,
         "required_evidence": "clinical_record"}
    return {**r, "cohort": cohort} if cohort else r


def _cohort(rules, holds, known, sources=None, unbound=(), expert=None, paths=None):
    """손으로 만든 코호트 JSON: 환자 P001, 시험 T1."""
    extra = {} if expert is None else {"expert_rules": {"T1": list(expert)}}
    extra |= {} if paths is None else {"paths": {"T1": list(paths)}}
    return {"public": {
        "trials": ["T1"], "rules": {"T1": rules}, "unbound_rules": {"T1": list(unbound)}, "enrollment_remaining": {"T1": None},
        "sources": sources or {}, "budgets_min": [30], **extra,
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


# ---------------------------------------------------------------------------
# 이슈 #23: 미연결(검수 전) 기준이 남은 시험은 적격 확정 금지
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rules, holds, known", [
    ([_rule("I1", "age")], {"I1": "yes"}, {"age": 60}),  # 선정 기준만 통과
    ([_rule("E1", "tx.egfr_tki", kind="exclusion")], {"E1": "no"}, {"tx.egfr_tki": "no"}),  # 제외 기준만 통과
    ([_rule("I1", "age"), _rule("E1", "tx.egfr_tki", kind="exclusion")], {"I1": "yes", "E1": "no"},
     {"age": 60, "tx.egfr_tki": "no"}),
])
def test_unbound_rule_blocks_confirmed_eligible(rules, holds, known):
    """연결된 기준이 모두 통과해도 미연결 기준이 있으면 확정 아님 -> NOT_EVALUATED (충족 간주 금지)."""
    s = _status(_cohort(rules, holds, known, unbound=["I9", "E9"]))
    assert s["status"] == "NOT_EVALUATED" and s["eligibility"] == "NOT_EVALUATED"
    assert s["bound_eligibility"] == "ELIGIBLE" and s["unbound_rules"] == 2
    assert s["pending_kinds"] == []


def test_single_unbound_rule_is_enough_to_block():
    s = _status(_cohort([_rule("I1", "age")], {"I1": "yes"}, {"age": 60}, unbound=["E9"]))
    assert s["status"] == "NOT_EVALUATED"


def test_unbound_rule_keeps_decisive_ineligible():
    """다른 기준에서 결정적인 불통과가 있으면 미연결 기준이 남아도 부적격 확정 유지."""
    s = _status(_cohort([_rule("I1", "age"), _rule("I2", "lab.hemoglobin")], {"I1": "no", "I2": "unknown"}, {"age": 10},
                        unbound=["I9"]))
    assert s["status"] == "CONFIRMED_INELIGIBLE" and s["eligibility"] == "INELIGIBLE"


@pytest.mark.parametrize("slot, status, gap", [("lab.hemoglobin", "INFO_MISSING", "missing"),
                                               ("judge.x", "EXPERT_REVIEW", "physician_review")])
def test_unbound_rule_does_not_absorb_open_evidence_gap(slot, status, gap):
    """근거 부족·판단 대기(남은 작업이 있음)를 미평가로 뭉개지 않음: 두 상태 구분 유지."""
    s = _status(_cohort([_rule("I1", "age"), _rule("I2", slot)], {"I1": "yes", "I2": "unknown"}, {"age": 60}, unbound=["E9"]))
    assert s["status"] == status and s["eligibility"] == "UNCERTAIN" and s["pending_kinds"] == [gap]


def test_missing_unbound_list_is_not_treated_as_empty():
    """미연결 목록이 없는(형식이 다른) 데이터는 전체 기준을 봤다고 볼 수 없음 -> 확정 안 함."""
    c = _cohort([_rule("I1", "age")], {"I1": "yes"}, {"age": 60})
    del c["public"]["unbound_rules"]["T1"]
    assert _status(c)["status"] == "NOT_EVALUATED"
    del c["public"]["unbound_rules"]
    assert _status(c)["status"] == "NOT_EVALUATED"


def _default_cohorts():
    cfg, sd, cat, parsed = gen.load_inputs(config.DATA_DIR / "cohort" / "generator_config.json")
    return [c.model_dump(mode="json") for c in gen.generate_all(cfg, sd, cat, parsed)]


def test_default_data_no_confirmed_eligible_on_trials_with_unbound_rules():
    """이슈 #23 재현 데이터: 근거 없는 확정 81쌍(72+9)이 모두 미평가가 되고, 다른 상태는 그대로.
    81쌍이 임상적으로 부적격이라는 뜻이 아님: 필수 근거 없이 확정할 수 없다는 뜻."""
    moved = {}
    for d in _default_cohorts():
        after = cb.board(d)["pairs"]
        without = copy.deepcopy(d)  # 대조군: 미연결 기준을 지우면 게이트가 없을 때의 상태
        without["public"]["unbound_rules"] = {t: [] for t in d["public"]["trials"]}
        for k, v in cb.board(without)["pairs"].items():
            t = k.split("|")[1]
            assert after[k]["status"] != "CONFIRMED_ELIGIBLE" or not d["public"]["unbound_rules"][t]
            if v["status"] == "CONFIRMED_ELIGIBLE":
                assert after[k]["status"] == "NOT_EVALUATED" and after[k]["bound_eligibility"] == "ELIGIBLE"
                moved[t] = moved.get(t, 0) + 1
            else:
                assert after[k]["status"] == v["status"]  # 부적격·정보 부족 대조군은 변화 없음
    # 팀 결정 5(11-07): cm.serious_organ_disease(NCT06125405 E11)·cm.immune_onc(NCT06530004 E5)가 판단 기준이 되어
    # 두 시험은 게이트가 없어도 EXPERT_REVIEW로 끝남 (이전 72+9쌍). 게이트가 옮긴 쌍은 미연결 기준이 남은 시험에서만 나옴
    assert moved == {"NCT07087314": 1}


# ---------------------------------------------------------------------------
# OR 경로(코호트): 공통 기준 + 코호트 기준, 가장 나은 경로로 판정 (Matcher._select_cohort와 같은 뜻)
# ---------------------------------------------------------------------------

OR_RULES = [_rule("I0", "age"), _rule("A1", "lab.hemoglobin", cohort="A"), _rule("B1", "ecog", cohort="B")]


@pytest.mark.parametrize("holds, unbound, status, eligibility", [
    ({"I0": "yes", "A1": "no", "B1": "yes"}, [], "CONFIRMED_ELIGIBLE", "ELIGIBLE"),  # 한 경로만 불통과 -> 통과 경로로 적격
    ({"I0": "yes", "A1": "no", "B1": "no"}, [], "CONFIRMED_INELIGIBLE", "INELIGIBLE"),  # 모든 경로 불통과
    ({"I0": "no", "A1": "yes", "B1": "yes"}, [], "CONFIRMED_INELIGIBLE", "INELIGIBLE"),  # 공통 기준 불통과 = 모든 경로
    ({"I0": "yes", "A1": "no", "B1": "unknown"}, [], "INFO_MISSING", "UNCERTAIN"),  # 남은 경로가 미확인 -> 부적격 확정 금지
    ({"I0": "yes", "A1": "no", "B1": "yes"}, ["E9"], "NOT_EVALUATED", "NOT_EVALUATED"),  # 통과 경로 + 미연결 -> 미평가
    ({"I0": "yes", "A1": "no", "B1": "no"}, ["E9"], "CONFIRMED_INELIGIBLE", "INELIGIBLE"),  # 모든 경로 불통과 + 미연결
])
def test_or_paths(holds, unbound, status, eligibility):
    s = _status(_cohort(OR_RULES, holds, {"age": 60}, unbound=unbound))
    assert (s["status"], s["eligibility"]) == (status, eligibility)


def test_or_gaps_come_from_the_applied_path_only():
    """불통과로 끝난 경로의 미확인 기준은 남은 작업이 아님 (계약: 판정에 적용된 경로의 gap만)."""
    rules = [_rule("A1", "age", cohort="A"), _rule("A2", "judge.x", cohort="A"), _rule("B1", "lab.hemoglobin", cohort="B")]
    s = _status(_cohort(rules, {"A1": "no", "A2": "unknown", "B1": "unknown"}, {"age": 60}))
    assert s["status"] == "INFO_MISSING" and s["pending_kinds"] == ["missing"]
    assert {r["rule_id"]: r["applied"] for r in s["rules"]} == {"A1": False, "A2": False, "B1": True}


def test_or_path_with_only_unbound_rules_stays_alive():
    """기준이 모두 미연결인 경로 C는 public.rules에 없어도 살아 있음 -> 부적격 확정 금지 (생성기 public.paths 입력)."""
    rules = [_rule("A1", "age", cohort="A"), _rule("B1", "ecog", cohort="B")]
    s = _status(_cohort(rules, {"A1": "no", "B1": "no"}, {"age": 60}, unbound=["C1"], paths=["A", "B", "C"]))
    assert s["status"] == "NOT_EVALUATED" and s["bound_eligibility"] == "ELIGIBLE"


# ---------------------------------------------------------------------------
# 임상의 판단 기준: 자동 통과·자동 탈락 금지, 정의는 생성기 public.expert_rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slot", ["dx.disease_setting", "tx.recent_radiotherapy", "cm.cardiac", "other.tumor_tissue"])
def test_expert_rules_without_judge_prefix_are_expert_review(slot):
    """이슈 #23 2-4: 접두어가 없는 판단 슬롯도 생성기가 내보낸 expert_rules로 EXPERT_REVIEW."""
    c = _cohort([_rule("I1", "age"), _rule("E9", slot, kind="exclusion")], {"I1": "yes", "E9": "unknown"}, {"age": 60},
                expert=["E9"])
    s = _status(c)
    assert s["status"] == "EXPERT_REVIEW" and s["rules"][1]["gap"] == "physician_review"


@pytest.mark.parametrize("slot, expert", [("judge.x", None), ("cm.cardiac", ["E9"])])
@pytest.mark.parametrize("holds", ["yes", "no"])
def test_expert_rule_value_never_decides(slot, expert, holds):
    """판단 기준에 값이 있어도(이전 형식 데이터) 통과·불통과로 쓰지 않음."""
    c = _cohort([_rule("I1", "age"), _rule("E9", slot, kind="exclusion")], {"I1": "yes", "E9": holds},
                {"age": 60, slot: "x"}, expert=expert)
    s = _status(c)
    assert s["status"] == "EXPERT_REVIEW" and s["eligibility"] == "UNCERTAIN"


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


def test_board_page_truth_label_keeps_unlabeled_apart():
    """이슈 #23 2-1: 라벨 없음을 부적격으로 보이지 않고 사유를 함께 표시."""
    js = (STATIC / "cohort.js").read_text(encoding="utf-8")
    assert '`정답: ${truth === "ELIGIBLE" ? "적격" : "부적격"}`' not in js
    assert "정답 없음" in js and "label_reasons" in js and "expert_review" in js and "unbound_rules" in js


def test_board_page_unbound_note_depends_on_generator_policy():
    """이슈 #23 2-2·2-5: '충족 간주' 문구는 이전 형식 데이터 경고에만 쓰고, 새 형식에서는 라벨이 없다고 안내."""
    js = (STATIC / "cohort.js").read_text(encoding="utf-8")
    assert 'unbound_policy === "assume_pass"' in js
    assert "적격 정답을 만들지 않습니다" in js and "build_cohort_benchmark.py generate" in js


def test_board_page_explains_partial_evaluation_and_or_rule():
    js = (STATIC / "cohort.js").read_text(encoding="utf-8")
    html = (STATIC / "cohort.html").read_text(encoding="utf-8")
    assert "일부 기준만 평가" in js
    assert "기준 하나만 맞지 않아도 확정됩니다" not in html  # OR 시험은 모든 경로 불통과일 때만 확정
    assert "미평가는 실제 확인" not in html  # 이제 정적 데이터에도 미평가가 나옴
