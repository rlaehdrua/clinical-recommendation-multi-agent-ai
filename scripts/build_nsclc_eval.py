"""NSCLC 축소 범위 평가셋 만들기 (3·4단계). 생성 -> 검토 -> 정답을 단계별 파일로 나눕니다.

  python scripts/build_nsclc_eval.py inputs    # 검수표 -> 평가 입력 (축소 시험 저장소·슬롯 사전·카탈로그)
  python scripts/build_nsclc_eval.py generate  # 생성기 실행 + validate + 2회 생성 해시 비교 -> generated/cohort.json
  python scripts/build_nsclc_eval.py gt        # 독립 규칙 GT -> patients·pairs·rule_gt·pair_gt·manifest
  python scripts/build_nsclc_eval.py all

단계와 파일 (data/nsclc/eval/)
- 검토(사람): data/nsclc/review/criteria_verification.csv - 서연(데이터) 검증, 임상 검수 미완료
- 입력(이 스크립트가 검토표에서 만듦): inputs/trial_store/<시험>.json, inputs/slot_dictionary.json,
  inputs/source_catalog.json(손으로 씀), inputs/generator_config.json(손으로 씀), inputs/lab_reference.json(손으로 씀)
- 생성(생성기): generated/cohort.json - 합성 환자 값 + 생성기 자체 라벨 (GT 아님)
- 정답(독립 GT, ctrec.cohort.gt): rule_gt.csv, pair_gt.csv. 생성기 truth·엔진을 쓰지 않고 검수표 + 환자 값만 씀
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ctrec.cohort import generate as gen  # noqa: E402
from ctrec.cohort.spec import CORE_SOURCE, Binding, Cohort, Cond, SlotDictionary, SlotSpec, is_expert_slot  # noqa: E402
from ctrec.cohort.validate import validate  # noqa: E402

NSCLC = ROOT / "data" / "nsclc"
REVIEW = NSCLC / "review" / "criteria_verification.csv"
EVAL = NSCLC / "eval"
INPUTS = EVAL / "inputs"
STORE = INPUTS / "trial_store"
CONFIG = INPUTS / "generator_config.json"
GENERATED = EVAL / "generated" / "cohort.json"
SCOPE_ID = "nsclc_reduced_v1"
NOTE = "데이터 검증 완료 / 임상 검수 미완료 (data/nsclc/review/criteria_verification.csv)"

# 슬롯 사양 (축소 범위). 값 목록은 data/nsclc/cohort/slot_candidates.csv value_range에서, 바꾼 곳은 사유를 적음
S = SlotSpec
SLOTS: dict[str, SlotSpec] = {s.slot_id: s for s in [
    S(slot_id="age", kind="numeric", category="demographic", source=CORE_SOURCE, unit="years"),
    S(slot_id="sex", kind="categorical", category="demographic", source=CORE_SOURCE, values=["male", "female"]),
    S(slot_id="ecog", kind="numeric", category="performance_status", source="S_chart"),
    # NSCLC-NOS는 비편평 해당 여부가 불확실해 값에서 뺌 (표현 검토 expression_review.csv)
    S(slot_id="dx.histology", kind="categorical", category="diagnosis", source="S_path",
      values=["adenocarcinoma", "nonsquamous_other", "squamous", "mixed_sclc"]),
    # III(A/B/C)를 나눔 (NCT07416058 IIIB-IVB). recurrent는 병기와 축이 달라 값에서 뺌 (표현 검토)
    S(slot_id="dx.stage", kind="categorical", category="diagnosis", source="S_chart",
      values=["IIIA", "IIIB", "IIIC", "IVA", "IVB"]),
    S(slot_id="bm.driver_alteration", kind="categorical", category="biomarker", source="S_path",
      values=["none", "egfr_ex19del_L858R", "egfr_uncommon", "egfr_ex20ins", "alk", "ros1", "ret", "kras_g12c",
              "braf_v600", "her2_ex20ins", "met_ex14", "ntrk", "nrg1", "not_tested"]),
    S(slot_id="tx.prior_platinum", kind="categorical", category="treatment_history", source="S_chart", values=["yes", "no"]),
    S(slot_id="tx.prior_pd1_pdl1", kind="categorical", category="treatment_history", source="S_chart",
      values=["none", "durvalumab_consolidation", "anti_pd1_pdl1", "ici_other"]),
    S(slot_id="tx.prior_docetaxel", kind="categorical", category="treatment_history", source="S_chart", values=["yes", "no"]),
    S(slot_id="tx.prior_il12", kind="categorical", category="treatment_history", source="S_chart", values=["yes", "no"]),
    S(slot_id="tx.other_investigational_4w", kind="categorical", category="treatment_history", source="S_chart",
      values=["yes", "no"]),
    # 수치(일) 슬롯을 시험 기준 경계로 나눈 구간 (NCT07416058 I6: 14일)
    S(slot_id="tx.days_since_last_anticancer", kind="categorical", category="treatment_history", source="S_chart",
      values=["lt_14d", "ge_14d"]),
    # 수치(일) 슬롯을 시험 기준 경계로 나눈 구간 (NCT06216301 E4: 21일, NCT07416058 E12: 28일)
    S(slot_id="proc.days_since_major_surgery", kind="categorical", category="procedure", source="S_chart",
      values=["lt_21d", "21_27d", "ge_28d_or_none"]),
    S(slot_id="repro.pregnant_or_breastfeeding", kind="categorical", category="reproductive", source="S_phone",
      values=["yes", "no"]),
    S(slot_id="lab.tsh_normal", kind="categorical", category="lab", source="S_lab", values=["yes", "no"]),
    S(slot_id="med.live_vaccine_30d", kind="categorical", category="medication", source="S_med", values=["yes", "no"]),
    S(slot_id="med.anticoagulant", kind="categorical", category="medication", source="S_med",
      values=["none", "warfarin", "other_therapeutic"]),
    S(slot_id="cm.organ_transplant", kind="categorical", category="comorbidity", source="S_chart", values=["yes", "no"]),
    # 임상의 판단 슬롯 (spec.is_expert_slot): 값·정답을 만들지 않음. 값 목록은 연결 형식용
    S(slot_id="judge.life_expectancy", kind="categorical", category="other", source="S_md", values=["meets", "not_meets"]),
    S(slot_id="judge.investigator_unsuitable", kind="categorical", category="other", source="S_md",
      values=["suitable", "unsuitable"]),
    S(slot_id="cm.cardiac", kind="categorical", category="comorbidity", source="S_chart",
      values=["none", "controlled_history", "significant_uncontrolled"]),
    S(slot_id="dx.disease_setting", kind="categorical", category="diagnosis", source="S_chart",
      values=["newly_diagnosed_advanced", "progressed_after_crt_durvalumab", "progressed_after_1L", "recurrent"]),
    S(slot_id="tx.recent_radiotherapy", kind="categorical", category="treatment_history", source="S_chart",
      values=["none", "palliative_non_cns", "thoracic_gt30gy_6m", "any_2w"]),
    S(slot_id="other.tumor_tissue", kind="categorical", category="other", source="S_chart",
      values=["archival_adequate", "fresh_biopsy_willing", "insufficient"]),
    # 검사 수치 4개 (팀 결정 3). 파싱된 6개 시험에는 기준값이 있는 기준이 없어 연결 없음. 단위·참고 범위는 lab_reference.json
    S(slot_id="lab.anc", kind="numeric", category="lab", source="S_lab", unit="10^9/L"),
    S(slot_id="lab.bilirubin_uln", kind="numeric", category="lab", source="S_lab", unit="x ULN"),
    S(slot_id="lab.creatinine_clearance", kind="numeric", category="lab", source="S_lab", unit="mL/min"),
    S(slot_id="lab.hemoglobin", kind="numeric", category="lab", source="S_lab", unit="g/dL"),
]}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False) + "\n", encoding="utf-8")


def _review_rows() -> list[dict]:
    with open(REVIEW, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def binding_of(r: dict) -> Binding:
    base = dict(trial_id=r["trial_id"], rule_id=r["criterion_id"], kind=r["kind"], category=r["category"],
                text=r["parsed_text"], required_evidence=r["required_evidence"] or "clinical_record", note=NOTE)
    use = r["gt_use"]
    if use == "excluded":
        return Binding(**base, status="excluded")
    if use in ("unbound", "region_conditional"):  # 범위 안이지만 정답을 만들지 않음 -> 생성기 라벨 없음, GT NOT_EVALUATED
        return Binding(**{**base, "note": f"{NOTE}; gt_use={use}: {r['issue']}"}, status="needs_review")
    if use == "needs_expert":
        slot = r["expert_slot"]
        return Binding(**base, status="reviewed", slot=slot, truth="categorical", op="in", values=[SLOTS[slot].values[0]])
    if r["scope"] == "in_rule_engine":
        return Binding(**base, status="reviewed", slot=r["slot"], truth="engine")
    values = r["values"].split("|")
    also, combine = [], "all"
    if r["applies_if"]:  # "sex in female": 해당하는 환자에게만 적용
        slot, vals = r["applies_if"].split(" in ")
        vals = vals.split("|")
        if r["kind"] == "inclusion":  # 해당 없으면 충족 (해당 없음 = 통과)
            also, combine = [Cond(slot=slot, op="not_in", values=vals)], "any"
        else:  # 제외 기준은 해당할 때만 성립
            also, combine = [Cond(slot=slot, op="in", values=vals)], "all"
    return Binding(**base, status="reviewed", slot=r["slot"], truth="categorical", op=r["op"], values=values,
                   also=also, combine=combine)


def cmd_inputs(_args=None) -> int:
    rows = _review_rows()
    keep = {(r["trial_id"], r["criterion_id"]) for r in rows if r["in_scope"] == "yes" or r["scope"] == "excluded"}
    expert = {(r["trial_id"], r["criterion_id"]) for r in rows if r["gt_use"] == "needs_expert"}
    for t in sorted({r["trial_id"] for r in rows}):
        src = NSCLC / "parsed" / f"{t}.json"
        rec = json.loads(src.read_text(encoding="utf-8"))
        rules, removed, flagged = [], [], []
        for rule in rec["parsed"]["rules"]:
            if (t, rule["rule_id"]) not in keep:
                removed.append(rule["rule_id"])
                continue
            if (t, rule["rule_id"]) in expert and not rule["underspecified"]:
                rule = {**rule, "underspecified": True}  # 판단 기준 표시 (엔진이 담당의 판단으로 보냄, #37 mark_expert와 같음)
                flagged.append(rule["rule_id"])
            rules.append(rule)
        out = {"format_version": rec.get("format_version"), "trial_id": t, "trial_hash": rec["trial_hash"],
               "model": rec.get("model"), "compiled_at": rec.get("compiled_at"),
               "parsed": {**rec["parsed"], "rules": rules},
               "rule_meta": {k: v for k, v in (rec.get("rule_meta") or {}).items() if k in {x["rule_id"] for x in rules}},
               "eval_derivation": {"scope": SCOPE_ID, "derived_from": f"data/nsclc/parsed/{t}.json",
                                   "derived_from_sha256": _sha(src), "review": "data/nsclc/review/criteria_verification.csv",
                                   "kept": [x["rule_id"] for x in rules], "removed_out_of_scope": removed,
                                   "underspecified_set_for_expert": flagged,
                                   "note": "축소 범위 평가용 사본. 원본 파싱본은 바꾸지 않음. 범위 밖 기준을 뺐으므로 "
                                           "이 저장소로 낸 '적격'은 전체 프로토콜 적격이 아님"}}
        _dump(STORE / f"{t}.json", out)
    bindings = [binding_of(r) for r in rows if (r["trial_id"], r["criterion_id"]) in keep]
    used = {s for b in bindings for s in b.slots_used()}
    slots = {k: v for k, v in SLOTS.items() if k in used or k.startswith("lab.")}
    sd = SlotDictionary(version=1, built_from=sorted({b.trial_id for b in bindings}), slots=slots, bindings=bindings)
    bad = [b.key for b in bindings if b.status == "reviewed" and b.slot and is_expert_slot(b.slot)
           and (b.trial_id, b.rule_id) not in expert]
    if bad:  # 판단 슬롯을 쓰는 연결은 검수표에서도 판단 기준이어야 함
        raise SystemExit(f"검수표의 판단 기준 표시와 판단 슬롯이 다름: {bad}")
    _dump(INPUTS / "slot_dictionary.json", sd.model_dump(mode="json"))
    c = Counter(b.status for b in bindings)
    print(f"trial_store={len(list(STORE.glob('*.json')))} bindings={dict(c)} slots={len(slots)}")
    return 0


def _generate():
    cfg, sd, cat, parsed = gen.load_inputs(CONFIG)
    cohorts = gen.generate_all(cfg, sd, cat, parsed)
    return cfg, sd, cat, parsed, cohorts


def _cohort_hash(c: Cohort) -> str:
    return hashlib.sha256(json.dumps(c.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cmd_generate(_args=None) -> int:
    cfg, sd, cat, parsed, cohorts = _generate()
    if len(cohorts) != 1:
        raise SystemExit(f"평가셋은 코호트 1개 (설정 {len(cohorts)}개)")
    c = cohorts[0]
    problems = validate(c, sd, parsed)
    if problems:
        print(f"[FAIL] validate {len(problems)}개 문제, 예: {problems[:5]}", file=sys.stderr)
        return 1
    again = _generate()[4][0]  # 같은 설정 2회 생성 -> 같은 해시여야 함 (결정성)
    h1, h2 = _cohort_hash(c), _cohort_hash(again)
    if h1 != h2:
        print(f"[FAIL] 같은 설정 2회 생성 해시가 다름 {h1} != {h2}", file=sys.stderr)
        return 1
    _dump(GENERATED, c.model_dump(mode="json"))
    print(f"cohort={c.cohort_id} config_hash={c.config_hash} seed={c.seed} patients={len(c.public.patients)} "
          f"pairs={len(c.oracle.pair_label)} validate=0 problems, sha256 run1=run2={h1[:16]}")
    print("generator labels (GT 아님):", dict(Counter(c.oracle.pair_label.values())))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("inputs", "generate", "gt", "all"):
        sub.add_parser(name)
    a = ap.parse_args()
    if a.cmd == "all":
        return cmd_inputs() or cmd_generate() or cmd_gt()
    return {"inputs": cmd_inputs, "generate": cmd_generate, "gt": lambda: cmd_gt()}[a.cmd]()


def cmd_gt(_args=None) -> int:  # 4단계에서 채움
    raise SystemExit("gt 단계는 4단계에서 구현")


if __name__ == "__main__":
    sys.exit(main())
