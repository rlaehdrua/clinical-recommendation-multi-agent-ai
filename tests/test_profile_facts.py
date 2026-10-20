"""PatientProfile -> 환자 사실 저장소 어댑터 (Phase 1-2)."""

from __future__ import annotations

import pytest

from ctrec.fact_store import FactStore, FactStoreError
from ctrec.profile_facts import ingest_profile
from ctrec.schemas import Fact, LabValue, PatientProfile

RAW = ("62세 여성. 비소세포폐암 4기. EGFR exon 19 deletion 양성. 고혈압으로 amlodipine 복용 중.\n"
       "2026-08-01 Hb 11.2 g/dL, 혈소판 150 x10^9/L. 2025년 carboplatin/pemetrexed 4주기.")
ANSWER = "\n\n[추가 확인 정보 - 라운드 1]\nQ: 최근 혈소판 수치는?\nA: 9월에 18만이라고 들었어요"


def _profile(**kw) -> PatientProfile:
    base = dict(
        patient_id="P001", age=62, age_value=62, age_unit="years", sex="female",
        primary_diagnosis="non-small cell lung cancer", disease_stage="IV",
        diagnoses=[Fact(description="hypertension", date=None, source_quote="고혈압")],
        biomarkers=[Fact(description="EGFR exon 19 deletion positive", date=None,
                         source_quote="EGFR exon 19 deletion 양성")],
        labs=[LabValue(name="Hb", value=11.2, unit="g/dL", date="2026-08-01", source_quote="Hb 11.2 g/dL"),
              LabValue(name="platelets", value=150, unit="x10^9/L", date="2026-08-01",
                       source_quote="혈소판 150 x10^9/L")],
        medications=[Fact(description="amlodipine", date=None, source_quote="amlodipine 복용 중")],
        treatment_history=[Fact(description="carboplatin/pemetrexed 4 cycles", date="2025",
                                source_quote="2025년 carboplatin/pemetrexed 4주기")],
        ecog=1, other_facts=[], missing_or_ambiguous=["최근 뇌 MRI"],
    )
    base.update(kw)
    return PatientProfile(**base)


def _by_field(store, pid="P001"):
    out = {}
    for f in store.facts(pid):
        out.setdefault(f.field, []).append(f)
    return out


# ------------------------------------------------------------ 1. 주요 필드 변환
def test_all_major_fields_converted():
    s = FactStore()
    r = ingest_profile(s, _profile(), raw_text=RAW, input_source="clinical_record", session_id="S1")
    by = _by_field(s)
    assert set(by) == {"age", "sex", "ecog", "primary_diagnosis", "disease_stage", "diagnosis", "biomarker",
                       "medication", "treatment_history", "lab:hemoglobin", "lab:platelets"}
    assert (by["age"][0].value, by["age"][0].unit) == (62, "years") and by["sex"][0].value == "female"
    assert by["ecog"][0].value == 1 and by["disease_stage"][0].value == "IV"
    assert by["biomarker"][0].value == "EGFR exon 19 deletion positive"  # 자유 텍스트 그대로
    hb = by["lab:hemoglobin"][0]
    assert (hb.value, hb.unit, hb.observed_at) == (11.2, "g/dL", "2026-08-01")  # 검사명만 동의어 정리, 단위 그대로
    assert by["lab:platelets"][0].unit == "x10^9/L"  # 단위 환산 없음
    assert len(r.added) == 11 and r.duplicates == [] and r.rejected == []
    assert "missing_or_ambiguous" not in by and all(f.session_id == "S1" for f in s.facts("P001"))


def test_age_uses_original_notation_when_present():
    s = FactStore()
    ingest_profile(s, _profile(age=0, age_value=3, age_unit="months"), raw_text=RAW)
    f = s.facts("P001", "age")[0]
    assert (f.value, f.unit) == (3, "months")
    t = FactStore()
    ingest_profile(t, _profile(age=62, age_value=None, age_unit=None), raw_text=RAW)
    assert (t.facts("P001", "age")[0].value, t.facts("P001", "age")[0].unit) == (62, "years")


# ------------------------------------------------------------ 2-5. 출처·인용·검증 상태
def test_scalar_fields_have_no_invented_quote_and_unknown_source():
    s = FactStore()
    ingest_profile(s, _profile(), raw_text=RAW, input_source="clinical_record")
    for name in ("age", "sex", "ecog", "primary_diagnosis", "disease_stage"):
        f = s.facts("P001", name)[0]
        assert f.source_type == "unknown" and f.source_reference.quote is None
        assert f.source_reference.ref == "profile:no_quote" and f.observed_at is None


