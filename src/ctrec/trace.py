"""에이전트 실행 기록(trace.jsonl). 발표 자료의 오케스트레이션 흐름 시연에 사용합니다."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


class Tracer:
    def __init__(self, path: Path | None, verbose: bool = True, label: str = "",
                 listener: Callable[[dict], None] | None = None):
        self.path = path
        self.verbose = verbose
        self.label = label  # 여러 환자 동시 처리 시 콘솔 로그 구분용
        self.listener = listener  # 웹 UI 등 실시간 구독자
        self._lock = threading.Lock()
        self._t0 = time.time()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def log(self, agent: str, event: str, **data: Any) -> None:
        record = {"t": round(time.time() - self._t0, 2), "agent": agent, "event": event, **data}
        with self._lock:
            if self.verbose:
                detail = data.get("message") or ", ".join(
                    f"{k}={v}" for k, v in data.items() if isinstance(v, (str, int, float)) and len(str(v)) < 80
                )
                prefix = f"[{self.label}] " if self.label else ""
                print(f"{prefix}[{record['t']:>7.1f}s] {agent:<20} {event:<18} {detail}", flush=True)
            if self.path:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.listener:
            self.listener(record)
