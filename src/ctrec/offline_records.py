"""WP7 오프라인 실행기 어댑터: 실제 오프라인 Session 실행에서 관찰한 값으로 DecisionRecord와 진단 파일을 만듦.

engine.record_producer(웹·오프라인 공통 순수 생산기)에 관찰만 넘기는 얇은 층입니다. 평가 경로(question_eval,
eval_hygiene)를 쓰므로 engine 패키지 밖에 둡니다(엔진은 평가·정답 쪽 모듈을 import하지 않음).

실행 방식 (docs/dev/wp7_offline_records.md)
- question_eval.run_method_hygienic + factories 주입: 파이프라인에는 별칭만 들어가고 프롬프트 누출 검사가 걸림.
- 라운드마다 질문 시점의 Session을 읽어 CohortState를 만들고 LegacyAdapterPolicy.propose/select로 행동을 고른 뒤,
  질문으로 만들 수 있는 첫 행동 **하나만** question_bridge.render_action으로 질문화 (라운드당 1문항, 이 경로 전용).
  같은 라운드의 재추출·재매칭 결과를 그 한 행동에 귀속할 수 있게 하려는 제한입니다. 기본 평가의 3문항 설정은 그대로.
- 청구는 실제로 질문을 보낸 행동만 EffortLedger에 함(응답 여부와 무관하게 요청 1회). 응답 결과는 policy.apply_outcome으로
  운영 상태(ops)에 반영하고, 다음 라운드의 CohortState를 그 ops로 만듦.
- 첫 프로필 추출·매칭은 행동의 결과가 아니므로 기록하지 않음. 기록은 첫 질문부터 시작.

관찰할 수 없는 값은 만들지 않습니다: 질문별 사실 출처가 없으므로 source_ref=None (사실은 귀속되지 않고 진단으로 남음),
지연 시간·상태 변경 사유는 넘기지 않음.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import question_eval as qe
from .decision_state import SessionView, build_state
from .engine import audit
from .engine import record_producer as rp
from .engine.cohort_state import CohortState, OpsState
from .engine.contracts import ActionOutcome, EffortBudget
from .engine.cost import EffortLedger
from .engine.policy import LegacyAdapterPolicy
from .engine.question_bridge import Wording, render_action, renderable
from .eval_hygiene import run_method_hygienic
from .schemas import QuestionSet

QUESTIONS_PER_ROUND = 1
MIN_SENTENCE_CHARS = 12  # 짧은 조각은 코드 값과 우연히 겹칠 수 있음
METHOD = "engine_records"
QUESTION_ID = "Q1"
DEFAULT_BUDGET = EffortBudget(coordinator_minutes=100000.0)  # 실험 예산이 아니라 선택이 예산에서 잘리지 않게 하는 값


def _wording(action):
    """결정적 문구 (LLM 없음). 질문 문장은 대상·목적을 바꾸지 않음 (render_action이 검사)."""
    return Wording(question=f"[engine] {action.info_key or 'criterion'}", answer_type="free_text",
                   why=action.reason_code)


@dataclass
class _Round:
    round: int
    snapshot: object
    cohort: CohortState
    action: object
    question: object
    rematch: frozenset
    cost: object
    charge_key: str | None
    policy_transitions: tuple | None = ()  # None = 응답을 찾지 못해 처리 불가
    settled: bool = False  # 응답을 운영 상태에 반영했는가 (한 번만)


@dataclass
class OfflineRunResult:
    run_id: str
    run_dir: Path
    method_status: str
    steps: list[rp.StepResult] = field(default_factory=list)
    rounds: list[_Round] = field(default_factory=list, repr=False)
    unsupported: list[str] = field(default_factory=list)  # 이 경로가 표현하지 못한 실행 (코드만)
    selected_not_asked: int = 0  # 선택됐지만 1문항 제한으로 묻지 않은 행동 수 (기록하지 않음)
    questions_in_session: int = 0
    rematch_rounds_in_session: int = 0
    verification: list[str] = field(default_factory=list)  # verify_run 결과 (빈 목록 = 정합)
    final_snapshot: object = field(default=None, repr=False)  # 마지막 단계의 after (검증용, 파일에 쓰지 않음)
    final_cohort: CohortState | None = field(default=None, repr=False)

    @property
    def audit(self) -> rp.RunAuditStatus:
        return rp.run_status(self.steps)

    @property
    def complete(self) -> bool:
        """정상 감사 완료: 실행 성공, 미지원 0, 차단 0, 파일 정합성 문제 0."""
        return (self.method_status == "ok" and not self.unsupported and self.audit.complete
                and not self.verification)

    def summary(self) -> dict:
        a = self.audit
        codes: dict[str, int] = {}
        for s in self.steps:
            for i in s.issues:
                k = f"{i.severity}:{i.code}"
                codes[k] = codes.get(k, 0) + 1
        return {"run_id": self.run_id, "method_status": self.method_status, "complete": self.complete,
                "audit_status": a.status, "steps": len(self.steps), "records": a.records, "warnings": a.warnings,
                "blocking": a.blocking, "violation_steps": list(a.violation_steps), "issue_codes": codes,
                "unsupported": list(self.unsupported), "selected_not_asked": self.selected_not_asked,
                "questions_in_session": self.questions_in_session, "verification": list(self.verification)}


class _Recorder:
    """factories로 주입하는 질문 생성기. 질문 시점의 실제 상태·행동·청구를 모음 (생산기 입력 재료)."""

    def __init__(self, policy: LegacyAdapterPolicy, budget: EffortBudget):
        self.policy, self.budget = policy, budget
        self.ledger = EffortLedger(budget, policy.catalog)
        self.ops: dict[str, OpsState] = {}
        self.rounds: list[_Round] = []
        self.unsupported: list[str] = []
        self.selected_not_asked = 0
        self.ref: dict = {}

    def state(self, s):
        view = SessionView.from_session(s)
        view.matched_versions = {t: view.snapshot.version for t in s.matches}
        alias = s.case.patient_id
        return view.snapshot, CohortState.from_decision_states({alias: build_state(view)}, ops=dict(self.ops))

    def settle(self, s) -> None:
        """직전 라운드 질문의 응답을 운영 상태에 반영 (다음 상태를 만들기 전에)."""
        if not self.rounds or self.rounds[-1].settled:
            return
        rd = self.rounds[-1]
        rd.settled = True
        qa = [p for p in s.qa_log if p.round == rd.round and p.question_id == QUESTION_ID]
        if len(qa) != 1:
            self.unsupported.append(f"answer_not_found_round_{rd.round}")
            rd.policy_transitions = None
            return
        declined = rp.is_declined(qa[0].answer)  # 생산기와 같은 규칙 (decision_state._declined)
        outcome = ActionOutcome(action_id=rd.action.action_id, status="no_response" if declined else "succeeded",
                                error_code=rp.NO_ANSWER_ERROR if declined else None)
        new, transitions = self.policy.apply_outcome(rd.cohort, rd.action, outcome)
        self.ops[rd.action.patient_alias] = new.ops[rd.action.patient_alias]
        rd.policy_transitions = tuple(transitions)

    def factory(self, ref: dict):
        self.ref = ref

        def generate(profile, matches, qa_log, n, acquisition_plan=None) -> QuestionSet:
            s = ref["session"]
            self.settle(s)
            if n != QUESTIONS_PER_ROUND:  # 이 경로는 라운드당 1문항에서만 귀속이 성립
                self.unsupported.append(f"question_limit_{n}")
                return QuestionSet(questions=[])
            snap, co = self.state(s)
            plan = self.policy.select(self.policy.propose(co), self.budget, self.ledger, co)
            asks = [a for a in plan.selected if renderable(a)]
            self.selected_not_asked += max(len(plan.selected) - (1 if asks else 0), 0)
            if not asks:
                return QuestionSet(questions=[])
            a = asks[0]
            q = render_action(a, QUESTION_ID, wording=_wording)
            cost, key = self.ledger.charge(a)
            self.rounds.append(_Round(s.rounds, snap, co, a, q, frozenset(m.trial_id for m in matches), cost, key))
            return QuestionSet(questions=[q])
        return generate


def _observations(rec: _Recorder, s, run_id: str, final_snap, final_co) -> list[rp.StepObservation]:
    out = []
    for i, rd in enumerate(rec.rounds):
        if rd.policy_transitions is None:
            continue
        nxt_snap, nxt_co = ((rec.rounds[i + 1].snapshot, rec.rounds[i + 1].cohort) if i + 1 < len(rec.rounds)
                            else (final_snap, final_co))
        qa = next(p for p in s.qa_log if p.round == rd.round and p.question_id == QUESTION_ID)
        out.append(rp.StepObservation(
            run_id=run_id, step=i + 1, action=rd.action,
            execution="declined" if rp.is_declined(qa.answer) else "answered",
            asked=rp.AskedQuestion(question_id=qa.question_id, action_id=rd.action.action_id,
                                   target_criteria=tuple(qa.target), purpose=qa.purpose, source_ref=None),
            before=rd.cohort, after=nxt_co, facts_before=rd.snapshot, facts_after=nxt_snap,
            reevaluated_trials=rd.rematch, policy_transitions=rd.policy_transitions, cost=rd.cost,
            charge_keys=(rd.charge_key,) if rd.charge_key else ()))
    return out


def _patient_sentences(text: str) -> list[str]:
    """쓰기 검사에 넘길 환자 원문 문장. 기록은 ID·코드뿐이므로 원문 전체가 아니라 문장 단위로 찾습니다."""
    return [s.strip() for s in re.split(r"(?<=[.!?。])\s+|\n+", text or "") if len(s.strip()) >= MIN_SENTENCE_CHARS]


def record_case(hc, trials: dict, scanner, run_dir: Path, *, rounds: int = 2,
                policy: LegacyAdapterPolicy | None = None, budget: EffortBudget = DEFAULT_BUDGET) -> OfflineRunResult:
    """별칭 사례 하나를 오프라인 대역으로 실행하고 기록·진단을 run_dir에 씀 (OfflineEnvironment는 여기서 설치)."""
    run_dir = Path(run_dir)
    alias = hc.case.patient_id
    run_id = f"RUN-{alias}"  # 별칭에서 만들어 원래 ID가 없고, 같은 salt면 재현됨
    rec = _Recorder(policy or LegacyAdapterPolicy(), budget)
    with qe.OfflineEnvironment(QUESTIONS_PER_ROUND):
        run, guard = run_method_hygienic(METHOD, hc, trials, rounds, QUESTIONS_PER_ROUND, scanner,
                                         factories={METHOD: rec.factory})
    s = rec.ref.get("session")
    if s is None:  # Session을 만들기 전에 실패
        return OfflineRunResult(run_id=run_id, run_dir=run_dir, method_status=run.status,
                                unsupported=["session_not_created"])
    rec.settle(s)
    res = OfflineRunResult(run_id=run_id, run_dir=run_dir, method_status=run.status, rounds=rec.rounds,
                           unsupported=list(rec.unsupported), selected_not_asked=rec.selected_not_asked,
                           questions_in_session=len(s.qa_log),
                           rematch_rounds_in_session=sum(1 for h in s.match_history if h["step"] != "match(round=0)"))
    if sum(guard.leaks.values()):
        res.unsupported.append("prompt_leak_blocked")
    res.final_snapshot, res.final_cohort = rec.state(s)
    for o in _observations(rec, s, run_id, res.final_snapshot, res.final_cohort):
        res.steps.append(rp.build_step(o))
    sentences = _patient_sentences(hc.case.raw_text)
    records = audit.RecordWriter(run_dir / audit.RECORDS_FILE, run_id, forbidden_texts=sentences, scanner=scanner)
    issues = rp.IssueWriter(run_dir / rp.ISSUES_FILE, run_id, forbidden_texts=sentences, scanner=scanner)
    for step in res.steps:
        rp.write_step(step, records, issues)
    res.verification = verify_run(res, scanner)
    return res


def verify_run(res: OfflineRunResult, scanner=None) -> list[str]:
    """저장한 두 파일을 다시 읽어 실행과 맞는지 확인. 문제 코드 목록 (빈 목록 = 정합). scanner를 주면 읽을 때도 값을 검사."""
    problems: list[str] = []
    run_dir = res.run_dir
    expected = {i + 1: rd.action.action_id for i, rd in enumerate(res.rounds)}
    if res.questions_in_session != len(res.rounds):
        problems.append("question_count_mismatch")
    if res.rematch_rounds_in_session != len(res.rounds):
        problems.append("rematch_count_mismatch")  # 질문 1개당 재매칭 1번이어야 귀속이 성립
    try:
        loaded = audit.load_run_v2(run_dir, scanner=scanner) if (run_dir / audit.RECORDS_FILE).exists() else None
    except audit.AuditError:
        return problems + ["records_unreadable"]
    records = loaded.records if loaded else []
    if loaded and loaded.issues:
        problems.extend(f"load:{x}" for x in loaded.issues)
    issue_path = run_dir / rp.ISSUES_FILE
    issues = rp.load_issues(issue_path) if issue_path.exists() else []
    if any(r.run_id != res.run_id for r in records) or any(i.run_id != res.run_id for i in issues):
        problems.append("run_id_mismatch")
    rec_steps = {r.step for r in records}
    blocked = {i.step for i in issues if i.severity == "blocking"}
    if rec_steps & blocked:
        problems.append("violation_step_has_record")
    if rec_steps | blocked != set(expected):
        problems.append("step_coverage_mismatch")
    for r in records:
        if expected.get(r.step) != r.action.action_id or (r.outcome and r.outcome.action_id != r.action.action_id):
            problems.append(f"record_action_mismatch:{r.step}")
    for i in issues:
        if expected.get(i.step) != i.action_id:
            problems.append(f"issue_action_mismatch:{i.step}")
    if len(records) != res.audit.records or len(issues) != sum(len(s.issues) for s in res.steps):
        problems.append("file_count_mismatch")
    problems.extend(_coverage_problems(res, {r.step: r for r in records}, issues))
    return problems


def _coverage_problems(res: OfflineRunResult, records: dict, issues: list) -> list[str]:
    """실제 상태 변화가 파일에 빠짐없이 있는지: 단계마다 (1) 상태가 바뀐 모든 쌍이 기록의 PairTransition 또는 위반 진단에
    있고 (2) 실제 새 사실이 모두 facts_added 또는 진단 fact_ids에 있는지. 실행 중 관찰한 전후 상태와 비교."""
    out: list[str] = []
    if res.final_cohort is None:
        return ["final_state_missing"] if res.rounds else []
    for i, rd in enumerate(res.rounds):
        step = i + 1
        nxt_snap, nxt_co = ((res.rounds[i + 1].snapshot, res.rounds[i + 1].cohort) if i + 1 < len(res.rounds)
                            else (res.final_snapshot, res.final_cohort))
        alias = rd.action.patient_alias
        after = {p.pair.trial_id: p.status for p in nxt_co.patients[alias].pairs}
        changed = {p.pair.trial_id for p in rd.cohort.patients[alias].pairs
                   if p.pair.trial_id in after and after[p.pair.trial_id] != p.status}
        rec = records.get(step)
        in_rec = {p.pair.trial_id for p in rec.pairs if p.before != p.after} if rec else set()
        in_issue = {x.trial_id for x in issues if x.step == step and x.severity == "blocking" and x.trial_id}
        for t in sorted(changed - in_rec - in_issue):
            out.append(f"pair_transition_missing:{step}:{t}")
        new = {f.fact_id for f in nxt_snap.facts} - {f.fact_id for f in rd.snapshot.facts}
        kept = set(rec.facts_added if rec else ()) | {f for x in issues if x.step == step for f in x.fact_ids}
        if new - kept:
            out.append(f"fact_missing_from_audit:{step}:{len(new - kept)}")
        if rec and set(rec.facts_added) - new:
            out.append(f"recorded_fact_not_new:{step}")
    return out


def normalized_files(run_dir: Path) -> str:
    """결정성 비교용: FactStore가 실행마다 새로 만드는 fact_id(uuid)를 등장 순서 번호로 바꾼 두 파일 내용."""
    import re
    text = ""
    for name in (audit.RECORDS_FILE, rp.ISSUES_FILE):
        p = Path(run_dir) / name
        text += f"== {name}\n" + (p.read_text(encoding="utf-8") if p.exists() else "")
    seen: dict[str, str] = {}
    return re.sub(r"F-[0-9a-f]{12}", lambda m: seen.setdefault(m.group(0), f"F#{len(seen)}"), text)


def write_summary(results: list[OfflineRunResult], path: Path) -> dict:
    total = {"runs": len(results), "complete_runs": sum(r.complete for r in results),
             "steps": sum(len(r.steps) for r in results), "records": sum(r.audit.records for r in results),
             "issues": sum(len(s.issues) for r in results for s in r.steps),
             "blocking": sum(r.audit.blocking for r in results), "warnings": sum(r.audit.warnings for r in results),
             "unsupported_runs": sum(bool(r.unsupported) for r in results),
             "verification_problems": sum(len(r.verification) for r in results),
             "selected_not_asked": sum(r.selected_not_asked for r in results)}
    codes: dict[str, int] = {}
    for r in results:
        for k, v in r.summary()["issue_codes"].items():
            codes[k] = codes.get(k, 0) + v
    out = {"total": total, "issue_codes": codes, "runs": [r.summary() for r in results]}
    Path(path).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


__all__ = ["OfflineRunResult", "record_case", "verify_run", "normalized_files", "write_summary",
           "QUESTIONS_PER_ROUND", "METHOD"]
