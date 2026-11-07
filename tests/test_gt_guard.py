"""gt/에는 템플릿·스키마·문서만 있어야 합니다. 라벨 데이터(세트, 라벨 시트, 판정 기록, FROZEN_GT)는 아직 없어야 합니다.

독립 GT가 생기면 이 테스트를 고치는 것이 아니라, GT 책임자가 동결 절차(gt/README.md)를 거쳐 파일을 추가하고
이 허용 목록을 같은 커밋에서 명시적으로 바꿉니다.
"""
import csv
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GT = ROOT / "gt"
sys.path.insert(0, str(ROOT / "scripts"))

from gt_lib.labelio import GC_LABEL_COLS, GU_LABEL_COLS  # noqa: E402

ALLOWED = {
    "README.md",
    "schema/gt_set.schema.json", "schema/g_u_record.schema.json", "schema/g_c_record.schema.json",
    "schema/g_p_record.schema.json",
    "templates/labeler_instructions_v1_DRAFT.md",
    "templates/g_u_cases.template.json", "templates/g_c_cases.template.json", "templates/g_p_cases.template.json",
    "templates/g_u_label_sheet.template.csv", "templates/g_c_label_sheet.template.csv",
    "templates/g_p_label_sheet.template.json", "templates/adjudication_log.template.csv",
    "templates/set_meta.template.json",
}
LITERAL_KEYS = {"_template", "layer"}


def _files():
    return {p.relative_to(GT).as_posix() for p in GT.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def test_gt_contains_only_allowed_files():
    extra = _files() - ALLOWED
    assert not extra, f"gt/에 허용 목록 밖 파일: {sorted(extra)}"
    assert not (GT / "FROZEN_GT.json").exists()
    for d in ("g_u", "g_c", "g_p", "labels", "results"):
        assert not (GT / d).exists(), f"gt/{d}/가 있음: 라벨 데이터는 동결 절차로만 추가"


def _leaves(o, key=None):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _leaves(v, k)
    elif isinstance(o, list):
        for v in o:
            yield from _leaves(v, key)
    else:
        yield key, o


def test_json_templates_hold_only_placeholders():
    for rel in sorted(f for f in ALLOWED if f.startswith("templates/") and f.endswith(".json")):
        data = json.loads((GT / rel).read_text(encoding="utf-8"))
        first = data[0] if isinstance(data, list) else data
        assert "TEMPLATE" in first.get("_template", ""), rel
        for key, v in _leaves(data):
            if key in LITERAL_KEYS:
                continue
            assert isinstance(v, str) and (v == "" or v.startswith("<")), f"{rel}: {key}={v!r}는 자리표시가 아님"


def test_csv_templates_hold_only_placeholders():
    for rel in sorted(f for f in ALLOWED if f.endswith(".template.csv")):
        rows = list(csv.DictReader(io.StringIO((GT / rel).read_text(encoding="utf-8"))))
        assert len(rows) <= 1, f"{rel}: 템플릿에는 자리표시 한 행만"
        for r in rows:
            for k, v in r.items():
                assert v == "" or v.startswith("<"), f"{rel}: {k}={v!r}는 자리표시가 아님"


def test_label_columns_match_tooling():
    for rel, cols in (("templates/g_u_label_sheet.template.csv", GU_LABEL_COLS),
                      ("templates/g_c_label_sheet.template.csv", GC_LABEL_COLS)):
        header = (GT / rel).read_text(encoding="utf-8").splitlines()[0].split(",")
        assert header[-len(cols):] == cols


def test_templates_and_instructions_reveal_no_engine_output():
    banned = ("directness", "conflicting", "unlocated", "hedged", "CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE",
              "VERIFICATION_PENDING", "INFO_MISSING", "match_status", "validation_flags")
    for rel in sorted(f for f in ALLOWED if f.startswith("templates/")):
        text = (GT / rel).read_text(encoding="utf-8")
        hit = [b for b in banned if b in text]
        assert not hit, f"{rel}에 엔진 출력 용어 {hit}"


def test_fixture_data_is_marked_and_confined():
    fx = ROOT / "tests" / "data" / "gt_fixture"
    files = sorted(fx.glob("*"))
    assert files and all(p.name.startswith("FIXTURE_ONLY") for p in files)
    for p in files:
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["fixture_only"] is True and d["set_id"].startswith("FIXTURE_ONLY")
        assert "FIXTURE_ONLY" in d["description"]
        for it in d["items"]:
            assert it["source"]["origin"] == "fixture"
            for a in it["annotations"]:
                assert a["labeler_id"].startswith("FIXTURE_")
