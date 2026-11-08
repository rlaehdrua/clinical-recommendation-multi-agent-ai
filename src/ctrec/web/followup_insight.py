"""연구 코디네이터 작업 시간 보기 (담당자 화면 '질문별'): 어떤 확인을 누구에게 먼저 하면 적은 시간에 많은 판정이 정해지는가.

열린 사례 전체를 한 코호트로 보고(staff_queue와 같은 Cohort C 계획), 확인 하나(같은 확인 경로 + 같은 항목 info_key)마다
- 적용 환자: 그 확인이 지금 제안된 환자
- 판정이 정해질 수 있는 쌍·환자: 그 쌍(환자 × 시험)에 남은 필수 확인이 **이 항목뿐**인 경우 (답을 받으면 판정이 정해질 수 있음).
  환자 질문은 진료 기록이 필요한 기준(required_evidence=clinical_record)을 정하지 못하므로 그런 쌍은 세지 않음
- 예상 코디네이터 분: 비용 원장(EffortLedger) 그대로 - 같은 환자·같은 정보원 연락은 한 번만 청구되므로 계획 순서의 추가 비용
그리고 계획 전체를 추천 순서(C)대로 할 때와 환자 순서대로 할 때, 같은 수의 쌍이 정해지기까지 걸리는 분을 비교합니다.
- 따로 요청할 때의 분(minutes_alone): 같은 확인을 다른 확인과 묶지 않고 하나만 요청할 때의 원장 비용. 계획의 추가 비용과의 차이가
  같은 환자·같은 정보원(예: 검사 결과지 한 장)으로 묶어서 줄어든 연락 비용입니다 (DCV 번들, 같은 가정 비용)
- 보낸 요청의 분(spent_minutes): 지금까지 보낸 요청을 같은 원장으로 청구한 값 (실제 측정 시간이 아님)

해석: 판정 '가능' 수는 답이 실제로 나온다고 볼 때의 상한이며(모름·연락 불가·상충이면 정해지지 않음), 분은 엔진 카탈로그의
가정 비용입니다. 판정 규칙은 여기 없고 decision_state·정책 엔진 결과를 읽기만 합니다.
"""

from __future__ import annotations

from ..decision_state import SessionView, build_state
from ..engine.cohort_state import OBTAINABLE_GAPS, CohortState
from ..engine.cost import EffortLedger
from ..followup import codec
from ..engine.contracts import Action
from ..followup.models import OPEN_STATUSES
from ..followup.service import FOLLOWUP_BUDGET, FollowupService

UNDECIDED = frozenset({"INFO_MISSING", "VERIFICATION_PENDING", "BLOCKED_FAILURE"})


def _open_criteria(trial) -> list:
    return [c for c in trial.criteria if c.requirement == "required" and (c.gap != "none" or c.conflict_on_decided)]


def _settles(c, by_ask: set[str], by_doc: set[str]) -> bool:
    """이 기준의 남은 확인이 끝나는가 (환자 답변은 진료 기록 필요 기준을 끝내지 못함)."""
    if c.conflict_on_decided or c.gap not in OBTAINABLE_GAPS:
        return False
    if c.info_key in by_doc:
        return True
    return c.info_key in by_ask and c.required_evidence != "clinical_record" and c.gap != "unverified"


def _decided_pairs(states: dict, alias: str, by_ask: set[str], by_doc: set[str]) -> set[str]:
    out = set()
    for tid, t in states[alias]["trials"].items():
        crit = _open_criteria(t)
        if crit and states[alias]["status"].get(tid) in UNDECIDED and all(_settles(c, by_ask, by_doc) for c in crit):
            out.add(tid)
    return out


