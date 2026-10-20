"""소규모 유료 평가(A vs B) 준비 - dry-run 전용 (Phase 4-4). 실제 LLM 호출 경로는 없습니다.

합성 사례(개발셋·검증셋·보충 세트)에서 결정적으로 작은 표본을 고르고(선정/제외 균형, 코호트·바이오마커·확정 불충족 포함),
호출 단계별 예상 비용과 상한을 계산합니다. 단가·토큰 수는 가정값이며 실행 전 확인해야 합니다.
승인 게이트(question_eval.require_paid_approval)는 이번 단계에서 모든 조건을 갖춰도 실행하지 않습니다.

    PYTHONPATH=src python scripts/prepare_paid_pilot.py [--per-kind 4] [--max-cost-usd 15]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from build_validation_set import SUPPLEMENT  # noqa: E402
from eval_question_priority import VALIDATION  # noqa: E402

from ctrec import llm, question_eval as qe  # noqa: E402

METHODS = ("A_current", "B_protocol")


def select(parsed, per_kind: int = 4) -> list[dict]:
    """정답 기준 종류·사례 유형별로 앞에서부터 결정적으로 고름 (ID·종류만, 원문 없음)."""
    picked = []

    def kinds(g):
        out = set()
        for t in g.targets:
            for k in t.rule_keys:
                tid, rid = k.split(":", 1)
                out.add(next(r for r in parsed[tid].rules if r.rule_id == rid).kind)
        return out

    pools = {name: qe.load_cases(parsed, path) for name, path in
             (("dev_101", qe.DEFAULT_CASES), ("validation", VALIDATION), ("supplement", SUPPLEMENT))}
    want = [("validation", "single", "inclusion", per_kind), ("validation", "single", "exclusion", per_kind),
            ("validation", "shared", None, 1), ("supplement", "cohort", None, 2), ("supplement", "biomarker", None, 2),
            ("supplement", "confirmed", None, 1), ("dev_101", "rescue", "inclusion", 1), ("dev_101", "cleanup", "exclusion", 1)]
    for pool, ctype, kind, n in want:
        got = 0
        for _, g in sorted(pools[pool], key=lambda x: x[1].case_id):
            if got >= n:
                break
            if g.case_type == ctype and (kind is None or kinds(g) == {kind}) and g.exact_keys():
                picked.append({"set": pool, "case_id": g.case_id, "case_type": ctype, "target_kinds": sorted(kinds(g)),
                               "trials": len(g.candidate_trials)})
                got += 1
    return picked


def plan(per_kind: int = 4, cap: float = 15.0, rounds: int = 2) -> dict:
    parsed = load_frozen()
    cases = select(parsed, per_kind)
    avg_trials = sum(c["trials"] for c in cases) / len(cases)
    upper = qe.estimate_cost(len(cases), avg_trials, rounds, cap_usd=cap, methods=len(METHODS))
    # A·B는 질문을 Decision Loop 계획에서 가져오고(질문 생성 LLM 없음), 답변은 결정적 정답 시뮬레이터(LLM 없음)
    est = qe.estimate_cost(len(cases), avg_trials, rounds, cap_usd=cap, methods=len(METHODS),
                           disabled=("question_generator", "simulated_patient"))
    with qe.OfflineEnvironment(3):
        try:
            llm.client()
            blocked = False
        except RuntimeError:
            blocked = True
    try:
        qe.require_paid_approval(True, est)
        gate = "open"
    except qe.PaidRunNotApproved as e:
        gate = f"closed: {e}"
    kinds = {}
    for c in cases:
        for k in c["target_kinds"]:
            kinds[k] = kinds.get(k, 0) + 1
    return {"methods": METHODS, "cases": cases, "n_cases": len(cases), "target_kinds": kinds,
            "case_types": sorted({c["case_type"] for c in cases}), "estimate": est, "upper_bound_all_stages": upper,
            "real_llm_blocked_offline": blocked, "paid_gate": gate}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per-kind", type=int, default=4)
    ap.add_argument("--max-cost-usd", type=float, default=15.0)
    args = ap.parse_args()
    print(json.dumps(plan(args.per_kind, args.max_cost_usd), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
