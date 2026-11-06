"""보수적 표현 계층: 사용자에게 나가는 결과가 엔진 쌍 상태보다 확정적으로 말하지 않게 함. 순수 읽기, LLM 호출 없음.

**판정을 고치는 계층이 아닙니다.** eligibility·기준별 판정·Fact Store·PairStatus·Session.matches·Session.recommendation은
그대로이고, 호출자가 받은 사본의 표현(외부 상태·추천 그룹·문구)만 바뀝니다. 추천에서 내려간 시험의 판정은 여전히 ELIGIBLE입니다.

표현을 낮추는 경우 (HOLD 대상)
- side="match"   : 외부 CRITERIA_MATCH인데 엔진이 CONFIRMED_ELIGIBLE이 아님.
- side="mismatch": 외부 CRITERIA_MISMATCH인데 불충족 근거가 모두 상충이라 열린 경로가 있음(TrialState.mismatch == "none").
  표시만 낮추고 추천 목록에는 넣지 않습니다. 제외 근거가 다툼 중일 뿐 충족이 확인된 것은 아니기 때문입니다.

일부러 낮추지 않는 경우 (engine_boundary에 불일치로 기록만 함)
- 필수 기준 0개 쌍: 표시 정책 미결 (R5 P2/P3).
- 상충 없는 불충족이지만 문서 검증 전(exclusion_unverified): 문서 검증 없는 clinical_record 문제와 같은 축이라 R2 소관.
- OR 동점: 이 계층은 엔진이 낸 상태만 읽고 동점 정책을 정하지 않음.

보류 사유는 사실 관계별로 나눕니다. 날짜 차이를 값 모순으로 말하지 않습니다.
- time_dependent     : 날짜가 다른 값. 실제 변화일 수 있음
- contradictory      : 같은 시점(또는 시점 불명)의 값이 맞지 않음. 출처가 다른 상충 포함
- incomparable       : 값을 비교할 수 없음(단위 등)
- differing_biomarker: 관련 바이오마커 기록의 내용이 다름. 상반된 결과인지 별개 변이인지 코드가 모름
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .agents.recommender import DISPLAY_BASIS, display_key
from .decision_state import SessionView, build_state
from .engine.cohort_state import CohortState, required_count
from .engine.contracts import PairStatus
from .eval_hygiene import AliasMap
from .status import LABEL_KO, label

ENGINE_PENDING = "engine_pending"
TIME_DEPENDENT = "time_dependent"
DISPLAY_BY_KIND = {TIME_DEPENDENT: "POTENTIAL_MATCH", "contradictory": "NEEDS_REVIEW", "incomparable": "NEEDS_REVIEW",
                   "differing_biomarker": "NEEDS_REVIEW", ENGINE_PENDING: "NEEDS_REVIEW"}
NOTE_BY_KIND = {
    TIME_DEPENDENT: "날짜가 다른 값이 있어 어느 시점 값을 기준으로 할지 진료 기록 확인 필요",
    "contradictory": "서로 맞지 않는 값이 있어 의료진 확인 필요",
    "incomparable": "값을 서로 비교할 수 없어 의료진 확인 필요",
    "differing_biomarker": "내용이 다른 바이오마커 기록이 있어 의료진 확인 필요 (상반된 결과인지 별개 변이인지 미확인)",
    ENGINE_PENDING: "정책 엔진이 아직 확정하지 않은 항목이 있어 의료진 확인 필요",
}
_SEVERE_FIRST = ("contradictory", "incomparable", "differing_biomarker", ENGINE_PENDING, TIME_DEPENDENT)
_CONFIRMED = {"match": ("CRITERIA_MATCH", label("CRITERIA_MATCH")),
              "mismatch": ("CRITERIA_MISMATCH", label("CRITERIA_MISMATCH"))}
_TEXT_FIELDS = ("rationale", "key_matches", "key_concerns", "next_steps")
HELD_EXCLUSION = "engine_hold"  # 화면의 제외 종류: 확정 제외가 아니라 근거 확인 대기


@dataclass(frozen=True)
class Hold:
    trial_id: str
    side: Literal["match", "mismatch"]
    pair_status: str
    kinds: tuple[str, ...]
    rule_ids: tuple[str, ...]
    display_status: str

    def notes(self) -> list[str]:
        return [f"{', '.join(self.rule_ids)}: {NOTE_BY_KIND[k]}" for k in self.kinds]


@dataclass
class View:
    """사용자에게 내보낼 사본. holds가 비어 있으면 result·report는 받은 값 그대로 (engine_boundary만 더함)."""
    result: dict
    report: str | None
    holds: dict[str, Hold] = field(default_factory=dict)
    engine: dict[str, dict] = field(default_factory=dict)  # 시험 -> 판정·외부 상태·엔진 상태·남은 불일치 (코드만)


def _kind(c, conflicting: dict[str, str]) -> str:
    if not (c.gap == "conflict" or c.conflict_on_decided):
        return ENGINE_PENDING
    if c.category == "biomarker":
        return "differing_biomarker"
    return next((conflicting[f] for f in c.fields if f in conflicting), ENGINE_PENDING)


def _hold(tid: str, side: str, status: PairStatus, t, conflicting: dict[str, str]) -> Hold:
    open_c = [c for c in t.criteria if c.requirement == "required" and (c.gap != "none" or c.conflict_on_decided)]
    found = {_kind(c, conflicting) for c in open_c} or {ENGINE_PENDING}
    kinds = tuple(k for k in _SEVERE_FIRST if k in found)
    # 불충족 쪽은 최신 값이 탈락일 수 있으므로 '충족 가능'으로 말하지 않음
    shown = DISPLAY_BY_KIND[kinds[0]] if side == "match" else "NEEDS_REVIEW"
    return Hold(tid, side, status.value, kinds, tuple(sorted({c.rule_id for c in open_c})), shown)  # type: ignore[arg-type]


def _unresolved(m, status: PairStatus, t, held: bool) -> str | None:
    """표현을 낮추지 않았지만 엔진과 어긋나는 쌍의 사유 코드. 어느 정책 결정을 기다리는지 나타냄."""
    if held:
        return None
    if m.match_status == "CRITERIA_MATCH" and status != PairStatus.CONFIRMED_ELIGIBLE and required_count(t) == 0:
        return "no_required_criteria_policy_open"
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
        if t is not None and m.match_status == "CRITERIA_MATCH" and pair.status != PairStatus.CONFIRMED_ELIGIBLE \
                and required_count(t) > 0:
            hold = _hold(tid, "match", pair.status, t, state.conflicting_fields)
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


def _notice(holds: dict[str, Hold]) -> str:
    items = ", ".join(f"{t} ({label(h.display_status)})" for t, h in sorted(holds.items()))
    return (f"정책 엔진이 확정하지 않은 시험이 있습니다: {items}. 아래 설명과 다르면 이 표시가 우선하며, "
            f"이 표시는 판정을 바꾼 것이 아닙니다.")


def _held_item(item: dict, hold: Hold) -> dict:
    notes = hold.notes()
    return {**item, **{k: _relabel(item[k], hold) for k in _TEXT_FIELDS if k in item},
            "match_status": hold.display_status,
            "verification_needed": [*(item.get("verification_needed") or []), *notes],
            "next_steps": [*_relabel(item.get("next_steps") or [], hold), *notes]}


def _recommendation(rec: dict, session, holds: dict[str, Hold]) -> dict:
    held = {t: session.matches[t].model_copy(update={"match_status": h.display_status}) for t, h in holds.items()}
    index = {t: i for i, t in enumerate(session.matches)}

    def key(item: dict) -> tuple:
        t = item["trial_id"]
        return display_key(held.get(t, session.matches[t]), session.trials[t], index[t])
    ranked = sorted((_held_item(r, holds[r["trial_id"]]) if r.get("trial_id") in holds else r
                     for r in rec.get("ranked") or []), key=key)
    ranked = [{**r, "rank": pos} for pos, r in enumerate(ranked, start=1)]
    top = ranked[0]["match_status"] if ranked else None
    out = {**rec, "ranked": ranked, "recommended": [r for r in ranked if r["match_status"] == top],
           "selection_reason": f"추천 그룹: {LABEL_KO.get(top or '', '')}. {DISPLAY_BASIS}" if ranked else "",
           "overall_comment": f"{_notice(holds)} {rec.get('overall_comment') or ''}".strip()}
    outs = {t: h for t, h in holds.items() if h.side == "mismatch"}
    if outs:  # 확정 제외가 아니므로 제외 목록에서 뺌. 추천에도 넣지 않음
        out["excluded"] = [x for x in rec.get("excluded") or [] if x.get("trial_id") not in outs]
        out["held_out"] = [{"trial_id": t, "match_status": h.display_status, "reasons": h.notes()}
                           for t, h in sorted(outs.items())]
        if rec.get("no_recommendation_reason"):
            out["no_recommendation_reason"] = f"{rec['no_recommendation_reason']} {_notice(outs)}"
    return out


def _report(report: str | None, rec: dict | None, holds: dict[str, Hold], session) -> str | None:
    """코드가 쓴 줄과 보류 시험을 가리키는 줄만 고침. 시험을 가리키지 않는 LLM 문장은 고치지 못하므로 맨 위 안내가 우선함을 밝힘."""
    if not report:
        return report
    others = [t for t in session.matches if t not in holds]
    lines = []
    for line in report.split("\n"):
        mine = [holds[t] for t in holds if t in line]
        if mine and not any(t in line for t in others):
            for hold in mine:
                line = _relabel(line, hold)
        elif rec and line.startswith("## 추천 그룹:") and rec.get("recommended"):
            line = f"## 추천 그룹: {label(rec['recommended'][0]['match_status'])}"
        elif rec and line.startswith("추천 그룹:") and rec.get("selection_reason"):
            line = rec["selection_reason"]
        lines.append(line)
    return f"> **확인 필요:** {_notice(holds)}\n\n" + "\n".join(lines)


def build(session, result: dict | None = None, report: str | None = None) -> View:
    """사용자용 사본. result·report를 주지 않으면 Session의 값을 씀. 넘긴 값과 Session은 바꾸지 않음."""
    base = session.result() if result is None else result
    text = session.report if report is None else report
    holds, engine = evaluate(session)
    out = {**base, "engine_boundary": engine}
    if not holds:
        return View(out, text, holds, engine)
    out["matches"] = {t: {**m, "match_status": holds[t].display_status,
                          "summary": _relabel(m.get("summary"), holds[t])} if t in holds else m
                      for t, m in (base.get("matches") or {}).items()}
    rec = _recommendation(base["recommendation"], session, holds) if base.get("recommendation") else None
    if rec is not None:
        out["recommendation"] = rec
    return View(out, _report(text, rec, holds, session), holds, engine)


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
    """Session.save 뒤에 호출. result.json의 기존 값은 그대로 두고(평가·감사 입력) engine_boundary만 더함.

    보류가 있을 때만 사용자가 읽는 report.md를 보수적 사본으로 바꾸고 원본은 report.raw.md에, 표현 사본은 conservative_view.json에 둠."""
    view = build(session)
    path = out_dir / "result.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**raw, "engine_boundary": view.engine}, ensure_ascii=False, indent=2), encoding="utf-8")
    if not view.holds:
        return view
    (out_dir / "conservative_view.json").write_text(json.dumps({
        "note": "표현 사본입니다. 판정·사실은 result.json과 같고 외부 상태·추천 그룹·문구만 다릅니다.",
        "holds": [asdict(h) for h in view.holds.values()], "recommendation": view.result.get("recommendation"),
        "matches": {t: {"eligibility": m.get("eligibility"), "match_status": m.get("match_status")}
                    for t, m in (view.result.get("matches") or {}).items()},
        "engine_boundary": view.engine}, ensure_ascii=False, indent=2), encoding="utf-8")
    if view.report != session.report:
        (out_dir / "report.raw.md").write_text(session.report, encoding="utf-8")
        (out_dir / "report.md").write_text(view.report, encoding="utf-8")
    return view


__all__ = ["Hold", "View", "HELD_EXCLUSION", "evaluate", "build", "adjust_display", "save"]
