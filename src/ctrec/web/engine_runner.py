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
실행하지 않으며(repeat_key), 라운드·질문·작업시간 한도는 Session과 정책이 그대로 지킵니다. 한도로 끝나 실행하지 못한 행동은
close가 EngineRunner.unexecuted, trace("engine", "actions_not_executed"), 보고서 끝 절에 남깁니다. 그 쌍의 상태는 바꾸지 않음
(문서가 남아 있으므로 BLOCKED가 아님).

공통 엔진 (P). 계획·반복 억제·실행 배분·청구·정산은 engine.loop.EngineLoop 하나이며 시뮬레이터(sim.runner)와 같은 객체를
씁니다. 이 모듈이 다른 것은 실행 수단(화면 질문)뿐입니다.
- 이 화면에서 실행할 수 없는 선택 행동(VERIFY_RECORD 등)은 시뮬레이터의 미실행과 같이 not_executed로 정산하고 시도로 셉니다
  (같은 사실 상태에서 다시 고르지 않음, 청구 0).
- 판정 시점: Session이 실제로 재매칭한 시험의 사실 버전을 기록합니다 (전에는 모든 시험을 최신으로 표시해, 다시 매칭하지 않은
  INELIGIBLE 시험의 stale을 숨김). 엔진 모드에서는 Session의 부분 재매칭(ELIGIBLE·UNCERTAIN만)에 사실이 바뀐 뒤 판정이
  오래된 시험을 더해, 시뮬레이터처럼 그 환자의 모든 평가 시험을 다시 판정합니다 (풀 재평가와 같은 상태).
- 코호트 (CohortRunner, run_cohort): 여러 환자의 Session을 한 정책·한 원장·한 계획으로 묶습니다. 라운드마다 모든 환자의
  상태를 합쳐 계획하고, 각 Session의 확인 라운드는 자기 환자 몫만 질문합니다 (같은 (환자, 정보원) 비용 한 번, 환자별 최소 배정).

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
from ..engine.loop import Dispatch, EngineLoop, repeat_key
from ..engine.policy import LegacyAdapterPolicy
from ..engine.question_bridge import RenderError, Wording, check_rendered, render_action, renderable
from ..eval_hygiene import AliasMap
from ..fact_changes import take_snapshot
from ..schemas import ClarifyingQuestion, QuestionSet

POLICY_MODES = ("legacy", "engine")
ENV_POLICY_MODE = "CTREC_POLICY_MODE"
ENV_POLICY_ID = "CTREC_POLICY_ID"
# 제품 기본 정책 = 실험의 제안 정책 (같은 등록표의 같은 클래스, M14). legacy_greedy 등은 CTREC_POLICY_ID로 고름
DEFAULT_POLICY_ID = "full_dcvc"
# 웹에는 아직 코디네이터 작업시간 예산 입력이 없음 -> 선택이 예산에서 잘리지 않게 하는 값 (실험 예산이 아님)
DEFAULT_BUDGET = EffortBudget(coordinator_minutes=100000.0)
DOCUMENT_ACTIONS = frozenset({"REQUEST_DOCUMENT"})
SOURCE_KO = {"patient_interview": "환자 문진", "lab_report": "검사 결과지", "pathology_report": "병리 보고서",
             "medication_list": "복용 약 목록", "medical_record": "진료 기록", "clinician_review": "담당의 확인"}
NOT_EXECUTABLE_HERE = "not_executable_on_screen"

_repeat_key = repeat_key  # 이전 이름 (engine.loop.repeat_key, 시뮬레이터와 같은 기준)


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
    session: object = field(default=None, repr=False, compare=False)


@dataclass
class _Member:
    session: object
    alias: str
    versions: dict[str, str] = field(default_factory=dict)  # 시험 -> 실제로 판정한 때의 사실 버전
    session_uncertain: object = None  # 원래 session.uncertain


def fact_version(session) -> str:
    return take_snapshot(session.fact_store, session.case.patient_id).version


