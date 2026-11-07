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
    if use == "out_of_scope":  # OR 경로 보존용으로 남긴 코호트 전용 범위 밖 기준: 연결하지 않음 (미연결 -> 라벨 없음)
        return Binding(**{**base, "note": f"{NOTE}; 범위 밖 코호트 전용 기준 - OR 경로 보존용으로 저장소에 남김"},
                       status="needs_review")
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
    # 남기는 기준: 범위 안 + excluded(엔진은 R7 현행대로 처리) + 범위 밖이라도 코호트 전용 기준.
    # 코호트 전용 기준을 빼면 엔진(Matcher)이 그 코호트를 경로로 보지 않아, 다른 코호트만 불통과인 환자를
    # 부적격 확정함 (원 프로토콜에서는 그 경로가 살아 있음). 남긴 범위 밖 기준은 연결하지 않아 미확인으로 남음
    or_keep = {(r["trial_id"], r["criterion_id"]) for r in rows if r["scope"] == "out_of_scope" and r["cohort"]}
    keep = {(r["trial_id"], r["criterion_id"]) for r in rows
            if r["in_scope"] == "yes" or r["scope"] == "excluded"} | or_keep
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
                                   "kept_out_of_scope_for_or_path": sorted(r_ for (t_, r_) in or_keep if t_ == t),
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


SOURCE_MAP = INPUTS / "source_map.json"  # data/nsclc/cohort/source_map.json 사본 + sex (원본 누락)
CLAIMS = ["합성 환자 + 실제 프로토콜 6개의 축소 범위 기준. 정책 간 상대 비교 전용",
          "규칙 GT(독립): 검수표 + 환자 값으로만 계산. 임상 정답 아님, 임상 검수 미완료",
          "범위 밖 기준은 평가하지 않음 -> 적격 라벨은 '축소 범위 안에서'의 뜻 (전체 프로토콜 적격 아님)",
          "정확도·임상 성능 표현 금지"]
CONTENT_FILES = ("patients.json", "pairs.csv", "rule_gt.csv", "pair_gt.csv", "label_summary.json")


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def sim_cohort(c: Cohort, sd: SlotDictionary):
    """runner(#20)가 읽는 시뮬레이터 코호트. 시험은 평가용 축소 저장소에서 읽음 (CLI 기본값 data/parsed가 아님)."""
    from ctrec.sim import from_generator as FG
    trials = FG.load_trials(list(c.public.trials), STORE, NSCLC / "trials")
    return FG.to_sim_cohort(c, sd, FG.load_source_map(SOURCE_MAP), trials)


