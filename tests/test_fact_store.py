"""환자 사실 저장소 (Phase 1-1): 사실 단위 저장, 출처·검증 상태 분리, 이력 보존, 충돌 표시, JSON 저장·로드."""

from __future__ import annotations

import json

import pytest

from ctrec.fact_store import FactStore, FactStoreError, relation

P = "P001"


def _ecog(store, value, **kw):
    return store.add(P, "ecog", value, **{"source_type": "self_report", **kw})


# ------------------------------------------------------------ 추가·조회
def test_add_and_get():
    s = FactStore()
    f = s.add(P, "ECOG", 1, source_type="self_report",
              source_reference={"kind": "answer", "ref": "qa:round1:Q1", "quote": "ECOG 1이래요"}, session_id="S1")
    assert f.fact_id.startswith("F-") and f.field == "ecog" and f.version == 1 and f.previous_fact_id is None
    assert s.get(f.fact_id) is f and s.facts(P) == [f] and s.facts(P, "ecog") == [f]
    assert f.recorded_at and f.observed_at is None and f.session_id == "S1"
    with pytest.raises(FactStoreError):
        s.get("F-missing")


def test_same_field_keeps_all_facts_and_history():
    s = FactStore()
    a = _ecog(s, 1)
    b = _ecog(s, 2)
    assert s.facts(P, "ecog") == [a, b]  # 이전 사실을 지우거나 덮어쓰지 않음
    assert (a.value, b.value) == (1, 2)
    assert b.version == 2 and b.previous_fact_id == a.fact_id


# ------------------------------------------------------------ 충돌
def test_contradiction_marked_without_choosing_a_value():
    s = FactStore()
    a, b = _ecog(s, 1), _ecog(s, 2)
    assert a.conflict_status == b.conflict_status == "contradictory"
    assert a.related_fact_ids == {b.fact_id: "contradictory"}
    [c] = s.conflicts(P)
    assert c["relation"] == "contradictory" and c["values"] == [1, 2]
    assert not hasattr(s, "current") and not hasattr(s, "resolve")  # 자동 채택 기능 없음


def test_same_value_is_consistent():
    s = FactStore()
    a = s.add(P, "lab:platelets", 150, unit="10^9/L", source_type="clinical_record")
    b = s.add(P, "lab:platelets", 150.0, unit="x10^9/L", source_type="self_report")
    assert a.conflict_status == "consistent" and s.conflicts(P) == []
    c = s.add(P, "diagnosis", "NSCLC")
    d = s.add(P, "diagnosis", " nsclc ")
    assert relation(c, d) == "consistent"


def test_different_observation_times_need_review_not_error():
    s = FactStore()
    a = _ecog(s, 1, observed_at="2026-06-01")
    b = _ecog(s, 2, observed_at="2026-09-15")
    assert a.conflict_status == b.conflict_status == "time_dependent"
    assert s.conflicts(P)[0]["observed_at"] == ["2026-06-01", "2026-09-15"]
    c = _ecog(s, 3)  # 시점을 모르는 다른 값은 모순
    assert relation(a, c) == "contradictory" and c.conflict_status == "contradictory"
    assert a.conflict_status == "contradictory"  # 가장 심각한 관계로 다시 계산


def test_time_invariant_field_contradicts_even_at_different_times():
    s = FactStore()
    s.add(P, "sex", "female", observed_at="2025-01-01")
    b = s.add(P, "sex", "male", observed_at="2026-01-01")
    assert b.conflict_status == "contradictory"


def test_different_units_are_incomparable_not_converted():
    s = FactStore()
    s.add(P, "lab:hemoglobin", 10, unit="g/dL")
    b = s.add(P, "lab:hemoglobin", 100, unit="g/L")
    assert b.conflict_status == "incomparable"


# ------------------------------------------------------------ 출처·검증 상태
def test_source_and_verification_are_separate():
    s = FactStore()
    f = s.add(P, "biomarker:egfr", "exon 19 deletion", source_type="clinical_record",
              source_reference={"kind": "input_text", "quote": "EGFR exon 19 deletion"})
    assert f.source_type == "clinical_record" and f.verification_status == "unknown"  # 출처 표기로 승격하지 않음
    g = s.add(P, "ecog", 1)
    assert g.source_type == "unknown" and g.verification_status == "unknown"  # 기본값
    prov = s.provenance(f.fact_id)
    assert prov["source_reference"]["quote"] == "EGFR exon 19 deletion" and prov["verification_history"] == []


