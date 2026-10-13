"""파이프라인 상태(Session)와 6단계 실행 로직.

두 가지 실행 모드가 같은 Session을 공유합니다.
- fixed : 과제의 6단계를 정해진 순서로 실행 (재현성·평가용)
- agent : 오케스트레이터 에이전트가 스스로 계획을 세우고 Session의 단계를 도구로 호출 (orchestrator.py)
"""

from __future__ import annotations

import contextvars
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import DISCLAIMER, acquisition, config, evidence, fact_compare, incremental, llm, profile_facts, trial_store
from .agents import criteria_parser, explainer, matcher, patient_profiler, question_generator, recommender, screener
from .agents.answerers import Answerer
from .fact_changes import take_snapshot
from .fact_store import FactStore
from .schemas import ParsedTrial, PatientProfile, QAPair, Recommendation, TrialMatch
from .trace import Tracer


@dataclass
class PatientCase:
    patient_id: str
    raw_text: str
    hidden_details: str | None = None  # 가상 환자 시뮬레이션용 (파이프라인에는 노출되지 않음)
    candidate_trials: list[str] | None = None  # 이 환자에게 평가할 후보 시험 ID (없으면 전체)
    oracle_facts: list[dict] | None = None  # 정보 가리기 평가셋: 전체 사실 목록 (가상 환자만 사용, 파이프라인에는 노출되지 않음)
    # 최초 입력(raw_text)의 선언된 출처: clinical_record / self_report / unknown (evidence.InputSource).
    # 선언이 없으면 unknown -> 진료 기록이 필요한 기준은 UNVERIFIED (문자열이라는 이유로 진료 기록으로 보지 않음)
    input_source: str = "unknown"


# 파이프라인에 노출하지 않는 필드 (시뮬레이터·평가용 메타데이터)
_HIDDEN_KEYS = {
    "hidden_details", "labels", "design_note", "synthetic", "base_topic", "candidate_trials", "input_source",
    # 정보 가리기 평가셋(scripts/build_masked_benchmark.py)의 정답·숨긴 사실
    "oracle_facts", "hidden_facts", "masked_fact_ids", "clue_fact_ids", "masked_criteria", "case_type", "source_patient",
}


def _case_from_dict(data: dict, default_id: str) -> PatientCase:
    pid = str(data.get("patient_id") or data.get("num") or data.get("id") or default_id)
    hidden = data.get("hidden_details")
    if "text" in data:
        text = data["text"]
    elif set(data) - _HIDDEN_KEYS <= {"patient_id", "num", "id", "title"} and "title" in data:
        text = data["title"]  # TREC topic 형식: title이 곧 환자 서술
    else:
        visible = {k: v for k, v in data.items() if k not in _HIDDEN_KEYS}
        text = json.dumps(visible, ensure_ascii=False, indent=1)
    source = data.get("input_source", "unknown")
    if source not in evidence.INPUT_SOURCES:
        raise ValueError(f"{pid}: input_source는 {', '.join(evidence.INPUT_SOURCES)} 중 하나여야 합니다 (받은 값: {source!r})")
    return PatientCase(patient_id=pid, raw_text=text, hidden_details=hidden,
                       candidate_trials=data.get("candidate_trials"), oracle_facts=data.get("oracle_facts"),
                       input_source=source)


def load_patients(path: Path) -> list[PatientCase]:
    """환자 파일 로드. 한 파일에 여러 환자가 있을 수 있음.

    지원 형식:
    - .txt: 자유 서술 1명
    - {"patient_id", "text", ...}: 1명 (권장)
    - {"topics": [{"num", "title"}, ...]}: 사업단 제공 예시(TREC topic) 형식, 여러 명
    - [{...}, {...}]: 여러 명
    그 외 JSON 필드는 텍스트로 직렬화해 환자 정보 이해 에이전트에 전달합니다.
    JSON 환자마다 "input_source"(clinical_record / self_report / unknown)로 최초 입력의 출처를 밝힐 수 있고,
    없으면(.txt 포함) unknown입니다.
    """
    if path.suffix.lower() == ".txt":
        return [PatientCase(patient_id=path.stem, raw_text=path.read_text(encoding="utf-8"))]
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("topics"), list):
        items = data["topics"]
    elif isinstance(data, list):
        items = data
    else:
        return [_case_from_dict(data, path.stem)]
    return [_case_from_dict(d, f"{path.stem}-{i + 1}") for i, d in enumerate(items)]


