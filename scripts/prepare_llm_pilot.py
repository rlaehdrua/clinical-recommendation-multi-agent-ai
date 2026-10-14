"""실제 LLM 소규모 평가 준비 - dry-run 전용 (Phase 5-0). 실제 API 호출 경로는 없습니다.

기존 16개 합성 사례(prepare_paid_pilot.select)와 그중 먼저 실행할 대표 사례 5개에 대해
실험 A(파이프라인 안정성)·실험 B(질문 계획 비교)를 LLM 대역으로 실제 순서대로 돌려 단계별 호출 수와 요청 크기를 재고,
시나리오(low/base/worst)별 비용과 실험별 상한 제안, 승인 게이트 상태를 출력합니다. 단가·토큰은 확인 전 가정값입니다.

    PYTHONPATH=src python scripts/prepare_llm_pilot.py [--subset representative|all16]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from build_validation_set import SUPPLEMENT  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402
from eval_question_priority import VALIDATION  # noqa: E402
from prepare_paid_pilot import select  # noqa: E402

from ctrec import config, llm, llm_pilot as lp, question_eval as qe  # noqa: E402

PATHS = {"dev_101": qe.DEFAULT_CASES, "validation": VALIDATION, "supplement": SUPPLEMENT}

# 먼저 실행할 대표 사례 (16개 중 5개, 선정 3·제외 2) - 이유와 검증 기능
REPRESENTATIVE = [
    ("M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1",
     "16개 중 가장 긴 서술형 합성 원문(약물 이력 속 유전자 EGFR 언급): 실제 프로파일러 추출·출처, 약물 이력의 표적 연결, 선정 기준 질문"),
    ("V-DEMO-LUNG-001-NCT06908096-lab_platelets-shared",
     "두 시험이 같은 검사 기준(혈소판)을 공유: 시험별 Matcher 동시 호출, 정보 키 묶음, 중복 질문 여부"),
    ("S-NCT07369063-I9-biomarker", "바이오마커 선정 기준: 실제 응답의 바이오마커 사실 연결, 질문 대상 매핑"),
    ("S-NCT05127980-E1-cohort", "코호트가 나뉜 시험의 제외 기준: 코호트 경로 판정, 열린 경로만 묻는지"),
    ("S-NCT06125405-E5-confirmed-NCT04063540",
     "한 시험은 확정 불충족(문서 검증 나이) + 다른 시험의 제외 기준: 불필요 질문(확정 불충족 시험에 묻기) 탐지, 승격 없음"),
]

# 16개에는 신장 eGFR 문장이 없음. 실험 A의 eGFR/EGFR 확인용 추가 후보 (16개 밖, 개발셋 합성 전체 원문, 숨긴 정답 없음,
# 시험 2개로 가장 짧음). 포함 여부는 사용자 결정 사항
EGFR_ADDON = ("dev_101", "F-SYN-S005-02", "신장 기능 eGFR(mL/min/1.73m2) 문장이 있는 합성 전체 원문: 실제 응답의 eGFR/EGFR 구분")


def load_pairs(parsed, subset: str):
    picked = select(parsed)
    if subset.startswith("representative"):
        ids = [cid for cid, _ in REPRESENTATIVE]
        picked = sorted((c for c in picked if c["case_id"] in ids), key=lambda c: ids.index(c["case_id"]))
    if subset == "representative_egfr":
        pool, cid, _ = EGFR_ADDON
        case, gt = qe.load_cases(parsed, PATHS[pool], only=[cid])[0]
        picked = picked + [{"set": pool, "case_id": cid, "case_type": gt.case_type, "target_kinds": [],
                            "trials": len(gt.candidate_trials), "addon": "egfr_check"}]
    pairs = []
    for c in picked:
        pairs += qe.load_cases(parsed, PATHS[c["set"]], only=[c["case_id"]])
    return picked, pairs


def build(subset: str = "representative") -> dict:
    parsed = load_frozen()
    all_trials = load_trials()
    trials = {t: all_trials[t] for t in parsed}
    picked, pairs = load_pairs(parsed, subset)
    measured = lp.measure_offline(pairs, trials)
    est = {sc: lp.estimate(measured, sc) for sc in ("low", "base", "worst")}
    plans = {}
    for exp in lp.EXPERIMENTS:
        lim = lp.suggest_limits(est["worst"]["experiments"][exp], est["base"]["experiments"][exp])
        plans[exp] = {"experiment": exp, "case_ids": [c["case_id"] for c in picked], "model": config.MODEL,
                      "rounds": lp.EXPERIMENTS[exp]["rounds"], "question_budget": lp.EXPERIMENTS[exp]["question_budget"],
                      "limits": lp.asdict(lim), "save_patient_data": False, "redact_outputs": True}
        plans[exp]["plan_hash"] = lp.plan_hash(plans[exp])
        try:
            lp.authorize(plans[exp], None)
            plans[exp]["gate"] = "open"
        except lp.PaidRunNotApproved as e:
            plans[exp]["gate"] = f"closed: {e}"
    with qe.OfflineEnvironment(3):
        try:
            llm.client()
            blocked = False
        except RuntimeError:
            blocked = True
    kinds = {}
    for c in picked:
        for k in c["target_kinds"]:
            kinds[k] = kinds.get(k, 0) + 1
    out = {
        "subset": subset, "cases": picked, "target_kinds": kinds,
        "representative_reasons": ({**dict(REPRESENTATIVE), **({EGFR_ADDON[1]: EGFR_ADDON[2]}
                                    if subset == "representative_egfr" else {})}
                                   if subset.startswith("representative") else None),
        "model": config.MODEL, "fallbacks_enabled": config.USE_FALLBACKS, "price": lp.asdict(lp.ASSUMED_PRICE),
        "unknown": {"model_price": lp.UNKNOWN, "tokens_per_call": lp.UNKNOWN, "cache_hit": lp.UNKNOWN,
                    "retry_billing": lp.UNKNOWN, "fallback_model": lp.UNKNOWN, "latency": lp.UNKNOWN},
        "measured_offline": measured["per_experiment"],
        "offline_results": measured["results"],
        "estimates": {sc: {exp: {k: v for k, v in e.items() if k != "stages"} for exp, e in est[sc]["experiments"].items()}
                      for sc in est},
        "stages_base": {exp: e["stages"] for exp, e in est["base"]["experiments"].items()},
        "plans": plans, "real_llm_blocked_offline": blocked, "paid_execution_enabled": lp.PAID_EXECUTION_ENABLED,
        "previous_estimate_note": "Phase 4-4의 약 $12는 검증 전 가정값 (A/B를 두 방식 모두 2라운드 전체 파이프라인으로 셈)",
    }
    lp.assert_redacted(out, lp.forbidden_texts(pairs))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--subset", choices=("representative", "representative_egfr", "all16"), default="representative")
    args = ap.parse_args()
    print(json.dumps(build(args.subset), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
