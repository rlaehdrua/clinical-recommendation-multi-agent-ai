"""후보 A 합성 코호트 생성기 (ctrec.cohort). 가짜 시험 2개로 생성·정답·검증·정답 비노출을 확인합니다 (API 호출 없음)."""

import json

import pytest

from ctrec.cohort import generate as gen
from ctrec.cohort import slots as slot_draft
from ctrec.cohort.spec import (Binding, Cohort, GeneratorConfig, SlotDictionary, SlotSpec, SourceCatalog, SourceSpec,
                               policy_view)
from ctrec.cohort.truth import rule_holds
from ctrec.cohort.validate import stats, validate
from ctrec.schemas import ParsedTrial, Rule


def _rule(**kw) -> Rule:
    base = dict(rule_id="I1", kind="inclusion", text="t", category="lab", field=None, operator=None, value=None,
                unit=None, time_window_days=None, structured_evaluable=True, underspecified=False, cohort=None)
    base.update(kw)
    return Rule(**base)


def _parsed(tid, rules) -> ParsedTrial:
    return ParsedTrial(trial_id=tid, title="t", target_population="", intervention_summary="", key_conditions=[],
                       cohorts=[], rules=rules, parsing_notes="")


PARSED = {
    "T1": _parsed("T1", [
        _rule(rule_id="I1", category="demographic", field="age", operator=">=", value="18", unit="years"),
        _rule(rule_id="I2", field="lab:hemoglobin", operator=">=", value="9", unit="g/dL", time_window_days=28),
        _rule(rule_id="E1", kind="exclusion", category="treatment_history", text="Prior EGFR TKI", structured_evaluable=False),
    ]),
    "T2": _parsed("T2", [
        _rule(rule_id="I1", field="lab:hgb", operator=">=", value="10", unit="g/dL"),
        _rule(rule_id="I2", field="lab:platelets", operator=">=", value="100", unit="10^9/L"),
        _rule(rule_id="I3", field="lab:hemoglobin", operator=">", value="80", unit="g/L"),  # 슬롯 단위(g/dL)와 다름
        _rule(rule_id="E1", kind="exclusion", category="comorbidity", text="Uncontrolled infection", structured_evaluable=False),
    ]),
}


def _dictionary() -> SlotDictionary:
    d = slot_draft.draft(PARSED)
    d.slots["tx.egfr_tki"] = SlotSpec(slot_id="tx.egfr_tki", kind="categorical", category="treatment_history",
                                      source="S_summary", values=["yes", "no"])
    bindings = []
    for b in d.bindings:
        if b.key == "T1:E1":  # 사람이 검수했다고 가정한 범주형 연결
            b = b.model_copy(update=dict(status="reviewed", slot="tx.egfr_tki", truth="categorical", op="in", values=["yes"]))
        bindings.append(b)
    return SlotDictionary(built_from=d.built_from, slots=d.slots, bindings=bindings)


CATALOG = SourceCatalog(sources={
    "S_lab": SourceSpec(label="lab", evidence="record", cost_min=3, cost_range=(1, 10), reveals=["lab.*"], basis="가정"),
    "S_summary": SourceSpec(label="summary", evidence="record", cost_min=12, cost_range=(5, 30), reveals=["tx.*"], basis="가정"),
    "S_phone": SourceSpec(label="phone", evidence="self_report", cost_min=15, cost_range=(5, 30), reveals=["tx.*"], basis="가정"),
})


def _config(**scenario_overrides) -> GeneratorConfig:
    sc = {"scenario_id": "s", "cluster": "c", "n_patients": 12, "replicates": 3, "dev_replicates": 2,
          "knobs": {"trials_per_patient": 2, "cost_heterogeneity": 1.0, "grouping": "source",
                    "missing_rate": {"default": 0.5}}}
    sc.update(scenario_overrides)
    return GeneratorConfig(**{
        "reference_date": "2026-11-01", "base_seed": 7, "slot_dictionary": "-", "source_catalog": "-",
        "clusters": {"c": {"trials": ["T1", "T2"], "priors": {
            "age": {"dist": "uniform", "a": 10, "b": 80, "decimals": 0},
            "lab.hemoglobin": {"dist": "normal", "a": 10.5, "b": 2, "lo": 5, "hi": 17, "unit": "g/dL"},
            "lab.platelets": {"dist": "normal", "a": 200, "b": 80, "lo": 10, "hi": 600, "decimals": 0, "unit": "10^9/L"},
            "tx.egfr_tki": {"dist": "categorical", "probs": {"yes": 0.3, "no": 0.7}},
            "lab_age_days": {"dist": "uniform", "a": 0, "b": 56, "decimals": 0, "unit": "days"},
        }}},
        "scenarios": [sc],
    })


