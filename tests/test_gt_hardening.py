"""GT 도구 보강 (코드 검토 D-1..D-4). FIXTURE_ONLY 가짜 데이터만 사용. 수치는 엔진 정확도와 무관합니다."""
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gt_eval  # noqa: E402
from gt_lib import freeze as fz  # noqa: E402
from gt_lib.metrics import ENGINE_FORMAT  # noqa: E402

FX = ROOT / "tests" / "data" / "gt_fixture"
FX_U = FX / "FIXTURE_ONLY_g_u.json"

# FIXTURE_ONLY_g_u 항목 전부에 대한 형식상 올바른 엔진 결과 (값 자체는 의미 없음)
FULL_U = {"FX-U-001": {"directness": "conflicting"}, "FX-U-002": {"directness": "direct"},
          "FX-U-003": {"accepted": True}, "FX-U-004": {"accepted": False}, "FX-U-005": {"accepted": False}}


def _engine_file(tmp_path: Path, gt_path: Path, results: dict, name="engine.json") -> Path:
    out = {"format": ENGINE_FORMAT, "gt_set_id": json.loads(gt_path.read_text(encoding="utf-8"))["set_id"],
           "gt_set_sha256": hashlib.sha256(gt_path.read_bytes()).hexdigest(), "level": "U",
           "items": [{"item_id": k, "result": v} for k, v in results.items()]}
    p = tmp_path / name
    p.write_text(json.dumps(out), encoding="utf-8")
    return p


def _frozen_root(tmp_path: Path) -> Path:
    g = tmp_path / "gt"
    (g / "g_u").mkdir(parents=True)
    shutil.copy(FX_U, g / "g_u" / "FIXTURE_ONLY_g_u.json")
    m = fz.build_manifest(g, gt_version="FIXTURE_ONLY-v0", reference_date="2026-11-07")
    fz.write_manifest(g, m)
    assert fz.verify(g) == []
    return g


# --------------------------------------------------------------------------- D-1

def test_d1_unfrozen_gt_file_is_never_official(tmp_path):
    """동결 루트는 깨끗하지만 --gt 파일이 동결 목록에 없으면 공식 결과가 아닙니다."""
    g = _frozen_root(tmp_path)
    raw = json.loads(FX_U.read_text(encoding="utf-8"))
    raw["description"] += " (FIXTURE_ONLY tampered copy, not frozen)"
    other = tmp_path / "unfrozen.json"
    other.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    eng = _engine_file(tmp_path, other, FULL_U)
    rep = gt_eval.run(other, eng, None, g)
    assert rep["official"] is False
    assert "탐색적" in rep["exploratory"]
    assert any("동결 목록에 없" in b for b in rep["official_blockers"])
    # 공식 결과 요청이면 CLI가 거부
    assert gt_eval.main(["--gt", str(other), "--engine", str(eng), "--frozen", str(g), "--official"]) == 1
    # 공식 요청이 아니면 탐색적 결과로 계산 (기존 동작)
    assert gt_eval.main(["--gt", str(other), "--engine", str(eng), "--frozen", str(g)]) == 0


def test_d1_frozen_file_with_clean_manifest_is_official(tmp_path):
    g = _frozen_root(tmp_path)
    gt_path = g / "g_u" / "FIXTURE_ONLY_g_u.json"
    eng = _engine_file(tmp_path, gt_path, FULL_U)
    rep = gt_eval.run(gt_path, eng, None, g)
    assert rep["official_blockers"] == [] and rep["official"] is True and "exploratory" not in rep
    out = tmp_path / "rep.json"
    assert gt_eval.main(["--gt", str(gt_path), "--engine", str(eng), "--frozen", str(g), "--official",
                         "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["official"] is True


def test_d1_official_refused_when_manifest_fails_or_no_frozen(tmp_path):
    g = _frozen_root(tmp_path)
    gt_path = g / "g_u" / "FIXTURE_ONLY_g_u.json"
    eng = _engine_file(tmp_path, gt_path, FULL_U)
    # 동결 목록 밖 파일이 생기면 verify 실패 -> 공식 아님
    (g / "g_u" / "sneaked_in.json").write_text("{}", encoding="utf-8")
    rep = gt_eval.run(gt_path, eng, None, g)
    assert rep["official"] is False and rep["frozen"]["problems"]
    assert gt_eval.main(["--gt", str(gt_path), "--engine", str(eng), "--frozen", str(g), "--official"]) == 1
    # --frozen 없이 --official 요청 -> 거부
    assert gt_eval.main(["--gt", str(gt_path), "--engine", str(eng), "--official"]) == 1


def test_d1_manifest_hash_of_non_set_file_does_not_count(tmp_path):
    """GT 파일 해시가 세트 폴더(g_u/g_c/g_p)가 아닌 항목(예: protocol/)으로 등록되어 있으면 공식 아님."""
    g = tmp_path / "gt"
    (g / "g_u").mkdir(parents=True)
    (g / "protocol").mkdir()
    shutil.copy(FX_U, g / "protocol" / "copy.json")
    real = json.loads(FX_U.read_text(encoding="utf-8"))
    real["description"] += " (FIXTURE_ONLY second set)"
    (g / "g_u" / "other.json").write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
    fz.write_manifest(g, fz.build_manifest(g, gt_version="FIXTURE_ONLY-v0", reference_date="2026-11-07"))
    gt_path = g / "protocol" / "copy.json"
    eng = _engine_file(tmp_path, gt_path, FULL_U)
    rep = gt_eval.run(gt_path, eng, None, g)
    assert rep["official"] is False
