"""GT 동결 (D 2.7). Frozen Store(src/ctrec/store_patch.py verify_frozen, trial_store.freeze)와 같은 방식:
파일별 sha256 목록을 쓰고, 확인은 다시 계산해 비교합니다. 동결된 파일은 고치지 않고 새 버전을 만듭니다.

상태는 sealed -> measured_once -> regression 순서로만 바뀝니다.
- sealed: 측정 전. 원문을 저장소 밖에 두고 해시만 커밋할 수 있음 (storage=external, 사전 등록)
- measured_once: 고정 커밋에서 1회 측정, 결과 해시 기록
- regression: 결과를 보고 코드를 고쳤거나 회귀 세트로 옮김. 더 이상 독립 평가 아님
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import GT_LIB_DIR, REPO_ROOT

FROZEN_NAME = "FROZEN_GT.json"
MANIFEST_FORMAT = "gt-frozen/1"
STATUSES = ("sealed", "measured_once", "regression")
# 동결 대상 폴더·파일 (gt/ 기준). templates/, README.md, FROZEN_GT.json, frozen_history/는 제외.
HASHED_DIRS = ("g_u", "g_c", "g_p", "labels", "protocol", "schema")
HASHED_FILES = ("adjudication_log.csv", "agreement.json", "preregistration_v1.md")
SET_DIRS = ("g_u", "g_c", "g_p")
# 평가 결과를 바꿀 수 있는 코드. 동결 뒤 바뀌면 verify()가 문제로 보고하고, 그 뒤 결과는 공식이 아님.
# (키 이름은 이전 매니페스트와 맞추려고 metric_definitions를 그대로 씀)
METRIC_DEFINITION_FILES = ("scripts/gt_lib/metrics.py", "scripts/gt_lib/stats.py", "scripts/gt_lib/models.py",
                           "scripts/gt_lib/labelio.py", "scripts/gt_lib/freeze.py", "scripts/gt_eval.py",
                           "scripts/gt_engine_adapter.py")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def collect_files(base: Path) -> dict[str, Path]:
    """base 아래 동결 대상 파일 {상대 경로(posix): 경로}."""
    out = {}
    for d in HASHED_DIRS:
        for p in sorted((base / d).rglob("*")) if (base / d).is_dir() else []:
            if p.is_file():
                out[p.relative_to(base).as_posix()] = p
    for f in HASHED_FILES:
        if (base / f).is_file():
            out[f] = base / f
    return out


def build_manifest(gt_root: Path, *, gt_version: str, reference_date: str, external: Path | None = None,
                   trial_store: list[Path] | None = None, labelers: list[str] | None = None,
                   adjudicator: str | None = None, note: str = "", repo_root: Path | None = None) -> dict:
    repo_root = repo_root or REPO_ROOT
    files = {}
    for rel, p in collect_files(gt_root).items():
        files[rel] = {"sha256": sha256_file(p), "storage": "repo"}
    if external is not None:
        for rel, p in collect_files(external).items():
            if rel in files:
                raise ValueError(f"{rel}: 저장소와 외부 보관소에 같은 경로가 있습니다")
            files[rel] = {"sha256": sha256_file(p), "storage": "external"}
    if not any(k.split("/")[0] in SET_DIRS for k in files):
        raise ValueError("동결할 GT 세트 파일(g_u/, g_c/, g_p/)이 없습니다")
    agreement = None
    for base in (gt_root, external):
        if base is not None and (base / "agreement.json").is_file():
            agreement = json.loads((base / "agreement.json").read_text(encoding="utf-8"))
    adj_sha = files.get("adjudication_log.csv", {}).get("sha256")

    def _rel(p: Path) -> str:
        p = Path(p).resolve()
        try:
            return p.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            return p.as_posix()

    return {
        "format": MANIFEST_FORMAT,
        "gt_version": gt_version,
        "frozen_at": now_iso(),
        "reference_date": reference_date,
        "note": note,
        "status": "sealed",
        "status_history": [{"status": "sealed", "at": now_iso(), "reason": "동결"}],
        "files": files,
        "metric_definitions": {f: sha256_file(repo_root / f) for f in METRIC_DEFINITION_FILES
                               if (repo_root / f).is_file()},
        "trial_store": {_rel(p): sha256_file(p) for p in (trial_store or [])},
        "labelers": sorted(labelers or []),
        "adjudicator": adjudicator,
        "agreement": agreement,
        "adjudication_log_sha256": adj_sha,
        "measurements": [],
    }


def write_manifest(gt_root: Path, manifest: dict) -> Path:
    """기존 FROZEN_GT.json이 있으면: 같은 버전이면 거부, 다른 버전이면 frozen_history/로 옮긴 뒤 씀."""
    path = gt_root / FROZEN_NAME
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("gt_version") == manifest["gt_version"]:
            raise ValueError(f"{manifest['gt_version']}은 이미 동결됨. 고치려면 새 버전(예: gt-v1.1)을 만드세요")
        hist = gt_root / "frozen_history"
        hist.mkdir(exist_ok=True)
        (hist / f"{old.get('gt_version', 'unknown')}.json").write_text(path.read_text(encoding="utf-8"),
                                                                       encoding="utf-8")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def verify(gt_root: Path, *, external: Path | None = None, repo_root: Path | None = None) -> list[str]:
    """동결 목록과 현재 파일이 같은지. 반환: 문제 목록 (비어 있으면 정상)."""
    repo_root = repo_root or REPO_ROOT
    path = gt_root / FROZEN_NAME
    if not path.exists():
        return [f"{path}이 없습니다"]
    m = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if m.get("format") != MANIFEST_FORMAT:
        problems.append(f"알 수 없는 형식 {m.get('format')}")
    if m.get("status") not in STATUSES:
        problems.append(f"알 수 없는 상태 {m.get('status')}")
    hist = [h.get("status") for h in m.get("status_history", [])]
    ordered = all(h in STATUSES for h in hist) and all(
        STATUSES.index(a) < STATUSES.index(b) for a, b in zip(hist, hist[1:]))
    if not ordered:
        problems.append(f"상태 이력이 순서를 어김: {hist}")
    elif hist and hist[-1] != m.get("status"):
        problems.append("status와 status_history 끝이 다릅니다")
    files = m.get("files", {})
    for rel, info in files.items():
        base = gt_root if info.get("storage") == "repo" else external
        if base is None:
            problems.append(f"{rel}: 외부 보관 파일 (--external로 위치를 주어야 확인 가능)")
            continue
        p = base / rel
        if not p.is_file():
            problems.append(f"{rel}: 파일 없음")
        elif sha256_file(p) != info.get("sha256"):
            problems.append(f"{rel}: 동결된 해시와 다름")
    present = set(collect_files(gt_root))
    if external is not None:
        present |= set(collect_files(external))
    for rel in sorted(present - set(files)):
        problems.append(f"{rel}: 동결 목록에 없는 파일")
    for rel, digest in m.get("metric_definitions", {}).items():
        p = repo_root / rel
        if not p.is_file() or sha256_file(p) != digest:
            problems.append(f"지표 정의·평가 코드 {rel}: 동결 뒤 바뀜 (측정 결과는 '탐색적'으로 표시해야 함)")
    for rel, digest in m.get("trial_store", {}).items():
        p = Path(rel) if Path(rel).is_absolute() else repo_root / rel
        if not p.is_file() or sha256_file(p) != digest:
            problems.append(f"파싱본 {rel}: 동결 뒤 바뀜 (기준 ID 대응을 다시 확인해야 함)")
    return problems


def official_blockers(gt_digest: str, gt_root: Path, *, external: Path | None = None,
                      repo_root: Path | None = None) -> tuple[list[str], dict]:
    """공식(비탐색적) 결과가 되기 위한 동결 조건. 반환: (막는 사유 목록, {"problems", "status", "listed_as"}).

    1) FROZEN_GT.json이 verify()를 통과하고, 2) 상태가 sealed이고, 3) 평가하는 GT 파일의 sha256이
    매니페스트의 세트 파일(g_u/, g_c/, g_p/) 해시로 등록되어 있어야 합니다.
    """
    problems = verify(gt_root, external=external, repo_root=repo_root)
    status, listed = None, None
    path = gt_root / FROZEN_NAME
    if path.exists():
        m = json.loads(path.read_text(encoding="utf-8"))
        status = m.get("status")
        for rel, info in m.get("files", {}).items():
            if rel.split("/")[0] in SET_DIRS and info.get("sha256") == gt_digest:
                listed = rel
                break
    blockers = []
    if problems:
        blockers.append(f"동결 확인 실패 {len(problems)}건")
    if status != "sealed":
        blockers.append(f"상태가 sealed가 아님 ({status})")
    if listed is None:
        blockers.append("GT 파일의 sha256이 동결 목록에 없음 (동결되지 않았거나 변조된 파일)")
    return blockers, {"problems": problems, "status": status, "listed_as": listed}


def advance(gt_root: Path, to: str, *, reason: str, measurement: Path | None = None,
            engine_commit: str | None = None, external: Path | None = None, repo_root: Path | None = None) -> dict:
    """상태를 앞으로만 옮김. measured_once에는 측정 결과 파일이 필요. 먼저 verify()가 깨끗해야 합니다."""
    problems = verify(gt_root, external=external, repo_root=repo_root)
    if problems:
        raise ValueError(f"동결 확인 실패 {len(problems)}건이라 상태를 바꾸지 않습니다: {problems[:5]}")
    path = gt_root / FROZEN_NAME
    m = json.loads(path.read_text(encoding="utf-8"))
    cur = m["status"]
    if to not in STATUSES or STATUSES.index(to) <= STATUSES.index(cur):
        raise ValueError(f"상태는 앞으로만 바뀝니다: {cur} -> {to} 불가")
    if to == "measured_once":
        if measurement is None or not engine_commit:
            raise ValueError("measured_once에는 --measurement와 --engine-commit이 필요합니다")
        m["measurements"].append({"at": now_iso(), "engine_commit": engine_commit,
                                  "report": Path(measurement).as_posix(), "report_sha256": sha256_file(measurement)})
    m["status"] = to
    m["status_history"].append({"status": to, "at": now_iso(), "reason": reason})
    path.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m


__all__ = ["build_manifest", "write_manifest", "verify", "official_blockers", "advance", "sha256_file", "collect_files", "GT_LIB_DIR"]