def _cohorts(cfg=None, sd=None):
    return gen.generate_all(cfg or _config(), sd or _dictionary(), CATALOG, PARSED)


# ---------------------------------------------------------------------------
# 슬롯 사전 초안
# ---------------------------------------------------------------------------

def test_draft_links_only_engine_decidable_rules():
    d = slot_draft.draft(PARSED)
    by = {b.key: b for b in d.bindings}
    assert by["T1:I1"].status == "auto" and by["T1:I1"].slot == "age"
    assert by["T1:I2"].status == by["T2:I1"].status == "auto"
    assert by["T1:I2"].slot == by["T2:I1"].slot == "lab.hemoglobin"  # hgb -> 표준 검사명, 두 시험이 슬롯 공유
    assert by["T2:I3"].status == "needs_review" and "g/L" in by["T2:I3"].note  # 단위가 달라 엔진이 판정 못 함
    assert by["T1:E1"].status == "needs_review" and by["T1:E1"].slot is None  # 비구조화 기준
    assert d.slots["lab.hemoglobin"].unit == "g/dL" and d.slots["age"].source == "core"


def test_draft_merge_keeps_reviewed_bindings_and_manual_slots():
    reviewed = _dictionary()
    again = slot_draft.draft(PARSED, existing=reviewed)
    b = next(b for b in again.bindings if b.key == "T1:E1")
    assert b.status == "reviewed" and b.slot == "tx.egfr_tki"
    assert "tx.egfr_tki" in again.slots


def test_usable_binding_requires_slot_and_truth():
    with pytest.raises(ValueError):
        Binding(trial_id="T", rule_id="I1", kind="inclusion", category="lab", text="t", status="reviewed")


# ---------------------------------------------------------------------------
# 정답 계산
# ---------------------------------------------------------------------------

def test_truth_uses_rule_engine_and_categorical_sets():
    sd = _dictionary()
    by = {b.key: b for b in sd.bindings}
    rules = {f"{t}:{r.rule_id}": r for t, p in PARSED.items() for r in p.rules}
    assert rule_holds(by["T1:I2"], rules["T1:I2"], {"lab.hemoglobin": 8.9}, sd.slots, "2026-11-01") == "no"
    assert rule_holds(by["T1:I2"], rules["T1:I2"], {"lab.hemoglobin": 9.0}, sd.slots, "2026-11-01") == "yes"
    assert rule_holds(by["T1:E1"], None, {"tx.egfr_tki": "yes"}, sd.slots, "2026-11-01") == "yes"
    assert rule_holds(by["T1:E1"], None, {"tx.egfr_tki": "no"}, sd.slots, "2026-11-01") == "no"


def test_exclusion_that_holds_makes_pair_ineligible():
    for c in _cohorts():
        for key, label in c.oracle.pair_label.items():
            pid, t = key.split("|")
            if t == "T1" and c.oracle.truth[f"{pid}|T1|E1"] == "yes":
                assert label == "INELIGIBLE"


# ---------------------------------------------------------------------------
# 생성: 결정성, 분할, 검증
# ---------------------------------------------------------------------------

def test_same_config_gives_identical_cohorts():
    a = [c.model_dump(mode="json") for c in _cohorts()]
    b = [c.model_dump(mode="json") for c in _cohorts()]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a[0]["public"] != a[1]["public"]  # 반복마다 다른 시드


def test_splits_follow_dev_replicates():
    assert [c.split for c in _cohorts()] == ["dev", "dev", "test"]


