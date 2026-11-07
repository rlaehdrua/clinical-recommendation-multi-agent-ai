"""확정성 경계: 엔진이 확정하지 않은 쌍을 어떤 출력도 확정으로 말하지 않게 하는 단일 기준. 순수 읽기, LLM 호출 없음.

**판정을 고치는 계층이 아닙니다.** eligibility·기준별 판정·Fact Store·PairStatus·Session.matches는 그대로입니다.
바뀌는 것은 외부 상태(match_status)·추천 그룹·문구뿐이고, 추천에서 내려간 시험의 판정은 여전히 ELIGIBLE입니다.

evaluate 하나가 기준이고 모든 출력이 이를 따릅니다.
- Session.finalize: 추천과 보고서 LLM 입력 (held_matches, held_recommendation, report_input)
- Session.save: result.json, report.md (save)
- 웹 result 이벤트와 SSE 재생, CLI, 오케스트레이터 종료 응답 (build)

표현을 낮추는 경우 (HOLD 대상)
- side="match"   : 외부 CRITERIA_MATCH인데 엔진이 CONFIRMED_ELIGIBLE이 아님. 필수 기준 0개인 쌍도 포함 (엔진 NOT_EVALUATED).
                   또는 엔진은 확정했어도 원문에 구체 기준이 없는 항목이 통과로 판정됨 (임상의 판단 대기, 판정은 그대로).
- side="mismatch": 외부 CRITERIA_MISMATCH인데 불충족 근거가 모두 상충이라 열린 경로가 있음(TrialState.mismatch == "none").
  표시만 낮추고 추천 목록에는 넣지 않습니다. 제외 근거가 다툼 중일 뿐 충족이 확인된 것은 아니기 때문입니다.

일부러 낮추지 않는 경우 (engine_boundary에 불일치로 기록만 함)
- 상충 없는 불충족이지만 문서 검증 전(exclusion_unverified): 문서 검증 없는 clinical_record 문제와 같은 축이라 R2 소관.

보류 사유는 사실 관계별로 나눕니다. 날짜 차이를 값 모순으로 말하지 않습니다.
- time_dependent     : 날짜가 다른 값. 실제 변화일 수 있음
- contradictory      : 같은 시점(또는 시점 불명)의 값이 맞지 않음. 출처가 다른 상충 포함
- incomparable       : 값을 비교할 수 없음(단위 등)
- differing_biomarker: 관련 바이오마커 기록의 내용이 다름. 상반된 결과인지 별개 변이인지 코드가 모름
- no_required_criteria: 판정에 쓰인 필수 기준이 없음
- clinical_judgment  : 원문에 구체 기준이 없는 항목이 통과로 판정됨

LLM 문장은 정규식으로만 점검합니다(_CLAIM). 표현 목록에 없는 바꿔 쓴 문장은 잡지 못하므로, 점검 통과가 안전을 뜻하지 않습니다.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .agents.recommender import DISPLAY_BASIS, display_key
from .decision_state import SessionView, build_state
from .engine.cohort_state import CohortState, decisive_criteria, required_count
from .engine.contracts import PairStatus
from .eval_hygiene import AliasMap
from .schemas import Recommendation
from .status import LABEL_KO, label

ENGINE_PENDING = "engine_pending"
TIME_DEPENDENT = "time_dependent"
NO_REQUIRED = "no_required_criteria"
CLINICAL = "clinical_judgment"
DISPLAY_BY_KIND = {TIME_DEPENDENT: "POTENTIAL_MATCH", "contradictory": "NEEDS_REVIEW", "incomparable": "NEEDS_REVIEW",
                   "differing_biomarker": "NEEDS_REVIEW", ENGINE_PENDING: "NEEDS_REVIEW", NO_REQUIRED: "NEEDS_REVIEW",
                   CLINICAL: "NEEDS_REVIEW"}
NOTE_BY_KIND = {
    TIME_DEPENDENT: "날짜가 다른 값이 있어 어느 시점 값을 기준으로 할지 진료 기록 확인 필요",
    "contradictory": "서로 맞지 않는 값이 있어 의료진 확인 필요",
    "incomparable": "값을 서로 비교할 수 없어 의료진 확인 필요",
    "differing_biomarker": "내용이 다른 바이오마커 기록이 있어 의료진 확인 필요 (상반된 결과인지 별개 변이인지 미확인)",
    ENGINE_PENDING: "정책 엔진이 아직 확정하지 않은 항목이 있어 의료진 확인 필요",
    NO_REQUIRED: "판정에 쓰인 구체 기준이 없어 의료진 확인 필요",
    CLINICAL: "원문에 구체 기준이 없는 항목이라 담당 의사의 판단 필요",
}
_SEVERE_FIRST = ("contradictory", "incomparable", "differing_biomarker", CLINICAL, NO_REQUIRED, ENGINE_PENDING,
                 TIME_DEPENDENT)
_CONFIRMED = {"match": ("CRITERIA_MATCH", label("CRITERIA_MATCH")),
              "mismatch": ("CRITERIA_MISMATCH", label("CRITERIA_MISMATCH"))}
_TEXT_FIELDS = ("rationale", "key_matches", "key_concerns", "next_steps")
HELD_EXCLUSION = "engine_hold"  # 화면의 제외 종류: 확정 제외가 아니라 근거 확인 대기
_NOT_FIT = "모집 중인 후보 시험이 모두 선정/제외 기준에 맞지 않았습니다."  # recommender 문구. 다툼 중인 불충족이 있으면 사실이 아님
_NO_CONFIRMED_FIT = "모집 중인 후보 시험 중 기준 충족이 확정된 시험이 없습니다."

# 시험 전체가 확정된 것처럼 읽히는 표현. 기준 하나의 통과("나이 기준 충족")는 대상이 아니고, 여부·가능성·부정은 뒤의 제외 조건으로 거름
_NOT_A_CLAIM = r"(?!\s*(?:여부|가능|하지|되지|못|않|할 수 있는지|할지|되는지|되었는지|했는지))"
_CLAIM = re.compile(
    r"CRITERIA_MATCH|(?<![A-Za-z])ELIGIBLE|(?<![A-Za-z])eligible\b"
    r"|(?:공개|모든|전체|선정)\S*\s*(?:(?:제외|선정)\s*)?기준(?:을|에|이)?\s*(?:모두\s*|전부\s*)?(?:충족|부합|통과|만족)" + _NOT_A_CLAIM +
    r"|기준(?:을|에|이)?\s*(?:모두|전부)\s*(?:충족|부합|통과|만족)" + _NOT_A_CLAIM +
    r"|(?<![부비])적격(?!\s*(?:여부|성|아님|이 아|판정을? 하지|하지))"
    r"|(?:참여|등록|참가)(?:할|하실)?\s*(?:가능|수\s*있)(?!\s*(?:여부|성|한지|하지|할지|는지|을지))"
    r"|(?:참여|등록)\s*대상(?:입니다|이다|임|으로)"
    r"|(?:가장\s*)?적합(?:합니다|한\s*시험|함)")
_CLAIM_REMOVED = "확정처럼 읽히는 문장을 표시하지 않습니다."


@dataclass(frozen=True)
class Hold:
    trial_id: str
    side: Literal["match", "mismatch"]
    pair_status: str
    kinds: tuple[str, ...]
    rule_ids: tuple[str, ...]
    display_status: str

    def notes(self) -> list[str]:
        where = f"{', '.join(self.rule_ids)}: " if self.rule_ids else ""
        return [f"{where}{NOTE_BY_KIND[k]}" for k in self.kinds]


@dataclass
class View:
    """사용자에게 내보낼 사본. holds가 비어 있으면 result·report는 받은 값 그대로 (engine_boundary만 더함)."""
    result: dict
    report: str | None
    holds: dict[str, Hold] = field(default_factory=dict)
    engine: dict[str, dict] = field(default_factory=dict)  # 시험 -> 판정·외부 상태·엔진 상태·남은 불일치 (코드만)
    claims: list[dict] = field(default_factory=list)  # 보고서에서 낮춘 LLM 확정 표현 (줄 번호·표현·시험, 문장 원문 없음)


def _kind(c, conflicting: dict[str, str]) -> str:
    if not (c.gap == "conflict" or c.conflict_on_decided):
        return ENGINE_PENDING
    if c.category == "biomarker":
        return "differing_biomarker"
    return next((conflicting[f] for f in c.fields if f in conflicting), ENGINE_PENDING)


def _judged_by_llm(m) -> tuple[str, ...]:
    """원문에 구체 기준이 없는데 통과로 판정된 항목. 엔진 gap에 잡히지 않으므로 판정 기록을 직접 읽음."""
    return tuple(a.rule_id for a in m.assessments if a.underspecified and a.passes is True)


def _hold(tid: str, side: str, status: PairStatus, t, conflicting: dict[str, str], judged: tuple[str, ...] = ()) -> Hold:
    open_c = [c for c in decisive_criteria(t) if c.gap != "none" or c.conflict_on_decided]
    found = {_kind(c, conflicting) for c in open_c}
    if judged:
        found.add(CLINICAL)
    if side == "match" and required_count(t) == 0:
        found.add(NO_REQUIRED)
    found = found or {ENGINE_PENDING}
    kinds = tuple(k for k in _SEVERE_FIRST if k in found)
    # 불충족 쪽은 최신 값이 탈락일 수 있으므로 '충족 가능'으로 말하지 않음
    shown = DISPLAY_BY_KIND[kinds[0]] if side == "match" else "NEEDS_REVIEW"
    rule_ids = tuple(sorted({c.rule_id for c in open_c} | set(judged)))
    return Hold(tid, side, status.value, kinds, rule_ids, shown)  # type: ignore[arg-type]


def _unresolved(m, status: PairStatus, t, held: bool) -> str | None:
    """표현을 낮추지 않았지만 엔진과 어긋나는 쌍의 사유 코드. 어느 정책 결정을 기다리는지 나타냄."""
    if held:
        return "no_required_criteria_policy_open" if required_count(t) == 0 else None
    if m.match_status == "CRITERIA_MISMATCH" and status != PairStatus.CONFIRMED_INELIGIBLE:
        return "exclusion_not_document_verified"
    return None


def evaluate(session) -> tuple[dict[str, Hold], dict[str, dict]]:
    """(보류, 시험별 엔진 대조). 판정·Session을 바꾸지 않음. 소진은 확정을 만들지 않으므로 운영 상태는 읽지 않음."""
    if not session.matches:
        return {}, {}
    view = SessionView.from_session(session)
    view.matched_versions = {t: view.snapshot.version for t in session.matches}
    state = build_state(view)
    alias = AliasMap().alias(session.case.patient_id)
    cohort = CohortState.from_decision_states({alias: state})
    holds: dict[str, Hold] = {}
    engine: dict[str, dict] = {}
    for tid, m in session.matches.items():
        pair, t = cohort.pair(alias, tid), state.trials.get(tid)
        if pair is None:
            continue
        hold = None
        judged = _judged_by_llm(m) if m.match_status == "CRITERIA_MATCH" else ()
        if t is not None and m.match_status == "CRITERIA_MATCH" and (pair.status != PairStatus.CONFIRMED_ELIGIBLE or judged):
            hold = _hold(tid, "match", pair.status, t, state.conflicting_fields, judged)
        elif t is not None and m.match_status == "CRITERIA_MISMATCH" and t.mismatch == "none":
            hold = _hold(tid, "mismatch", pair.status, t, state.conflicting_fields)
        if hold:
            holds[tid] = hold
        engine[tid] = {"eligibility": m.eligibility, "external": m.match_status, "engine_status": pair.status.value,
                       "pending_kinds": sorted(pair.pending_kinds), "shown": hold.display_status if hold else m.match_status,
                       "unresolved": _unresolved(m, pair.status, t, bool(hold)) if t is not None else None}
    return holds, engine


# ---------------------------------------------------------------------------
# 사본 표현
# ---------------------------------------------------------------------------

def _relabel(value, hold: Hold):
    """시험별 LLM 문구의 확정 표현만 낮춘 라벨로 바꿈. 코드 값과 한국어 라벨만 다루고 다른 표현은 추측하지 않음."""
    code, ko = _CONFIRMED[hold.side]
    if isinstance(value, list):
        return [_relabel(v, hold) for v in value]
    if isinstance(value, str):
        return value.replace(code, hold.display_status).replace(ko, label(hold.display_status))
    return value


def _held_summary(summary: str | None, hold: Hold) -> str | None:
    """matcher 요약의 맨 앞 판정 코드를 외부 상태로 바꿈. 통과·불충족 건수는 그대로."""
    return re.sub(r"^(?:IN)?ELIGIBLE\b", hold.display_status, _relabel(summary, hold)) if summary else summary


def _strip_claims(value, status: str | None):
    """확정되지 않은 시험의 LLM 문구에서 확정처럼 읽히는 문장을 뺌. 문장을 고쳐 쓰지 않고 상태 표시로 대신함."""
    if isinstance(value, list):
        return [_strip_claims(v, status) for v in value]
    if isinstance(value, str) and _CLAIM.search(value):
        return f"{label(status)}: {_CLAIM_REMOVED}"
    return value


def _notice(holds: dict[str, Hold]) -> str:
    items = ", ".join(f"{t} ({label(h.display_status)})" for t, h in sorted(holds.items()))
    return (f"정책 엔진이 확정하지 않은 시험이 있습니다: {items}. 아래 설명과 다르면 이 표시가 우선하며, "
            f"이 표시는 판정을 바꾼 것이 아닙니다.")


def _with(items: list | None, notes: list[str]) -> list[str]:
    return [*(items or []), *(n for n in notes if n not in (items or []))]  # 두 번 적용돼도 같은 결과


def _held_item(item: dict, hold: Hold) -> dict:
    notes = hold.notes()
    return {**item, **{k: _relabel(item[k], hold) for k in _TEXT_FIELDS if k in item},
            "match_status": hold.display_status,
            "verification_needed": _with(item.get("verification_needed"), notes),
            "next_steps": _with(_relabel(item.get("next_steps") or [], hold), notes)}


def _shown_item(item: dict, hold: Hold | None) -> dict:
    item = _held_item(item, hold) if hold else item
    if item.get("match_status") == "CRITERIA_MATCH":
        return item
    return {**item, **{k: _strip_claims(item[k], item.get("match_status")) for k in _TEXT_FIELDS if k in item}}


def _recommendation(rec: dict, session, holds: dict[str, Hold]) -> dict:
    """추천 사본. 판정은 읽기만 하고 그룹·순서·문구를 기준에 맞춤. 이미 적용된 추천에 다시 적용해도 같은 결과.

    finalize 뒤 사실이 바뀌어 새로 생긴 보류도 여기서 낮춰지므로 build는 항상 이 함수를 거침."""
    held = {t: held_match(session.matches[t], h) for t, h in holds.items()}
    index = {t: i for i, t in enumerate(session.matches)}

    def key(item: dict) -> tuple:
        t = item["trial_id"]
        return display_key(held.get(t, session.matches[t]), session.trials[t], index[t])
    ranked = sorted((_shown_item(r, holds.get(r.get("trial_id"))) for r in rec.get("ranked") or []), key=key)
    ranked = [{**r, "rank": pos} for pos, r in enumerate(ranked, start=1)]
    top = ranked[0]["match_status"] if ranked else None
    comment, reason = rec.get("overall_comment") or "", rec.get("no_recommendation_reason")
    outs = {t: h for t, h in holds.items() if h.side == "mismatch"}
    if reason and outs:
        fixed = reason.replace(_NOT_FIT, _NO_CONFIRMED_FIT)
        reason = fixed if _notice(outs) in fixed else f"{fixed} {_notice(outs)}"
        comment = reason
    elif not reason and _CLAIM.search(comment) and (holds or top != "CRITERIA_MATCH"):
        comment = _CLAIM_REMOVED
    if holds and _notice(holds) not in comment:
        comment = f"{_notice(holds)} {comment}".strip()
    out = {**rec, "ranked": ranked, "recommended": [r for r in ranked if r["match_status"] == top],
           "selection_reason": f"추천 그룹: {LABEL_KO.get(top or '', '')}. {DISPLAY_BASIS}" if ranked else "",
           "overall_comment": comment, "no_recommendation_reason": reason}
    if outs:  # 확정 제외가 아니므로 제외 목록에서 뺌. 추천에도 넣지 않음
        out["excluded"] = [x for x in rec.get("excluded") or [] if x.get("trial_id") not in outs]
        out["held_out"] = [{"trial_id": t, "match_status": h.display_status, "reasons": h.notes()}
                           for t, h in sorted(outs.items())]
    return out


# ---------------------------------------------------------------------------
# Session.finalize가 쓰는 입력 (판정은 Session.matches에 그대로 남음)
# ---------------------------------------------------------------------------

def held_match(m, hold: Hold):
    """외부 상태와 요약만 낮춘 사본. eligibility는 recommender의 대상 판단에 쓰이므로 그대로."""
    return m.model_copy(update={"match_status": hold.display_status, "summary": _held_summary(m.summary, hold)})


def held_matches(session, holds: dict[str, Hold]) -> list:
    return [held_match(m, holds[t]) if t in holds else m for t, m in session.matches.items()]


def held_recommendation(rec: Recommendation, session, holds: dict[str, Hold]) -> Recommendation:
    return Recommendation.model_validate(_recommendation(rec.model_dump(), session, holds))


def report_input(matches: list, rec: Recommendation, history: list[dict], holds: dict[str, Hold]) -> tuple:
    """보고서 LLM 입력: 보류 시험의 판정 코드를 넘기지 않음. 설명 LLM이 ELIGIBLE을 보고 확정 문장을 쓸 근거를 없앰.

    보류 사유는 report_section이 코드로 씀. verification_needed는 'UNVERIFIED(답변뿐)' 절로 나가므로 여기서 뺌."""
    if not holds:
        return matches, rec, history

    def item(r):
        if r.trial_id not in holds:
            return r
        notes = holds[r.trial_id].notes()
        return r.model_copy(update={"eligibility": "UNCERTAIN",
                                    "verification_needed": [n for n in r.verification_needed if n not in notes]})
    return ([m.model_copy(update={"eligibility": "UNCERTAIN"}) if m.trial_id in holds else m for m in matches],
            rec.model_copy(update={"ranked": [item(r) for r in rec.ranked], "recommended": [item(r) for r in rec.recommended]}),
            [{**h, "eligibility": {t: "보류" if t in holds else e for t, e in h["eligibility"].items()}} for h in history])


def report_section(holds: dict[str, Hold]) -> str:
    """보류 사유 절 (코드가 고정 기재)."""
    if not holds:
        return ""
    lines = ["## 정책 엔진 미확정 항목", "", "아래 시험은 판정을 바꾸지 않고 표시만 낮췄습니다. 근거를 확인한 뒤 다시 평가하세요.", ""]
    lines += [f"- **{t}** ({label(h.display_status)}) - {n}" for t, h in sorted(holds.items()) for n in h.notes()]
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# 보고서 본문 점검
# ---------------------------------------------------------------------------

def _trial_re(trials) -> re.Pattern | None:
    ids = sorted(trials, key=len, reverse=True)
    return re.compile("(?<![A-Za-z0-9_-])(" + "|".join(map(re.escape, ids)) + ")(?![A-Za-z0-9_-])") if ids else None


def _is_code_line(line: str, rec: dict | None) -> bool:
    """explainer가 코드로 쓴 머리글: 코드 판정을 그대로 옮긴 줄이라 점검 대상이 아님."""
    if line.startswith(("## 추천 그룹:", "추천 그룹:", "## 추천 결과:")):
        return True
    reason = (rec or {}).get("no_recommendation_reason")
    return bool(reason) and line.strip("* ") == reason


def _scan_report(text: str, shown: dict[str, str], holds: dict[str, Hold], rec: dict | None) -> tuple[str, list[dict]]:
    """LLM 본문에서 확정되지 않은 시험을 확정처럼 말하는 줄을 낮춤. 어느 시험도 가리키지 않는 문장은 확정 시험이 없거나
    보류가 있으면 대상이 됨. 제목 아래 줄은 그 제목의 시험을 가리키는 것으로 봄. 표 행은 셀만, 그 밖의 줄은 줄 전체를 바꿈."""
    names = _trial_re(shown)
    if names is None:
        return text, []
    confirmed = any(s == "CRITERIA_MATCH" for s in shown.values())
    unnamed = sorted(holds) if holds else ([] if confirmed else sorted(shown))
    out, claims, context = [], [], []
    for n, line in enumerate(text.split("\n"), start=1):
        named = sorted(set(names.findall(line)))
        if line.startswith("#"):
            context = named
        found = _CLAIM.findall(line)
        pointed = named or context
        about = [t for t in pointed if shown[t] != "CRITERIA_MATCH"] if pointed else unnamed
        if not found or not about or _is_code_line(line, rec):
            out.append(line)
            continue
        claims.append({"line": n, "phrases": sorted(set(found)), "trials": about})
        desc = ", ".join(f"{t}: {label(shown[t])}" for t in about)
        out.append(_CLAIM.sub(label(shown[about[0]]), line) if line.startswith("|") else f"> {desc}. {_CLAIM_REMOVED}")
    return "\n".join(out), claims


def _report(report: str | None, rec: dict | None, holds: dict[str, Hold], session) -> tuple[str | None, list[dict]]:
    """코드가 쓴 줄과 보류 시험을 가리키는 줄의 라벨을 고친 뒤 LLM 문장을 점검. session.report(LLM 원문)는 그대로 둠."""
    if not report:
        return report, []
    names = _trial_re(session.matches)
    lines = []
    for line in report.split("\n"):
        named = set(names.findall(line)) if names else set()
        mine = [holds[t] for t in holds if t in named]
        if mine and all(t in holds for t in named):
            for hold in mine:
                line = _relabel(line, hold)
        elif rec and line.startswith("## 추천 그룹:") and rec.get("recommended"):
            line = f"## 추천 그룹: {label(rec['recommended'][0]['match_status'])}"
        elif rec and line.startswith("추천 그룹:") and rec.get("selection_reason"):
            line = rec["selection_reason"]
        lines.append(line)
    shown = {t: holds[t].display_status if t in holds else m.match_status for t, m in session.matches.items()}
    text, claims = _scan_report("\n".join(lines), shown, holds, rec)
    head = f"> **확인 필요:** {_notice(holds)}\n\n" if holds else ""
    if claims:
        head += f"> **자동 점검:** 확정되지 않은 시험을 확정처럼 읽히게 하는 표현 {len(claims)}줄을 낮췄습니다.\n\n"
    return head + text, claims


def build(session, result: dict | None = None, report: str | None = None) -> View:
    """사용자용 사본. result·report를 주지 않으면 Session의 값을 씀. 넘긴 값과 Session은 바꾸지 않음."""
    base = session.result() if result is None else result
    text = session.report if report is None else report
    holds, engine = evaluate(session)
    out = {**base, "engine_boundary": engine}
    if holds:
        out["matches"] = {t: {**m, "match_status": holds[t].display_status,
                              "summary": _held_summary(m.get("summary"), holds[t])} if t in holds else m
                          for t, m in (base.get("matches") or {}).items()}
    rec = _recommendation(base["recommendation"], session, holds) if base.get("recommendation") else None
    if rec is not None:
        out["recommendation"] = rec
    shown_report, claims = _report(text, rec, holds, session)
    return View(out, shown_report, holds, engine, claims)


def adjust_display(info: dict | None, holds: dict[str, Hold]) -> dict | None:
    """web.display.build 결과의 제외 종류를 사본에서 맞춤: 다툼 중인 불충족을 '맞지 않음'으로 세지 않음."""
    outs = [t for t, h in holds.items() if h.side == "mismatch"]
    if not info or not outs:
        return info
    trials = {t: {**d, "exclusion": HELD_EXCLUSION} if t in outs and d.get("exclusion") == "mismatch" else d
              for t, d in (info.get("trials") or {}).items()}
    out = {**info, "trials": trials}
    if info.get("no_recommendation"):
        kinds = {k: [t for t in v if t not in outs] for k, v in info["no_recommendation"].items()}
        out["no_recommendation"] = {**kinds, HELD_EXCLUSION: outs}
    return out


def save(session, out_dir: Path) -> View:
    """Session.save가 호출. 저장 결과도 같은 기준: match_status·추천은 보수적 값, eligibility·기준 판정은 그대로이고 원래 외부
    상태는 engine_boundary의 external에 남음. 표현이 달라졌을 때만 report.raw.md(LLM 원문)와 conservative_view.json을 둠."""
    view = build(session)
    (out_dir / "result.json").write_text(json.dumps(view.result, ensure_ascii=False, indent=2), encoding="utf-8")
    if view.holds or view.claims:
        (out_dir / "conservative_view.json").write_text(json.dumps({
            "note": "표현 사본입니다. 판정·사실은 그대로이고 외부 상태·추천 그룹·문구만 다릅니다.",
            "holds": [asdict(h) for h in view.holds.values()], "report_claims": view.claims,
            "engine_boundary": view.engine}, ensure_ascii=False, indent=2), encoding="utf-8")
    if view.report != session.report:
        (out_dir / "report.raw.md").write_text(session.report, encoding="utf-8")
        (out_dir / "report.md").write_text(view.report, encoding="utf-8")
    return view


__all__ = ["Hold", "View", "HELD_EXCLUSION", "evaluate", "build", "adjust_display", "save", "held_match", "held_matches",
           "held_recommendation", "report_input", "report_section"]