class CohortRunner:
    """여러 Session(환자마다 하나)의 엔진 모드 상태. 한 정책·한 원장(EngineLoop)·한 계획. 실행마다 새로 만듦.

    Session이 하나면 이전 EngineRunner와 같습니다 (EngineRunner는 이 클래스의 이전 이름)."""

    def __init__(self, sessions, policy: LegacyAdapterPolicy | None = None, budget: EffortBudget = DEFAULT_BUDGET,
                 aliases: AliasMap | None = None, ops: dict[str, OpsState] | None = None):
        if not isinstance(sessions, (list, tuple)):
            sessions = [sessions]
        self.policy = policy or LegacyAdapterPolicy()
        self.budget = budget
        self.aliases = aliases or AliasMap()  # salt는 실행별·메모리에만 (W-2)
        self.ops: dict[str, OpsState] = dict(ops or {})
        self.asked: list[_Asked] = []
        self.plans: list[dict] = []  # 환자 라운드별 요약 (코드·건수만)
        self.unexecuted: list[dict] = []  # 종료 시 실행하지 못한 행동 (코드·대상 기준만)
        self.loop = EngineLoop(self.policy, self.budget)
        self.members = [_Member(s, self.aliases.alias(s.case.patient_id)) for s in sessions]
        if len({m.alias for m in self.members}) != len(self.members):
            raise ValueError("같은 환자의 Session이 둘 이상임")
        for m in self.members:
            m.session_uncertain = getattr(m.session, "uncertain", None)
        self.cohort_mode = len(self.members) > 1
        self._current = self.members[0]
        self._outcomes: list[dict] = []  # 다음 라운드 요약에 넣을 정산 결과
        self._round: dict | None = None
        # 라운드 기록 (메모리만, 화면·trace에 내보내지 않음): 웹·시뮬레이터 일치 검사(재생)와 감사용
        self.rounds_log: list[dict] = []
        self.final_log: dict = {}
        self._settled: list[tuple[Action, ActionOutcome]] = []

    # ------------------------------------------------------------------ 이전 API 이름
    @property
    def session(self):
        return self.members[0].session

    @property
    def alias(self) -> str:
        return self.members[0].alias

    @property
    def ledger(self):
        return self.loop.ledger

    @property
    def _session_uncertain(self):
        return self.members[0].session_uncertain

    @_session_uncertain.setter
    def _session_uncertain(self, fn):
        self.members[0].session_uncertain = fn

    def unfunded_aliases(self, plan: Plan) -> set[str]:
        """예산이 모자라 실행하지 못한 안전 필수 행동이 있는 환자. 환자를 알 수 없는 ID면 모든 환자 (안전 쪽)."""
        names = {m.alias for m in self.members}
        out: set[str] = set()
        for x in plan.safety_unfunded:
            alias = x.split(":", 1)[0]
            out |= {alias} if alias in names else names
        return out

    # ------------------------------------------------------------------ 상태
    def _state(self, m: _Member):
        view = SessionView.from_session(m.session)
        # 실제로 판정한 때의 버전 (기록이 없는 시험 = 엔진 모드 전에 판정됨 -> 지금 버전으로 봄, 이전 동작)
        view.matched_versions = {t: m.versions.get(t, view.snapshot.version) for t in m.session.matches}
        return build_state(view)

    def cohort(self) -> CohortState:
        return CohortState.from_decision_states({m.alias: self._state(m) for m in self.members}, ops=dict(self.ops))

    def _keep_ops(self, co: CohortState) -> None:
        for m in self.members:
            self.ops[m.alias] = co.ops[m.alias]

    def settle(self) -> list[dict]:
        """직전에 보낸 질문의 답 -> ActionOutcome -> apply_outcome (운영 상태만). 반환: 결과 요약 (코드만)."""
        todo = [a for a in self.asked if not a.settled]
        if not todo:
            return []
        co = self.cohort()
        out = []
        for a in todo:
            a.settled = True
            s = a.session or self.session
            qa = [p for p in s.qa_log if p.round == a.round and p.question_id == a.question_id]
            answer = qa[0].answer if len(qa) == 1 else None  # 답을 못 찾으면 무응답으로 (사실 없음)
            status, code = answer_outcome(answer)
            outcome = ActionOutcome(action_id=a.action.action_id, status=status, error_code=code)
            co, transitions = self.loop.settle(co, a.action, outcome)
            self._settled.append((a.action, outcome))
            out.append({"action_type": a.action.action_type, "source_id": a.action.source_id, "status": status,
                        "error_code": code, "pair_transitions": len(transitions)})
        self._keep_ops(co)
        return out

    # ------------------------------------------------------------------ 질문화
    def _session_of(self, alias: str):
        return next((m.session for m in self.members if m.alias == alias), self._current.session)

    def _rule_text(self, key: str, session=None) -> str:
        tid, rid = key.split(":", 1)
        s = session or self._current.session
        parsed = s.parsed.get(tid)
        rule = next((r for r in (parsed.rules if parsed else []) if r.rule_id == rid), None)
        return " ".join(rule.text.split()) if rule else key

    def _texts(self, action: Action) -> str:
        s = self._session_of(action.patient_alias)
        return "; ".join(dict.fromkeys(self._rule_text(k, s) for k in action.target_criteria)) or "해당 기준"

    def _wording(self, action: Action) -> Wording:
        texts = self._texts(action)
        if action.reason_code == REPHRASE:  # 같은 문장을 반복하지 않음
            return Wording(question=f"앞서 답하기 어려우셨던 내용입니다. 의료진에게 들으신 내용이나 대략 기억나는 내용도 괜찮습니다: "
                                    f"\"{texts}\"", answer_type="free_text", why=action.reason_code)
        if action.purpose == "physician_reference":
            return Wording(question=f"담당 의사가 참고할 정보입니다. 다음 내용과 관련해 알고 계신 것을 적어 주세요: {texts}",
                           answer_type="free_text", why=action.reason_code)
        return Wording(question=f"다음 시험 기준에 해당하는지 확인이 필요합니다. 해당되시나요? \"{texts}\"",
                       answer_type="free_text", why=action.reason_code)

    def _document_question(self, action: Action, qid: str) -> ClarifyingQuestion:
        where = SOURCE_KO.get(action.source_id or "", "관련 기록")
        q = ClarifyingQuestion(
            question_id=qid, answer_type="free_text", target=list(action.target_criteria), purpose=action.purpose,
            question=f"[기록 확인 요청 · {where}] 기록에서 다음 내용을 확인해 입력해 주세요 (확인할 수 없으면 '모름'): "
                     f"\"{self._texts(action)}\"", why=action.reason_code)
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

    def executable(self, a: Action) -> bool:
        return self.render(a, "X") is not None

    # ------------------------------------------------------------------ 남은 행동
    def _fresh(self, co: CohortState) -> list[Action]:
        """정책의 행동 중 이번 실행에서 아직 시도하지 않은 것 (engine.loop 반복 억제)."""
        return self.loop.fresh(self.policy.propose(co))

    def _plan(self, co: CohortState) -> Plan:
        return self.loop.preview(co)

    def recovery_actions(self, member: _Member | None = None) -> list[Action]:
        """지금 계획에 들어가고 이 화면에서 실행할 수 있는 대체 행동. 호출할 때 이미 받은 답을 먼저 정산합니다."""
        self._outcomes += self.settle()
        plan = self._plan(self.cohort())
        alias = (member or self._current).alias
        if alias in self.unfunded_aliases(plan):
            return []
        return [a for a in plan.selected if a.patient_alias == alias and is_recovery(a) and self.executable(a)]

    def recovery_pending(self) -> bool:
        return bool(self.recovery_actions())

    def recovery_uncertain(self, member: _Member | None = None) -> list:
        """session.uncertain 자리: 기존 대상이 없으면 대체 행동이 남은 모집 중 시험을 질문 대상으로.
        코호트 라운드에서는 이번 코호트 계획의 그 환자 몫이 질문 대상입니다."""
        m = member or self._current
        if self.cohort_mode and self._round is not None:
            targets = {t for a in self._round["dispatch"].execute if a.patient_alias == m.alias
                       for t in a.target_trial_ids}
            return [x for x in m.session.open_candidates() if x.trial_id in targets]
        base = m.session_uncertain()
        if base:
            return base
        targets = {t for a in self.recovery_actions(m) for t in a.target_trial_ids}
        return [x for x in m.session.open_candidates() if x.trial_id in targets]

    @staticmethod
    def _stop_reason(s) -> str:
        if s.rounds >= s.max_rounds:
            return "round_limit"
        return "question_budget" if s.remaining_questions() <= 0 else "session_stopped"

    def close(self) -> list[dict]:
        """실행 끝: 마지막 답을 정산하고, 실행하지 못한 행동을 기록. 실행 권한이 없어 기록만 하는 행동(render 불가)은 빠짐.
        쌍 상태는 바꾸지 않음 (미해결은 미해결로 남음)."""
        self.settle()
        self.final_log = {"states": {m.alias: self._state(m) for m in self.members} if any(
            m.session.matches for m in self.members) else {}, "settled": self._settled}
        self._settled = []
        if not any(m.session.matches for m in self.members):
            return []
        co = self.cohort()
        over = {x.action_id for x in self._plan(co).skipped if x.reason_code == "over_budget"}
        fresh = self._fresh(co)
        self.unexecuted = []
        for m in self.members:
            stop = self._stop_reason(m.session)
            left = [a for a in fresh if a.patient_alias == m.alias and a.action_type not in NON_EXECUTING
                    and self.executable(a)]
            rows = [{"action_type": a.action_type, "source_id": a.source_id, "reason_code": a.reason_code,
                     "targets": list(a.target_criteria), "recovery": is_recovery(a),
                     "stop_reason": "over_budget" if a.action_id in over else stop} for a in left]
            self.unexecuted += rows
            if left:
                m.session.tracer.log("engine", "actions_not_executed", count=len(left), stop_reason=stop,
                                     actions=rows, message=f"실행하지 못한 행동 {len(left)}개 ({stop})")
                self._report_unexecuted(m.session, left)
        return self.unexecuted

    def _report_unexecuted(self, session, left: list[Action]) -> None:
        docs = [a for a in left if is_recovery(a)]
        if not (docs and session.report):
            return
        lines = [f"- {SOURCE_KO.get(a.source_id or '', '관련 기록')}: {self._texts(a)}" for a in docs]
        session.report += "\n\n## 실행하지 못한 확인\n\n" \
            "확인 한도로 끝나 다음 확인을 하지 못했습니다. 이 항목은 소진된 것이 아니므로 담당자가 이어서 확인해야 합니다.\n" \
            + "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ 라운드
    def start_round(self, capacity: dict[str, int] | None = None) -> Dispatch:
        """모든 환자의 답을 정산하고 코호트 전체를 한 번 계획 (engine.loop). 이 화면에서 실행할 수 없는 선택 행동은
        not_executed로 정산. capacity: 별칭 -> 이번 라운드 질문 수 (없으면 각 Session의 남은 질문 수)."""
        outcomes = self._outcomes + self.settle()
        self._outcomes = []
        states = {m.alias: self._state(m) for m in self.members}
        co = CohortState.from_decision_states(states, ops=dict(self.ops))
        plan = self.loop.plan(co)
        caps = capacity if capacity is not None else {m.alias: m.session.remaining_questions() for m in self.members}
        unfunded = self.unfunded_aliases(plan)
        disp = self.loop.dispatch(plan, self.executable, lambda alias: 0 if alias in unfunded else caps.get(alias, 0))
        settled = co
        for a in disp.not_executable:  # 시뮬레이터의 미실행과 같은 정산 (시도로 셈, 청구 0)
            self.loop.begin(a, executed=False)
            settled, _ = self.loop.settle_not_executed(settled, a, NOT_EXECUTABLE_HERE)
        self._keep_ops(settled)
        self._round = {"dispatch": disp, "consumed": set(), "co": co, "plan": plan, "outcomes": outcomes}
        self.rounds_log.append({"states": states, "settled": self._settled, "plan": plan, "dispatch": disp,
                                "capacity": dict(caps), "asked": []})
        self._settled = []
        return disp

    # ------------------------------------------------------------------ question_generator 자리
    def generate(self, profile, matches, qa_log, n, acquisition_plan=None) -> QuestionSet:
        m = self._current
        s = m.session
        if self._round is None or m.alias in self._round["consumed"]:
            self.start_round(None if self.cohort_mode else {m.alias: n})
        rnd = self._round
        first = not rnd["consumed"]
        rnd["consumed"].add(m.alias)
        plan, co, disp = rnd["plan"], rnd["co"], rnd["dispatch"]
        mine = [a for a in plan.selected if a.patient_alias == m.alias]
        unfunded = len(plan.safety_unfunded) if m.alias in self.unfunded_aliases(plan) else 0
        summary = {"round": s.rounds, "outcomes": rnd["outcomes"] if first else [], "selected": len(mine),
                   "held": sum(a.patient_alias == m.alias for a in plan.held),
                   "skipped": sum(x.action_id.startswith(m.alias + ":") for x in plan.skipped),
                   "safety_unfunded": unfunded, "pairs": _status_counts(co, m.alias), "asked": []}
        questions: list[ClarifyingQuestion] = []
        if unfunded:  # W-7: 안전 확인을 못 하면 다른 질문을 진행하지 않음 (정책도 그 환자를 막음)
            s.tracer.log("engine", "safety_unfunded", round=s.rounds, count=unfunded,
                         message="안전 확인 미실행: 예산이 모자라 안전 필수 확인을 하지 못했습니다")
        else:
            for a in disp.execute:
                if a.patient_alias != m.alias or len(questions) >= n:
                    continue
                qid = f"EQ{s.rounds}-{len(questions) + 1}"
                q = self.render(a, qid)
                if q is None:
                    continue
                self.loop.begin(a, executed=True)
                self.asked.append(_Asked(s.rounds, qid, a, session=s))
                self.rounds_log[-1]["asked"].append(a)
                questions.append(q)
                summary["asked"].append({"action_type": a.action_type, "source_id": a.source_id,
                                         "reason_code": a.reason_code, "targets": list(a.target_criteria)})
        not_run = [a for a in disp.not_executable if a.patient_alias == m.alias]
        summary["not_executable_here"] = len(not_run)
        unsafe = sorted({a.reason_code for a in not_run if a.safety_required})
        if unsafe:
            s.tracer.log("engine", "safety_not_executed", round=s.rounds, count=sum(a.safety_required for a in not_run),
                         reasons=unsafe, message="안전 확인(근거 상충 등)은 이 화면에서 실행하지 않습니다 - 담당자 확인 필요")
        self.plans.append(summary)
        s.tracer.log("engine", "plan", **summary,
                     message=f"정책 엔진: 행동 {len(mine)}개 중 질문 {len(questions)}개")
        return QuestionSet(questions=questions)

    # ------------------------------------------------------------------ 재매칭 (풀 재평가)
    def track_match(self, m: _Member):
        """session.match 자리: 판정 시점의 사실 버전을 기록하고, 부분 재매칭에 판정이 오래된 시험을 더함."""
        orig = m.session.match

        def match(trial_ids=None):
            ids = list(trial_ids) if trial_ids else None
            if ids is not None and m.versions:
                cur = fact_version(m.session)
                ids += sorted(t for t, v in m.versions.items() if v != cur and t not in ids
                              and t in m.session.matches and m.session.matches[t].eligibility != "NOT_EVALUATED")
            out = orig(ids)
            v = fact_version(m.session)
            for x in out:
                m.versions[x.trial_id] = v
            return out
        return match


