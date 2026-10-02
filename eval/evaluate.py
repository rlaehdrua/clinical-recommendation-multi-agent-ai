"""매칭 정확성 평가 (평가 기준 1: 매칭 정확성 30%).

정답 파일(CSV) 형식:
    patient_id,trial_id,label
    SYN-001,DEMO-LUNG-001,ELIGIBLE
label은 ELIGIBLE / INELIGIBLE (선택적으로 UNCERTAIN).
합성 데이터 라벨에는 label_text_only 열(공개 서술만으로 판단한 정답, UNCERTAIN 포함)도 있습니다.
  --label-column label           : 확인 질문으로 숨은 정보를 얻은 뒤의 정답 (--answers simulated 실행과 비교)
  --label-column label_text_only : 서술만으로 본 정답 (--answers none 실행과 비교)

사용:
    python eval/evaluate.py --labels data/labels/synthetic_labels.csv --outputs outputs
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from ctrec.agents.recommender import is_open

CLASSES = ["ELIGIBLE", "INELIGIBLE", "UNCERTAIN"]


def load_predictions(outputs: Path) -> tuple[dict, dict]:
    elig, ranked, top = {}, {}, {}
    for result in outputs.glob("*/result.json"):
        data = json.loads(result.read_text(encoding="utf-8"))
        pid = data["patient_id"]
        for tid, m in data["matches"].items():
            elig[(pid, tid)] = m["eligibility"]
        rec = data.get("recommendation") or {}
        ranked[pid] = [r["trial_id"] for r in rec.get("ranked", [])]
        top[pid] = [r["trial_id"] for r in rec.get("recommended", [])] or ranked[pid][:1]
    return elig, ranked, top


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--outputs", default="outputs")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--label-column", default="label")
    ap.add_argument("--trials-dir", default="data/trials", help="모집 상태 확인용")
    args = ap.parse_args()

    with open(args.labels, encoding="utf-8") as f:
        gold = {(r["patient_id"], r["trial_id"]): r[args.label_column].strip().upper() for r in csv.DictReader(f)}
    pred, ranked, top = load_predictions(Path(args.outputs))

    pairs = [k for k in gold if k in pred]
    missing = [k for k in gold if k not in pred]
    if not pairs:
        raise SystemExit("정답과 겹치는 예측이 없습니다. outputs 경로와 ID를 확인하세요.")

    confusion = Counter((gold[k], pred[k]) for k in pairs)
    correct = sum(gold[k] == pred[k] for k in pairs)
    decided = [k for k in pairs if pred[k] != "UNCERTAIN"]
    decided_correct = sum(gold[k] == pred[k] for k in decided)

    print(f"평가 쌍: {len(pairs)} (예측 누락 {len(missing)})")
    print(f"전체 정확도 (UNCERTAIN 예측=오답): {correct / len(pairs):.3f}")
    print(f"판정 확정 비율 (coverage):          {len(decided) / len(pairs):.3f}")
    if decided:
        print(f"확정 판정 정확도:                    {decided_correct / len(decided):.3f}")

    print("\n혼동 행렬 (행=정답, 열=예측)")
    print(" " * 12 + "".join(f"{c:>12}" for c in CLASSES))
    for g in CLASSES:
        if any(gg == g for gg, _ in confusion):
            print(f"{g:>12}" + "".join(f"{confusion[(g, p)]:>12}" for p in CLASSES))

    # 추천 품질. 정답 추천 대상 = 정답 ELIGIBLE이면서 현재 모집 중인 시험.
    # 모집 종료 시험만 적격인 환자는 '추천 없음'을 내야 정답.
    status = {}
    for tf in Path(args.trials_dir).glob("*.json"):
        t = json.loads(tf.read_text(encoding="utf-8"))
        if "trial_id" in t:
            status[t["trial_id"]] = t.get("overall_status")
    patients = {pid for pid, _ in pairs}
    target = defaultdict(set)
    for (pid, tid), lab in gold.items():
        if lab == "ELIGIBLE" and is_open({"overall_status": status.get(tid)}):
            target[pid].add(tid)
    top1, precision, none_ok = [], [], []
    for pid in sorted(patients):
        r, want = ranked.get(pid, []), target.get(pid, set())
        if not want:
            none_ok.append(1.0 if not r else 0.0)  # 추천할 시험이 없는 환자
            continue
        # 최종 추천(1순위, 공동 1순위면 모두)이 모두 정답 추천 대상이면 적중
        rec_top = top.get(pid, [])
        top1.append(1.0 if rec_top and all(t in want for t in rec_top) else 0.0)
        topk = r[: args.k]
        precision.append(sum(t in want for t in topk) / len(topk) if topk else 0.0)
    if top1:
        print(f"\n최종 추천 적중률 (모집 중 적격 시험이 있는 환자 {len(top1)}명): {sum(top1) / len(top1):.3f}")
        print(f"추천 Precision@{args.k}:                                        {sum(precision) / len(precision):.3f}")
    if none_ok:
        print(f"'추천 없음' 정확도 (추천할 시험이 없는 환자 {len(none_ok)}명):    {sum(none_ok) / len(none_ok):.3f}")

if __name__ == "__main__":
    main()
