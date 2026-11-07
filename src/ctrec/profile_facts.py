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
- 원문의 수치가 환자의 측정값이 아니면(미측정, 결과 대기, 무효, 타인, 기관 기준) 사실로 만들지 않습니다(value_grounding.not_measured).
- 나이: 원문 표기(age_value, age_unit)가 있으면 그 값과 단위, 없으면 age(년).

출처·검증 상태
- 검증 상태는 항상 unknown으로 시작합니다(fact_store가 강제). 선언이 clinical_record여도 document_verified가 아님.
- source_quote가 있는 항목: 인용을 그대로 보존하고, 환자 원문(raw_text)에서 인용 위치를 확인합니다.
  처음 입력에서 확인되면 선언된 출처(input_source), 확인 질문 답변에서만 확인되면 self_report,
  원문을 받지 못했거나 위치를 확인하지 못하면 unknown(참조 위치를 추정하지 않음).
- source_quote가 없는 단일 값 항목(age, sex, ecog, primary_diagnosis, disease_stage): 인용을 만들지 않고 출처는 unknown.
- observed_at: 항목의 date가 YYYY, YYYY-MM, YYYY-MM-DD일 때만. 다른 표기는 해석하지 않고 observed_at_text에 보존.

질문별 출처 (qa_pairs, 선택)
- qa_pairs(확인 질문 문답 기록)를 넘기면, 답변에서 확인된 인용이 정확히 한 문답의 답에만 들어 있을 때 출처 참조를
  계약 질문 경로와 같은 형식 "q:r<라운드>:<질문ID>"로 남깁니다. 둘 이상이거나 찾지 못하면 "raw_text:answers"(추정하지 않음).
- 인용이 걸린 문답의 답이 모두 "모름"·거부·미응답이면(하나든 여럿이든) 사실로 만들지 않습니다(응답 상태이지 의료
  사실이 아님). 이 검사는 유일성 검사보다 먼저 하므로 "모름"이 여러 번 나와도 raw_text:answers 사실이 되지 않습니다.
- 그 문답이 의사 참고용(purpose="physician_reference")이면 사실로 만들지 않습니다(적격성 판정 근거가 아님, P14 D4).
  인용이 걸린 문답이 모두 의사 참고용일 때도 같습니다. 적격성 문답과 함께 걸리면 모호하므로 "raw_text:answers".
- qa_pairs를 넘기지 않으면(None) 이전과 같은 결과입니다.

중복 방지
같은 환자·세션에 같은 프로필을 다시 넣으면 같은 사실을 또 만들지 않습니다. 같은 근거인지는
(항목, 값, 단위 표기, 관찰 시점, 출처, 출처 참조) 전체로 판단하므로, 같은 값이라도 관찰 시점·출처·인용이 다르면
별개의 사실로 남고, 값이 바뀌면 새 사실이 되어 fact_store의 충돌 표시를 따릅니다.
라운드 간: 앞 라운드에 질문별 출처(q:r1:Q1)로 저장된 인용이 뒤 라운드 답에도 들어 있어 모호해지면(raw_text:answers),
그 인용이 걸린 문답의 질문별 출처로 저장된 사실을 같은 근거로 보고 새로 만들지 않습니다(_dedup_keys).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from . import evidence, value_grounding
from .fact_store import (_PATIENT_ID, FactStore, FactStoreError, PatientFact, SourceReference, _check_value,
                         _norm_field)
from .schemas import Fact, LabValue, PatientProfile, QAPair
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
    # 사실로 만들지 않은 항목 (항목, 사유). "not_measured"는 항상, "non_answer" | "physician_reference"는 qa_pairs를 넘겼을 때만
    withheld: list[tuple[str, str]] = field(default_factory=list)


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _qa_ref(pair) -> str:
    return f"q:r{pair.round}:{pair.question_id}"


def _answer_links(quote: str, qa_pairs, session_id: str | None) -> list:
    """인용이 답에 들어 있는 문답 (같은 세션만, 세션을 모르면 모두)."""
    q = _ws(quote)
    return [p for p in qa_pairs
            if (session_id is None or p.session_id in (None, session_id)) and q and q in _ws(p.answer or "")]