def test_generated_cohorts_pass_validation():
    sd = _dictionary()
    for c in _cohorts(sd=sd):
        assert validate(c, sd, PARSED) == []
        assert c.public.unbound_rules == {"T1": [], "T2": ["I3", "E1"]}


def test_validation_detects_tampered_truth_and_leaks():
    sd = _dictionary()
    c = _cohorts(sd=sd)[0]
    key = next(iter(c.oracle.truth))
    bad = c.model_copy(deep=True)
    bad.oracle.truth[key] = "no" if c.oracle.truth[key] == "yes" else "yes"
    assert any("정답 불일치" in i for i in validate(bad, sd, PARSED))

    leak = c.model_copy(deep=True)
    p = next(p for p in leak.public.patients if p.unknown_slots)
    s = p.unknown_slots[0]
    p.known[s] = leak.oracle.hidden[p.patient_id][s]
    assert any("숨긴 슬롯이 공개됨" in i for i in validate(leak, sd, PARSED))


def test_config_hash_changes_with_inputs():
    sd = _dictionary()
    h1 = gen.config_hash(_config(), sd, CATALOG)
    h2 = gen.config_hash(_config(n_patients=13), sd, CATALOG)
    assert h1 != h2 and all(c.config_hash == h1 for c in _cohorts(sd=sd))


# ---------------------------------------------------------------------------
# 정답 비노출
# ---------------------------------------------------------------------------

def test_policy_view_has_no_oracle_and_hidden_values_stay_unknown():
    c = _cohorts()[0]
    view = policy_view(c).model_dump(mode="json")
    assert "oracle" not in view and "truth" not in json.dumps(view) and "pair_label" not in json.dumps(view)
    for p in c.public.patients:
        assert not set(p.unknown_slots) & set(p.known)
        for t in p.candidate_trials:
            for r in c.public.rules[t]:
                if r.slot in p.unknown_slots:
                    assert c.public.initial_holds[f"{p.patient_id}|{t}|{r.rule_id}"] == "unknown"


def test_core_slots_are_never_hidden():
    for c in _cohorts():
        assert all("age" not in p.unknown_slots for p in c.public.patients)


# ---------------------------------------------------------------------------
# knob: 비용 이질성, 소스 묶음, 영 시나리오
# ---------------------------------------------------------------------------

def test_cost_heterogeneity_and_grouping():
    sd = _dictionary()
    het = _cohorts(sd=sd)[0].public.sources
    assert {k: v.cost_min for k, v in het.items()} == {"S_lab": 3, "S_summary": 12, "S_phone": 15}
    assert het["S_lab"].reveals == ["lab.hemoglobin", "lab.platelets"]  # 결과지 한 장이 두 검사를 함께
    assert het["S_phone"].evidence == "self_report"

    flat = _cohorts(_config(knobs={"trials_per_patient": 2, "cost_heterogeneity": 0.0, "grouping": "per_slot"}), sd)[0]
    costs = {v.cost_min for v in flat.public.sources.values()}
    assert costs == {10.0}  # (3 + 12 + 15) / 3
    assert set(flat.public.sources) == {"S_lab:lab.hemoglobin", "S_lab:lab.platelets", "S_summary:tx.egfr_tki",
                                        "S_phone:tx.egfr_tki"}


def test_null_scenario_forces_null_knobs_and_validates():
    sd = _dictionary()
    cfg = _config(null=True, knobs={"trials_per_patient": 2, "cost_heterogeneity": 1.0, "grouping": "source",
                                    "enrollment_remaining": 2, "missing_rate": {"default": 0.5}})
    for c in _cohorts(cfg, sd):
        assert c.knobs.trials_per_patient == 1 and c.knobs.grouping == "per_slot" and c.knobs.enrollment_remaining is None
        assert all(len(p.candidate_trials) == 1 for p in c.public.patients)
        assert validate(c, sd, PARSED) == []


def test_validation_flags_broken_null_scenario():
    sd = _dictionary()
    c = _cohorts(sd=sd)[0].model_copy(update={"null": True})
    assert any("영 시나리오" in i for i in validate(c, sd, PARSED))