def test_source_quote_preserved_exactly_and_located():
    s = FactStore()
    ingest_profile(s, _profile(), raw_text=RAW, input_source="clinical_record")
    b = s.facts("P001", "biomarker")[0]
    assert b.source_reference.quote == "EGFR exon 19 deletion 양성"
    assert (b.source_type, b.source_reference.kind, b.source_reference.ref) == \
        ("clinical_record", "input_text", "raw_text:initial")


def test_default_source_is_unknown():
    s = FactStore()
    ingest_profile(s, _profile(), raw_text=RAW)  # input_source 선언 없음
    assert {f.source_type for f in s.facts("P001")} == {"unknown"}
    t = FactStore()
    ingest_profile(t, _profile(), input_source="clinical_record")  # 원문이 없으면 위치를 확인할 수 없음
    b = t.facts("P001", "biomarker")[0]
    assert b.source_type == "unknown" and b.source_reference.ref is None
    assert b.source_reference.quote == "EGFR exon 19 deletion 양성"


def test_quote_not_found_in_text_is_not_attributed():
    s = FactStore()
    p = _profile(biomarkers=[Fact(description="ALK negative", date=None, source_quote="ALK 음성")])
    ingest_profile(s, p, raw_text=RAW, input_source="clinical_record")
    b = s.facts("P001", "biomarker")[0]
    assert b.source_type == "unknown" and b.source_reference.kind == "other" and b.source_reference.ref is None


def test_answer_quote_is_self_report():
    s = FactStore()
    p = _profile(labs=[LabValue(name="platelets", value=180000, unit=None, date=None,
                                source_quote="9월에 18만이라고 들었어요")])
    ingest_profile(s, p, raw_text=RAW + ANSWER, input_source="clinical_record")
    f = s.facts("P001", "lab:platelets")[0]
    assert (f.source_type, f.source_reference.kind) == ("self_report", "answer") and f.unit is None


def test_clinical_record_is_not_document_verified():
    s = FactStore()
    ingest_profile(s, _profile(), raw_text=RAW, input_source="clinical_record")
    assert {f.verification_status for f in s.facts("P001")} == {"unknown"}
    assert all(f.verification_history == [] for f in s.facts("P001"))


# ------------------------------------------------------------ 6-9. 중복 방지와 구분
def test_reprocessing_same_profile_adds_nothing():
    s = FactStore()
    first = ingest_profile(s, _profile(), raw_text=RAW, input_source="clinical_record", session_id="S1")
    snapshot = [f.model_dump() for f in s.facts("P001")]
    again = ingest_profile(s, _profile(), raw_text=RAW, input_source="clinical_record", session_id="S1")
    assert again.added == [] and sorted(again.duplicates) == sorted(first.added)
    assert [f.model_dump() for f in s.facts("P001")] == snapshot  # 기존 사실을 덮어쓰지 않음


def test_same_value_different_source_kept_separately():
    s = FactStore()
    ingest_profile(s, _profile(), raw_text=RAW, input_source="clinical_record", session_id="S1")
    p = _profile(labs=[LabValue(name="platelets", value=150, unit="x10^9/L", date="2026-08-01",
                                source_quote="혈소판 150이래요")])
    ingest_profile(s, p, raw_text=RAW + "\n\n[추가 확인 정보 - 라운드 1]\nQ: 혈소판?\nA: 혈소판 150이래요",
                   input_source="clinical_record", session_id="S1")
    plt = s.facts("P001", "lab:platelets")
    assert [f.source_type for f in plt] == ["clinical_record", "self_report"]
    assert plt[1].conflict_status == "consistent"  # 같은 값의 다른 근거


def test_same_value_different_observation_time_kept_separately():
    s = FactStore()
    ingest_profile(s, _profile(), raw_text=RAW, session_id="S1")
    p = _profile(labs=[LabValue(name="Hb", value=11.2, unit="g/dL", date="2026-09-15", source_quote="Hb 11.2 g/dL")])
    ingest_profile(s, p, raw_text=RAW, session_id="S1")
    hb = s.facts("P001", "lab:hemoglobin")
    assert [f.observed_at for f in hb] == ["2026-08-01", "2026-09-15"] and hb[1].conflict_status == "consistent"


