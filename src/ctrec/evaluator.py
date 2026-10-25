"""결정적 평가기 (재설계 P4): 승인된 Condition IR + 계약 명제 사실 -> 기존 CriterionAssessment.

LLM을 부르지 않습니다. 판정 상태를 새로 만들지 않고 기존 체계에 그대로 매핑합니다.
- 확정: holds yes/no (또는 applicable=False) -> passes는 rule_engine.passes와 같은 규칙
- 근거 부족: holds unknown, passes None (질문 대상, decision_state gap = missing)
- 진료 기록 필요 + 자기 보고뿐: UNVERIFIED (needs_verification, self_reported_holds 보존, gap = unverified)
- 상충: holds unknown + needs_verification (matcher의 conflicting 처리와 같음, gap = unverified/conflict)

사실 -> 명제 값 (leaf)
1. 기준 시점: 기준의 기간이 assessment가 아닌 시점(first_dose 등)이면 IR의 anchor_policy가 승인한 근사만 씀. 없으면 미확인
2. 계약 확인: 사실을 만든 계약이 지금도 활성이고 내용(digest)이 같아야 함 (계약이 바뀌면 이전 답은 근거가 아님)
3. 시점 이동: 답변 기준일이 평가 기준일과 다르면 '있음' 사실만 기간을 늘려 씀(4주 이내 있음 @d0 -> 4주+경과일 이내 있음).
   '없음'·'현재' 사실은 그 사이 일을 모르므로 쓰지 않음(stale)
4. 함의: propositions.entails (좁은 명제 참 -> 넓은 명제 참, 넓은 명제 거짓 -> 좁은 명제 거짓)만
5. 같은 명제에 참과 거짓이 함께 나오면 상충 (최신 답을 고르지 않음)
식은 Kleene 3값 논리(AND/OR/NOT)로 계산합니다. 상충 leaf가 하나라도 있으면 확정하지 않습니다.
completeness=partial IR은 확정에 쓰지 않습니다(계산 값은 기록만).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .condition_ir import CriterionIR
from .proposition_store import PropFact
from .propositions import Proposition, TimeScope, entails
from .question_contract import ContractRegistry
from .schemas import CriterionAssessment, Rule
from .tools.rule_engine import passes as rule_passes


@dataclass
class Leaf:
    target: str
    value: bool | None
    status: str  # decided | missing | conflict | anchor_unresolved | unsupported
    facts: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    decided: bool  # 평가기가 확정·UNVERIFIED·상충 중 하나를 냈는가 (False면 기존 판정을 유지)
    assessment: CriterionAssessment | None
    provenance: dict


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _shift(f: PropFact, ref: str) -> tuple[Proposition, bool] | None:
    """사실을 평가 기준일 기준으로 옮김. 옮길 수 없으면 None."""
    try:
        gap = _days(f.as_of, ref)
    except ValueError:
        return None
    if gap == 0:
        return f.proposition, f.value
    if gap < 0 or f.value is not True:
        return None
    s = f.proposition.scope
    if s.kind == "ever":
        return f.proposition, True
    if s.kind == "window" and s.anchor == "assessment":
        return f.proposition.with_scope(TimeScope("window", s.days + gap)), True  # type: ignore[operator]
    if s.kind == "current":
        return f.proposition.with_scope(TimeScope("window", gap)), True
    return None


def _contract_ok(f: PropFact, contracts: ContractRegistry) -> bool:
    """사실을 만든 계약이 지금도 활성·같은 내용이고, 그 선택지가 실제로 이 명제·값을 단언하는가 (명제 키·값 위조 차단)."""
    c = contracts.raw(f.contract_id)
    if c is None or contracts.status(f.contract_id)[0] != "active" or c.version != f.contract_version             or c.digest()[:12] != f.contract_digest12:
        return False
    try:
        return (f.proposition.key, f.value) in {(p.key, v) for p, v in c.assertions(f.answer_label)}
    except KeyError:
        return False


def leaf_value(target: Proposition, ir: CriterionIR, facts: list[PropFact], ref: str,
               contracts: ContractRegistry) -> Leaf:
    s = target.scope
    if s.kind == "window" and s.anchor != "assessment":
        if ir.anchor_policy.get(s.anchor) != "assessment_proxy":
            return Leaf(target.key, None, "anchor_unresolved")
        target = target.with_scope(TimeScope("window", s.days, "assessment"))
    derived: dict[bool, list[str]] = {True: [], False: []}
    for f in facts:
        if not _contract_ok(f, contracts):
            continue
        moved = _shift(f, ref)
        if moved is None:
            continue
        v = entails(moved[0], moved[1], target)
        if v is not None:
            derived[v].append(f.fact_id)
    if derived[True] and derived[False]:
        return Leaf(target.key, None, "conflict", derived[True] + derived[False])
    for v in (True, False):
        if derived[v]:
            return Leaf(target.key, v, "decided", derived[v])
    return Leaf(target.key, None, "missing")


def _eval(node: dict, ir, facts, ref, contracts, leaves: list[Leaf]) -> bool | None:
    op = node["op"]
    if op == "atom":
        lf = leaf_value(Proposition.parse(node["prop"]), ir, facts, ref, contracts)
        leaves.append(lf)
        return lf.value
    if op in ("compare", "unsupported"):
        # 계약은 아직 수치 답을 사실로 만들지 않음 -> 수치 비교는 기존 규칙 엔진·기록 경로가 판정
        leaves.append(Leaf(node.get("prop") or node.get("text", ""), None, "unsupported"))
        return None
    if op == "not":
        v = _eval(node["arg"], ir, facts, ref, contracts, leaves)
        return None if v is None else not v
    vals = [_eval(a, ir, facts, ref, contracts, leaves) for a in node["args"]]
    if op == "and":
        return False if False in vals else True if all(v is True for v in vals) else None
    return True if True in vals else False if all(v is False for v in vals) else None


def evaluate(ir: CriterionIR, rule: Rule, facts: list[PropFact], reference_date: str,
             contracts: ContractRegistry, required_evidence: str = "self_report_ok") -> EvalResult:
    leaves: list[Leaf] = []
    applicable = True
    if ir.applicability is not None:
        a = _eval(ir.applicability, ir, facts, reference_date, contracts, leaves)
        applicable = a  # type: ignore[assignment]
    holds = None
    if applicable is True:
        holds = _eval(ir.condition, ir, facts, reference_date, contracts, leaves)
    used = sorted({fid for lf in leaves for fid in lf.facts})
    by_id = {f.fact_id: f for f in facts}
    prov = {
        "criterion_id": f"{ir.trial_id}:{ir.rule_id}", "ir_version": ir.ir_version, "ir_digest": ir.digest(),
        "propositions": sorted({lf.target for lf in leaves if lf.status != "unsupported"}),
        "leaves": [{"proposition": lf.target, "status": lf.status, "value": lf.value, "facts": lf.facts}
                   for lf in leaves],
        "fact_ids": used,
        "contracts": sorted({f"{by_id[i].contract_id}@v{by_id[i].contract_version}" for i in used}),
        "questions": sorted({f"r{by_id[i].round}:{by_id[i].question_id}" for i in used}),
        "sources": sorted({by_id[i].source for i in used}),
        "method": "contract", "completeness": ir.completeness,
    }
    conflict = any(lf.status == "conflict" for lf in leaves)
    confirmed = (applicable is False) or (holds is not None)
    if conflict:
        prov["outcome"] = "conflict"
        return EvalResult(True, _assessment(rule, ir, "unknown", True, None, used, by_id, required_evidence,
                                            flags=["conflicting_contract_answers"], needs_verification=True,
                                            note="같은 명제에 대한 계약 답변이 서로 반대 (최신 답을 자동 채택하지 않음)"),
                          prov)
    if not confirmed:
        prov["outcome"] = "undecided"
        return EvalResult(False, None, prov)
    if ir.completeness != "complete":
        prov["outcome"] = "partial_ir_not_used"
        prov["provisional"] = {"applicable": applicable, "holds": holds}
        return EvalResult(False, None, prov)
    sources = {by_id[i].source for i in used}
    need_record = "clinical_record" in (ir.source_requirement, required_evidence)
    holds_s = "unknown" if holds is None else ("yes" if holds else "no")
    if need_record and sources != {"clinical_record"}:
        prov["outcome"] = "unverified"
        a = _assessment(rule, ir, "unknown", True, None, used, by_id, required_evidence,
                        flags=["contract_self_report_needs_record"], needs_verification=True,
                        note="진료 기록이 필요한 기준인데 근거가 계약 답변(자기 보고)뿐")
        a.self_reported_holds = holds_s if applicable else "unknown"  # type: ignore[assignment]
        a.self_reported_applicable = bool(applicable)
        return EvalResult(True, a, prov)
    prov["outcome"] = "decided"
    passes = True if applicable is False else rule_passes(rule.kind, holds_s)
    return EvalResult(True, _assessment(rule, ir, holds_s if applicable else "unknown", bool(applicable), passes,
                                        used, by_id, required_evidence), prov)


def _assessment(rule: Rule, ir: CriterionIR, holds: str, applicable: bool, passes, used, by_id, required_evidence,
                *, flags=(), needs_verification=False, note="") -> CriterionAssessment:
    ev = " / ".join(f"{i} ({by_id[i].contract_id}@v{by_id[i].contract_version} r{by_id[i].round}:"
                    f"{by_id[i].question_id} '{by_id[i].answer_label}')" for i in used) or "근거 없음"
    verdict = "해당 없음" if not applicable else holds
    return CriterionAssessment(
        rule_id=rule.rule_id, kind=rule.kind, criterion_text=rule.text, holds=holds,  # type: ignore[arg-type]
        passes=passes, confidence="high" if passes is not None else "low", evidence=ev,
        reasoning=f"[결정적 평가: IR {ir.key} v{ir.ir_version} -> {verdict}]{' ' + note if note else ''}",
        missing_info=None if passes is not None else (note or "검증된 질문 계약으로 확인 필요"),
        method="contract", category=rule.category, underspecified=rule.underspecified, cohort=rule.cohort,
        applicable=applicable, evidence_source="self_report" if used else "none",
        required_evidence=required_evidence,  # type: ignore[arg-type]
        needs_verification=needs_verification, evidence_directness="contract", validation_flags=list(flags),
    )
