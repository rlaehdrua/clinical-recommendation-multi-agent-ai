"""문서 검증·임상의 판단 권한, 문서 저장, 감사 기록 (TRIALOG O 후속: 문서 검증 및 임상의 판단 완료 경로).

운영 경로와 합성 시험 경로를 나눕니다.
- 운영(기본, CTREC_VERIFICATION_MODE 미설정): 실제 의료기관의 사용자 인증·권한 시스템과 연결돼 있지 않으므로 문서 검증과
  임상의 판단을 인정하는 권한이 없습니다. 권한은 NoDocumentAuthority / NoClinicianAuthority이고 완료 API는 503입니다.
  CTREC_VERIFICATION_MODE에 synthetic 외의 값(예: production)을 넣어도 운영 권한이 생기지 않고 503입니다.
- 합성(CTREC_VERIFICATION_MODE=synthetic): 합성 사례(사례 기록 synthetic=True, 고정 시험 저장소가 아닌 카탈로그)에서만
  동작합니다. 실제 시험 저장소 사례는 거부(403). 역할마다 다른 서버 키:
    CTREC_SYNTHETIC_DOC_VERIFIER_KEY : 문서 검증자 (X-CTREC-Verifier-Key)
    CTREC_SYNTHETIC_CLINICIAN_KEY    : 임상의 (X-CTREC-Clinician-Key)
    CTREC_SYNTHETIC_SIGNING_SECRET   : 증명·판단 서명용 비밀 (요청으로 받지 않음)
  키는 16자 이상, 서로 다르고 연구 담당자 키(CTREC_STAFF_KEY)와도 달라야 합니다. 아니면 설정 오류로 503.
  공유 키는 사람 계정이 아니라 "이 키를 가진 쪽"만 확인합니다: 운영 인증이 아님.

증명·판단은 서버 비밀로 서명(HMAC-SHA256)하고 사례·문서 해시·사실 값(fact_digest)에 묶습니다. 서명 비밀이 바뀌거나
없어진 서버에서는 저장된 증명·판단이 인정되지 않고(실패 시 닫힘) 기준은 다시 보류됩니다.
검증자 식별자에 synthetic- 접두어를 붙여, 나중에 생길 운영 권한이 합성 증명을 받아들이지 않게 합니다.

감사 기록: 사례마다 추가 전용 JSONL(<root>/audit/<case_id>.jsonl), 항목마다 이전 항목의 해시를 담는 연결 해시.
사례 기록의 events는 저장이 끝난 뒤 순서대로 옮기고(sync), 권한 확인 뒤 거부된 시도도 남깁니다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..clinician_authority import ClinicianJudgment, NoClinicianAuthority
from ..decision_state import criterion_version
from ..document_authority import NoDocumentAuthority, ServerAttestation, fact_digest
from ..fact_changes import take_snapshot

MODE_ENV = "CTREC_VERIFICATION_MODE"
DOC_KEY_ENV = "CTREC_SYNTHETIC_DOC_VERIFIER_KEY"
CLINICIAN_KEY_ENV = "CTREC_SYNTHETIC_CLINICIAN_KEY"
SIGNING_ENV = "CTREC_SYNTHETIC_SIGNING_SECRET"
SYNTHETIC = "synthetic"
MIN_KEY_CHARS = 16

DOC_VERIFIER_PREFIX = "synthetic-document-verifier:"
CLINICIAN_PREFIX = "synthetic-clinician:"

DOCUMENT_ID = re.compile(r"^DOC-[0-9a-f]{16}$")
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
DOCUMENT_KINDS = ("lab_report", "pathology_report", "genomic_report", "imaging_report", "clinical_note", "other")
MEDIA_TYPES = ("application/pdf", "image/png", "image/jpeg", "text/plain")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

class VerificationConfig:
    """환경 변수에서 읽은 검증 권한 설정. problems()가 비어 있을 때만 합성 권한이 동작."""

    def __init__(self, env: dict | None = None):
        e = os.environ if env is None else env
        self.mode = (e.get(MODE_ENV) or "").strip().lower()
        self.doc_key = e.get(DOC_KEY_ENV) or ""
        self.clinician_key = e.get(CLINICIAN_KEY_ENV) or ""
        self.secret = e.get(SIGNING_ENV) or ""
        self.staff_key = e.get("CTREC_STAFF_KEY") or ""

    @property
    def synthetic(self) -> bool:
        return self.mode == SYNTHETIC and not self.problems()

    def problems(self) -> list[str]:
        if not self.mode:
            return ["verification_not_configured"]
        if self.mode != SYNTHETIC:
            return ["production_authority_unavailable"]  # 운영 권한 어댑터가 없음
        out = []
        keys = {DOC_KEY_ENV: self.doc_key, CLINICIAN_KEY_ENV: self.clinician_key, SIGNING_ENV: self.secret}
        out += [f"{name}_missing_or_short" for name, v in keys.items() if len(v) < MIN_KEY_CHARS]
        values = [v for v in (self.doc_key, self.clinician_key, self.secret, self.staff_key) if v]
        if len(values) != len(set(values)):
            out.append("role_keys_not_distinct")
        return out

    def _key(self, purpose: str) -> bytes:
        return hmac.new(self.secret.encode(), purpose.encode(), hashlib.sha256).digest()

    def doc_signing_key(self) -> bytes:
        return self._key("ctrec.document_attestation/v1")

    def clinician_signing_key(self) -> bytes:
        return self._key("ctrec.clinician_judgment/v1")


def key_matches(given: str | None, expected: str) -> bool:
    if not given or not expected:
        return False
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest())


# ---------------------------------------------------------------------------
# 권한 (합성 시험 전용)
# ---------------------------------------------------------------------------

class SyntheticCaseDocumentAuthority:
    """합성 사례 하나의 문서 권한: 이 사례에 등록된 문서(해시 일치)로 이 서버 비밀이 서명한 증명만 인정."""

    def __init__(self, key: bytes, case_id: str, documents: dict[str, str]):
        self._key, self.case_id, self.documents = key, case_id, dict(documents)

    def _sign(self, document_id: str, sha256: str, digest: str, verifier: str) -> str:
        body = "|".join((self.case_id, document_id, sha256, digest, verifier))
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()

    def issue(self, fact, document_id: str, verifier_id: str) -> ServerAttestation:
        sha = self.documents[document_id]
        verifier = DOC_VERIFIER_PREFIX + verifier_id
        digest = fact_digest(fact)
        return ServerAttestation(document_id=document_id, sha256=sha, fact_digest=digest, verifier=verifier,
                                 signature=self._sign(document_id, sha, digest, verifier))

    def valid(self, attestation: ServerAttestation, fact) -> bool:
        a = attestation
        return (a.verifier.startswith(DOC_VERIFIER_PREFIX) and self.documents.get(a.document_id) == a.sha256
                and a.fact_digest == fact_digest(fact)
                and hmac.compare_digest(a.signature, self._sign(a.document_id, a.sha256, a.fact_digest, a.verifier)))


class SyntheticCaseClinicianAuthority:
    """합성 사례 하나의 임상의 권한: 서명이 맞고, 기준이 그대로이고, 판단 뒤 사실이 바뀌지 않은 판단만 인정."""

    def __init__(self, key: bytes, case_id: str, judgments: list[dict], parsed: dict, fact_version):
        self._key, self.case_id, self.parsed = key, case_id, parsed
        self._fact_version = fact_version  # 호출할 때의 사실 버전을 돌려주는 함수
        self.judgments = [ClinicianJudgment.model_validate(j) for j in judgments]

    def _sign(self, j: dict) -> str:
        body = json.dumps({k: j.get(k) for k in ("judgment_id", "case_id", "trial_id", "rule_id", "decision",
                                                  "criterion_version", "fact_version", "clinician", "rationale_code",
                                                  "basis_fact_ids", "at")},
                          sort_keys=True, ensure_ascii=False)
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()

    def issue(self, *, trial_id: str, rule_id: str, decision: str, rationale_code: str, clinician_id: str,
              basis_fact_ids: list[str] | None = None) -> ClinicianJudgment:
        body = {"judgment_id": "CJ-" + secrets.token_hex(8), "case_id": self.case_id, "trial_id": trial_id,
                "rule_id": rule_id, "decision": decision,
                "criterion_version": criterion_version(self.parsed[trial_id], rule_id),
                "fact_version": self._fact_version(), "clinician": CLINICIAN_PREFIX + clinician_id,
                "rationale_code": rationale_code, "basis_fact_ids": list(basis_fact_ids or []), "at": _now()}
        return ClinicianJudgment(**body, signature=self._sign(body))

    def _valid(self, j: ClinicianJudgment, fact_version: str) -> bool:
        p = self.parsed.get(j.trial_id)
        if p is None or j.case_id != self.case_id or not j.clinician.startswith(CLINICIAN_PREFIX):
            return False
        if not any(r.rule_id == j.rule_id for r in p.rules):
            return False
        return (j.criterion_version == criterion_version(p, j.rule_id) and j.fact_version == fact_version
                and hmac.compare_digest(j.signature, self._sign(j.model_dump())))

    def judgment_for(self, trial_id: str, rule) -> ClinicianJudgment | None:
        mine = [j for j in self.judgments if j.trial_id == trial_id and j.rule_id == getattr(rule, "rule_id", None)]
        if not mine:
            return None
        version = self._fact_version()
        return next((j for j in reversed(mine) if self._valid(j, version)), None)

    def status(self, j: ClinicianJudgment) -> str:
        """화면 표시용: recognized | stale(사실·기준이 바뀜) | invalid(서명·권한 불일치)."""
        p = self.parsed.get(j.trial_id)
        if p is None or not hmac.compare_digest(j.signature, self._sign(j.model_dump())):
            return "invalid"
        latest = self.judgment_for(j.trial_id, next((r for r in p.rules if r.rule_id == j.rule_id), None))
        return "recognized" if latest is not None and latest.judgment_id == j.judgment_id else "stale"


def document_authority_for(cfg: VerificationConfig, data: dict):
    if not (cfg.synthetic and data.get("synthetic")):
        return NoDocumentAuthority()
    docs = {d["document_id"]: d["sha256"] for d in data.get("documents", [])}
    return SyntheticCaseDocumentAuthority(cfg.doc_signing_key(), data["case_id"], docs)


def clinician_authority_for(cfg: VerificationConfig, data: dict, session):
    if not (cfg.synthetic and data.get("synthetic")):
        return NoClinicianAuthority()
    pid = session.case.patient_id
    return SyntheticCaseClinicianAuthority(cfg.clinician_signing_key(), data["case_id"], data.get("judgments", []),
                                           session.parsed, lambda: take_snapshot(session.fact_store, pid).version)


# ---------------------------------------------------------------------------
# 문서 저장 (합성 사례 전용)
# ---------------------------------------------------------------------------

class DocumentStore:
    """사례별 문서 파일. 파일 이름은 서버가 만든 ID만, 읽을 때마다 해시를 다시 확인."""

    def __init__(self, root: Path):
        self.root = Path(root) / "documents"

    def _path(self, case_id: str, document_id: str) -> Path:
        if not DOCUMENT_ID.match(document_id or ""):
            raise KeyError(document_id)
        return self.root / case_id / document_id

    def put(self, case_id: str, content: bytes) -> tuple[str, str]:
        doc_id = "DOC-" + secrets.token_hex(8)
        p = self._path(case_id, doc_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{secrets.token_hex(4)}.tmp")
        tmp.write_bytes(content)
        os.replace(tmp, p)
        return doc_id, hashlib.sha256(content).hexdigest()

    def get(self, case_id: str, document_id: str, sha256: str) -> bytes:
        p = self._path(case_id, document_id)
        if not p.exists():
            raise KeyError(document_id)
        content = p.read_bytes()
        if hashlib.sha256(content).hexdigest() != sha256:
            raise ValueError("document hash mismatch")
        return content


def decode_document(content_base64: str) -> bytes:
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("content_base64 is not valid base64") from None
    if not content:
        raise ValueError("empty document")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("document too large")
    return content


# ---------------------------------------------------------------------------
# 감사 기록 (추가 전용, 연결 해시)
# ---------------------------------------------------------------------------

GENESIS = "0" * 64


def _entry_hash(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AuditLog:
    def __init__(self, root: Path):
        self.root = Path(root) / "audit"
        self._lock = threading.Lock()

    def _path(self, case_id: str) -> Path:
        if not re.match(r"^FC-[0-9a-f]{32}$", case_id or ""):
            raise KeyError(case_id)
        return self.root / f"{case_id}.jsonl"

    def entries(self, case_id: str) -> list[dict]:
        p = self._path(case_id)
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _append(self, case_id: str, items: list[dict]) -> None:
        p = self._path(case_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        cur = self.entries(case_id)
        prev = cur[-1]["hash"] if cur else GENESIS
        lines = []
        for i, item in enumerate(items, len(cur)):
            entry = {"seq": i, "recorded_at": _now(), **item, "prev": prev}
            entry["hash"] = prev = _entry_hash(entry)
            lines.append(json.dumps(entry, ensure_ascii=False, sort_keys=True))
        with p.open("a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))
            fh.flush()
            os.fsync(fh.fileno())

    def sync(self, data: dict) -> int:
        """사례 기록의 events 중 아직 옮기지 않은 것을 순서대로 추가 (저장이 끝난 뒤 부름). 추가한 수."""
        cid = data["case_id"]
        with self._lock:
            done = sum(1 for e in self.entries(cid) if e.get("source") == "case_event")
            todo = data.get("events", [])[done:]
            if todo:
                self._append(cid, [{"source": "case_event", "event_index": done + i, "case_revision": data["revision"],
                                    "event": e} for i, e in enumerate(todo)])
            return len(todo)

    def denied(self, case_id: str, *, actor: str, action: str, code: str, request_id: str | None = None) -> None:
        """권한은 확인됐으나 거부된 시도 (사례 기록·개정 번호는 바꾸지 않음)."""
        with self._lock:
            self._append(case_id, [{"source": "denied", "event": {"at": _now(), "kind": "denied", "by": actor,
                                                                  "action": action, "code": code,
                                                                  "request_id": request_id}}])

    def verify(self, case_id: str) -> dict:
        entries = self.entries(case_id)
        prev = GENESIS
        for e in entries:
            if e.get("prev") != prev or _entry_hash(e) != e.get("hash"):
                return {"intact": False, "entries": len(entries), "broken_at": e.get("seq")}
            prev = e["hash"]
        return {"intact": True, "entries": len(entries), "head": prev}


__all__ = ["AuditLog", "CLINICIAN_PREFIX", "DOCUMENT_KINDS", "DOC_VERIFIER_PREFIX", "DocumentStore", "MEDIA_TYPES",
           "SyntheticCaseClinicianAuthority", "SyntheticCaseDocumentAuthority", "VerificationConfig",
           "clinician_authority_for", "decode_document", "document_authority_for", "key_matches"]