def test_add_cannot_set_verification_status():
    with pytest.raises(TypeError):
        FactStore().add(P, "ecog", 1, verification_status="document_verified")


def test_document_verified_requires_document_and_reason():
    s = FactStore()
    f = _ecog(s, 1)
    with pytest.raises(FactStoreError, match="문서 참조"):
        s.set_verification(f.fact_id, "document_verified", reason="확인", by="연구간호사")
    with pytest.raises(FactStoreError, match="사유"):
        s.set_verification(f.fact_id, "unverified", reason=" ", by="연구간호사")
    with pytest.raises(FactStoreError, match="검증 상태"):
        s.set_verification(f.fact_id, "verified", reason="r", by="b")
    assert f.verification_status == "unknown" and f.verification_history == []


def test_verification_history_recorded():
    s = FactStore()
    f = _ecog(s, 1)
    s.set_verification(f.fact_id, "unverified", reason="자기 보고뿐, 문서 없음", by="system")
    s.set_verification(f.fact_id, "document_verified", reason="외래 기록 확인", by="연구간호사",
                       document_reference="외래기록 2026-09-01")
    assert f.verification_status == "document_verified" and f.source_type == "self_report"  # 출처는 그대로
    assert [(e.from_status, e.to_status) for e in f.verification_history] == [("unknown", "unverified"),
                                                                              ("unverified", "document_verified")]
    assert f.verification_history[1].document_reference == "외래기록 2026-09-01" and f.value == 1


# ------------------------------------------------------------ 저장·로드, 환자 분리
def test_json_roundtrip_preserves_ids_and_history(tmp_path):
    s = FactStore(tmp_path)
    a = _ecog(s, 1, observed_at="2026-06")
    b = _ecog(s, 2)
    s.set_verification(a.fact_id, "unverified", reason="문서 없음", by="system")
    s.save()
    t = FactStore.load(tmp_path)
    assert [f.model_dump() for f in t.facts(P)] == [a.model_dump(), b.model_dump()]
    c = t.add(P, "ecog", 2)
    assert c.version == 3 and c.previous_fact_id == b.fact_id


def test_patients_are_separated(tmp_path):
    s = FactStore(tmp_path)
    s.add("A1", "ecog", 1)
    s.add("B2", "ecog", 3)
    s.save()
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["A1.json", "B2.json"]
    t = FactStore.load(tmp_path)
    assert [f.value for f in t.facts("A1")] == [1] and [f.value for f in t.facts("B2")] == [3]
    assert t.conflicts("A1") == [] and t.facts("A1")[0].conflict_status == "none"  # 다른 환자 값과 비교하지 않음


def test_load_rejects_mixed_or_mismatched_files(tmp_path):
    s = FactStore(tmp_path)
    s.add("A1", "ecog", 1)
    s.save()
    data = json.loads((tmp_path / "A1.json").read_text(encoding="utf-8"))
    data["facts"][0]["patient_id"] = "B2"
    (tmp_path / "A1.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(FactStoreError, match="다른 환자"):
        FactStore.load(tmp_path)
    (tmp_path / "A1.json").write_text(json.dumps({**data, "patient_id": "Z9"}), encoding="utf-8")
    with pytest.raises(FactStoreError, match="파일 이름"):
        FactStore.load(tmp_path)


# ------------------------------------------------------------ 잘못된 입력
@pytest.mark.parametrize("pid,field,value,kw", [
    ("", "ecog", 1, {}),
    ("../etc", "ecog", 1, {}),
    (P, "", 1, {}),
    (P, "ecog", None, {}),
    (P, "ecog", "  ", {}),
    (P, "ecog", 7, {}),
    (P, "ecog", 1.5, {}),
    (P, "age", -1, {}),
    (P, "sex", "unknown", {}),
    (P, "note", {"a": 1}, {}),
    (P, "ecog", 1, {"source_type": "document_verified"}),
    (P, "ecog", 1, {"observed_at": "06/01/2026"}),
])
def test_invalid_input_rejected(pid, field, value, kw):
    s = FactStore()
    with pytest.raises(FactStoreError):
        s.add(pid, field, value, **kw)
    assert s.patients() == []
