"""두 라벨러 일치도 (D 2.5, EN-10)와 판정 대상 목록."""

from __future__ import annotations

from collections import defaultdict

from .stats import cohen_kappa

KAPPA_TARGET = 0.7
KAPPA_RELABEL = 0.6

FIELDS = {
    "G-U": ("decision", "conflict", "hearsay", "approx", "threshold_crossing"),
    "G-C": ("scoped", "scope_unit", "applies_when", "needs_clinical_judgement"),
    "G-P": ("pair_status",),
}


def _get(layer: str, label, f: str):
    if layer == "G-C" and f in ("scoped", "scope_unit", "applies_when"):
        return getattr(label.country_scope, f)
    return getattr(label, f)


def compute(layer: str, a: dict, b: dict) -> dict:
    """a, b: {item_id: (Annotation, needs_expert)} (labelio.read_labels 결과)."""
    common = sorted(set(a) & set(b))
    out = {"layer": layer, "n_common": len(common), "only_a": sorted(set(a) - set(b)),
           "only_b": sorted(set(b) - set(a)), "fields": {}, "per_category": {}, "target": KAPPA_TARGET,
           "relabel_below": KAPPA_RELABEL}
    for f in FIELDS[layer]:
        xs = [str(_get(layer, a[i][0].label, f)) for i in common]
        ys = [str(_get(layer, b[i][0].label, f)) for i in common]
        out["fields"][f] = cohen_kappa(xs, ys)
    if layer == "G-U":
        by = defaultdict(lambda: ([], []))
        cat_disagree = 0
        for i in common:
            ca, cb = a[i][0].label.category, b[i][0].label.category
            if ca != cb:
                cat_disagree += 1
                continue
            by[ca][0].append(a[i][0].label.decision)
            by[ca][1].append(b[i][0].label.decision)
        out["per_category"] = {k: cohen_kappa(*v) for k, v in sorted(by.items())}
        out["category_disagreements"] = cat_disagree
    if layer == "G-P":
        xs, ys = [], []
        for i in common:
            ma = {c.criterion_id: c.status for c in a[i][0].label.criteria}
            mb = {c.criterion_id: c.status for c in b[i][0].label.criteria}
            for cid in sorted(set(ma) | set(mb)):
                xs.append(ma.get(cid, "<없음>"))
                ys.append(mb.get(cid, "<없음>"))
        out["fields"]["criterion_status"] = cohen_kappa(xs, ys)
    low = [f for f, k in out["fields"].items() if k["kappa"] is not None and k["kappa"] < KAPPA_RELABEL]
    out["relabel_fields"] = low
    return out


def adjudication_queue(layer: str, a: dict, b: dict) -> list[dict]:
    """판정 대상: 라벨 불일치, 확신도 3 미만, needs_expert 표시. adjudication_log.csv 행 (결정 칸은 비어 있음)."""
    rows = []
    for i in sorted(set(a) | set(b)):
        anns = [x for x in (a.get(i), b.get(i)) if x is not None]
        triggers = []
        if len(anns) < 2:
            triggers.append("missing_label")
        elif anns[0][0].label != anns[1][0].label:
            triggers.append("disagreement")
        if any(x[0].confidence < 3 for x in anns):
            triggers.append("low_confidence")
        if any(x[1][0] for x in anns):
            triggers.append("needs_expert")
        if triggers:
            rows.append({"log_id": f"ADJ-{len(rows) + 1:04d}", "item_id": i, "layer": layer,
                         "trigger": ";".join(triggers),
                         "labels_summary": " || ".join(f"{x[0].labeler_id}: {x[0].label.model_dump_json()}"
                                                       for x in anns)})
    return rows
