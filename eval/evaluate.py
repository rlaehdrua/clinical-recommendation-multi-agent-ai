"""매칭 정확성 평가 (평가 기준 1: 매칭 정확성 30%).

정답 파일(CSV) 형식:
    patient_id,trial_id,label
    SYN-001,DEMO-LUNG-001,ELIGIBLE
label은 ELIGIBLE / INELIGIBLE (선택적으로 UNCERTAIN).
합성 데이터 라벨에는 label_text_only 열(공개 서술만으로 판단한 정답, UNCERTAIN 포함)도 있습니다.
  --label-column label           : 확인 질문으로 숨은 정보를 얻은 뒤의 정답 (--answers simulated 실행과 비교)
  --label-column label_text_only : 서술만으로 본 정답 (--answers none 실행과 비교)

label_text_only 열이 있으면 Rescue/Cleanup(숨은 정보로 판정을 바로잡았는가)도 함께 계산합니다.
False Removal / Premature Match 등 지표 정의는 src/ctrec/metrics.py에 있습니다.

사용:
    python eval/evaluate.py --labels data/labels/synthetic_labels.csv --outputs outputs
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from ctrec.agents.recommender import is_open
from ctrec.metrics import (CLASSES, accuracy_by_group, eligibility_metrics, fact_acquisition_metrics, fmt,
                           load_label_rows, load_labels, load_predictions, load_reveals, load_usage)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--outputs", default="outputs")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--label-column", default="label")
    ap.add_argument("--text-only-column", default="label_text_only",
                    help="서술만으로 본 정답 열. 있으면 Rescue/Cleanup을 계산 (없으면 생략)")
    ap.add_argument("--trials-dir", default="data/trials", help="모집 상태 확인용")
    ap.add_argument("--json", help="지표를 JSON 파일로도 저장")
    args = ap.parse_args()

    gold = load_labels(Path(args.labels), args.label_column)
    with open(args.labels, encoding="utf-8") as f:
        has_text_only = args.text_only_column in (csv.DictReader(f).fieldnames or [])
    text_only = load_labels(Path(args.labels), args.text_only_column) if has_text_only else None
    pred, ranked, top = load_predictions(Path(args.outputs))

    pairs = [k for k in gold if k in pred]
    if not pairs:
        raise SystemExit("정답과 겹치는 예측이 없습니다. outputs 경로와 ID를 확인하세요.")
    m = eligibility_metrics(gold, pred, text_only)
    confusion = m["confusion"]

    print(f"평가 쌍: {m['pairs']} (예측 누락 {m['missing_predictions']})")
    print(f"전체 정확도 (UNCERTAIN 예측=오답): {fmt(m['accuracy'])}")
    print(f"판정 확정 비율 (coverage):          {fmt(m['coverage'])}")
    print(f"확정 판정 정확도:                    {fmt(m['decided_accuracy'])}")

    print("\n안전 지표 (낮을수록 좋음)")
    print(f"  False Removal   (정답 적격 -> 부적격 예측): {fmt(m['false_removal'])} ({m['false_removal_n']})")
    print(f"  Premature Match (정답 비적격 -> 적격 예측): {fmt(m['premature_match'])} ({m['premature_match_n']})")
    if text_only is not None:
        print("상호작용 지표 (서술만으로 판정 보류였던 쌍, 높을수록 좋음)")
        print(f"  Rescue  (숨은 정보로 적격 후보 회복):     {fmt(m['rescue'])} ({m['rescue_n']})")
        print(f"  Cleanup (숨은 정보로 부적격 시험 제거):   {fmt(m['cleanup'])} ({m['cleanup_n']})")

    rows = load_label_rows(Path(args.labels))
    by_type = accuracy_by_group(gold, pred, rows)
    if by_type:
        print("사례 유형별 정확도: " + ", ".join(f"{k} {v}" for k, v in by_type.items()))
        m["accuracy_by_case_type"] = by_type
    acq = fact_acquisition_metrics(rows, load_reveals(Path(args.outputs)))
    if acq:
        print(f"숨긴 정보 회수 (가린 환자 {acq['masked_patients']}명)")
        print(f"  숨긴 사실 회수율:            {fmt(acq['masked_fact_recovery'])}")
        print(f"  숨긴 사실 전부 회수한 비율:  {fmt(acq['full_recovery'])}")
        print(f"  쓸모 있는 질문 비율:         {fmt(acq['useful_question_rate'])}")
        print(f"  사실 1개 회수당 질문 수:     {fmt(acq['questions_per_masked_fact'])}")
        m["fact_acquisition"] = acq

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

    if args.json:
        m["top1"] = sum(top1) / len(top1) if top1 else None
        m[f"precision_at_{args.k}"] = sum(precision) / len(precision) if precision else None
        m["no_recommendation_accuracy"] = sum(none_ok) / len(none_ok) if none_ok else None
        m["usage"] = load_usage(Path(args.outputs))
        m["confusion"] = {f"{g}->{p}": n for (g, p), n in confusion.items()}
        Path(args.json).write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
