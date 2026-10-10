"""파이프라인 상태(Session)와 6단계 실행 로직.

두 가지 실행 모드가 같은 Session을 공유합니다.
- fixed : 과제의 6단계를 정해진 순서로 실행 (재현성·평가용)
- agent : 오케스트레이터 에이전트가 스스로 계획을 세우고 Session의 단계를 도구로 호출 (orchestrator.py)
"""

from __future__ import annotations

import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import DISCLAIMER, config, llm
from .agents import criteria_parser, explainer, matcher, patient_profiler, question_generator, recommender
from .agents.answerers import Answerer
from .schemas import ParsedTrial, PatientProfile, QAPair, Recommendation, TrialMatch
from .trace import Tracer


@dataclass
class PatientCase:
    patient_id: str
    raw_text: str
    hidden_details: str | None = None  # 가상 환자 시뮬레이션용 (파이프라인에는 노출되지 않음)
    candidate_trials: list[str] | None = None  # 이 환자에게 평가할 후보 시험 ID (없으면 전체)
    oracle_facts: list[dict] | None = None  # 정보 가리기 평가셋: 전체 사실 목록 (가상 환자만 사용, 파이프라인에는 노출되지 않음)


# 파이프라인에 노출하지 않는 필드 (시뮬레이터·평가용 메타데이터)
_HIDDEN_KEYS = {
    "hidden_details", "labels", "design_note", "synthetic", "base_topic", "candidate_trials",
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
    return PatientCase(patient_id=pid, raw_text=text, hidden_details=hidden,
                       candidate_trials=data.get("candidate_trials"), oracle_facts=data.get("oracle_facts"))


def load_patients(path: Path) -> list[PatientCase]:
    """환자 파일 로드. 한 파일에 여러 환자가 있을 수 있음.

    지원 형식:
    - .txt: 자유 서술 1명
    - {"patient_id", "text", ...}: 1명 (권장)
    - {"topics": [{"num", "title"}, ...]}: 사업단 제공 예시(TREC topic) 형식, 여러 명
    - [{...}, {...}]: 여러 명
    그 외 JSON 필드는 텍스트로 직렬화해 환자 정보 이해 에이전트에 전달합니다.
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


@dataclass
class Session:
    case: PatientCase
    trials: dict[str, dict]
    answerer: Answerer
    tracer: Tracer
    max_rounds: int = config.MAX_CLARIFY_ROUNDS
    allow_search: bool = False
    question_budget: int | None = None  # 전체 라운드 합산 최대 질문 수 (None = 라운드당 한도만 적용)

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

    # ----------------------------------------------------------- 단계 ①+③a
    def parse_criteria(self, trial_ids: list[str] | None = None) -> None:
        ids = [t for t in (trial_ids or list(self.trials)) if t not in self.parsed]

        def work(tid: str):
            self.tracer.log("criteria_parser", "start", trial_id=tid)
            p = criteria_parser.parse_trial(self.trials[tid])
            self.tracer.log("criteria_parser", "done", trial_id=tid, n_rules=len(p.rules),
                            integrity_issues=p.integrity_issues)
            return tid, p

        for tid, p in self._parallel(work, ids):
            self.parsed[tid] = p

    # ----------------------------------------------------------- 단계 ②+③b
    def profile_patient(self) -> PatientProfile:
        self.tracer.log("patient_profiler", "start", patient_id=self.case.patient_id)
        self.profile = patient_profiler.profile_patient(self.case.patient_id, self.case.raw_text)
        self.tracer.log("patient_profiler", "done", missing=self.profile.missing_or_ambiguous)
        return self.profile

    # ----------------------------------------------------------- 단계 ③c, ④
    def match(self, trial_ids: list[str] | None = None) -> list[TrialMatch]:
        assert self.profile is not None, "profile_patient()를 먼저 실행하세요"
        ids = trial_ids or list(self.trials)
        self.parse_criteria(ids)

        def work(tid: str):
            self.tracer.log("matcher", "start", trial_id=tid)
            m = matcher.match_trial(self.profile, self.parsed[tid], self.trials[tid])
            self.tracer.log("matcher", "done", trial_id=tid, message=m.summary)
            return tid, m

        out = []
        for tid, m in self._parallel(work, [t for t in ids if t in self.parsed]):
            self.matches[tid] = m
            out.append(m)
        self._snapshot(f"match(round={self.rounds})")
        return out

    def uncertain(self) -> list[TrialMatch]:
        """확인 질문 대상: 판정 보류이면서 현재 참여 가능한(모집 중) 시험만."""
        return [
            m for m in self.matches.values()
            if m.eligibility == "UNCERTAIN" and recommender.is_open(self.trials[m.trial_id])
        ]

    def open_candidates(self) -> list[TrialMatch]:
        """추천 가능성이 있는 시험: 모집 중 + ELIGIBLE/UNCERTAIN."""
        return [
            m for m in self.matches.values()
            if m.eligibility != "INELIGIBLE" and recommender.is_open(self.trials[m.trial_id])
        ]

    def pending_physician_review(self) -> bool:
        """원문에 구체 기준이 없는 항목이 있는데 아직 참고용 질문을 하지 않았는가."""
        has_items = any(matcher.needs_physician_review(a) for m in self.open_candidates() for a in m.assessments)
        asked = any(qa.purpose == "physician_reference" for qa in self.qa_log)
        return has_items and not asked

    def remaining_questions(self) -> int:
        """이번 라운드에 할 수 있는 질문 수 (라운드당 한도와 남은 전체 예산 중 작은 값)."""
        per_round = config.MAX_QUESTIONS_PER_ROUND
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
            self.profile, targets, self.qa_log, self.remaining_questions()
        )
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
        self.tracer.log("recommender", "start", n_trials=len(matches))
        self.recommendation = recommender.recommend(self.profile, matches, self.trials, self.qa_log)
        self.tracer.log("recommender", "done",
                        message=" > ".join(r.trial_id for r in self.recommendation.ranked) or "(추천 없음)")
        self.tracer.log("explainer", "start")
        self.report = explainer.explain(
            self.profile, matches, self.recommendation, self.trials, self.qa_log, self.match_history
        )
        self.tracer.log("explainer", "done")

    # ----------------------------------------------------------- 고정 순서 실행
    def run_fixed(self) -> None:
        self.parse_criteria()
        self.profile_patient()
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
            "model": config.MODEL,
            "disclaimer": DISCLAIMER,
            "profile": self.profile.model_dump() if self.profile else None,
            "matches": {tid: m.model_dump() for tid, m in self.matches.items()},
            "qa_log": [qa.model_dump() for qa in self.qa_log],
            "eligibility_history": self.match_history,
            "recommendation": self.recommendation.model_dump() if self.recommendation else None,
            "trial_sources": {tid: t.get("source") for tid, t in self.trials.items()},
            "errors": self.errors,
            # 사실 단위 가상 환자가 질문별로 공개한 사실 ID (정보 가리기 평가용)
            "revealed_facts": self.answerer.revealed(self.case.patient_id) if hasattr(self.answerer, "revealed") else [],
            "usage": self.usage,
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
        results = []
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
            # copy_context: 환자별 토큰 사용량 집계(contextvars)를 하위 스레드에 전달
            futures = {pool.submit(contextvars.copy_context().run, fn, it): it for it in items}
            for fut, it in futures.items():
                try:
                    results.append(fut.result())
                except Exception as e:  # 한 시험의 실패가 전체를 멈추지 않도록 기록 후 계속
                    self.errors.append({"item": it, "error": repr(e)})
                    self.tracer.log("pipeline", "error", item=it, message=repr(e))
        return results
