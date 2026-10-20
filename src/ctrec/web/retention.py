"""웹 실행 산출물의 저장·보존·삭제 정책 (개인정보 최소화).

웹 실행은 기본적으로 디스크에 아무것도 저장하지 않습니다(환자 원문·프로필·질문·답변·판정·보고서가 담기는 파일).
시연·디버깅에 저장이 필요하면 CTREC_WEB_SAVE_OUTPUTS=1로만 켜며, 그때도 CTREC_WEB_OUTPUT_TTL_S(기본 24시간)가 지나면
자동 삭제합니다. CLI·평가용 출력(outputs/<환자>/ 등)은 이 정책과 무관합니다.

자동 삭제 대상은 이 모듈이 만든 실행 폴더뿐입니다.
- outputs/web/ 바로 아래, 이름이 웹 실행 ID 형식이고, 표식 파일(.ctrec-web-run)의 run_id가 폴더 이름과 같은 폴더
- 폴더·표식 파일·내부 항목 어디에도 심볼릭 링크나 정션(reparse point)이 없고, 실제 경로가 outputs/web 안에 있을 때
표식이 없는 기존 폴더(이 정책 이전의 산출물)는 지우지 않고 legacy로 세기만 합니다 (정리는 별도 승인 절차).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

from .. import config

MARKER = ".ctrec-web-run"
RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{32}$")


def _flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


def save_enabled() -> bool:
    return _flag("CTREC_WEB_SAVE_OUTPUTS", False)


def ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("CTREC_WEB_OUTPUT_TTL_S", "") or 24 * 60 * 60))
    except ValueError:
        return 24 * 60 * 60


def web_root() -> Path:
    return config.OUTPUT_DIR / "web"


def prepare_run_dir(run_id: str) -> Path:
    """저장이 켜졌을 때 실행 폴더를 만들고 표식 파일을 씀. 실행 ID 형식이 아니면 거절."""
    if not RUN_ID.match(run_id):
        raise ValueError("잘못된 실행 ID")
    root = web_root()
    d = root / run_id
    d.mkdir(parents=True, exist_ok=False)
    (d / MARKER).write_text(json.dumps({"run_id": run_id, "created_at": time.time()}), encoding="utf-8")
    return d


def _is_link(path: Path) -> bool:
    """심볼릭 링크 또는 Windows 정션 등 reparse point."""
    try:
        st = os.lstat(path)
    except OSError:
        return True  # 확인할 수 없으면 안전하지 않은 것으로 봄
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _contains_link(d: Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(d, followlinks=False):
        for name in dirnames + filenames:
            if _is_link(Path(dirpath) / name):
                return True
    return False


def _marker_time(d: Path) -> float | None:
    m = d / MARKER
    if _is_link(m) or not m.is_file():
        return None
    try:
        data = json.loads(m.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("run_id") != d.name or not isinstance(data.get("created_at"), (int, float)):
        return None
    return float(data["created_at"])


def purge_expired(now: float | None = None, ttl: int | None = None) -> dict:
    """보존 기간이 지난 웹 실행 폴더 삭제. 반환: 건수만 (경로·내용 없음)."""
    now = time.time() if now is None else now
    ttl = ttl_seconds() if ttl is None else ttl
    out = {"deleted": 0, "kept": 0, "legacy": 0, "unsafe": 0}
    root = web_root()
    if not root.exists():
        return out
    if _is_link(root):
        out["unsafe"] += 1
        return out
    real_root = root.resolve()
    for d in root.iterdir():
        if _is_link(d):
            out["unsafe"] += 1
            continue
        if not d.is_dir():
            continue
        created = _marker_time(d) if RUN_ID.match(d.name) else None
        if created is None:
            out["legacy"] += 1  # 이 정책이 만든 폴더가 아님 -> 지우지 않음
            continue
        if d.resolve().parent != real_root or _contains_link(d):
            out["unsafe"] += 1
            continue
        if now - created <= ttl:
            out["kept"] += 1
            continue
        shutil.rmtree(d)
        out["deleted"] += 1
    return out