def _link_answer(quote: str | None, ref: SourceReference, qa_pairs, session_id: str | None):
    """답변에서 확인된 인용을 질문 단위로 연결. 반환: (새 SourceReference, 보류 사유 | None)."""
    from .evidence_directness import declared_purpose, hedged  # 지연 import: evidence_directness가 이 모듈보다 무거움
    from .question_contract import non_answer

    if qa_pairs is None or ref.ref != "raw_text:answers" or not quote:
        return ref, None
    links = _answer_links(quote, qa_pairs, session_id)
    purposes = {declared_purpose(p) for p in links}
    if links and purposes == {"physician_reference"}:
        return ref, "physician_reference"
    if links and all(non_answer(p.answer) for p in links):
        return ref, "non_answer"  # 연결된 문답이 모두 "모름"·거부·미응답 (몇 개든): 모호함보다 먼저 봄
    if links and hedged(quote):
        return ref, "uncertain_answer"  # 인용 자체가 추측·불확실 표현: 사실로 저장하지 않음
    if len(links) != 1:
        return ref, None  # 모호하거나 찾지 못함: raw_text:answers 유지
    pair = links[0]
    if declared_purpose(pair) != "eligibility":
        return ref, None  # 목적을 밝히지 않은 문답: 질문 단위로 연결하지 않음
    return SourceReference(kind="answer", ref=_qa_ref(pair), quote=ref.quote), None


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


_REF_AT = 8  # evidence_key 안에서 source_reference.ref의 위치


def _dedup_keys(key: tuple, quote: str | None, ref: SourceReference, qa_pairs, session_id: str | None) -> set:
    """중복 판정에 쓸 키들. 질문 단위로 연결되지 못한(모호한) 답변 인용은 그 인용이 걸린 문답의 질문별 출처로
    이미 저장된 사실도 같은 근거로 봄.

    Session은 라운드마다 프로필 전체를 다시 넣으므로, 1라운드에 q:r1:Q1로 저장된 짧은 인용("네" 등)이 2라운드 답에도
    들어 있으면 2라운드에서는 모호해져 raw_text:answers가 됨 -> ref만 달라 같은 사실이 또 생기는 것을 막음.
    새 사실의 출처 참조는 그대로 raw_text:answers (추정해서 질문에 붙이지 않음).
    """
    keys = {key}
    if qa_pairs is not None and quote and ref.ref == "raw_text:answers":
        keys |= {key[:_REF_AT] + (_qa_ref(p),) + key[_REF_AT + 1:] for p in _answer_links(quote, qa_pairs, session_id)}
    return keys


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
                   input_source: str = "unknown", session_id: str | None = None,
                   qa_pairs: Sequence[QAPair] | None = None) -> IngestResult:
    """PatientProfile의 사실을 store에 추가. 기존 사실은 바꾸지 않고, 같은 근거는 다시 만들지 않음.

    raw_text: 프로필을 만든 환자 원문(확인 질문 답변 포함). 없으면 인용 위치를 확인할 수 없어 출처는 unknown.
    input_source: 처음 입력의 선언된 출처 (PatientCase.input_source).
    qa_pairs: 확인 질문 문답 기록 (Session.qa_log). 넘기면 답변 인용을 질문 단위로 연결 (모듈 설명 "질문별 출처").
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
        if value_grounding.not_measured(name, value, quote, raw_text, unit=unit):
            result.withheld.append((name, "not_measured"))  # 원문의 수치가 예정·무효·타인·기관 기준이면 환자 사실이 아님
            continue
        source_type, ref = _locate(quote, raw_text, input_source)
        ref, withhold = _link_answer(quote, ref, qa_pairs, session_id)
        if withhold:
            result.withheld.append((name, withhold))
            continue
        observed_at, observed_text = _dates(date)
        probe = PatientFact(fact_id="probe", patient_id=pid, session_id=session_id, field=name, value=value,
                            unit=unit or None, source_type=source_type, source_reference=ref, observed_at=observed_at,
                            observed_at_text=observed_text, recorded_at="", version=0)
        keys = _dedup_keys(evidence_key(probe), quote, ref, qa_pairs, session_id)
        existing = next((f for f in store.facts(pid, name) if evidence_key(f) in keys), None)
        if existing is not None:
            result.duplicates.append(existing.fact_id)
            continue
        fact = store.add(pid, name, value, unit=unit, source_type=source_type, source_reference=ref,
                         observed_at=observed_at, observed_at_text=observed_text, session_id=session_id)
        result.added.append(fact.fact_id)
    return result