EngineRunner = CohortRunner  # 이전 이름 (Session 하나)


def _status_counts(co: CohortState, alias: str | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in co.pairs():
        if alias is None or p.pair.patient_alias == alias:
            out[p.status.value] = out.get(p.status.value, 0) + 1
    return out


@contextlib.contextmanager
def activate_cohort(sessions, **kw):
    """이 컨텍스트 안에서 각 session.clarify의 질문을 한 엔진이 고름. 다른 실행(스레드·컨텍스트)에는 영향 없음.
    Session 인스턴스의 uncertain·match만 바꿉니다 (pipeline.py는 고치지 않음)."""
    runner = CohortRunner(list(sessions), **kw)
    for m in runner.members:
        s = m.session
        s.tracer.log("engine", "start", policy_id=runner.policy.policy_id, policy_version=runner.policy.version,
                     message="정책 엔진 모드로 확인 질문을 고릅니다")
        s.match = runner.track_match(m)
        if runner.cohort_mode:
            s.uncertain = (lambda mm: (lambda: runner.recovery_uncertain(mm)))(m)
            # ContractSession의 기존 대상 판단 (session.uncertain을 거치지 않음)
            s.engine_recovery_pending = (lambda mm: (lambda: bool(runner.recovery_actions(mm))))(m)
        else:
            s.uncertain, s.engine_recovery_pending = runner.recovery_uncertain, runner.recovery_pending
    try:
        with question_generator.use_generator(runner.generate):
            yield runner
    finally:
        for m in runner.members:
            del m.session.uncertain, m.session.engine_recovery_pending, m.session.match
    runner.close()  # 마지막 라운드 답도 운영 상태에 반영하고, 실행하지 못한 행동을 기록


@contextlib.contextmanager
def activate(session, **kw):
    """Session 하나 (웹·CLI)."""
    with activate_cohort([session], **kw) as runner:
        yield runner


def run_cohort(sessions, **kw) -> CohortRunner:
    """여러 환자의 Session을 한 엔진으로 끝까지 실행 (CLI·연구용 코호트 실행). 각 Session의 단계는 run_fixed와 같음:
    프로필 -> (선별) -> 매칭 -> [코호트 계획 -> 환자별 확인 라운드]* -> finalize. 끝나면 미해결 쌍은 미해결로 남음."""
    with activate_cohort(sessions, **kw) as runner:
        for m in runner.members:
            s = m.session
            s.profile_patient()
            if s.screen_trials:
                s.screen()
            s.match()
        while True:
            disp = runner.start_round()
            asked = False
            for m in runner.members:
                if not any(a.patient_alias == m.alias for a in disp.execute) or not m.session.needs_clarification():
                    continue
                runner._current = m
                asked |= m.session.clarify().get("status") == "done"
            runner._current = runner.members[0]
            if not asked:
                break
        for m in runner.members:
            m.session.finalize()
    return runner


def policy_from_env():
    """CTREC_POLICY_ID (기본 full_dcvc): 시뮬레이터와 같은 정책 등록표(engine.baselines.make_policy)에서 고름 (M14)."""
    from ..engine.baselines import make_policy
    return make_policy(os.getenv(ENV_POLICY_ID, "").strip() or DEFAULT_POLICY_ID)


def for_mode(session):
    """실행 진입점(웹·CLI)용: 엔진 모드면 activate, 아니면 아무것도 바꾸지 않는 컨텍스트."""
    return activate(session, policy=policy_from_env()) if policy_mode() == "engine" else contextlib.nullcontext()


__all__ = ["EngineRunner", "CohortRunner", "activate", "activate_cohort", "run_cohort", "for_mode", "policy_from_env",
           "policy_mode", "POLICY_MODES", "ENV_POLICY_MODE", "ENV_POLICY_ID", "DEFAULT_POLICY_ID"]
