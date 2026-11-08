"""LLM 기준 대신 판정하는 결정적 판정기 (설계 §3.3 categorical 평가기). 오프라인 실행 전용.

Matcher가 LLM에 넘기는 기준 중 생성기 슬롯 사전에 연결(Binding)이 있는 기준만, **지금 확보된 사실**로 판정합니다.
- 판정 함수는 생성기의 정답 계산과 같은 cohort.truth.holds3 (값 집합 in/not_in, 추가 조건 all/any, 3값 논리).
  값을 모르면 unknown. 숨긴 값(oracle)은 받지 않습니다 - 사실 목록에서 슬롯 값을 다시 읽음.
- 연결 상태: 기본은 생성기 정답과 같은 auto·reviewed만. include_draft=True면 검수 전(draft·needs_review) 연결도 쓰고
  manifest·digest에 표시합니다 (검수 전 결과를 검수된 것처럼 보고하지 않게).
- 판정 결과는 Matcher의 LLM 응답 형식(LLMAssessment)으로 돌려주므로 Matcher의 근거 검사(인용이 원문에 있는가, 진료 기록이
  필요한 기준을 답변만으로 확정하지 않는가)를 그대로 거칩니다. 인용 = 그 슬롯 사실의 원문 줄.
- 해석: "LLM이 완벽하게 읽었다면"의 상한입니다. 실제 LLM의 오판·누락은 반영하지 않습니다.
- 성별 적용 대상 (0.2): 연결의 추가 조건이 성별(슬롯 sex)이고 그 조건만으로 결과가 정해지면(all의 불성립·any의 성립 =
  환자가 기준의 대상 성별이 아님) '해당 없음'(applicable=false)으로, 근거는 구조화 필드 "sex: <값>"으로 답함.
  Matcher는 임신·수유 기준에서만 이 근거를 인정함(evidence_directness.check_structured 예외) - 그 밖에는 Matcher가 보류.
  성별 사실은 항목 이름이 슬롯 이름과 같은 범주형 사실(sex)로 읽음.
"""

from __future__ import annotations

import re

from ..cohort.spec import USABLE, Binding, SlotSpec
from ..cohort.truth import IndeterminateTruth, _cond, holds3
from ..engine.contracts import canonical_hash
from ..schemas import Holds, LLMAssessment, Rule

JUDGE_ID = "categorical_judge/0.2"  # 판정 논리 버전 (평가기 ID에 들어감)
# 코호트 해시(SimCohort.digest)에 들어가는 연결 내용 해시의 이름. 연결이 같으면 같은 해시 (고정 평가셋 manifest와 맞춤).
# 판정 논리가 바뀌면 JUDGE_ID만 올림
DIGEST_ID = "categorical_judge/0.1"
SEX_SLOT = "sex"
ENCODED = re.compile(r"^([a-z][a-z0-9_.]*(?: [a-z0-9_.]+)*)=(.+)$")  # "<슬롯>=<값>" (sim.from_generator 값 표기)


def encode(slot: str, value) -> str:
    return f"{slot}={value}"


class CategoricalJudge:
    judge_id = JUDGE_ID

    def __init__(self, bindings: list[Binding], slots: dict[str, SlotSpec], reference_date: str,
                 field_slots: dict[str, str] | None = None, include_draft: bool = False,
                 expert_rules: frozenset[str] = frozenset()):
        """field_slots: 수치 사실 항목 -> 슬롯 (예: lab:hemoglobin -> lab.hemoglobin). 추가 조건·engine 연결의 수치 읽기용.
        expert_rules: 임상의 판단 기준("<시험>:<기준>") - 값이 있어도 판정하지 않음 (담당의 판단으로 넘김)."""
        allowed = set(USABLE) | ({"draft", "needs_review"} if include_draft else set())
        self.bindings = {b.key: b for b in bindings if b.status in allowed and b.slot and b.truth
                         and b.key not in expert_rules}
        self.slots = dict(slots)
        self.reference_date = reference_date
        self.field_slots = dict(field_slots or {})
        self.include_draft = include_draft
        self.judged = 0  # 판정해 돌려준 기준 수 (unknown 제외)

    def digest(self) -> str:
        return canonical_hash({"judge": DIGEST_ID, "include_draft": self.include_draft,
                               "bindings": sorted((k, b.model_dump_json()) for k, b in self.bindings.items())})

    def values(self, facts) -> tuple[dict[str, float | str], dict[str, str]]:
        """사실 -> (슬롯 값, 슬롯 -> 인용 줄). 같은 슬롯에 다른 값이 있으면 그 슬롯은 모름 (상충)."""
        vals: dict[str, set] = {}
        quote: dict[str, str] = {}
        for f in facts:
            slot, v = None, f.value
            m = ENCODED.match(str(f.value)) if isinstance(f.value, str) else None
            if m and m.group(1) in self.slots:
                slot, v = m.group(1), m.group(2)
            elif f.field in self.field_slots:
                slot = self.field_slots[f.field]
            elif f.field == SEX_SLOT and SEX_SLOT in self.slots and self.slots[SEX_SLOT].kind == "categorical":
                slot = SEX_SLOT  # 성별 사실 (값 male/female, 슬롯 표기 없이 저장됨)
            if slot is None:
                continue
            vals.setdefault(slot, set()).add(v)
            quote.setdefault(slot, f.source_reference.quote or "")
        return {s: next(iter(v)) for s, v in vals.items() if len(v) == 1}, quote

    def assess(self, trial_id: str, rule: Rule, facts) -> LLMAssessment | None:
        """연결이 없으면 None (기존 대역처럼 판정 불가). 있으면 yes/no/unknown."""
        b = self.bindings.get(f"{trial_id}:{rule.rule_id}")
        if b is None:
            return None
        values, quote = self.values(facts)
        outside = self._outside_sex(b, values)
        if outside is not None:
            self.judged += 1
            return LLMAssessment(rule_id=rule.rule_id, applicable=False, holds="unknown", evidence_type="explicit",
                                 confidence="high", evidence=f"{SEX_SLOT}: {outside}",
                                 reasoning="offline_categorical_judge:not_target_sex", missing_info=None)
        try:
            h: Holds = holds3(b, rule, values, self.slots, self.reference_date, check_fresh=True)
        except IndeterminateTruth:
            h = "unknown"
        if h == "unknown":
            return LLMAssessment(rule_id=rule.rule_id, applicable=True, holds="unknown", evidence_type="absent",
                                 confidence="low", evidence="근거 없음", reasoning="offline_categorical_judge",
                                 missing_info="연결된 슬롯 값 없음")
        self.judged += 1
        lines = [quote[s] for s in b.slots_used() if quote.get(s)]
        return LLMAssessment(rule_id=rule.rule_id, applicable=True, holds=h, evidence_type="explicit",
                             confidence="high", evidence=lines[0] if lines else "근거 없음",
                             reasoning="offline_categorical_judge", missing_info=None)


    @staticmethod
    def _outside_sex(b: Binding, values) -> str | None:
        """성별 추가 조건만으로 결과가 정해지면 그 성별 값 (환자가 대상 성별이 아님), 아니면 None."""
        decisive = "no" if b.combine == "all" else "yes"
        for c in b.also:
            if c.slot == SEX_SLOT and SEX_SLOT in values and _cond(c, values) == decisive:
                return str(values[SEX_SLOT])
        return None


__all__ = ["CategoricalJudge", "JUDGE_ID", "encode"]
