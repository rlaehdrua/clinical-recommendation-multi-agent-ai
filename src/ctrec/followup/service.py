"""환자 -> 연구 담당자 -> 환자 -> 재평가 흐름의 서비스 계층 (TRIALOG O). 웹·권한과 무관한 순수 흐름.

흐름
1. create_case       : 환자 초기 입력 -> 실제 Session(profile_patient -> match -> finalize). 사례 기록 저장.
2. 평가 기록         : 쌍 상태(engine PairStatus), 부족 정보(gap), DCV+C 제안(FullDCVCPolicy propose -> select 순서)
3. create_requests   : 연구 담당자가 현재 제안 중에서 골라 요청을 만듦 (환자 질문 / 문서 검토 / 충돌 확인 / 임상 검토).
                       같은 제안의 열린 요청이 있으면 새로 만들지 않음. '모름'으로 끝난 항목의 환자 질문은 다시 만들지 않음.
4. patient_requests  : 환자가 볼 수 있는 질문 (환자 질문 종류만, 담당자 내부 작업은 보이지 않음)
5. respond           : 환자 답변 또는 '모름'. 같은 답의 재전송은 중복으로 처리하고 다시 평가하지 않음.
   - 답 -> QAPair(질문 = 요청, 출처 = 이 요청) -> 원문 뒤 답변 블록 (Session.clarify와 같은 형식) -> 사실 재추출
     (사실 저장소는 확인 답변에서만 확인된 사실을 self_report로, 답변 인용을 출처로 기록)
   - 증분 재평가: 바뀐 사실 -> 의존 그래프 영향 분석(dependency_graph) -> 영향받은 시험만 다시 매칭, 나머지는 재사용
   - 전체 재평가 대조: 같은 입력(재추출 직후 상태)의 복원본에서 모든 시험을 다시 매칭해 판정·쌍 상태를 비교.
     다르면 전체 결과를 쓰고 기록에 남김 (증분 결과가 조용히 쓰이지 않음)
   - '모름'은 운영 상태(OpsState)의 항목 실패로 기록 -> 정책 엔진이 낸 대체 경로(문서 검토, 없으면 임상 검토)를 새 요청으로
6. 충돌: 사실 저장소가 같은 항목의 불일치(contradictory/time_dependent/incomparable)를 표시하면 충돌 확인 요청을 만듦.
   어느 값도 채택하지 않음. 엔진 쌍 상태는 확인 대기(VERIFICATION_PENDING)로 보류됨.
7. 매 평가 뒤: 대상 기준이 더는 열려 있지 않은 요청은 superseded로 닫음 (환자에게 지난 질문을 보이지 않음).

지키는 것
- 확정 적격은 판정 계층(conservative_view·engine)만 정함. 이 모듈은 판정 값을 만들거나 고치지 않음.
- 담당 의료진의 판단 완료, 문서 확인 완료 상태를 만들지 않음 (그런 입력 계약이 없음).
- 연구 담당자가 환자 답을 대신 입력하는 경로가 없음 (respond는 웹 계층에서 환자 권한으로만 부름).
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from ..agents import matcher
from ..contract_session import session_class
from ..decision_state import SessionView, build_state
from ..dependency_graph import DependencyGraph, analyze_impact, resolve
from ..engine.answers import answer_outcome
from ..engine.cohort_state import CohortState, OpsState
from ..engine.contracts import UNREACHABLE, Action, ActionOutcome, PairStatus
from ..engine.cost import EffortLedger
from ..engine.dcvc import FullDCVCPolicy
from ..engine.question_bridge import qa_pair
from ..fact_changes import detect_changes, take_snapshot
from ..pipeline import PatientCase
from ..schemas import ClarifyingQuestion, ParsedTrial
from ..trace import Tracer
from ..web.engine_runner import DEFAULT_BUDGET, EngineRunner
from . import codec
from .models import (ACTION_ROUTES, FORMAT, OPEN_STATUSES, PATIENT_KINDS, STAFF_TRANSITIONS, EvaluationRecord,
                     FollowupRequest, FollowupResponse, PatientQuestion, StatusEvent)
from .store import CaseStore, new_case_id

CLOSED_PAIRS = frozenset({PairStatus.CONFIRMED_ELIGIBLE.value, PairStatus.CONFIRMED_INELIGIBLE.value,
                          PairStatus.NOT_EVALUATED.value})
DECLINED = "모름"
INPUT_SOURCES = ("self_report", "clinical_record", "unknown")
FALLBACK_TYPES = ("REQUEST_DOCUMENT", "VERIFY_RECORD", "CLINICIAN_REVIEW")


class FollowupError(Exception):
    """웹 계층이 HTTP 상태로 옮기는 흐름 오류. code는 화면이 분기할 고정 값."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


