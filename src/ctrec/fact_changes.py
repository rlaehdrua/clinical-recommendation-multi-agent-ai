"""환자 사실 스냅샷과 변경 감지 (Phase 2). 진단·부분 재평가용이며 사실 저장소를 바꾸지 않습니다.

스냅샷(FactSnapshot)
- 한 환자의 사실 목록을 그 시점 그대로 깊은 복사로 보관합니다. 원본 사실·변경 이력을 지우거나 덮어쓰지 않습니다.
- version은 사실의 '판정에 쓰일 수 있는 내용'(fact_signature)으로 만든 요약값입니다. fact_id·기록 시각·세션은 넣지 않으므로
  같은 내용을 다른 ID로 다시 기록해도 version이 같습니다(Fact ID가 바뀌었다는 이유만으로 재평가하지 않음).

사실 서명(fact_signature): 항목, 값, 단위 표기, 출처, 검증 상태, 관찰 시점(해석한 값·원래 표기), 인용, 출처 참조 종류.
값이 같아도 출처·검증 상태·관찰 시점·인용이 다르면 판정(근거 수준, 기간 조건, 근거 연결)에 영향을 줄 수 있으므로 다른 서명입니다.
같은 서명의 사실이 여러 개면 하나로 봅니다(중복 입력은 판정 입력을 바꾸지 않음).

변경 감지(detect_changes): 항목별로 이전·이후 서명 집합을 비교. 이유 코드
- field_added / field_removed : 항목이 새로 생김 / 사라짐
- value_changed              : 값 집합이 달라짐
- source_changed             : 같은 값인데 출처가 다른 사실이 생김, 또는 같은 사실의 출처가 달라짐
- verification_changed       : 같은 값인데 검증 상태가 다른 사실이 생김, 또는 같은 사실의 검증 상태가 바뀜
- observed_at_changed        : 같은 값인데 관찰 시점(또는 원래 날짜 표기)이 다른 사실이 생김
- reference_changed          : 같은 값인데 인용·출처 참조가 다른 사실이 생김
- conflict_added / conflict_resolved : 단일 값 항목이 일치 -> 불일치(contradictory 등) / 불일치 -> 일치
- not_compared_added / not_compared_resolved : 여러 값 항목에서 값이 다른 쌍(상반된 결과일 수 있음)이 생김 / 없어짐
- superseded                 : 같은 항목의 새 버전(previous_fact_id)이 이전 사실과 다른 내용
- fact_removed               : 이전 스냅샷의 사실이 없어짐 (저장소는 사실을 지우지 않으므로 무결성 경고로도 남김)
- evidence_changed           : 위 어느 것으로도 설명되지 않는 서명 변화 (조용히 넘기지 않기 위한 보수적 표시)
- duplicate                  : 새 fact_id가 있으나 서명이 이미 있던 것과 같음 -> 변경 아님(재평가하지 않음)
출력에는 항목 이름·이유 코드·fact_id·건수만 넣고 환자 값·인용은 넣지 않습니다(changed_facts의 표적 정보는 내부용).
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from .fact_store import MULTI_VALUED_FIELDS, FactStore, PatientFact
from .tools.rule_engine import _norm_unit


class FactSnapshot(BaseModel):
    patient_id: str
    facts: list[PatientFact]
    version: str


def _ws(s) -> str:
    return " ".join(str(s).split()).casefold()


def _value_key(v) -> str:
    if isinstance(v, bool):
        return f"bool:{v}"
    if isinstance(v, (int, float)):
        return f"num:{float(v)!r}"
    return f"str:{_ws(v)}"


def fact_signature(f: PatientFact) -> tuple:
    """판정에 쓰일 수 있는 사실 내용. fact_id·기록 시각·세션·버전 번호는 넣지 않음."""
    ref = f.source_reference
    return (f.field, _value_key(f.value), _norm_unit(f.unit) if f.unit else None, f.source_type,
            f.verification_status, f.observed_at, f.observed_at_text, _ws(ref.quote) if ref.quote else None, ref.kind)


def signature_id(sig: tuple) -> str:
    """서명의 짧은 요약값 (출력·비교용, 원래 값을 드러내지 않음)."""
    return "S-" + hashlib.sha256(json.dumps(sig, ensure_ascii=False).encode()).hexdigest()[:12]


def _version(facts: list[PatientFact]) -> str:
    sigs = sorted({json.dumps(fact_signature(f), ensure_ascii=False) for f in facts})
    return "V-" + hashlib.sha256("\n".join(sigs).encode()).hexdigest()[:12]


def make_snapshot(patient_id: str, facts: list[PatientFact]) -> FactSnapshot:
    copies = [f.model_copy(deep=True) for f in facts]
    if any(f.patient_id != patient_id for f in copies):
        raise ValueError("다른 환자의 사실이 섞여 있음")
    return FactSnapshot(patient_id=patient_id, facts=copies, version=_version(copies))


def take_snapshot(store: FactStore, patient_id: str) -> FactSnapshot:
    """저장소의 현재 사실을 복사한 스냅샷 (저장소는 바꾸지 않음)."""
    return make_snapshot(patient_id, store.facts(patient_id))


def snapshot_store(snap: FactSnapshot) -> FactStore:
    """스냅샷을 담은 메모리 전용 저장소 (사실 사본, 충돌 표시는 사실들로부터 다시 계산). 파일로 저장하지 않음."""
    store = FactStore()
    facts = [f.model_copy(deep=True) for f in snap.facts]
    if facts:
        store._facts[snap.patient_id] = facts
        for field in {f.field for f in facts}:
            store._recompute(snap.patient_id, field)
    return store


# ---------------------------------------------------------------------------
# 변경 감지
# ---------------------------------------------------------------------------

class FieldChange(BaseModel):
    field: str
    reasons: list[str]
    added_fact_ids: list[str] = []  # 새 서명을 가진 사실
    removed_fact_ids: list[str] = []  # 이후 스냅샷에서 서명이 없어진 사실
    duplicate_fact_ids: list[str] = []  # 새 fact_id지만 서명이 이미 있던 사실


class FactChangeSet(BaseModel):
    patient_id: str
    version_before: str
    version_after: str
    changed: dict[str, FieldChange] = {}  # 판정에 영향을 줄 수 있는 변경 (재평가 대상 판단에 씀)
    duplicates: dict[str, list[str]] = {}  # 변경 아님: 항목 -> 중복 fact_id
    integrity_warnings: list[str] = []  # 사라진 사실 등

    @property
    def changed_fields(self) -> list[str]:
        return sorted(self.changed)

    def summary(self) -> dict:
        """환자 값 없이 항목·이유·건수만."""
        return {"version_before": self.version_before, "version_after": self.version_after,
                "changed_fields": self.changed_fields,
                "change_reasons": {n: c.reasons for n, c in sorted(self.changed.items())},
                "duplicate_fields": sorted(self.duplicates), "integrity_warnings": list(self.integrity_warnings)}


def _field_state(facts: list[PatientFact]) -> str:
    """사실 보기(fact_view)와 같은 기준의 항목 상태: agreed / conflict / multi:<값이 다른 쌍 유무>."""
    if not facts:
        return "absent"
    store = snapshot_store(make_snapshot(facts[0].patient_id, facts))
    group = store.facts(facts[0].patient_id, facts[0].field)
    if facts[0].field in MULTI_VALUED_FIELDS:
        return "multi:open" if any(r == "not_compared" for f in group for r in f.related_fact_ids.values()) \
            else "multi:closed"
    return "agreed" if all(f.conflict_status in ("none", "consistent") for f in group) else "conflict"


def _diff_field(name: str, before: list[PatientFact], after: list[PatientFact]) -> tuple[FieldChange | None, list[str]]:
    """반환: (변경 또는 None, 중복 fact_id)."""
    sig_b = {fact_signature(f) for f in before}
    sig_a = {fact_signature(f) for f in after}
    ids_b = {f.fact_id for f in before}
    new = [f for f in after if f.fact_id not in ids_b]
    dup = [f.fact_id for f in new if fact_signature(f) in sig_b]
    if sig_b == sig_a:
        return None, dup
    reasons: list[str] = []
    if not before:
        reasons.append("field_added")
    if not after:
        reasons.append("field_removed")
    if {_value_key(f.value) for f in before} != {_value_key(f.value) for f in after}:
        reasons.append("value_changed")
    by_id_b = {f.fact_id: f for f in before}
    for f in after:  # 같은 사실의 속성 변화 (저장소는 검증 상태만 바꿈)
        old = by_id_b.get(f.fact_id)
        if old is None:
            continue
        if old.verification_status != f.verification_status:
            reasons.append("verification_changed")
        if old.source_type != f.source_type:
            reasons.append("source_changed")
        if (old.observed_at, old.observed_at_text) != (f.observed_at, f.observed_at_text):
            reasons.append("observed_at_changed")
    for f in new:  # 같은 값의 새 사실이 출처·검증·시점·인용 중 무엇이 다른가
        same_value = [o for o in before if _value_key(o.value) == _value_key(f.value)]
        if not same_value or fact_signature(f) in sig_b:
            continue
        if all(o.source_type != f.source_type for o in same_value):
            reasons.append("source_changed")
        if all(o.verification_status != f.verification_status for o in same_value):
            reasons.append("verification_changed")
        if all((o.observed_at, o.observed_at_text) != (f.observed_at, f.observed_at_text) for o in same_value):
            reasons.append("observed_at_changed")
        if all((o.source_reference.quote, o.source_reference.kind) != (f.source_reference.quote, f.source_reference.kind)
               for o in same_value):
            reasons.append("reference_changed")
    state_b, state_a = _field_state(before), _field_state(after)
    if state_b == "agreed" and state_a == "conflict":
        reasons.append("conflict_added")
    elif state_b == "conflict" and state_a in ("agreed", "absent"):
        reasons.append("conflict_resolved")
    elif state_b == "multi:closed" and state_a == "multi:open":
        reasons.append("not_compared_added")
    elif state_b == "multi:open" and state_a in ("multi:closed", "absent"):
        reasons.append("not_compared_resolved")
    for f in new:
        prev = by_id_b.get(f.previous_fact_id or "")
        if prev is not None and fact_signature(prev) != fact_signature(f):
            reasons.append("superseded")
            break
    removed = [f.fact_id for f in before if f.fact_id not in {x.fact_id for x in after}]
    if removed:
        reasons.append("fact_removed")
    if not reasons:
        reasons.append("evidence_changed")
    change = FieldChange(
        field=name, reasons=list(dict.fromkeys(reasons)),
        added_fact_ids=[f.fact_id for f in after if fact_signature(f) not in sig_b],
        removed_fact_ids=[f.fact_id for f in before if fact_signature(f) not in sig_a],
        duplicate_fact_ids=dup)
    return change, dup


def detect_changes(before: FactSnapshot, after: FactSnapshot) -> FactChangeSet:
    """두 스냅샷의 항목별 변경. 같은 환자의 스냅샷만 비교."""
    if before.patient_id != after.patient_id:
        raise ValueError("다른 환자의 스냅샷은 비교하지 않음")
    groups_b: dict[str, list[PatientFact]] = {}
    groups_a: dict[str, list[PatientFact]] = {}
    for f in before.facts:
        groups_b.setdefault(f.field, []).append(f)
    for f in after.facts:
        groups_a.setdefault(f.field, []).append(f)
    out = FactChangeSet(patient_id=after.patient_id, version_before=before.version, version_after=after.version)
    for name in sorted(set(groups_b) | set(groups_a)):
        change, dup = _diff_field(name, groups_b.get(name, []), groups_a.get(name, []))
        if change is not None:
            out.changed[name] = change
            if "fact_removed" in change.reasons:
                out.integrity_warnings.append(f"{name}: 이전 스냅샷의 사실이 없어짐")
        if dup:
            out.duplicates[name] = dup
    return out


def changed_facts(change: FieldChange, before: FactSnapshot, after: FactSnapshot) -> list[PatientFact]:
    """변경된 서명의 사실(추가된 것과 없어진 것). 영향 분석 내부용 (바이오마커 표적 확인)."""
    ids = set(change.added_fact_ids) | set(change.removed_fact_ids)
    seen, out = set(), []
    for f in after.facts + before.facts:
        if f.fact_id in ids and f.fact_id not in seen:
            seen.add(f.fact_id)
            out.append(f)
    return out
