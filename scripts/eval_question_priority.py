"""질문 우선순위 동점 처리 정책 A–E 비교 (Phase 4-2, 오프라인·평가 전용).

개발셋(기존 101개 masked case, 동점 문제를 찾는 데 이미 씀)과 새 합성 검증셋을 따로 평가합니다.
LLM은 결정적 대역이며, 결과는 구조 지표입니다(실제 질문 품질·추천 정확도·비용과 무관).

    PYTHONPATH=src python scripts/eval_question_priority.py [--budget 3] [--rounds 2] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, question_eval as qe, question_priority as qp  # noqa: E402

VALIDATION = config.DATA_DIR / "patients" / "masked_validation" / "validation_cases.json"
METHODS = ("baseline", "candidate") + qp.POLICIES


def target_breakdown(result: dict, cases: list, parsed: dict, budget: int, extra_targets: dict | None = None) -> dict:
    """정답 기준 단위 적중 (EXACT 대응만, extra_targets가 있으면 그 키도 정답으로 - 민감도 분석용)."""
    recs = defaultdict(list)
    for q in result["question_records"]:
        recs[(q["case_id"], q["method"])].append(q)
    status = {(r["case_id"], r["method"]): r["status"] for r in result["case_rows"]}
    out = {}
    for m in {q["method"] for q in result["question_records"]} | {r["method"] for r in result["case_rows"]}:
        c = defaultdict(Counter)
        for _, g in cases:
            if status.get((g.case_id, m)) != "ok":
                continue
            targets = [t for t in g.targets if t.status == "EXACT"]
            if extra_targets and g.case_id in extra_targets:
                targets = targets + [qe.GTTarget("extra", "EXTRA", tuple(extra_targets[g.case_id]))]
            qs = recs[(g.case_id, m)]
            first_round = [q for q in qs if q["round_id"] == min((x["round_id"] for x in qs), default=1)]
            for t in targets:
                if not t.rule_keys:
                    continue
                tid, rid = t.rule_keys[0].split(":", 1)
                rule = next(r for r in parsed[tid].rules if r.rule_id == rid)
                hit = any(set(q["target_criterion_ids"]) & set(t.rule_keys) for q in qs)
                hit_b = any(set(q["target_criterion_ids"]) & set(t.rule_keys) for q in first_round[:budget])
                for key in (("all", "all"), ("kind", rule.kind), ("category", rule.category),
                            ("case_type", g.case_type)):
                    c[key]["n"] += 1
                    c[key]["hit"] += hit
                    c[key]["hit_in_budget"] += hit_b
        out[m] = {f"{a}:{b}": {"n": v["n"], "hit": round(v["hit"] / v["n"], 3) if v["n"] else None,
                                "hit_in_budget": round(v["hit_in_budget"] / v["n"], 3) if v["n"] else None}
                  for (a, b), v in sorted(c.items())}
    return out


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9가-힣]+", s.casefold()) if len(w) > 2}


def mapping_analysis(parsed: dict) -> dict:
    """개발셋 숨긴 기준 중 EXACT가 아닌 것의 원인 분류 (평가기 전용, 질문 정책에는 전달하지 않음)."""
    raw = json.loads(qe.DEFAULT_CASES.read_text(encoding="utf-8"))
    rows, extra = [], defaultdict(set)
    for c in raw:
        for text in c.get("masked_criteria", []):
            t = qe.map_criterion(text, c["candidate_trials"], parsed)
            if t.status == "EXACT":
                continue
            ct = _tokens(text)
            scored = []
            for tid in c["candidate_trials"]:
                for r in parsed[tid].rules:
                    rt = _tokens(r.text)
                    if rt:
                        scored.append((len(ct & rt) / len(rt), f"{tid}:{r.rule_id}", r))
            strong = [(s, k, r) for s, k, r in scored if s >= 0.6]
            if t.status == "INFERRED":
                cause = "단일 규칙으로 축약·확장 (한쪽이 다른 쪽을 포함)"
            elif any((r.field or "").lower() in ("age", "sex") for _, _, r in strong) or re.search(r"\baged?\b", text, re.I):
                cause = "구조화 필드로 다시 씀 (예: 나이 범위 -> minimum_age/maximum_age 규칙)"
            elif len(strong) >= 2:
                cause = "여러 원자 규칙으로 나뉨"
            elif len(strong) == 1:
                cause = "문장을 바꿔 쓴 단일 규칙 (토큰 일치만)"
            else:
                cause = "대응 규칙을 찾지 못함 (파서가 빠뜨렸거나 다른 기준과 합침)"
            keys = sorted({k for _, k, _ in strong})
            extra[c["patient_id"]] |= set(keys)
            rows.append({"case_id": c["patient_id"], "status": t.status, "cause": cause, "relaxed_rule_keys": keys})
    return {"rows": rows, "causes": dict(Counter(r["cause"] for r in rows)),
            "relaxed_targets": {k: sorted(v) for k, v in extra.items() if v}}


def summarize(result: dict, breakdown: dict) -> dict:
    out = {}
    for m, a in result["methods"].items():
        out[m] = {
            "ok": a["ok"], "failed": a["failed"], "coverage_exact": a["coverage_exact"]["value"],
            "coverage_exact_in_budget": a["coverage_exact_within_budget"]["value"],
            "questions": a["questions_total"], "dup_same": a["duplicate_same_criterion_rate"]["numerator"],
            "dup_semantic": a["duplicate_semantic_rate"]["numerator"], "irrelevant": a["irrelevant_rate"]["numerator"],
            "provisional_targets": a["provisional_mismatch_targets"], "revealed": a["revealed_answers"],
            "gt_resolved": a["gt_resolved_after"]["numerator"],
            "safety_violations": sum(a["safety"].values()),
            "inclusion_hit": breakdown[m].get("kind:inclusion", {}).get("hit"),
            "exclusion_hit": breakdown[m].get("kind:exclusion", {}).get("hit"),
            "inclusion_hit_in_budget": breakdown[m].get("kind:inclusion", {}).get("hit_in_budget"),
            "exclusion_hit_in_budget": breakdown[m].get("kind:exclusion", {}).get("hit_in_budget"),
        }
    return out


def run(budget: int = 3, rounds: int = 2) -> dict:
    parsed = load_frozen()
    all_trials = load_trials()
    trials = {t: all_trials[t] for t in parsed}
    factories = {p: qp.generator_factory(p) for p in qp.POLICIES}
    out = {"budget": budget, "rounds": rounds, "methods": list(METHODS)}
    for name, path in (("dev_101", qe.DEFAULT_CASES), ("validation", VALIDATION)):
        res = qe.run_evaluation(parsed, trials, rounds=rounds, budget=budget, cases_path=path, methods=METHODS,
                                factories=factories)
        cases = qe.load_cases(parsed, path)
        bd = target_breakdown(res, cases, parsed, budget)
        out[name] = {"cases": res["cases_total"], "seconds": res["seconds"],
                     "round1_matcher_identical": res["round1_matcher_identical"],
                     "summary": summarize(res, bd), "breakdown": bd}
        if name == "dev_101":
            ma = mapping_analysis(parsed)
            out["mapping_analysis"] = {"causes": ma["causes"], "rows": ma["rows"]}
            out["dev_101"]["sensitivity_relaxed_mapping"] = {
                m: v.get("all:all") for m, v in target_breakdown(res, cases, parsed, budget, ma["relaxed_targets"]).items()}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--budget", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    res = run(args.budget, args.rounds)
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "priority_comparison.json").write_text(text, encoding="utf-8")
    for name in ("dev_101", "validation"):
        print(f"\n== {name} ({res[name]['cases']}개, {res[name]['seconds']}s, 라운드1 Matcher 동일 "
              f"{res[name]['round1_matcher_identical']})")
        for m, s in res[name]["summary"].items():
            print(f"  {m:16} " + " ".join(f"{k}={v}" for k, v in s.items()))
    print("\n매핑 원인:", res["mapping_analysis"]["causes"])
    print("완화 매핑 민감도(dev):", res["dev_101"]["sensitivity_relaxed_mapping"])


if __name__ == "__main__":
    main()
