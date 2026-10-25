"""IR·질문 계약의 사이드카 저장소와 사람 검수 기록 (재설계 P2·P3). store_patch.py의 '제안 -> 승인(digest) -> 적용' 패턴.

저장 위치 (Frozen Store와 분리, data/ 밖): <root>/ir/<trial_id>.json, <root>/question_contracts.json, <root>/reviews.json
root 기본값은 저장소의 contracts/ 입니다. 이 모듈은 data/parsed/와 FROZEN.json을 읽기만 합니다.

활성화 규칙 (모두 만족해야 결정적 확정에 사용)
1. 형식 검사 통과 (CriterionIR.errors() 비어 있음)
2. 잠긴 기준표와 일치: IR의 frozen_file_hash == FROZEN.json의 해시 == 실제 파일 해시, 그리고 rule_id가 있고 원문이 같음
3. 사람 검수 기록: 현재 IR 내용 digest에 대한 'approved' 기록이 있고, 같은 digest의 'rejected'가 그 뒤에 없음
   (내용이 한 글자라도 바뀌면 digest가 달라져 이전 승인은 효력이 없음)
파일 안의 review_status 값은 표시용이며 활성화에 쓰지 않습니다. 자동화 도구 이름으로는 승인을 기록할 수 없습니다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .condition_ir import CriterionIR, IRError

DEFAULT_ROOT = config.ROOT / "contracts"
REVIEWS = "reviews.json"
# 사람이 아닌 기록자 (LLM·스크립트가 스스로 승인하지 못하게)
_AUTOMATED = ("claude", "llm", "gpt", "auto", "bot", "script", "codex", "assistant")

IRStatus = str  # active | unreviewed | rejected | invalid | stale_frozen | missing_rule


class ReviewError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def frozen_state(trial_id: str, parsed_dir: Path | None = None) -> tuple[str | None, dict | None]:
    """(잠긴 해시, 잠긴 기준표 내용). FROZEN.json에 없거나 실제 파일 해시가 다르면 (None, None)."""
    store = parsed_dir or config.PARSED_DIR
    manifest = _read(store / "FROZEN.json", {})
    h = manifest.get("files", {}).get(trial_id)
    path = store / f"{trial_id}.json"
    if not h or not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != h:
        return None, None
    return h, json.loads(path.read_text(encoding="utf-8"))


@dataclass
class IRRecord:
    ir: CriterionIR | None
    status: IRStatus
    reasons: list[str]
    raw: dict

    @property
    def active(self) -> bool:
        return self.status == "active"


class Registry:
    """사이드카 IR·계약과 검수 기록을 읽어 상태를 계산. 쓰기는 record_review만 (검수자가 직접 호출)."""

    def __init__(self, root: Path | None = None, parsed_dir: Path | None = None):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.parsed_dir = parsed_dir

    # -------------------------------------------------------------- 검수 기록
    def reviews(self) -> dict:
        return _read(self.root / REVIEWS, {"format_version": 1, "ir": {}, "contracts": {}})

    def _decision(self, kind: str, key: str, digest: str) -> str | None:
        last = None
        for r in self.reviews().get(kind, {}).get(key, []):
            if r.get("digest") == digest:
                last = r.get("decision")
        return last

    def record_review(self, kind: str, key: str, digest: str, decision: str, *, reviewer: str, note: str,
                      clinical_review_confirmed: bool) -> dict:
        """사람 검수 결과 기록. kind = ir | contracts. 자동화 도구 이름, 빈 사유, 확인 플래그 없는 승인은 거부."""
        if kind not in ("ir", "contracts"):
            raise ReviewError(f"kind {kind!r}")
        if decision not in ("approved", "rejected"):
            raise ReviewError(f"decision {decision!r}")
        who = (reviewer or "").strip()
        if not who or any(t in who.lower() for t in _AUTOMATED):
            raise ReviewError(f"사람 검수자 이름이 필요 (받은 값 {reviewer!r})")
        if not (note or "").strip():
            raise ReviewError("검수 사유(note)가 필요")
        if decision == "approved" and not clinical_review_confirmed:
            raise ReviewError("승인에는 clinical_review_confirmed=True (원문·기간·기준 시점·예외 확인)가 필요")
        if decision == "approved" and kind == "ir":
            rec = self.ir_record(*key.split(":", 1), require_review=False)
            if rec.ir is None or rec.ir.digest() != digest or rec.status not in ("active", "unreviewed"):
                raise ReviewError(f"현재 IR과 맞지 않거나 사용할 수 없는 IR은 승인할 수 없음: {rec.status} {rec.reasons}")
        if decision == "approved" and kind == "contracts":
            from .question_contract import ContractRegistry
            c = ContractRegistry(self).raw(key)
            if c is None or c.digest() != digest or c.errors():
                raise ReviewError("현재 계약과 맞지 않거나 형식 오류가 있는 계약은 승인할 수 없음")
        data = self.reviews()
        entry = {"digest": digest, "decision": decision, "reviewer": who, "note": note.strip(), "at": _now()}
        data.setdefault(kind, {}).setdefault(key, []).append(entry)
        _write(self.root / REVIEWS, data)
        return entry

    # -------------------------------------------------------------- IR
    def ir_file(self, trial_id: str) -> dict:
        return _read(self.root / "ir" / f"{trial_id}.json", {"criteria": []})

    def trial_ids(self) -> list[str]:
        d = self.root / "ir"
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    def ir_record(self, trial_id: str, rule_id: str, *, require_review: bool = True) -> IRRecord:
        raws = [c for c in self.ir_file(trial_id).get("criteria", []) if c.get("rule_id") == rule_id]
        if not raws:
            return IRRecord(None, "missing_rule", ["IR 없음"], {})
        if len(raws) > 1:
            return IRRecord(None, "invalid", ["같은 기준의 IR이 여러 개"], raws[0])
        raw = raws[0]
        try:
            ir = CriterionIR.from_dict(raw)
        except IRError as e:
            return IRRecord(None, "invalid", [str(e)], raw)
        if ir.trial_id != trial_id:
            return IRRecord(ir, "invalid", ["trial_id 불일치"], raw)
        errs = ir.errors()
        if errs:
            return IRRecord(ir, "invalid", errs, raw)
        frozen_hash, frozen = frozen_state(trial_id, self.parsed_dir)
        if frozen_hash is None or ir.frozen_file_hash != frozen_hash:
            return IRRecord(ir, "stale_frozen", ["잠긴 기준표 해시와 IR의 해시가 다름(또는 잠금 없음)"], raw)
        rules = {r["rule_id"]: r for r in frozen["parsed"]["rules"]}
        if rule_id not in rules:
            return IRRecord(ir, "stale_frozen", ["잠긴 기준표에 rule_id 없음"], raw)
        if rules[rule_id]["text"] != ir.criterion_text:
            return IRRecord(ir, "stale_frozen", ["잠긴 기준표의 원문과 IR 원문이 다름"], raw)
        if not require_review:
            return IRRecord(ir, "unreviewed", [], raw)
        decision = self._decision("ir", ir.key, ir.digest())
        if decision == "approved":
            return IRRecord(ir, "active", [], raw)
        if decision == "rejected":
            return IRRecord(ir, "rejected", ["검수에서 거부됨"], raw)
        return IRRecord(ir, "unreviewed", ["현재 내용에 대한 사람 검수 승인 없음"], raw)

    def trial_records(self, trial_id: str) -> dict[str, IRRecord]:
        ids = [c.get("rule_id") for c in self.ir_file(trial_id).get("criteria", [])]
        return {rid: self.ir_record(trial_id, rid) for rid in dict.fromkeys(ids) if rid}

    def active_irs(self, trial_id: str) -> dict[str, CriterionIR]:
        return {rid: r.ir for rid, r in self.trial_records(trial_id).items() if r.active and r.ir is not None}