def test_enrollment_knob_is_recorded_per_trial():
    c = _cohorts(_config(knobs={"trials_per_patient": 2, "enrollment_remaining": 1}))[0]
    assert c.public.enrollment_remaining == {"T1": 1, "T2": 1}


# ---------------------------------------------------------------------------
# 설정 오류
# ---------------------------------------------------------------------------

def test_missing_prior_or_unit_mismatch_is_an_error():
    cfg = _config()
    del cfg.clusters["c"].priors["lab.platelets"]
    with pytest.raises(gen.GenerationError, match="사전 분포가 없음"):
        _cohorts(cfg)
    cfg = _config()
    cfg.clusters["c"].priors["lab.hemoglobin"].unit = "g/L"
    with pytest.raises(gen.GenerationError, match="단위 불일치"):
        _cohorts(cfg)


def test_stats_report_structure():
    s = stats(_cohorts()[0])
    assert s["pairs"] == sum(len(p.candidate_trials) for p in _cohorts()[0].public.patients)
    assert 0 <= s["eligible_rate"] <= 1 and s["bound_rules"] == {"T1": 3, "T2": 2}


def test_cohort_round_trips_through_json():
    c = _cohorts()[0]
    assert Cohort(**json.loads(json.dumps(c.model_dump(mode="json")))) == c


# ---------------------------------------------------------------------------
# 웹 코호트 현황판 (읽기 전용 API)
# ---------------------------------------------------------------------------

def _web_client(monkeypatch, tmp_path, cohorts):
    from fastapi.testclient import TestClient

    from ctrec.web import app as web
    monkeypatch.setattr(web, "PASSWORD", "")
    monkeypatch.setattr(web, "COHORT_DIR", tmp_path)
    if cohorts:
        out = tmp_path / cohorts[0].config_hash
        out.mkdir()
        for c in cohorts:
            (out / f"{c.cohort_id}.json").write_text(json.dumps(c.model_dump(mode="json")), encoding="utf-8")
        (out / "report.json").write_text(json.dumps({"config_hash": cohorts[0].config_hash, "generator": "g",
                                                     "cohorts": [stats(c) for c in cohorts]}), encoding="utf-8")
    return TestClient(web.app, base_url="https://testserver", client=("198.51.100.20", 50000))


def test_web_cohort_api_lists_and_serves_latest_set(monkeypatch, tmp_path):
    cohorts = _cohorts()
    c = _web_client(monkeypatch, tmp_path, cohorts)
    listed = c.get("/api/cohorts").json()
    assert [x["cohort_id"] for x in listed["cohorts"]] == [x.cohort_id for x in cohorts]
    one = c.get(f"/api/cohorts/{cohorts[0].cohort_id}").json()
    assert Cohort(**one) == cohorts[0]
    page = c.get("/cohort")
    assert page.status_code == 200 and "코호트 현황판" in page.text
    assert c.get("/static/cohort.js").status_code == 200 and c.get("/static/cohort.css").status_code == 200


def test_web_cohort_api_rejects_unknown_ids_and_handles_empty(monkeypatch, tmp_path):
    c = _web_client(monkeypatch, tmp_path, [])
    assert c.get("/api/cohorts").json()["cohorts"] == []
    assert c.get("/api/cohorts/s-r00").status_code == 404
    c = _web_client(monkeypatch, tmp_path, _cohorts())
    assert c.get("/api/cohorts/..%2Freport").status_code == 404
    assert c.get("/api/cohorts/report").status_code == 404


# ---------------------------------------------------------------------------
# LLM 초안 적용 · 사람 검수 반영
# ---------------------------------------------------------------------------

