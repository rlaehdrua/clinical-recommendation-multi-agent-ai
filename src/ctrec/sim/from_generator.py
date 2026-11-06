"""생성기 코호트(src/ctrec/cohort, 슬롯 형식) -> 시뮬레이터 코호트(sim.cohort, 사실 형식). 설계 §2.1 대응표를 씁니다.

대응표(JSON, 예: data/nsclc/cohort/source_map.json, data/cohort/source_map.json - 모두 draft)
- ① sources: 생성기 정보원 ID -> 엔진 정보원 ID (S_lab -> lab_report 등). 비용·청구·해시는 엔진 카탈로그 하나만 씀
- ② slots: 슬롯 -> 사실 항목 (정확한 항목 단위, 패턴 아님 - M7). fields가 비면 사실을 만들지 않음(담당의 판단 등).
  sources가 있으면 그 슬롯을 드러내는 엔진 정보원 목록, 없으면 코호트의 유효 소스(reveals)에서 정함
- ③ value_encoding: 수치(age·ecog·수치 검사)는 숫자, sex는 그대로, 나머지는 "<슬롯>=<값>" 문자열 (sim.judge가 다시 읽음)
쓰는 슬롯이 대응표에 없으면 변환을 거부합니다 (조용히 빠지지 않게).

옮기는 것
- public.known -> 처음 알려진 사실 (기록 출처 선언, 문서 검증은 public_verified로 선택). oracle.hidden까지 합친 값 = 정답 사실
- 검사 사실의 날짜 = 기준일 - lab_age_days (기간 조건 기준이 기존 규칙 엔진에서 그대로 작동)
- 환자별 후보 시험, 기준별 슬롯(criterion_slots: 환경이 그 슬롯의 사실만 드러냄)
- 판정기: 슬롯 사전의 연결(Binding)로 CategoricalJudge (기본 auto·reviewed만, include_draft로 검수 전 포함)
옮기지 않는 것: 생성기의 쌍 라벨(oracle.pair_label)·assume_pass. 시뮬레이터의 규칙 GT는 같은 평가기로 정답 사실을 판정해
다시 만들고, 검수 전 기준은 '충족으로 간주'하지 않고 라벨 없음으로 둡니다 (설계 §3.4, Q-G3).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from .. import config as app_config
from .. import trial_store
from ..cohort.spec import LAB_AGE, Cohort, SlotDictionary
from ..engine.contracts import canonical_hash
from ..schemas import ParsedTrial, RuleMeta
from .cohort import SimCohort, SimFact, SimTrial, make_cohort
from .judge import CategoricalJudge, encode

NUMERIC_RAW = frozenset({"age", "ecog"})


class ConversionError(ValueError):
    pass


class SlotMap(BaseModel):
    model_config = ConfigDict(extra="allow")
    fields: list[str]
    sources: list[str] | None = None
    unit: str | None = None
    status: str | None = None
    note: str | None = None


class SourceMap(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: int = 1
    status: str = "draft"
    sources: dict[str, str]
    core_slots: list[str] = Field(default_factory=lambda: ["age", "sex", LAB_AGE])
    slots: dict[str, SlotMap]

    def digest(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


def load_source_map(path: str | Path) -> SourceMap:
    p = Path(path)
    if not p.is_absolute():
        p = app_config.ROOT / p
    return SourceMap.model_validate(json.loads(p.read_text(encoding="utf-8")))


def load_trials(trial_ids: list[str], store_dir: Path | None = None, trials_dir: Path | None = None) -> list[SimTrial]:
    """시험 저장소(data/parsed)의 컴파일된 기준·규칙 메타 + 시험 정보(data/trials)."""
    tdir = trials_dir or app_config.ROOT / "data" / "trials"
    out = []
    for tid in trial_ids:
        rec = trial_store.read_record(tid, store_dir)
        if rec is None:
            raise ConversionError(f"{tid}: 시험 저장소에 기준이 없음")
        tpath = tdir / f"{tid}.json"
        trial = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else {"trial_id": tid}
        meta = {k: RuleMeta.model_validate(v).model_dump(mode="json") for k, v in (rec.get("rule_meta") or {}).items()}
        out.append(SimTrial(ParsedTrial(**rec["parsed"]), trial, MappingProxyType(meta)))
    return out


def _lab_date(reference_date: str, lab_age) -> str:
    days = int(float(lab_age)) if lab_age is not None else 1
    return (date.fromisoformat(reference_date) - timedelta(days=days)).isoformat()


def slot_facts(slot: str, value, sd: SlotDictionary, smap: SourceMap, sources: tuple[str, ...] | None,
               reference_date: str, lab_age, *, source_type: str = "clinical_record",
               verified: bool = False) -> list[SimFact]:
    """슬롯 값 하나 -> 사실 (대응표 ②·③). fields가 비면 빈 목록."""
    m = smap.slots[slot]
    spec = sd.slots.get(slot)
    numeric = spec is not None and spec.kind == "numeric"
    out = []
    for f in m.fields:
        if f in NUMERIC_RAW:
            v, unit = (int(value) if f == "ecog" else value), ("years" if f == "age" else None)
        elif f == "sex":
            v, unit = str(value), None
        elif f.startswith("lab:") and numeric:
            v, unit = float(value), (spec.unit if spec else m.unit)
        else:
            v, unit = encode(slot, value), None
        obs = {"observed_at": _lab_date(reference_date, lab_age)} if f.startswith("lab:") else {}
        out.append(SimFact(field=f, value=v, unit=unit, source_type=source_type, verified=verified, slot=slot,
                           sources=sources, **obs))
    return out


def to_sim_cohort(cohort: Cohort, sd: SlotDictionary, smap: SourceMap, trials: list[SimTrial] | None = None,
                  *, public_verified: bool = False, include_draft: bool = False,
                  salt: str | None = None) -> SimCohort:
    pub = cohort.public
    trials = trials if trials is not None else load_trials(pub.trials)
    if sorted(t.trial_id for t in trials) != sorted(pub.trials):
        raise ConversionError("시험 목록이 코호트와 다름")
    unknown_src = sorted({s.base_source for s in pub.sources.values()} - set(smap.sources))
    if unknown_src:
        raise ConversionError(f"대응표 ①에 없는 생성기 정보원 {unknown_src}")
    used = {s for p in pub.patients for s in (*p.known, *p.unknown_slots)} - set(smap.core_slots)
    unmapped = sorted(used - set(smap.slots))
    if unmapped:
        raise ConversionError(f"대응표 ②에 없는 슬롯 {unmapped}")

    revealed_by: dict[str, set[str]] = {}
    for es in pub.sources.values():
        for s in es.reveals:
            revealed_by.setdefault(s, set()).add(smap.sources[es.base_source])

    def sources_of(slot: str) -> tuple[str, ...]:
        m = smap.slots.get(slot)
        if m is not None and m.sources is not None:
            return tuple(m.sources)
        return tuple(sorted(revealed_by.get(slot, ())))

    patients = []
    for p in pub.patients:
        hidden = cohort.oracle.hidden.get(p.patient_id, {})
        lab_age = p.known.get(LAB_AGE)
        public, truth = [], []
        for slot, v in sorted(p.known.items()):
            if slot == LAB_AGE:
                continue
            public += slot_facts(slot, v, sd, smap, sources_of(slot), cohort.reference_date, lab_age,
                                 verified=public_verified)
            truth += slot_facts(slot, v, sd, smap, sources_of(slot), cohort.reference_date, lab_age)
        for slot, v in sorted(hidden.items()):
            truth += slot_facts(slot, v, sd, smap, sources_of(slot), cohort.reference_date, lab_age)
        patients.append((p.patient_id, public, truth, tuple(p.candidate_trials)))

    criterion_slots = {f"{t}:{r.rule_id}": tuple(r.slots()) for t, refs in pub.rules.items() for r in refs}
    field_slots = {f: s for s, m in smap.slots.items() for f in m.fields
                   if (f in NUMERIC_RAW or f.startswith("lab:")) and s in sd.slots and sd.slots[s].kind == "numeric"}
    judge = CategoricalJudge(sd.bindings, sd.slots, cohort.reference_date, field_slots, include_draft=include_draft)
    return make_cohort(f"{cohort.cohort_id}@{cohort.config_hash}+map{smap.digest()[:8]}", cohort.reference_date, trials,
                       patients, salt or f"sim:{cohort.cohort_id}", criterion_slots, judge)


__all__ = ["SourceMap", "SlotMap", "ConversionError", "load_source_map", "load_trials", "slot_facts", "to_sim_cohort"]
