"""환자 정보 -> 임상시험 기준 의존 그래프와 변경 영향 분석 (Phase 2). 진단·부분 재평가용입니다.

노드: fact:<fact_id>, field:<항목>, trial:<시험>, criterion:<시험>:<기준>, group:<시험>:<코호트|all>, decision:<시험>
관계: 사실 -> 항목 -> 기준 -> 기준 묶음(공통 + 코호트, AND) -> 시험 판정(코호트 중 가장 나은 것, OR)
기준 묶음과 시험 판정은 기존 Matcher의 결정 규칙(matcher.decide, matcher._select_cohort)과 같은 구조입니다.

기준의 의존 항목 식별 (단순 문자열 유사도로 의존성을 확정하지 않음)
구조적으로 검증된 의존성만 좁게 잡고, 나머지는 모든 항목에 의존하는 것으로 봅니다.
1) static_review      : 파싱 검증에서 자동 판정하지 않는 기준(review). 판정은 환자와 무관하나, 구조화 필드의 사실 값이
                        맞지 않으면 '상충 정보 확인'으로 바뀌므로 그 필드에만 의존 (코드 경로로 검증됨)
2) explicit_field     : 규칙 엔진으로 판정된 기준. 규칙 엔진은 구조화 필드(age, sex, ecog, lab:<표준 검사명>) 하나만 읽으므로
                        그 필드(바이오마커 유형이면 근거 게이트가 읽는 biomarker 포함)에만 의존 (코드 경로로 검증됨)
3) llm_all_fields     : LLM 판정이 필요한 기준(구조화 필드가 있어도 규칙 엔진이 판정하지 못한 기준 포함).
                        기존 Matcher LLM은 환자 프로필 전체를 받으므로 실제로 쓴 입력 항목을 구조적으로 검증할 수 없음
                        -> 기본 정책에서 모든 항목에 의존하는 것으로 봄. 이전 LLM 근거에 나오지 않았다는 이유로 항목을 빼지 않음.
                        유형별 매핑(LLM_CATEGORY_FIELDS)·바이오마커 표적·이전 근거가 이어진 항목은 candidate_fields에
                        최적화 후보·진단 정보로만 남기며, 이것만으로 LLM 판정 재사용을 허용하지 않음
4) fallback_all_fields: LLM 판정 기준 중 유형별 매핑 후보도 없는 유형(other, procedure, consent_or_logistics, 알 수 없는 유형)
바이오마커 표적 구분(EGFR 기준은 PD-L1 사실과 무관)은 규칙 엔진 기준의 의존 범위와 근거 게이트(fact_view)에서 쓰며,
LLM 기준에서는 후보 정보(biomarker_targets)로만 남깁니다.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel

from .agents import matcher
from .fact_changes import FactChangeSet, FactSnapshot, changed_facts
from .fact_view import FactEvidenceView, _engine_field, link_evidence, non_biomarker_mentions, targets_in
from .schemas import CriterionAssessment, ParsedTrial, Rule, RuleMeta

Resolution = Literal["static_review", "explicit_field", "llm_all_fields", "fallback_all_fields"]

# LLM이 판정에 쓸 수 있는 진단 맥락 (거의 모든 임상 기준의 해석에 쓰임)
CONTEXT_FIELDS = {"primary_diagnosis", "diagnosis", "disease_stage", "other_fact"}
ANY_LAB = "lab:*"
LLM_CATEGORY_FIELDS: dict[str, set[str]] = {
    "diagnosis": set(CONTEXT_FIELDS),
    "disease_stage": set(CONTEXT_FIELDS),
    "biomarker": CONTEXT_FIELDS | {"biomarker"},
    "lab": CONTEXT_FIELDS | {ANY_LAB},
    "treatment_history": CONTEXT_FIELDS | {"treatment_history", "medication"},
    "medication": CONTEXT_FIELDS | {"medication", "treatment_history"},
    "comorbidity": CONTEXT_FIELDS | {"medication", "treatment_history", ANY_LAB},
    "performance_status": CONTEXT_FIELDS | {"ecog"},
    "demographic": CONTEXT_FIELDS | {"age", "sex"},
    "reproductive": CONTEXT_FIELDS | {"age", "sex", "medication", "treatment_history"},
}
# 매핑하지 않는 유형: other, procedure, consent_or_logistics -> fallback_all_fields


class CriterionDependency(BaseModel):
    trial_id: str
    rule_id: str
    cohort: str | None
    evaluator: Literal["review", "rule_engine", "llm"]  # 이 기준을 판정하는 경로
    fields: list[str]  # 의존 항목 (ANY_LAB은 모든 검사 항목)
    wildcard: bool = False  # 모든 항목에 의존 (의존성 미확정 또는 LLM 입력 미검증)
    candidate_fields: list[str] = []  # LLM 기준의 유형별 매핑 등 최적화 후보 (검증 전이라 영향 판단에 쓰지 않음)
    biomarker_targets: list[str] = []  # biomarker 의존을 이 표적으로 한정 (비어 있으면 모든 바이오마커 사실)
    resolution: Resolution
    notes: list[str] = []


def _matches(dep_field: str, name: str) -> bool:
    return dep_field == name or (dep_field == ANY_LAB and name.startswith("lab:"))


def resolve(trial_id: str, rule: Rule, meta: RuleMeta, prev: CriterionAssessment | None = None,
            prev_view: FactEvidenceView | None = None) -> CriterionDependency:
    """기준 하나의 의존 항목. prev: 이전 평가의 원 판정(근거 게이트 전), prev_view: 그때의 사실 보기."""
    base = {"trial_id": trial_id, "rule_id": rule.rule_id, "cohort": rule.cohort}
    hint = _engine_field(rule)
    if meta.review:
        # 판정은 환자와 무관하지만, 구조화 필드의 사실 값이 맞지 않으면 '상충 정보 확인'으로 바뀌므로 그 필드에는 의존
        return CriterionDependency(**base, evaluator="review", fields=[hint] if hint else [], resolution="static_review")
    if hint and rule.structured_evaluable and rule.operator and prev is not None and prev.method == "rule_engine":
        fields = [hint] + (["biomarker"] if rule.category == "biomarker" else [])  # 근거 게이트의 바이오마커 확인
        return CriterionDependency(**base, evaluator="rule_engine", fields=fields, resolution="explicit_field",
                                   biomarker_targets=sorted(targets_in(rule.text)) if rule.category == "biomarker" else [])
    # LLM 판정 경로 (규칙 엔진이 판정하지 못한 구조화 기준 포함): 입력 항목을 검증할 수 없으므로 항상 모든 항목에 의존
    notes: list[str] = ["llm_inputs_unverified"]
    candidates: set[str] = {hint} if hint else set()
    mapped = LLM_CATEGORY_FIELDS.get(rule.category)
    if mapped:
        candidates |= mapped
    else:
        notes.append(f"no_mapping_for_category:{rule.category}")
    targets = sorted(targets_in(rule.text))
    if targets:
        candidates.add("biomarker")
    if prev is not None and prev.method == "llm" and prev_view is not None and prev.evidence:
        link = link_evidence(prev.evidence, prev_view)
        candidates |= set(link.fields) | {k.lower() for k, _ in matcher._FIELD_REF.findall(prev.evidence)}
    return CriterionDependency(**base, evaluator="llm", fields=[hint] if hint else [], wildcard=True,
                               candidate_fields=sorted(candidates), biomarker_targets=targets,
                               resolution="llm_all_fields" if mapped else "fallback_all_fields", notes=notes)


def cohort_groups(parsed: ParsedTrial) -> dict[str, list[str]]:
    """기준 묶음 -> rule_id. matcher._select_cohort와 같은 코호트 목록(선언 + 규칙에 붙은 값). 코호트가 없으면 all."""
    labels = [c for c in parsed.cohorts if any(r.cohort == c for r in parsed.rules)]
    for r in parsed.rules:
        if r.cohort is not None and r.cohort not in labels:
            labels.append(r.cohort)
    if not labels:
        return {"all": [r.rule_id for r in parsed.rules]}
    return {c: [r.rule_id for r in parsed.rules if r.cohort in (None, c)] for c in labels}


class DependencyGraph:
    """의존 그래프. edges: (상위, 하위) = 하위가 상위에 의존."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: set[tuple[str, str]] = set()
        self.deps: dict[tuple[str, str], CriterionDependency] = {}
        self.groups: dict[str, dict[str, list[str]]] = {}  # trial -> 묶음 -> rule_id
        self.not_evaluated: list[str] = []

    def _node(self, nid: str, **attrs) -> str:
        self.nodes.setdefault(nid, attrs)
        return nid

    @classmethod
    def build(cls, parsed: dict[str, ParsedTrial | None], deps: dict[tuple[str, str], CriterionDependency],
              snapshot: FactSnapshot | None = None) -> "DependencyGraph":
        g = cls()
        if snapshot is not None:
            for f in snapshot.facts:
                g.edges.add((g._node(f"fact:{f.fact_id}", type="fact", field=f.field),
                             g._node(f"field:{f.field}", type="field")))
        for tid, p in parsed.items():
            trial = g._node(f"trial:{tid}", type="trial")
            decision = g._node(f"decision:{tid}", type="decision")
            g.edges.add((trial, decision))
            if p is None:  # NOT_EVALUATED: 기준이 없어 의존 관계도 없음 (판정 그대로 보존)
                g.nodes[decision]["not_evaluated"] = True
                g.not_evaluated.append(tid)
                continue
            g.groups[tid] = cohort_groups(p)
            for label, rids in g.groups[tid].items():
                group = g._node(f"group:{tid}:{label}", type="group", combine="AND", cohort=label)
                g.edges.add((group, decision))
                for rid in rids:
                    g.edges.add((f"criterion:{tid}:{rid}", group))
            g.nodes[decision]["combine"] = "OR(best cohort)" if len(g.groups[tid]) > 1 else "AND"
            for r in p.rules:
                d = deps[(tid, r.rule_id)]
                g.deps[(tid, r.rule_id)] = d
                crit = g._node(f"criterion:{tid}:{r.rule_id}", type="criterion", resolution=d.resolution,
                               evaluator=d.evaluator)
                g.edges.add((trial, crit))
                for name in (["*"] if d.wildcard else []) + d.fields:
                    g.edges.add((g._node(f"field:{name}", type="field"), crit))
        return g

    def summary(self) -> dict:
        return {"nodes": dict(Counter(n["type"] for n in self.nodes.values())), "edges": len(self.edges),
                "resolution": dict(Counter(d.resolution for d in self.deps.values())),
                "not_evaluated_trials": list(self.not_evaluated)}


