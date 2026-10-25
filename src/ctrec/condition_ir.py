"""Condition IR: 기준 하나의 조건을 명제 논리식으로 표현한 검수 대상 초안 (재설계 P2).

IR은 Frozen Store(data/parsed/)와 따로 저장합니다(ir_review.py, contracts/ir/). trial_id·rule_id·원문은 바꾸지 않고
참조만 하며, 잠긴 기준표 파일 해시와 원문이 달라지면 IR은 무효입니다.

식 노드 (JSON)
- {"op": "atom", "prop": "<명제 키>"}                         명제가 참
- {"op": "compare", "prop": "<수치 명제 키>", "cmp": ">=", "value": 6.5, "unit": "%"}
- {"op": "and"|"or", "args": [노드, ...]}, {"op": "not", "arg": 노드}
- {"op": "unsupported", "text": "<원문 일부>", "reason": "<표현하지 못한 이유>"}   평가하면 항상 미확인
CriterionIR.condition은 '기준 문장이 서술하는 조건이 환자에게 성립함(holds=yes)'을 뜻합니다(제외 기준도 같음, schemas.Rule).
applicability가 있으면 그 식이 거짓일 때 기준이 적용되지 않음(해당 없음)입니다.

completeness
- complete: 원문의 모든 조건·한정어·기간이 식에 들어 있음 (검수자가 확인할 사항)
- partial : unsupported 노드가 있거나 일부만 표현함 -> 평가기는 이 IR로 확정하지 않음(미확인만)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .propositions import ANCHORS, Proposition, PropositionError

CMP = (">=", "<=", ">", "<", "==")
SOURCE_REQUIREMENTS = ("self_report_ok", "clinical_record")
ANCHOR_POLICIES = ("assessment_proxy",)  # 이 기준 시점을 평가 시점으로 근사해도 된다는 검수 결정


class IRError(ValueError):
    pass


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_node(node, path: str = "condition") -> list[str]:
    """식 노드의 형식 오류 목록 (빈 목록 = 형식상 올바름. 의미가 맞는지는 검수 사항)."""
    errs: list[str] = []
    if not isinstance(node, dict) or "op" not in node:
        return [f"{path}: 노드는 op가 있는 객체"]
    op = node["op"]
    if op == "atom":
        try:
            Proposition.parse(node.get("prop", ""))
        except PropositionError as e:
            errs.append(f"{path}: {e}")
        extra = set(node) - {"op", "prop"}
    elif op == "compare":
        try:
            p = Proposition.parse(node.get("prop", ""))
            if p.aspect != "measurement":
                errs.append(f"{path}: compare는 수치 명제에만")
        except PropositionError as e:
            errs.append(f"{path}: {e}")
        if node.get("cmp") not in CMP:
            errs.append(f"{path}: 비교 연산자 {node.get('cmp')!r}")
        v = node.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errs.append(f"{path}: 비교값은 숫자")
        if not node.get("unit"):
            errs.append(f"{path}: 단위가 없음 (단위 없는 수치 비교는 하지 않음)")
        extra = set(node) - {"op", "prop", "cmp", "value", "unit"}
    elif op in ("and", "or"):
        args = node.get("args")
        if not isinstance(args, list) or len(args) < 2:
            errs.append(f"{path}: {op}는 인자 2개 이상")
            args = args if isinstance(args, list) else []
        for i, a in enumerate(args):
            errs += validate_node(a, f"{path}.{op}[{i}]")
        extra = set(node) - {"op", "args"}
    elif op == "not":
        errs += validate_node(node.get("arg"), f"{path}.not")
        extra = set(node) - {"op", "arg"}
    elif op == "unsupported":
        if not str(node.get("text", "")).strip() or not str(node.get("reason", "")).strip():
            errs.append(f"{path}: unsupported에는 원문(text)과 이유(reason)가 필요")
        extra = set(node) - {"op", "text", "reason"}
    else:
        return [f"{path}: 알 수 없는 op {op!r}"]
    if extra:
        errs.append(f"{path}: 알 수 없는 필드 {sorted(extra)}")
    return errs


def walk(node):
    yield node
    for a in node.get("args", []) or []:
        yield from walk(a)
    if "arg" in node:
        yield from walk(node["arg"])


def propositions_in(node) -> set[str]:
    return {Proposition.parse(n["prop"]).key for n in walk(node) if n.get("op") in ("atom", "compare")}


@dataclass
class CriterionIR:
    trial_id: str
    rule_id: str
    ir_version: int
    criterion_text: str  # 잠긴 기준표의 Rule.text 그대로
    source_span: str  # 식이 표현한 원문 구간 (criterion_text의 부분 문자열)
    frozen_file_hash: str  # data/parsed/<trial_id>.json의 sha256 (FROZEN.json에 적힌 값)
    condition: dict
    completeness: str  # complete | partial
    source_requirement: str  # self_report_ok | clinical_record
    applicability: dict | None = None
    anchor_policy: dict = field(default_factory=dict)  # {"first_dose": "assessment_proxy"} (검수 결정)
    unsupported_parts: list[str] = field(default_factory=list)
    notes: str = ""
    author: str = ""
    review_status: str = "draft"  # 파일에 적힌 표시일 뿐, 활성화는 ir_review의 검수 기록으로만 정해짐

    CONTENT_KEYS = ("trial_id", "rule_id", "ir_version", "criterion_text", "source_span", "frozen_file_hash",
                    "condition", "completeness", "source_requirement", "applicability", "anchor_policy",
                    "unsupported_parts")

    @classmethod
    def from_dict(cls, d: dict) -> "CriterionIR":
        allowed = set(cls.CONTENT_KEYS) | {"notes", "author", "review_status"}
        extra = set(d) - allowed
        if extra:
            raise IRError(f"{d.get('trial_id')}:{d.get('rule_id')} 알 수 없는 필드 {sorted(extra)}")
        try:
            return cls(**d)
        except TypeError as e:
            raise IRError(str(e)) from e

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (*self.CONTENT_KEYS, "notes", "author", "review_status")}

    @property
    def key(self) -> str:
        return f"{self.trial_id}:{self.rule_id}"

    def digest(self) -> str:
        """검수 대상 내용의 해시 (notes·author·review_status 제외). 내용이 바뀌면 이전 승인은 무효."""
        return hashlib.sha256(_canon({k: getattr(self, k) for k in self.CONTENT_KEYS}).encode("utf-8")).hexdigest()

    def errors(self) -> list[str]:
        e: list[str] = []
        if not isinstance(self.ir_version, int) or isinstance(self.ir_version, bool) or self.ir_version < 1:
            e.append("ir_version은 1 이상의 정수")
        if not self.source_span or self.source_span not in self.criterion_text:
            e.append("source_span이 원문(criterion_text)의 부분 문자열이 아님")
        if self.completeness not in ("complete", "partial"):
            e.append(f"completeness {self.completeness!r}")
        if self.source_requirement not in SOURCE_REQUIREMENTS:
            e.append(f"source_requirement {self.source_requirement!r}")
        e += validate_node(self.condition)
        if self.applicability is not None:
            e += validate_node(self.applicability, "applicability")
        if e:
            return e
        nodes = list(walk(self.condition)) + (list(walk(self.applicability)) if self.applicability else [])
        if any(n["op"] == "unsupported" for n in nodes) and self.completeness == "complete":
            e.append("unsupported 노드가 있는데 completeness=complete")
        if self.unsupported_parts and self.completeness == "complete":
            e.append("unsupported_parts가 있는데 completeness=complete")
        for anchor, pol in self.anchor_policy.items():
            if anchor not in ANCHORS or anchor == "assessment" or pol not in ANCHOR_POLICIES:
                e.append(f"anchor_policy {anchor}: {pol}")
        return e

    def anchors(self) -> set[str]:
        nodes = list(walk(self.condition)) + (list(walk(self.applicability)) if self.applicability else [])
        return {Proposition.parse(n["prop"]).scope.anchor for n in nodes if n.get("op") in ("atom", "compare")}
