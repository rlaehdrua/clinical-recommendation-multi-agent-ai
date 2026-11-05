"""파싱된 시험(data/parsed)에서 슬롯 사전 초안을 만들고, LLM 초안 적용·사람 검수 결과 반영을 처리합니다.

- 구조화 기준(structured_evaluable, field = age | sex | ecog | lab:*)은 슬롯과 진위 함수(engine)를 자동으로 연결하고,
  규칙 엔진이 실제로 판정할 수 있는지 시험 값으로 확인(probe)한 뒤에만 auto로 둡니다.
  판정하지 못하면(단위 불일치, 비표준 단위, 나이 구간 경계 등) needs_review로 남깁니다.
- 나머지 기준(진단·병력·약물·동반질환 등)은 needs_review. 사람이 slot·truth(categorical)·values를 채우고
  status를 reviewed로 바꿔야 정답 계산에 들어갑니다. 이 파일은 LLM을 부르지 않습니다.
- merge: 기존 사전의 reviewed·excluded·draft 연결과 사람이 추가한 슬롯은 그대로 보존합니다.
- apply_draft: LLM 초안을 status=draft로 넣음 (정답 계산에 쓰이지 않음). apply_review: 검수 시트의 '확인'만 reviewed로 올림.
"""

from __future__ import annotations

from itertools import product

from ..evidence import required_evidence
from ..schemas import ParsedTrial, Rule
from .spec import CORE_SOURCE, LAB_AGE, Binding, Cond, SlotDictionary, SlotSpec
from .truth import LAB_PREFIX, IndeterminateTruth, rule_holds, slot_for_field

PROBE_DATE = "2026-01-01"
PROBE = {"age": [1, 5, 17, 18, 19, 30, 50, 64, 65, 75, 80, 90], "sex": ["male", "female"], "ecog": [0, 1, 2, 3, 4]}
# 엔진이 판정을 포기하는 조건(단위·연산자·기준값)은 값의 크기와 무관. 여러 자릿수를 넣는 것은 '항상 같은 결과' 확인용
LAB_PROBE = [0.1, 1.0, 10.0, 100.0, 1000.0, 100000.0]
LAB_AGE_SPEC = SlotSpec(slot_id=LAB_AGE, kind="numeric", category="meta", source=CORE_SOURCE, unit="days")
PRESERVE = ("reviewed", "excluded", "draft")


def _slot_spec(slot_id: str, unit: str | None) -> SlotSpec:
    if slot_id in ("age", "sex"):
        return SlotSpec(slot_id=slot_id, kind="categorical" if slot_id == "sex" else "numeric", category="demographic",
                        source=CORE_SOURCE, values=["male", "female"] if slot_id == "sex" else [])
    if slot_id == "ecog":
        return SlotSpec(slot_id="ecog", kind="numeric", category="performance_status", source="S_chart")
    return SlotSpec(slot_id=slot_id, kind="numeric", category="lab", source="S_lab", unit=unit)


def _engine_binding(tid: str, rule: Rule, slot_id: str) -> Binding:
    window = rule.time_window_days if slot_id.startswith(LAB_PREFIX) else None
    return Binding(trial_id=tid, rule_id=rule.rule_id, kind=rule.kind, category=rule.category, text=rule.text[:200],
                   status="auto", slot=slot_id, truth="engine", time_window_days=window,
                   required_evidence=required_evidence(rule)[0])


def _probe_failures(b: Binding, rule: Rule, spec: SlotSpec) -> list[str]:
    fails = []
    for v in PROBE.get(spec.slot_id, LAB_PROBE):
        try:
            rule_holds(b, rule, {spec.slot_id: v}, {spec.slot_id: spec}, PROBE_DATE)
        except IndeterminateTruth:
            fails.append(repr(v))
    return fails


def _lab_unit(slot_id: str, rules: list[tuple[str, Rule]]) -> str | None:
    """슬롯 단위: 그 단위로 두었을 때 엔진이 판정할 수 있는 첫 기준의 단위."""
    for tid, rule in rules:
        spec = _slot_spec(slot_id, rule.unit)
        if not _probe_failures(_engine_binding(tid, rule, slot_id), rule, spec):
            return rule.unit
    return None


