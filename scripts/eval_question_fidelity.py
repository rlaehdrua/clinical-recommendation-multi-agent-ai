"""평가 충실도 확인과 정책 A–E 재비교 (Phase 4-3, 오프라인·평가 전용).

1) fidelity: 평가 경로 정책 A(tie_order 주입, 순서 변경 없음)가 실제 Decision Loop와 같은 결과를 내는지 사례별로 비교
   (후보 목록·단계·점수·우선순위·제외 사유·선택된 행동 순서·질문 예산·종료 사유·사실 버전·오래된 판정 처리·질문)
2) legacy: Phase 4-2 방식(평가기가 상태를 다시 만듦)의 A와 실제 루프의 차이와 원인
3) 정책 A–E와 무작위 동점 기준선(R_random_1..3)을 실제 루프 경로에서 개발셋·검증셋·보충 세트로 비교

    PYTHONPATH=src python scripts/eval_question_fidelity.py [--out DIR] [--sets dev_101 validation supplement]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from build_validation_set import SUPPLEMENT  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402
from eval_question_priority import VALIDATION, mapping_analysis, summarize, target_breakdown  # noqa: E402

from ctrec import question_eval as qe, question_priority as qp  # noqa: E402

SETS = {"dev_101": qe.DEFAULT_CASES, "validation": VALIDATION, "supplement": SUPPLEMENT}
RANDOM = tuple(f"{qp.RANDOM_PREFIX}{i}" for i in (1, 2, 3))
VOLATILE = {("usage", "seconds"), ("stats", "state_build_ms")}


_FACT_ID = re.compile(r"F-[0-9a-f]{12}")


def _canon_ids(text: str) -> str:
    """사실 ID(F-<uuid>)는 실행마다 새로 만들어지므로 등장 순서 번호로 바꿔 비교 (내용·순서는 그대로)."""
    seen: dict[str, str] = {}
    return _FACT_ID.sub(lambda m: seen.setdefault(m.group(0), f"F#{len(seen) + 1}"), text)


def _norm_diag(diags: list[dict]) -> list[dict]:
    out = []
    for d in json.loads(_canon_ids(json.dumps(diags, default=str))):
        for a, b in VOLATILE:
            d.get(a, {}).pop(b, None)
        out.append(d)
    return out


def _questions(run) -> list:
    return [(q["round_id"], q["question_id"], tuple(q["target_criterion_ids"]), q["fact_version"]) for q in run.questions]


def compare(real, other) -> list[str]:
    diffs = []
    if real.candidate_log and other.candidate_log and real.candidate_log != other.candidate_log:
        diffs.append("candidates")
    dr, do = _norm_diag(real.diagnostics), _norm_diag(other.diagnostics)
    for key in ("actions", "rejected", "stop_reason", "finalize_reasons", "iterations", "usage", "stats", "state"):
        if [d.get(key) for d in dr] != [d.get(key) for d in do]:
            diffs.append(key)
    if _questions(real) != _questions(other):
        diffs.append("questions")
    if real.final_eligibility != other.final_eligibility:
        diffs.append("final_eligibility")
    if real.status != other.status:
        diffs.append("status")
    return diffs


def legacy_cause(real, legacy) -> str:
    """Phase 4-2 방식 A와 실제 루프의 질문 차이 원인 분류."""
    rq, lq = _questions(real), _questions(legacy)
    if rq == lq:
        return "same"
    stale = {k for d in real.diagnostics for a in d.get("actions", [])
             if a.get("reason_code") == "llm_reevaluation_required" for k in a.get("target_criterion_ids", [])}
    extra = [q for q in lq if (q[0], q[2]) not in {(x[0], x[2]) for x in rq}]
    if extra and all(set(q[2]) & stale for q in extra if q[0] > 1):
        return "stale_baseline_reused (다시 매칭되지 않은 시험의 오래된 LLM 판정으로 질문)"
    if any(q[0] == 1 for q in extra):
        return "round1_state_or_budget"
    return "later_round_divergence"


def fidelity(parsed, trials, path, budget=3, rounds=2) -> dict:
    cases = qe.load_cases(parsed, path)
    rows, causes = [], Counter()
    with qe.OfflineEnvironment(budget):
        for case, gt in cases:
            plain = qe.run_method("candidate", case, gt, trials, rounds, budget)
            real = qe.run_method("candidate", case, gt, trials, rounds, budget, record_candidates=True)
            a = qe.run_method("A_current", case, gt, trials, rounds, budget, tie_policy="A_current",
                              record_candidates=True)
            legacy = qe.run_method("A_legacy", case, gt, trials, rounds, budget,
                                   factories={"A_legacy": qp.generator_factory("A_current")})
            recorder_neutral = compare(plain, real)  # 기록용 포장 자체가 동작을 바꾸지 않음
            d = compare(real, a)
            cause = legacy_cause(real, legacy)
            causes[cause] += 1
            rows.append({"case_id": gt.case_id, "case_type": gt.case_type, "recorder_diffs": recorder_neutral,
                         "a_vs_real_diffs": d, "legacy_vs_real": cause,
                         "legacy_provisional_extra": sum(1 for q in legacy.questions
                                                         if q["relevance"].get("provisional_mismatch"))
                         - sum(1 for q in real.questions if q["relevance"].get("provisional_mismatch"))})
    n = len(rows)
    return {"cases": n,
            "a_matches_real": sum(not r["a_vs_real_diffs"] for r in rows),
            "recorder_neutral": sum(not r["recorder_diffs"] for r in rows),
            "a_mismatch_cases": [r for r in rows if r["a_vs_real_diffs"]],
            "legacy_causes": dict(causes),
            "legacy_extra_provisional_questions": sum(r["legacy_provisional_extra"] for r in rows),
            "legacy_mismatch_cases": [{"case_id": r["case_id"], "case_type": r["case_type"], "cause": r["legacy_vs_real"]}
                                      for r in rows if r["legacy_vs_real"] != "same"]}


def compare_policies(parsed, trials, path, budget=3, rounds=2) -> dict:
    methods = ("candidate",) + qp.POLICIES + RANDOM
    tie = {p: p for p in qp.POLICIES + RANDOM}
    res = qe.run_evaluation(parsed, trials, rounds=rounds, budget=budget, cases_path=path, methods=methods,
                            tie_policies=tie)
    cases = qe.load_cases(parsed, path)
    bd = target_breakdown(res, cases, parsed, budget)
    summ = summarize(res, bd)
    rand = [summ[m] for m in RANDOM]
    summ["R_random_mean"] = {k: round(sum(r[k] for r in rand) / len(rand), 4) for k in
                             ("coverage_exact", "coverage_exact_in_budget", "inclusion_hit", "exclusion_hit")
                             if all(r[k] is not None for r in rand)}
    out = {"cases": res["cases_total"], "round1_matcher_identical": res["round1_matcher_identical"],
           "summary": summ, "breakdown": {m: bd[m] for m in methods}}
    if path == qe.DEFAULT_CASES:
        ma = mapping_analysis(parsed)
        out["sensitivity_relaxed_mapping"] = {
            m: v.get("all:all") for m, v in target_breakdown(res, cases, parsed, budget, ma["relaxed_targets"]).items()}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--sets", nargs="*", default=list(SETS))
    ap.add_argument("--skip-fidelity", action="store_true")
    args = ap.parse_args()
    parsed = load_frozen()
    all_trials = load_trials()
    trials = {t: all_trials[t] for t in parsed}
    result = {}
    for name in args.sets:
        path = SETS[name]
        if not args.skip_fidelity:
            result[f"fidelity:{name}"] = fidelity(parsed, trials, path)
        result[f"policies:{name}"] = compare_policies(parsed, trials, path)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "fidelity.json").write_text(text, encoding="utf-8")
    for k, v in result.items():
        if k.startswith("fidelity"):
            print(f"\n== {k}: A=real {v['a_matches_real']}/{v['cases']}, recorder neutral {v['recorder_neutral']}/"
                  f"{v['cases']}, legacy causes {v['legacy_causes']}, legacy extra provisional "
                  f"{v['legacy_extra_provisional_questions']}")
        else:
            print(f"\n== {k} ({v['cases']}개, 라운드1 Matcher 동일 {v['round1_matcher_identical']})")
            for m, s in v["summary"].items():
                print(f"  {m:16} " + " ".join(f"{a}={b}" for a, b in s.items()))


if __name__ == "__main__":
    main()