def hidden_case(slot: str, smap) -> str:
    """숨긴 값을 어떻게 확보할 수 있는가 (응답 모델의 모름·무응답·연락 불가는 실행 때 정해짐)."""
    from ctrec.sim.cohort import PATIENT_KNOWN_FIELDS
    m = smap.slots[slot]
    patient_knows = any(f in PATIENT_KNOWN_FIELDS for f in m.fields)
    asks = "patient_interview" in (m.sources or [])
    if slot.startswith("lab."):
        return "lab_or_patient" if patient_knows and asks else "lab_report_only"
    if patient_knows and asks:
        return "patient_may_answer"  # 환자 답변 가능 (응답 모델 가정: 모름 0.2·연락 불가 0.05) + 문서
    return "document_only"  # 환자가 모르는 항목 (병리·유전자·진단 등) -> 결과지·기록으로만


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def cmd_gt(_args=None) -> int:
    from ctrec.cohort import gt as GT  # 독립 GT (ctrec 다른 모듈을 import하지 않음)
    from ctrec.sim import from_generator as FG
    c = Cohort.model_validate(json.loads(GENERATED.read_text(encoding="utf-8")))
    cfg, sd, cat, parsed = gen.load_inputs(CONFIG)
    if c.config_hash != gen.config_hash(cfg, sd, cat):
        raise SystemExit("generated/cohort.json이 현재 입력과 다름 - generate부터 다시 실행")
    crit = GT.load_criteria(REVIEW, {t: list(p.cohorts) for t, p in parsed.items()})
    sim = sim_cohort(c, sd)
    alias = sim.aliases()
    smap = FG.load_source_map(SOURCE_MAP)
    patients, pairs, rule_rows, pair_rows = [], [], [], []
    by_trial: dict[str, Counter] = {}
    cases = Counter()
    for p in c.public.patients:
        hidden = c.oracle.hidden.get(p.patient_id, {})
        values = {**p.known, **hidden}
        hc = {s: hidden_case(s, smap) for s in sorted(hidden)}
        cases.update(hc.values())
        patients.append({"patient_id": p.patient_id, "sim_alias": alias[p.patient_id], "candidate_trials": p.candidate_trials,
                         "public_known": p.known, "hidden_slots": sorted(hidden), "hidden_case": hc,
                         "true_values": values})
        for t in p.candidate_trials:
            pk, sk = f"{p.patient_id}|{t}", f"{alias[p.patient_id]}|{t}"
            res = GT.pair_gt(t, values, crit)
            by_trial.setdefault(t, Counter())[res.label] += 1
            unknown = sum(c.public.initial_holds[f"{pk}|{r.rule_id}"] == "unknown" for r in c.public.rules[t])
            pairs.append({"patient_id": p.patient_id, "trial_id": t, "sim_pair_key": sk,
                          "generator_label": c.oracle.pair_label[pk],
                          "generator_reasons": "|".join(c.oracle.label_reasons.get(pk, [])),
                          "generator_bound_result": c.oracle.bound_result[pk], "initial_unknown_rules": unknown})
            for rr in res.rules:
                cr = rr.criterion
                rule_rows.append({"patient_id": p.patient_id, "trial_id": t, "sim_pair_key": sk,
                                  "criterion_id": cr.criterion_id, "kind": cr.kind, "category": cr.category,
                                  "cohort": cr.cohort or "", "gt_use": cr.gt_use, "status": rr.status, "basis": rr.basis,
                                  "verification": cr.verification, "clinical_review": "미완료"})
            pair_rows.append({"patient_id": p.patient_id, "trial_id": t, "sim_pair_key": sk, "gt_label": res.label,
                              "runner_label": GT.RUNNER_LABEL[res.label], "path": res.path or "",
                              "reasons": "|".join(res.reasons),
                              **{f"n_{s.lower()}": res.count(s) for s in GT.RULE_STATUSES},
                              "path_labels": json.dumps({str(k): v[0] for k, v in res.path_labels.items()},
                                                        ensure_ascii=False),
                              "excluded_criteria": "|".join(crit.excluded.get(t, [])),
                              "out_of_scope_criteria": len(crit.out_of_scope.get(t, [])),
                              "generator_label": c.oracle.pair_label[pk], "clinical_review": "미완료"})
    _dump(EVAL / "patients.json", {"note": "합성 환자 (실제 환자 아님). true_values는 정답 값(oracle) - 정책에 주지 말 것",
                                   "cohort_id": c.cohort_id, "patients": patients})
    _write_csv(EVAL / "pairs.csv", pairs)
    _write_csv(EVAL / "rule_gt.csv", rule_rows)
    _write_csv(EVAL / "pair_gt.csv", pair_rows)
    overall = Counter(r["gt_label"] for r in pair_rows)
    summary = {"pairs": len(pair_rows), "gt_labels": {k: overall.get(k, 0) for k in GT.PAIR_LABELS},
               "gt_labels_by_trial": {t: {k: v.get(k, 0) for k in GT.PAIR_LABELS} for t, v in sorted(by_trial.items())},
               "rule_status": dict(sorted(Counter(r["status"] for r in rule_rows).items())),
               "hidden_value_cases": dict(sorted(cases.items())),
               "generator_labels": dict(sorted(Counter(r["generator_label"] for r in pairs).items())),
               "eligible_family_present": overall.get(GT.CONFIRMED_ELIGIBLE, 0) + overall.get(GT.PENDING_EXPERT_ELIGIBLE, 0) > 0,
               "ineligible_present": overall.get(GT.INELIGIBLE, 0) > 0,
               "confirmed_eligible_note": "범위 안 판단 기준이 없는 경로에서만 나옴. 6개 시험 모두 판단 기준이 있어 0건이 정상"}
    _dump(EVAL / "label_summary.json", summary)
    content = {n: _sha(EVAL / n) for n in CONTENT_FILES}
    inputs = {pth.relative_to(ROOT).as_posix(): _sha(pth)
              for pth in [REVIEW, NSCLC / "review" / "overrides.json", CONFIG, INPUTS / "slot_dictionary.json",
                          INPUTS / "source_catalog.json", SOURCE_MAP, GENERATED, *sorted(STORE.glob("*.json"))]}
    _dump(EVAL / "manifest.json", {
        "dataset": "nsclc_eval_v1", "scope": SCOPE_ID, "gt_kind": GT.GT_KIND, "gt_version": GT.GT_VERSION,
        "verification": {"data": "서연(데이터) 검증 완료", "clinical": "임상 검수 미완료"},
        "cohort_id": c.cohort_id, "scenario_id": c.scenario_id, "seed": c.seed, "base_seed": cfg.base_seed,
        "config_hash": c.config_hash, "generator_version": gen.GENERATOR_VERSION, "reference_date": c.reference_date,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "build_commit": _git_commit(), "build_command": "python scripts/build_nsclc_eval.py all",
        "sim": {"cohort_id": sim.cohort_id, "cohort_hash": sim.digest(), "store_hash": sim.store_hash(),
                "salt": sim.salt, "pair_key": "sim_pair_key = <sim 별칭>|<시험> (RunResult.final 쌍 키와 같음)"},
        "inputs_sha256": inputs, "content_sha256": content,
        "content_hash": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
        "label_summary": summary["gt_labels"], "claims": CLAIMS})
    print("GT:", summary["gt_labels"])
    for t, v in summary["gt_labels_by_trial"].items():
        print(f"  {t}: {v}")
    print("rule status:", summary["rule_status"], "hidden cases:", summary["hidden_value_cases"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
