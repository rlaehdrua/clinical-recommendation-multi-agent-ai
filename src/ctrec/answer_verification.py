"""확인 질문의 예/아니요 답을 기준 판정 근거로 쓸 수 있는지 코드로 검증 (P12, 기존 Session 경로).

문제: 환자가 확인 질문에 답했는데도 그 기준이 '정보 부족'으로 남는 경우가 있음. 원인은 둘이다.
  1. Matcher LLM은 재추출한 프로필만 보므로 답변 문장을 그대로 인용하지 못하고 의역함 -> 인용 검증(_enforce_quote)이 되돌림
  2. 짧은 답("아니요")은 질문·대상 기준 연결(qa_log)이 있어야 근거가 되는데, Session.match가 그 기록을 넘기지 않았음
'그 기준을 겨냥한 질문에 답했다'는 사실은 대상 식별 정보일 뿐 답이 그 기준을 충족·불충족한다는 증거가 아니다
(fix: b11ec29는 이 둘을 같게 보아, 답 내용·질문 극성·기간과 무관하게 판정을 인정했음).

여기서는 질문과 기준이 같은 대상·같은 시간 범위를 묻는다는 것을 코드로 확인할 수 있는 경우에만 답을 근거로 인정한다.
- 기준이 E9 개념군(임신·수유, 알레르기·금기)이고 대상 이름이 있음 (그 밖의 기준은 질문과 기준의 의미 대응을 코드로
  검증할 수 없어 인정하지 않음 -> 질문 계약 + 승인된 IR 경로(ContractSession)가 맡음)
- 답이 예/아니요 단답 (모름·거부·추측·긴 서술 제외). 질문이 부정형·조건·인과·검사 여부 질문이 아님
- '없음' 답: 질문의 대상·기간이 기준의 대상·기간을 모두 덮음 / '있음' 답: 질문의 대상·기간이 기준 안에 있음
- 같은 기준을 겨냥한 다른 적격성 답과 어긋나지 않고(답 수정·다른 질문의 반대 답 포함), 처음 입력 기록과 상충하지 않음
- 판정 방향은 LLM이 아니라 답으로 정함: LLM 판정이 답이 말하는 방향과 다르면 인정하지 않음 (판정을 뒤집지도 않음)
인정한 판정도 이어서 근거 수준 정책(답변 = 자기 보고)과 Q&A 정책 경계(contract 정책이면 UNVERIFIED)를 그대로 거친다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import evidence_directness as ed

# 질문의 시간 범위 ("최근 4주 안에", "현재", "~한 적이 있나요")
_Q_WINDOW = re.compile(r"(\d+)\s*(일|주|개월|달|년)")
_Q_CURRENT = re.compile(r"현재|지금|요즘|[가-힣]\s*중(?:이|인|입)|하고\s*있")
_Q_EVER = re.compile(r"[ㄴ은던]?\s*적(?:이)?\s*(?:있|없)|평생|과거에|이전에|지금까지|살면서")
# 기준이 과거 이력 전체를 말하는가 (개념군 기본 범위 대신)
_C_EVER = re.compile(r"\b(?:history|prior|previous|ever)\b|과거|이력|병력", re.IGNORECASE)
# 질문이 다른 사건·조건을 끼워 묻는가 ("항암 치료 때문에 입원했나요?", "수술 후에", "~라면")
_Q_CONDITIONAL = re.compile(r"때문|인해|인한|으로\s*인|후에|뒤에|하면|라면|경우|도중")
_C_OR = re.compile(r"\bor\b|/|또는|이거나|거나|,", re.IGNORECASE)
# 개념군별 기본 시간 범위: 임신·수유 기준은 현재 상태, 알레르기·금기는 이력
_DEFAULT_SCOPE = {"pregnancy_lactation": ("current",), "allergy_contraindication": ("ever",)}
_UNIT_DAYS = {"일": 1, "주": 7, "개월": 30, "달": 30, "년": 365}


@dataclass
class Verified:
    evidence: str  # 원문 그대로의 'Q: 질문 A: 답'
    pairs: list  # 근거가 된 문답 (QAPair)
    check: ed.Check
    reason: str  # 판정 근거 설명 (질문 ID·라운드·검증 항목)


@dataclass
class Rejected:
    reason: str  # 인정하지 않은 이유 코드


def _question_scope(q: str) -> tuple | None:
    kinds = []
    w = _Q_WINDOW.findall(q)
    if len(w) > 1:
        return None
    if w:
        kinds.append(("window", int(w[0][0]) * _UNIT_DAYS[w[0][1]]))
    if _Q_CURRENT.search(q):
        kinds.append(("current",))
    if _Q_EVER.search(q):
        kinds.append(("ever",))
    return kinds[0] if len(kinds) == 1 else None  # 범위가 없거나 여러 개면 정할 수 없음


def _criterion_scope(text: str, time_window_days: int | None, concept: str) -> tuple:
    days = ed.window_days(text, time_window_days)
    if days:
        return ("window", days)
    if _C_EVER.search(text):
        return ("ever",)
    return _DEFAULT_SCOPE[concept]


def _covers(broad: tuple, narrow: tuple) -> bool:
    """시간 범위 broad가 narrow를 포함하는가: 현재 ⊂ 최근 N일 ⊂ 평생, 최근 N ⊂ 최근 M (N <= M)."""
    if broad == narrow or broad[0] == "ever":
        return True
    if narrow[0] == "current":
        return broad[0] == "window"
    return broad[0] == narrow[0] == "window" and narrow[1] <= broad[1]


def _objects_ok(present: bool, q_objs: set[str], c_objs: set[str], criterion: str) -> bool:
    if not c_objs or not q_objs:
        return False  # 대상을 이름으로 대조할 수 없음 (일반 '알레르기', 가임 여부 등)
    if present:  # '있음': 질문이 물은 대상이 모두 기준의 대상(또는 그 하위)이어야 함. 여럿이면 기준이 '또는'일 때만
        if len(q_objs) > 1 and not _C_OR.search(criterion):
            return False
        return all(any(c in ed._ancestors(q) for c in c_objs) for q in q_objs)
    # '없음': 기준의 대상마다 질문이 같은 대상이나 상위 대상의 부재를 물었어야 함
    return all(any(q in ed._ancestors(c) for q in q_objs) for c in c_objs)


def _fact(p, rule, concept: ed.Concept) -> bool | Rejected:
    """이 문답이 말하는 사실: True(있음)/False(없음), 검증할 수 없으면 Rejected."""
    ans = ed._norm(p.answer).strip(" .!")
    q = ed._norm(p.question)
    if ed.non_answer(p.answer):
        return Rejected("non_answer")
    if ans in ed._YES:
        present = True
    elif ans in ed._NO:
        present = False
    else:
        return Rejected("not_yes_no")  # 긴 서술은 일반 인용 경로에서 검사
    if ed.hedged(q.replace("?", " ")) or ed._negated(q) or q.count("?") > 1 or _Q_CONDITIONAL.search(q):
        return Rejected("question_form")  # 부정형("없나요?")·조건·인과·여러 질문: 예/아니요의 뜻을 정할 수 없음
    if not concept.direct.search(q):
        return Rejected("question_concept")
    test_q = bool(concept.requires_test and concept.requires_test.search(q))
    test_c = bool(concept.requires_test and concept.requires_test.search(rule.text))
    if test_q != test_c:
        return Rejected("test_vs_condition")  # '검사를 받았나요?'는 임신 여부가 아님 (반대도 마찬가지)
    q_objs = ed._objects(q, concept)[0]
    c_objs = ed._objects(rule.text, concept)[0]
    if not _objects_ok(present, q_objs, c_objs, rule.text):
        return Rejected("object_scope")
    qs, cs = _question_scope(q), _criterion_scope(rule.text, rule.time_window_days, concept.name)
    if qs is None:
        return Rejected("time_scope_unknown")
    if not (_covers(qs, cs) if not present else _covers(cs, qs)):
        return Rejected("time_scope_mismatch")  # 평생 이력 답으로 최근 4주 기준을, 최근 답으로 평생 기준을 정하지 않음
    return present


def _in_source(p, segs: list) -> bool:
    """문답이 원문 답변 블록에 그 라운드의 질문·답 그대로 있는가."""
    q, a = ed._norm(p.question), ed._norm(p.answer)
    return any(s.role == "answer" and s.question is not None and s.question.body == q and s.body == a
               and s.round == p.round for s in segs)


def verify(rule, trial_id: str, holds: str, qa, source_text: str | None, *, session_id: str | None,
           reference_date: str | None) -> Verified | Rejected:
    """LLM의 확정 판정(holds yes/no, applicable)을 이 기준을 겨냥한 예/아니요 답으로 뒷받침할 수 있는가."""
    if holds not in ("yes", "no") or not qa or source_text is None:
        return Rejected("no_answer")
    if session_id is None:
        return Rejected("no_session")  # 세션을 확인할 수 없는 기록은 쓰지 않음
    concept = next((c for c in ed.CONCEPTS if c.criterion.search(rule.text)), None)
    if concept is None:
        return Rejected("criterion_not_verifiable")  # 질문-기준 의미 대응을 코드로 정할 수 없음 (질문 계약 필요)
    if ed._negated(rule.text):
        return Rejected("criterion_polarity")  # "Negative pregnancy test", "not pregnant": 극성 대응을 코드로 정하지 않음
    key = f"{trial_id}:{rule.rule_id}"
    pairs = [p for p in ed.eligibility_answers(key, qa) if getattr(p, "session_id", None) == session_id]
    pairs = [p for p in pairs if not ed.non_answer(p.answer)]  # 모름·거부는 근거도 반대 근거도 아님
    if not pairs:
        return Rejected("no_answer")
    _, segs = ed.parse_source(source_text)
    facts = []
    for p in pairs:
        if not _in_source(p, segs):
            return Rejected("answer_not_in_source")  # 기록과 원문이 어긋남: 어느 답도 쓰지 않음
        f = _fact(p, rule, concept)
        if isinstance(f, Rejected):
            return f  # 같은 기준을 겨냥한 답 중 하나라도 해석할 수 없으면 상충 여부를 알 수 없음
        facts.append(f)
    if len(set(facts)) > 1:
        return Rejected("conflicting_answers")  # 답 수정·다른 질문의 반대 답: 최신 답을 고르지 않음
    present = facts[0]
    negated_criterion = bool(ed._CRITERION_NEGATION.search(rule.text))
    want = "yes" if present != negated_criterion else "no"
    if holds != want:
        return Rejected("judgment_direction")  # LLM 판정이 답과 반대: 인정하지도 뒤집지도 않음
    quote = f"Q: {pairs[-1].question} A: {pairs[-1].answer}"
    c = ed.check(rule.text, quote, source_text, holds=holds, applicable=True, category=rule.category,
                 time_window_days=rule.time_window_days, reference_date=reference_date, key=key, qa_log=pairs,
                 session_id=session_id)
    if not c.ok:
        return Rejected(f"directness:{c.directness}")  # 처음 입력과 상충·대상 불일치 등 기존 E9 검사
    ids = ", ".join(f"r{p.round}:{p.question_id}" for p in pairs)
    return Verified(quote, pairs, c, f"[코드 확인: 확인 질문 {ids}의 답이 이 기준과 같은 대상·기간을 물은 예/아니요 답으로 검증됨]")