def _impact(states: dict, alias: str, route: str, key: str) -> dict:
    """이 항목(info_key) 확인이 이 환자의 쌍에 주는 영향 (항목 단위, 확인 경로와 무관하게 그 항목이 끝났다고 볼 때).
    - related: 이 항목이 남은 필수 확인 중 하나인 쌍
    - now: 이 항목이 마지막 남은 항목인 쌍 (끝나면 판정이 정해질 수 있음)
    - next: 이 항목 뒤 남는 항목이 하나인 쌍
    - record_after: 환자 질문인데 진료 기록이 필요한 기준이라, 답변 뒤 서류 확인까지 해야 끝나는 쌍"""
    out = {"related": [], "now": [], "next": [], "record_after": []}
    for tid, t in states[alias]["trials"].items():
        crit = _open_criteria(t)
        if not crit or states[alias]["status"].get(tid) not in UNDECIDED:
            continue
        mine = [c for c in crit if c.info_key == key and not c.conflict_on_decided and c.gap in OBTAINABLE_GAPS]
        if not mine:
            continue
        out["related"].append(tid)
        if route == "ask_patient" and any(c.required_evidence == "clinical_record" for c in mine):
            out["record_after"].append(tid)
        rest = [c for c in crit if c.info_key != key]
        if any(c.conflict_on_decided or c.gap not in OBTAINABLE_GAPS for c in rest):
            continue  # 상충·임상의 판단이 남으면 정보를 더 모아도 정해지지 않음
        left = {c.info_key for c in rest}
        if not left:
            out["now"].append(tid)
        elif len(left) == 1:
            out["next"].append(tid)
    return out


def _curve(items: list[dict], states: dict, catalog, pre_charged: list) -> list[dict]:
    """순서대로 확인할 때의 누적 (분, 판정이 정해질 수 있는 쌍 수). 비용은 그 순서의 원장 추가 비용."""
    ledger = EffortLedger(FOLLOWUP_BUDGET, catalog)  # 대기열·제안과 같은 예산 (임상 검토 포함)
    for a in pre_charged:
        ledger.charge(a)
    asked: dict[str, tuple[set, set]] = {}
    minutes, out = 0.0, []
    for x in items:
        cost, _ = ledger.charge(x["action"])
        minutes += cost.coordinator_minutes
        ask, doc = asked.setdefault(x["alias"], (set(), set()))
        (ask if x["route"] == "ask_patient" else doc).add(x["info_key"])
        decided = sum(len(_decided_pairs(states, a, *asked[a])) for a in asked)
        out.append({"minutes": round(minutes, 1), "decided_pairs": decided})
    return out


def _alone(action, catalog) -> float:
    """다른 확인과 묶지 않고 이것 하나만 요청할 때의 코디네이터 분 (새 원장)."""
    cost, _ = EffortLedger(FOLLOWUP_BUDGET, catalog).charge(action)
    return cost.coordinator_minutes


def _spent(requested: list[dict], catalog) -> float:
    ledger, total = EffortLedger(FOLLOWUP_BUDGET, catalog), 0.0
    for r in requested:
        if r.get("action") and r.get("created_by") != "system":
            cost, _ = ledger.charge(Action.model_validate(r["action"]))
            total += cost.coordinator_minutes
    return round(total, 1)


def _minutes_to(curve: list[dict], n: int) -> float | None:
    return next((p["minutes"] for p in curve if p["decided_pairs"] >= n), None)