DRAFT = {
    "meta": {"trials": ["T1", "T2"], "cluster": "c"},
    "slots": {"cm.infection": {"kind": "categorical", "category": "comorbidity", "source": "S_summary",
                               "values": ["none", "mild", "severe"]},
              "lab.hbv": {"kind": "categorical", "category": "lab", "source": "S_lab", "values": ["neg", "pos"]}},
    "bindings": [
        {"trial_id": "T2", "rule_id": "E1", "slot": "cm.infection", "truth": "categorical", "op": "in", "values": ["severe"]},
        {"trial_id": "T2", "rule_id": "I3", "slot": "lab.hemoglobin", "truth": "engine",
         "rule_override": {"value": "8.0", "unit": "g/dL"}, "note": "80 g/L = 8.0 g/dL"},
        {"trial_id": "T1", "rule_id": "I1", "slot": "age", "truth": "engine"},  # auto 연결은 바꾸지 않음
    ],
}


def test_apply_draft_marks_draft_and_keeps_auto():
    sd = slot_draft.draft(PARSED)
    new, report = slot_draft.apply_draft(sd, DRAFT, PARSED)
    by = {b.key: b for b in new.bindings}
    assert by["T2:E1"].status == by["T2:I3"].status == "draft"
    assert by["T2:I3"].rule_override == {"value": "8.0", "unit": "g/dL"} and by["T2:I3"].note.startswith("LLM 초안")
    assert report["skipped"] == ["T1:I1 (auto)"] and report["problems"] == {}
    assert new.unbound("T2") and {b.rule_id for b in new.usable("T2")} == {"I1", "I2"}  # 초안은 정답에 안 쓰임


def test_apply_draft_reports_uncomputable_and_constant_bindings():
    bad = {**DRAFT, "bindings": [
        {"trial_id": "T2", "rule_id": "E1", "slot": "cm.infection", "truth": "categorical", "op": "in",
         "values": ["none", "mild", "severe"]},  # 어떤 값이든 성립 -> 항상 yes
        {"trial_id": "T2", "rule_id": "I3", "slot": "lab.hemoglobin", "truth": "engine"}]}  # g/L 그대로면 판정 불가
    _, report = slot_draft.apply_draft(slot_draft.draft(PARSED), bad, PARSED)
    assert "항상 yes" in report["problems"]["T2:E1"][0]
    assert "판정하지 못하는" in report["problems"]["T2:I3"][0]


def test_rule_override_reads_rule_differently_and_needs_engine():
    sd, _ = slot_draft.apply_draft(slot_draft.draft(PARSED), DRAFT, PARSED)
    b = next(x for x in sd.bindings if x.key == "T2:I3")
    rule = next(r for r in PARSED["T2"].rules if r.rule_id == "I3")
    assert rule_holds(b, rule, {"lab.hemoglobin": 8.1}, sd.slots, "2026-11-01") == "yes"
    assert rule_holds(b, rule, {"lab.hemoglobin": 8.0}, sd.slots, "2026-11-01") == "no"  # '>' 8.0
    with pytest.raises(ValueError):
        Binding(trial_id="T", rule_id="I1", kind="inclusion", category="lab", text="t", status="draft",
                truth="categorical", rule_override={"value": "1"})
    with pytest.raises(ValueError):
        Binding(trial_id="T", rule_id="I1", kind="inclusion", category="lab", text="t", status="draft",
                truth="engine", rule_override={"kind": "exclusion"})


def test_categorical_lab_slot_does_not_break_engine_profile():
    sd, _ = slot_draft.apply_draft(slot_draft.draft(PARSED), DRAFT, PARSED)
    b = next(x for x in sd.bindings if x.key == "T1:I2")
    rule = next(r for r in PARSED["T1"].rules if r.rule_id == "I2")
    assert rule_holds(b, rule, {"lab.hemoglobin": 9.5, "lab.hbv": "pos"}, sd.slots, "2026-11-01") == "yes"


