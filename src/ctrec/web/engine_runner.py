"""WP8 실행기: 실제 Session(웹·CLI)의 확인 질문을 DCV+C 정책 엔진이 고르게 함 (W-1~W-7, M14).

기존 Session.clarify는 질문을 question_generator.generate_questions(LLM)와 acquisition.plan으로 만듭니다. 이 모듈은
question_generator.use_generator(실행별 생성기 교체 장치)로 그 자리만 바꿉니다. pipeline.py(보호 파일)는 고치지 않습니다.

라운드마다 (generate가 불릴 때)
1. settle: 직전 라운드에 보낸 질문의 답을 ActionOutcome으로 만들어 policy.apply_outcome -> 운영 상태(ops) 갱신 (W-6).
   답 판별은 engine.answers.answer_outcome 하나 ("모름"·무응답 = no_answer, 항목 단위 실패). 사실·판정은 바꾸지 않음 -
   답은 기존 경로(답변 블록 -> 재추출·재매칭)를 거쳐서만 판정에 반영됩니다.
2. 현재 Session을 SessionView -> build_state -> CohortState로 읽음 (환자 키는 실행별 salt의 별칭, W-2).
3. policy.propose -> select (오프라인 기록기·시뮬레이터와 같은 정책 객체, M14).
4. 선택된 행동 중 이 화면에서 실행할 수 있는 것만 질문으로 바꿈 (W-3, LLM 없음):
   - ASK_PATIENT, 의사 참고용 CLINICIAN_REVIEW -> question_bridge.render_action
   - REQUEST_DOCUMENT -> 화면 사용자(코디네이터)에게 "기록에서 확인해 입력" 요청. 입력값은 다른 답과 같은
     경로로 들어가므로 문서 검증(document_verified)으로 올라가지 않습니다 (자기 보고 수준 그대로, 안전 쪽).
   - 그 밖(상충 근거의 출처 확인 VERIFY_RECORD, 임상의 판단 CLINICIAN_REVIEW, RUN_TOOL 등)은 이 화면에서 실행하지 않고
     기록만 남김. VERIFY_RECORD는 사실 출처를 코드로 대조하는 안전 확인이라 자유 입력 질문으로 바꾸면 이미 상충한 기준에
     자기 보고 사실만 하나 더 쌓이고, "모름"이면 항목 실패로 잘못 기록됨. 실행하지 못한 안전 필수 행동은
     ("engine", "safety_not_executed")로 따로 알림.
   질문 대상·목적은 행동 그대로이고(check_rendered), 질문 수는 Session이 넘긴 한도를 넘지 않습니다.
5. 안전 필수 행동이 예산 부족으로 실행되지 못하면(Plan.safety_unfunded) 화면에 알리고 그 라운드는 질문하지 않음 (W-7).

질문 루프 (activate). Session의 루프 조건(uncertain)은 acquisition의 열린 항목만 봐서, 마지막 열린 항목을 물으면 정책에 남은
대체 행동(문서 요청, 바꿔 묻기)을 실행하지 않고 끝납니다. 실행 동안 session.uncertain을 recovery_uncertain으로 바꿔 그
행동이 남은 시험도 질문 대상으로 둡니다. 이어 가는 것은 대체 행동뿐이고(새 환자 질문으로 넓히지 않음), 같은 행동은 다시
실행하지 않으며(_repeat_key), 라운드·질문·작업시간 한도는 Session과 정책이 그대로 지킵니다. 한도로 끝나 실행하지 못한 행동은
close가 EngineRunner.unexecuted, trace("engine", "actions_not_executed"), 보고서 끝 절에 남깁니다. 그 쌍의 상태는 바꾸지 않음
(문서가 남아 있으므로 BLOCKED가 아님).

trace에는 행동 유형·정보원·사유 코드·대상 기준·건수만 남깁니다 (행동 ID·별칭·환자 값 없음, W-4).
코디네이터 작업시간 원장(EffortLedger)은 웹 LLM 비용 원장(web/budget.py)과 섞지 않습니다 (W-5).
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field

from ..agents import question_generator
from ..decision_state import SessionView, build_state
from ..engine.answers import answer_outcome
from ..engine.cohort_state import CohortState, OpsState
from ..engine.contracts import NON_EXECUTING, REPHRASE, Action, ActionOutcome, EffortBudget, Plan
from ..engine.cost import EffortLedger
from ..engine.policy import LegacyAdapterPolicy
from ..engine.question_bridge import RenderError, Wording, check_rendered, render_action, renderable
from ..eval_hygiene import AliasMap
from ..schemas import ClarifyingQuestion, QuestionSet

POLICY_MODES = ("legacy", "engine")
ENV_POLICY_MODE = "CTREC_POLICY_MODE"
ENV_POLICY_ID = "CTREC_POLICY_ID"
DEFAULT_POLICY_ID = "legacy_greedy"
# 웹에는 아직 코디네이터 작업시간 예산 입력이 없음 -> 선택이 예산에서 잘리지 않게 하는 값 (실험 예산이 아님)
DEFAULT_BUDGET = EffortBudget(coordinator_minutes=100000.0)
DOCUMENT_ACTIONS = frozenset({"REQUEST_DOCUMENT"})
SOURCE_KO = {"patient_interview": "환자 문진", "lab_report": "검사 결과지", "pathology_report": "병리 보고서",
             "medication_list": "복용 약 목록", "medical_record": "진료 기록", "clinician_review": "담당의 확인"}


def _repeat_key(a: Action) -> tuple:
    """같은 행동의 재실행 판단 (sim.runner._repeat_key와 같은 기준). "모름" 뒤 정책이 바꾼 문서 요청·바꿔 묻기는 다른 키."""
    return (a.patient_alias, a.fingerprint, a.action_type, a.source_id)


def is_recovery(a: Action) -> bool:
    """Session 루프를 이어 갈 이유가 되는 대체 행동: 문서 요청과 바꿔 묻기. 일반 환자 질문은 Session 조건을 그대로 따름."""
    return a.action_type in DOCUMENT_ACTIONS or a.reason_code == REPHRASE


def policy_mode() -> str:
    """CTREC_POLICY_MODE=engine일 때만 엔진 모드 (기본 legacy: 기존 질문 생성기 그대로)."""
    mode = os.getenv(ENV_POLICY_MODE, "").strip().lower()
    return mode if mode in POLICY_MODES else "legacy"


@dataclass
class _Asked:
    round: int
    question_id: str
    action: Action
    settled: bool = False


@dataclass
class EngineRunner:
    """Session 하나의 엔진 모드 상태 (실행마다 새로 만듦). generate를 question_generator 자리에 끼움."""
    session: object
    policy: LegacyAdapterPolicy = field(default_factory=LegacyAdapterPolicy)
    budget: EffortBudget = DEFAULT_BUDGET
    aliases: AliasMap = field(default_factory=AliasMap)  # salt는 실행별·메모리에만 (W-2)
    ops: dict[str, OpsState] = field(default_factory=dict)
    asked: list[_Asked] = field(default_factory=list)
    plans: list[dict] = field(default_factory=list)  # 라운드별 요약 (코드·건수만)
    unexecuted: list[dict] = field(default_factory=list)  # 종료 시 실행하지 못한 행동 (코드·대상 기준만)

    def __post_init__(self):
        self.ledger = EffortLedger(self.budget, self.policy.catalog)
        self.alias = self.aliases.alias(self.session.case.patient_id)
        self._outcomes: list[dict] = []  # 다음 라운드 요약에 넣을 정산 결과

    # ------------------------------------------------------------------ 상태
    def cohort(self) -> CohortState:
        view = SessionView.from_session(self.session)
        view.matched_versions = {t: view.snapshot.version for t in self.session.matches}
        return CohortState.from_decision_states({self.alias: build_state(view)}, ops=dict(self.ops))

    def settle(self) -> list[dict]:
        """직전에 보낸 질문의 답 -> ActionOutcome -> apply_outcome (운영 상태만). 반환: 결과 요약 (코드만)."""
        todo = [a for a in self.asked if not a.settled]
        if not todo:
            return []
        co = self.cohort()
        out = []
        for a in todo:
            a.settled = True
            qa = [p for p in self.session.qa_log if p.round == a.round and p.question_id == a.question_id]
            answer = qa[0].answer if len(qa) == 1 else None  # 답을 못 찾으면 무응답으로 (사실 없음)
            status, code = answer_outcome(answer)
            co, transitions = self.policy.apply_outcome(
                co, a.action, ActionOutcome(action_id=a.action.action_id, status=status, error_code=code))
            out.append({"action_type": a.action.action_type, "source_id": a.action.source_id, "status": status,
                        "error_code": code, "pair_transitions": len(transitions)})
        self.ops[self.alias] = co.ops[self.alias]
        return out

    # ------------------------------------------------------------------ 질문화
    def _rule_text(self, key: str) -> str:
        tid, rid = key.split(":", 1)
        parsed = self.session.parsed.get(tid)
        rule = next((r for r in (parsed.rules if parsed else []) if r.rule_id == rid), None)
        return " ".join(rule.text.split()) if rule else key

    # ------------------------------------------------------------------ 남은 행동
    def _fresh(self, co: CohortState) -> list[Action]:
        """정책의 행동 중 이번 실행에서 아직 실행하지 않은 것."""
        done = {_repeat_key(a.action) for a in self.asked}
        return [a for a in self.policy.propose(co) if a.action_type in NON_EXECUTING or _repeat_key(a) not in done]

    def _plan(self, co: CohortState) -> Plan:
        return self.policy.select(self._fresh(co), self.budget, self.ledger, co)

    def recovery_actions(self) -> list[Action]:
        """지금 계획에 들어가고 이 화면에서 실행할 수 있는 대체 행동. 호출할 때 이미 받은 답을 먼저 정산합니다."""
        self._outcomes += self.settle()
        plan = self._plan(self.cohort())
        if plan.safety_unfunded:
            return []
        return [a for a in plan.selected if is_recovery(a) and self.render(a, "X") is not None]

    def recovery_pending(self) -> bool:
        return bool(self.recovery_actions())

    def recovery_uncertain(self) -> list:
        """session.uncertain 자리: 기존 대상이 없으면 대체 행동이 남은 모집 중 시험을 질문 대상으로."""
        base = self._session_uncertain()
        if base:
            return base
        targets = {t for a in self.recovery_actions() for t in a.target_trial_ids}
        return [m for m in self.session.open_candidates() if m.trial_id in targets]

    def _stop_reason(self) -> str:
        s = self.session
        if s.rounds >= s.max_rounds:
            return "round_limit"
        return "question_budget" if s.remaining_questions() <= 0 else "session_stopped"

    def close(self) -> list[dict]:
        """실행 끝: 마지막 답을 정산하고, 실행하지 못한 행동을 기록. 실행 권한이 없어 기록만 하는 행동(render 불가)은 빠짐."""
        self.settle()
        if not self.session.matches:
            return []
        co = self.cohort()
        over = {x.action_id for x in self._plan(co).skipped if x.reason_code == "over_budget"}
        stop = self._stop_reason()
        left = [a for a in self._fresh(co) if a.action_type not in NON_EXECUTING and self.render(a, "X") is not None]
        self.unexecuted = [{"action_type": a.action_type, "source_id": a.source_id, "reason_code": a.reason_code,
                            "targets": list(a.target_criteria), "recovery": is_recovery(a),
                            "stop_reason": "over_budget" if a.action_id in over else stop} for a in left]
        if left:
            self.session.tracer.log("engine", "actions_not_executed", count=len(left), stop_reason=stop,
                                    actions=self.unexecuted, message=f"실행하지 못한 행동 {len(left)}개 ({stop})")
            self._report_unexecuted(left)
        return self.unexecuted

    def _report_unexecuted(self, left: list[Action]) -> None:
        docs = [a for a in left if is_recovery(a)]
        if not (docs and self.session.report):
            return
        lines = [f"- {SOURCE_KO.get(a.source_id or '', '관련 기록')}: "
                 f"{'; '.join(dict.fromkeys(self._rule_text(k) for k in a.target_criteria)) or '해당 기준'}" for a in docs]
        self.session.report += "\n\n## 실행하지 못한 확인\n\n" \
            "확인 한도로 끝나 다음 확인을 하지 못했습니다. 이 항목은 소진된 것이 아니므로 담당자가 이어서 확인해야 합니다.\n" \
            + "\n".join(lines) + "\n"

    def _wording(self, action: Action) -> Wording:
        texts = "; ".join(dict.fromkeys(self._rule_text(k) for k in action.target_criteria)) or "해당 기준"
        if action.reason_code == REPHRASE:  # 같은 문장을 반복하지 않음
            return Wording(question=f"앞서 답하기 어려우셨던 내용입니다. 의료진에게 들으신 내용이나 대략 기억나는 내용도 괜찮습니다: "
                                    f"\"{texts}\"", answer_type="free_text", why=action.reason_code)
        if action.purpose == "physician_reference":
            return Wording(question=f"담당 의사가 참고할 정보입니다. 다음 내용과 관련해 알고 계신 것을 적어 주세요: {texts}",
                           answer_type="free_text", why=action.reason_code)
        return Wording(question=f"다음 시험 기준에 해당하는지 확인이 필요합니다. 해당되시나요? \"{texts}\"",
                       answer_type="free_text", why=action.reason_code)

    def _document_question(self, action: Action, qid: str) -> ClarifyingQuestion:
        texts = "; ".join(dict.fromkeys(self._rule_text(k) for k in action.target_criteria)) or "해당 기준"
        where = SOURCE_KO.get(action.source_id or "", "관련 기록")
        q = ClarifyingQuestion(
            question_id=qid, answer_type="free_text", target=list(action.target_criteria), purpose=action.purpose,
            question=f"[기록 확인 요청 · {where}] 기록에서 다음 내용을 확인해 입력해 주세요 (확인할 수 없으면 '모름'): "
                     f"\"{texts}\"", why=action.reason_code)
        check_rendered(action, q)
        return q

    def render(self, action: Action, qid: str) -> ClarifyingQuestion | None:
        """이 화면에서 실행할 수 있는 행동만 질문으로. 실행할 수 없으면 None."""
        try:
            if renderable(action):
                q = render_action(action, qid, wording=self._wording)
                check_rendered(action, q)
                return q
            if action.action_type in DOCUMENT_ACTIONS:
                return self._document_question(action, qid)
        except RenderError:
            return None
        return None

    # ------------------------------------------------------------------ question_generator 자리
    def generate(self, profile, matches, qa_log, n, acquisition_plan=None) -> QuestionSet:
        s = self.session
        outcomes = self._outcomes + self.settle()
        self._outcomes = []
        co = self.cohort()
        plan = self._plan(co)
        summary = {"round": s.rounds, "outcomes": outcomes, "selected": len(plan.selected), "held": len(plan.held),
                   "skipped": len(plan.skipped), "safety_unfunded": len(plan.safety_unfunded),
                   "pairs": _status_counts(co), "asked": []}
        questions: list[ClarifyingQuestion] = []
        if plan.safety_unfunded:  # W-7: 안전 확인을 못 하면 다른 질문을 진행하지 않음 (정책도 그 환자를 막음)
            s.tracer.log("engine", "safety_unfunded", round=s.rounds, count=len(plan.safety_unfunded),
                         message="안전 확인 미실행: 예산이 모자라 안전 필수 확인을 하지 못했습니다")
        else:
            for a in plan.selected:
                if len(questions) >= n:
                    break
                qid = f"EQ{s.rounds}-{len(questions) + 1}"
                q = self.render(a, qid)
                if q is None:
                    continue
                self.ledger.charge(a)
                self.asked.append(_Asked(s.rounds, qid, a))
                questions.append(q)
                summary["asked"].append({"action_type": a.action_type, "source_id": a.source_id,
                                         "reason_code": a.reason_code, "targets": list(a.target_criteria)})
        not_run = [a for a in plan.selected if self.render(a, "X") is None]
        summary["not_executable_here"] = len(not_run)
        unsafe = sorted({a.reason_code for a in not_run if a.safety_required})
        if unsafe:
            s.tracer.log("engine", "safety_not_executed", round=s.rounds, count=sum(a.safety_required for a in not_run),
                         reasons=unsafe, message="안전 확인(근거 상충 등)은 이 화면에서 실행하지 않습니다 - 담당자 확인 필요")
        self.plans.append(summary)
        s.tracer.log("engine", "plan", **summary,
                     message=f"정책 엔진: 행동 {len(plan.selected)}개 중 질문 {len(questions)}개")
        return QuestionSet(questions=questions)


def _status_counts(co: CohortState) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in co.pairs():
        out[p.status.value] = out.get(p.status.value, 0) + 1
    return out


@contextlib.contextmanager
def activate(session, **kw):
    """이 컨텍스트 안에서 session.clarify의 질문을 엔진이 고름. 다른 실행(스레드·컨텍스트)에는 영향 없음."""
    runner = EngineRunner(session, **kw)
    session.tracer.log("engine", "start", policy_id=runner.policy.policy_id, policy_version=runner.policy.version,
                       message="정책 엔진 모드로 확인 질문을 고릅니다")
    runner._session_uncertain = session.uncertain
    session.uncertain = runner.recovery_uncertain  # 이 Session 인스턴스만 (pipeline.py는 고치지 않음)
    session.engine_recovery_pending = runner.recovery_pending  # ContractSession의 기존 대상 판단 (session.uncertain을 거치지 않음)
    try:
        with question_generator.use_generator(runner.generate):
            yield runner
    finally:
        del session.uncertain, session.engine_recovery_pending
    runner.close()  # 마지막 라운드 답도 운영 상태에 반영하고, 실행하지 못한 행동을 기록


def policy_from_env():
    """CTREC_POLICY_ID (기본 legacy_greedy): 시뮬레이터와 같은 정책 등록표(engine.baselines.make_policy)에서 고름 (M14)."""
    from ..engine.baselines import make_policy
    return make_policy(os.getenv(ENV_POLICY_ID, "").strip() or DEFAULT_POLICY_ID)


def for_mode(session):
    """실행 진입점(웹·CLI)용: 엔진 모드면 activate, 아니면 아무것도 바꾸지 않는 컨텍스트."""
    return activate(session, policy=policy_from_env()) if policy_mode() == "engine" else contextlib.nullcontext()


__all__ = ["EngineRunner", "activate", "for_mode", "policy_from_env", "policy_mode", "POLICY_MODES", "ENV_POLICY_MODE",
           "ENV_POLICY_ID"]
