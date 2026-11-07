"""라벨 시트 -> 두 라벨러 -> kappa·판정 대상 -> 판정 기록 -> 세트 조립 흐름 (FIXTURE_ONLY 가짜 라벨, 임시 폴더)."""
import csv
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gt_agreement  # noqa: E402
import gt_assemble  # noqa: E402
from gt_lib.labelio import read_csv, write_csv  # noqa: E402
from gt_lib.models import freeze_problems, load_set  # noqa: E402

FX = ROOT / "tests" / "data" / "gt_fixture" / "FIXTURE_ONLY_g_u.json"
T = "2026-11-08T00:00:00+00:00"


def _cases(tmp_path):
    raw = json.loads(FX.read_text(encoding="utf-8"))
    cases = {"layer": "G-U", "items": [{"item_id": it["item_id"], "input": it["input"], "source": it["source"]}
                                       for it in raw["items"][:3]]}
    p = tmp_path / "cases.json"
    p.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    meta = {k: raw[k] for k in ("set_id", "gt_version", "fixture_only", "description", "blinding")}
    m = tmp_path / "meta.json"
    m.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return p, m


def _fill(sheet: Path, out: Path, lid: str, decisions: dict):
    rows = read_csv(sheet)
    for r in rows:
        dec, conflict = decisions[r["item_id"]]
        r.update(labeler_id=lid, labeled_at=T, decision=dec, category="FIXTURE_ONLY_cat", hearsay="false",
                 approx="false", conflict=str(conflict).lower(), confidence="3")
    write_csv(out, list(rows[0].keys()), rows)


def test_sheet_has_no_label_or_engine_content(tmp_path):
    cases, _ = _cases(tmp_path)
    sheet = tmp_path / "sheet.csv"
    assert gt_assemble.main(["sheet", "--cases", str(cases), "--out", str(sheet)]) == 0
    rows = list(csv.DictReader(io.StringIO(sheet.read_text(encoding="utf-8"))))
    assert len(rows) == 3
    for r in rows:
        assert r["decision"] == "" and r["labeler_id"] == "" and r["category"] == ""
        assert "directness" not in r and "expected" not in r


def test_full_flow_kappa_queue_adjudication_assembly(tmp_path):
    cases, meta = _cases(tmp_path)
    sheet = tmp_path / "sheet.csv"
    gt_assemble.main(["sheet", "--cases", str(cases), "--out", str(sheet)])
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _fill(sheet, a, "FIXTURE_LABELER_A", {"FX-U-001": ("hold", True), "FX-U-002": ("accept", False),
                                          "FX-U-003": ("accept", False)})
    _fill(sheet, b, "FIXTURE_LABELER_B", {"FX-U-001": ("hold", True), "FX-U-002": ("hold", False),
                                          "FX-U-003": ("accept", False)})
    agr, queue = tmp_path / "agreement.json", tmp_path / "queue.csv"
    assert gt_agreement.main(["--layer", "G-U", "--a", str(a), "--b", str(b), "--out", str(agr),
                              "--queue", str(queue)]) == 0
    res = json.loads(agr.read_text(encoding="utf-8"))
    assert res["n_common"] == 3 and res["fields"]["decision"]["n"] == 3
    q = read_csv(queue)
    assert [r["item_id"] for r in q] == ["FX-U-002"] and "disagreement" in q[0]["trigger"]

    # 조립: 판정 전에는 pending (동결 불가)
    out = tmp_path / "set.json"
    assert gt_assemble.main(["build", "--cases", str(cases), "--meta", str(meta), "--labels", str(a), str(b),
                             "--out", str(out)]) == 0
    s = load_set(out)
    st = {r.item_id: r.adjudication.status for r in s.items}
    assert st == {"FX-U-001": "agreed", "FX-U-002": "pending", "FX-U-003": "agreed"}
    assert any("pending" in p for p in freeze_problems(s, allow_fixture=True))

    # 판정자 결정
    q[0].update(adjudicator_id="FIXTURE_ADJ", adjudicated_at=T, status="adjudicated", reason="FIXTURE_ONLY",
                final_label_json=json.dumps({"decision": "accept", "category": "FIXTURE_ONLY_cat", "hearsay": False,
                                             "approx": False, "conflict": False}))
    log = tmp_path / "adjudication_log.csv"
    write_csv(log, list(q[0].keys()), q)
    assert gt_assemble.main(["build", "--cases", str(cases), "--meta", str(meta), "--labels", str(a), str(b),
                             "--adjudication", str(log), "--out", str(out)]) == 0
    s = load_set(out)
    r2 = next(r for r in s.items if r.item_id == "FX-U-002")
    assert r2.adjudication.status == "adjudicated" and r2.final.decision == "accept"
    assert freeze_problems(s, allow_fixture=True) == []


def test_same_labeler_in_both_sheets_refused(tmp_path):
    cases, _ = _cases(tmp_path)
    sheet = tmp_path / "sheet.csv"
    gt_assemble.main(["sheet", "--cases", str(cases), "--out", str(sheet)])
    a = tmp_path / "a.csv"
    _fill(sheet, a, "FIXTURE_LABELER_A", {k: ("accept", False) for k in ("FX-U-001", "FX-U-002", "FX-U-003")})
    assert gt_agreement.main(["--layer", "G-U", "--a", str(a), "--b", str(a)]) == 1
