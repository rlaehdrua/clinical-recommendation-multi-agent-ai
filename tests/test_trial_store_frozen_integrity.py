"""잠긴 기준표(FROZEN.json)와 파일 해시가 다르면 로드할 때 integrity_issues로 알림 (감사 V5 F-11).

로드 동작(반환하는 기준, 예외)은 바꾸지 않고 경고만 더합니다. 원본 data/는 읽기만 하고 임시 복사본을 고칩니다.
"""

from __future__ import annotations

import json
import shutil

from ctrec import config, trial_store

TID = "NCT04063540"


def _copy_store(tmp_path):
    for name in (trial_store.FREEZE_FILE, f"{TID}.json"):
        shutil.copy(config.PARSED_DIR / name, tmp_path / name)
    return json.loads((config.TRIALS_DIR / f"{TID}.json").read_text(encoding="utf-8"))


def _issues(parsed) -> list[str]:
    return [i for i in parsed.integrity_issues if i.startswith("frozen_hash")]


def test_unchanged_frozen_file_has_no_frozen_issue(tmp_path):
    trial = _copy_store(tmp_path)
    assert _issues(trial_store.load(trial, tmp_path)) == []


def test_edited_frozen_file_loads_the_same_but_reports_the_mismatch(tmp_path):
    trial = _copy_store(tmp_path)
    path = tmp_path / f"{TID}.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    for r in rec["parsed"]["rules"]:
        if r["rule_id"] == "E2":
            r["value"] = "9.9"  # 잠긴 기준을 손으로 바꿈
    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    p = trial_store.load(trial, tmp_path)
    assert next(r.value for r in p.rules if r.rule_id == "E2") == "9.9"  # 로드 동작은 그대로
    assert len(_issues(p)) == 1 and TID in _issues(p)[0]


def test_store_without_manifest_is_not_checked(tmp_path):
    trial = _copy_store(tmp_path)
    (tmp_path / trial_store.FREEZE_FILE).unlink()  # 후보 저장소처럼 잠금 파일이 없는 저장소
    assert _issues(trial_store.load(trial, tmp_path)) == []
