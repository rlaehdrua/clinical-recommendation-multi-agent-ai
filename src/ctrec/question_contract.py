"""질문 계약(Question Contract): 질문 문장·선택지와 '각 답이 어떤 명제의 참·거짓을 뜻하는가'를 고정한 검수 대상 (재설계 P3).

계약 질문은 LLM이 쓰지 않습니다. 계약에 적힌 문장과 선택지를 그대로 보여 주고(render), 답은 선택지 일치로만
명제 사실로 바꿉니다(map_answer). 화면·LLM이 문장이나 선택지를 바꿨으면(verify_asked 실패) 답을 사실로 쓰지 않습니다.

계약 JSON (<root>/question_contracts.json의 "contracts" 목록)
{"contract_id": "QC-SURGERY-LAST", "version": 1, "question": "...", "answer_type": "single_choice",
 "options": [{"label": "4주(28일) 이내", "assertions": [["surgery/event/w28d-assessment", true]]}, ...],
 "notes": "...", "review_status": "draft"}
- 선택지의 assertions는 그 선택지를 고른 환자가 '평가 시점 기준으로' 말한 명제 값입니다(anchor는 assessment만).
- 선택지 안의 단언끼리 코드로 승인된 함의에 비추어 모순이면 형식 오류입니다.
- 활성화는 ir_review.Registry의 'contracts' 검수 기록(digest 승인)으로만 정해집니다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .ir_review import Registry, _read
from .propositions import Proposition, PropositionError, entails
from .schemas import ClarifyingQuestion

CONTRACTS_FILE = "question_contracts.json"
YES_NO = ("예", "아니요")
# 사실을 말하지 않은 답 (모름·거부·미응답). decision_state.DECLINED_ANSWERS에 흔한 변형을 더함.
# 답 전체가 거부·모름인 경우만 (문장 안의 '거부'·'모름'은 사실 진술일 수 있음: "BCG 치료를 거부했습니다").
# fix/qa-evidence-link의 판별(문장형 거부 등)을 합친 유일한 판별 함수 (E9 evidence_directness도 이것을 씀)
# 거부 문장 앞의 짧은 화제어 ("그 부분은 답변하지 않겠습니다", "그건 말하고 싶지 않아요"). 거부 문장에만 붙임
# P9: "해당 질문에", "그 질문에는", "이 부분에 대해서는"처럼 '-에'로 끝나는 화제어도 받음
_TOPIC = r"(?:[가-힣]{1,6}(?:\s+[가-힣]{1,6})?(?:은|는|건|에|에는|에\s*대해(?:서)?(?:는)?)\s+)?"
# 답하지 않겠다는 서술 ("답하고 싶지 않습니다", "답변을 원하지 않습니다", "대답하기 싫어요")
_REFUSE_PRED = (r"(?:을|은|는|도)?\s*(?:하지\s*않[가-힣]*|안\s*함|없음|불가|하고\s*싶지\s*않[가-힣]*|"
                r"원하지\s*않[가-힣]*|드리(?:기|고\s*싶지)\s*(?:싫|어렵|않)[가-힣]*|하기\s*(?:싫|어렵|곤란)[가-힣]*)")
_NON_ANSWER = re.compile(
    r"^(?:잘\s*)?(?:모름|모르겠[가-힣]*|모릅니다|몰라요?)$|^기억(?:이)?\s*(?:안\s*[가-힣]+|나지\s*않[가-힣]*)$|"
    r"^확실하지\s*않[가-힣]*$|"
    rf"^{_TOPIC}(?:답변\s*)?거부(?:합니다|할게요|해요|함)?$|"
    rf"^{_TOPIC}(?:답변|대답|응답|답){_REFUSE_PRED}$|^(?:무|미)응답$|"
    # 확인·판단할 수 없다는 답 ("확인할 수 없습니다", "확인이 어렵습니다", "알 수 없어요"): 사실이 없다는 말이 아님
    rf"^{_TOPIC}(?:확인|판단|파악)(?:할\s*수(?:가)?\s*없[가-힣]*|이\s*(?:어렵|안\s*되|불가)[가-힣]*|\s*불가|"
    r"하기\s*(?:어렵|힘들)[가-힣]*)$|^알\s*수(?:가)?\s*없[가-힣]*$|"
    rf"^{_TOPIC}말하고\s*싶지\s*않(?:아요|습니다|음)?$|^{_TOPIC}말(?:씀\s*드리|하)기\s*(?:싫|어렵)[가-힣]*$|^노\s*코멘트$|"
    r"^(?:unknown|not sure|unsure|don'?t know|i don'?t know|idk|decline[ds]?|refuse[ds]?|"
    r"prefer not to (?:say|answer)|no answer|no comment|n/?a)$", re.IGNORECASE)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()


def non_answer(answer: str | None) -> bool:
    from .decision_state import DECLINED_ANSWERS
    a = _norm(answer).strip(" .!~")
    base = re.sub(r"\s*\([^)]*\)\s*$", "", a).strip(" .!~")  # "모름(답변 없음)"
    return not base or a in DECLINED_ANSWERS or base in DECLINED_ANSWERS or bool(_NON_ANSWER.match(base))


@dataclass
class QuestionContract:
    contract_id: str
    version: int
    question: str
    answer_type: str
    options: list[dict]
    notes: str = ""
    review_status: str = "draft"
    CONTENT_KEYS = ("contract_id", "version", "question", "answer_type", "options")

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionContract":
        extra = set(d) - {*cls.CONTENT_KEYS, "notes", "review_status"}
        if extra:
            raise ValueError(f"{d.get('contract_id')}: 알 수 없는 필드 {sorted(extra)}")
        return cls(**d)

    def digest(self) -> str:
        body = {k: getattr(self, k) for k in self.CONTENT_KEYS}
        return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    @property
    def labels(self) -> list[str]:
        return [o["label"] for o in self.options]

    def assertions(self, label: str) -> list[tuple[Proposition, bool]]:
        for o in self.options:
            if o["label"] == label:
                return [(Proposition.parse(k), v) for k, v in o["assertions"]]
        raise KeyError(label)

    def propositions(self) -> set[str]:
        return {Proposition.parse(k).key for o in self.options for k, _ in o.get("assertions", [])}

    def errors(self) -> list[str]:
        e: list[str] = []
        if not re.fullmatch(r"QC-[A-Z0-9\-]+", self.contract_id or ""):
            e.append(f"contract_id 형식 {self.contract_id!r}")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            e.append("version은 1 이상의 정수")
        if not (self.question or "").strip():
            e.append("질문 문장이 없음")
        if self.answer_type not in ("yes_no", "single_choice"):
            e.append(f"answer_type {self.answer_type!r} (계약은 yes_no·single_choice만)")
        labels = [o.get("label", "") for o in self.options]
        if len(labels) < 2 or len(set(map(_norm, labels))) != len(labels) or not all(l.strip() for l in labels):
            e.append("선택지는 서로 다른 2개 이상")
        if self.answer_type == "yes_no" and tuple(labels) != YES_NO:
            e.append("yes_no 계약의 선택지는 ['예', '아니요']")
        if any(non_answer(l) for l in labels):
            e.append("'모름' 같은 미응답은 선택지로 넣지 않음 (화면이 붙이며 사실이 아님)")
        for o in self.options:
            pairs = []
            for item in o.get("assertions", []):
                try:
                    k, v = item
                    p = Proposition.parse(k)
                except (ValueError, PropositionError) as ex:
                    e.append(f"{o.get('label')}: {ex}")
                    continue
                if not isinstance(v, bool):
                    e.append(f"{o.get('label')}: {k} 값은 true/false")
                    continue
                if p.scope.kind == "window" and p.scope.anchor != "assessment":
                    e.append(f"{o.get('label')}: 환자 답변의 기준 시점은 assessment만 ({k})")
                pairs.append((p, v))
            if not pairs:
                e.append(f"{o.get('label')}: 단언이 없음")
            for p, v in pairs:  # 선택지 안의 모순 (승인된 함의 기준)
                for q, w in pairs:
                    if entails(p, v, q) is not None and entails(p, v, q) != w:
                        e.append(f"{o.get('label')}: {p.key}={v}와 {q.key}={w}가 모순")
        return e


@dataclass
class MappedAnswer:
    kind: str  # option | non_answer | free_text | rejected
    label: str | None = None
    assertions: list[tuple[Proposition, bool]] = field(default_factory=list)
    reason: str = ""


class ContractRegistry:
    def __init__(self, registry: Registry | None = None):
        self.registry = registry or Registry()

    def _all(self) -> list[dict]:
        return _read(self.registry.root / CONTRACTS_FILE, {"contracts": []}).get("contracts", [])

    def raw(self, contract_id: str) -> QuestionContract | None:
        found = [c for c in self._all() if c.get("contract_id") == contract_id]
        if len(found) != 1:
            return None
        try:
            return QuestionContract.from_dict(found[0])
        except (TypeError, ValueError):
            return None

    def status(self, contract_id: str) -> tuple[str, list[str]]:
        c = self.raw(contract_id)
        if c is None:
            return "invalid", ["계약이 없거나 중복·형식 오류"]
        errs = c.errors()
        if errs:
            return "invalid", errs
        decision = self.registry._decision("contracts", contract_id, c.digest())
        if decision == "approved":
            return "active", []
        if decision == "rejected":
            return "rejected", ["검수에서 거부됨"]
        return "unreviewed", ["현재 내용에 대한 사람 검수 승인 없음"]

    def active(self) -> list[QuestionContract]:
        ids = [c.get("contract_id") for c in self._all()]
        return [self.raw(i) for i in dict.fromkeys(ids) if i and self.status(i)[0] == "active"]  # type: ignore[misc]

    def deciding(self, target: Proposition) -> list[QuestionContract]:
        """target 명제의 참·거짓을 적어도 한 선택지가 코드 함의로 정할 수 있는 활성 계약."""
        out = []
        for c in self.active():
            for o in c.options:
                if any(entails(Proposition.parse(k), v, target) is not None for k, v in o["assertions"]):
                    out.append(c)
                    break
        return out


def render(c: QuestionContract, question_id: str, targets: list[str]) -> ClarifyingQuestion:
    """계약 문장·선택지를 그대로 쓴 질문 (LLM 개입 없음)."""
    return ClarifyingQuestion(question_id=question_id, question=c.question, answer_type=c.answer_type,
                              options=list(c.labels) if c.answer_type == "single_choice" else [],
                              target=list(targets), purpose="eligibility",
                              why=f"contract:{c.contract_id}@v{c.version}")


def verify_asked(c: QuestionContract, q: ClarifyingQuestion) -> bool:
    """실제로 보여 준 질문이 계약 그대로인가 (문장·답 형식·선택지)."""
    opts = list(c.labels) if c.answer_type == "single_choice" else []
    return q.question == c.question and q.answer_type == c.answer_type and list(q.options) == opts


def map_answer(c: QuestionContract, q: ClarifyingQuestion, answer: str | None) -> MappedAnswer:
    if not verify_asked(c, q):
        return MappedAnswer("rejected", reason="보여 준 질문이 계약 문장·선택지와 다름")
    if non_answer(answer):
        return MappedAnswer("non_answer", reason="모름·거부·미응답")
    a = _norm(answer or "").strip(" .!")
    if c.answer_type == "yes_no":  # 예/아니요의 흔한 표기만 (문장형 답은 자유 서술로 둠)
        a = {"네": "예", "yes": "예", "y": "예", "아니오": "아니요", "아뇨": "아니요", "no": "아니요", "n": "아니요"}.get(a, a)
    for label in c.labels:
        if a == _norm(label):
            return MappedAnswer("option", label, c.assertions(label))
    return MappedAnswer("free_text", reason="선택지와 정확히 일치하지 않는 답 (사실 후보로만 보관)")
