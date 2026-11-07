"""NSCLC 축소 범위 평가셋 + 독립 규칙 GT (data/nsclc/eval, src/ctrec/cohort/gt.py)."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_nsclc_eval as B  # noqa: E402
from ctrec.cohort import generate as gen  # noqa: E402
from ctrec.cohort import gt as GT  # noqa: E402
from ctrec.cohort.spec import Cohort, is_expert_slot  # noqa: E402
from ctrec.cohort.truth import passes  # noqa: E402

GT_SRC = ROOT / "src" / "ctrec" / "cohort" / "gt.py"


def _rows(name: str) -> list[dict]:
    with open(B.EVAL / name, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _review() -> list[dict]:
    with open(B.REVIEW, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _cohort() -> Cohort:
    return Cohort.model_validate(json.loads(B.GENERATED.read_text(encoding="utf-8")))


def _crit() -> GT.Criteria:
    _, _, _, parsed = gen.load_inputs(B.CONFIG)
    return GT.load_criteria(B.REVIEW, {t: list(p.cohorts) for t, p in parsed.items()})


def C(cid="X1", kind="inclusion", category="lab", cohort=None, gt_use="bindable", slot="s", op="in", values=("yes",),
      applies_if=None, expert_slot=None, trial="T"):
    return GT.Criterion(trial_id=trial, criterion_id=cid, kind=kind, category=category, cohort=cohort, gt_use=gt_use,
                        slot=slot, op=op, values=tuple(values), applies_if=applies_if, expert_slot=expert_slot,
                        verification="일치")


# ---------------------------------------------------------------------------
# 독립성: 엔진·생성기 truth·시뮬레이터를 쓰지 않음
# ---------------------------------------------------------------------------

def test_gt_module_imports_no_ctrec_module():
    tree = ast.parse(GT_SRC.read_text(encoding="utf-8"))
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {("." * n.level) + (n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m.startswith(".") or m.startswith("ctrec") for m in mods), mods


def test_importing_gt_does_not_load_engine_or_rule_engine():
    code = ("import sys, ctrec.cohort.gt; "
            "print(','.join(sorted(m for m in sys.modules if m.startswith('ctrec'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}).stdout.strip()
    assert set(out.split(",")) == {"ctrec", "ctrec.cohort", "ctrec.cohort.gt"}, out


# ---------------------------------------------------------------------------
# 규칙 수준
# ---------------------------------------------------------------------------

def test_rule_status_kinds():
    assert GT.rule_status(C(gt_use="needs_expert", expert_slot="judge.life_expectancy"), {})[0] == GT.NEEDS_EXPERT
    assert GT.rule_status(C(gt_use="unbound"), {"s": "yes"})[0] == GT.NOT_EVALUATED  # 값이 있어도 판정 안 함
    assert GT.rule_status(C(), {"s": "yes"})[0] == GT.PASS
    assert GT.rule_status(C(), {"s": "no"})[0] == GT.FAIL
    assert GT.rule_status(C(kind="exclusion"), {"s": "yes"})[0] == GT.FAIL
    assert GT.rule_status(C(kind="exclusion"), {"s": "no"})[0] == GT.PASS
    num = C(slot="age", op="<=", values=("75",), category="demographic")
    assert GT.rule_status(num, {"age": 75})[0] == GT.PASS and GT.rule_status(num, {"age": 76})[0] == GT.FAIL
    assert GT.rule_status(C(slot="sex", op="==", values=("all",)), {"sex": "female"})[0] == GT.PASS
    assert GT.rule_status(C(), {})[0] == GT.NOT_EVALUATED  # 값이 없으면 추측하지 않음


def test_region_conditional_age_is_not_evaluated_below_the_regional_threshold():
    """NCT06216301 I1 '≥22 in the USA': 국가를 모르므로 22세 미만은 판정하지 않음 (거짓 부적격 방지)."""
    r = C(slot="age", op=">=", values=("22",), gt_use="region_conditional", category="demographic")
    assert GT.rule_status(r, {"age": 30})[0] == GT.PASS
    assert GT.rule_status(r, {"age": 20})[0] == GT.NOT_EVALUATED


def test_applies_if_gives_not_applicable_not_pass():
    r = C(slot="repro", values=("no",), applies_if=("sex", ("female",)), category="reproductive")
    assert GT.rule_status(r, {"sex": "male", "repro": "yes"})[0] == GT.NOT_APPLICABLE
    assert GT.rule_status(r, {"sex": "female", "repro": "yes"})[0] == GT.FAIL


# ---------------------------------------------------------------------------
# 쌍 수준
# ---------------------------------------------------------------------------

def _crit_of(rules, cohorts=None):
    return GT.Criteria(rules={"T": list(rules)}, cohorts={"T": list(cohorts or [])})


def test_pair_expert_rule_gives_pending_not_confirmed():
    crit = _crit_of([C("I1", slot="a"), C("I2", gt_use="needs_expert", expert_slot="judge.x")])
    assert GT.pair_gt("T", {"a": "yes"}, crit).label == GT.PENDING_EXPERT_ELIGIBLE
    assert GT.pair_gt("T", {"a": "yes"}, _crit_of([C("I1", slot="a")])).label == GT.CONFIRMED_ELIGIBLE
    assert GT.pair_gt("T", {"a": "no"}, crit).label == GT.INELIGIBLE  # 판단 기준이 남아도 불통과면 부적격


def test_pair_not_evaluated_rule_blocks_eligible_labels():
    crit = _crit_of([C("I1", slot="a"), C("I2", gt_use="unbound")])
    assert GT.pair_gt("T", {"a": "yes"}, crit).label == GT.NOT_EVALUATED
    assert GT.pair_gt("T", {"a": "no"}, crit).label == GT.INELIGIBLE


def test_all_not_applicable_or_demographic_only_is_not_eligible():
    na = C("I1", slot="repro", values=("no",), applies_if=("sex", ("female",)), category="reproductive")
    age = C("I2", slot="age", op=">=", values=("18",), category="demographic")
    res = GT.pair_gt("T", {"sex": "male", "repro": "no", "age": 40}, _crit_of([na, age]))
    assert res.label == GT.NOT_EVALUATED and res.reasons == ("no_substantive_pass",)


def test_or_paths_ineligible_only_when_every_path_fails_and_substantive_path_first():
    common = C("I1", slot="age", op=">=", values=("18",), category="demographic")
    a = C("A1", slot="a", cohort="A")
    b = C("B1", slot="b", cohort="B", kind="exclusion")
    crit = _crit_of([common, a, b], cohorts=["A", "B", "C"])  # C: 범위 안 전용 기준 없음 (실질 경로 아님)
    one = GT.pair_gt("T", {"age": 40, "a": "no", "b": "no"}, crit)
    assert one.label == GT.CONFIRMED_ELIGIBLE and one.path == "B"  # A만 불통과 -> B로 적격
    none = GT.pair_gt("T", {"age": 40, "a": "no", "b": "yes"}, crit)
    assert none.label == GT.NOT_EVALUATED and none.path == "C"  # A·B 불통과, C는 실질 경로가 아니라 판정 안 함
    assert none.reasons == ("non_substantive_path",)
    assert GT.pair_gt("T", {"age": 10, "a": "yes", "b": "no"}, crit).label == GT.INELIGIBLE  # 공통 불통과 = 모든 경로


def test_score_counts_expert_ready_against_independent_gt():
    gt = {"p1|T": GT.PENDING_EXPERT_ELIGIBLE, "p2|T": GT.PENDING_EXPERT_ELIGIBLE, "p3|T": GT.INELIGIBLE,
          "p4|T": GT.NOT_EVALUATED}
    st = {"p1|T": "EXPERT_REVIEW", "p2|T": "INFO_MISSING", "p3|T": "CONFIRMED_INELIGIBLE", "p4|T": "CONFIRMED_INELIGIBLE"}
    s = GT.score(st, gt)
    assert s["expert_ready_with_gt_pending"] == 1 and s["gt_pending_expert_eligible"] == 2
    assert s["confirmed_ineligible_with_gt_ineligible"] == 1 and s["confirmed_ineligible_gt_not_ineligible"] == 1
    with pytest.raises(ValueError):
        GT.score({}, gt)


# ---------------------------------------------------------------------------
# 검수표·평가셋
# ---------------------------------------------------------------------------

def test_review_table_marks_data_verified_and_clinical_review_pending():
    rows = _review()
    assert len(rows) == 160 and {r["trial_id"] for r in rows} == set(B.gen.load_inputs(B.CONFIG)[0].clusters["nsclc_reduced"].trials)
    assert all(r["clinical_review"] == "미완료" for r in rows)
    assert all(r["verifier"] == "서연(데이터)" for r in rows if r["in_scope"] == "yes" or r["scope"] == "excluded")
    assert all(r["result"] in ("일치", "불일치", "모호") for r in rows if r["in_scope"] == "yes")
    region = [r for r in rows if r["gt_use"] == "region_conditional"]
    assert [(r["trial_id"], r["criterion_id"]) for r in region] == [("NCT06216301", "I1")]
    for r in rows:
        if r["gt_use"] == "needs_expert":
            assert is_expert_slot(r["expert_slot"]), r
        elif r["slot"]:
            assert not is_expert_slot(r["slot"]) or r["gt_use"] == "unbound", r


def test_protocol_status_covers_all_eight_protocols():
    with open(B.NSCLC / "review" / "protocol_status.csv", encoding="utf-8-sig", newline="") as f:
        rows = {r["trial_id"]: r for r in csv.DictReader(f)}
    assert len(rows) == 8
    assert all(rows[t]["parsed"].startswith("파싱 미완료") for t in ("NCT04892953", "NCT05255302"))


def test_generated_cohort_is_reproducible_from_inputs():
    cfg, sd, cat, parsed = gen.load_inputs(B.CONFIG)
    again = gen.generate_all(cfg, sd, cat, parsed)
    assert len(again) == 1
    assert B._cohort_hash(again[0]) == B._cohort_hash(_cohort())  # 같은 설정 2회 생성 -> 같은 해시


def test_pair_gt_file_matches_gt_module_and_excludes_excluded_criteria():
    c, crit = _cohort(), _crit()
    rows = {(r["patient_id"], r["trial_id"]): r for r in _rows("pair_gt.csv")}
    assert len(rows) == sum(len(p.candidate_trials) for p in c.public.patients)
    excluded = {(t, x) for t, xs in crit.excluded.items() for x in xs}
    rule_keys = {(r["trial_id"], r["criterion_id"]) for r in _rows("rule_gt.csv")}
    assert not (rule_keys & excluded)  # excluded는 규칙 GT를 만들지 않음 (목록만)
    for p in c.public.patients:
        values = {**p.known, **c.oracle.hidden.get(p.patient_id, {})}
        for t in p.candidate_trials:
            r = rows[(p.patient_id, t)]
            assert r["gt_label"] == GT.pair_gt(t, values, crit).label
            assert r["runner_label"] == GT.RUNNER_LABEL[r["gt_label"]]
            assert r["excluded_criteria"] == "|".join(crit.excluded.get(t, []))


def test_manifest_content_hashes_match_files():
    m = json.loads((B.EVAL / "manifest.json").read_text(encoding="utf-8"))
    for name, h in m["content_sha256"].items():
        assert hashlib.sha256((B.EVAL / name).read_bytes()).hexdigest() == h, name
    assert m["config_hash"] == _cohort().config_hash
    assert m["verification"] == {"data": "서연(데이터) 검증 완료", "clinical": "임상 검수 미완료"}
    assert m["gt_kind"] == GT.GT_KIND


def test_label_distribution_has_eligible_family_and_ineligible():
    labels = Counter(r["gt_label"] for r in _rows("pair_gt.csv"))
    assert labels[GT.PENDING_EXPERT_ELIGIBLE] > 0 and labels[GT.INELIGIBLE] > 0 and labels[GT.NOT_EVALUATED] > 0
    # 6개 시험 모두 범위 안 판단 기준이 있어 확정 적격은 구조적으로 0 (정상)
    crit = _crit()
    assert all(any(r.gt_use == "needs_expert" for r in rs) for rs in crit.rules.values())
    assert labels[GT.CONFIRMED_ELIGIBLE] == 0


def test_independent_gt_agrees_with_generator_truth_on_bound_rules():
    """두 구현(생성기 truth: 공용 rule_engine·holds3 / GT: 자체 비교)이 연결된 기준에서 같은 결론인지 (일관성 점검)."""
    c, crit = _cohort(), _crit()
    by = {(r["patient_id"], r["trial_id"], r["criterion_id"]): r["status"] for r in _rows("rule_gt.csv")}
    checked = 0
    for t, rules in crit.rules.items():
        for cr in rules:
            if cr.gt_use != "bindable":
                continue
            for p in c.public.patients:
                if t not in p.candidate_trials:
                    continue
                h = c.oracle.truth[f"{p.patient_id}|{t}|{cr.criterion_id}"]
                st = by[(p.patient_id, t, cr.criterion_id)]
                assert (st == GT.FAIL) == (passes(cr.kind, h) is False), (p.patient_id, t, cr.criterion_id, st, h)
                checked += 1
    assert checked > 500


def test_generator_ineligible_label_implies_gt_ineligible():
    for r in _rows("pair_gt.csv"):
        if r["generator_label"] == "INELIGIBLE":
            assert r["gt_label"] == GT.INELIGIBLE, r


def test_eval_store_keeps_or_paths_and_preserves_source():
    """축소 저장소: 원본 파싱본 해시를 남기고, 코호트 전용 범위 밖 기준을 남겨 OR 경로가 사라지지 않음."""
    for f in sorted(B.STORE.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        d = rec["eval_derivation"]
        src = ROOT / d["derived_from"]
        assert hashlib.sha256(src.read_bytes()).hexdigest() == d["derived_from_sha256"]
        orig = json.loads(src.read_text(encoding="utf-8"))["parsed"]
        kept = {r["rule_id"] for r in rec["parsed"]["rules"]}
        for c in orig["cohorts"]:
            if any(r["cohort"] == c for r in orig["rules"]):
                assert any(r["cohort"] == c for r in rec["parsed"]["rules"]), (rec["trial_id"], c)
        assert kept | set(d["removed_out_of_scope"]) == {r["rule_id"] for r in orig["rules"]}


def test_runner_pair_keys_match_eval_set():
    """#20 runner의 시뮬레이터 코호트로 변환되고, 쌍 키가 pair_gt.csv의 sim_pair_key와 같음."""
    c = _cohort()
    _, sd, _, _ = gen.load_inputs(B.CONFIG)
    sim = B.sim_cohort(c, sd)
    alias = sim.aliases()
    keys = {f"{alias[p.key]}|{t}" for p in sim.patients for t in (p.trials or ())}
    assert keys == {r["sim_pair_key"] for r in _rows("pair_gt.csv")}
    m = json.loads((B.EVAL / "manifest.json").read_text(encoding="utf-8"))
    assert m["sim"]["cohort_hash"] == sim.digest() and m["sim"]["store_hash"] == sim.store_hash()