# ---------------------------------------------------------------------------
# 영향 분석
# ---------------------------------------------------------------------------

class AffectedCriterion(BaseModel):
    trial_id: str
    rule_id: str
    matched_fields: list[str]
    resolution: Resolution
    reason: Literal["dependency_changed", "dependency_unresolved", "llm_inputs_unverified"]


class ImpactReport(BaseModel):
    changed_fields: list[str]
    change_reasons: dict[str, list[str]]
    affected_trials: list[str]
    affected_groups: list[str]
    affected_criteria: list[AffectedCriterion]
    unaffected_criteria: list[str]  # "<trial>:<rule>"
    skipped_by_target: list[str] = []  # 바이오마커 표적이 달라 영향 없음으로 본 기준
    dependency_resolution: dict[str, int]
    fallback_required: bool  # 의존성 미확정 기준이 영향 대상에 있음
    fallback_criteria: list[str] = []
    reevaluation_required: bool

    def summary(self) -> dict:
        return {"changed_fields": self.changed_fields, "affected_trials": self.affected_trials,
                "n_affected_criteria": len(self.affected_criteria),
                "n_unaffected_criteria": len(self.unaffected_criteria),
                "n_fallback_criteria": len(self.fallback_criteria), "skipped_by_target": len(self.skipped_by_target),
                "dependency_resolution": self.dependency_resolution, "fallback_required": self.fallback_required,
                "reevaluation_required": self.reevaluation_required}