def _not_evaluated(patient_id: str, trial_id: str, error: str) -> TrialMatch:
    return TrialMatch(
        patient_id=patient_id, trial_id=trial_id, eligibility="NOT_EVALUATED", assessments=[],
        n_pass=0, n_fail=0, n_unknown=0, evaluation_error=error,
        summary=f"NOT_EVALUATED: 평가 실패로 판정하지 못함(부적격 아님) - {error[:200]}",
    )


@dataclass
class Session:
    case: PatientCase
    trials: dict[str, dict]
    answerer: Answerer
    tracer: Tracer
    max_rounds: int = config.MAX_CLARIFY_ROUNDS
    allow_search: bool = False
    question_budget: int | None = None  # 전체 라운드 합산 최대 질문 수 (None = 라운드당 한도만 적용)
    screen_trials: bool = False  # True: trials 전체를 풀로 보고 후보 추림 후 자세히 평가
    max_candidates: int = config.SCREEN_MAX_CANDIDATES

    profile: PatientProfile | None = None
    parsed: dict[str, ParsedTrial] = field(default_factory=dict)
    matches: dict[str, TrialMatch] = field(default_factory=dict)
    qa_log: list[QAPair] = field(default_factory=list)
    match_history: list[dict] = field(default_factory=list)
    rounds: int = 0
    recommendation: Recommendation | None = None
    report: str | None = None
    errors: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # 이 환자 처리에 쓴 토큰 (저장 직전에 기록)
    candidates: list[str] | None = None  # 자세히 평가할 시험 (None = trials 전체)
    screening: screener.ScreeningResult | None = None
    # 판정 기준일: 세션 시작 시 한 번 고정 (자정을 넘기거나 재평가해도 같은 기준일)
    reference_date: str = field(default_factory=config.reference_date)
    # 판정 불가 항목별 확보 시도 기록 (D7): "<trial_id>:<rule_id>" -> MissingFact
    missing_facts: dict[str, acquisition.MissingFact] = field(default_factory=dict)
    # 환자 사실 저장소 병행 기록(shadow, Phase 1-3A): 프로필을 추출할 때마다 사실로 옮겨 이력·출처·충돌을 남김.
    # 판정·질문·추천에는 쓰지 않으며(모두 기존 profile 사용), 기록에 실패해도 파이프라인은 그대로 진행.
    # 기본은 이 Session의 메모리에만 두고 파일로 저장하지 않음. fact_store_dir는 개발·테스트용 명시 옵션.
    session_id: str = field(default_factory=lambda: f"S-{uuid.uuid4().hex[:12]}")
    fact_shadow: bool = True
    fact_store_dir: Path | None = None
    fact_store: FactStore = field(default_factory=FactStore)
    fact_shadow_log: list[dict] = field(default_factory=list)  # 기록 결과 (건수·상태만, 환자 값은 넣지 않음)
    # 사실 기반 비교 Matcher (진단용, Phase 1-3B/1-4): 기본 off. "deterministic"은 LLM 없이 규칙 엔진 기준만,
    # "llm"은 Matcher를 사실 기반 프로필로 한 번 더 실행(유료 API, 명시적 선택). 결과는 fact_comparison에만 두고
    # 판정·질문·추천에는 쓰지 않음. 실패해도 추천 흐름은 그대로.
    fact_compare: str = "off"
    fact_compare_strict: bool = False  # True면 진료 기록 기준에 문서 검증(document_verified)을 요구
    fact_comparison: dict | None = None
    # 의존 그래프·부분 재평가 진단 (Phase 2): 기본 off. 매칭이 끝날 때마다 사실 스냅샷으로 부분 재평가와 전체 평가를 하고
    # 비교 결과를 dependency_diagnostics에만 둠. LLM 판정은 기존 Matcher가 이미 낸 값만 쓰며(새 호출 없음),
    # 판정·질문·추천에는 쓰지 않음. 실패해도 추천 흐름은 그대로.
    dependency_diagnostic: bool = False
    dependency_diagnostics: list[dict] = field(default_factory=list)
    _dependency_tracker: incremental.DependencyTracker | None = field(default=None, repr=False)
    # Agentic Decision Loop 진단 (Phase 3): 기본 "off". "diagnostic"이면 매칭이 끝날 때마다 현재 사실·판정을 읽어
    # 다음 행동 계획(질문 후보·문서 요청·검토·종료 사유)을 decision_diagnostics에만 기록. 질문을 보내거나 판정·질문·추천을
    # 바꾸지 않고 LLM을 부르지 않음. 실패해도 추천 흐름은 그대로.
    decision_loop: str = "off"
    decision_diagnostics: list[dict] = field(default_factory=list)
    _decision_tracker: object | None = field(default=None, repr=False)

    # ----------------------------------------------------------- 후보
    def candidate_ids(self) -> list[str]:
        return list(self.trials) if self.candidates is None else [t for t in self.candidates if t in self.trials]

    def candidate_trials(self) -> dict[str, dict]:
        return {t: self.trials[t] for t in self.candidate_ids()}

    def add_candidates(self, trial_ids: list[str]) -> None:
        """검색 등으로 새로 찾은 시험을 후보에 추가 (추림을 했더라도 직접 찾은 시험은 바로 후보)."""
        if self.candidates is not None:
            self.candidates += [t for t in trial_ids if t not in self.candidates]

    def screen(self) -> screener.ScreeningResult:
        """후보 추림: 전체 시험 풀을 한 번에 훑어 자세히 평가할 시험을 고름."""
        assert self.profile is not None, "profile_patient()를 먼저 실행하세요"
        self.tracer.log("screener", "start", pool=len(self.trials))
        r = screener.screen(self.profile, self.trials, self.max_candidates, self.case.raw_text)
        self.screening = r
        self.candidates = [s.trial_id for s in r.selected]
        self.tracer.log("screener", "done", selected=self.candidates,
                        message=f"전체 {r.pool_size}개 중 {len(r.selected)}개 선택 "
                                f"(확정 제외 {len(r.hard_filtered)}, 제외 {len(r.excluded)}, 상한 초과 {len(r.over_limit)})")
        return r

    # ----------------------------------------------------------- 단계 ①+③a
    def parse_criteria(self, trial_ids: list[str] | None = None) -> dict[str, str]:
        """반환: 파싱에 실패한 시험 {trial_id: 오류}."""
        ids = [t for t in (trial_ids or self.candidate_ids()) if t not in self.parsed]

        def work(tid: str):
            # 사전 컴파일한 고정 기준을 불러옴 (D8). 범위 밖 시험은 CompilationPending -> NOT_EVALUATED
            self.tracer.log("criteria_parser", "start", trial_id=tid)
            p = trial_store.get(self.trials[tid])
            self.tracer.log("criteria_parser", "done", trial_id=tid, n_rules=len(p.rules),
                            integrity_issues=p.integrity_issues,
                            review=[rid for rid, m in p.rule_meta.items() if m.review])
            return tid, p

        results, failed = self._parallel(work, ids)
        for tid, p in results:
            self.parsed[tid] = p
        return failed

    # ----------------------------------------------------------- 단계 ②+③b
    def profile_patient(self) -> PatientProfile:
        self.tracer.log("patient_profiler", "start", patient_id=self.case.patient_id)
        self.profile = patient_profiler.profile_patient(self.case.patient_id, self.case.raw_text,
                                                        reference_date=self.reference_date)
        self.tracer.log("patient_profiler", "done", missing=self.profile.missing_or_ambiguous)
        self._shadow_record()
        return self.profile

    def _shadow_record(self) -> None:
        """프로필을 환자 사실 저장소에 병행 기록. 프로필 사본을 넘기고, 어떤 오류도 판정 흐름으로 번지지 않게 함.

        출처는 profile_facts가 인용 위치로 정함: 처음 입력에서 확인되면 선언된 출처, 확인 질문 답변에서만 확인되면
        self_report, 확인하지 못하면 unknown. 다시 추출한 프로필의 같은 근거는 새 사실을 만들지 않음.
        """
        if not self.fact_shadow or self.profile is None:
            return
        pid = self.case.patient_id
        entry: dict = {"round": self.rounds, "session_id": self.session_id}
        before = len(self.fact_store.facts(pid))
        try:
            r = profile_facts.ingest_profile(self.fact_store, self.profile.model_copy(deep=True),
                                             raw_text=self.case.raw_text, input_source=self.case.input_source,
                                             session_id=self.session_id)
            entry.update(status="partial" if r.rejected else "ok", added=len(r.added), duplicates=len(r.duplicates),
                         skipped=len(r.skipped), rejected=len(r.rejected),
                         rejected_kinds=sorted({name.split(":")[0] for name, _ in r.rejected}))
            if self.fact_store_dir is not None:
                self.fact_store.root = self.fact_store_dir
                self.fact_store.save()
                entry["saved"] = True
        except Exception as e:  # noqa: BLE001 - 병행 기록 실패는 판정에 영향을 주지 않고 상태로 남김
            # 예외 메시지에는 환자 값이 들어 있을 수 있으므로 종류만 기록
            entry.update(status="error", error_type=type(e).__name__,
                         added_before_error=len(self.fact_store.facts(pid)) - before)
            if self.fact_store_dir is not None:
                entry["saved"] = False
        self.fact_shadow_log.append(entry)
        self.tracer.log("fact_store", entry["status"], **{k: v for k, v in entry.items() if k != "status"})

    def _run_fact_comparison(self) -> None:
        """사실 기반 비교 (진단용). 추천이 끝난 뒤 기준선 판정 사본과 비교하고, 결과는 fact_comparison에만 둠."""
        if self.fact_compare == "off":
            return
        if self.fact_compare not in ("deterministic", "llm"):
            self.fact_comparison = {"status": "error", "error_type": "InvalidMode"}
        elif not self.fact_shadow:
            self.fact_comparison = {"status": "skipped", "reason": "fact_shadow가 꺼져 있어 사실이 없음"}
        else:
            try:
                out = fact_compare.compare_session(
                    self.fact_store, self.case.patient_id,
                    {t: m.model_copy(deep=True) for t, m in self.matches.items()}, self.parsed, self.trials,
                    self.reference_date, mode=self.fact_compare,  # type: ignore[arg-type]
                    require_document_verification=self.fact_compare_strict)
                self.fact_comparison = {"status": "ok", **out}
            except Exception as e:  # noqa: BLE001 - 비교 실패는 추천에 영향을 주지 않고 상태로 남김
                self.fact_comparison = {"status": "error", "error_type": type(e).__name__}
        c = self.fact_comparison
        self.tracer.log("fact_compare", c["status"], mode=self.fact_compare,
                        risks=len(c.get("risks", [])), changes=c.get("summary", {}).get("changes"))

    # ----------------------------------------------------------- 단계 ③c, ④
    def match(self, trial_ids: list[str] | None = None) -> list[TrialMatch]:
        assert self.profile is not None, "profile_patient()를 먼저 실행하세요"
        ids = trial_ids or self.candidate_ids()
        parse_failed = self.parse_criteria(ids)

        def work(tid: str):
            self.tracer.log("matcher", "start", trial_id=tid)
            m = matcher.match_trial(self.profile, self.parsed[tid], self.trials[tid],
                                    reference_date=self.reference_date, source_text=self.case.raw_text,
                                    input_source=self.case.input_source)
            self.tracer.log("matcher", "done", trial_id=tid, message=m.summary)
            return tid, m

        results, match_failed = self._parallel(work, [t for t in ids if t in self.parsed])
        out = []
        for tid, m in results:
            self.matches[tid] = m
            out.append(m)
        # 평가 실패도 공식 결과로 남김 (조용히 빠지지 않도록, 부적격으로 처리하지도 않음)
        failures = {**{t: f"기준 파싱 실패: {e}" for t, e in parse_failed.items()},
                    **{t: f"적격성 판정 실패: {e}" for t, e in match_failed.items()}}
        for tid in ids:
            if tid in failures:
                m = _not_evaluated(self.case.patient_id, tid, failures[tid])
                self.matches[tid] = m
                out.append(m)
        self._snapshot(f"match(round={self.rounds})")
        acquisition.update(self.missing_facts, self.matches.values())
        self._run_dependency_diagnostic({tid for tid, _ in results})
        self._run_decision_diagnostic({tid for tid, _ in results})
        return out

    def _run_decision_diagnostic(self, fresh: set[str]) -> None:
        """Decision Loop 진단 (기본 off). Session 사본만 읽고 결과는 decision_diagnostics에만 둠."""
        if self.decision_loop == "off":
            return
        entry: dict = {"round": self.rounds}
        if self.decision_loop != "diagnostic":
            entry.update(status="error", error_type="InvalidMode")
        elif not self.fact_shadow:
            entry.update(status="skipped", reason="fact_shadow가 꺼져 있어 사실이 없음")
        else:
            try:
                from . import decision_loop
                if self._decision_tracker is None:
                    self._decision_tracker = decision_loop.DecisionTracker()
                entry.update(status="ok", **self._decision_tracker.observe(self, fresh))
            except Exception as e:  # noqa: BLE001 - 진단 실패는 추천에 영향을 주지 않고 상태로 남김 (종류만 기록)
                entry.update(status="error", error_type=type(e).__name__)
        self.decision_diagnostics.append(entry)
        self.tracer.log("decision", entry["status"], round=self.rounds,
                        stop_reason=entry.get("stop_reason"), diagnostic=entry)

    def _run_dependency_diagnostic(self, fresh: set[str]) -> None:
        """부분 재평가 진단 (기본 off). 사실 저장소의 스냅샷과 판정 사본만 읽고, 결과는 dependency_diagnostics에만 둠."""
        if not self.dependency_diagnostic:
            return
        entry: dict = {"round": self.rounds}
        if not self.fact_shadow:
            entry.update(status="skipped", reason="fact_shadow가 꺼져 있어 사실이 없음")
        else:
            try:
                pid = self.case.patient_id
                if self._dependency_tracker is None:
                    self._dependency_tracker = incremental.DependencyTracker(
                        pid, incremental.EvaluationPolicy(reference_date=self.reference_date))
                matches = {t: m.model_copy(deep=True) for t, m in self.matches.items()}
                parsed = {t: None if m.eligibility == "NOT_EVALUATED" else self.parsed.get(t) for t, m in matches.items()}
                out = self._dependency_tracker.observe(take_snapshot(self.fact_store, pid), parsed,
                                                       incremental.baseline_assessor(matches, fresh))
                entry.update(status="ok", **out)
            except Exception as e:  # noqa: BLE001 - 진단 실패는 추천에 영향을 주지 않고 상태로 남김 (종류만 기록)
                entry.update(status="error", error_type=type(e).__name__)
        self.dependency_diagnostics.append(entry)
        self.tracer.log("dependency", entry["status"], round=self.rounds, mode=entry.get("mode"),
                        equivalence=entry.get("equivalence", {}).get("status"))

    def uncertain(self) -> list[TrialMatch]:
        """확인 질문 대상: 판정 보류이면서 현재 참여 가능한(모집 중) 시험 중 질문으로 해소할 항목이 있는 시험.

        (의사 확인 필요 항목만 남은 시험은 확인 질문으로 해소되지 않으므로 제외)
        """
        return [
            m for m in self.matches.values()
            if m.eligibility == "UNCERTAIN" and recommender.is_open(self.trials[m.trial_id])
            and any(matcher.is_askable_unknown(a)
                    and acquisition.is_open(self.missing_facts, f"{m.trial_id}:{a.rule_id}")
                    for a in m.assessments)
        ]

    def open_candidates(self) -> list[TrialMatch]:
        """추천 가능성이 있는 시험: 모집 중 + ELIGIBLE/UNCERTAIN."""
        return [
            m for m in self.matches.values()
            if recommender.is_candidate(m) and recommender.is_open(self.trials[m.trial_id])
        ]

    def pending_physician_review(self) -> bool:
        """원문에 구체 기준이 없는 항목이 있는데 아직 참고용 질문을 하지 않았는가."""
        has_items = any(matcher.needs_physician_review(a) for m in self.open_candidates() for a in m.assessments)
        asked = any(qa.purpose == "physician_reference" for qa in self.qa_log)
        return has_items and not asked

    def remaining_questions(self) -> int:
        """이번 라운드에 할 수 있는 질문 수 (라운드당 한도와 남은 전체 예산 중 작은 값)."""
        per_round = 1 if config.QUESTION_POLICY == "single" else config.MAX_QUESTIONS_PER_ROUND
        if self.question_budget is None:
            return per_round
        return max(0, min(per_round, self.question_budget - len(self.qa_log)))

    def needs_clarification(self) -> bool:
        return (self.rounds < self.max_rounds and self.remaining_questions() > 0
                and (bool(self.uncertain()) or self.pending_physician_review()))

    # ----------------------------------------------------------- 단계 ⑤
    def clarify(self) -> dict:
        """확인 질문 1라운드: 질문 생성 -> 답변 수집 -> 프로파일 갱신 -> 재평가.

        질문 대상: 모집 중 시험의 판정 불가 항목(적격성용) + 원문에 구체 기준이 없는 항목(의사 참고용).
        """
        if self.rounds >= self.max_rounds:
            return {"status": "skipped", "reason": f"최대 라운드({self.max_rounds}) 도달"}
        if self.remaining_questions() <= 0:
            return {"status": "skipped", "reason": f"질문 예산({self.question_budget}개) 소진"}
        if not (self.uncertain() or self.pending_physician_review()):
            return {"status": "skipped", "reason": "판정 불가 항목과 의사 확인용 미질문 항목 없음"}
        targets = self.open_candidates()

        self.rounds += 1
        qs = question_generator.generate_questions(
            self.profile, targets, self.qa_log, self.remaining_questions(),
            acquisition_plan={k: v for k, v in acquisition.plan(self.missing_facts).items()
                              if k.split(":")[0] in {m.trial_id for m in targets}},
        )
        acquisition.record_attempts(self.missing_facts, qs.questions, self.rounds)
        self.tracer.log("question_generator", "questions", round=self.rounds,
                        questions=[q.model_dump() for q in qs.questions],
                        message=f"{len(qs.questions)}개 질문 생성")
        if not qs.questions:
            return {"status": "no_questions"}

        answers = self.answerer.answer(qs.questions, self.case.patient_id)
        new_pairs = [
            QAPair(round=self.rounds, question_id=q.question_id, question=q.question,
                   answer=answers.get(q.question_id, "모름"), purpose=q.purpose, target=q.target)
            for q in qs.questions
        ]
        self.qa_log.extend(new_pairs)
        self.tracer.log("answerer", "answers", round=self.rounds, qa=[p.model_dump() for p in new_pairs])

        # 답변을 환자 정보에 추가 후 재추출·재평가
        block = "\n".join(f"Q: {p.question}\nA: {p.answer}" for p in new_pairs)
        self.case.raw_text += f"\n\n[추가 확인 정보 - 라운드 {self.rounds}]\n{block}"
        before = {m.trial_id: m.eligibility for m in targets}
        self.profile_patient()
        after = self.match([m.trial_id for m in targets])
        changes = {m.trial_id: f"{before[m.trial_id]} -> {m.eligibility}" for m in after}
        return {"status": "done", "round": self.rounds, "qa": [p.model_dump() for p in new_pairs], "changes": changes}

    # ----------------------------------------------------------- 단계 ⑥
    def finalize(self) -> None:
        matches = list(self.matches.values())
        evaluated = {t: self.trials[t] for t in self.matches}
        self.tracer.log("recommender", "start", n_trials=len(matches))
        self.recommendation = recommender.recommend(self.profile, matches, evaluated, self.qa_log)
        self.tracer.log("recommender", "done",
                        message=" > ".join(r.trial_id for r in self.recommendation.ranked) or "(추천 없음)")
        self._run_fact_comparison()
        self.tracer.log("explainer", "start")
        if not config.WRITE_REPORT:  # 평가 실험용: 보고서(설명 에이전트) 생략
            return
        ranked_ids = [r.trial_id for r in self.recommendation.ranked]
        extra = (screener.report_section(self.screening) if self.screening else "")
        extra += acquisition.report_section(self.missing_facts, set(ranked_ids))
        self.report = explainer.explain(
            self.profile, matches, self.recommendation, evaluated, self.qa_log, self.match_history,
            extra_section=extra,
        )
        self.tracer.log("explainer", "done")

    # ----------------------------------------------------------- 고정 순서 실행
    def run_fixed(self) -> None:
        self.profile_patient()
        if self.screen_trials:
            self.screen()
        self.match()
        while self.needs_clarification():
            result = self.clarify()
            if result["status"] != "done":
                break
        self.finalize()

    # ----------------------------------------------------------- 저장
    def result(self) -> dict:
        return {
            "patient_id": self.case.patient_id,
            "input_source": self.case.input_source,
            "model": config.MODEL,
            "disclaimer": DISCLAIMER,
            "profile": self.profile.model_dump() if self.profile else None,
            "matches": {tid: m.model_dump() for tid, m in self.matches.items()},
            "qa_log": [qa.model_dump() for qa in self.qa_log],
            "eligibility_history": self.match_history,
            "screening": self.screening.model_dump() if self.screening else None,
            "missing_facts": {k: f.model_dump() for k, f in self.missing_facts.items()},
            "recommendation": self.recommendation.model_dump() if self.recommendation else None,
            "trial_sources": {tid: self.trials[tid].get("source") for tid in self.matches},
            "errors": self.errors,
            # 사실 단위 가상 환자가 질문별로 공개한 사실 ID (정보 가리기 평가용)
            "revealed_facts": self.answerer.revealed(self.case.patient_id) if hasattr(self.answerer, "revealed") else [],
            "usage": self.usage,
            # 사실 저장소 병행 기록 상태 (판정·추천과 별개, 건수·상태만)
            "fact_shadow": self.fact_shadow_log,
            # 사실 기반 비교 (진단용, 켰을 때만. ID·상태·이유 코드만)
            **({"fact_comparison": self.fact_comparison} if self.fact_compare != "off" else {}),
            # 의존 그래프·부분 재평가 진단 (켰을 때만. 항목 이름·ID·건수·이유 코드만)
            **({"dependency_diagnostic": self.dependency_diagnostics} if self.dependency_diagnostic else {}),
            # Decision Loop 진단 (켰을 때만. 행동·이유 코드·ID·건수만, 판정·추천이 아님)
            **({"decision_diagnostic": self.decision_diagnostics} if self.decision_loop != "off" else {}),
        }

    def save(self, out_dir: Path) -> None:
        self.usage = llm.usage_snapshot()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "result.json").write_text(
            json.dumps(self.result(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if self.report:
            (out_dir / "report.md").write_text(self.report, encoding="utf-8")

    # ----------------------------------------------------------- 내부
    def _snapshot(self, label: str) -> None:
        self.match_history.append({
            "step": label,
            "eligibility": {tid: m.eligibility for tid, m in self.matches.items()},
        })

    def _parallel(self, fn, items):
        """반환: (성공 결과 목록, 실패 {item: 오류}). 한 항목의 실패가 전체를 멈추지 않음."""
        results, failed = [], {}
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
            # copy_context: 환자별 토큰 사용량 집계(contextvars)를 하위 스레드에 전달
            futures = {pool.submit(contextvars.copy_context().run, fn, it): it for it in items}
            for fut, it in futures.items():
                try:
                    results.append(fut.result())
                except Exception as e:  # 한 시험의 실패가 전체를 멈추지 않도록 기록 후 계속
                    self.errors.append({"item": it, "error": repr(e)})
                    self.tracer.log("pipeline", "error", item=it, message=repr(e))
                    failed[it] = repr(e)
        return results, failed
