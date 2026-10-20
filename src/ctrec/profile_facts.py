"""PatientProfile -> 환자 사실 저장소(fact_store) 어댑터 (Phase 1-2).

환자 정보 이해 에이전트가 만든 PatientProfile을 사실 단위로 옮깁니다. Session·Matcher·patient_profiler는 바꾸지 않습니다.

항목 이름 (field) 정규화 정책
- 단일 값: age, sex, ecog, primary_diagnosis, disease_stage
- 여러 값(목록 항목마다 사실 하나): diagnosis, biomarker, medication, treatment_history, other_fact
  자유 텍스트(description)는 그대로 값으로 쓰고 의미를 묶거나 나누지 않습니다(예: 바이오마커 이름을 뽑아내지 않음).
- 검사: "lab:<표준 검사명>". 검사명은 규칙 엔진의 동의어 표(rule_engine.canonical_lab: hb -> hemoglobin 등)로만
  맞추고, 값·단위는 그대로 둡니다(단위 환산 없음).
- 누락은 사실로 만들지 않습니다: None, 빈 문자열, sex="unknown", 값이 없는 검사. missing_or_ambiguous는 '없음'의
  기록이라 사실로 옮기지 않습니다.
- 나이: 원문 표기(age_value, age_unit)가 있으면 그 값과 단위, 없으면 age(년).

출처·검증 상태
- 검증 상태는 항상 unknown으로 시작합니다(fact_store가 강제). 선언이 clinical_record여도 document_verified가 아님.
- source_quote가 있는 항목: 인용을 그대로 보존하고, 환자 원문(raw_text)에서 인용 위치를 확인합니다.
  처음 입력에서 확인되면 선언된 출처(input_source), 확인 질문 답변에서만 확인되면 self_report,
  원문을 받지 못했거나 위치를 확인하지 못하면 unknown(참조 위치를 추정하지 않음).
- source_quote가 없는 단일 값 항목(age, sex, ecog, primary_diagnosis, disease_stage): 인용을 만들지 않고 출처는 unknown.
- observed_at: 항목의 date가 YYYY, YYYY-MM, YYYY-MM-DD일 때만. 다른 표기는 해석하지 않고 observed_at_text에 보존.

중복 방지
같은 환자·세션에 같은 프로필을 다시 넣으면 같은 사실을 또 만들지 않습니다. 같은 근거인지는
(항목, 값, 단위 표기, 관찰 시점, 출처, 출처 참조) 전체로 판단하므로, 같은 값이라도 관찰 시점·출처·인용이 다르면
별개의 사실로 남고, 값이 바뀌면 새 사실이 되어 fact_store의 충돌 표시를 따릅니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import evidence
from .fact_store import (_PATIENT_ID, FactStore, FactStoreError, PatientFact, SourceReference, _check_value,
                         _norm_field)
from .schemas import Fact, LabValue, PatientProfile
from .tools.rule_engine import _norm_unit, canonical_lab

_ISO_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
LIST_FIELDS = {"diagnoses": "diagnosis", "biomarkers": "biomarker", "medications": "medication",
               "treatment_history": "treatment_history", "other_facts": "other_fact"}


@dataclass
class IngestResult:
    added: list[str] = field(default_factory=list)  # 새로 만든 fact_id
    duplicates: list[str] = field(default_factory=list)  # 이미 있어 만들지 않은 사실의 fact_id
    skipped: list[str] = field(default_factory=list)  # 값이 없어 옮기지 않은 항목
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (항목, 사유): 이름·값이 저장소 규칙에 맞지 않아 옮기지 못함


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _locate(quote: str | None, raw_text: str | None, input_source: str) -> tuple[str, SourceReference]:
    """인용의 위치로 출처를 정함. 반환: (source_type, source_reference). 인용은 그대로 보존."""
    if not quote or not quote.strip():
        return "unknown", SourceReference(kind="other", ref="profile:no_quote")
    if raw_text is None:
        return "unknown", SourceReference(kind="other", ref=None, quote=quote)
    initial, answers = evidence.split_source(raw_text)
    q = _ws(quote)
    if q and q in _ws(initial):
        return input_source, SourceReference(kind="input_text", ref="raw_text:initial", quote=quote)
    if q and answers and q in _ws(answers):
        return "self_report", SourceReference(kind="answer", ref="raw_text:answers", quote=quote)
    return "unknown", SourceReference(kind="other", ref=None, quote=quote)  # 위치 미확인: 참조를 추정하지 않음


def _dates(date: str | None) -> tuple[str | None, str | None]:
    """(observed_at, observed_at_text)."""
    if not date or not date.strip():
        return None, None
    d = date.strip()
    return (d, None) if _ISO_DATE.match(d) else (None, d)


def evidence_key(f: PatientFact) -> tuple:
    """같은 근거인지 판단하는 키. 값은 대소문자·공백만, 단위는 표기만 맞춤(환산 없음)."""
    value = _ws(f.value).casefold() if isinstance(f.value, str) else (type(f.value).__name__ == "bool", float(f.value))
    ref = f.source_reference
    return (f.session_id, f.field, value, _norm_unit(f.unit) if f.unit else None, f.observed_at, f.observed_at_text,
            f.source_type, ref.kind, ref.ref, _ws(ref.quote) if ref.quote else None)


def _items(profile: PatientProfile) -> list[tuple[str, object, str | None, str | None, str | None]]:
    """(field, value, unit, date, quote). 값이 없는 항목은 value=None."""
    out: list[tuple] = []
    if profile.age_value is not None and profile.age_unit:
        out.append(("age", profile.age_value, profile.age_unit, None, None))
    else:
        out.append(("age", profile.age, "years" if profile.age is not None else None, None, None))
    out.append(("sex", None if profile.sex == "unknown" else profile.sex, None, None, None))
    out.append(("ecog", profile.ecog, None, None, None))
    out.append(("primary_diagnosis", profile.primary_diagnosis, None, None, None))
    out.append(("disease_stage", profile.disease_stage, None, None, None))
    for attr, name in LIST_FIELDS.items():
        item: Fact
        for item in getattr(profile, attr):
            out.append((name, item.description, None, item.date, item.source_quote))
    lab: LabValue
    for lab in profile.labs:
        out.append((f"lab:{canonical_lab(lab.name)}", lab.value, lab.unit, lab.date, lab.source_quote))
    return out


def ingest_profile(store: FactStore, profile: PatientProfile, *, raw_text: str | None = None,
                   input_source: str = "unknown", session_id: str | None = None) -> IngestResult:
    """PatientProfile의 사실을 store에 추가. 기존 사실은 바꾸지 않고, 같은 근거는 다시 만들지 않음.

    raw_text: 프로필을 만든 환자 원문(확인 질문 답변 포함). 없으면 인용 위치를 확인할 수 없어 출처는 unknown.
    input_source: 처음 입력의 선언된 출처 (PatientCase.input_source).
    """
    if input_source not in evidence.INPUT_SOURCES:
        raise ValueError(f"input_source는 {', '.join(evidence.INPUT_SOURCES)} 중 하나: {input_source!r}")
    pid = profile.patient_id
    if not _PATIENT_ID.match(pid or ""):
        raise FactStoreError(f"잘못된 patient_id: {pid!r}")
    result = IngestResult()
    for name, value, unit, date, quote in _items(profile):
        if value is None or (isinstance(value, str) and not value.strip()):
            result.skipped.append(name)
            continue
        if name == "age" and isinstance(value, float) and value.is_integer():
            value = int(value)
        try:  # 저장소 규칙(항목 이름, 값 범위)을 먼저 확인해 중간에 실패하지 않게 함
            name = _norm_field(name)
            _check_value(name, value)
        except FactStoreError as e:
            result.rejected.append((name, str(e)))
            continue
        source_type, ref = _locate(quote, raw_text, input_source)
        observed_at, observed_text = _dates(date)
        probe = PatientFact(fact_id="probe", patient_id=pid, session_id=session_id, field=name, value=value,
                            unit=unit or None, source_type=source_type, source_reference=ref, observed_at=observed_at,
                            observed_at_text=observed_text, recorded_at="", version=0)
        key = evidence_key(probe)
        existing = next((f for f in store.facts(pid, name) if evidence_key(f) == key), None)
        if existing is not None:
            result.duplicates.append(existing.fact_id)
            continue
        fact = store.add(pid, name, value, unit=unit, source_type=source_type, source_reference=ref,
                         observed_at=observed_at, observed_at_text=observed_text, session_id=session_id)
        result.added.append(fact.fact_id)
    return result
