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

8. 문서 검증 (verify_document, 문서 검증자 권한): 등록된 문서 + 요청 범위의 사실 -> 서명된 증명(FactStore.attest)
   -> 증명이 바꾼 사실에 영향받은 기준 재평가 (프로필 재추출 없음) -> 요청 document_verified.
9. 임상의 판단 (clinician_judgment, 임상의 권한): 임상의 판단 기준(expert_boundary.clinician_reason)만, 서명된 판단 기록
   -> 재평가 (expert_boundary가 인정한 판단만 반영) -> 요청 clinician_decided.
   임상의 판단 기준이 EXPERT_REVIEW로 남으면 정책 제안과 별개로 임상 검토 요청을 엶 (충돌 확인과 같은 방식).

지키는 것
- 확정 적격은 판정 계층(conservative_view·engine)만 정함. 이 모듈은 판정 값을 만들거나 고치지 않음.
- 문서 확인·임상의 판단은 상태 문자열이 아니라 현재 권한(_scope: document_authority·clinician_authority)이 인정한
  증명·판단으로만 판정에 들어감. 운영에는 그 권한이 없음 (verification.py). 모든 평가·보기는 _scope 안에서 함.
- 연구 담당자·검증자·임상의가 환자 답을 대신 입력하는 경로가 없음 (respond는 웹 계층에서 환자 권한으로만 부름).
- 사례 기록의 events는 저장 뒤 추가 전용 감사 기록(verification.AuditLog)으로 옮김.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .. import clinician_authority, document_authority, expert_boundary
from ..agents import matcher
from ..contract_session import session_class
from ..decision_state import SessionView, build_state
from ..dependency_graph import DependencyGraph, analyze_impact, resolve
from ..engine.answers import answer_outcome
from ..engine.cohort_state import CohortState, OpsState
from ..engine.contracts import UNREACHABLE, Action, ActionOutcome, PairStatus
from ..engine.loop import EngineLoop
from ..engine.dcvc import FullDCVCPolicy
from ..engine.question_bridge import qa_pair
from ..fact_changes import detect_changes, take_snapshot
from ..pipeline import PatientCase
from ..schemas import ClarifyingQuestion, ParsedTrial
from ..trace import Tracer
from ..web.engine_runner import DEFAULT_BUDGET, EngineRunner
from . import codec, verification
from .models import (ACTION_ROUTES, FORMAT, OPEN_STATUSES, PATIENT_KINDS, STAFF_TRANSITIONS, EvaluationRecord,
                     FollowupRequest, FollowupResponse, PatientQuestion, StatusEvent)
from .store import CaseStore, new_case_id

# 남은 정보원이 없어 시스템이 만든 임상 검토 (_route_after_failure·_conflict_to_clinician). 대상 기준이 임상의 판단 기준이
# 아니면 판단 API로 끝낼 수 없고(judgeable=False), 쌍은 보류로 남음. 화면은 '의사 판단 대기'가 아니라 '확인할 방법 없음'으로 보여 줌
NO_PATH_REASONS = frozenset({"item_sources_exhausted", "conflict_unresolved"})
CLOSED_PAIRS = frozenset({PairStatus.CONFIRMED_ELIGIBLE.value, PairStatus.CONFIRMED_INELIGIBLE.value,
                          PairStatus.NOT_EVALUATED.value})
DECLINED = "모름"
INPUT_SOURCES = ("self_report", "clinical_record", "unknown")
FALLBACK_TYPES = ("REQUEST_DOCUMENT", "VERIFY_RECORD", "CLINICIAN_REVIEW")
# 웹 엔진 실행기(DEFAULT_BUDGET)는 임상의 시간이 0: 화면에서 임상의 검토를 실행할 수단이 없어 not_executed로 정산함.
# Follow-up은 임상 검토 요청(clinician_review, requires_clinician)으로 담당 의료진에게 넘길 수 있으므로 같은 예산에
# 임상의 시간만 더함. 0이면 원문에 구체 기준이 없는 기준·임상의 전용 기준의 검토(CLINICIAN_REVIEW)가 언제나 over_budget으로
# 빠져 담당자에게 제안되지 않음 (Q-P 통합 점검에서 NSCLC E3·E4·I2로 확인).
log = logging.getLogger(__name__)

FOLLOWUP_BUDGET = DEFAULT_BUDGET.model_copy(update={"clinician_minutes": DEFAULT_BUDGET.coordinator_minutes})


# Inspector 보기의 구분: 현재 판정(지금 권한 기준으로 다시 계산) vs 과거 평가 기록(그 평가 당시 권한의 판정, 고쳐 쓰지 않음)
INSPECTOR_CURRENT_KEYS = ("overview", "trials", "recommendation", "decision", "screening", "gaps", "verification")
INSPECTOR_HISTORY_KEYS = ("steps", "history", "timeline", "changes", "fact_changes", "unlinked_question_rounds", "followup")
INSPECTOR_HISTORY_NOTICE = "평가 당시 기록 — 현재 판정과 다를 수 있음"


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


def _sink_tracer(sink: list[dict]) -> Tracer:
    """Inspector의 라운드별 판정 변화(matcher done 기록)만 모으는 tracer. 화면·파일로 보내지 않음."""
    def keep(rec: dict) -> None:
        if rec.get("agent") == "matcher" and rec.get("event") == "done":
            sink.append({k: v for k, v in rec.items() if k != "t"})
    return Tracer(None, verbose=False, listener=keep)


def _criteria_snapshot(session) -> dict[str, dict]:
    return {f"{tid}:{a.rule_id}": {"passes": a.passes, "holds": a.holds, "method": a.method,
                                    "needs_verification": a.needs_verification}
            for tid, m in session.matches.items() for a in m.assessments}


