"""라벨러 시트·판정 기록 입출력 (CSV/JSON <-> 스키마 라벨).

파일 종류:
- 사례 파일 (cases): {"layer", "items": [{"item_id", "input", "source"}]}. 사례 작성자가 만듦. 라벨 없음.
- 라벨 시트: 사례 파일에서 만든 빈 CSV(G-U, G-C) 또는 JSON(G-P). 라벨러가 채움. 엔진 출력 열 없음.
- 판정 기록 (adjudication_log.csv): 불일치·확신도 3 미만·needs_expert 항목의 판정자 결정.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from .models import (Annotation, CriterionLabel, CountryScope, PairLabel, StructuredReading, UtteranceLabel)

GU_INPUT_COLS = ["item_id", "kind", "text", "criterion", "quote", "holds", "field", "value", "unit", "reference_date"]
GU_LABEL_COLS = ["labeler_id", "labeled_at", "decision", "category", "hearsay", "approx", "conflict",
                 "threshold_crossing", "concept_cluster", "source_priority_note", "confidence",
                 "evidence_start", "evidence_end", "needs_expert", "needs_expert_reason", "note"]
GC_INPUT_COLS = ["item_id", "trial_id", "criterion_id", "criterion_text", "trial_source"]
GC_LABEL_COLS = ["labeler_id", "labeled_at", "interpretation", "scoped", "scope_unit", "countries", "applies_when",
                 "scope_note", "applicability_condition", "needs_clinical_judgement", "structured_field",
                 "structured_operator", "structured_value", "structured_unit", "confidence",
                 "needs_expert", "needs_expert_reason", "note"]
ADJ_COLS = ["log_id", "item_id", "layer", "trigger", "labels_summary", "adjudicator_id", "adjudicated_at",
            "status", "final_label_json", "reason", "needs_expert", "needs_expert_reason"]
LABEL_COLS = {"G-U": GU_LABEL_COLS, "G-C": GC_LABEL_COLS}
INPUT_COLS = {"G-U": GU_INPUT_COLS, "G-C": GC_INPUT_COLS}


def _b(v: str | None) -> bool | None:
    v = (v or "").strip().lower()
    if v in ("", "na", "n/a"):
        return None
    if v in ("1", "true", "yes", "y", "예"):
        return True
    if v in ("0", "false", "no", "n", "아니오"):
        return False
    raise ValueError(f"참/거짓 값이 아님: {v!r}")


def _s(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


def read_csv(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def write_csv(path: Path, cols: list[str], rows: list[dict]):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})


def make_sheet(cases: dict, path: Path):
    """빈 라벨 시트. 입력 열 + 빈 라벨 열. G-P는 JSON."""
    layer = cases["layer"]
    if layer == "G-P":
        rows = [{"item_id": it["item_id"], "input": it["input"], "labeler_id": "", "labeled_at": "", "confidence": None,
                 "needs_expert": False, "needs_expert_reason": "", "note": "",
                 "label": {"criteria": [], "pair_status": "", "aggregation_rule_version": ""}}
                for it in cases["items"]]
        Path(path).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        return
    rows = []
    for it in cases["items"]:
        row = {"item_id": it["item_id"], **{k: it["input"].get(k) for k in INPUT_COLS[layer] if k != "item_id"}}
        rows.append(row)
    write_csv(path, INPUT_COLS[layer] + LABEL_COLS[layer], rows)


def _spans(r: dict) -> list[dict]:
    s, e = _s(r.get("evidence_start")), _s(r.get("evidence_end"))
    return [{"start": int(s), "end": int(e)}] if s is not None and e is not None else []


def parse_label_row(layer: str, r: dict) -> tuple[Annotation, tuple[bool, str | None]]:
    """시트 한 행 -> (Annotation, (needs_expert, reason))."""
    ne = (bool(_b(r.get("needs_expert"))), _s(r.get("needs_expert_reason")))
    if layer == "G-U":
        label = UtteranceLabel(decision=r["decision"].strip(), category=r["category"].strip(),
                               hearsay=bool(_b(r["hearsay"])), approx=bool(_b(r["approx"])),
                               conflict=bool(_b(r["conflict"])), threshold_crossing=_b(r.get("threshold_crossing")),
                               concept_cluster=_s(r.get("concept_cluster")),
                               source_priority_note=_s(r.get("source_priority_note")))
        ann = Annotation[UtteranceLabel](labeler_id=r["labeler_id"], labeled_at=r["labeled_at"],
                                         confidence=int(r["confidence"]), evidence_spans=_spans(r),
                                         note=_s(r.get("note")), label=label)
    elif layer == "G-C":
        structured = None
        if _s(r.get("structured_field")):
            structured = StructuredReading(field=r["structured_field"], operator=r["structured_operator"],
                                           value=r["structured_value"], unit=_s(r.get("structured_unit")))
        countries = [c.strip() for c in (r.get("countries") or "").split(";") if c.strip()]
        label = CriterionLabel(
            interpretation=r["interpretation"],
            country_scope=CountryScope(scoped=bool(_b(r["scoped"])), scope_unit=r["scope_unit"].strip(),
                                       countries=countries,
                                       applies_when=_s(r.get("applies_when")) or "not_applicable",
                                       note=_s(r.get("scope_note"))),
            applicability_condition=_s(r.get("applicability_condition")),
            needs_clinical_judgement=bool(_b(r["needs_clinical_judgement"])), structured=structured)
        ann = Annotation[CriterionLabel](labeler_id=r["labeler_id"], labeled_at=r["labeled_at"],
                                         confidence=int(r["confidence"]), note=_s(r.get("note")), label=label)
    else:
        ann = Annotation[PairLabel](labeler_id=r["labeler_id"], labeled_at=r["labeled_at"],
                                    confidence=int(r["confidence"]), note=r.get("note") or None,
                                    label=PairLabel.model_validate(r["label"]))
        ne = (bool(r.get("needs_expert")), r.get("needs_expert_reason") or None)
    return ann, ne


def read_labels(layer: str, path: Path) -> dict[str, tuple[Annotation, tuple[bool, str | None]]]:
    rows = json.loads(Path(path).read_text(encoding="utf-8")) if layer == "G-P" else read_csv(path)
    out = {}
    for r in rows:
        iid = r["item_id"]
        if iid in out:
            raise ValueError(f"{path}: {iid} 중복")
        out[iid] = parse_label_row(layer, r)
    return out


LABEL_MODELS = {"G-U": UtteranceLabel, "G-C": CriterionLabel, "G-P": PairLabel}