def _fact_targets(f) -> set[str]:
    # 검사로 확인된 언급(예: 신장 기능 eGFR)도 표시해 '표적 미상'으로 보지 않음 (fact_view._fact_targets와 같은 규칙)
    return (targets_in(f.value) | targets_in(f.source_reference.quote)
            | non_biomarker_mentions(f.value) | non_biomarker_mentions(f.source_reference.quote))


def analyze_impact(changes: FactChangeSet, graph: DependencyGraph, before: FactSnapshot,
                   after: FactSnapshot) -> ImpactReport:
    """변경된 항목 -> 영향받는 기준 -> 기준 묶음 -> 시험 판정. 놓치는 것(거짓 음성)을 막는 쪽으로 판단."""
    changed = changes.changed
    biomarker_targets: list[set[str]] = []  # 변경된 바이오마커 사실의 표적 (빈 집합 = 표적 미상)
    if "biomarker" in changed:
        biomarker_targets = [_fact_targets(f) for f in changed_facts(changed["biomarker"], before, after)]
        if not biomarker_targets:  # 사실 단위로 확인하지 못한 변화 -> 표적 미상으로 봄
            biomarker_targets = [set()]
    affected, unaffected, skipped, fallback = [], [], [], []
    for (tid, rid), d in sorted(graph.deps.items()):
        matched = sorted({n for n in changed for df in d.fields if _matches(df, n)})
        if "biomarker" in matched and d.biomarker_targets:
            wanted = set(d.biomarker_targets)
            if not any(not t or t & wanted for t in biomarker_targets):
                matched.remove("biomarker")
                if not matched and not d.wildcard:
                    skipped.append(f"{tid}:{rid}")
        if d.wildcard and changed:
            affected.append(AffectedCriterion(trial_id=tid, rule_id=rid, matched_fields=sorted(changed),
                                              resolution=d.resolution,
                                              reason="llm_inputs_unverified" if d.evaluator == "llm"
                                              else "dependency_unresolved"))
            fallback.append(f"{tid}:{rid}")
        elif matched:
            affected.append(AffectedCriterion(trial_id=tid, rule_id=rid, matched_fields=matched,
                                              resolution=d.resolution, reason="dependency_changed"))
        else:
            unaffected.append(f"{tid}:{rid}")
    groups = sorted({grp for a in affected for grp, rids in
                     {f"group:{a.trial_id}:{lab}": r for lab, r in graph.groups.get(a.trial_id, {}).items()}.items()
                     if a.rule_id in rids})
    return ImpactReport(
        changed_fields=changes.changed_fields,
        change_reasons={n: c.reasons for n, c in sorted(changed.items())},
        affected_trials=sorted({a.trial_id for a in affected}), affected_groups=groups,
        affected_criteria=affected, unaffected_criteria=unaffected, skipped_by_target=skipped,
        dependency_resolution=dict(Counter(d.resolution for d in graph.deps.values())),
        fallback_required=bool(fallback), fallback_criteria=fallback, reevaluation_required=bool(affected))