@dataclass
class TrialCatalog:
    """평가할 수 있는 시험. parsed가 있으면 그 기준표를 쓰고, 없으면 고정 저장소(trial_store)에서 읽음."""
    name: str
    trials: dict[str, dict]
    parsed: dict[str, ParsedTrial] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def answer_digest(answer: str) -> str:
    return hashlib.sha256(" ".join((answer or "").split()).casefold().encode()).hexdigest()


def _criteria(ds) -> list:
    return [c for ts in ds.trials.values() for c in ts.criteria]


def _match_signature(m) -> tuple:
    return (m.eligibility, m.match_status, m.matched_cohort,
            tuple(sorted((a.rule_id, a.passes, a.holds, a.method, a.needs_verification, a.applicable)
                         for a in m.assessments)))


def _criteria_snapshot(session) -> dict[str, dict]:
    return {f"{tid}:{a.rule_id}": {"passes": a.passes, "holds": a.holds, "method": a.method,
                                    "needs_verification": a.needs_verification}
            for tid, m in session.matches.items() for a in m.assessments}


class FollowupService:
    def __init__(self, store: CaseStore, catalog: TrialCatalog, *, full_check: bool = True,
                 llm_scope: Callable[[str], contextlib.AbstractContextManager] | None = None,
                 policy_factory: Callable[[], FullDCVCPolicy] = FullDCVCPolicy):
        self.store = store
        self.catalog = catalog
        self.full_check = full_check
        self.llm_scope = llm_scope or (lambda label: contextlib.nullcontext())
        self.policy_factory = policy_factory

    # ------------------------------------------------------------------ Session
    def _new_session(self, case: PatientCase, trial_ids: list[str]):
        s = session_class()(case=case, trials={t: self.catalog.trials[t] for t in trial_ids},
                            answerer=codec.NoAnswerer(), tracer=Tracer(None, verbose=False))
        s.parsed.update({t: p for t, p in self.catalog.parsed.items() if t in s.trials})
        return s

    def _restore(self, data: dict):
        return codec.restore_session(data["session"], self.catalog.trials, self.catalog.parsed)

    def _cohort(self, session, alias: str, ops: OpsState):
        view = SessionView.from_session(session)
        view.matched_versions = {t: view.snapshot.version for t in session.matches}
        ds = build_state(view)
        return ds, CohortState.from_decision_states({alias: ds}, ops={alias: ops})

    @staticmethod
    def _pairs(co: CohortState) -> dict[str, dict]:
        return {p.pair.trial_id: {"status": p.status.value, "eligibility": p.eligibility, "open": p.open,
                                  "gaps": sorted(p.gaps), "pending_kinds": sorted(p.pending_kinds),
                                  "mismatch": p.mismatch} for p in co.pairs()}

    # ------------------------------------------------------------------ 제안 (DCV+C)
    def _plan(self, co: CohortState, open_actions: list[Action]):
        pol = self.policy_factory()
        ledger = EffortLedger(DEFAULT_BUDGET, pol.catalog)
        for a in open_actions:  # 이미 요청한 (환자, 정보원)은 추가 비용이 줄어 C 점수에 묶음으로 반영
            ledger.charge(a)
        return pol.select(pol.propose(co), DEFAULT_BUDGET, ledger, co)

    @staticmethod
    def _open_actions(data: dict) -> list[Action]:
        return [Action.model_validate(r["action"]) for r in data["requests"]
                if r["status"] in OPEN_STATUSES and r.get("action")]

    def _proposals(self, data: dict, session, co: CohortState) -> tuple[list[dict], list[dict]]:
        plan = self._plan(co, self._open_actions(data))
        by_proposal = {r["proposal_id"]: r for r in data["requests"] if r.get("proposal_id")}
        runner = EngineRunner(session)
        out = []
        for rank, a in enumerate(plan.selected, 1):
            route = self._route(a)
            req = by_proposal.get(a.action_id)
            preview = None
            if route and route[0] == "ask_patient":
                q = runner.render(a, "preview")
                preview = q.question if q else None
            out.append({"proposal_id": a.action_id, "rank": rank, "action_type": a.action_type,
                        "route": route[0] if route else None, "source_id": a.source_id, "info_key": a.info_key,
                        "target_criteria": list(a.target_criteria), "target_trials": list(a.target_trial_ids),
                        "reason_code": a.reason_code, "purpose": a.purpose, "safety_required": a.safety_required,
                        "requestable": route is not None and (route[0] != "ask_patient" or preview is not None),
                        "patient_question_preview": preview,
                        "request_id": req["request_id"] if req else None,
                        "request_status": req["status"] if req else None})
        held = [{"action_type": a.action_type, "reason_code": a.reason_code, "info_key": a.info_key,
                 "target_criteria": list(a.target_criteria)} for a in plan.held]
        return out, held

    @staticmethod
    def _route(a: Action) -> tuple[str, str] | None:
        if a.action_type == "CLINICIAN_REVIEW" and a.purpose == "physician_reference":
            return ("ask_patient", "pending")  # 의사 참고용 정보 수집 질문 (판정 근거가 아님, evidence_directness)
        return ACTION_ROUTES.get(a.action_type)

    # ------------------------------------------------------------------ 결과 보기 (웹 실행 결과와 같은 형태)
    @staticmethod
    def _result_view(session) -> dict:
        from .. import conservative_view
        from ..status import MATCH_NOTE
        from ..web import display
        shown = conservative_view.build(session, display.redact_errors(session.result()), session.report)
        try:
            info = display.build(session)
        except Exception:  # noqa: BLE001 - 표시 정보 실패는 결과를 막지 않음 (web/app._display와 같은 처리)
            info = None
        return {"result": shown.result, "report": shown.report, "match_note": MATCH_NOTE,
                "display": conservative_view.adjust_display(info, shown.holds),
                "trials": {tid: {"title": t.get("title"), "overall_status": t.get("overall_status"),
                                 "url": (t.get("source") or {}).get("url")} for tid, t in session.trials.items()}}

    # ------------------------------------------------------------------ 평가 기록
    def _record(self, data: dict, session, ops: OpsState, trigger: dict, *, prev_pairs: dict | None,
                prev_criteria: dict | None, reeval: dict | None = None) -> EvaluationRecord:
        alias = data["alias"]
        ds, co = self._cohort(session, alias, ops)
        pairs = self._pairs(co)
        self._supersede(data, ds, pairs, session)
        self._open_conflicts(data, session)
        proposals, held = self._proposals(data, session, co)
        cur_criteria = _criteria_snapshot(session)
        transitions = [{"trial_id": t, "before": (prev_pairs or {}).get(t, {}).get("status"), "after": p["status"]}
                       for t, p in pairs.items() if prev_pairs is not None and prev_pairs.get(t, {}).get("status") != p["status"]]
        changes = [{"criterion": k, "before": (prev_criteria or {}).get(k), "after": v}
                   for k, v in cur_criteria.items() if prev_criteria is not None and prev_criteria.get(k) != v]
        rev = data.get("revision", 0) + 1  # 저장할 때의 개정 번호
        rec = EvaluationRecord(
            revision=rev, at=_now(), trigger=trigger, pairs=pairs, transitions=transitions, criteria_changes=changes,
            reevaluated_trials=(reeval or {}).get("reevaluated", sorted(session.matches) if prev_pairs is None else []),
            reused_trials=(reeval or {}).get("reused", []), impact=(reeval or {}).get("impact"),
            equivalence=(reeval or {}).get("equivalence", {"status": "initial" if prev_pairs is None else "not_applicable"}),
            proposals=proposals, conflicts=[{k: c[k] for k in ("field", "relation", "fact_ids")}
                                            for c in session.fact_store.conflicts(session.case.patient_id)],
            fact_version=ds.fact_version)
        data["evaluations"].append(rec.model_dump(mode="json"))
        data["session"] = codec.dump_session(session)
        data["ops"] = codec.dump_ops(ops)
        data["current"] = {"revision": rev, "pairs": pairs, "proposals": proposals, "held": held,
                           "view": self._result_view(session)}
        return rec

    def _event(self, data: dict, kind: str, by: str, **kw) -> None:
        data["events"].append({"at": _now(), "kind": kind, "by": by, **kw})

    # ------------------------------------------------------------------ 요청 상태
    @staticmethod
    def _set_status(req: dict, status: str, by: str, reason: str) -> None:
        req["history"].append(StatusEvent(at=_now(), from_status=req["status"], to_status=status, by=by,
                                          reason=reason).model_dump())
        req["status"] = status
        req["updated_at"] = _now()

    def _supersede(self, data: dict, ds, pairs: dict, session) -> None:
        """대상 기준이 더는 열려 있지 않은 요청을 닫음. 충돌 확인은 그 충돌이 사라졌을 때만."""
        open_criteria = {f"{c.trial_id}:{c.rule_id}" for c in _criteria(ds) if c.gap != "none"}
        live_pairs = {t for t, p in pairs.items() if p["status"] not in CLOSED_PAIRS}
        conflicts = {(c["field"], tuple(sorted(c["fact_ids"]))) for c in session.fact_store.conflicts(session.case.patient_id)}
        for r in data["requests"]:
            if r["status"] not in OPEN_STATUSES:
                continue
            if r["kind"] == "conflict_resolution":
                c = r.get("conflict") or {}
                if (c.get("field"), tuple(sorted(c.get("fact_ids", [])))) not in conflicts:
                    self._set_status(r, "superseded", "system", "conflict_no_longer_present")
            elif r["target_criteria"] and not any(k in open_criteria and k.split(":", 1)[0] in live_pairs
                                                  for k in r["target_criteria"]):
                self._set_status(r, "superseded", "system", "targets_resolved")

    def _open_conflicts(self, data: dict, session) -> list[dict]:
        """사실 저장소의 새 불일치마다 충돌 확인 요청 (값은 넣지 않음, 같은 사실 쌍은 한 번만)."""
        known = {(r["conflict"]["field"], tuple(sorted(r["conflict"]["fact_ids"])))
                 for r in data["requests"] if r["kind"] == "conflict_resolution" and r.get("conflict")}
        made = []
        for c in session.fact_store.conflicts(session.case.patient_id):
            key = (c["field"], tuple(sorted(c["fact_ids"])))
            if key in known:
                continue
            known.add(key)
            targets = sorted({f"{x.trial_id}:{x.rule_id}" for x in self._field_criteria(session, c["field"])})
            r = self._new_request(data, kind="conflict_resolution", status="requires_document", by="system",
                                  reason_code="fact_conflict", action=None, targets=targets, info_key=c["field"],
                                  source_id="medical_record",
                                  conflict={"field": c["field"], "relation": c["relation"], "fact_ids": list(key[1])})
            made.append(r)
        return made

    @staticmethod
    def _field_criteria(session, field_name: str) -> list:
        view = SessionView.from_session(session)
        ds = build_state(view)
        return [c for c in _criteria(ds) if field_name in (c.fields or []) or c.info_key == field_name]

    def _new_request(self, data: dict, *, kind: str, status: str, by: str, reason_code: str, action: Action | None,
                     targets: list[str], info_key: str | None = None, source_id: str | None = None,
                     question: PatientQuestion | None = None, follows: str | None = None,
                     conflict: dict | None = None, request_id: str | None = None) -> dict:
        now = _now()
        r = FollowupRequest(
            request_id=request_id or "FR-" + secrets.token_hex(8), kind=kind, status=status, created_at=now,
            updated_at=now, created_by=by, proposal_id=action.action_id if action else None,
            action=action.model_dump(mode="json") if action else None,
            action_type=action.action_type if action else None,
            source_id=(action.source_id if action else None) or source_id,
            info_key=(action.info_key if action else None) or info_key,
            target_criteria=list(action.target_criteria) if action else targets,
            target_trials=sorted(set(action.target_trial_ids)) if action else sorted({k.split(":", 1)[0] for k in targets}),
            reason_code=reason_code, question=question, follows=follows, conflict=conflict,
            revision_created=data.get("revision", 0) + 1,
            history=[StatusEvent(at=now, from_status=None, to_status=status, by=by, reason=reason_code)])
        d = r.model_dump(mode="json")
        data["requests"].append(d)
        return d

    # ------------------------------------------------------------------ 1. 사례 생성
    def create_case(self, *, patient_text: str, trial_ids: list[str], input_source: str = "self_report",
                    patient_token_digest: str, synthetic: bool = False) -> dict:
        text = (patient_text or "").strip()
        if not text:
            raise FollowupError(400, "empty_patient_text", "환자 정보를 입력하세요.")
        ids = list(dict.fromkeys(trial_ids))
        if not ids:
            raise FollowupError(400, "no_trials", "평가할 임상시험을 하나 이상 선택하세요.")
        unknown = [t for t in ids if t not in self.catalog.trials]
        if unknown:
            raise FollowupError(404, "trial_not_found", "선택한 임상시험을 찾을 수 없습니다.")
        if input_source not in INPUT_SOURCES:
            raise FollowupError(400, "bad_input_source", "input_source는 self_report / clinical_record / unknown 중 하나입니다.")
        case_id = new_case_id()
        case = PatientCase(patient_id="FU-" + secrets.token_hex(6).upper(), raw_text=text, input_source=input_source,
                           candidate_trials=ids)
        session = self._new_session(case, ids)
        with self.llm_scope(f"followup-{case_id[3:15]}-r1"):
            session.profile_patient()
            session.match(ids)
            session.finalize()
        data = {"format": FORMAT, "case_id": case_id, "created_at": _now(), "revision": 0,
                "patient_token_digest": patient_token_digest, "alias": "PA-" + secrets.token_hex(8),
                "catalog": self.catalog.name, "synthetic": synthetic, "trial_ids": ids,
                "requests": [], "responses": [], "evaluations": [], "events": [], "seen_responses": {}}
        self._record(data, session, OpsState(), {"type": "intake", "by": "patient"}, prev_pairs=None,
                     prev_criteria=None)
        self._event(data, "case_created", "patient", trials=len(ids))
        self.store.save(data, expected_revision=None)
        return self.store.load(case_id)

    # ------------------------------------------------------------------ 3. 요청 생성 (연구 담당자)
    def create_requests(self, case_id: str, proposal_ids: list[str], *, staff_id: str,
                        expected_revision: int | None = None) -> dict:
        by = f"staff:{staff_id}"
        if not proposal_ids:
            raise FollowupError(400, "no_proposals", "요청할 제안을 하나 이상 고르세요.")
        with self.store.lock(case_id):
            data = self._load(case_id)
            base = data["revision"]
            if expected_revision is not None and expected_revision != base:
                raise FollowupError(409, "stale_revision", "다른 변경이 먼저 저장되었습니다. 화면을 새로 고친 뒤 다시 고르세요.")
            session = self._restore(data)
            ops = codec.load_ops(data["ops"])
            _, co = self._cohort(session, data["alias"], ops)
            plan = self._plan(co, self._open_actions(data))
            current = {a.action_id: a for a in plan.selected}
            runner = EngineRunner(session)
            created, existing = [], []
            todo = list(dict.fromkeys(proposal_ids))
            for pid in todo:  # 먼저 모두 검사 (일부만 만들지 않음)
                if pid not in current:
                    raise FollowupError(409, "proposal_not_current", "지금 상태의 제안이 아닙니다. 화면을 새로 고치세요.")
                if self._route(current[pid]) is None:
                    raise FollowupError(400, "not_requestable", "기록용 행동은 요청으로 만들 수 없습니다.")
            for pid in todo:
                a = current[pid]
                kind, status = self._route(a)
                same = [r for r in data["requests"] if r.get("proposal_id") == pid]
                live = [r for r in same if r["status"] in OPEN_STATUSES]
                if live:
                    existing.append(live[0])
                    continue
                if kind == "ask_patient":
                    if any(r["status"] == "answered" for r in same):
                        raise FollowupError(409, "already_answered", "이미 답을 받은 질문입니다.")
                    if self._unknown_before(data, a):
                        raise FollowupError(409, "already_unknown",
                                            "환자가 '모름'으로 답한 항목입니다. 같은 질문 대신 대체 경로(문서·임상 검토)를 쓰세요.")
                rid = "FR-" + secrets.token_hex(8)
                question = None
                if kind == "ask_patient":
                    q = runner.render(a, rid)
                    if q is None:
                        raise FollowupError(422, "not_renderable", "이 제안은 환자 질문으로 만들 수 없습니다.")
                    question = PatientQuestion(question=q.question, answer_type=q.answer_type, options=list(q.options),
                                               unit=q.unit)
                created.append(self._new_request(data, kind=kind, status=status, by=by, reason_code=a.reason_code,
                                                 action=a, targets=list(a.target_criteria), question=question,
                                                 request_id=rid))
            if created:
                self._event(data, "requests_created", by, request_ids=[r["request_id"] for r in created])
                _, co = self._cohort(session, data["alias"], ops)
                data["current"]["proposals"], data["current"]["held"] = self._proposals(data, session, co)
                self.store.save(data, expected_revision=base)
                data = self.store.load(case_id)
            return {"created": [r["request_id"] for r in created], "existing": [r["request_id"] for r in existing],
                    "case": data}

    @staticmethod
    def _unknown_before(data: dict, a: Action) -> bool:
        for r in data["requests"]:
            if r["kind"] != "ask_patient" or r["status"] not in ("unknown", "unreachable"):
                continue
            if (a.info_key and r.get("info_key") == a.info_key) or set(r["target_criteria"]) & set(a.target_criteria):
                return True
        return False

    def _load(self, case_id: str) -> dict:
        try:
            return self.store.load(case_id)
        except KeyError:
            raise FollowupError(404, "case_not_found", "사례를 찾을 수 없거나 접근 권한이 없습니다.") from None

    # ------------------------------------------------------------------ 5. 환자 응답
    def respond(self, case_id: str, request_id: str, *, answer: str | None, unknown: bool,
                client_response_id: str | None = None, validate: Callable[[dict, str], bool] | None = None) -> dict:
        text = DECLINED if unknown else (answer or "").strip()
        if not text:
            raise FollowupError(400, "empty_answer", "답을 입력하거나 '모름'을 선택하세요.")
        digest = answer_digest(text)
        with self.store.lock(case_id):
            data = self._load(case_id)
            base = data["revision"]
            req = next((r for r in data["requests"] if r["request_id"] == request_id), None)
            if req is None or req["kind"] not in PATIENT_KINDS:
                raise FollowupError(404, "request_not_found", "요청을 찾을 수 없습니다.")
            if req["status"] != "pending":
                prev = next((x for x in data["responses"] if x["response_id"] == req.get("response_id")), None)
                if prev is not None and (prev["answer_digest"] == digest or
                                         (client_response_id and prev.get("client_response_id") == client_response_id)):
                    return {"duplicate": True, "response": prev, "request": req, "case": data}
                raise FollowupError(409, "request_closed", "이미 처리된 요청입니다.")
            if not unknown and validate is not None and validate(req["question"] or {}, text):
                raise FollowupError(400, "invalid_answer", "선택지에 없는 답변입니다. 화면의 선택지에서 고르거나 '모름'을 선택하세요.")
            status, code = ("no_response", "no_answer") if unknown else answer_outcome(text)
            outcome = "unknown" if status == "no_response" else "answered"
            if outcome == "unknown":
                text = DECLINED  # '잘 모르겠어요' 등도 무응답 규칙 하나로 (engine.answers)
            session = self._restore(data)
            ops = codec.load_ops(data["ops"])
            prev_pairs, prev_criteria = data["current"]["pairs"], _criteria_snapshot(session)
            action = Action.model_validate(req["action"])
            q = req["question"]
            cq = ClarifyingQuestion(question_id=request_id, question=q["question"], answer_type=q["answer_type"],
                                    options=q["options"], unit=q.get("unit"), target=req["target_criteria"],
                                    purpose=action.purpose, why=req["reason_code"])
            session.rounds += 1
            pair = qa_pair(action, cq, round_no=session.rounds, answer=text, session_id=session.session_id)
            session.qa_log.append(pair)  # Session.clarify와 같은 기록·원문 블록
            session.case.raw_text += f"\n\n[추가 확인 정보 - 라운드 {session.rounds}]\nQ: {pair.question}\nA: {pair.answer}"
            _, co = self._cohort(session, data["alias"], ops)
            co, _ = self.policy_factory().apply_outcome(
                co, action, ActionOutcome(action_id=action.action_id, status=status, error_code=code))
            ops = co.ops[data["alias"]]
            facts_before = {f.fact_id for f in session.fact_store.facts(session.case.patient_id)}
            with self.llm_scope(f"followup-{case_id[3:15]}-r{base + 1}"):
                session, reeval = self._reevaluate(session, req["target_criteria"], data["alias"], ops)
            added = [f.fact_id for f in session.fact_store.facts(session.case.patient_id) if f.fact_id not in facts_before]
            rid = "FS-" + secrets.token_hex(8)
            self._set_status(req, outcome, "patient", "patient_answered" if outcome == "answered" else "patient_unknown")
            req["response_id"] = rid
            trigger = {"type": "response", "request_id": request_id, "outcome": outcome}
            before_ids = {r["request_id"] for r in data["requests"]}
            rec = self._record(data, session, ops, trigger, prev_pairs=prev_pairs, prev_criteria=prev_criteria,
                               reeval=reeval)
            routed = []
            if outcome == "unknown":
                routed = self._route_after_failure(data, req, session, ops)
            conflicts = [r for r in data["requests"] if r["request_id"] not in before_ids
                         and r["kind"] == "conflict_resolution"]
            resp = FollowupResponse(response_id=rid, request_id=request_id, at=_now(), outcome=outcome, answer=text,
                                    answer_digest=digest, client_response_id=client_response_id,
                                    revision_after=rec.revision, fact_ids_added=added,
                                    conflicts=[{"request_id": r["request_id"], **r["conflict"]} for r in conflicts])
            data["responses"].append(resp.model_dump(mode="json"))
            self._event(data, "response_recorded", "patient", request_id=request_id, outcome=outcome,
                        routed=[r["request_id"] for r in routed])
            self.store.save(data, expected_revision=base)
            data = self.store.load(case_id)
            return {"duplicate": False, "response": resp.model_dump(mode="json"), "request": req,
                    "routed": routed, "evaluation": rec.model_dump(mode="json"), "case": data}

    def _route_after_failure(self, data: dict, failed: dict, session, ops: OpsState) -> list[dict]:
        """'모름'·연락 불가·문서 없음 뒤: 같은 질문을 반복하지 않고 정책 엔진의 대체 경로를 새 요청으로.

        대체 정보원(문서) 제안이 있으면 그 요청, 남은 정보원이 없으면 임상 검토 요청 (완료 상태는 만들지 않음)."""
        targets = set(failed["target_criteria"])
        key = failed.get("info_key")
        live = {r["proposal_id"] for r in data["requests"] if r["status"] in OPEN_STATUSES and r.get("proposal_id")}
        _, co = self._cohort(session, data["alias"], ops)
        plan = self._plan(co, self._open_actions(data))
        for a in plan.selected:
            if a.action_type not in FALLBACK_TYPES or self._route(a)[0] == "ask_patient":
                continue
            if not ((key and a.info_key == key) or targets & set(a.target_criteria)):
                continue
            if a.action_id in live:
                return []
            kind, status = self._route(a)
            r = self._new_request(data, kind=kind, status=status, by="system", reason_code=a.reason_code, action=a,
                                  targets=list(a.target_criteria), follows=failed["request_id"])
            self._refresh_current(data, session, ops)
            return [r]
        pairs = data["current"]["pairs"]
        still = sorted(k for k in targets if pairs.get(k.split(":", 1)[0], {}).get("status") not in CLOSED_PAIRS)
        if not still:
            return []
        if any(r["kind"] == "clinician_review" and r["status"] in OPEN_STATUSES and set(r["target_criteria"]) == set(still)
               for r in data["requests"]):
            return []
        r = self._new_request(data, kind="clinician_review", status="requires_clinician", by="system",
                              reason_code="item_sources_exhausted", action=None, targets=still, info_key=key,
                              source_id="clinician_review", follows=failed["request_id"])
        self._refresh_current(data, session, ops)
        return [r]

    def _refresh_current(self, data: dict, session, ops: OpsState) -> None:
        _, co = self._cohort(session, data["alias"], ops)
        data["current"]["proposals"], data["current"]["held"] = self._proposals(data, session, co)

    # ------------------------------------------------------------------ 증분·전체 재평가
    def _reevaluate(self, session, targets: list[str], alias: str, ops: OpsState):
        pid = session.case.patient_id
        prev = {t: m.model_copy(deep=True) for t, m in session.matches.items()}
        before = take_snapshot(session.fact_store, pid)
        session.profile_patient()
        after = take_snapshot(session.fact_store, pid)
        changes = detect_changes(before, after)
        parsed_map = {t: (None if m.eligibility == "NOT_EVALUATED" else session.parsed.get(t)) for t, m in prev.items()}
        deps = {}
        for tid, p in parsed_map.items():
            if p is None:
                continue
            by_rule = {a.rule_id: a for a in prev[tid].assessments}
            for r in p.rules:
                deps[(tid, r.rule_id)] = resolve(tid, r, matcher._meta(p, r), by_rule.get(r.rule_id))
        graph = DependencyGraph.build(parsed_map, deps, before)
        impact = analyze_impact(changes, graph, before, after)
        affected = set(impact.affected_trials) | {k.split(":", 1)[0] for k in targets}
        # 새 문답이 원문에 붙었으므로 LLM 판정 기준(입력 항목을 검증할 수 없음)이 있는 시험도 다시 평가
        affected |= {tid for (tid, _), d in deps.items() if d.evaluator == "llm"}
        affected = sorted(t for t in affected if parsed_map.get(t) is not None)
        reused = sorted(t for t in prev if t not in affected)
        pre_match = codec.dump_session(session)
        if affected:
            session.match(affected)
        equivalence: dict = {"status": "not_checked"}
        if self.full_check:
            full = codec.restore_session(pre_match, self.catalog.trials, self.catalog.parsed)
            full.match(list(prev))
            diffs = sorted(t for t in prev if _match_signature(session.matches[t]) != _match_signature(full.matches[t]))
            inc_pairs = self._pairs(self._cohort(session, alias, ops)[1])
            full_pairs = self._pairs(self._cohort(full, alias, ops)[1])
            pair_diffs = sorted(t for t in prev if inc_pairs.get(t, {}).get("status") != full_pairs.get(t, {}).get("status"))
            if diffs or pair_diffs:
                equivalence = {"status": "mismatch_full_adopted", "trials": diffs, "pairs": pair_diffs}
                session = full  # 증분 결과를 조용히 쓰지 않음: 전체 재평가 결과로
            else:
                equivalence = {"status": "match", "compared_trials": len(prev)}
        session.finalize()
        return session, {"reevaluated": affected, "reused": reused, "impact": impact.summary(),
                         "equivalence": equivalence}

    # ------------------------------------------------------------------ 연구 담당자 상태 변경
    def set_status(self, case_id: str, request_id: str, status: str, *, staff_id: str) -> dict:
        by = f"staff:{staff_id}"
        with self.store.lock(case_id):
            data = self._load(case_id)
            base = data["revision"]
            req = next((r for r in data["requests"] if r["request_id"] == request_id), None)
            if req is None:
                raise FollowupError(404, "request_not_found", "요청을 찾을 수 없습니다.")
            allowed = STAFF_TRANSITIONS.get(req["kind"], {}).get(req["status"], frozenset())
            if status not in allowed:
                raise FollowupError(409, "transition_not_allowed", "이 요청에서 할 수 없는 상태 변경입니다.")
            session = self._restore(data)
            ops = codec.load_ops(data["ops"])
            prev_pairs = data["current"]["pairs"]
            failed: list[dict] = []
            if status in ("unreachable", "document_unavailable"):
                group = [req]
                if status == "unreachable":  # 연락 불가는 정보원 전체: 같은 환자의 다른 대기 질문도 함께
                    group += [r for r in data["requests"] if r is not req and r["kind"] == "ask_patient"
                              and r["status"] == "pending"]
                _, co = self._cohort(session, data["alias"], ops)
                pol = self.policy_factory()
                for r in group:
                    if r.get("action"):
                        a = Action.model_validate(r["action"])
                        co, _ = pol.apply_outcome(co, a, ActionOutcome(action_id=a.action_id, status="failed",
                                                                       error_code=UNREACHABLE))
                    self._set_status(r, status, by if r is req else "system",
                                     "staff_marked" if r is req else "source_unreachable")
                    failed.append(r)
                ops = co.ops.get(data["alias"], ops)
            else:
                self._set_status(req, status, by, "staff_marked")
            rec = self._record(data, session, ops, {"type": "staff_status", "request_id": request_id, "status": status},
                               prev_pairs=prev_pairs, prev_criteria=_criteria_snapshot(session))
            routed = [x for r in failed for x in self._route_after_failure(data, r, session, ops)]
            self._event(data, "request_status", by, request_id=request_id, status=status,
                        routed=[r["request_id"] for r in routed])
            self.store.save(data, expected_revision=base)
            return {"request": req, "routed": routed, "evaluation": rec.model_dump(mode="json"),
                    "case": self.store.load(case_id)}

    # ------------------------------------------------------------------ 보기
    def patient_requests(self, case_id: str) -> dict:
        data = self._load(case_id)
        resp = {r["response_id"]: r for r in data["responses"]}
        out = []
        for r in data["requests"]:
            if r["kind"] not in PATIENT_KINDS or r["status"] in ("superseded", "cancelled") and not r.get("response_id"):
                continue
            item = {"request_id": r["request_id"], "status": r["status"], "created_at": r["created_at"],
                    "question": r["question"], "answerable": r["status"] == "pending"}
            if r.get("response_id") in resp:
                x = resp[r["response_id"]]
                item["response"] = {"outcome": x["outcome"], "answer": x["answer"], "at": x["at"]}
            out.append(item)
        return {"case_id": case_id, "revision": data["revision"], "requests": out,
                "pending": sum(1 for x in out if x["answerable"])}

    def inspector_view(self, case_id: str) -> dict:
        from .. import inspector
        data = self._load(case_id)
        session = self._restore(data)
        rec = inspector.record_from_session(session, meta={"llm_backend": "recorded", "synthetic": bool(data["synthetic"])})
        rec.result = data["current"]["view"]["result"]  # 화면과 같은 보류 적용 결과
        view = inspector.build_view(rec)
        view["followup"] = {"evaluations": data["evaluations"], "requests": data["requests"],
                            "responses": [{k: v for k, v in x.items() if k != "answer_digest"} for x in data["responses"]]}
        return view

    def staff_queue(self) -> dict:
        """모든 열린 사례를 한 코호트로 보고 DCV+C가 고른 순서 (Cohort C 우선순위). 판정은 바꾸지 않음."""
        states, ops, case_of, requested, sessions = {}, {}, {}, [], {}
        for cid in self.store.case_ids():
            data = self.store.load(cid)
            session = self._restore(data)
            view = SessionView.from_session(session)
            view.matched_versions = {t: view.snapshot.version for t in session.matches}
            states[data["alias"]] = build_state(view)
            ops[data["alias"]] = codec.load_ops(data["ops"])
            case_of[data["alias"]] = data
            sessions[data["alias"]] = session
            requested += self._open_actions(data)
        if not states:
            return {"items": []}
        co = CohortState.from_decision_states(states, ops=ops)
        plan = self._plan(co, requested)
        items = []
        for rank, a in enumerate(plan.selected, 1):
            data = case_of[a.patient_alias]
            req = next((r for r in data["requests"] if r.get("proposal_id") == a.action_id), None)
            route = self._route(a)
            items.append({"rank": rank, "case_id": data["case_id"], "proposal_id": a.action_id,
                          "action_type": a.action_type, "route": route[0] if route else None,
                          "source_id": a.source_id, "info_key": a.info_key, "target_criteria": list(a.target_criteria),
                          "reason_code": a.reason_code, "safety_required": a.safety_required,
                          "request_id": req["request_id"] if req else None,
                          "request_status": req["status"] if req else None})
        return {"items": items, "cases": len(states)}