class FollowupService:
    def __init__(self, store: CaseStore, catalog: TrialCatalog, *, full_check: bool = True,
                 llm_scope: Callable[[str], contextlib.AbstractContextManager] | None = None,
                 policy_factory: Callable[[], FullDCVCPolicy] = FullDCVCPolicy,
                 verification_config: Callable[[], verification.VerificationConfig] = verification.VerificationConfig):
        self.store = store
        self.catalog = catalog
        self.full_check = full_check
        self.llm_scope = llm_scope or (lambda label: contextlib.nullcontext())
        self.policy_factory = policy_factory
        self.verification_config = verification_config  # 호출할 때마다 읽음 (재시작 뒤 설정이 바뀌면 그 설정으로)
        self.documents = verification.DocumentStore(store.root)
        self.audit = verification.AuditLog(store.root)

    # ------------------------------------------------------------------ 권한 범위·저장
    @contextlib.contextmanager
    def _scope(self, data: dict, session):
        """이 사례의 문서·임상의 권한 아래에서 평가·보기. 합성 모드가 아니거나 실제 사례면 권한 없음(아무것도 인정 안 함).

        프로세스 기본값(테스트 conftest의 합성 권한 포함)을 쓰지 않도록 항상 명시적으로 설정."""
        cfg = self.verification_config()
        with document_authority.use(verification.document_authority_for(cfg, data)), \
                clinician_authority.use(verification.clinician_authority_for(cfg, data, session)):
            yield cfg

    def _commit(self, data: dict, base: int | None) -> dict:
        self.store.save(data, expected_revision=base)
        saved = self.store.load(data["case_id"])
        self.audit.sync(saved)
        return saved

    # ------------------------------------------------------------------ Session
    def _new_session(self, case: PatientCase, trial_ids: list[str], sink: list[dict]):
        s = session_class()(case=case, trials={t: self.catalog.trials[t] for t in trial_ids},
                            answerer=codec.NoAnswerer(), tracer=_sink_tracer(sink))
        s.parsed.update({t: p for t, p in self.catalog.parsed.items() if t in s.trials})
        return s

    def _restore(self, data: dict, sink: list[dict] | None = None):
        return codec.restore_session(data["session"], self.catalog.trials, self.catalog.parsed,
                                     tracer=_sink_tracer(sink if sink is not None else []))

    def _match(self, session, ids: list[str]) -> None:
        """Session.match 뒤 자기 보고 보류 해제 (evidence_basis.release_match, 이 사례의 문서 권한 범위 안에서 부름)."""
        session.match(ids)
        self._release(session, ids)

    @staticmethod
    def _release(session, ids) -> None:
        from .. import evidence_basis
        pid = session.case.patient_id
        for tid in ids:
            m, p = session.matches.get(tid), session.parsed.get(tid)
            if m is not None and p is not None:
                check = evidence_basis.document_check(session.fact_store, pid, p, session.reference_date)
                session.matches[tid] = evidence_basis.release_match(m, p, check)

    @staticmethod
    def _view(session) -> SessionView:
        """판정 상태를 읽는 사본. 저장된 임상의 판정은 지금 권한이 그 판단을 인정할 때만 그대로 (아니면 판단 대기)."""
        from .. import evidence_basis
        view = SessionView.from_session(session)
        for tid, m in list(view.matches.items()):
            p = session.parsed.get(tid)
            allx = m.assessments + m.other_cohort_assessments
            if p is None or not any(a.method == "clinician" for a in allx):
                continue
            rules = {r.rule_id: r for r in p.rules}
            after = [expert_boundary.unrecognized_to_unknown(tid, rules[a.rule_id], a) if a.rule_id in rules else a
                     for a in allx]
            if any(x is not y for x, y in zip(after, allx)):
                view.matches[tid] = evidence_basis.reaggregate(m, p, after, "인정되지 않은 임상의 판단 보류")
        return view

    def _cohort(self, session, alias: str, ops: OpsState):
        view = self._view(session)
        view.matched_versions = {t: view.snapshot.version for t in session.matches}
        ds = build_state(view)
        return ds, CohortState.from_decision_states({alias: ds}, ops={alias: ops})

    @staticmethod
    def _pairs(co: CohortState) -> dict[str, dict]:
        return {p.pair.trial_id: {"status": p.status.value, "eligibility": p.eligibility, "open": p.open,
                                  "gaps": sorted(p.gaps), "pending_kinds": sorted(p.pending_kinds),
                                  "mismatch": p.mismatch} for p in co.pairs()}

    # ------------------------------------------------------------------ 제안 (DCV+C)
    def _loop(self, requests: list[dict], unproven: frozenset[str] = frozenset()) -> EngineLoop:
        """P 공통 엔진 루프(engine.loop.EngineLoop)를 요청 기록에서 다시 세움. 웹(CohortRunner)·WP6 시뮬레이터와 같은 객체.

        - 진행 중 요청(OPEN_STATUSES): 실행했으므로 원장에 청구(같은 정보원 묶음 할인). 아직 결과가 없어 시도로 세지 않음
          -> 같은 제안이 화면에 남고, 다시 고르면 기존 요청을 돌려줌 (create_requests의 existing).
        - 끝난 요청(답변·모름·연락 불가·문서 없음·확인 완료·대체됨): loop.begin(executed=True) = 청구 + 시도 기록. 같은 사실
          상태에서 같은 정보원에 같은 행동을 다시 고르지 않음 (repeat_key, 시뮬레이터·웹과 같은 반복 억제).
        - 취소: 실행하지 않았으므로 기록하지 않음.
        - 문서 확인을 마쳤지만 그 증명이 지금 권한으로 인정되지 않는 요청(unproven, _plan_gate): 청구만 하고 시도로 세지 않음.
          권한 철회(서명 비밀 변경 등)는 사실 지문을 바꾸지 않으므로, 세면 같은 확인을 다시 제안할 수 없음 (V4-B1)."""
        loop = EngineLoop(self.policy_factory(), FOLLOWUP_BUDGET)
        for r in requests:
            if not r.get("action") or r["status"] == "cancelled":
                continue
            a = Action.model_validate(r["action"])
            if r["status"] in OPEN_STATUSES or r["request_id"] in unproven:
                loop.ledger.charge(a)
            else:
                loop.begin(a, executed=True)
        return loop

    @staticmethod
    def _plan_gate(data: dict, session, gate: dict | None = None) -> dict:
        """계획에 쓰는 지금 권한의 사실 (반드시 그 사례의 _scope 안에서 부름). 여러 사례(대기열·Insight)는 같은 gate에 모음.

        - unproven: 문서 확인 완료(document_verified) 요청 중 확인한 사실이 지금 document_backed가 아닌 것
        - no_basis: 증명이 철회된 확인(unproven)이 있고 문서로 확인된 사실이 하나도 남지 않은 환자 별칭 -> 그 환자의 시험
          해석본. 진료 기록 필요 기준의 임상의 판단은 문서로 확인된 근거 사실이 하나 이상 있어야 기록되므로(clinician_judgment,
          409 basis_not_document_verified) 그 임상 검토(CLINICIAN_REVIEW) 제안은 지금 끝낼 수 없음 -> 다시 확인 제안을 먼저
          냄. 처음 접수(검증 기록 없음)의 임상 검토 요청·판단 가능 표시는 기존 계약 그대로 (서비스가 병행해서 엶)."""
        from ..fact_store import FactStoreError, document_backed
        gate = gate if gate is not None else {"unproven": set(), "no_basis": {}}
        withdrawn = False
        for v in data.get("verifications") or []:
            for it in v.get("items") or []:
                try:
                    ok = document_backed(session.fact_store.get(it.get("fact_id")))
                except FactStoreError:
                    ok = False
                if not ok:
                    gate["unproven"].add(v.get("request_id"))
                    withdrawn = True
                    break
        if withdrawn and not any(document_backed(f) for f in session.fact_store.facts(session.case.patient_id)):
            gate["no_basis"][data["alias"]] = dict(session.parsed)
        return gate

    @staticmethod
    def _needs_record_basis(parsed: dict, keys) -> bool:
        """대상 기준 중 진료 기록 근거가 필요한 기준(matcher._meta required_evidence=clinical_record)이 있는가."""
        for key in keys:
            tid, _, rid = key.partition(":")
            p = parsed.get(tid)
            rule = next((x for x in p.rules if x.rule_id == rid), None) if p else None
            if rule is not None and matcher._meta(p, rule).required_evidence == "clinical_record":
                return True
        return False

    def _plan(self, co: CohortState, requests: list[dict], gate: dict):
        """제안 = 공통 루프의 계획 (propose -> 반복 억제 -> select). 상태를 바꾸지 않는 preview. gate = _plan_gate."""
        no_basis = gate["no_basis"]

        def keep(a: Action) -> bool:
            return not (a.action_type == "CLINICIAN_REVIEW" and a.patient_alias in no_basis
                        and self._needs_record_basis(no_basis[a.patient_alias], a.target_criteria))

        return self._loop(requests, frozenset(gate["unproven"])).preview(co, keep)

    def _settle(self, co: CohortState, requests: list[dict], action: Action,
                outcome: ActionOutcome) -> CohortState:
        """결과 반영 = 공통 루프의 settle (policy.apply_outcome, 운영 상태만, 판정 불변)."""
        co, _ = self._loop(requests).settle(co, action, outcome)
        return co

    def _proposals(self, data: dict, session, co: CohortState) -> tuple[list[dict], list[dict]]:
        plan = self._plan(co, data["requests"], self._plan_gate(data, session))
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
        """행동 -> 요청 종류. 근거 요건은 정책(P full_dcvc 근거 경로)이 행동 유형으로 정함:
        자기 보고 허용 기준 = ASK_PATIENT(환자 질문), 진료 기록 필요 기준 = REQUEST_DOCUMENT(문서 확인),
        임상의 전용·원문에 구체 기준 없음 = CLINICIAN_REVIEW(임상 검토). 의사 참고용(physician_reference) 임상 검토도
        환자 질문으로 바꾸지 않고 임상 검토로 보냄 (환자 답은 그 기준의 판정 근거가 될 수 없음)."""
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
        self._open_conflicts(data, session, co)
        self._open_clinician_reviews(data, ds, pairs, session)
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
        # 결과 화면도 쌍 상태와 같은 지금 권한 기준 (권한 철회 뒤 상태 변경만 있는 재기록에서 철회 전 해제·판단이 남지 않게).
        # 저장 Session은 위에서 이미 dump함 (판정 기록은 그대로). 권한이 그대로면 None -> 기존 화면 경로
        regated = (self._regated_view_if_changed(session)
                   if (data.get("verifications") or data.get("judgments")) else None)
        data["current"] = {"revision": rev, "pairs": pairs, "proposals": proposals, "held": held,
                           "view": regated or self._result_view(session)}
        # 판정 변경 감사: 쌍 상태 전이와 바뀐 기준 (값·원문은 넣지 않음)
        self._event(data, "evaluation_recorded", "system", revision=rev, trigger=trigger.get("type"),
                    transitions=transitions, criteria_changed=sorted(c["criterion"] for c in changes),
                    equivalence=rec.equivalence.get("status"), fact_version=ds.fact_version)
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
        """대상 기준이 더는 열려 있지 않은 요청을 닫음. 충돌 확인은 그 충돌이 사라졌을 때만.

        열린 기준 = 정보 빈칸(gap)이 남은 기준 + 아직 문서로 검증되지 않은 불충족 근거 기준 (쌍이 provisional 불충족일 때의
        mismatch_basis, decision_policy의 exclusion_basis_unverified 제안과 같은 조건). 근거 기준은 값이 있어 gap이 없지만
        확인이 남았으므로, gap만 보면 그 근거 확인 요청이 다른 서류 확인의 재평가에서 닫히고 다시 제안되지 않음 (11-08)."""
        crit = {c.key: c for c in _criteria(ds)}
        open_criteria = {k for k, c in crit.items() if c.gap != "none"}
        open_criteria |= {k for t in ds.trials.values() if t.open and t.mismatch == "provisional"
                          for k in t.mismatch_basis if k in crit and crit[k].basis_tier != "verified"}
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

    def _open_conflicts(self, data: dict, session, co: CohortState) -> list[dict]:
        """사실 저장소의 새 불일치마다 충돌 확인 요청 (값은 넣지 않음, 같은 사실 쌍은 한 번만).

        정책 엔진이 같은 항목의 안전 확인(VERIFY_RECORD)을 냈으면 그 제안에 연결 (담당자가 같은 확인을 또 만들지 않게)."""
        known = {(r["conflict"]["field"], tuple(sorted(r["conflict"]["fact_ids"])))
                 for r in data["requests"] if r["kind"] == "conflict_resolution" and r.get("conflict")}
        made = []
        for c in session.fact_store.conflicts(session.case.patient_id):
            key = (c["field"], tuple(sorted(c["fact_ids"])))
            if key in known:
                continue
            known.add(key)
            targets = sorted({f"{x.trial_id}:{x.rule_id}" for x in self._field_criteria(session, c["field"])})
            linked = {r["proposal_id"] for r in data["requests"] if r.get("proposal_id")}
            verify = next((a for a in self._plan(co, data["requests"], self._plan_gate(data, session)).selected
                           if a.action_type == "VERIFY_RECORD" and a.info_key == c["field"] and a.action_id not in linked),
                          None)
            r = self._new_request(data, kind="conflict_resolution", status="requires_document", by="system",
                                  reason_code="fact_conflict", action=verify, targets=targets, info_key=c["field"],
                                  source_id="medical_record",
                                  conflict={"field": c["field"], "relation": c["relation"], "fact_ids": list(key[1])})
            made.append(r)
        return made

    def _open_clinician_reviews(self, data: dict, ds, pairs: dict, session) -> list[dict]:
        """임상의 판단 기준(expert_boundary)이 판단 대기로 남은 열린 쌍마다 임상 검토 요청 (기준 하나에 열린 요청 하나).

        정책 엔진은 이 기준에 판정 근거가 되는 제안을 내지 않으므로(의사 참고용 질문만) 서비스가 직접 엶."""
        live_pairs = {t for t, p in pairs.items() if p["status"] not in CLOSED_PAIRS}
        covered = {k for r in data["requests"] if r["kind"] == "clinician_review" and r["status"] in OPEN_STATUSES
                   for k in r["target_criteria"]}
        made = []
        for c in _criteria(ds):
            key = f"{c.trial_id}:{c.rule_id}"
            parsed = session.parsed.get(c.trial_id)
            rule = next((r for r in parsed.rules if r.rule_id == c.rule_id), None) if parsed else None
            if (c.gap != "physician_review" or c.trial_id not in live_pairs or key in covered or rule is None
                    or expert_boundary.clinician_reason(c.trial_id, rule) is None):
                continue
            covered.add(key)
            made.append(self._new_request(data, kind="clinician_review", status="requires_clinician", by="system",
                                          reason_code="clinician_judgment_required", action=None, targets=[key],
                                          info_key=c.info_key, source_id="clinician_review"))
        return made

    @staticmethod
    def _field_criteria(session, field_name: str) -> list:
        view = FollowupService._view(session)
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
        sink: list[dict] = []
        session = self._new_session(case, ids, sink)
        data = {"format": FORMAT, "case_id": case_id, "created_at": _now(), "revision": 0,
                "patient_token_digest": patient_token_digest, "alias": "PA-" + secrets.token_hex(8),
                "catalog": self.catalog.name, "synthetic": synthetic, "trial_ids": ids,
                "requests": [], "responses": [], "evaluations": [], "events": [], "trace": sink,
                "documents": [], "verifications": [], "judgments": []}
        with self._scope(data, session):  # 문서·판단이 아직 없음: 어떤 증명도 인정되지 않음
            with self.llm_scope(f"followup-{case_id[3:15]}-r1"):
                session.profile_patient()
                self._match(session, ids)
                session.finalize()
            self._event(data, "case_created", "patient", trials=len(ids))
            self._record(data, session, OpsState(), {"type": "intake", "by": "patient"}, prev_pairs=None,
                         prev_criteria=None)
        return self._commit(data, None)

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
            with self._scope(data, session):
                ops = codec.load_ops(data["ops"])
                _, co = self._cohort(session, data["alias"], ops)
                plan = self._plan(co, data["requests"], self._plan_gate(data, session))
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
                    if any(r["status"] == "document_unavailable" for r in same):
                        raise FollowupError(409, "already_unavailable", "기록을 구할 수 없다고 표시한 확인입니다. 대체 경로를 쓰세요.")
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
                    data = self._commit(data, base)
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
            sink: list[dict] = []
            session = self._restore(data, sink)
            with self._scope(data, session):
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
                co = self._settle(co, data["requests"], action,
                                  ActionOutcome(action_id=action.action_id, status=status, error_code=code))
                ops = co.ops[data["alias"]]
                facts_before = {f.fact_id for f in session.fact_store.facts(session.case.patient_id)}
                with self.llm_scope(f"followup-{case_id[3:15]}-r{base + 1}"):
                    session, reeval = self._reevaluate(session, req["target_criteria"], data["alias"], ops, sink)
                data.setdefault("trace", []).extend(reeval.pop("trace"))  # 채택한 결과(증분 또는 전체)의 판정 기록
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
                data = self._commit(data, base)
                return {"duplicate": False, "response": resp.model_dump(mode="json"), "request": req,
                        "routed": routed, "evaluation": rec.model_dump(mode="json"), "case": data}

    def _route_after_failure(self, data: dict, failed: dict, session, ops: OpsState) -> list[dict]:
        """'모름'·연락 불가·문서 없음 뒤: 같은 질문을 반복하지 않고 정책 엔진의 대체 경로를 새 요청으로.

        대체 정보원(문서) 제안이 있으면 그 요청, 남은 정보원이 없으면 임상 검토 요청 (완료 상태는 만들지 않음)."""
        targets = set(failed["target_criteria"])
        key = failed.get("info_key")
        live = {r["proposal_id"] for r in data["requests"] if r["status"] in OPEN_STATUSES and r.get("proposal_id")}
        _, co = self._cohort(session, data["alias"], ops)
        plan = self._plan(co, data["requests"], self._plan_gate(data, session))
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

    def _conflict_to_clinician(self, data: dict, failed: dict, session, ops: OpsState) -> list[dict]:
        """충돌을 확인할 기록이 없음: 같은 확인을 다시 만들지 않고 임상 검토로 (어느 값도 채택하지 않음)."""
        r = self._new_request(data, kind="clinician_review", status="requires_clinician", by="system",
                              reason_code="conflict_unresolved", action=None, targets=list(failed["target_criteria"]),
                              info_key=failed.get("info_key"), source_id="clinician_review",
                              follows=failed["request_id"])
        self._refresh_current(data, session, ops)
        return [r]

    def _refresh_current(self, data: dict, session, ops: OpsState) -> None:
        _, co = self._cohort(session, data["alias"], ops)
        data["current"]["proposals"], data["current"]["held"] = self._proposals(data, session, co)

    # ------------------------------------------------------------------ 증분·전체 재평가
    def _reevaluate(self, session, targets: list[str], alias: str, ops: OpsState, sink: list[dict], *,
                    reprofile: bool = True, before=None):
        """reprofile=False: 새 원문이 없는 변경(문서 증명·임상의 판단). 이미 바뀐 사실은 before(변경 전 스냅샷)와 비교."""
        pid = session.case.patient_id
        prev = {t: m.model_copy(deep=True) for t, m in session.matches.items()}
        before = before or take_snapshot(session.fact_store, pid)
        if reprofile:
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
        sink.clear()
        if affected:
            self._match(session, affected)
        kept = list(sink)
        equivalence: dict = {"status": "not_checked"}
        if self.full_check:
            full_sink: list[dict] = []
            full = codec.restore_session(pre_match, self.catalog.trials, self.catalog.parsed,
                                         tracer=_sink_tracer(full_sink))
            self._match(full, list(prev))
            diffs = sorted(t for t in prev if _match_signature(session.matches[t]) != _match_signature(full.matches[t]))
            inc_pairs = self._pairs(self._cohort(session, alias, ops)[1])
            full_pairs = self._pairs(self._cohort(full, alias, ops)[1])
            pair_diffs = sorted(t for t in prev if inc_pairs.get(t, {}).get("status") != full_pairs.get(t, {}).get("status"))
            if diffs or pair_diffs:
                equivalence = {"status": "mismatch_full_adopted", "trials": diffs, "pairs": pair_diffs}
                session, kept = full, full_sink  # 증분 결과를 조용히 쓰지 않음: 전체 재평가 결과로
            else:
                equivalence = {"status": "match", "compared_trials": len(prev)}
        session.tracer = _sink_tracer([])  # 이후 기록(finalize 등)은 판정 기록에 넣지 않음
        session.finalize()
        return session, {"reevaluated": affected, "reused": reused, "impact": impact.summary(),
                         "equivalence": equivalence, "trace": kept}

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
            with self._scope(data, session):
                ops = codec.load_ops(data["ops"])
                prev_pairs = data["current"]["pairs"]
                failed: list[dict] = []
                if status in ("unreachable", "document_unavailable"):
                    group = [req]
                    if status == "unreachable":  # 연락 불가는 정보원 전체: 같은 환자의 다른 대기 질문도 함께
                        group += [r for r in data["requests"] if r is not req and r["kind"] == "ask_patient"
                                  and r["status"] == "pending"]
                    _, co = self._cohort(session, data["alias"], ops)
                    for r in group:
                        if r.get("action") and r["kind"] != "conflict_resolution":  # 충돌 확인은 정보원 실패가 아님
                            a = Action.model_validate(r["action"])
                            co = self._settle(co, data["requests"], a, ActionOutcome(action_id=a.action_id, status="failed",
                                                                                     error_code=UNREACHABLE))
                        self._set_status(r, status, by if r is req else "system",
                                         "staff_marked" if r is req else "source_unreachable")
                        failed.append(r)
                    ops = co.ops.get(data["alias"], ops)
                else:
                    self._set_status(req, status, by, "staff_marked")
                rec = self._record(data, session, ops, {"type": "staff_status", "request_id": request_id, "status": status},
                                   prev_pairs=prev_pairs, prev_criteria=_criteria_snapshot(session))
                routed = [x for r in failed for x in (self._conflict_to_clinician(data, r, session, ops)
                                                      if r["kind"] == "conflict_resolution"
                                                      else self._route_after_failure(data, r, session, ops))]
                self._event(data, "request_status", by, request_id=request_id, status=status,
                            routed=[r["request_id"] for r in routed])
                return {"request": req, "routed": routed, "evaluation": rec.model_dump(mode="json"),
                        "case": self._commit(data, base)}

    # ------------------------------------------------------------------ 10. 문서 등록·검증, 임상의 판단 (합성 전용 권한)
    _NOT_CONFIGURED = {
        "verification_not_configured": "문서 검증·임상의 판단 권한이 이 서버에 설정되지 않았습니다. 운영 의료기관 인증 연동은 아직 없습니다.",
        "production_authority_unavailable": "운영 검증 권한(의료기관 인증·권한 연동)은 구현되지 않았습니다. 완료를 기록할 수 없습니다.",
    }

    def _require_authority(self, data: dict, actor: str, action: str, request_id: str | None = None):
        cfg = self.verification_config()
        problems = cfg.problems()
        if problems:
            code = problems[0] if problems[0] in self._NOT_CONFIGURED else "verification_misconfigured"
            raise FollowupError(503, code, self._NOT_CONFIGURED.get(code, "검증 권한 설정이 올바르지 않습니다."))
        if not data.get("synthetic"):
            raise FollowupError(403, "synthetic_authority_real_case",
                                "합성 시험 전용 검증 권한은 실제 시험 저장소 사례에 쓸 수 없습니다. 운영 검증 권한은 없습니다.")
        return cfg

    @contextlib.contextmanager
    def _audited(self, case_id: str, actor: str, action: str, request_id: str | None = None):
        """권한 확인을 통과한 사람의 거부된 시도를 감사 기록에 남김 (설정 없음 503은 사례와 무관하므로 제외)."""
        try:
            yield
        except FollowupError as e:
            if e.status != 503 and e.code != "case_not_found":
                self.audit.denied(case_id, actor=actor, action=action, code=e.code, request_id=request_id)
            raise

    def register_document(self, case_id: str, *, content: bytes, media_type: str, kind: str, staff_id: str,
                          expected_revision: int | None = None) -> dict:
        """연구 담당자가 합성 사례에 문서를 등록 (서버가 내용과 SHA-256을 보관). 판정은 바뀌지 않음."""
        by = f"staff:{staff_id}"
        with self.store.lock(case_id), self._audited(case_id, by, "register_document"):
            data = self._load(case_id)
            base = data["revision"]
            self._require_authority(data, by, "register_document")
            if expected_revision is not None and expected_revision != base:
                raise FollowupError(409, "stale_revision", "다른 변경이 먼저 저장되었습니다. 화면을 새로 고치세요.")
            if kind not in verification.DOCUMENT_KINDS:
                raise FollowupError(400, "bad_document_kind", "지원하지 않는 문서 종류입니다.")
            if media_type not in verification.MEDIA_TYPES:
                raise FollowupError(400, "bad_media_type", "지원하지 않는 파일 형식입니다.")
            doc_id, sha = self.documents.put(case_id, content)
            doc = {"document_id": doc_id, "sha256": sha, "size": len(content), "media_type": media_type, "kind": kind,
                   "registered_by": by, "registered_at": _now()}
            data.setdefault("documents", []).append(doc)
            self._event(data, "document_registered", by, document_id=doc_id, sha256=sha, document_kind=kind,
                        size=len(content))
            return {"document": doc, "case": self._commit(data, base)}

    def document_content(self, case_id: str, document_id: str) -> tuple[bytes, dict]:
        data = self._load(case_id)
        doc = next((d for d in data.get("documents", []) if d["document_id"] == document_id), None)
        if doc is None:
            raise FollowupError(404, "document_not_found", "문서를 찾을 수 없습니다.")
        try:
            return self.documents.get(case_id, document_id, doc["sha256"]), doc
        except KeyError:
            raise FollowupError(404, "document_not_found", "문서를 찾을 수 없습니다.") from None
        except ValueError:
            raise FollowupError(409, "document_integrity", "저장된 문서의 해시가 등록 기록과 다릅니다.") from None

    @staticmethod
    def _allowed_fields(ds, req: dict) -> set[str]:
        """요청 범위의 사실 항목: 요청 항목 키 + 대상 기준의 항목 (다른 항목의 사실은 이 요청으로 검증하지 않음)."""
        keys = set(req["target_criteria"])
        out = {req["info_key"]} if req.get("info_key") else set()
        for c in _criteria(ds):
            if f"{c.trial_id}:{c.rule_id}" in keys:
                out |= set(c.fields or [])
                if c.info_key:
                    out.add(c.info_key)
        return out

    @staticmethod
    def _fact_view(f, backed: bool) -> dict:
        return {"fact_id": f.fact_id, "field": f.field, "value": f.value, "unit": f.unit, "observed_at": f.observed_at,
                "source_type": f.source_type, "source_reference": f.source_reference.model_dump(),
                "verification_status": f.verification_status, "document_verified": backed,
                "conflict_status": f.conflict_status}

    def verifier_view(self, case_id: str) -> dict:
        """문서 검증자 화면: 열린 문서 검토 요청, 요청마다 검증할 수 있는 사실, 등록된 문서. 환자 원문은 넣지 않음."""
        from ..fact_store import document_backed
        data = self._load(case_id)
        session = self._restore(data)
        with self._scope(data, session):
            ds, _ = self._cohort(session, data["alias"], codec.load_ops(data["ops"]))
            facts = session.fact_store.facts(session.case.patient_id)
            items = []
            for r in data["requests"]:
                if r["kind"] not in ("document_review", "conflict_resolution"):
                    continue
                allowed = self._allowed_fields(ds, r)
                items.append({"request_id": r["request_id"], "kind": r["kind"], "status": r["status"],
                              "verifiable": r["kind"] == "document_review" and r["status"] == "requires_document",
                              "info_key": r.get("info_key"), "source_id": r.get("source_id"),
                              "target_criteria": r["target_criteria"], "criteria": self._criteria_text(session, r),
                              "allowed_fields": sorted(allowed),
                              "facts": [self._fact_view(f, document_backed(f)) for f in facts if f.field in allowed]})
        return {"case_id": case_id, "revision": data["revision"], "synthetic": data["synthetic"],
                "documents": data.get("documents", []), "requests": items,
                "verifications": data.get("verifications", [])}

    @staticmethod
    def _criteria_text(session, req: dict) -> list[dict]:
        out = []
        for key in req["target_criteria"]:
            tid, rid = key.split(":", 1)
            p = session.parsed.get(tid)
            rule = next((x for x in p.rules if x.rule_id == rid), None) if p else None
            out.append({"criterion": key, "kind": rule.kind if rule else None, "text": rule.text if rule else None,
                        "clinician_reason": expert_boundary.clinician_reason(tid, rule) if rule else None,
                        "required_evidence": matcher._meta(p, rule).required_evidence if rule else None})
        return out

    def verify_document(self, case_id: str, request_id: str, *, verifier_id: str, document_id: str,
                        document_sha256: str, items: list[dict], expected_revision: int | None = None) -> dict:
        """문서 검증: 등록된 문서로 요청 범위의 사실을 확인하고 서명된 증명을 남긴 뒤 영향받은 기준을 재평가.

        items: {"fact_id"} = 기존 사실이 문서와 같음 / {"field", "value", "unit"?, "observed_at"?} = 문서의 값을 새 사실로
        (출처 clinical_record, 참조 = 이 문서). 증명은 사실 값에 묶이므로 값이 다른 사실로 옮겨지지 않음."""
        from ..fact_store import FactStoreError, _norm_field, document_backed
        by = f"verifier:{verifier_id}"
        with self.store.lock(case_id), self._audited(case_id, by, "verify_document", request_id):
            data = self._load(case_id)
            base = data["revision"]
            self._require_authority(data, by, "verify_document", request_id)
            if expected_revision is not None and expected_revision != base:
                raise FollowupError(409, "stale_revision", "다른 변경이 먼저 저장되었습니다. 화면을 새로 고치세요.")
            req = next((r for r in data["requests"] if r["request_id"] == request_id), None)
            if req is None:
                raise FollowupError(404, "request_not_found", "요청을 찾을 수 없습니다.")
            if req["kind"] == "conflict_resolution":
                raise FollowupError(409, "conflict_not_closed_by_document",
                                    "충돌 확인은 문서 검증으로 닫지 않습니다 (어느 값도 채택하지 않음). 문서 검토 요청을 쓰세요.")
            if req["kind"] != "document_review":
                raise FollowupError(409, "not_document_review", "문서 검토 요청이 아닙니다.")
            if req["status"] != "requires_document":
                raise FollowupError(409, "request_closed", "이미 처리된 요청입니다.")
            doc = next((d for d in data.get("documents", []) if d["document_id"] == document_id), None)
            if doc is None:
                raise FollowupError(404, "document_not_found", "이 사례에 등록된 문서가 아닙니다.")
            if doc["sha256"] != document_sha256:
                raise FollowupError(409, "document_changed", "확인한 문서의 해시가 등록 기록과 다릅니다.")
            self.document_content(case_id, document_id)  # 디스크의 내용도 같은지
            if not items:
                raise FollowupError(400, "no_items", "확인한 사실을 하나 이상 고르세요.")
            sink: list[dict] = []
            session = self._restore(data, sink)
            ops = codec.load_ops(data["ops"])
            pid = session.case.patient_id
            with self._scope(data, session):
                authority = document_authority.current()
                prev_pairs, prev_criteria = data["current"]["pairs"], _criteria_snapshot(session)
                ds, _ = self._cohort(session, data["alias"], ops)
                allowed = self._allowed_fields(ds, req)
                todo = []
                for it in items:  # 먼저 모두 검사 (일부만 검증하지 않음)
                    if it.get("fact_id"):
                        try:
                            f = session.fact_store.get(it["fact_id"])
                        except FactStoreError:
                            raise FollowupError(404, "fact_not_found", "이 사례의 사실이 아닙니다.") from None
                        if f.patient_id != pid:
                            raise FollowupError(404, "fact_not_found", "이 사례의 사실이 아닙니다.")
                        if f.field not in allowed:
                            raise FollowupError(409, "field_out_of_scope", "이 요청이 확인하는 항목의 사실이 아닙니다.")
                        if document_backed(f):
                            raise FollowupError(409, "already_verified", "이미 문서로 확인된 사실입니다.")
                        todo.append(("confirm", f.fact_id, None))
                    else:
                        try:
                            name = _norm_field(it.get("field") or "")
                        except FactStoreError:
                            raise FollowupError(400, "bad_field", "항목 이름이 올바르지 않습니다.") from None
                        if name not in allowed:
                            raise FollowupError(409, "field_out_of_scope", "이 요청이 확인하는 항목이 아닙니다.")
                        todo.append(("record", None, {**it, "field": name}))
                if len({t[1] for t in todo if t[1]}) != sum(1 for t in todo if t[1]):
                    raise FollowupError(400, "duplicate_items", "같은 사실을 두 번 고를 수 없습니다.")
                before = take_snapshot(session.fact_store, pid)
                done = []
                for mode, fid, it in todo:
                    if mode == "record":
                        try:
                            fid = session.fact_store.add(
                                pid, it["field"], it.get("value"), unit=it.get("unit"), source_type="clinical_record",
                                source_reference={"kind": "document", "ref": document_id},
                                observed_at=it.get("observed_at"), session_id=session.session_id).fact_id
                        except FactStoreError:
                            raise FollowupError(400, "bad_value", "문서 값이 항목 형식에 맞지 않습니다.") from None
                    f = session.fact_store.get(fid)
                    att = authority.issue(f, document_id, verifier_id)
                    session.fact_store.attest(fid, att, authority, reason=f"document_review:{request_id}", by=by)
                    done.append({"fact_id": fid, "field": f.field, "mode": mode})
                self._project_recorded(session, [d["field"] for d in done if d["mode"] == "record"], document_id)
                with self.llm_scope(f"followup-{case_id[3:15]}-r{base + 1}"):
                    session, reeval = self._reevaluate(session, req["target_criteria"], data["alias"], ops, sink,
                                                       reprofile=False, before=before)
                data.setdefault("trace", []).extend(reeval.pop("trace"))
                vid = "DV-" + secrets.token_hex(8)
                self._set_status(req, "document_verified", by, "document_attested")
                rec = self._record(data, session, ops, {"type": "document_verification", "request_id": request_id,
                                                        "verification_id": vid},
                                   prev_pairs=prev_pairs, prev_criteria=prev_criteria, reeval=reeval)
                ver = {"verification_id": vid, "request_id": request_id, "document_id": document_id,
                       "document_sha256": document_sha256, "verifier": verification.DOC_VERIFIER_PREFIX + verifier_id,
                       "by": by, "at": _now(), "items": done, "revision_after": rec.revision,
                       "authority": "synthetic"}
                data.setdefault("verifications", []).append(ver)
                self._event(data, "document_verified", by, request_id=request_id, verification_id=vid,
                            document_id=document_id, document_sha256=document_sha256,
                            fact_ids=[d["fact_id"] for d in done])
            return {"verification": ver, "request": req, "evaluation": rec.model_dump(mode="json"),
                    "case": self._commit(data, base)}

    @staticmethod
    def _project_recorded(session, fields: list[str], document_id: str | None = None) -> None:
        """문서 검증자가 문서에서 기록한 값(record)을 Matcher 입력(PatientProfile)과 원문에 넣음.

        Matcher·규칙 엔진은 사실 저장소가 아니라 Session.profile을 읽음. 확인 모드(confirm)는 이미 프로필에 있는 값의
        사실을 문서로 확인하지만, 기록 모드로만 들어온 값(환자가 말한 적 없는 검사값)은 프로필에 없어서 기준이 계속
        '정보 없음'으로 남았음. P 근거 경로 뒤에는 진료 기록 필요 기준을 환자에게 묻지 않으므로 이 값이 들어오는 길은 문서뿐임.
        값은 사실 저장소의 결정적 투영(fact_view.FactEvidenceView.to_profile, WP6 평가기와 같은 함수)에서만 가져옴:
        같은 항목의 사실 값이 맞지 않거나(충돌) 검체가 맞지 않으면 투영에 없으므로 넣지 않음 (충돌은 기존 경로로 보류·충돌 확인).

        원문에는 답변 구역(evidence.ANSWER_MARKER)으로 문서 확인 값 한 줄을 붙이고 그 줄을 인용으로 씀. 그래서 Matcher의
        출처 판정은 확인 답변과 같은 자기 보고 수준(self_report)이 되어 진료 기록 필요 기준은 그대로 보류되고, 확인 모드와 같은
        기존 해제 경로(evidence_basis.release: 서버 권한이 인정한 문서 확인 사실과 값이 이어질 때만)로만 판정이 됨.
        권한이 없거나 바뀌면 해제되지 않음. matcher·evidence_basis(보호·안전 계층)는 바꾸지 않음. LLM을 부르지 않음."""
        if not fields or session.profile is None:
            return
        from ..fact_view import FactEvidenceView
        proj = FactEvidenceView(session.fact_store, session.case.patient_id).to_profile()
        prof, update = session.profile, {}
        labs = list(prof.labs)
        lines = []
        for name in fields:
            if name.startswith("lab:"):
                lab = next((x for x in proj.labs if x.name.casefold() == name[4:].casefold()), None)
                if lab is not None:
                    value = f"{lab.value:g}"
                    line = f"{lab.name} {value} {lab.unit or ''}".strip()
                    lines.append(line + (f" ({lab.date})" if lab.date else ""))
                    lab = lab.model_copy(update={"source_quote": line})
                    labs = [x for x in labs if x.name.casefold() != lab.name.casefold()] + [lab]
            # 나이·ECOG·성별도 검사값처럼 확인 줄을 붙임: Matcher는 프로필 구조화 값이 원문(최초 입력·답변)에 적혀 있는지로
            # 출처를 정하므로, 줄이 없으면 문서로 기록한 값이 '출처 불명'으로 남아 해제 경로(release)를 타지 못했음
            elif name == "age" and proj.age_value is not None:
                update.update(age=proj.age, age_value=proj.age_value, age_unit=proj.age_unit)
                lines.append(f"Age {proj.age_value:g} {proj.age_unit or 'years'}")
            elif name == "ecog" and proj.ecog is not None:
                update["ecog"] = proj.ecog
                lines.append(f"ECOG {proj.ecog}")
            elif name in ("primary_diagnosis", "disease_stage") and getattr(proj, name) is not None:
                update[name] = getattr(proj, name)
            elif name == "sex" and proj.sex != "unknown":
                update["sex"] = proj.sex
                lines.append(f"Sex {proj.sex}")
        update["labs"] = labs
        session.profile = prof.model_copy(update=update)
        if lines:
            session.case.raw_text += (f"\n\n[추가 확인 정보 - 문서 확인 {document_id or ''}]\n"
                                      "Q: 문서 검증자가 문서에서 기록한 값\nA: " + "\n".join(lines))

    def clinician_view(self, case_id: str) -> dict:
        """임상의 화면: 임상 검토 요청, 기준 원문, 판단할 수 있는지, 환자 사실(출처·문서 확인 여부), 이전 판단과 상태."""
        from ..fact_store import document_backed
        data = self._load(case_id)
        session = self._restore(data)
        with self._scope(data, session):
            auth = clinician_authority.current()
            items = [{"request_id": r["request_id"], "status": r["status"], "reason_code": r["reason_code"],
                      "follows": r.get("follows"), "criteria": self._criteria_text(session, r),
                      "judgeable": r["status"] == "requires_clinician" and bool(r["target_criteria"]) and all(
                          c["clinician_reason"] for c in self._criteria_text(session, r))}
                     for r in data["requests"] if r["kind"] == "clinician_review"]
            facts = [self._fact_view(f, document_backed(f)) for f in session.fact_store.facts(session.case.patient_id)]
            judgments = [{**j, "recognition": self._judgment_status(auth, j)} for j in data.get("judgments", [])]
            # 쌍 상태는 담당자·환자 화면(refresh_pairs)과 같은 지금 권한 기준 (권한 철회 뒤 저장된 확정을 보이지 않음).
            # 위 항목·사실을 다 만든 뒤 부름 (_live_current가 session.matches를 다시 막힌 사본으로 바꿈)
            live = self._live_current(data, session) if (data.get("verifications") or data.get("judgments")) else None
        return {"case_id": case_id, "revision": data["revision"], "synthetic": data["synthetic"],
                "pairs": (live or data["current"])["pairs"], "requests": items, "facts": facts, "judgments": judgments}

    @staticmethod
    def _judgment_status(auth, j: dict) -> str:
        if isinstance(auth, verification.SyntheticCaseClinicianAuthority):
            return auth.status(clinician_authority.ClinicianJudgment.model_validate(j))
        return "not_recognized"  # 권한 없음 (운영 모드·설정 없음·실제 사례)

    def clinician_judgment(self, case_id: str, request_id: str, *, clinician_id: str, decisions: list[dict],
                           expected_revision: int | None = None) -> dict:
        """임상의 판단: 요청의 모든 대상 기준이 임상의 판단 기준이어야 하고 한 번에 모두 판단. 서명된 판단 -> 재평가.

        decisions: [{"criterion": "<시험>:<기준>", "decision": "MET"|"NOT_MET", "rationale_code": "<코드>",
                     "basis_fact_ids": ["F-..."]}]. 근거 사실은 이 환자의 사실이어야 하고, 진료 기록이 필요한 기준
        (matcher._meta의 required_evidence=clinical_record)이면 하나 이상이며 모두 지금 문서로 확인된 사실이어야 함:
        임상의 판단이 문서 확인 요건을 대신하지 않음.
        판단은 그때의 사실에 묶여, 이후 사실이 바뀌면 인정되지 않고 기준은 다시 판단 대기로 돌아감."""
        by = f"clinician:{clinician_id}"
        with self.store.lock(case_id), self._audited(case_id, by, "clinician_judgment", request_id):
            data = self._load(case_id)
            base = data["revision"]
            self._require_authority(data, by, "clinician_judgment", request_id)
            if expected_revision is not None and expected_revision != base:
                raise FollowupError(409, "stale_revision", "다른 변경이 먼저 저장되었습니다. 화면을 새로 고치세요.")
            req = next((r for r in data["requests"] if r["request_id"] == request_id), None)
            if req is None:
                raise FollowupError(404, "request_not_found", "요청을 찾을 수 없습니다.")
            if req["kind"] != "clinician_review":
                raise FollowupError(409, "not_clinician_review", "임상 검토 요청이 아닙니다.")
            if req["status"] != "requires_clinician":
                raise FollowupError(409, "request_closed", "이미 처리된 요청입니다.")
            by_key = {d.get("criterion"): d for d in decisions}
            if len(by_key) != len(decisions) or set(by_key) != set(req["target_criteria"]) or not by_key:
                raise FollowupError(409, "judgment_incomplete", "요청의 대상 기준을 모두, 한 번씩 판단해야 합니다.")
            sink: list[dict] = []
            session = self._restore(data, sink)
            ops = codec.load_ops(data["ops"])
            for key, d in by_key.items():
                tid, rid = key.split(":", 1)
                p = session.parsed.get(tid)
                rule = next((x for x in p.rules if x.rule_id == rid), None) if p else None
                if rule is None or expert_boundary.clinician_reason(tid, rule) is None:
                    raise FollowupError(409, "not_clinician_criterion",
                                        "임상의 판단 기준이 아닙니다 (문서·환자 답으로 확인할 기준은 임상의 판단으로 확정하지 않음).")
                if d.get("decision") not in ("MET", "NOT_MET"):
                    raise FollowupError(400, "bad_decision", "decision은 MET 또는 NOT_MET입니다.")
            from ..fact_store import FactStoreError, document_backed
            with self._scope(data, session):
                auth = clinician_authority.current()
                prev_pairs, prev_criteria = data["current"]["pairs"], _criteria_snapshot(session)
                pid = session.case.patient_id
                for key, d in by_key.items():  # 근거 사실 검사 (모두 통과해야 기록)
                    tid, rid = key.split(":", 1)
                    rule = next(x for x in session.parsed[tid].rules if x.rule_id == rid)
                    basis = list(dict.fromkeys(d.get("basis_fact_ids") or []))
                    for fid in basis:
                        try:
                            f = session.fact_store.get(fid)
                        except FactStoreError:
                            raise FollowupError(404, "fact_not_found", "이 사례의 사실이 아닙니다.") from None
                        if f.patient_id != pid:
                            raise FollowupError(404, "fact_not_found", "이 사례의 사실이 아닙니다.")
                    if matcher._meta(session.parsed[tid], rule).required_evidence == "clinical_record" and (
                            not basis or not all(document_backed(session.fact_store.get(i)) for i in basis)):
                        raise FollowupError(409, "basis_not_document_verified",
                                            "진료 기록이 필요한 기준입니다. 판단 근거로 문서로 확인된 사실을 하나 이상 고르세요 "
                                            "(문서로 확인되지 않은 사실은 근거로 쓸 수 없음).")
                    d["basis_fact_ids"] = basis
                made = []
                for key, d in by_key.items():
                    tid, rid = key.split(":", 1)
                    try:
                        j = auth.issue(trial_id=tid, rule_id=rid, decision=d["decision"], clinician_id=clinician_id,
                                       rationale_code=d.get("rationale_code") or "",
                                       basis_fact_ids=d["basis_fact_ids"])
                    except ValueError:
                        raise FollowupError(400, "bad_rationale_code", "rationale_code는 영문 소문자·숫자·_ 1~64자입니다.") from None
                    made.append(j)
                auth.judgments.extend(made)
                with self.llm_scope(f"followup-{case_id[3:15]}-r{base + 1}"):
                    session, reeval = self._reevaluate(session, req["target_criteria"], data["alias"], ops, sink,
                                                       reprofile=False)
                data.setdefault("trace", []).extend(reeval.pop("trace"))
                self._set_status(req, "clinician_decided", by, "clinician_judged")
                data.setdefault("judgments", []).extend(j.model_dump(mode="json") for j in made)
                rec = self._record(data, session, ops, {"type": "clinician_judgment", "request_id": request_id,
                                                        "judgment_ids": [j.judgment_id for j in made]},
                                   prev_pairs=prev_pairs, prev_criteria=prev_criteria, reeval=reeval)
                self._event(data, "clinician_judged", by, request_id=request_id,
                            judgments=[{"judgment_id": j.judgment_id, "criterion": f"{j.trial_id}:{j.rule_id}",
                                        "decision": j.decision, "fact_version": j.fact_version} for j in made])
            return {"judgments": [j.model_dump(mode="json") for j in made], "request": req,
                    "evaluation": rec.model_dump(mode="json"), "case": self._commit(data, base)}

    def verification_summary(self, data: dict, session) -> dict:
        """연구 담당자·Inspector용 (권한 범위 안에서 부름): 문서, 검증, 판단과 지금 인정 여부, 권한 방식."""
        from ..fact_store import document_backed
        cfg = self.verification_config()
        auth = clinician_authority.current()
        facts = session.fact_store.facts(session.case.patient_id)
        return {"authority": {"mode": cfg.mode or "none", "active": cfg.synthetic and bool(data.get("synthetic")),
                              "synthetic_only": True, "production_authority": False, "problems": cfg.problems()},
                "documents": data.get("documents", []), "verifications": data.get("verifications", []),
                "judgments": [{**j, "recognition": self._judgment_status(auth, j)} for j in data.get("judgments", [])],
                "document_verified_facts": [f.fact_id for f in facts if document_backed(f)],
                "verification_status_only_facts": [f.fact_id for f in facts if f.verification_status == "document_verified"
                                                   and not document_backed(f)]}

    def refresh_pairs(self, data: dict) -> dict:
        """보기 직전에 쌍 상태를 지금 권한으로 다시 계산 (판정은 다시 하지 않음). 저장된 current.pairs는 그때의 권한 기준이라,
        서명 비밀이 바뀌었거나 운영 모드로 바뀐 서버에서 문서·임상의 근거 확정이 그대로 보이지 않게 함.

        문서·판단 기록이 없는 사례는 권한에 따라 달라질 것이 없으므로 저장값을 그대로 씀."""
        if not (data.get("verifications") or data.get("judgments")):
            return data
        session = self._restore(data)
        with self._scope(data, session):
            live = self._live_current(data, session)
        return data if live is None else {**data, "current": live}

    def _live_current(self, data: dict, session) -> dict | None:
        """지금 권한 기준의 current (쌍 상태·추천 화면·제안). 저장값과 쌍 상태가 같으면 None. 반드시 _scope 안에서 부름.

        쌍 상태만 바꾸고 저장된 추천 화면을 그대로 두면 '쌍은 확인 대기인데 추천은 공개 기준 충족'이 되고 (단일 판정 상태 계약
        위반, 환자 화면에도 보임), 저장된 제안을 그대로 두면 사례 화면의 다음 행동이 Cohort C 대기열(staff_queue는 지금 권한으로
        계획)과 달라짐. 같은 권한으로 판정 사본을 다시 막고 추천 화면을 다시 만들며 (LLM 없음), 제안도 같은 cohort 상태에서
        다시 계획함 (create_requests가 검사하는 '지금 상태의 제안'과 같은 계획). 저장하지 않음: 저장된 current는 그 평가 때의 기록.
        끝나면 session.matches는 지금 권한으로 다시 막힌 판정 (Inspector가 그대로 씀)."""
        _, co = self._cohort(session, data["alias"], codec.load_ops(data["ops"]))
        live = self._pairs(co)
        if live == data["current"]["pairs"]:
            return None
        proposals, held = self._proposals(data, session, co)
        view = self._regated_view(session)
        return {**data["current"], "pairs": live, "view": view, "proposals": proposals, "held": held,
                "stored_pairs": data["current"]["pairs"], "authority_changed": True}

    def _regated_view(self, session) -> dict:
        """지금 권한으로 저장된 판정의 해제·판단을 다시 확인한 결과 화면. 반드시 그 사례의 _scope 안에서 부름.

        - 문서 확인으로 해제(RELEASED_FLAG)된 자기 보고 판정: 지금 권한의 문서 확인(document_check)을 다시 통과해야 유지,
          아니면 처음처럼 보류(evidence_basis.hold)
        - 임상의 판정: 지금 권한이 인정해야 유지 (_view와 같은 unrecognized_to_unknown)
        추천은 finalize와 같은 결정적 경로(conservative_view 보류 -> recommender._validate -> 보류 표시)로 다시 만들고, 저장된
        추천의 근거 문장만 재사용함 (LLM을 다시 부르지 않음). 판정 상태·순서·그룹은 모두 다시 계산됨."""
        from .. import conservative_view as boundary
        from ..agents import recommender
        from ..schemas import RecommendationDraft
        pid = session.case.patient_id
        self._regate_matches(session)
        if session.recommendation is not None:
            old = session.recommendation.model_copy(deep=True)
            holds = boundary.evaluate(session)[0]
            matches = boundary.held_matches(session, holds)
            draft = RecommendationDraft(patient_id=pid, ranked=old.ranked, excluded=old.excluded,
                                        overall_comment=old.overall_comment or "")
            evaluated = {t: session.trials[t] for t in session.matches}
            session.recommendation = boundary.held_recommendation(
                recommender._validate(draft, pid, matches, evaluated, session.profile, session.qa_log), session, holds)
        return self._result_view(session)

    def _regated_view_if_changed(self, session) -> dict | None:
        """_record용: 지금 권한으로 다시 막을 판정이 있으면 다시 막은 결과 화면, 없으면 None (기존 _result_view 경로 그대로).
        저장할 Session을 dump한 뒤에 부름 (저장 판정은 바꾸지 않음). 끝나면 session.matches·recommendation을 원래대로 돌려
        _record 뒤의 같은 요청 처리(대체 경로 등)가 기존과 같은 Session을 봄."""
        original, rec = dict(session.matches), session.recommendation
        try:
            if not self._regate_matches(session):
                return None
            session.matches = dict(original)
            return self._regated_view(session)
        finally:
            session.matches, session.recommendation = original, rec

    def _regate_matches(self, session) -> bool:
        """session.matches를 지금 권한으로 다시 막은 판정 사본으로 바꿈 (_regated_view의 판정 부분). 바뀐 것이 있으면 True."""
        from .. import evidence_basis
        pid = session.case.patient_id
        regated = {}
        for tid, m in self._view(session).matches.items():
            p = session.parsed.get(tid)
            if p is None or m.eligibility == "NOT_EVALUATED":
                regated[tid] = m
                continue
            rules = {r.rule_id: r for r in p.rules}
            check = evidence_basis.document_check(session.fact_store, pid, p, session.reference_date)
            allx = m.assessments + m.other_cohort_assessments
            after = [evidence_basis.hold(a) if (evidence_basis.RELEASED_FLAG in a.validation_flags and a.passes is True
                                                and a.rule_id in rules and not check(a, rules[a.rule_id])) else a
                     for a in allx]
            regated[tid] = (evidence_basis.reaggregate(m, p, after, "권한이 바뀌어 문서 확인 해제를 다시 보류")
                            if any(x is not y for x, y in zip(after, allx)) else m)
        changed = any(regated[t] != session.matches.get(t) for t in regated)
        session.matches = regated
        return changed

    def staff_verification(self, case_id: str) -> dict:
        data = self._load(case_id)
        session = self._restore(data)
        with self._scope(data, session):
            return self.verification_summary(data, session)

    def audit_view(self, case_id: str) -> dict:
        self._load(case_id)
        return {"case_id": case_id, **self.audit.verify(case_id), "entries": self.audit.entries(case_id)}

    # ------------------------------------------------------------------ 보기
    def patient_requests(self, case_id: str) -> dict:
        data = self._load(case_id)
        resp = {r["response_id"]: r for r in data["responses"]}
        out = []
        for r in data["requests"]:
            if r["kind"] not in PATIENT_KINDS or r["status"] in ("superseded", "cancelled") and not r.get("response_id"):
                continue
            item = {"request_id": r["request_id"], "status": r["status"], "created_at": r["created_at"],
                    "question": r["question"], "answerable": r["status"] == "pending", "info_key": r.get("info_key")}
            if r.get("response_id") in resp:
                x = resp[r["response_id"]]
                item["response"] = {"outcome": x["outcome"], "answer": x["answer"], "at": x["at"]}
            out.append(item)
        # 연구진이 서류·의사 판단으로 확인 중인 것 (환자가 할 일은 없음): 종류·항목·상태만, 값·문서·판단 내용은 넣지 않음
        # 확인할 방법이 없어 멈춘 임상 검토는 '의사가 판단 중'이 아니라 on_hold로 (환자에게 기다리라고 하지 않음)
        work = [{"kind": r["kind"], "info_key": r.get("info_key"),
                 "status": "on_hold" if r["status"] == "requires_clinician" and r.get("reason_code") in NO_PATH_REASONS
                 else r["status"]} for r in data["requests"]
                if r["kind"] not in PATIENT_KINDS and r["status"] not in ("superseded", "cancelled")]
        return {"case_id": case_id, "revision": data["revision"], "requests": out,
                "pending": sum(1 for x in out if x["answerable"]), "in_progress": work}

    def inspector_view(self, case_id: str) -> dict:
        from .. import inspector
        data = self._load(case_id)
        session = self._restore(data)
        with self._scope(data, session):
            # 사례 화면(refresh_pairs)과 같은 지금 권한 상태: 권한이 바뀌었으면 다시 막힌 판정·추천, 아니면 저장된 결과
            records = bool(data.get("verifications") or data.get("judgments"))
            live = self._live_current(data, session) if records else None
            if live is None and records:
                self._regate_matches(session)  # 쌍 상태가 같아도 판정 사본은 지금 권한으로 (refresh와 같은 다시 막기)
            elif live is None:
                session.matches = self._view(session).matches  # 보기용 복원본: 인정되지 않은 임상의 판정은 판단 대기로
            current = live or data["current"]
            rec = inspector.record_from_session(session, meta={"llm_backend": "recorded",
                                                               "synthetic": bool(data["synthetic"])})
            rec.result = current["view"]["result"]  # 화면과 같은 보류 적용 결과
            rec.trace = list(data.get("trace", []))  # 평가마다의 matcher done 기록 -> 라운드별 판정 변화
            view = inspector.build_view(rec)
            view["followup"] = {"evaluations": data["evaluations"], "requests": data["requests"],
                                "responses": [{k: v for k, v in x.items() if k != "answer_digest"}
                                              for x in data["responses"]]}
            view["verification"] = self.verification_summary(data, session)
            # 과거 이력(steps 등)은 그대로 두고, 어느 부분이 현재 판정이고 어느 부분이 평가 당시 기록인지만 표시
            view["authority_changed"] = live is not None
            view["record_scope"] = {"current": list(INSPECTOR_CURRENT_KEYS), "history": list(INSPECTOR_HISTORY_KEYS),
                                    "history_notice": INSPECTOR_HISTORY_NOTICE}
        return view

    def readable_cases(self) -> tuple[list[dict], list[str]]:
        """목록·대기열용: 읽을 수 있는 사례와 읽지 못한 사례 id (감사 F-17). 손상된 사례 하나가 목록 전체를 500으로 만들지 않게
        건너뛰고, 조용히 사라지지 않도록 id를 돌려줌 (응답의 unreadable). 원문·예외 메시지는 기록하지 않음 (종류만)."""
        good, bad = [], []
        for cid in self.store.case_ids():
            try:
                data = self.store.load(cid)
                self._check_shape(data)  # JSON은 정상이지만 구조가 깨진 사례 (session·current 없음, requests: null)
                good.append(data)
            except (ValueError, OSError, TypeError) as e:  # JSONDecodeError는 ValueError
                log.warning("followup case unreadable: %s (%s)", cid, type(e).__name__)
                bad.append(cid)
        return good, bad

    @staticmethod
    def _check_shape(data: dict) -> None:
        """목록·대기열이 사례마다 기대는 구조 (감사 F-17). 어긋나면 TypeError (메시지에 내용 없음)."""
        for key, typ in (("session", dict), ("current", dict), ("requests", list), ("alias", str)):
            if not isinstance(data.get(key), typ):
                raise TypeError(key)
        if "ops" not in data or not isinstance(data["current"].get("pairs"), dict) \
                or not isinstance(data["current"].get("proposals"), list):
            raise TypeError("current")
        if not all(isinstance(r, dict) for r in data["requests"]):
            raise TypeError("requests")

    def each_readable_case(self, body: Callable[[dict], object]) -> tuple[list, list[str]]:
        """읽을 수 있는 사례마다 body(data)를 부르고 (결과 목록, 읽지 못한 사례 id)를 돌려줌 (감사 F-17).

        복원·권한 범위·refresh를 포함한 사례별 본문 전체를 사례 단위로 격리: 한 사례에서 예외가 나면 그 사례만 빼고
        id를 unreadable에 더함. 기록은 사례 id와 예외 종류만 (원문·예외 메시지 없음)."""
        cases, bad = self.readable_cases()
        out = []
        for data in cases:
            try:
                out.append(body(data))
            except Exception as e:  # noqa: BLE001 - 사례 하나가 목록 전체를 500으로 만들지 않게
                log.warning("followup case unreadable: %s (%s)", data["case_id"], type(e).__name__)
                bad.append(data["case_id"])
        return out, bad

    def staff_queue(self) -> dict:
        """모든 열린 사례를 한 코호트로 보고 DCV+C가 고른 순서 (Cohort C 우선순위). 판정은 바꾸지 않음."""
        states, ops, case_of, requested, sessions, req_of = {}, {}, {}, [], {}, {}
        gate = {"unproven": set(), "no_basis": {}}  # V4-B1: 사례마다 따로 모아 성공한 사례만 합침 (F-17 격리)

        def one(data):
            session = self._restore(data)
            with self._scope(data, session):  # 사례마다 그 사례의 권한으로 (다른 사례의 증명·판단은 무효)
                view = self._view(session)
                view.matched_versions = {t: view.snapshot.version for t in session.matches}
                state = build_state(view)
                g = self._plan_gate(data, session)  # V4-B1 (그 사례의 _scope 안에서)
            reqs: dict = {}
            for r in data["requests"]:  # 제안마다 첫 요청 (아래 항목의 request_id·status)
                reqs.setdefault(r.get("proposal_id"), (r["request_id"], r["status"]))
            return data, session, state, codec.load_ops(data["ops"]), reqs, g

        done, unreadable = self.each_readable_case(one)
        for data, session, state, op, reqs, g in done:
            gate["unproven"] |= g["unproven"]
            gate["no_basis"].update(g["no_basis"])
            states[data["alias"]] = state
            ops[data["alias"]] = op
            case_of[data["alias"]] = data
            sessions[data["alias"]] = session
            req_of[data["alias"]] = reqs
            requested += data["requests"]
        if not states:
            return {"items": [], "unreadable": unreadable}
        co = CohortState.from_decision_states(states, ops=ops)
        plan = self._plan(co, requested, gate)
        items = []
        for rank, a in enumerate(plan.selected, 1):
            data = case_of[a.patient_alias]
            req = req_of[a.patient_alias].get(a.action_id)
            route = self._route(a)
            items.append({"rank": rank, "case_id": data["case_id"], "proposal_id": a.action_id,
                          "action_type": a.action_type, "route": route[0] if route else None,
                          "source_id": a.source_id, "info_key": a.info_key, "target_criteria": list(a.target_criteria),
                          "reason_code": a.reason_code, "safety_required": a.safety_required,
                          "request_id": req[0] if req else None,
                          "request_status": req[1] if req else None})
        return {"items": items, "cases": len(states), "unreadable": unreadable}