def test_changed_value_kept_as_conflict():
    s = FactStore()
    ingest_profile(s, _profile(), raw_text=RAW, session_id="S1")
    ingest_profile(s, _profile(ecog=2), raw_text=RAW, session_id="S1")
    ecog = s.facts("P001", "ecog")
    assert [f.value for f in ecog] == [1, 2] and ecog[1].conflict_status == "contradictory"
    assert ecog[1].previous_fact_id == ecog[0].fact_id


def test_list_items_are_not_conflicts():
    s = FactStore()
    p = _profile(diagnoses=[Fact(description="hypertension", date=None, source_quote="고혈압"),
                            Fact(description="type 2 diabetes", date=None, source_quote="당뇨")])
    ingest_profile(s, p, raw_text=RAW + " 당뇨.")
    dx = s.facts("P001", "diagnosis")
    assert [f.value for f in dx] == ["hypertension", "type 2 diabetes"]  # 공존 가능한 별도 사실로 보존
    assert [f.conflict_status for f in dx] == ["not_compared", "not_compared"]  # 일치도 모순도 아님
    assert s.conflicts("P001") == []


def test_ecog_conflict_still_detected_with_lists_present():
    s = FactStore()
    ingest_profile(s, _profile(), raw_text=RAW, session_id="S1")
    ingest_profile(s, _profile(ecog=2), raw_text=RAW, session_id="S1")
    [c] = s.conflicts("P001")
    assert c["field"] == "ecog" and c["relation"] == "contradictory" and c["values"] == [1, 2]


def test_other_session_is_not_deduplicated():
    s = FactStore()
    ingest_profile(s, _profile(), raw_text=RAW, session_id="S1")
    r = ingest_profile(s, _profile(), raw_text=RAW, session_id="S2")
    assert r.duplicates == [] and len(r.added) == 11


# ------------------------------------------------------------ 10. 날짜·단위·빈 값
def test_dates_units_and_empty_values():
    s = FactStore()
    p = _profile(
        age=None, age_value=None, age_unit=None, sex="unknown", ecog=None, primary_diagnosis=" ", disease_stage=None,
        labs=[LabValue(name="ANC", value=1500, unit="cells/µL", date="Aug 2026", source_quote="ANC 1500"),
              LabValue(name="creatinine", value=None, unit="mg/dL", date=None, source_quote="크레아티닌 결과 대기")],
        treatment_history=[Fact(description="surgery", date="2024-03", source_quote="수술")],
    )
    r = ingest_profile(s, p, raw_text=RAW)
    by = _by_field(s)
    assert {"age", "sex", "ecog", "primary_diagnosis", "disease_stage", "lab:creatinine"} <= set(r.skipped)
    assert not {"age", "sex", "ecog", "primary_diagnosis", "disease_stage", "lab:creatinine"} & set(by)
    anc = by["lab:absolute neutrophil count"][0]
    assert (anc.value, anc.unit, anc.observed_at, anc.observed_at_text) == (1500, "cells/µL", None, "Aug 2026")
    assert by["treatment_history"][0].observed_at == "2024-03"


def test_invalid_items_reported_not_silently_dropped():
    s = FactStore()
    r = ingest_profile(s, _profile(ecog=7, labs=[LabValue(name="β2-microglobulin", value=2.1, unit="mg/L", date=None,
                                                            source_quote="b2m")]), raw_text=RAW)
    assert {name for name, _ in r.rejected} == {"ecog", "lab:β2-microglobulin"}
    assert s.facts("P001", "ecog") == []
    with pytest.raises(ValueError):
        ingest_profile(s, _profile(), raw_text=RAW, input_source="document_verified")
    with pytest.raises(FactStoreError):
        ingest_profile(s, _profile(patient_id="../x"), raw_text=RAW)


# ------------------------------------------------------------ 11. 저장·로드
def test_roundtrip_keeps_information_and_dedup(tmp_path):
    s = FactStore(tmp_path)
    ingest_profile(s, _profile(), raw_text=RAW + ANSWER, input_source="clinical_record", session_id="S1")
    s.save()
    t = FactStore.load(tmp_path)
    assert [f.model_dump() for f in t.facts("P001")] == [f.model_dump() for f in s.facts("P001")]
    r = ingest_profile(t, _profile(), raw_text=RAW + ANSWER, input_source="clinical_record", session_id="S1")
    assert r.added == []  # 다시 로드한 뒤에도 같은 근거를 다시 만들지 않음
