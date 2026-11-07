"""GT 세트 + 엔진 출력(gt-engine-output/1) -> 설계 D 3절 지표.

- 분자·분모를 그대로 보고하고 Wilson·정확 95% 구간을 붙입니다. 분모 0은 '추정 불가'.
- 분모가 D 2.8 최소 n 미만이면 합격 판정을 하지 않습니다.
- 거짓 확정 적격·거짓 확정 부적격·미해결·과잉 보류를 따로 셉니다.
- LLM이 필요한 지표(A7, B6 등)는 '미측정'으로 표시합니다.
- 엔진 출력의 gt_set_sha256이 GT 파일 해시와 다르면 거부합니다.
- --frozen을 주면 FROZEN_GT.json 확인을 먼저 하고, 실패하거나 상태가 sealed가 아니면 결과를 '탐색적'으로 표시합니다.

사용:
  python scripts/gt_eval.py --gt gt/g_u/set7.json --engine out_after.json [--baseline out_before.json]
      [--frozen gt] [--out report.json]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_lib import freeze as fz  # noqa: E402
from gt_lib.metrics import evaluate  # noqa: E402
from gt_lib.models import GTSet  # noqa: E402


def _fmt(m: dict) -> str:
    if not isinstance(m, dict) or "denominator" not in m:
        return str(m)
    if m["status"] != "ok":
        return f"{m['numerator']}/{m['denominator']} {m['status']}"
    s = (f"{m['numerator']}/{m['denominator']} = {m['rate']:.3f} Wilson95 [{m['wilson95'][0]:.3f}, "
         f"{m['wilson95'][1]:.3f}] 정확95 [{m['exact95'][0]:.3f}, {m['exact95'][1]:.3f}]")
    if m.get("d_judgement"):
        s += f" | D 기준: {m['d_judgement']}"
    return s


def run(gt_path: Path, engine_path: Path, baseline_path: Path | None = None, frozen: Path | None = None) -> dict:
    data = gt_path.read_bytes()
    gt = GTSet.model_validate_json(data)
    engine = json.loads(engine_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(data).hexdigest()
    if engine.get("gt_set_sha256") != digest:
        raise ValueError("엔진 출력이 이 GT 파일에서 만들어지지 않았습니다 (gt_set_sha256 불일치)")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path else None
    if baseline is not None and baseline.get("gt_set_sha256") != digest:
        raise ValueError("기준선 출력의 gt_set_sha256이 GT 파일과 다릅니다")
    report = evaluate(gt, engine, baseline)
    report["gt_set_sha256"] = digest
    if frozen is not None:
        problems = fz.verify(frozen)
        status = None
        if (frozen / fz.FROZEN_NAME).exists():
            status = json.loads((frozen / fz.FROZEN_NAME).read_text(encoding="utf-8")).get("status")
        report["frozen"] = {"problems": problems, "status": status}
        if problems or status != "sealed":
            report["exploratory"] = "동결 확인 실패 또는 이미 측정·강등된 세트: 결과는 탐색적"
    else:
        report["exploratory"] = "동결 확인 없이 계산 (--frozen 없음): 결과는 탐색적"
    return report


def print_report(rep: dict):
    print(f"# {rep['gt_set_id']} ({rep['layer']}, level={rep['level']})")
    for k in ("warning", "exploratory"):
        if rep.get(k):
            print(f"!! {rep[k]}")
    print("coverage:", rep["coverage"])
    for block in ("A", "B", "C", "E"):
        if block not in rep:
            continue
        print(f"## {block}")
        for k, v in rep[block].items():
            print(f"  {k}: {_fmt(v)}")
    if "paired" in rep:
        print("## 짝지은 비교", json.dumps(rep["paired"], ensure_ascii=False))
    print("## 미측정")
    for k, v in rep["not_measured"].items():
        print(f"  {k}: {v}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gt", required=True, type=Path)
    ap.add_argument("--engine", required=True, type=Path)
    ap.add_argument("--baseline", type=Path)
    ap.add_argument("--frozen", type=Path, help="FROZEN_GT.json이 있는 gt 폴더")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    try:
        rep = run(a.gt, a.engine, a.baseline, a.frozen)
    except ValueError as e:
        print(f"거부: {e}")
        return 1
    print_report(rep)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
