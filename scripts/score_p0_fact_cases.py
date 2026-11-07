"""P0-1·P0-2 사례 채점 (엔진 import 없음 - 사례 파일과 결과 파일만 읽음).

  python scripts/score_p0_fact_cases.py --results data/eval/p0_fact_cases/results/<tag>.json [--md out.md] [--json out.json]

입력
- 사례: data/eval/p0_fact_cases/{value_cases,narrative_conflict_cases,holdout6_failure_cases}.json  (expected = accept|hold|conflict)
- 결과: {"pipeline": "...", "commit": "...", "results": {"<case_id>": {"got": "accept|hold|conflict", "detail": "..."}}}
  결과를 만드는 쪽(어댑터)은 따로 있습니다 (scripts/p0_fact_adapter_offline.py). 다른 파이프라인도 같은 형식으로 내면 채점됩니다.
출력 (유형별)
- match       : 기대와 같은 처리 (conflict 기대는 conflict 또는 hold를 안전 처리로 셈)
- wrong_accept: 오채택 = 기대가 hold·conflict인데 accept
- over_hold   : 과잉 보류 = 기대가 accept인데 hold·conflict
- conflict_detected: conflict 기대 중 conflict로 탐지
- missing     : 결과 파일에 없는 사례 (오답으로 셈)
건수이며 정확도가 아닙니다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

CASES = Path(__file__).resolve().parents[1] / "data" / "eval" / "p0_fact_cases"
FILES = ("value_cases.json", "narrative_conflict_cases.json", "holdout6_failure_cases.json")
VALID = {"accept", "hold", "conflict"}


def load_cases(files=FILES) -> list[dict]:
    out = []
    for name in files:
        p = CASES / name
        if p.exists():
            for c in json.loads(p.read_text(encoding="utf-8"))["cases"]:
                out.append({**c, "set": name.removesuffix(".json")})
    return out


def score(cases: list[dict], results: dict) -> dict:
    rows = defaultdict(lambda: {"n": 0, "match": 0, "wrong_accept": 0, "over_hold": 0, "conflict_expected": 0,
                                "conflict_detected": 0, "missing": 0})
    per_case = []
    for c in cases:
        r = results.get(c["case_id"])
        got = r.get("got") if r else None
        if got is not None and got not in VALID:
            raise ValueError(f"{c['case_id']}: 알 수 없는 결과 {got!r}")
        key = (c["set"], c["category"])
        row = rows[key]
        row["n"] += 1
        exp = c["expected"]
        if got is None:
            row["missing"] += 1
            ok = False
        elif exp == "conflict":
            ok = got in ("conflict", "hold")
            row["conflict_expected"] += 1
            row["conflict_detected"] += got == "conflict"
            row["wrong_accept"] += got == "accept"
        elif exp == "hold":
            ok = got == "hold" or got == "conflict"
            row["wrong_accept"] += got == "accept"
        else:
            ok = got == "accept"
            row["over_hold"] += got != "accept"
        row["match"] += ok
        per_case.append({"case_id": c["case_id"], "set": c["set"], "category": c["category"], "expected": exp, "got": got,
                         "ok": ok})
    total = {k: sum(r[k] for r in rows.values()) for k in ("n", "match", "wrong_accept", "over_hold", "conflict_expected",
                                                          "conflict_detected", "missing")}
    return {"by_category": {f"{s}|{c}": r for (s, c), r in sorted(rows.items())}, "total": total, "cases": per_case}


def to_md(res: dict, meta: dict) -> str:
    L = [f"# P0 사례 채점 - {meta.get('pipeline', '?')} ({meta.get('commit', '?')})", "",
         "건수이며 정확도가 아닙니다. 임상 검수 미완료.", "",
         "| 세트 | 유형 | 사례 | 안전 처리 | 오채택 | 과잉 보류 | 상충 탐지 | 결과 없음 |", "|---|---|---|---|---|---|---|---|"]
    for k, r in res["by_category"].items():
        s, c = k.split("|")
        det = f"{r['conflict_detected']}/{r['conflict_expected']}" if r["conflict_expected"] else "-"
        L.append(f"| {s} | {c} | {r['n']} | {r['match']} | {r['wrong_accept']} | {r['over_hold']} | {det} | {r['missing']} |")
    t = res["total"]
    L.append(f"| **합계** | | {t['n']} | {t['match']} | {t['wrong_accept']} | {t['over_hold']} | "
             f"{t['conflict_detected']}/{t['conflict_expected']} | {t['missing']} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--md", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    data = json.loads(a.results.read_text(encoding="utf-8"))
    res = score(load_cases(), data["results"])
    meta = {k: data.get(k) for k in ("pipeline", "commit")}
    md = to_md(res, meta)
    if a.md:
        a.md.write_text(md, encoding="utf-8")
    if a.json:
        a.json.write_text(json.dumps({**meta, **res}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
