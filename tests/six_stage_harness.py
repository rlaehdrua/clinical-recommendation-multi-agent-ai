"""N 감사: 최초 6단계 파이프라인 E2E 하네스 (pytest가 수집하지 않는 도우미 모듈).

실제 Orchestrator 루프·Session/ContractSession·trial_store·rule_engine·Matcher 검증·acquisition·recommender·explainer·
FactStore·Inspector를 그대로 쓰고, LLM 호출만 시나리오 대역(ScriptedLLM)으로 바꿉니다. API 키·네트워크 없음.

대역은 사람이 정한 시나리오 값만 돌려줍니다.
- 환자 이해(PatientProfile): 원문(답변 포함)에 들어 있는 표지(marker)로 프로필 조각을 고름
- 매칭(MatcherOutput): (시험, 기준)마다 정한 판정과 원문 인용. 인용이 원문에 없으면 Matcher 검증이 되돌림
- 질문(QuestionSet): 시나리오가 정한 질문 (대상 '<trial>:<rule>')
- 오케스트레이터(llm.create): 도구 호출 순서 고정 (프로필 -> 평가 -> 질문 xN -> 최종)
판정·질문 선택·추천 규칙은 모두 실제 코드가 정합니다. 실제 Claude 모델의 판단 품질은 검증하지 않습니다.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from ctrec import config, llm, trial_store
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import (ClarifyingQuestion, Fact, LabValue, LLMAssessment, MatcherOutput, PatientProfile,
                           QuestionSet, RecommendationDraft)
from ctrec.tools import ctgov
from ctrec.trace import Tracer

ROOT = Path(__file__).resolve().parents[1]
NSCLC_TRIALS = ROOT / "data" / "nsclc" / "trials"
NSCLC_PARSED = ROOT / "data" / "nsclc" / "parsed"
REF_DATE = "2026-11-07"


def nsclc_trial(tid: str) -> dict:
    return ctgov.load_local(NSCLC_TRIALS / f"{tid}.json")


def demo_trial(tid: str = "DEMO-LUNG-001") -> dict:
    return ctgov.load_local(ROOT / "data" / "trials" / f"{tid}.json")


def base_profile(**kw) -> PatientProfile:
    d = dict(patient_id="ignored", age=None, sex="unknown", primary_diagnosis=None, disease_stage=None,
             diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[], ecog=None,
             other_facts=[], missing_or_ambiguous=[])
    d.update(kw)
    return PatientProfile(**d)


def fact(desc: str, quote: str, date: str | None = None) -> Fact:
    return Fact(description=desc, date=date, source_quote=quote)


def lab(name: str, value: float, unit: str, date: str, quote: str) -> LabValue:
    return LabValue(name=name, value=value, unit=unit, date=date, source_quote=quote)


# 판정 대본: (holds, 인용, evidence_type, applicable). 인용은 환자 원문(답변 포함)에 있어야 함
Verdict = tuple


@dataclass
class Scenario:
    profile: Callable[[str], PatientProfile]  # 프로필 추출 입력(원문+판정용 답변) -> 프로필
    verdicts: Callable[[str, str, str], Verdict | None]  # (trial_id, rule_id, 원문) -> 판정 대본, None이면 unknown
    questions: Callable[[int, list], list[ClarifyingQuestion]] = lambda rnd, qa: []
    script: tuple[str, ...] = ("profile_patient", "evaluate_trials", "ask_clarifying_questions",
                               "ask_clarifying_questions", "finalize_recommendation")


class ScriptedLLM:
    def __init__(self, scenario: Scenario):
        self.sc = scenario
        self.calls: list[str] = []
        self.matcher_requests: list[tuple[str, list[str]]] = []
        self.question_requests = 0
        self._last_source = ""

    def create(self, *, system, messages, tools=None, **k):
        llm.guard()
        self.calls.append("orchestrator")
        step = (len(messages) - 1) // 2
        if step < len(self.sc.script):
            name = self.sc.script[step]
            args = {"trial_ids": []} if name == "evaluate_trials" else {}
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=args,
                                                            id=f"tu_{uuid.uuid4().hex[:8]}")], stop_reason="tool_use")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="완료")], stop_reason="end_turn")

    def structured(self, model_cls, *, system, user, **k):
        llm.guard()
        name = model_cls.__name__
        self.calls.append(name)
        if model_cls is PatientProfile:
            self._last_source = user
            return self.sc.profile(user)
        if model_cls is MatcherOutput:
            tid = re.search(r'<trial id="([^"]+)"', user).group(1)
            rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
            self.matcher_requests.append((tid, [r["rule_id"] for r in rules]))
            out = []
            for r in rules:
                v = self.sc.verdicts(tid, r["rule_id"], self._last_source)
                if v is None:
                    out.append(LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="unknown",
                                             evidence_type="absent", confidence="low", evidence="근거 없음",
                                             reasoning="scenario: 정보 없음", missing_info=r.get("text", "확인 필요")))
                else:
                    holds, quote, etype, *rest = v
                    out.append(LLMAssessment(rule_id=r["rule_id"], applicable=rest[0] if rest else True, holds=holds,
                                             evidence_type=etype, confidence="high", evidence=quote,
                                             reasoning="scenario", missing_info=None))
            return MatcherOutput(assessments=out)
        if model_cls is QuestionSet:
            self.question_requests += 1
            prev = re.findall(r"- Q: (.*?) / A: (.*)", user)
            return QuestionSet(questions=self.sc.questions(self.question_requests, prev))
        if name == "ScreeningOutput":
            ids = re.findall(r'<trial id="([^"]+)"', system)
            return model_cls(trials=[{"trial_id": t, "decision": "keep", "relevance": "high", "reason": "합성"}
                                     for t in ids])
        if model_cls is RecommendationDraft:
            pid = user.split("patient_id=")[1].split(" ")[0]
            return RecommendationDraft(patient_id=pid, ranked=[], excluded=[], overall_comment="합성 요약")
        raise AssertionError(f"예상하지 않은 LLM 출력 형식: {name}")

    def plain(self, *, system, user, **k):
        self.calls.append("plain")
        return "# 합성 보고서\n"


class ScriptedAnswerer:
    """라운드별 답변 대본. 값이 None인 질문은 무응답(웹 시간 초과와 같은 '모름')."""

    def __init__(self, by_round: dict[int, dict[str, str | None]] | Callable[[int, list], dict]):
        self.by_round = by_round
        self.asked: list[list[ClarifyingQuestion]] = []

    def answer(self, questions, patient_id):
        self.asked.append(list(questions))
        rnd = len(self.asked)
        plan = self.by_round(rnd, questions) if callable(self.by_round) else self.by_round.get(rnd, {})
        return {q.question_id: (plan.get(q.question_id) or "모름") for q in questions}


def q(qid: str, text: str, targets: list[str], answer_type: str = "yes_no", purpose: str = "eligibility",
      options: list[str] | None = None) -> ClarifyingQuestion:
    return ClarifyingQuestion(question_id=qid, question=text, answer_type=answer_type, options=options or [],
                              target=targets, purpose=purpose, why="scenario")


def install(monkeypatch, scenario: Scenario, *, nsclc: bool = True) -> ScriptedLLM:
    fake = ScriptedLLM(scenario)
    monkeypatch.setattr(llm, "create", fake.create)
    monkeypatch.setattr(llm, "structured", fake.structured)
    monkeypatch.setattr(llm, "plain", fake.plain)
    monkeypatch.setattr(llm, "client", lambda: (_ for _ in ()).throw(AssertionError("실제 API 클라이언트 사용 금지")))
    monkeypatch.setattr(config, "REFERENCE_DATE", REF_DATE)
    if nsclc:  # CTREC_TRIAL_SET=nsclc 와 같은 경로 (config.TRIAL_SETS)
        monkeypatch.setattr(config, "TRIALS_DIR", config.TRIAL_SETS["nsclc"][0])
        monkeypatch.setattr(config, "PARSED_DIR", config.TRIAL_SETS["nsclc"][1])
    return fake


def run_session(text: str, trials: dict[str, dict], answerer, *, session_cls=Session, input_source="unknown",
                patient_id: str = "SYN-N-1", max_rounds: int = 2, **kw) -> Session:
    from ctrec.orchestrator import Orchestrator
    case = PatientCase(patient_id=patient_id, raw_text=text, input_source=input_source)
    s = session_cls(case=case, trials=trials, answerer=answerer, tracer=Tracer(None, verbose=False),
                    max_rounds=max_rounds, **kw)
    Orchestrator(s).run()
    return s


def assessment(s: Session, tid: str, rid: str):
    return next(a for a in s.matches[tid].assessments if a.rule_id == rid)


def summary(s: Session) -> dict:
    """실행 로그용 요약 (합성 값만)."""
    rec = s.recommendation
    return {
        "eligibility": {t: m.eligibility for t, m in s.matches.items()},
        "match_status": {t: m.match_status for t, m in s.matches.items()},
        "history": s.match_history,
        "qa": [(p.round, p.question_id, p.target, p.answer) for p in s.qa_log],
        "recommended": [r.trial_id for r in rec.recommended] if rec else None,
        "ranked": [(r.trial_id, r.match_status) for r in rec.ranked] if rec else None,
        "missing_facts": {k: (f.status, len(f.attempted_actions)) for k, f in s.missing_facts.items()},
        "facts": len(s.fact_store.facts(s.case.patient_id)),
    }


def nsclc_parsed(tid: str):
    return trial_store.load(nsclc_trial(tid), store_dir=NSCLC_PARSED)


def use_subset(monkeypatch, parsed, keep: set[str]):
    """이 시험의 기준을 keep에 든 것만 남겨 실제 trial_store.get 자리에 넣음 (흐름 하나를 기준 몇 개로 분리해 보기 위함)."""
    from ctrec import pipeline
    p = parsed.model_copy(update={"rules": [r for r in parsed.rules if r.rule_id in keep]})
    p.rule_meta = {k: v for k, v in parsed.rule_meta.items() if k in keep}
    real = trial_store.get
    monkeypatch.setattr(pipeline.trial_store, "get",
                        lambda trial: p.model_copy(deep=True) if trial["trial_id"] == parsed.trial_id else real(trial))
    return p


def attest_all(session) -> None:
    """합성 문서 증명(sim.evaluator.SimDocumentAuthority)으로 이 환자의 모든 사실을 문서 확인.

    운영 권한이 아님: 서버 문서 계약이 생겼을 때의 하류(판정·추천) 연결만 보기 위한 가정 (sim.DOCUMENT_ASSUMPTION과 같음)."""
    from ctrec.sim.evaluator import SIM_AUTHORITY
    for f in list(session.fact_store.facts(session.case.patient_id)):
        session.fact_store.attest(f.fact_id, SIM_AUTHORITY.issue(f, "synthetic-doc"), SIM_AUTHORITY,
                                  reason="합성 문서 확인", by="six_stage_harness")
