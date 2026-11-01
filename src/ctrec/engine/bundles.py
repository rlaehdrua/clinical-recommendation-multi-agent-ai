"""정보원 카탈로그와 행동 묶음 (WP5 / M7). 문서 1건·접촉 1회가 기준 여러 개를 해결하는 구조.

- DEFAULT_SOURCES: 기본 정보원. **분 단위 값은 자리표시값**이며 실측·문헌값으로 바꿔야 합니다 (docs/engine_contracts.md §5).
- route(): 기존 행동 유형과 필요한 항목으로 정보원을 고름 (결정적: 항목 일치 수 -> 카탈로그 순서).
- bundle(): 같은 환자·같은 정보원의 행동을 묶고, 요청 비용은 한 번 + 항목별 비용만 더함.
acquisition.POLICIES의 안내 문구(문자열)를 구조화한 첫 단계이며, 기존 모듈은 바꾸지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from .contracts import Action, EffortCost, InformationSource, canonical_hash

CONTEXT_FIELDS = frozenset({"primary_diagnosis", "diagnosis", "disease_stage", "other_fact"})

DEFAULT_SOURCES: tuple[InformationSource, ...] = (
    InformationSource(source_id="patient_interview", kind="patient",
                      provides_fields=("age", "sex", "ecog", "medication", "treatment_history", "other_fact",
                                       "reproductive"),
                      request_cost=EffortCost(coordinator_minutes=5, patient_wait_minutes=1440), per_item_minutes=1,
                      can_verify=False),
    InformationSource(source_id="lab_report", kind="document", provides_fields=("lab:*",),
                      request_cost=EffortCost(coordinator_minutes=8, patient_wait_minutes=2880), per_item_minutes=0.5,
                      can_verify=True, alternatives=("medical_record",)),
    InformationSource(source_id="pathology_report", kind="document",
                      provides_fields=("biomarker", "biomarker:*", "diagnosis", "primary_diagnosis", "disease_stage"),
                      request_cost=EffortCost(coordinator_minutes=10, patient_wait_minutes=4320),
                      per_item_minutes=1, can_verify=True, alternatives=("medical_record",)),
    InformationSource(source_id="medication_list", kind="document", provides_fields=("medication", "treatment_history"),
                      request_cost=EffortCost(coordinator_minutes=6, patient_wait_minutes=1440), per_item_minutes=0.5,
                      can_verify=True, alternatives=("medical_record",)),
    InformationSource(source_id="medical_record", kind="document", provides_fields=("*",),
                      request_cost=EffortCost(coordinator_minutes=15, patient_wait_minutes=4320), per_item_minutes=1,
                      can_verify=True),
    InformationSource(source_id="clinician_review", kind="clinician", provides_fields=("*",),
                      request_cost=EffortCost(coordinator_minutes=2, clinician_minutes=10), per_item_minutes=0,
                      can_verify=True),
    InformationSource(source_id="system_check", kind="system", provides_fields=("*",)),
    InformationSource(source_id="system_reparse", kind="system", provides_fields=("*",),
                      request_cost=EffortCost(llm_calls=1)),
    InformationSource(source_id="trial_registry", kind="system", provides_fields=(),
                      request_cost=EffortCost(coordinator_minutes=5)),
)

# 기존 행동 유형 -> 정보원 종류
_KIND_BY_LEGACY = {"ASK_USER": "patient", "REQUEST_DOCUMENT": "document", "VERIFY": "system_check",
                   "NEEDS_REVIEW": "clinician", "RUN_TOOL": "system_check", "REPARSE": "system_reparse",
                   "RETRIEVE": "trial_registry"}


class SourceCatalog:
    def __init__(self, sources: tuple[InformationSource, ...] = DEFAULT_SOURCES):
        ids = [s.source_id for s in sources]
        if len(ids) != len(set(ids)):
            raise ValueError("정보원 ID 중복")
        self.sources = {s.source_id: s for s in sources}
        self.order = ids
        for s in sources:
            missing = [a for a in s.alternatives if a not in self.sources]
            if missing:
                raise ValueError(f"{s.source_id}: 없는 대체 정보원 {len(missing)}개")

    def __getitem__(self, sid: str) -> InformationSource:
        return self.sources[sid]

    def get(self, sid: str | None) -> InformationSource | None:
        return self.sources.get(sid) if sid else None

    def digest(self) -> str:
        return canonical_hash([self.sources[s].model_dump(mode="json") for s in self.order])

    def route(self, legacy_type: str, fields: list[str] | tuple[str, ...], info_key: str | None = None) -> str | None:
        """정보원 ID (없으면 None: DEFER/FINALIZE 등 실행하지 않는 행동)."""
        kind = _KIND_BY_LEGACY.get(legacy_type)
        if kind is None:
            return None
        if kind in self.sources:  # 특정 정보원으로 고정 (system_check 등)
            return kind
        wanted = [f for f in fields if f not in CONTEXT_FIELDS]
        if info_key and info_key.startswith("biomarker:"):
            wanted = wanted or ["biomarker"]
        cands = [s for s in self.order if self.sources[s].kind == kind]
        if not cands:
            return None

        def score(sid: str) -> int:
            pats = self.sources[sid].provides_fields
            specific = sum(any(fnmatch(f, p) for p in pats if p != "*") for f in wanted)
            return specific

        best = max(cands, key=lambda s: (score(s), -self.order.index(s)))
        if score(best) == 0:  # 맞는 전용 정보원이 없으면 같은 종류의 범용 정보원 (예: medical_record)
            generic = [s for s in cands if "*" in self.sources[s].provides_fields]
            return generic[0] if generic else cands[0]
        return best

    def alternatives(self, sid: str, failed: frozenset[str] | set[str]) -> list[str]:
        """실패하지 않은 대체 정보원 (대체의 대체까지 순서대로, 순환 방지)."""
        out, todo, seen = [], list(self.sources[sid].alternatives), {sid}
        while todo:
            a = todo.pop(0)
            if a in seen:
                continue
            seen.add(a)
            if a not in failed:
                out.append(a)
            todo.extend(self.sources[a].alternatives)
        return out


@dataclass(frozen=True)
class ActionBundle:
    patient_alias: str
    source_id: str
    actions: tuple[Action, ...]
    criteria: tuple[str, ...]
    cost: EffortCost

    @property
    def charge_key(self) -> str:
        return charge_key(self.patient_alias, self.source_id)


def charge_key(alias: str, source_id: str) -> str:
    return f"{alias}|{source_id}"


def items_of(action: Action) -> tuple[str, ...]:
    """청구 단위 항목: 대상 기준 (없으면 행동 지문 하나)."""
    return tuple(action.target_criteria) or (f"action:{action.fingerprint}",)


def bundle_cost(source: InformationSource, n_items: int) -> EffortCost:
    rc = source.request_cost
    return rc.model_copy(update={"coordinator_minutes": rc.coordinator_minutes + source.per_item_minutes * n_items})


def bundle(actions: list[Action], catalog: SourceCatalog) -> list[ActionBundle]:
    """같은 (환자, 정보원) 행동을 묶음. 같은 기준은 한 번만 셈. 정보원이 없는 행동은 묶지 않음."""
    groups: dict[tuple[str, str], list[Action]] = {}
    for a in actions:
        if a.source_id is None:
            continue
        groups.setdefault((a.patient_alias, a.source_id), []).append(a)
    out = []
    for (alias, sid), acts in sorted(groups.items()):
        items = tuple(dict.fromkeys(i for a in acts for i in items_of(a)))
        out.append(ActionBundle(alias, sid, tuple(acts), tuple(k for k in items if not k.startswith("action:")),
                                bundle_cost(catalog[sid], len(items))))
    return out
