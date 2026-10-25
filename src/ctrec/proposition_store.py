"""계약 답변 -> 환자 사실 저장소(fact_store)의 명제 사실 (재설계 P3).

명제 사실은 기존 FactStore에 항목 이름 "prop:<명제 키>"로 기록합니다(값 = 참/거짓, 출처 = self_report,
관찰 시점 = 답변 시점의 평가 기준일, source_reference.ref에 계약·질문 출처). FactStore의 원칙을 그대로 따릅니다:
값을 고치지 않고 새 사실로 추가하며, 같은 항목의 다른 값은 둘 다 남기고 관계(contradictory/time_dependent)만 표시합니다.

이 장부(PropositionLedger)는 그 위에서
- 이 장부가 직접 기록한 사실(issued)만 근거로 내놓습니다. 같은 저장소에 계약 출처처럼 보이는 항목이 따로 들어와도
  (프로필 병행 기록, 외부 입력, 위조된 ref) 쓰지 않습니다. 자유 서술은 근거가 아닙니다.
- 세션을 격리합니다(다른 세션의 답변은 쓰지 않음).
- 정정은 명시적으로만 합니다(correct: 이전 사실 ID와 사유를 받아 대체 관계를 기록). 최신 답을 자동 채택하지 않습니다.
- 자유 서술 답은 사실 후보(candidates)로만 보관합니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .fact_store import FactStore, PatientFact, SourceReference
from .propositions import Proposition
from .question_contract import MappedAnswer, QuestionContract

PREFIX = "prop:"
_REF = re.compile(r"^contract:(QC-[A-Z0-9\-]+)@v(\d+)#([0-9a-f]{12});q:r(\d+):([A-Za-z0-9_\-]+)$")


@dataclass
class PropFact:
    fact_id: str
    proposition: Proposition
    value: bool
    as_of: str  # 답변 시점의 평가 기준일 (YYYY-MM-DD)
    session_id: str | None
    contract_id: str
    contract_version: int
    contract_digest12: str
    round: int
    question_id: str
    answer_label: str
    source: str  # self_report (환자 답변) | clinical_record (문서 확인으로 검증된 경우만)
    verification_status: str
    recorded_at: str


@dataclass
class PropositionLedger:
    store: FactStore
    superseded: dict[str, dict] = field(default_factory=dict)  # 이전 fact_id -> {"by": [...], "reason": ...}
    candidates: list[dict] = field(default_factory=list)  # 자유 서술 답 (사실 아님)
    rejected: list[dict] = field(default_factory=list)  # 계약 검증 실패 (보여 준 문장이 계약과 다름 등)
    issued: set[str] = field(default_factory=set)  # 이 장부가 기록한 fact_id (메모리 안에서만, 다른 경로의 사실은 신뢰하지 않음)

    def record(self, patient_id: str, session_id: str | None, contract: QuestionContract, mapped: MappedAnswer, *,
               round_no: int, question_id: str, as_of: str, supersedes: list[str] | None = None,
               reason: str = "") -> list[PatientFact]:
        """계약 답변 하나를 명제 사실로 기록. 선택지 답만 사실이 되고, 나머지는 후보·거부 기록."""
        meta = {"contract_id": contract.contract_id, "round": round_no, "question_id": question_id,
                "session_id": session_id}
        if mapped.kind == "rejected":
            self.rejected.append({**meta, "reason": mapped.reason})
            return []
        if mapped.kind == "free_text":
            self.candidates.append({**meta, "reason": mapped.reason})
            return []
        if mapped.kind != "option":
            return []
        if supersedes:  # 기록 전에 검증 (실패하면 아무것도 기록하지 않음)
            if not reason.strip():
                raise ValueError("정정에는 사유가 필요")
            own = {f.fact_id for f in self.facts(patient_id, session_id)}
            missing = [f for f in supersedes if f not in own]
            if missing:
                raise ValueError(f"이 세션의 유효한 계약 사실이 아님: {missing}")
        ref =(f"contract:{contract.contract_id}@v{contract.version}#{contract.digest()[:12]};"
               f"q:r{round_no}:{question_id}")
        out = [self.store.add(patient_id, PREFIX + p.key, v, source_type="self_report",
                              source_reference=SourceReference(kind="answer", ref=ref, quote=mapped.label),
                              observed_at=as_of, session_id=session_id)
               for p, v in mapped.assertions]
        self.issued.update(f.fact_id for f in out)
        if supersedes:
            for old in supersedes:
                self.superseded[old] = {"by": [f.fact_id for f in out], "reason": reason.strip()}
        return out

    def correct(self, patient_id: str, session_id: str | None, contract: QuestionContract, mapped: MappedAnswer, *,
                round_no: int, question_id: str, as_of: str, previous: list[str], reason: str) -> list[PatientFact]:
        """환자가 이전 답을 고친다고 명시한 경우만: 이전 사실을 대체 표시하고 새 사실 기록."""
        return self.record(patient_id, session_id, contract, mapped, round_no=round_no, question_id=question_id,
                           as_of=as_of, supersedes=previous, reason=reason)

    def facts(self, patient_id: str, session_id: str | None, *, include_superseded: bool = False) -> list[PropFact]:
        """이 세션의 계약 출처 명제 사실. 계약 출처 형식이 아니거나 다른 세션이면 제외."""
        out = []
        for f in self.store.facts(patient_id):
            if f.fact_id not in self.issued or not f.field.startswith(PREFIX) or f.session_id != session_id                     or not isinstance(f.value, bool):
                continue
            m = _REF.match(f.source_reference.ref or "")
            if f.source_reference.kind != "answer" or not m or not f.observed_at:
                continue
            if not include_superseded and f.fact_id in self.superseded:
                continue
            out.append(PropFact(f.fact_id, Proposition.parse(f.field[len(PREFIX):]), f.value, f.observed_at,
                                f.session_id, m.group(1), int(m.group(2)), m.group(3), int(m.group(4)), m.group(5),
                                f.source_reference.quote or "",
                                "clinical_record" if f.verification_status == "document_verified" else f.source_type,
                                f.verification_status, f.recorded_at))
        return out
