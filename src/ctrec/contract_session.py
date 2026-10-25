"""계약 기반 Session 어댑터 (재설계 P1·P3·P4·P5 통합). pipeline.py·matcher.py(보호 파일)를 바꾸지 않고 상속으로 연결합니다.

기존 Session과 다른 점
1. 프로필 격리 (P1, Q&A -> Profiler -> 판정 우회 차단): 환자 프로필은 처음 입력만으로 추출하고, 확인 답변 뒤에 다시
   추출하지 않습니다. 따라서 규칙 엔진(나이·성별·ECOG·검사)과 Matcher의 프로필에는 답변에서 나온 값이 없습니다.
   답변 원문은 기존처럼 원문 뒤에 붙여 기록·Matcher 참고용으로 남기되(인용·출처 검사 대상),
2. Q&A 안전 경계 (P1, qa_guard): 계약 없는 답변에 기댄 확정 판정은 UNVERIFIED(답변상 판정 보존)로 되돌립니다.
3. 결정적 평가 (P4, evaluator): 활성(사람 검수 승인 + 잠긴 원문 일치) IR이 있는 기준은 계약 명제 사실로 평가하고,
   확정이 나오면 그 결과를 씁니다. 처음 입력으로 내린 반대 확정과 부딪히면 상충(UNVERIFIED)으로 둡니다.
   평가기가 결론을 못 내면(사실 없음·부분 IR·기준 시점 미승인) 기존 판정(2를 거친 것)을 그대로 둡니다.
4. 계약 질문 (P3): 활성 IR의 미확인 명제를 정할 수 있는 활성 계약 질문을 먼저 묻고(문장·선택지 고정, LLM 미사용),
   남은 질문 수만 기존 질문 생성 에이전트가 만듭니다. 같은 계약은 여러 시험·기준이 한 번의 답을 공유합니다.
   계약 답은 선택지 일치로만 명제 사실(fact_store "prop:<키>")이 되고, 자유 서술은 사실 후보로만 남습니다.

활성 IR·계약이 하나도 없으면(현재 저장소 상태: 모든 초안이 검수 전) 3·4는 아무것도 하지 않고 1·2만 적용됩니다.
진단 기능(dependency_diagnostic, decision_loop)은 기존 Session.match 안에서 경계 적용 전 판정으로 계산됩니다(기본 off).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import acquisition
from .agents import matcher, question_generator
from .evaluator import evaluate
from .ir_review import Registry
from .pipeline import Session, trace_match
from .proposition_store import PropositionLedger
from .propositions import Proposition
from .qa_guard import downgrade, qa_dependent, recompose
from .question_contract import ContractRegistry, map_answer, render
from .schemas import ClarifyingQuestion, CriterionAssessment, QAPair, TrialMatch
from .tools.rule_engine import passes as rule_passes


def merge(base: CriterionAssessment, ev: CriterionAssessment) -> CriterionAssessment:
    """기존 판정(경계 적용 후)과 평가기 판정을 합침. 어느 쪽도 상대를 근거 없이 덮지 않음."""
    def conflict(note: str) -> CriterionAssessment:
        return ev.model_copy(update={
            "holds": "unknown", "passes": None, "applicable": True, "confidence": "low", "needs_verification": True,
            "validation_flags": [*ev.validation_flags, "contract_vs_record_conflict"],
            "reasoning": f"[상충: {note} - 기존 판정 '{base.holds}'/{base.passes}] {ev.reasoning}",
            "missing_info": "처음 입력(기록)과 계약 답변이 다름: 진료 기록·담당 의료진으로 확인"})

    if "conflicting_contract_answers" in ev.validation_flags:
        return ev
    if ev.passes is not None:  # 평가기 확정
        if base.passes is not None and base.passes != ev.passes:
            return conflict("처음 입력에 근거한 판정과 계약 답변 판정이 반대")
        return ev
    if base.passes is not None:  # 평가기 UNVERIFIED(진료 기록 필요) + 기존 판정이 기록에 근거해 확정
        said = True if not ev.self_reported_applicable else rule_passes(ev.kind, ev.self_reported_holds or "unknown")
        return conflict("기록 판정과 계약 답변(자기 보고)이 반대") if said is not None and said != base.passes else base
    return ev


@dataclass
class ContractSession(Session):
    contract_root: Path | None = None  # IR·계약 사이드카 (기본: 저장소 contracts/)
    contract_parsed_dir: Path | None = None  # 잠긴 기준표 위치 (기본: config.PARSED_DIR, 테스트만 바꿈)
    initial_text: str = ""
    ledger: PropositionLedger | None = None
    contract_provenance: dict = field(default_factory=dict)  # "<trial>:<rule>" -> 평가기 출처
    contract_asked: list[dict] = field(default_factory=list)  # 계약 질문 기록 (라운드·질문 ID·계약·대상)
    _registry: Registry | None = field(default=None, repr=False)
    _contracts: ContractRegistry | None = field(default=None, repr=False)
    _asked_questions: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.initial_text = self.case.raw_text
        self._registry = Registry(self.contract_root, self.contract_parsed_dir)
        self._contracts = ContractRegistry(self._registry)
        if self.ledger is None:
            self.ledger = PropositionLedger(self.fact_store)

    # ----------------------------------------------------------- 1. 프로필 격리
    def profile_patient(self):
        if self.profile is not None and self.rounds > 0:
            self.tracer.log("patient_profiler", "skipped", round=self.rounds,
                            message="계약 Session: 확인 답변으로 프로필을 다시 추출하지 않음 (처음 입력만)")
            return self.profile
        full = self.case.raw_text
        self.case.raw_text = self.initial_text
        try:
            return super().profile_patient()
        finally:
            self.case.raw_text = full

    # ----------------------------------------------------------- 2·3. 경계 + 결정적 평가
    def match(self, trial_ids: list[str] | None = None) -> list[TrialMatch]:
        out = super().match(trial_ids)
        changed = False
        for i, m in enumerate(out):
            new = self._contract_and_guard(m)
            if new is not m:
                out[i] = self.matches[m.trial_id] = new
                changed = True
        if changed:  # 기록·확보 상태를 경계 적용 후 판정으로 갱신
            self.match_history[-1]["eligibility"] = {tid: m.eligibility for tid, m in self.matches.items()}
            acquisition.update(self.missing_facts, self.matches.values())
        return out

    def _facts(self):
        return self.ledger.facts(self.case.patient_id, self.session_id)  # type: ignore[union-attr]

    def _contract_and_guard(self, m: TrialMatch) -> TrialMatch:
        parsed = self.parsed.get(m.trial_id)
        if m.eligibility == "NOT_EVALUATED" or parsed is None:
            return m
        current = {a.rule_id: a for a in m.assessments + m.other_cohort_assessments}
        replaced = {rid: downgrade(a) for rid, a in current.items()
                    if qa_dependent(a, initial_text=self.initial_text, qa_log=self.qa_log)}  # 구조 모드
        irs = self._registry.active_irs(m.trial_id)  # type: ignore[union-attr]
        facts = self._facts() if irs else []
        for rule in parsed.rules:
            ir = irs.get(rule.rule_id)
            if ir is None or rule.rule_id not in current:
                continue
            res = evaluate(ir, rule, facts, self.reference_date, self._contracts,  # type: ignore[arg-type]
                           required_evidence=matcher._meta(parsed, rule).required_evidence)
            key = f"{m.trial_id}:{rule.rule_id}"
            self.contract_provenance[key] = {**res.provenance, "round": self.rounds}
            if res.decided and res.assessment is not None:
                base = replaced.get(rule.rule_id, current[rule.rule_id])
                replaced[rule.rule_id] = merge(base, res.assessment)
        if not replaced:
            return m
        new = recompose(m, parsed, replaced)
        # 경계·평가기 적용 후 판정도 trace에 남김 (matcher done은 적용 전 판정). Inspector는 이 기록을 그 라운드의 최종
        # 판정으로 읽음 - 없으면 result.json(적용 후)과 trace(적용 전)가 달라 기록 불일치로 보였음 (P9)
        self.tracer.log("contract", "applied", trial_id=m.trial_id, round=self.rounds,
                        guarded=sorted(r for r, a in replaced.items() if "qa_uncontracted" in a.validation_flags),
                        contract=sorted(r for r, a in replaced.items() if a.method == "contract"), **trace_match(new))
        return new

    # ----------------------------------------------------------- 4. 계약 질문
    def _contract_plan(self, targets: list[TrialMatch]) -> list[tuple]:
        """[(계약, ["<trial>:<rule>", ...])]: 활성 IR의 미확인 명제를 정할 수 있고 이 세션에서 아직 묻지 않은 계약."""
        asked = {q["contract_id"] for q in self.contract_asked}
        plan: dict[str, tuple] = {}
        for m in targets:
            for a in m.assessments:
                if a.passes is not None:
                    continue
                key = f"{m.trial_id}:{a.rule_id}"
                prov = self.contract_provenance.get(key)
                if not prov or prov.get("outcome") not in ("undecided",):
                    continue
                for leaf in prov["leaves"]:
                    if leaf["status"] != "missing":
                        continue
                    for c in self._contracts.deciding(Proposition.parse(leaf["proposition"])):  # type: ignore
                        if c.contract_id in asked:
                            continue
                        entry = plan.setdefault(c.contract_id, (c, []))
                        if key not in entry[1]:
                            entry[1].append(key)
                        break
        return [plan[k] for k in sorted(plan)]

    def uncertain(self) -> list[TrialMatch]:
        base = super().uncertain()
        seen = {m.trial_id for m in base}
        extra = [m for m in self.open_candidates() if m.trial_id not in seen and m.eligibility == "UNCERTAIN"
                 and self._contract_plan([m])]
        return base + extra

    def clarify(self) -> dict:
        """확인 질문 1라운드: 계약 질문(고정 문장) 먼저 -> 남은 수만 LLM 질문 -> 답변 기록 -> 재평가 (프로필 재추출 없음)."""
        if self.rounds >= self.max_rounds:
            return {"status": "skipped", "reason": f"최대 라운드({self.max_rounds}) 도달"}
        if self.remaining_questions() <= 0:
            return {"status": "skipped", "reason": f"질문 예산({self.question_budget}개) 소진"}
        targets = self.open_candidates()
        plan = self._contract_plan(targets)
        llm_needed = bool(super().uncertain()) or self.pending_physician_review()
        if not (plan or llm_needed):
            return {"status": "skipped", "reason": "판정 불가 항목과 의사 확인용 미질문 항목 없음"}
        self.rounds += 1
        budget = self.remaining_questions()
        contract_qs = [(c, render(c, f"C{i}", keys)) for i, (c, keys) in enumerate(plan[:budget], 1)]
        llm_qs: list[ClarifyingQuestion] = []
        if budget > len(contract_qs) and llm_needed:
            qs = question_generator.generate_questions(
                self.profile, targets, self.qa_log, budget - len(contract_qs),
                acquisition_plan={k: v for k, v in acquisition.plan(self.missing_facts).items()
                                  if k.split(":")[0] in {m.trial_id for m in targets}})
            acquisition.record_attempts(self.missing_facts, qs.questions, self.rounds)
            llm_qs = [q for q in qs.questions if not q.question_id.startswith("C")]  # 계약 질문 ID와 겹치지 않게
        questions = [q for _, q in contract_qs] + llm_qs
        self.tracer.log("question_generator", "questions", round=self.rounds,
                        questions=[q.model_dump() for q in questions],
                        contract_questions=[{"question_id": q.question_id, "contract_id": c.contract_id}
                                            for c, q in contract_qs],
                        message=f"{len(questions)}개 질문 생성 (계약 {len(contract_qs)}개)")
        if not questions:
            return {"status": "no_questions"}
        answers = self.answerer.answer(questions, self.case.patient_id)
        cid = {q.question_id: c.contract_id for c, q in contract_qs}
        new_pairs = [QAPair(round=self.rounds, question_id=q.question_id, question=q.question,
                            answer=answers.get(q.question_id, "모름"), purpose=q.purpose, target=q.target,
                            session_id=self.session_id, contract_id=cid.get(q.question_id))
                     for q in questions]
        self.qa_log.extend(new_pairs)
        self.tracer.log("answerer", "answers", round=self.rounds, qa=[p.model_dump() for p in new_pairs])
        for c, q in contract_qs:
            mapped = map_answer(c, q, answers.get(q.question_id))
            facts = self.ledger.record(self.case.patient_id, self.session_id, c, mapped,  # type: ignore[union-attr]
                                       round_no=self.rounds, question_id=q.question_id, as_of=self.reference_date)
            self.contract_asked.append({"round": self.rounds, "question_id": q.question_id,
                                        "contract_id": c.contract_id, "contract_version": c.version,
                                        "target": list(q.target), "mapped": mapped.kind,
                                        "fact_ids": [f.fact_id for f in facts]})
        block = "\n".join(f"Q: {p.question}\nA: {p.answer}" for p in new_pairs)
        self.case.raw_text += f"\n\n[추가 확인 정보 - 라운드 {self.rounds}]\n{block}"
        before = {m.trial_id: m.eligibility for m in targets}
        self.profile_patient()
        after = self.match([m.trial_id for m in targets])
        changes = {m.trial_id: f"{before[m.trial_id]} -> {m.eligibility}" for m in after}
        return {"status": "done", "round": self.rounds, "qa": [p.model_dump() for p in new_pairs], "changes": changes}

    # ----------------------------------------------------------- 기록
    def result(self) -> dict:
        out = super().result()
        pid = self.case.patient_id
        facts = self.ledger.facts(pid, self.session_id, include_superseded=True)  # type: ignore[union-attr]
        out["contract"] = {
            "session_type": "contract",
            "profile_source": "initial_input_only",
            "ir_status": {tid: {rid: r.status for rid, r in self._registry.trial_records(tid).items()}  # type: ignore
                          for tid in self.matches},
            "provenance": self.contract_provenance,
            "questions": self.contract_asked,
            "facts": [{"fact_id": f.fact_id, "proposition_key": f.proposition.key, "value": f.value,
                       "as_of": f.as_of, "contract_id": f.contract_id, "contract_version": f.contract_version,
                       "question": f"r{f.round}:{f.question_id}", "source": f.source,
                       "superseded": f.fact_id in self.ledger.superseded} for f in facts],  # type: ignore
            "superseded": self.ledger.superseded,  # type: ignore[union-attr]
            "free_text_candidates": self.ledger.candidates,  # type: ignore[union-attr]
            "rejected_answers": self.ledger.rejected,  # type: ignore[union-attr]
        }
        return out


def session_class():
    """실행 진입점(CLI·웹)이 쓸 Session 종류. 환경 변수 CTREC_SESSION=contract일 때만 ContractSession (기본은 기존 Session).

    진입점 연결은 docs/proposals/의 패치 제안(cli.py·web/app.py)으로 남겨 두었습니다(다른 브랜치와 충돌 가능).
    """
    import os
    return ContractSession if os.getenv("CTREC_SESSION", "").strip().lower() == "contract" else Session