def insight(svc: FollowupService) -> dict:
    states, ops, case_of, requested = {}, {}, {}, []
    cases, unreadable = svc.readable_cases()  # 감사 F-17
    for data in cases:
        data = svc.refresh_pairs(data)  # 사례 화면과 같은 지금 권한의 쌍 상태·제안 (저장값이 아니라)
        session = svc._restore(data)  # staff_queue와 같은 복원 (판정을 다시 만들지 않음)
        with svc._scope(data, session):  # staff_queue와 같이 사례마다 그 사례의 문서·임상의 권한으로
            view = svc._view(session)
            view.matched_versions = {t: view.snapshot.version for t in session.matches}
            ds = build_state(view)
        alias = data["alias"]
        states[alias] = {"ds": ds, "trials": ds.trials,
                         "status": {t: p["status"] for t, p in data["current"]["pairs"].items()}}
        ops[alias] = codec.load_ops(data["ops"])
        case_of[alias] = data
        requested += data["requests"]  # staff_queue와 같은 요청 기록 (공통 엔진 루프가 청구·반복 억제)
    if not states:
        return {"questions": [], "summary": {"cases": 0}, "unreadable": unreadable}
    co = CohortState.from_decision_states({a: s["ds"] for a, s in states.items()}, ops=ops)
    pol = svc.policy_factory()
    plan = svc._plan(co, requested)
    created = sorted(case_of, key=lambda a: (case_of[a]["created_at"], case_of[a]["case_id"]))

    items = []
    for rank, a in enumerate(plan.selected, 1):
        route = svc._route(a)
        if route is None or not a.info_key:
            continue
        data = case_of[a.patient_alias]
        prop = next((p for p in data["current"]["proposals"] if p["proposal_id"] == a.action_id), None)
        req = next((r for r in data["requests"] if r.get("proposal_id") == a.action_id), None)
        items.append({"rank": rank, "action": a, "alias": a.patient_alias, "case_id": data["case_id"], "route": route[0],
                      "source_id": a.source_id,
                      "info_key": a.info_key, "action_type": a.action_type, "proposal_id": a.action_id,
                      "requestable": bool(prop and prop.get("requestable")),
                      "preview": prop.get("patient_question_preview") if prop else None,
                      "request_id": req["request_id"] if req else None, "request_status": req["status"] if req else None})
    in_flight = [Action.model_validate(r["action"]) for r in requested
                 if r["status"] in OPEN_STATUSES and r.get("action")]
    c_curve = _curve(items, states, pol.catalog, in_flight)
    p_items = sorted(items, key=lambda x: (created.index(x["alias"]), x["rank"]))
    p_curve = _curve(p_items, states, pol.catalog, in_flight)
    for x, pt, prev in zip(items, c_curve, [{"minutes": 0.0, "decided_pairs": 0}] + c_curve[:-1]):
        x["minutes"] = round(pt["minutes"] - prev["minutes"], 1)
        x["newly_decided"] = pt["decided_pairs"] - prev["decided_pairs"]  # 추천 순서대로 할 때 이 확인에서 정해지는 쌍
        x["minutes_alone"] = round(_alone(x["action"], pol.catalog), 1)

    groups: dict[tuple, dict] = {}
    for x in items:
        g = groups.setdefault((x["route"], x["info_key"]), {
            "route": x["route"], "info_key": x["info_key"], "action_type": x["action_type"], "best_rank": x["rank"],
            "source_id": x["source_id"],
            "preview": None, "patients": []})
        g["preview"] = g["preview"] or x["preview"]
        imp = _impact(states, x["alias"], x["route"], x["info_key"])
        g["patients"].append({"case_id": x["case_id"], "proposal_id": x["proposal_id"], "rank": x["rank"],
                              "minutes": x["minutes"], "minutes_alone": x["minutes_alone"], "related_trials": imp["related"], "decided_trials": imp["now"],
                              "next_trials": imp["next"], "record_after_trials": imp["record_after"], "newly_decided_in_order": x["newly_decided"],
                              "requestable": x["requestable"], "request_id": x["request_id"],
                              "request_status": x["request_status"]})
    out = []
    for g in groups.values():
        ps = g["patients"]
        todo = [p for p in ps if not p["request_id"]]
        g.update(n_patients=len(ps), n_related_pairs=sum(len(p["related_trials"]) for p in ps),
                 n_decided_patients=sum(1 for p in ps if p["decided_trials"]),
                 n_decided_pairs=sum(len(p["decided_trials"]) for p in ps),
                 n_next_pairs=sum(len(p["next_trials"]) for p in ps),
                 n_record_after_pairs=sum(len(p["record_after_trials"]) for p in ps),
                 n_newly_decided_in_order=sum(p["newly_decided_in_order"] for p in ps),
                 minutes=round(sum(p["minutes"] for p in ps), 1),
                 minutes_alone=round(sum(p["minutes_alone"] for p in ps), 1),
                 n_open=len(todo), n_sendable=sum(1 for p in todo if p["requestable"]))
        out.append(g)
    out.sort(key=lambda g: g["best_rank"])
    total = c_curve[-1]["decided_pairs"] if c_curve else 0
    marks = sorted({max(1, total // 2), total} - {0})
    return {"questions": out, "unreadable": unreadable, "summary": {
        "cases": len(states), "items": len(items),
        "minutes_all": c_curve[-1]["minutes"] if c_curve else 0.0, "decidable_pairs": total,
        "minutes_all_alone": round(sum(x["minutes_alone"] for x in items), 1),
        "spent_minutes": _spent(requested, pol.catalog),
        "milestones": [{"pairs": n, "c_order_minutes": _minutes_to(c_curve, n), "patient_order_minutes": _minutes_to(p_curve, n)}
                       for n in marks],
        "c_curve": c_curve, "patient_order_curve": p_curve,
        "note": "판정 가능 수는 답이 나온다고 볼 때의 상한, 분은 엔진 카탈로그의 가정 비용"}}


__all__ = ["insight"]
