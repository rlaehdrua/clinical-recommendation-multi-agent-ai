"""라벨 시트 만들기와 GT 세트 조립.

사용:
  # 1) 사례 파일 -> 빈 라벨 시트 (라벨러에게 줄 것. 엔진 출력 열 없음)
  python scripts/gt_assemble.py sheet --cases cases.json --out sheet_L-A.csv
  # 2) 사례 + 라벨러 시트 2개 + 판정 기록 + 세트 메타 -> GT 세트 JSON (스키마 검증)
  python scripts/gt_assemble.py build --cases cases.json --meta meta.json
      --labels sheet_L-A.csv sheet_L-B.csv --adjudication adjudication_log.csv --out gt/g_u/set7.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_lib.assemble import assemble, read_adjudication  # noqa: E402
from gt_lib.labelio import make_sheet, read_labels  # noqa: E402
from gt_lib.models import INPUT_MODELS, Source  # noqa: E402


def load_cases(p: Path) -> dict:
    cases = json.loads(p.read_text(encoding="utf-8"))
    model = INPUT_MODELS[cases["layer"]]
    for it in cases["items"]:
        model.model_validate(it["input"])
        Source.model_validate(it["source"])
    return cases


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet")
    s.add_argument("--cases", required=True, type=Path)
    s.add_argument("--out", required=True, type=Path)
    b = sub.add_parser("build")
    b.add_argument("--cases", required=True, type=Path)
    b.add_argument("--meta", required=True, type=Path)
    b.add_argument("--labels", nargs="+", required=True, type=Path)
    b.add_argument("--adjudication", type=Path)
    b.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    cases = load_cases(a.cases)
    if a.cmd == "sheet":
        make_sheet(cases, a.out)
        print(f"wrote {a.out}")
        return 0
    meta = json.loads(a.meta.read_text(encoding="utf-8"))
    labels = [read_labels(cases["layer"], p) for p in a.labels]
    gt = assemble(cases, meta, labels, read_adjudication(a.adjudication))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(gt.model_dump_json(indent=1), encoding="utf-8")
    pend = sum(r.adjudication.status == "pending" for r in gt.items)
    print(f"wrote {a.out}: items={len(gt.items)} pending={pend}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
