"""추가 정보 확인 사례 저장소 (TRIALOG O): 사례마다 JSON 파일 하나.

- 위치: CTREC_FOLLOWUP_DIR (기본 outputs/followup). git에 넣지 않는 outputs/ 아래입니다.
  환자 입력 원문과 답변이 들어 있으므로 운영 배포에서는 접근이 제한된 저장 위치를 지정해야 합니다 (보관 기간 정책은 후속).
- 쓰기: 임시 파일에 쓴 뒤 바꿔 끼움 (중간에 프로세스가 끝나도 이전 기록 또는 새 기록 중 하나만 남음).
- 동시성: 같은 프로세스 안에서는 사례별 잠금. 저장할 때 디스크의 개정 번호가 읽은 값과 다르면 거절(StaleWrite).
  여러 프로세스가 같은 디렉터리를 쓰는 배포는 아직 지원하지 않습니다.
- 사례 ID는 128비트 난수 (경로 조작 불가: 형식이 맞는 ID만 파일 이름이 됨).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path

from .. import config
from .models import FORMAT

CASE_ID = re.compile(r"^FC-[0-9a-f]{32}$")


class CaseNotFound(KeyError):
    pass


class StaleWrite(RuntimeError):
    """읽은 뒤 다른 쓰기가 먼저 저장됨."""


def default_root() -> Path:
    env = os.getenv("CTREC_FOLLOWUP_DIR", "").strip()
    return Path(env) if env else config.OUTPUT_DIR / "followup"


def new_case_id() -> str:
    return "FC-" + secrets.token_hex(16)


class CaseStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else default_root()
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def _path(self, case_id: str) -> Path:
        if not CASE_ID.match(case_id or ""):
            raise CaseNotFound(case_id)
        return self.root / f"{case_id}.json"

    def lock(self, case_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(case_id, threading.RLock())

    @contextmanager
    def editing(self, case_id: str):
        """사례를 잠그고 읽어 돌려줌. 블록이 정상 종료하면 개정 번호를 올려 저장 (예외면 저장하지 않음)."""
        with self.lock(case_id):
            data = self.load(case_id)
            base = data["revision"]
            yield data
            self.save(data, expected_revision=base)

    def exists(self, case_id: str) -> bool:
        try:
            return self._path(case_id).exists()
        except CaseNotFound:
            return False

    def load(self, case_id: str) -> dict:
        p = self._path(case_id)
        if not p.exists():
            raise CaseNotFound(case_id)
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT or data.get("case_id") != case_id:
            raise ValueError(f"{p.name}: not a followup case record")
        return data

    def save(self, data: dict, expected_revision: int | None) -> None:
        p = self._path(data["case_id"])
        with self.lock(data["case_id"]):
            if p.exists():
                on_disk = json.loads(p.read_text(encoding="utf-8")).get("revision")
                if expected_revision is None or on_disk != expected_revision:
                    raise StaleWrite(data["case_id"])
            elif expected_revision is not None:
                raise CaseNotFound(data["case_id"])
            data["revision"] = (expected_revision or 0) + 1
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".{secrets.token_hex(4)}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, p)

    def case_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("FC-*.json") if CASE_ID.match(p.stem))
