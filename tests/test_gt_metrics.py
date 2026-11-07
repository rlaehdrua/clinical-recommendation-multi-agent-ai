"""지표 계산: 분모, 구간, 분모 0 처리, 오류 분해, 짝지은 비교. FIXTURE_ONLY 가짜 라벨만 사용."""
import copy
import hashlib
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gt_eval  # noqa: E402
from gt_lib import stats  # noqa: E402
from gt_lib.metrics import ENGINE_FORMAT, evaluate  # noqa: E402
from gt_lib.models import GTSet  # noqa: E402

FX = ROOT / "tests" / "data" / "gt_fixture"


# --------------------------------------------------------------------------- 통계

def test_wilson_known_values():
    lo, hi = stats.wilson(7, 15)  # D 2.8: 0.47 [0.25, 0.70]
    assert round(lo, 2) == 0.25 and round(hi, 2) == 0.70
    lo, hi = stats.wilson(12, 80)  # 0.15 [0.09, 0.25]
    assert round(lo, 2) == 0.09 and round(hi, 2) == 0.24
    assert stats.wilson(0, 0) is None


def test_clopper_pearson_known_values():
    lo, hi = stats.clopper_pearson(0, 60)
    assert lo == 0.0 and math.isclose(hi, 1 - 0.025 ** (1 / 60), rel_tol=1e-6)  # ~0.0596 (3의 규칙 ~ 0.05)
    lo, hi = stats.clopper_pearson(60, 60)
    assert hi == 1.0 and math.isclose(lo, 0.025 ** (1 / 60), rel_tol=1e-6)
    lo, hi = stats.clopper_pearson(3, 10)  # 표준값 [0.0667, 0.6525]
    assert round(lo, 4) == 0.0667 and round(hi, 4) == 0.6525
    assert stats.clopper_pearson(1, 0) is None


def test_rate_zero_denominator_is_not_estimable():
    r = stats.rate(0, 0)
    assert r["status"] == "추정 불가" and r["rate"] is None and r["wilson95"] is None and r["exact95"] is None
    with pytest.raises(ValueError):
        stats.rate(3, 2)


def test_mcnemar_exact():
    m = stats.mcnemar_exact(8, 0)  # D 2.8: p ~ 0.008
    assert m["discordant"] == 8 and round(m["p_two_sided"], 4) == 0.0078
    assert stats.mcnemar_exact(0, 0)["status"] == "추정 불가"
    assert stats.mcnemar_exact(3, 3)["p_two_sided"] == 1.0


def test_cohen_kappa():
    k = stats.cohen_kappa(list("aabb"), list("aabb"))
    assert k["kappa"] == 1.0
    k = stats.cohen_kappa(["y"] * 5 + ["n"] * 5, ["y"] * 4 + ["n"] * 6)  # po=0.9 pe=0.5
    assert math.isclose(k["kappa"], 0.8)
    assert stats.cohen_kappa(["x"] * 3, ["x"] * 3)["status"] == "추정 불가"  # 한 범주뿐
    assert stats.cohen_kappa([], [])["status"] == "추정 불가"


# --------------------------------------------------------------------------- G-U 지표