def test_apply_review_promotes_only_confirmed_and_computable():
    sd, _ = slot_draft.apply_draft(slot_draft.draft(PARSED), DRAFT, PARSED)
    rows = [{"trial_id": "T2", "rule_id": "E1", "검수 결과(확인/수정/제외)": "확인", "검수자": "A", "검수일": "2026-11-02"},
            {"trial_id": "T2", "rule_id": "I3", "검수 결과(확인/수정/제외)": "수정"},
            {"trial_id": "T1", "rule_id": "E1", "검수 결과(확인/수정/제외)": "확인"}]  # 슬롯이 비어 있는 needs_review
    new, report = slot_draft.apply_review(sd, rows, PARSED)
    by = {b.key: b for b in new.bindings}
    assert by["T2:E1"].status == "reviewed" and "검수 확인 A 2026-11-02" in by["T2:E1"].note
    assert by["T2:I3"].status == "draft" and report["unchanged"] == ["T2:I3"]
    assert by["T1:E1"].status == "needs_review" and "T1:E1" in report["rejected"]
    excluded, _ = slot_draft.apply_review(sd, [{"trial_id": "T2", "rule_id": "I3", "검수 결과(확인/수정/제외)": "제외"}], PARSED)
    assert next(b for b in excluded.bindings if b.key == "T2:I3").status == "excluded"
    assert "I3" not in excluded.unbound("T2") and "I3" not in [b.rule_id for b in excluded.unbound("T2")]


def test_review_sheet_rows_cover_unreviewed_bindings():
    sd, _ = slot_draft.apply_draft(slot_draft.draft(PARSED), DRAFT, PARSED)
    rows = slot_draft.review_rows(sd, PARSED, {"T2": {"rules": {"E1": {"ko": "조절되지 않는 감염"}}}})
    keys = {(r[0], r[1]) for r in rows}
    assert keys == {("T1", "E1"), ("T2", "E1"), ("T2", "I3")}
    e1 = next(r for r in rows if r[:2] == ["T2", "E1"])
    assert e1[4] == "조절되지 않는 감염" and e1[7] == "cm.infection 값이 [severe] 중 하나이면 기준 성립"
    assert len(e1) == len(slot_draft.REVIEW_HEADER)


def test_repository_nephrotic_draft_applies_cleanly():
    """저장소의 실제 초안(data/cohort/llm_draft_nephrotic_peds.json)이 실제 기준에 문제없이 들어가는지."""
    from ctrec import config
    doc = json.loads((config.DATA_DIR / "cohort" / "llm_draft_nephrotic_peds.json").read_text(encoding="utf-8"))
    parsed = gen.load_parsed(doc["meta"]["trials"])
    base = slot_draft.draft(parsed)
    new, report = slot_draft.apply_draft(base, doc, parsed)
    assert report["problems"] == {} and report["not_in_dictionary"] == [] and len(report["applied"]) == 52
    assert slot_draft.summary(new)["by_status"] == {"auto": 15, "draft": 52}


# ---------------------------------------------------------------------------
# 조건 묶음(also/combine) · 3값 논리 · 검사 유효 기간
# ---------------------------------------------------------------------------

from ctrec.cohort.spec import Cond  # noqa: E402
from ctrec.cohort.truth import combine3, holds3  # noqa: E402


def test_combine3_three_valued_logic():
    assert combine3(["yes", "no"], "all") == "no" and combine3(["yes", "unknown"], "all") == "unknown"
    assert combine3(["no", "unknown"], "all") == "no"  # 하나라도 불성립이면 나머지를 몰라도 확정
    assert combine3(["no", "unknown"], "any") == "unknown" and combine3(["yes", "unknown"], "any") == "yes"
    assert combine3(["yes", "yes"], "all") == "yes" and combine3(["no", "no"], "any") == "no"


def _composite_dictionary(combine="all"):
    sd = _dictionary()
    sd.slots["tx.transfusion"] = SlotSpec(slot_id="tx.transfusion", kind="categorical", category="medication",
                                           source="S_summary", values=["yes", "no"])
    bindings = [b.model_copy(update={"also": [Cond(slot="tx.transfusion", op="in", values=["no"])], "combine": combine})
                if b.key == "T1:I2" else b for b in sd.bindings]
    return SlotDictionary(built_from=sd.built_from, slots=sd.slots, bindings=bindings)