def draft(parsed: dict[str, ParsedTrial], existing: SlotDictionary | None = None) -> SlotDictionary:
    structured: dict[str, list[tuple[str, Rule]]] = {}
    for tid, p in parsed.items():
        for r in p.rules:
            sid = slot_for_field(r.field) if r.structured_evaluable and r.operator else None
            if sid:
                structured.setdefault(sid, []).append((tid, r))

    slots: dict[str, SlotSpec] = {}
    for sid, rules in structured.items():
        unit = _lab_unit(sid, rules) if sid.startswith(LAB_PREFIX) else None
        if sid.startswith(LAB_PREFIX) and unit is None and not any(r.unit is None for _, r in rules):
            continue  # 어떤 단위로도 판정할 수 없는 검사 -> 슬롯을 만들지 않고 기준은 needs_review
        slots[sid] = _slot_spec(sid, unit)

    kept = {b.key: b for b in (existing.bindings if existing else []) if b.status in PRESERVE}
    bindings: list[Binding] = []
    for tid, p in parsed.items():
        for r in p.rules:
            key = f"{tid}:{r.rule_id}"
            if key in kept:
                bindings.append(kept[key])
                continue
            sid = slot_for_field(r.field) if r.structured_evaluable and r.operator else None
            base = dict(trial_id=tid, rule_id=r.rule_id, kind=r.kind, category=r.category, text=r.text[:200],
                        required_evidence=required_evidence(r)[0])
            if r.cohort:
                bindings.append(Binding(**base, status="needs_review", note=f"코호트 전용 기준({r.cohort})"))
            elif sid in slots:
                b = _engine_binding(tid, r, sid)
                fails = _probe_failures(b, r, slots[sid])
                if fails:
                    b = Binding(**base, status="needs_review", slot=sid,
                                note=f"규칙 엔진이 판정하지 못하는 값: {', '.join(fails)} (단위 {r.unit!r}, 슬롯 단위 {slots[sid].unit!r})")
                bindings.append(b)
            elif sid:
                bindings.append(Binding(**base, status="needs_review", slot=sid, note=f"슬롯 단위를 정할 수 없음 (단위 {r.unit!r})"))
            else:
                bindings.append(Binding(**base, status="needs_review", note="비구조화 기준: 슬롯·진위 함수 검수 필요"))

    if existing:
        for sid, spec in existing.slots.items():
            slots.setdefault(sid, spec)  # 사람이 추가한 범주형 슬롯 보존
    if any(b.time_window_days is not None for b in bindings):
        slots[LAB_AGE] = LAB_AGE_SPEC
    return SlotDictionary(built_from=sorted(parsed), slots=dict(sorted(slots.items())), bindings=bindings)


def summary(d: SlotDictionary) -> dict:
    out: dict = {"slots": len(d.slots), "bindings": len(d.bindings), "by_status": {}, "by_trial": {}}
    for b in d.bindings:
        out["by_status"][b.status] = out["by_status"].get(b.status, 0) + 1
        t = out["by_trial"].setdefault(b.trial_id, {"usable": 0, "total": 0})
        t["total"] += 1
        t["usable"] += b.status in ("auto", "reviewed")
    return out


# ---------------------------------------------------------------------------
# LLM 초안 적용과 사람 검수 반영
# ---------------------------------------------------------------------------

def check_binding(b: Binding, rule: Rule | None, slots: dict[str, SlotSpec]) -> list[str]:
    """검수 전 연결이 reviewed가 되었을 때 정답을 계산할 수 있는지 미리 확인 (관련 슬롯 값의 모든 조합으로 시험 판정)."""
    involved = b.slots_used()
    missing = [s for s in involved if s not in slots]
    if not involved or missing:
        return [f"슬롯 {missing or b.slot!r}이 사전에 없음"]
    conds = ([Cond(slot=b.slot, op=b.op, values=b.values)] if b.truth == "categorical" else []) + b.also
    for c in conds:
        bad = set(c.values) - set(slots[c.slot].values)
        if slots[c.slot].kind != "categorical" or bad:
            return [f"슬롯 {c.slot}의 값 목록에 없는 값 {sorted(bad)}"]
    if b.truth == "engine" and rule is None:
        return ["원본 기준 없음"]
    domains = [slots[s].values if slots[s].kind == "categorical" else PROBE.get(s, LAB_PROBE) for s in involved]
    outcomes, fails = set(), []
    for combo in product(*domains):
        try:
            outcomes.add(rule_holds(b, rule, dict(zip(involved, combo)), slots, PROBE_DATE))
        except IndeterminateTruth:
            fails.append(repr(combo[0]))
    if fails:
        return [f"규칙 엔진이 판정하지 못하는 값: {', '.join(sorted(set(fails)))}"]
    if outcomes != {"yes", "no"}:
        return [f"슬롯 값과 관계없이 항상 {outcomes.pop()}"]
    return []


