"""G-U 항목을 엔진의 결정적 함수로 오프라인 실행해 gt-engine-output/1 파일을 만듭니다 (측정 위치 U).
scripts/eval_m_holdout.py와 같은 호출 방식:
- kind=conflict: evidence_directness.check(criterion, quote, text, holds=...)
- kind=value: value_grounding.ground (age/sex/ecog) 또는 ground_lab(field, value, text, text, unit=...)
  반환 None = 채택(accepted).

라벨 차단: GT 세트 파일에서 각 항목의 `input`만 꺼내 UtteranceInput(extra=forbid, 라벨 필드 없음)으로 만든 뒤
run_units()에 넘깁니다. run_units()는 UtteranceInput만 받습니다. annotations·final·adjudication·needs_expert·source는
읽은 직후 버려지고 엔진 함수에 닿지 않습니다. 실제 LLM 호출 없음.

사용:
  PYTHONPATH=src python scripts/gt_engine_adapter.py GT_SET.json --out engine_output.json
      [--reference-date 2026-11-07] [--engine-commit <sha>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_lib.metrics import ENGINE_FORMAT  # noqa: E402
from gt_lib.models import UtteranceInput  # noqa: E402

STRUCTURED_FIELDS = ("age", "sex", "ecog")


def blind_inputs(raw_set: dict) -> list[tuple[str, UtteranceInput]]:
    """GT 세트(원시 JSON)에서 (item_id, 입력)만 꺼냄. 라벨은 여기서 버려짐."""
    if raw_set.get("layer") != "G-U":
        raise ValueError("어댑터는 G-U 세트만 실행합니다 (G-C/G-P는 대역 Session 측정이 필요, 미구현)")
    return [(it["item_id"], UtteranceInput.model_validate(it["input"])) for it in raw_set["items"]]


def run_units(inputs: list[tuple[str, UtteranceInput]], reference_date: str) -> list[dict]:
    """엔진 함수 실행. 입력 외의 것은 받지 않습니다."""
    from ctrec import config, evidence_directness as ed, value_grounding as vg

    for iid, inp in inputs:
        if not isinstance(inp, UtteranceInput) or not isinstance(iid, str):
            raise TypeError("run_units는 (item_id, UtteranceInput)만 받습니다")
    prev = config.REFERENCE_DATE
    out = []
    try:
        for iid, inp in inputs:
            config.REFERENCE_DATE = inp.reference_date or reference_date
            if inp.kind == "conflict":
                c = ed.check(inp.criterion, inp.quote, inp.text, holds=inp.holds)
                result = {"directness": c.directness, "flags": list(c.flags), "concept": c.concept}
            else:
                if inp.field in STRUCTURED_FIELDS:
                    r = vg.ground(inp.field, inp.value, inp.text, unit=inp.unit)
                else:
                    r = vg.ground_lab(inp.field, inp.value, inp.text, inp.text, unit=inp.unit)
                result = {"accepted": r is None, "grounding": r}
            out.append({"item_id": iid, "layer": "G-U", "kind": inp.kind, "result": result})
    finally:
        config.REFERENCE_DATE = prev
    return out


def _commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parent, check=True).stdout.strip()
    except Exception:
        return None


def build_output(set_path: Path, reference_date: str, engine_commit: str | None = None) -> dict:
    data = set_path.read_bytes()
    raw = json.loads(data.decode("utf-8"))
    inputs = blind_inputs(raw)
    set_id = raw["set_id"]
    del raw  # 라벨이 든 원본은 여기서 버림
    import ctrec

    items = run_units(inputs, reference_date)
    return {
        "format": ENGINE_FORMAT,
        "gt_set_id": set_id,
        "gt_set_sha256": hashlib.sha256(data).hexdigest(),
        "level": "U",
        "engine": {"commit": engine_commit or _commit(), "ctrec_file": ctrec.__file__, "reference_date": reference_date},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": items,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gt_set", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--reference-date", default="2026-11-07")
    ap.add_argument("--engine-commit")
    a = ap.parse_args(argv)
    out = build_output(a.gt_set, a.reference_date, a.engine_commit)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {a.out}: items={len(out['items'])} ctrec={out['engine']['ctrec_file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
