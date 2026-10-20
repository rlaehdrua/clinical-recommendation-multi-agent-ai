"""사실 스냅샷과 변경 감지 (Phase 2, 체크포인트 2). 합성 데이터, API 호출 없음."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.fact_changes import detect_changes, make_snapshot

from incremental_fixtures import P, base_facts, snap, store


def _changes(before, after):
    return detect_changes(snap(before), snap(after))


def test_duplicate_fact_is_not_a_change():
    s = store(*base_facts())
    before = snap(s)
    s.add(P, "ecog", 1, source_type="clinical_record")  # 같은 내용을 새 fact_id로 다시 기록
    c = detect_changes(before, snap(s))
    assert c.changed == {} and list(c.duplicates) == ["ecog"]
    assert c.version_before == c.version_after


def test_fact_ids_alone_do_not_change_version():
    a, b = store(*base_facts()), store(*base_facts())  # 같은 내용, 다른 fact_id
    assert {f.fact_id for f in a.facts(P)} != {f.fact_id for f in b.facts(P)}
    c = _changes(a, b)
    assert c.changed == {} and c.version_before == c.version_after


def test_value_change_between_snapshots():
    c = _changes(store(("ecog", 1, {})), store(("ecog", 2, {})))
    assert c.changed_fields == ["ecog"] and "value_changed" in c.changed["ecog"].reasons


def test_source_change_with_same_value():
    s = store(("ecog", 1, {"source_type": "self_report"}))
    before = snap(s)
    s.add(P, "ecog", 1, source_type="clinical_record")
    c = detect_changes(before, snap(s))
    assert "source_changed" in c.changed["ecog"].reasons and "value_changed" not in c.changed["ecog"].reasons


def test_verification_change_on_same_fact():
    s = store(("lab:hemoglobin", 11.2, {"unit": "g/dL"}))
    before = snap(s)
    [f] = s.facts(P)
    s.set_verification(f.fact_id, "document_verified", reason="결과지 확인", by="tester", document_reference="DOC-1")
    c = detect_changes(before, snap(s))
    assert c.changed["lab:hemoglobin"].reasons == ["verification_changed"]
    assert before.facts[0].verification_status == "unknown"  # 스냅샷은 사본 (이후 변경이 스며들지 않음)


def test_observed_at_change_with_same_value():
    s = store(("lab:hemoglobin", 11.2, {"unit": "g/dL", "observed_at": "2026-01-05"}))
    before = snap(s)
    s.add(P, "lab:hemoglobin", 11.2, unit="g/dL", source_type="clinical_record", observed_at="2026-09-01")
    c = detect_changes(before, snap(s))
    assert "observed_at_changed" in c.changed["lab:hemoglobin"].reasons


def test_conflict_added_and_supersession():
    s = store(("ecog", 1, {}))
    before = snap(s)
    s.add(P, "ecog", 2, source_type="clinical_record")
    r = detect_changes(before, snap(s)).changed["ecog"].reasons
    assert {"value_changed", "conflict_added", "superseded"} <= set(r)


def test_conflict_resolved_between_snapshots():
    before = store(("ecog", 1, {}), ("ecog", 2, {}))  # 상충
    after = store(("ecog", 2, {}))
    c = _changes(before, after)
    assert "conflict_resolved" in c.changed["ecog"].reasons
    assert "fact_removed" in c.changed["ecog"].reasons and c.integrity_warnings  # 저장소는 사실을 지우지 않음 -> 경고


def test_biomarker_opposite_results_flagged():
    s = store(("biomarker", "EGFR exon 19 deletion positive", {}))
    before = snap(s)
    s.add(P, "biomarker", "EGFR negative", source_type="clinical_record")
    assert "not_compared_added" in detect_changes(before, snap(s)).changed["biomarker"].reasons


def test_multiple_fields_changed_at_once():
    s = store(*base_facts())
    before = snap(s)
    s.add(P, "ecog", 2, source_type="clinical_record")
    s.add(P, "lab:platelets", 210, unit="10^9/L", source_type="clinical_record")
    s.add(P, "biomarker", "PD-L1 TPS 60%", source_type="self_report")
    c = detect_changes(before, snap(s))
    assert c.changed_fields == ["biomarker", "ecog", "lab:platelets"]
    assert "field_added" in c.changed["lab:platelets"].reasons


def test_summary_has_no_patient_values_and_snapshot_store_untouched():
    s = store(*base_facts())
    n = len(s.facts(P))
    before = snap(s)
    s.add(P, "biomarker", "EGFR L858R positive", source_type="self_report")
    text = json.dumps(detect_changes(before, snap(s)).summary(), ensure_ascii=False)
    for v in ("L858R", "62", "11.2", "female", "비소세포폐암"):
        assert v not in text
    assert len(s.facts(P)) == n + 1  # 감지는 저장소를 바꾸지 않음


def test_different_patients_not_compared():
    with pytest.raises(ValueError):
        detect_changes(snap(store(("age", 60, {}))), snap(store(("age", 60, {}), patient="SYN-2"), "SYN-2"))
    with pytest.raises(ValueError):
        make_snapshot(P, store(("age", 60, {}), patient="SYN-2").facts("SYN-2"))