def _raw(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def _clone(item, iid, **final):
    it = copy.deepcopy(item)
    it["item_id"] = iid
    for a in it["annotations"]:
        a["label"].update(final)
    it["final"].update(final)
    return it


def _engine(set_id, results):
    return {"format": ENGINE_FORMAT, "gt_set_id": set_id, "level": "U",
            "items": [{"item_id": k, "result": v} for k, v in results.items()]}


def _gu_set():
    """상충 양성 5 (묶음 c1 3, c2 2), 상충 음성 4, 정상 값 3, 전해 들은 값 hold 4 (문턱 가로지름 2), 다른 음성 1."""
    d = _raw("FIXTURE_ONLY_g_u.json")
    cpos, cneg, vpos, vhear, amb = d["items"]
    items = [_clone(cpos, f"cp{i}", concept_cluster="c1" if i < 3 else "c2") for i in range(5)]
    items += [_clone(cneg, f"cn{i}") for i in range(4)]
    items += [_clone(vpos, f"vp{i}") for i in range(3)]
    items += [_clone(vhear, f"vh{i}", threshold_crossing=(i < 2)) for i in range(4)]
    items += [_clone(vpos, "vo0", decision="hold", category="FIXTURE_ONLY_other_person")]
    items.append(amb)
    d["items"] = items
    return GTSet.model_validate(d)


GU_RESULTS = {
    # 상충 양성: conflicting 2 (c1 2개), direct 2, unlocated 1 (c2 둘 다 미탐지)
    "cp0": {"directness": "conflicting"}, "cp1": {"directness": "conflicting"}, "cp2": {"directness": "direct"},
    "cp3": {"directness": "unchecked"}, "cp4": {"directness": "unlocated"},
    # 상충 음성: direct 2, conflicting 1, unlocated 1
    "cn0": {"directness": "direct"}, "cn1": {"directness": "direct"}, "cn2": {"directness": "conflicting"},
    "cn3": {"directness": "unlocated"},
    # 정상 값: 채택 2, 보류 1
    "vp0": {"accepted": True}, "vp1": {"accepted": True}, "vp2": {"accepted": False},
    # 전해 들은 값: 문턱 가로지름 2개 중 1개 채택, 나머지 2개 중 1개 채택
    "vh0": {"accepted": True}, "vh1": {"accepted": False}, "vh2": {"accepted": True}, "vh3": {"accepted": False},
    "vo0": {"accepted": True},
    "FX-U-005": {"accepted": True},  # ambiguous: 주 분모 제외, 따로 셈
}


def test_unit_metrics_denominators_and_counts():
    gt = _gu_set()
    rep = evaluate(gt, _engine(gt.set_id, GU_RESULTS))
    A, B = rep["A"], rep["B"]
    assert (A["n_positive"], A["n_negative"]) == (5, 4)
    assert (A["A1"]["numerator"], A["A1"]["denominator"]) == (2, 5)
    assert (A["A2"]["numerator"], A["A2"]["denominator"]) == (2, 5)
    assert (A["A3"]["numerator"], A["A3"]["denominator"]) == (1, 5)
    assert A["A1"]["numerator"] + A["A2"]["numerator"] + A["A3"]["numerator"] == 5
    assert (A["A4"]["numerator"], A["A4"]["denominator"]) == (1, 4)
    assert (A["A5"]["numerator"], A["A5"]["denominator"]) == (2, 4)
    assert A["A2_per_cluster"]["c1"]["numerator"] == 2 and A["A2_per_cluster"]["c2"]["denominator"] == 2
    assert A["errors"] == {"false_accept": 2, "over_hold": 2}
    # 작은 n: 합격 판정 안 함
    assert A["A1"]["n_sufficient"] is False and A["A1"]["d_criterion_met"] is None
    assert "판정 안 함" in A["A1"]["d_judgement"]

    assert (B["B1"]["numerator"], B["B1"]["denominator"]) == (2, 4)
    assert (B["B1_crossing"]["numerator"], B["B1_crossing"]["denominator"]) == (1, 2)
    assert (B["B2"]["numerator"], B["B2"]["denominator"]) == (1, 2)
    assert (B["B3"]["numerator"], B["B3"]["denominator"]) == (1, 3)
    assert B["B4"]["FIXTURE_ONLY_other_person"]["numerator"] == 1
    assert B["errors"] == {"false_accept": 3, "over_hold": 1}
    assert B["ambiguous"] == {"n": 1, "engine_accepted": 1}
    assert rep["coverage"]["labelled"] == 17 and rep["coverage"]["ambiguous"] == 1
    assert rep["warning"].startswith("FIXTURE_ONLY")
    assert "A7" in rep["not_measured"] and "B6" in rep["not_measured"]


def test_unit_metrics_intervals_match_stats():
    gt = _gu_set()
    a1 = evaluate(gt, _engine(gt.set_id, GU_RESULTS))["A"]["A1"]
    lo, hi = stats.wilson(2, 5)
    assert a1["wilson95"] == [round(lo, 4), round(hi, 4)]
    lo, hi = stats.clopper_pearson(2, 5)
    assert a1["exact95"] == [round(lo, 4), round(hi, 4)]


def test_zero_denominator_reports_not_estimable_and_never_passes():
    d = _raw("FIXTURE_ONLY_g_u.json")
    d["items"] = [d["items"][2]]  # 정상 값 하나: 상충 항목 없음
    gt = GTSet.model_validate(d)
    rep = evaluate(gt, _engine(gt.set_id, {"FX-U-003": {"accepted": True}}))
    for k in ("A1", "A2", "A4"):
        assert rep["A"][k]["status"] == "추정 불가" and rep["A"][k]["denominator"] == 0
        assert rep["A"][k]["d_criterion_met"] is None and "추정 불가" in rep["A"][k]["d_judgement"]
    assert rep["B"]["B1"]["status"] == "추정 불가"


def test_sufficient_n_produces_judgement():
    d = _raw("FIXTURE_ONLY_g_u.json")
    cpos = d["items"][0]
    d["items"] = [_clone(cpos, f"cp{i}") for i in range(60)]
    gt = GTSet.model_validate(d)
    res = {f"cp{i}": {"directness": "conflicting"} for i in range(60)}
    a = evaluate(gt, _engine(gt.set_id, res))["A"]
    assert a["A1"]["numerator"] == 0 and a["A1"]["d_criterion_met"] is True
    assert a["A2"]["d_criterion_met"] is True
    res["cp0"] = {"directness": "direct"}
    a = evaluate(gt, _engine(gt.set_id, res))["A"]
    assert a["A1"]["d_criterion_met"] is False  # 1/60: 정확 상한 > 5%


def test_paired_comparison():
    gt = _gu_set()
    before = dict(GU_RESULTS)
    after = dict(GU_RESULTS, cp2={"directness": "conflicting"}, cp3={"directness": "conflicting"},
                 vp0={"accepted": False})
    rep = evaluate(gt, _engine(gt.set_id, after), _engine(gt.set_id, before))
    assert rep["paired"]["conflict"]["mcnemar"]["fixed"] == 2
    assert rep["paired"]["conflict"]["mcnemar"]["broken"] == 0
    assert rep["paired"]["value"]["newly_held_correct_accepts"] == ["vp0"]


def test_missing_engine_items_are_reported_not_counted():
    gt = _gu_set()
    res = dict(GU_RESULTS)
    res.pop("cp0")
    rep = evaluate(gt, _engine(gt.set_id, res))
    assert rep["coverage"]["missing_engine_output"] == ["cp0"]
    assert rep["A"]["A1"]["denominator"] == 4


# --------------------------------------------------------------------------- G-P 지표

def _gp_set():
    d = _raw("FIXTURE_ONLY_g_p.json")
    el, inel, nd, nc = d["items"]
    items = [copy.deepcopy(el) | {"item_id": f"el{i}"} for i in range(3)]
    items += [copy.deepcopy(inel) | {"item_id": f"in{i}"} for i in range(2)]
    items += [copy.deepcopy(nd) | {"item_id": "nd0"}, copy.deepcopy(nc) | {"item_id": "nc0"}]
    oos = copy.deepcopy(el) | {"item_id": "oos0"}
    oos["input"] = dict(oos["input"], grid={"gc_item_id": "FX-C-001", "criterion_id": "FX-I1",
                                            "country_cell": "out_of_scope", "age_band": "between"})
    items.append(oos)
    d["items"] = items
    return GTSet.model_validate(d)


GP_RESULTS = {
    "el0": {"pair_status": "CONFIRMED_ELIGIBLE"}, "el1": {"pair_status": "INFO_MISSING", "hold_reason": "doc"},
    "el2": {"pair_status": "CONFIRMED_INELIGIBLE"},
    "in0": {"pair_status": "CONFIRMED_INELIGIBLE"}, "in1": {"pair_status": "VERIFICATION_PENDING"},
    "nd0": {"pair_status": "CONFIRMED_ELIGIBLE"}, "nc0": {"pair_status": "CONFIRMED_ELIGIBLE"},
    "oos0": {"pair_status": "CONFIRMED_INELIGIBLE", "deciding_criteria": ["FX-I1"]},
}


def test_pair_metrics_separate_error_kinds():
    gt = _gp_set()
    rep = evaluate(gt, {**_engine(gt.set_id, GP_RESULTS), "level": "S"})
    E = rep["E"]
    # CE: el0(정답), nd0, nc0 -> 거짓 확정 적격 2/3
    assert (E["E1"]["numerator"], E["E1"]["denominator"]) == (2, 3)
    # CI: el2, in0, oos0(GT ELIGIBLE) -> 거짓 확정 부적격 2/3
    assert (E["E2"]["numerator"], E["E2"]["denominator"]) == (2, 3)
    # GT ELIGIBLE 4 (el0-2, oos0) 중 CE 아님 3
    assert (E["E4"]["numerator"], E["E4"]["denominator"]) == (3, 4)
    assert (E["E6"]["numerator"], E["E6"]["denominator"]) == (1, 1)
    assert E["errors"]["false_eligible"] == 2
    assert E["errors"]["false_ineligible"] == 2
    assert E["errors"]["unresolved_gt_ineligible"] == 1  # in1
    assert E["errors"]["over_hold_gt_eligible"] == 1     # el1
    assert E["E4_breakdown"]["INFO_MISSING|doc"] == 1
    assert (E["C2"]["numerator"], E["C2"]["denominator"]) == (1, 1)
    assert "E3" in rep["not_measured"]
    # 분모 < 60: 합격 판정 없음
    assert E["E1"]["d_criterion_met"] is None


def test_pair_e1_zero_denominator_not_estimable():
    gt = _gp_set()
    res = {k: {"pair_status": "INFO_MISSING"} for k in GP_RESULTS}
    E = evaluate(gt, _engine(gt.set_id, res))["E"]
    assert E["E1"]["status"] == "추정 불가" and E["E1"]["d_criterion_met"] is None
    assert E["E2"]["status"] == "추정 불가"


def test_pair_transition_table_with_baseline():
    gt = _gp_set()
    base = {k: {"pair_status": "INFO_MISSING"} for k in GP_RESULTS}
    E = evaluate(gt, _engine(gt.set_id, GP_RESULTS), _engine(gt.set_id, base))["E"]
    assert E["E3"]["entered_confirmed"] == 6 and E["E3"]["left_confirmed"] == 0


def test_unknown_engine_status_rejected():
    gt = _gp_set()
    with pytest.raises(ValueError):
        evaluate(gt, _engine(gt.set_id, dict(GP_RESULTS, el0={"pair_status": "ELIGIBLE"})))


# --------------------------------------------------------------------------- CLI

def test_eval_refuses_output_from_other_file(tmp_path):
    gt_path = FX / "FIXTURE_ONLY_g_u.json"
    out = _engine("FIXTURE_ONLY_g_u", {"FX-U-001": {"directness": "direct"}})
    out["gt_set_sha256"] = "0" * 64
    p = tmp_path / "o.json"
    p.write_text(json.dumps(out), encoding="utf-8")
    assert gt_eval.main(["--gt", str(gt_path), "--engine", str(p)]) == 1
    out["gt_set_sha256"] = hashlib.sha256(gt_path.read_bytes()).hexdigest()
    p.write_text(json.dumps(out), encoding="utf-8")
    rep_path = tmp_path / "rep.json"
    assert gt_eval.main(["--gt", str(gt_path), "--engine", str(p), "--out", str(rep_path)]) == 0
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    assert "탐색적" in rep["exploratory"] and rep["fixture_only"] is True


def test_eval_refuses_wrong_set_id():
    gt = _gu_set()
    with pytest.raises(ValueError):
        evaluate(gt, _engine("other", GU_RESULTS))
