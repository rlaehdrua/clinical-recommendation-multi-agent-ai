"""환자 사실 저장소(Patient Fact Store) - 아키텍처 §16–19 기초 (Phase 1-1).

환자 정보를 개별 사실(PatientFact) 단위로 저장하고 출처·검증 상태·변경 이력·충돌을 추적합니다.
로컬 JSON 파일(환자당 1개)에 저장합니다. 아직 Matcher·Session과 연결하지 않은 독립 모듈입니다.

이 저장소는 시스템이 받은 정보를 출처와 함께 정리한 기록이며, 임상적으로 검증된 의무기록이 아닙니다.

원칙
- 출처(source_type)와 검증 상태(verification_status)는 별개입니다. 출처가 clinical_record라고 적혀 있어도
  검증 상태는 그대로 unknown이며, document_verified는 문서 참조와 사유를 함께 기록할 때만 부여합니다.
- 사실의 값은 고치지 않습니다. 같은 항목의 새 정보는 새 사실로 추가하고, 이전 사실은 그대로 남깁니다(version, previous_fact_id).
  바뀌는 것은 검증 상태(이력과 함께 기록)와 충돌 표시(같은 항목의 사실들로부터 다시 계산)뿐입니다.
- 같은 항목의 값이 다르면 둘 다 남기고 관계를 표시합니다. 최신 값이나 검증된 값을 자동으로 채택하지 않습니다.
  - consistent      : 같은 값 (단위가 같고 수치가 같거나, 문자열이 같음)
  - contradictory   : 다른 값인데 관찰 시점이 같거나 모름, 또는 시간에 따라 바뀌지 않는 항목(성별 등)
  - time_dependent  : 관찰 시점이 서로 달라 실제 변화일 수 있음 -> 추가 확인 필요
  - incomparable    : 단위가 달라 값을 비교할 수 없음 -> 추가 확인 필요 (단위를 추측해 환산하지 않음)
  - not_compared    : 여러 값을 가질 수 있는 항목(진단·바이오마커·투약 목록 등, MULTI_VALUED_FIELDS)에서 값 문자열이 다름.
                      서로 다른 항목(고혈압·당뇨)일 수도, 같은 대상의 상반된 결과(EGFR 양성·음성)일 수도 있으나
                      의미를 해석하지 않으므로 일치도 모순도 확정하지 않고 '비교하지 않음'으로 표시합니다(충돌 목록에는 넣지 않음).
                      값 문자열이 같을 때만 consistent입니다.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Union

from pydantic import BaseModel, Field

FORMAT_VERSION = 1

SourceType = Literal["clinical_record", "self_report", "unknown"]
VerificationStatus = Literal["unknown", "unverified", "document_verified", "unverifiable"]
Relation = Literal["consistent", "not_compared", "contradictory", "time_dependent", "incomparable"]
ConflictStatus = Literal["none", "consistent", "not_compared", "contradictory", "time_dependent", "incomparable"]
Scalar = Union[bool, int, float, str]

# 충돌 표시의 우선순위 (사실 하나가 여러 사실과 관계가 있으면 가장 심각한 것)
_SEVERITY = {"none": 0, "consistent": 1, "not_compared": 2, "incomparable": 3, "time_dependent": 4, "contradictory": 5}
# 관찰 시점이 달라도 값이 바뀔 수 없는 항목
TIME_INVARIANT_FIELDS = {"sex", "birth_date"}
# 한 환자에게 여러 값이 동시에 있을 수 있는 항목 (값이 다르면 not_compared: 일치·모순을 확정하지 않음)
MULTI_VALUED_FIELDS = {"diagnosis", "biomarker", "medication", "treatment_history", "other_fact"}

_FIELD = re.compile(r"^[a-z0-9][a-z0-9_.:\-() /]*$")
_PATIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


class FactStoreError(ValueError):
    """잘못된 입력이나 존재하지 않는 사실."""


class SourceReference(BaseModel):
    kind: Literal["input_text", "answer", "document", "other"] = "other"
    ref: str | None = None  # 예: "qa:round1:Q2", 문서 식별자
    quote: str | None = None  # 원문 인용


class VerificationEvent(BaseModel):
    from_status: VerificationStatus
    to_status: VerificationStatus
    reason: str
    by: str
    document_reference: str | None = None
    at: str


class PatientFact(BaseModel):
    fact_id: str
    patient_id: str
    session_id: str | None = None
    field: str
    value: Scalar
    unit: str | None = None
    source_type: SourceType = "unknown"
    source_reference: SourceReference = Field(default_factory=SourceReference)
    verification_status: VerificationStatus = "unknown"
    verification_history: list[VerificationEvent] = []
    observed_at: str | None = None  # 실제 측정·관찰 시점 (YYYY, YYYY-MM, YYYY-MM-DD)
    observed_at_text: str | None = None  # 해석하지 못한 원래 날짜 표기 (observed_at으로 바꾸지 않고 보존)
    recorded_at: str  # 시스템에 기록한 시점
    version: int  # 같은 환자·항목 안에서의 순번 (1, 2, ...)
    previous_fact_id: str | None = None  # 같은 항목의 직전 사실
    conflict_status: ConflictStatus = "none"  # 같은 항목의 다른 사실과의 관계 중 가장 심각한 것 (다시 계산됨)
    related_fact_ids: dict[str, Relation] = {}  # fact_id -> 관계


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _norm_field(field: str) -> str:
    f = re.sub(r"\s+", " ", (field or "").strip().lower())
    if not f or not _FIELD.match(f):
        raise FactStoreError(f"잘못된 항목 이름: {field!r}")
    return f


def _check_value(field: str, value) -> Scalar:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise FactStoreError(f"{field}: 값이 비어 있음")
    if not isinstance(value, (bool, int, float, str)):
        raise FactStoreError(f"{field}: 값은 문자열·숫자·참거짓만 가능 ({type(value).__name__})")
    if isinstance(value, float) and not math.isfinite(value):
        raise FactStoreError(f"{field}: 유한한 숫자가 아님")
    if field == "ecog" and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5):
        raise FactStoreError(f"ecog는 0-5 정수: {value!r}")
    if field == "age" and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
        raise FactStoreError(f"age는 0 이상의 숫자: {value!r}")
    if field == "sex" and value not in ("male", "female", "other"):
        raise FactStoreError(f"sex는 male/female/other: {value!r}")
    return value


def _same_unit(a: str | None, b: str | None) -> bool:
    from .tools.rule_engine import _norm_unit  # 표기만 다른 같은 단위 (환산은 하지 않음)
    return (a is None and b is None) or (a is not None and b is not None and _norm_unit(a) == _norm_unit(b))


def _same_value(a: Scalar, b: Scalar) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    return str(a).strip().casefold() == str(b).strip().casefold()


def relation(a: PatientFact, b: PatientFact) -> Relation:
    """같은 항목의 두 사실의 관계. 어느 쪽도 채택하지 않음."""
    if a.field != b.field:
        raise FactStoreError("다른 항목의 사실은 비교하지 않음")
    if a.field in MULTI_VALUED_FIELDS:
        return "consistent" if _same_unit(a.unit, b.unit) and _same_value(a.value, b.value) else "not_compared"
    if not _same_unit(a.unit, b.unit):
        return "incomparable"
    if _same_value(a.value, b.value):
        return "consistent"
    if a.field in TIME_INVARIANT_FIELDS:
        return "contradictory"
    if a.observed_at and b.observed_at and a.observed_at != b.observed_at:
        return "time_dependent"
    return "contradictory"


class FactStore:
    """환자별 사실 목록. root를 주면 save()/load()로 <root>/<patient_id>.json에 저장합니다."""

    def __init__(self, root: Path | None = None):
        self.root = root
        self._facts: dict[str, list[PatientFact]] = {}

    # ------------------------------------------------------------ 추가·조회
    def add(self, patient_id: str, field: str, value, *, unit: str | None = None, source_type: str = "unknown",
            source_reference: SourceReference | dict | None = None, observed_at: str | None = None,
            observed_at_text: str | None = None, session_id: str | None = None) -> PatientFact:
        """새 사실 추가. 검증 상태는 항상 unknown으로 시작 (검증은 set_verification으로 사유와 함께)."""
        if not isinstance(patient_id, str) or not _PATIENT_ID.match(patient_id):
            raise FactStoreError(f"잘못된 patient_id: {patient_id!r}")
        field = _norm_field(field)
        value = _check_value(field, value)
        if source_type not in ("clinical_record", "self_report", "unknown"):
            raise FactStoreError(f"잘못된 source_type: {source_type!r}")
        if observed_at is not None and not _DATE.match(observed_at):
            raise FactStoreError(f"observed_at은 YYYY, YYYY-MM, YYYY-MM-DD: {observed_at!r}")
        ref = SourceReference.model_validate(source_reference or {})
        same = self.facts(patient_id, field)
        fact = PatientFact(
            fact_id=f"F-{uuid.uuid4().hex[:12]}", patient_id=patient_id, session_id=session_id, field=field,
            value=value, unit=(unit or None), source_type=source_type, source_reference=ref,
            observed_at=observed_at, observed_at_text=(observed_at_text or None), recorded_at=_now(),
            version=len(same) + 1,
            previous_fact_id=same[-1].fact_id if same else None,
        )
        self._facts.setdefault(patient_id, []).append(fact)
        self._recompute(patient_id, field)
        return fact

    def get(self, fact_id: str) -> PatientFact:
        for facts in self._facts.values():
            for f in facts:
                if f.fact_id == fact_id:
                    return f
        raise FactStoreError(f"사실 {fact_id!r}가 없음")

    def facts(self, patient_id: str, field: str | None = None) -> list[PatientFact]:
        """환자의 사실 목록 (기록 순서). field를 주면 그 항목만."""
        facts = self._facts.get(patient_id, [])
        if field is None:
            return list(facts)
        f = _norm_field(field)
        return [x for x in facts if x.field == f]

    def patients(self) -> list[str]:
        return sorted(self._facts)

    def provenance(self, fact_id: str) -> dict:
        f = self.get(fact_id)
        return {"source_type": f.source_type, "source_reference": f.source_reference.model_dump(),
                "verification_status": f.verification_status,
                "verification_history": [e.model_dump() for e in f.verification_history]}

    # ------------------------------------------------------------ 검증 상태
    def set_verification(self, fact_id: str, status: str, *, reason: str, by: str,
                         document_reference: str | None = None) -> PatientFact:
        """검증 상태 변경. 사유·기록자를 남기고, document_verified는 문서 참조가 있어야 함."""
        f = self.get(fact_id)
        if status not in ("unknown", "unverified", "document_verified", "unverifiable"):
            raise FactStoreError(f"잘못된 검증 상태: {status!r}")
        if not (reason or "").strip() or not (by or "").strip():
            raise FactStoreError("검증 상태를 바꾸려면 사유(reason)와 기록자(by)가 필요함")
        if status == "document_verified" and not (document_reference or "").strip():
            raise FactStoreError("document_verified는 확인한 문서 참조(document_reference)가 있어야 함")
        if status == f.verification_status:
            raise FactStoreError(f"이미 {status} 상태")
        f.verification_history.append(VerificationEvent(
            from_status=f.verification_status, to_status=status, reason=reason.strip(), by=by.strip(),
            document_reference=(document_reference or None), at=_now()))
        f.verification_status = status  # type: ignore[assignment]
        return f

    # ------------------------------------------------------------ 충돌
    def _recompute(self, patient_id: str, field: str) -> None:
        group = self.facts(patient_id, field)
        for f in group:
            f.related_fact_ids = {o.fact_id: relation(f, o) for o in group if o is not f}
            f.conflict_status = max((r for r in f.related_fact_ids.values()), key=_SEVERITY.__getitem__,
                                    default="none")  # type: ignore[assignment]

    def conflicts(self, patient_id: str) -> list[dict]:
        """같은 항목에서 값이 일치하지 않는 사실 쌍 (consistent, not_compared 제외). 어느 값도 채택하지 않음."""
        out = []
        for field in sorted({f.field for f in self.facts(patient_id)}):
            group = self.facts(patient_id, field)
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    r = relation(a, b)
                    if r not in ("consistent", "not_compared"):
                        out.append({"field": field, "relation": r, "fact_ids": [a.fact_id, b.fact_id],
                                    "values": [a.value, b.value], "observed_at": [a.observed_at, b.observed_at]})
        return out

    # ------------------------------------------------------------ 저장·로드
    def _path(self, patient_id: str) -> Path:
        if self.root is None:
            raise FactStoreError("저장 위치(root)가 없음")
        return self.root / f"{patient_id}.json"

    def save(self) -> None:
        """환자마다 파일 하나. 임시 파일에 쓴 뒤 바꿔 끼움."""
        if self.root is None:
            raise FactStoreError("저장 위치(root)가 없음")
        self.root.mkdir(parents=True, exist_ok=True)
        for pid, facts in self._facts.items():
            data = {"format_version": FORMAT_VERSION, "patient_id": pid,
                    "facts": [f.model_dump() for f in facts]}
            tmp = self._path(pid).with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path(pid))

    @classmethod
    def load(cls, root: Path) -> "FactStore":
        store = cls(root)
        for path in sorted(root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("format_version") != FORMAT_VERSION:
                raise FactStoreError(f"{path.name}: 지원하지 않는 형식 {data.get('format_version')!r}")
            pid = data.get("patient_id")
            if pid != path.stem:
                raise FactStoreError(f"{path.name}: 파일 이름과 patient_id가 다름 ({pid!r})")
            facts = [PatientFact.model_validate(f) for f in data.get("facts", [])]
            if any(f.patient_id != pid for f in facts):
                raise FactStoreError(f"{path.name}: 다른 환자의 사실이 섞여 있음")
            ids = [f.fact_id for f in facts]
            if len(ids) != len(set(ids)):
                raise FactStoreError(f"{path.name}: fact_id 중복")
            store._facts[pid] = facts
            for field in {f.field for f in facts}:
                store._recompute(pid, field)  # 충돌 표시는 저장값이 아니라 사실들로부터 다시 계산
        return store
