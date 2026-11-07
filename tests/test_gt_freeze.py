"""동결·확인 왕복, 변조 탐지, 상태 전이 (FIXTURE_ONLY 데이터, 임시 폴더)."""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gt_freeze  # noqa: E402
import gt_verify  # noqa: E402
from gt_lib import freeze as fz  # noqa: E402

FX = ROOT / "tests" / "data" / "gt_fixture"


def _gt_root(tmp_path: Path) -> Path:
    g = tmp_path / "gt"
    (g / "g_u").mkdir(parents=True)
    shutil.copy(FX / "FIXTURE_ONLY_g_u.json", g / "g_u" / "FIXTURE_ONLY_g_u.json")
    (g / "adjudication_log.csv").write_text("log_id,item_id\nFIXTURE-ADJ-0001,FX-U-005\n", encoding="utf-8")
    (g / "templates").mkdir()
    (g / "templates" / "x.template.csv").write_text("a\n", encoding="utf-8")  # 동결 대상 아님
    return g


def _freeze(g, *extra, version="FIXTURE_ONLY-v0"):
    return gt_freeze.main(["freeze", "--gt-root", str(g), "--version", version, "--reference-date",
                           "2026-11-07", "--allow-fixture", *extra])


def test_round_trip_and_tamper_detection(tmp_path):
    g = _gt_root(tmp_path)
    assert _freeze(g) == 0
    m = json.loads((g / "FROZEN_GT.json").read_text(encoding="utf-8"))
    assert m["status"] == "sealed" and m["gt_version"] == "FIXTURE_ONLY-v0" and m["frozen_at"]
    assert set(m["files"]) == {"g_u/FIXTURE_ONLY_g_u.json", "adjudication_log.csv"}
    assert all(len(v["sha256"]) == 64 for v in m["files"].values())
    assert m["files"]["g_u/FIXTURE_ONLY_g_u.json"]["sha256"] == fz.sha256_file(g / "g_u" / "FIXTURE_ONLY_g_u.json")
    assert m["adjudication_log_sha256"] == m["files"]["adjudication_log.csv"]["sha256"]
    assert "scripts/gt_lib/metrics.py" in m["metric_definitions"]
    assert gt_verify.main(["--gt-root", str(g)]) == 0
    assert fz.verify(g) == []

    # 한 글자 변조
    p = g / "g_u" / "FIXTURE_ONLY_g_u.json"
    p.write_bytes(p.read_bytes().replace(b"11.8", b"11.9"))
    probs = fz.verify(g)
    assert any("동결된 해시와 다름" in x for x in probs)
    assert gt_verify.main(["--gt-root", str(g)]) == 1


def test_missing_and_unlisted_files_detected(tmp_path):
    g = _gt_root(tmp_path)
    assert _freeze(g) == 0
    (g / "adjudication_log.csv").unlink()
    (g / "g_u" / "sneaked_in.json").write_text("{}", encoding="utf-8")
    probs = fz.verify(g)
    assert any("adjudication_log.csv: 파일 없음" in x for x in probs)
    assert any("sneaked_in.json: 동결 목록에 없는 파일" in x for x in probs)


def test_trial_store_hash_change_detected(tmp_path):
    g = _gt_root(tmp_path)
    store = tmp_path / "FROZEN.json"
    store.write_text('{"files": {}}', encoding="utf-8")
    assert _freeze(g, "--trial-store", str(store)) == 0
    assert fz.verify(g) == []
    store.write_text('{"files": {"x": "y"}}', encoding="utf-8")
    assert any("파싱본" in x for x in fz.verify(g))


def test_same_version_cannot_be_refrozen_and_new_version_archives(tmp_path):
    g = _gt_root(tmp_path)
    assert _freeze(g) == 0
    assert _freeze(g) == 1  # 같은 버전 재동결 거부
    assert _freeze(g, version="FIXTURE_ONLY-v1") == 0
    assert (g / "frozen_history" / "FIXTURE_ONLY-v0.json").exists()


def test_fixture_refused_without_flag(tmp_path):
    g = _gt_root(tmp_path)
    assert gt_freeze.main(["freeze", "--gt-root", str(g), "--version", "v", "--reference-date", "2026-11-07"]) == 1
    assert not (g / "FROZEN_GT.json").exists()


def test_set_without_blinding_refused(tmp_path):
    g = _gt_root(tmp_path)
    p = g / "g_u" / "FIXTURE_ONLY_g_u.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["blinding"] = None
    p.write_text(json.dumps(d), encoding="utf-8")
    assert _freeze(g) == 1
    assert not (g / "FROZEN_GT.json").exists()


def test_set_with_leaked_sentence_refused(tmp_path):
    g = _gt_root(tmp_path)
    p = g / "g_u" / "FIXTURE_ONLY_g_u.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    h6 = json.loads((ROOT / "docs" / "dev" / "m_holdout_6_cases.json").read_text(encoding="utf-8"))
    d["items"][1]["input"]["text"] = h6["conflict"][2]["text"]
    d["items"][1]["input"]["quote"] = h6["conflict"][2]["quote"]
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert _freeze(g) == 1
    assert not (g / "FROZEN_GT.json").exists()


def test_external_storage_hash_only(tmp_path):
    """사전 등록: 원문은 저장소 밖, 저장소에는 해시만."""
    g = tmp_path / "gt"
    g.mkdir()
    ext = tmp_path / "private"
    (ext / "g_u").mkdir(parents=True)
    shutil.copy(FX / "FIXTURE_ONLY_g_u.json", ext / "g_u" / "FIXTURE_ONLY_g_u.json")
    assert _freeze(g, "--external", str(ext)) == 0
    m = json.loads((g / "FROZEN_GT.json").read_text(encoding="utf-8"))
    assert m["files"]["g_u/FIXTURE_ONLY_g_u.json"]["storage"] == "external"
    assert not (g / "g_u").exists()
    assert any("외부 보관 파일" in x for x in fz.verify(g))  # 위치 없이는 확인 불가
    assert fz.verify(g, external=ext) == []
    (ext / "g_u" / "FIXTURE_ONLY_g_u.json").write_text("{}", encoding="utf-8")
    assert any("동결된 해시와 다름" in x for x in fz.verify(g, external=ext))


def test_status_moves_forward_only(tmp_path):
    g = _gt_root(tmp_path)
    assert _freeze(g) == 0
    rep = tmp_path / "report.json"
    rep.write_text("{}", encoding="utf-8")
    adv = lambda *a: gt_freeze.main(["advance", "--gt-root", str(g), *a])  # noqa: E731
    assert adv("--to", "measured_once", "--reason", "x") == 1  # 결과·커밋 필요
    assert adv("--to", "measured_once", "--reason", "1회 측정", "--measurement", str(rep),
               "--engine-commit", "abc123") == 0
    assert adv("--to", "sealed", "--reason", "되돌리기") == 1
    assert adv("--to", "measured_once", "--reason", "재측정", "--measurement", str(rep),
               "--engine-commit", "def") == 1
    assert adv("--to", "regression", "--reason", "코드 수정에 사용") == 0
    m = json.loads((g / "FROZEN_GT.json").read_text(encoding="utf-8"))
    assert [h["status"] for h in m["status_history"]] == ["sealed", "measured_once", "regression"]
    assert m["measurements"][0]["engine_commit"] == "abc123"
    assert fz.verify(g) == []
    m["status_history"].reverse()  # 이력 조작
    (g / "FROZEN_GT.json").write_text(json.dumps(m), encoding="utf-8")
    assert any("순서" in x for x in fz.verify(g))
