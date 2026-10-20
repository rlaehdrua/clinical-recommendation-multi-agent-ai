"""Decision Loop 테스트용 합성 데이터 (pytest가 수집하지 않는 도우미). 기준선 판정은 실제 Matcher + 오프라인 LLM 대역."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import fact_changes
from ctrec.agents import matcher
from ctrec.decision_loop import LoopConfig, LoopContext, run_loop
from ctrec.decision_state import SessionView
from ctrec.fact_store import FactStore
from ctrec.fact_view import FactEvidenceView
from ctrec.schemas import LLMAssessment, MatcherOutput, QAPair, RuleMeta

from test_decisions import TRIAL, _parsed
from test_offline import _rule

P = "SYN-D"
REF = "2026-10-12"
AGE = _rule(rule_id="I1", text="Age >= 18", field="age", operator=">=", value="18", unit="years")
ECOG = _rule(rule_id="I2", text="ECOG 0-1", category="performance_status", field="ecog", operator="<=", value="1")
EGFR = _rule(rule_id="I3", text="EGFR mutation positive", category="biomarker", structured_evaluable=False)
HB = _rule(rule_id="I4", text="Hemoglobin >= 9 g/dL", category="lab", field="lab:hemoglobin", operator=">=",
           value="9", unit="g/dL")
DX = _rule(rule_id="I5", text="Histologically confirmed NSCLC", category="diagnosis", structured_evaluable=False)
VAGUE = _rule(rule_id="E1", kind="exclusion", text="Use of certain medications", category="medication",
              structured_evaluable=False, underspecified=True)
CONSENT = _rule(rule_id="I9", text="Able to provide informed consent", category="consent_or_logistics",
                structured_evaluable=False)


def unknown_llm(model_cls, *, system, user, **k):
    """기준선 LLM 대역: 요청받은 기준을 모두 판정 불가로 (근거 없음)."""
    rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
    return MatcherOutput(assessments=[LLMAssessment(rule_id=r["rule_id"], applicable=True, holds="unknown",
                                                    evidence_type="absent", confidence="low", evidence="근거 없음",
                                                    reasoning="stub", missing_info="확인 필요") for r in rules])


def store(*facts, patient=P) -> FactStore:
    s = FactStore()
    for field, value, kw in facts:
        s.add(patient, field, value, **{"source_type": "clinical_record", **kw})
    return s


def trial(tid, rules, cohorts=(), meta=None, status="RECRUITING"):
    p = _parsed([r.model_copy() for r in rules], cohorts).model_copy(update={"trial_id": tid})
    p.rule_meta = dict(meta or {})
    return p, dict(TRIAL, trial_id=tid, overall_status=status)


def view(trials: dict, s: FactStore, qa=None, missing=None, source_text=None, input_source="clinical_record",
         not_evaluated=(), patient=P, tweak=None):
    """trials: {tid: (ParsedTrial, trial dict)}. 기준선 판정은 matcher.match_trial (llm.structured는 대역이어야 함)."""
    from ctrec.pipeline import _not_evaluated
    profile = FactEvidenceView(s, patient).to_profile()
    parsed, tdict, matches = {}, {}, {}
    for tid, (p, t) in trials.items():
        tdict[tid] = t
        if tid in not_evaluated:
            matches[tid] = _not_evaluated(patient, tid, "합성 평가 실패")
            continue
        parsed[tid] = p
        matches[tid] = matcher.match_trial(profile, p, t, reference_date=REF, source_text=source_text,
                                           input_source=input_source)
    if tweak:
        tweak(matches)
    snap = fact_changes.take_snapshot(s, patient)
    return SessionView(session_id="S-test", patient_id=patient, snapshot=snap, matches=matches, parsed=parsed,
                       trials=tdict, qa_log=list(qa or []), missing_facts=dict(missing or {}), reference_date=REF,
                       matched_versions={t: snap.version for t in matches})


def run(v: SessionView, channel=None, config: LoopConfig | None = None, stop_check=lambda: None, matched=None):
    ctx = LoopContext(view=v, matched_snapshots=matched if matched is not None else
                      {t: v.snapshot for t in v.matches})
    return run_loop(ctx, channel, config, stop_check=stop_check), ctx


def qa(question_id, answer, *targets, round_no=1):
    return QAPair(round=round_no, question_id=question_id, question="합성 질문", answer=answer, purpose="eligibility",
                  target=list(targets))


class SimChannel:
    """답변 공급자 대역: info_key -> [(field, value, kwargs)] 또는 'declined'. 없으면 빈 답(정보 없음)."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.asked: list[str] = []

    def ask(self, action, state):
        self.asked.append(action.info_key)
        a = self.answers.get(action.info_key)
        if a == "declined":
            return {"facts": [], "declined": True}
        return {"facts": list(a or []), "declined": False}


def types(result):
    return [(a.action_type, a.info_key) for a in result.actions]


def record_meta(rid):
    return {rid: RuleMeta(required_evidence="clinical_record")}