def test_composite_binding_all_and_any():
    sd = _composite_dictionary("all")
    b = next(x for x in sd.bindings if x.key == "T1:I2")
    rule = next(r for r in PARSED["T1"].rules if r.rule_id == "I2")
    assert holds3(b, rule, {"lab.hemoglobin": 10.0, "tx.transfusion": "no"}, sd.slots, "2026-11-01") == "yes"
    assert holds3(b, rule, {"lab.hemoglobin": 10.0, "tx.transfusion": "yes"}, sd.slots, "2026-11-01") == "no"
    assert holds3(b, rule, {"lab.hemoglobin": 8.0}, sd.slots, "2026-11-01") == "no"  # Hb만으로 불성립 확정
    assert holds3(b, rule, {"lab.hemoglobin": 10.0}, sd.slots, "2026-11-01") == "unknown"
    with pytest.raises(Exception):
        rule_holds(b, rule, {"lab.hemoglobin": 10.0}, sd.slots, "2026-11-01")  # 정답 계산에는 모든 값 필요
    anyb = next(x for x in _composite_dictionary("any").bindings if x.key == "T1:I2")
    assert holds3(anyb, rule, {"lab.hemoglobin": 8.0, "tx.transfusion": "no"}, sd.slots, "2026-11-01") == "yes"


def test_composite_cohorts_validate_and_expose_extra_slots():
    sd = _composite_dictionary()
    cfg = _config()
    cfg.clusters["c"].priors["tx.transfusion"] = cfg.clusters["c"].priors["tx.egfr_tki"].model_copy()
    for c in _cohorts(cfg, sd):
        assert validate(c, sd, PARSED) == []
        ref = next(r for r in c.public.rules["T1"] if r.rule_id == "I2")
        assert ref.extra_slots == ["tx.transfusion"] and ref.time_window_days == 28


def test_stale_lab_is_unknown_for_windowed_rule_only():
    sd = _dictionary()
    by = {b.key: b for b in sd.bindings}
    rules = {f"{t}:{r.rule_id}": r for t, p in PARSED.items() for r in p.rules}
    fresh = {"lab.hemoglobin": 10.0, "lab_age_days": 10}
    stale = {"lab.hemoglobin": 10.0, "lab_age_days": 40}
    assert by["T1:I2"].time_window_days == 28 and by["T2:I1"].time_window_days is None
    assert holds3(by["T1:I2"], rules["T1:I2"], fresh, sd.slots, "2026-11-01", check_fresh=True) == "yes"
    assert holds3(by["T1:I2"], rules["T1:I2"], stale, sd.slots, "2026-11-01", check_fresh=True) == "unknown"
    assert holds3(by["T2:I1"], rules["T2:I1"], stale, sd.slots, "2026-11-01", check_fresh=True) == "yes"  # 기간 조건 없음
    assert rule_holds(by["T1:I2"], rules["T1:I2"], stale, sd.slots, "2026-11-01") == "yes"  # 정답은 값 그대로


def test_generated_initial_holds_respect_lab_age():
    sd = _dictionary()
    seen_stale = False
    for c in _cohorts(sd=sd):
        for p in c.public.patients:
            if "T1" in p.candidate_trials and "lab.hemoglobin" in p.known and p.known["lab_age_days"] > 28:
                assert c.public.initial_holds[f"{p.patient_id}|T1|I2"] == "unknown"
                seen_stale = True
    assert seen_stale


def test_check_binding_on_composite_detects_constant_result():
    sd = _composite_dictionary()
    b = next(x for x in sd.bindings if x.key == "T1:I2")
    rule = next(r for r in PARSED["T1"].rules if r.rule_id == "I2")
    assert slot_draft.check_binding(b, rule, sd.slots) == []
    always_no = b.model_copy(update={"also": [Cond(slot="tx.transfusion", op="in", values=[])]})
    assert "항상 no" in slot_draft.check_binding(always_no, rule, sd.slots)[0]


def test_dictionary_rejects_unknown_also_slot():
    sd = _composite_dictionary()
    slots = {k: v for k, v in sd.slots.items() if k != "tx.transfusion"}
    with pytest.raises(ValueError, match="tx.transfusion"):
        SlotDictionary(built_from=sd.built_from, slots=slots,
                       bindings=[b.model_copy(update={"status": "reviewed"}) if b.key == "T1:I2" else b for b in sd.bindings])
