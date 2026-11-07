"""P0-1·P0-2 사례로 현재 코드의 처리를 기록 (엔진 코드 변경 없음). 사용: PYTHONPATH=src python scripts/eval_p0_fact_cases.py [--out x.json]

호출은 scripts/eval_m_holdout.py와 같습니다 (판정 구현은 호출만 하고 열어 보지 않음).
- 값 사례: field가 age·sex·ecog면 value_grounding.ground, 그 밖은 ground_lab. 결과 None = 채택(accept), 그 밖 = 보류(hold)
- 서술 사례: evidence_directness.check(criterion, quote, text, holds="no"). conflicting = 상충 탐지(conflict),
  direct·unchecked 밖 = 그 밖 보류(hold), direct·unchecked = 채택(accept)
- 기존 held-out 6의 상충 15건·전해 들은 값 10건도 같은 방식으로 다시 기록 (--holdout)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from ctrec import config, evidence_directness as ed, value_grounding as vg

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "data" / "eval" / "p0_fact_cases"
config.REFERENCE_DATE = "2026-11-07"


def value_result(it: dict) -> tuple[str, str]:
    f, v, u, t = it["field"], it["value"], it.get("unit"), it["text"]
    r = vg.ground(f, v, t, unit=u) if f in ("age", "sex", "ecog") else vg.ground_lab(f, v, t, t, unit=u)
    return ("accept" if r is None else "hold"), ("" if r is None else str(r))


def narrative_result(it: dict) -> tuple[str, str]:
    c = ed.check(it["criterion"], it["quote"], it["text"], holds="no")
    got = "conflict" if c.directness == "conflicting" else "accept" if c.directness in ("direct", "unchecked") else "hold"
    return got, f"{c.directness} {list(c.flags or [])}"


def ok(expected: str, got: str) -> bool:
    if expected == "conflict":
        return got in ("conflict", "hold")  # 상충은 탐지 또는 보류면 안전 (탐지 여부는 따로 셈)
    return got == expected


def run(holdout: bool) -> dict:
    out = {"reference_date": config.REFERENCE_DATE, "sets": {}}
    vals = json.loads((CASES / "value_cases.json").read_text(encoding="utf-8"))["cases"]
    nars = json.loads((CASES / "narrative_conflict_cases.json").read_text(encoding="utf-8"))["cases"]
    rows = []
    for it in vals:
        got, why = value_result(it)
        rows.append({"case_id": it["case_id"], "category": it["category"], "expected": it["expected"], "got": got,
                     "ok": ok(it["expected"], got), "detail": why[:120]})
    for it in nars:
        got, why = narrative_result(it)
        rows.append({"case_id": it["case_id"], "category": it["category"], "expected": it["expected"], "got": got,
                     "ok": ok(it["expected"], got), "detail": why[:120]})
    out["sets"]["p0_fact_cases"] = rows
    if holdout:
        d = ROOT / "docs" / "dev"
        labs = json.loads((d / "m_holdout_6_labs.json").read_text(encoding="utf-8"))["negative"]
        hs = [x for x in labs if x["category"] == "hearsay_or_uncertain"]
        cf = [x for x in json.loads((d / "m_holdout_6_cases.json").read_text(encoding="utf-8"))["conflict"] if x["expected"] == "hold"]
        h_rows = []
        for i, it in enumerate(hs):
            got, why = value_result(it)
            h_rows.append({"case_id": f"H6-hearsay-{i + 1:02d}", "expected": "hold", "got": got, "ok": got == "hold",
                           "text": it["text"][:90], "detail": why[:100]})
        for i, it in enumerate(cf):
            got, why = narrative_result({**it})
            h_rows.append({"case_id": f"H6-conflict-{i + 1:02d}", "expected": "conflict", "got": got, "ok": got in ("conflict", "hold"),
                           "text": it["text"][:90], "detail": why[:100]})
        out["sets"]["holdout6_failures"] = h_rows
    summary = {}
    for name, rows in out["sets"].items():
        by = Counter((r.get("category", r["case_id"].rsplit("-", 1)[0]), r["ok"]) for r in rows)
        cats = sorted({k[0] for k in by})
        summary[name] = {c: f"{by[(c, True)]}/{by[(c, True)] + by[(c, False)]}" for c in cats}
    out["summary_ok"] = summary
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    res = run(a.holdout)
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    print(json.dumps(res["summary_ok"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
