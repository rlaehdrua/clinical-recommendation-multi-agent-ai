"""저장소의 outputs/ 쓰기 검사 (테스트 격리). 로컬 실행으로 이미 있는 outputs/ 산출물은 무시하고, 테스트가 새로 쓰거나 바꾼
파일만 잡음. 이전에는 `not (ROOT / "outputs").exists()`로 검사해, 로컬 실행 흔적만 있어도 실패했음 (P14)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def outputs_state(root: Path = ROOT) -> dict[str, int] | None:
    """outputs/ 아래 파일별 (상대 경로 -> 수정 시각·크기). 폴더가 없으면 None."""
    d = root / "outputs"
    if not d.exists():
        return None
    return {str(p.relative_to(d)): p.stat().st_mtime_ns ^ p.stat().st_size for p in d.rglob("*") if p.is_file()}


# 테스트 수집(모듈 import) 시점의 상태. 어떤 테스트도 저장소 outputs/에 쓰지 않으므로 이후 상태가 같아야 함
BEFORE = outputs_state()


def outputs_untouched() -> bool:
    return outputs_state() == BEFORE
