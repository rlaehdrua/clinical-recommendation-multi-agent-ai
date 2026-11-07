"""P0 사례 -> 현재 코드의 처리 결과 파일 (오프라인 어댑터, 엔진 코드 변경 없음). 채점은 scripts/score_p0_fact_cases.py.

  PYTHONPATH=src python scripts/p0_fact_adapter_offline.py --tag 4f81460 [--out data/eval/p0_fact_cases/results/4f81460.json]

호출은 scripts/eval_m_holdout.py와 같은 오프라인 스텁 경로입니다 (실제 LLM·세션 없음, 판정 구현은 호출만).
- 값 사례: field가 age·sex·ecog면 value_grounding.ground, 그 밖은 ground_lab. 결과 None -> accept, 그 밖 -> hold
- 서술 사례: evidence_directness.check(criterion, quote, text, holds="no"). conflicting -> conflict,
  direct·unchecked -> accept, 그 밖 -> hold
한계: 웹·세션 전체 경로(추출 LLM 포함)가 아니라 근거 확인 함수 단위입니다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from score_p0_fact_cases import load_cases  # noqa: E402


def _commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run() -> dict:
    from ctrec import config, evidence_directness as ed, value_grounding as vg
    config.REFERENCE_DATE = "2026-11-07"
    out = {}
    for c in load_cases():
        if "field" in c:
            f, v, u, t = c["field"], c["value"], c.get("unit"), c["text"]
            r = vg.ground(f, v, t, unit=u) if f in ("age", "sex", "ecog") else vg.ground_lab(f, v, t, t, unit=u)
            out[c["case_id"]] = {"got": "accept" if r is None else "hold", "detail": "" if r is None else str(r)[:120]}
        else:
            k = ed.check(c["criterion"], c["quote"], c["text"], holds="no")
            got = "conflict" if k.directness == "conflicting" else "accept" if k.directness in ("direct", "unchecked") else "hold"
            out[c["case_id"]] = {"got": got, "detail": f"{k.directness} {list(k.flags or [])}"[:120]}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    out = a.out or ROOT / "data" / "eval" / "p0_fact_cases" / "results" / f"{a.tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    res = {"pipeline": "offline_stub(value_grounding.ground*/evidence_directness.check)", "tag": a.tag, "commit": _commit(),
           "reference_date": "2026-11-07", "results": run()}
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(out, len(res["results"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
