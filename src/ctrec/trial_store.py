"""사전 컴파일한 시험 기준의 고정 저장소 - 팀 결정 D8 Option C.

대회 범위의 시험(data/trials/)은 모두 미리 파싱·검증해 data/parsed/<trial_id>.json에 저장하고, 환자를 평가할 때는
이 결과만 씁니다. 같은 시험이 환자마다 다른 기준 목록으로 평가되지 않고, 저장소가 git에 있으므로 팀원 모두 같은 기준을 씁니다.

- 저장소에 없거나 시험 원문이 바뀐 시험(범위 밖) -> CompilationPending (파이프라인이 NOT_EVALUATED로 기록)
- config.LAZY_COMPILE=1이면 그 자리에서 파싱 (검증 없음, 데모·검색 시연용)
- 추가·갱신: python scripts/compile_trials.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .agents import criteria_parser
from .agents.parser_validator import ValidationReport
from .schemas import ParsedTrial, RuleMeta

FORMAT_VERSION = 1


class CompilationPending(RuntimeError):
    """고정 저장소에 컴파일된 기준이 없는 시험."""


def trial_hash(trial: dict) -> str:
    """파서가 보는 시험 원문의 해시. 모집 상태·기관 목록이 바뀌어도 기준 원문이 같으면 같은 값."""
    return hashlib.sha256(criteria_parser._render_trial(trial).encode()).hexdigest()[:16]


def parser_prompt_hash() -> str:
    return hashlib.sha256((config.MODEL + criteria_parser.SYSTEM).encode()).hexdigest()[:16]


def path_for(trial_id: str, store_dir: Path | None = None) -> Path:
    return (store_dir or config.PARSED_DIR) / f"{trial_id}.json"


def save(trial: dict, parsed: ParsedTrial, meta: dict[str, RuleMeta], report: ValidationReport,
         store_dir: Path | None = None, extra: dict | None = None) -> Path:
    path = path_for(trial["trial_id"], store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format_version": FORMAT_VERSION,
        "trial_id": trial["trial_id"],
        "trial_hash": trial_hash(trial),
        "parser_prompt_hash": parser_prompt_hash(),
        "model": config.MODEL,
        "compiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **(extra or {}),
        "parsed": parsed.model_dump(),
        "rule_meta": {rid: m.model_dump() for rid, m in meta.items()},
        "validation": report.model_dump(),
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def read_record(trial_id: str, store_dir: Path | None = None) -> dict | None:
    path = path_for(trial_id, store_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def is_current(trial: dict, store_dir: Path | None = None) -> bool:
    rec = read_record(trial["trial_id"], store_dir)
    return rec is not None and rec.get("trial_hash") == trial_hash(trial)


def load(trial: dict, store_dir: Path | None = None) -> ParsedTrial:
    """컴파일된 기준. 없거나 원문이 바뀌었으면 CompilationPending."""
    rec = read_record(trial["trial_id"], store_dir)
    if rec is None:
        raise CompilationPending(f"COMPILATION_PENDING: {trial['trial_id']}의 사전 컴파일 기준이 없음 "
                                 f"(대회 범위 밖 시험) - scripts/compile_trials.py로 추가")
    if rec.get("trial_hash") != trial_hash(trial):
        raise CompilationPending(f"COMPILATION_PENDING: {trial['trial_id']}의 기준 원문이 컴파일 이후 바뀜 "
                                 f"- scripts/compile_trials.py로 다시 컴파일")
    parsed = ParsedTrial.model_validate(rec["parsed"])
    parsed.rule_meta = {rid: RuleMeta.model_validate(m) for rid, m in (rec.get("rule_meta") or {}).items()}
    det = (rec.get("validation") or {}).get("deterministic", {})
    parsed.integrity_issues = [f"{rid}: {m}" for rid, msgs in det.items() for m in msgs]
    return parsed


def get(trial: dict) -> ParsedTrial:
    """파이프라인이 쓰는 진입점: 고정 저장소 -> (LAZY_COMPILE이면) 즉석 파싱 -> CompilationPending."""
    try:
        return load(trial)
    except CompilationPending:
        if not config.LAZY_COMPILE:
            raise
    return criteria_parser.parse_trial(trial)