def apply_draft(d: SlotDictionary, draft_doc: dict, parsed: dict[str, ParsedTrial]) -> tuple[SlotDictionary, dict]:
    """LLM 초안을 status=draft로 넣음. auto·reviewed·excluded 연결은 바꾸지 않음. 반환: (새 사전, 적용 보고)."""
    slots = dict(d.slots)
    for sid, raw in draft_doc.get("slots", {}).items():
        spec = SlotSpec(slot_id=sid, **raw)
        if sid in slots and slots[sid] != spec:
            raise ValueError(f"슬롯 {sid}: 기존 정의와 다름 (기존을 고치려면 사전에서 직접 수정)")
        slots[sid] = spec
    if any(x.get("time_window_days") is not None for x in draft_doc.get("bindings", [])):
        slots[LAB_AGE] = LAB_AGE_SPEC
    rules = {f"{t}:{r.rule_id}": r for t, p in parsed.items() for r in p.rules}
    proposals = {f"{x['trial_id']}:{x['rule_id']}": x for x in draft_doc.get("bindings", [])}
    out, report = [], {"applied": [], "skipped": [], "problems": {}, "not_in_dictionary": []}
    known = {b.key for b in d.bindings}
    report["not_in_dictionary"] = sorted(set(proposals) - known)
    for b in d.bindings:
        x = proposals.get(b.key)
        if x is None:
            out.append(b)
            continue
        if b.status not in ("needs_review", "draft"):
            report["skipped"].append(f"{b.key} ({b.status})")
            out.append(b)
            continue
        nb = b.model_copy(update=dict(status="draft", slot=x["slot"], truth=x["truth"], op=x.get("op"),
                                      values=x.get("values", []), rule_override=x.get("rule_override"),
                                      also=[Cond(**c) for c in x.get("also", [])], combine=x.get("combine", "all"),
                                      time_window_days=x.get("time_window_days"),
                                      note=f"LLM 초안: {x.get('note', '')}"))
        nb = Binding(**nb.model_dump())  # 검증기 다시 실행 (rule_override 키 등)
        problems = check_binding(nb, rules.get(b.key), slots)
        if problems:
            report["problems"][b.key] = problems
        report["applied"].append(b.key)
        out.append(nb)
    return SlotDictionary(built_from=d.built_from, slots=dict(sorted(slots.items())), bindings=out), report


def _cond_text(c: Cond) -> str:
    return f"{c.slot} 값이 [{', '.join(c.values)}] {'중 하나' if c.op == 'in' else '가 아님'}"


def rule_summary(b: Binding) -> str:
    if b.truth == "categorical":
        main = _cond_text(Cond(slot=b.slot, op=b.op, values=b.values))
    elif b.truth == "engine":
        main = f"규칙 엔진으로 {b.slot} 수치 비교" + (f" (원문을 바꿔 읽음: {b.rule_override})" if b.rule_override else "")
        if b.time_window_days is not None:
            main += f", 검사가 {b.time_window_days}일 이내일 때만 판정"
    else:
        return ""
    if b.also:
        joiner = " 그리고 " if b.combine == "all" else " 또는 "
        main = joiner.join([main, *(_cond_text(c) for c in b.also)])
    return main + "이면 기준 성립"


REVIEW_HEADER = ["trial_id", "rule_id", "구분", "원문", "번역", "상태", "제안 슬롯", "판정 규칙", "초안 메모",
                 "검수 결과(확인/수정/제외)", "수정 내용", "검수자", "검수일"]


def review_rows(d: SlotDictionary, parsed: dict[str, ParsedTrial], ko: dict | None = None) -> list[list[str]]:
    ko = ko or {}
    texts = {f"{t}:{r.rule_id}": r.text for t, p in parsed.items() for r in p.rules}
    rows = []
    for b in d.bindings:
        if b.status not in ("draft", "needs_review"):
            continue
        tr = ((ko.get(b.trial_id) or {}).get("rules") or {}).get(b.rule_id) or {}
        rows.append([b.trial_id, b.rule_id, "선정" if b.kind == "inclusion" else "제외", texts.get(b.key, b.text),
                     tr.get("ko", "") if isinstance(tr, dict) else "", b.status, b.slot or "", rule_summary(b),
                     b.note or "", "", "", "", ""])
    return rows


def apply_review(d: SlotDictionary, rows: list[dict], parsed: dict[str, ParsedTrial]) -> tuple[SlotDictionary, dict]:
    """검수 시트 반영: '확인' -> reviewed (판정 시험을 통과할 때만), '제외' -> excluded, '수정'·빈칸 -> 그대로."""
    decide = {f"{r['trial_id']}:{r['rule_id']}": r for r in rows}
    rules = {f"{t}:{r.rule_id}": r for t, p in parsed.items() for r in p.rules}
    report = {"reviewed": [], "excluded": [], "unchanged": [], "rejected": {}}
    out = []
    for b in d.bindings:
        r = decide.get(b.key)
        verdict = (r or {}).get("검수 결과(확인/수정/제외)", "").strip()
        who = f"{(r or {}).get('검수자', '').strip()} {(r or {}).get('검수일', '').strip()}".strip()
        if b.status not in ("draft", "needs_review") or verdict not in ("확인", "제외"):
            if r is not None:
                report["unchanged"].append(b.key)
            out.append(b)
            continue
        if verdict == "제외":
            out.append(b.model_copy(update={"status": "excluded", "note": f"{b.note or ''} | 검수 제외 {who}".strip(" |")}))
            report["excluded"].append(b.key)
            continue
        problems = check_binding(b, rules.get(b.key), d.slots) if b.slot and b.truth else ["슬롯·진위 함수가 비어 있음"]
        if problems:
            report["rejected"][b.key] = problems
            out.append(b)
            continue
        memo = (r or {}).get("수정 내용", "").strip()
        note = f"{b.note or ''} | 검수 확인 {who}" + (f" | 검수 메모: {memo}" if memo else "")
        out.append(Binding(**{**b.model_dump(), "status": "reviewed", "note": note.strip(" |")}))
        report["reviewed"].append(b.key)
    return SlotDictionary(built_from=d.built_from, slots=d.slots, bindings=out), report
