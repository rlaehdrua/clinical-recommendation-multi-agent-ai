"""사례 파일 + 라벨러 시트 + 판정 기록 -> GT 세트 JSON (스키마 검증 포함)."""

from __future__ import annotations

import json
from pathlib import Path

from .labelio import LABEL_MODELS, read_csv
from .models import GTSet


def assemble(cases: dict, meta: dict, labels: list[dict], adjudication_rows: list[dict]) -> GTSet:
    """labels: labelio.read_labels 결과 목록 (라벨러별). adjudication_rows: adjudication_log.csv 행 (결정 채워진 것)."""
    layer = cases["layer"]
    adj = {}
    for row in adjudication_rows:
        if (row.get("status") or "").strip():
            if row["item_id"] in adj:
                raise ValueError(f"판정 기록에 {row['item_id']}가 두 번 있습니다")
            adj[row["item_id"]] = row
    items = []
    for it in cases["items"]:
        iid = it["item_id"]
        anns = [lab[iid] for lab in labels if iid in lab]
        flagged = [ne for _, ne in anns if ne[0]]
        needs = {"flag": bool(flagged), "reason": "; ".join(r or "사유 없음" for _, r in flagged) or None}
        final = None
        row = adj.get(iid)
        if row is not None:
            status = row["status"].strip()
            if status not in ("adjudicated", "ambiguous"):
                raise ValueError(f"{iid}: 판정 기록 status는 adjudicated 또는 ambiguous ({status})")
            adjudication = {"status": status, "adjudicator_id": row.get("adjudicator_id") or None,
                            "adjudicated_at": row.get("adjudicated_at") or None, "reason": row.get("reason") or None,
                            "log_ref": row.get("log_id") or None}
            if status == "adjudicated":
                final = LABEL_MODELS[layer].model_validate(json.loads(row["final_label_json"])).model_dump(mode="json")
            if (row.get("needs_expert") or "").strip().lower() in ("1", "true", "yes", "y"):
                needs = {"flag": True, "reason": row.get("needs_expert_reason") or needs["reason"] or "판정자 표시"}
        else:
            same = len(anns) >= 2 and len({a.label.model_dump_json() for a, _ in anns}) == 1
            confident = all(a.confidence == 3 for a, _ in anns)
            if same and confident and not flagged:
                adjudication = {"status": "agreed"}
                final = anns[0][0].label.model_dump(mode="json")
            else:
                adjudication = {"status": "pending"}
        items.append({"item_id": iid, "layer": layer, "input": it["input"], "source": it["source"],
                      "annotations": [a.model_dump(mode="json") for a, _ in anns],
                      "needs_expert": needs, "adjudication": adjudication, "final": final})
    raw = {"schema_version": "gt-schema/1", "layer": layer, **meta, "items": items}
    return GTSet.model_validate(raw)


def read_adjudication(path: Path | None) -> list[dict]:
    return read_csv(path) if path else []
