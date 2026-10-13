"""질문 우선순위 검증용 합성 검증셋 생성 (Phase 4-2). LLM 없이 고정 기준표와 결정적 규칙으로 만듭니다.

개발셋(data/patients/masked/masked_cases.json)은 동점 문제를 발견·분석하는 데 이미 썼으므로, 이 검증셋은 별도로 만듭니다.
- 개발셋의 정답 기준(EXACT·INFERRED로 대응된 규칙)은 숨김 대상에서 제외하고, 환자 사실은 새로 만듭니다.
- 모집 중인 시험만 씁니다(모집 종료 시험에는 확인 질문을 하지 않음). 개발셋이 이미 모집 중인 시험 23개를 모두 쓰므로
  시험 단위로는 겹칩니다(독립성 한계, build_report에 기록).
- 숨김 대상은 선정(I)·제외(E) 기준을 같은 수로 뽑고(가능한 경우), 기준 유형(검사·바이오마커·치료 이력 등)을 기록합니다.
  나이·성별(핵심 사실), 파싱 검증 review 기준, 절차 기준, 원문에 구체 기준이 없는 기준은 숨기지 않습니다.
- 사례 유형:
  single    시험 1개, 숨긴 기준 1개
  shared    같은 구조화 정보(ECOG·같은 검사)를 쓰는 시험 2개, 그 정보를 숨김 (여러 시험에 공통인 정보)
  cohort    코호트가 나뉜 시험의 코호트 전용 기준을 숨김 (OR 경로)
  provisional 숨긴 기준이 있는 시험 + 나이로 불충족인 시험(사실이 문서 검증 전이라 잠정 불충족)
  확정 불충족은 만들지 않습니다: 웹·Session 경로에는 문서 검증 단계가 없어 사실이 document_verified가 되지 않음.
- 숨긴 사실 문장: 구조화 기준이면 기준을 충족하는 값(대역이 읽을 수 있는 형식일 때), LLM 판정 기준이면 식별자만 있는 합성 문장.

    PYTHONPATH=src python scripts/build_validation_set.py [--seed 20261013] [--per-kind 2]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, question_eval as qe  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.agents.recommender import is_open  # noqa: E402
from ctrec.fact_view import _engine_field  # noqa: E402

OUT = config.DATA_DIR / "patients" / "masked_validation"
NO_HIDE = {"demographic", "consent_or_logistics"}
LAB_TEXT = {  # 대역 환자 정보(stub_profiler)가 읽는 형식
    "lab:hemoglobin": ("Hemoglobin", "g/dl", "g/dL"), "lab:platelets": ("Platelets", "x10^9/l", "x10^9/L"),
    "lab:absolute neutrophil count": ("ANC", "x10^9/l", "x10^9/L"), "lab:creatinine": ("Creatinine", "mg/dl", "mg/dL"),
}


def _age_range(p) -> tuple[float, float] | None:
    lo, hi = 0.0, 120.0
    for r in p.rules:
        if (r.field or "").lower() != "age" or not r.structured_evaluable:
            continue
        unit = (r.unit or "years").lower()
        if not unit.startswith("year"):
            return None  # 주·개월 단위 시험은 이 생성기 범위 밖
        v = float(r.value)
        want = r.operator if r.kind == "inclusion" else {">=": "<", ">": "<=", "<=": ">", "<": ">="}.get(r.operator)
        if want in (">=", ">"):
            lo = max(lo, v + (1 if want == ">" else 0))
        elif want in ("<=", "<"):
            hi = min(hi, v - (1 if want == "<" else 0))
    return (lo, hi) if lo <= hi else None


def _sex(p) -> str:
    for r in p.rules:
        if (r.field or "").lower() == "sex" and r.operator == "==" and r.value and r.value.lower() in ("male", "female"):
            return r.value.lower()
    return "female"


def _satisfying(rule) -> str | None:
    """구조화 기준을 충족하는 사실 문장 (대역이 읽을 수 있을 때만)."""
    f = _engine_field(rule)
    if not f or not rule.structured_evaluable or not rule.operator:
        return None
    try:
        v = float(rule.value)
    except (TypeError, ValueError):
        return None
    passes_if = rule.operator if rule.kind == "inclusion" else {">=": "<", ">": "<=", "<=": ">", "<": ">="}.get(rule.operator)
    if f == "ecog":
        val = int(v) if passes_if in (">=", "<=") else int(v) + (1 if passes_if == ">" else -1)
        return f"ECOG performance status is {max(0, min(5, val))}." if 0 <= val <= 5 else None
    if f in LAB_TEXT:
        name, unit_re, unit_txt = LAB_TEXT[f]
        if (rule.unit or "").replace(" ", "").replace("×", "x").lower() != unit_re:
            return None
        val = v * 1.25 if passes_if in (">=", ">") else v * 0.75
        return f"{name} {val:.1f} {unit_txt}."
    return None


def _visible(p, age: float, sex: str, hide: set[str]) -> list[str]:
    lines = [f"The patient is {int(age)} years old.", f"The patient is {sex}."]
    seen = set()
    for r in p.rules:
        f = _engine_field(r)
        if f in hide or f in seen or r.rule_id in hide:
            continue
        s = _satisfying(r)
        if s:
            lines.append(s)
            seen.add(f)
    return lines


def _hideable(p, meta_review, dev_gt: set[str], tid: str) -> list:
    out = []
    for r in p.rules:
        m = matcher._meta(p, r)
        if (f"{tid}:{r.rule_id}" in dev_gt or r.category in NO_HIDE or m.review or r.underspecified
                or (r.field or "").lower() in ("age", "sex")):
            continue
        out.append(r)
    return out


def _hidden_text(tid: str, rule) -> str:
    return _satisfying(rule) or f"Synthetic record item {hashlib.sha256(f'{tid}:{rule.rule_id}'.encode()).hexdigest()[:8]}."


def _case(cid, case_type, trials, visible, targets) -> dict:
    hidden = [{"fact_id": f"H{i + 1}", "text": _hidden_text(t, r), "core": False} for i, (t, r) in enumerate(targets)]
    facts = [{"fact_id": f"V{i + 1}", "text": s, "core": i < 2} for i, s in enumerate(visible)]
    return {"patient_id": cid, "case_type": case_type, "synthetic": True, "split": "validation",
            "candidate_trials": trials, "text": "\n".join(f"- {s}" for s in visible),
            "masked_criteria": [r.text for _, r in targets], "masked_fact_ids": [h["fact_id"] for h in hidden],
            "hidden_facts": hidden, "oracle_facts": facts + hidden, "clue_fact_ids": [],
            "target_meta": [{"rule_key": f"{t}:{r.rule_id}", "kind": r.kind, "category": r.category,
                             "structured": bool(_engine_field(r) and r.structured_evaluable)} for t, r in targets]}


def build(seed: int = 20261013, per_kind: int = 2) -> tuple[list[dict], dict]:
    parsed, trials = load_frozen(), load_trials()
    rng = random.Random(seed)
    dev_cases = qe.load_cases(parsed)
    dev_gt = {k for _, g in dev_cases for k in g.exact_keys() | g.inferred_keys()}
    open_trials = sorted(t for t in parsed if is_open(trials[t]))
    cases, report = [], Counter()
    ranges = {t: _age_range(parsed[t]) for t in open_trials}
    for tid in open_trials:
        p, rng_age = parsed[tid], ranges[tid]
        if rng_age is None:
            report["skipped_age_units"] += 1
            continue
        age = (rng_age[0] + min(rng_age[1], rng_age[0] + 40)) // 2
        pool = _hideable(p, None, dev_gt, tid)
        for kind in ("inclusion", "exclusion"):
            cand = [r for r in pool if r.kind == kind and r.cohort is None]
            for r in rng.sample(cand, min(per_kind, len(cand))):
                vis = _visible(p, age, _sex(p), {r.rule_id, _engine_field(r) or "-"})
                cases.append(_case(f"V-{tid}-{r.rule_id}-single", "single", [tid], vis, [(tid, r)]))
        coh = [r for r in pool if r.cohort is not None]
        for r in rng.sample(coh, min(2, len(coh))):
            vis = _visible(p, age, _sex(p), {r.rule_id, _engine_field(r) or "-"})
            cases.append(_case(f"V-{tid}-{r.rule_id}-cohort", "cohort", [tid], vis, [(tid, r)]))
    # shared: 같은 구조화 정보를 쓰는 시험 쌍 (나이·성별이 함께 맞는 경우)
    by_field: dict[str, list[tuple[str, object]]] = {}
    for tid in open_trials:
        if ranges[tid] is None:
            continue
        for r in _hideable(parsed[tid], None, dev_gt, tid):
            f = _engine_field(r)
            if f and r.structured_evaluable and r.cohort is None:
                by_field.setdefault(f, []).append((tid, r))
    for f, items in sorted(by_field.items()):
        trial_ids = sorted({t for t, _ in items})
        pairs = [(a, b) for i, a in enumerate(trial_ids) for b in trial_ids[i + 1:]
                 if _sex(parsed[a]) == _sex(parsed[b]) and max(ranges[a][0], ranges[b][0]) <= min(ranges[a][1], ranges[b][1])]
        for a, b in rng.sample(pairs, min(3, len(pairs))):
            age = max(ranges[a][0], ranges[b][0])
            targets = [(t, r) for t, r in items if t in (a, b)]
            vis = sorted(set(_visible(parsed[a], age, _sex(parsed[a]), {f})) | set(_visible(parsed[b], age, _sex(parsed[a]), {f})),
                         key=lambda s: (not s.startswith("The patient is"), s))
            cases.append(_case(f"V-{a}-{b}-{f.replace(':', '_').replace(' ', '_')}-shared", "shared", [a, b], vis, targets))
    # provisional: 숨긴 기준이 있는 시험 + 나이로 불충족인 다른 시험
    singles = [c for c in cases if c["case_type"] == "single"]
    for c in rng.sample(singles, min(15, len(singles))):
        tid = c["candidate_trials"][0]
        age = int(c["text"].split(" years old")[0].split()[-1])
        others = [t for t in open_trials if t != tid and ranges[t] and not (ranges[t][0] <= age <= ranges[t][1])]
        if not others:
            continue
        other = rng.choice(others)
        cases.append({**c, "patient_id": c["patient_id"].replace("-single", f"-prov-{other}"), "case_type": "provisional",
                      "candidate_trials": [tid, other]})
    ids = [c["patient_id"] for c in cases]
    assert len(ids) == len(set(ids))
    kinds = Counter(m["kind"] for c in cases for m in c["target_meta"])
    cats = Counter(m["category"] for c in cases for m in c["target_meta"])
    summary = {
        "seed": seed, "cases": len(cases), "case_types": dict(Counter(c["case_type"] for c in cases)),
        "target_kinds": dict(kinds), "target_categories": dict(cats),
        "structured_targets": sum(m["structured"] for c in cases for m in c["target_meta"]),
        "trials_used": len({t for c in cases for t in c["candidate_trials"]}), "dev_trials_overlap": len(
            {t for c in cases for t in c["candidate_trials"]} & {t for _, g in dev_cases for t in g.candidate_trials}),
        "dev_gt_reused": sum(m["rule_key"] in dev_gt for c in cases for m in c["target_meta"]),
        "skipped": dict(report),
        "independence_limits": [
            "모집 중인 시험 23개를 개발셋도 모두 사용 -> 시험 단위 중복 (기준·환자는 새로 생성)",
            "숨김 대상 선택은 결정적 무작위 층화(선정/제외 같은 수)이며 '판정을 가르는 기준'이라는 임상 판단이 아님",
            "확정 불충족 사례는 만들지 않음(문서 검증 경로 없음)",
        ],
    }
    return cases, summary


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=20261013)
    ap.add_argument("--per-kind", type=int, default=2)
    args = ap.parse_args()
    cases, summary = build(args.seed, args.per_kind)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "validation_cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "build_report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
