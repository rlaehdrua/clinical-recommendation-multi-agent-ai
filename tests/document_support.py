"""테스트 전용 문서 권한 (합성). 운영 코드에는 이런 권한이 없습니다 (ctrec.document_authority).

"문서가 실제로 확인된 경우"의 대조군이 진짜 경로(증명 발급 -> 이력 -> document_backed -> gate)를 지나게 합니다.
conftest가 모든 테스트에 이 권한을 켜 둡니다. 이 권한이 발급하지 않은 증명, 상태 문자열, 문서 참조 문자열은 그대로 무효입니다.
운영 기본값(NoDocumentAuthority)은 test_clinical_record_gate.py가 따로 확인합니다.
"""

from __future__ import annotations

import hashlib

from ctrec.document_authority import ServerAttestation, fact_digest
from ctrec.fact_store import FactStore

VERIFIER = "synthetic-test-authority"
DOCUMENT_HASH = "0" * 64


class SyntheticAuthority:
    def _sign(self, document_id: str, digest: str) -> str:
        return hashlib.sha256(f"test|{document_id}|{digest}".encode()).hexdigest()

    def issue(self, fact, document_id: str = "SYN-DOC-1") -> ServerAttestation:
        digest = fact_digest(fact)
        return ServerAttestation(document_id=document_id, sha256=DOCUMENT_HASH, fact_digest=digest, verifier=VERIFIER,
                                 signature=self._sign(document_id, digest))

    def valid(self, attestation: ServerAttestation, fact) -> bool:
        return (attestation.verifier == VERIFIER and attestation.fact_digest == fact_digest(fact)
                and attestation.signature == self._sign(attestation.document_id, attestation.fact_digest))


AUTHORITY = SyntheticAuthority()


def attest(store: FactStore, fact_id: str, document_id: str = "SYN-DOC-1"):
    """합성 서버 문서로 사실 하나를 확인."""
    return store.attest(fact_id, AUTHORITY.issue(store.get(fact_id), document_id), AUTHORITY, reason="합성 문서 확인", by="test")


def attest_declared(store: FactStore, patient_id: str, document_id: str = "SYN-DOC-1") -> None:
    """환자 답변이 아닌 사실을 모두 합성 문서로 확인. 프로필의 나이·성별·ECOG는 인용이 없어 출처가 unknown으로 저장됨 (profile_facts)."""
    for f in store.facts(patient_id):
        if f.source_type != "self_report" and f.verification_status != "document_verified":
            attest(store, f.fact_id, document_id)


def documented(a, rule) -> bool:
    """match_trial 단위 테스트의 '문서 확인 있음' 대역. 게이트가 주제가 아닌 테스트(직접성·출처 분류 등)만 씁니다."""
    return True
