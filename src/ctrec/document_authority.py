"""문서 검증을 판단하는 서버 측 지점 (입력·상태 문자열이 아니라 여기서만 인정).

서버가 문서를 저장하고 확인하는 운영 계약은 아직 없습니다. 그래서 기본 권한은 NoDocumentAuthority이고 어떤 증명도 인정하지
않습니다: document_verified 상태 문자열, 문서 참조 문자열, 직접 만든 증명 모두 검증이 아니며 사실은 미검증으로 남습니다.
운영 어댑터를 추가하는 일은 계약 결정이고, 그 전에는 운영 경로에서 use()를 부르지 않습니다.

증명은 사실 값에 묶입니다(fact_digest): 다른 값의 사실로 옮기면 어떤 권한에서도 무효여야 합니다.
권한은 컨텍스트 값이라 use() 밖으로 새지 않습니다 (llm.use_client와 같은 방식).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
from typing import Protocol

from pydantic import BaseModel, Field


class ServerAttestation(BaseModel):
    """서버가 저장한 문서로 이 값을 확인했다는 증명. 형식이 맞아도 현재 권한이 인정해야 효력이 있음."""
    document_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # 확인한 문서의 해시
    fact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")  # 확인한 값 (fact_digest)
    verifier: str = Field(min_length=1)
    signature: str = Field(min_length=1)


class DocumentAuthority(Protocol):
    def valid(self, attestation: ServerAttestation, fact) -> bool:
        """이 증명이 이 사실의 값에 대해 서버가 발급한 것인가."""
        ...


class NoDocumentAuthority:
    """운영 기본값: 서버가 확인한 문서가 없음. 어떤 증명도 유효하지 않음."""

    def valid(self, attestation: ServerAttestation, fact) -> bool:
        return False


_authority: contextvars.ContextVar[DocumentAuthority] = contextvars.ContextVar("document_authority",
                                                                                default=NoDocumentAuthority())


def current() -> DocumentAuthority:
    return _authority.get()


@contextlib.contextmanager
def use(authority: DocumentAuthority):
    """이 컨텍스트에서만 authority로 판단. 끝나면(예외 포함) 이전 값으로 복구, 중첩 가능."""
    token = _authority.set(authority)
    try:
        yield authority
    finally:
        _authority.reset(token)


def fact_digest(fact) -> str:
    """증명이 묶이는 값: 환자·항목·값·단위·관찰 시점. fact_id·순번은 스냅샷마다 달라질 수 있어 넣지 않음."""
    body = json.dumps([fact.patient_id, fact.field, fact.value, fact.unit, fact.observed_at], ensure_ascii=False,
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


__all__ = ["DocumentAuthority", "NoDocumentAuthority", "ServerAttestation", "current", "fact_digest", "use"]
