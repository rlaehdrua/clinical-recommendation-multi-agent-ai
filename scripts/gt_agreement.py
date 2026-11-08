"""두 라벨러 일치도(Cohen's kappa, 필드별·범주별)와 판정 대상 목록.

사용:
  python scripts/gt_agreement.py --layer G-U --a labels_a.csv --b labels_b.csv
      --out agreement.json --queue adjudication_queue.csv
G-P 라벨은 JSON 시트. 목표 kappa >= 0.7, 0.6 미만 필드는 규약 수정 뒤 재라벨 (D 2.5).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_lib.agreement import adjudication_queue, compute  # noqa: E402
from gt_lib.labelio import ADJ_COLS, read_labels, write_csv  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--layer", required=True, choices=["G-U", "G-C", "G-P"])
    ap.add_argument("--a", required=True, type=Path)
    ap.add_argument("--b", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--queue", type=Path, help="판정 대상 CSV (adjudication_log 형식, 결정 칸 빈칸)")
    x = ap.parse_args(argv)
    la, lb = read_labels(x.layer, x.a), read_labels(x.layer, x.b)
    ids_a = {ann.labeler_id for ann, _ in la.values()}
    ids_b = {ann.labeler_id for ann, _ in lb.values()}
    if ids_a & ids_b:
        print(f"거부: 두 시트에 같은 라벨러 ID {sorted(ids_a & ids_b)} (독립 라벨이 아님)")
        return 1
    res = compute(x.layer, la, lb)
    for f, k in res["fields"].items():
        kv = None if k["kappa"] is None else round(k["kappa"], 3)
        print(f"{f}: kappa={kv} n={k['n']} ({k['status']})")
    if res["relabel_fields"]:
        print("재라벨 필요(kappa < 0.6):", res["relabel_fields"])
    if x.out:
        x.out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    if x.queue:
        q = adjudication_queue(x.layer, la, lb)
        write_csv(x.queue, ADJ_COLS, q)
        print(f"판정 대상 {len(q)}건 -> {x.queue}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
