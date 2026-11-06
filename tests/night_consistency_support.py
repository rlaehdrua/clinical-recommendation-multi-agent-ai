"""상태·추천 일관성 테스트용 합성 도우미 (audit/engine-contract-2026-11-06의 audit_helpers.py 사본). LLM·네트워크 없음."""

from __future__ import annotations

from dataclasses import dataclass, field

from ctrec import config, llm, question_eval as qe, status as status_mod
from ctrec.agents import question_generator
from ctrec.decision_state import SessionView, build_state
from ctrec.engine import contracts as C
from ctrec.engine.cohort_state import CohortState, OpsState
from ctrec.engine.policy import LegacyAdapterPolicy
from ctrec.eval_hygiene import AliasMap
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import LLMAssessment, MatcherOutput, ParsedTrial, RecommendationDraft, Rule, RuleMeta

ALIAS = AliasMap(salt="engine-audit").alias("SYN-AUDIT")
RAW = "Synthetic 62-year-old male. ECOG 1."


def rule(rid, text="synthetic criterion", kind="inclusion", category="other", **kw):
    base = dict(rule_id=rid, kind=kind, text=text, category=category, field=None, operator=None, value=None, unit=None,
                time_window_days=None, structured_evaluable=False, underspecified=False, cohort=None)
    return Rule(**{**base, **kw})


AGE = rule("AGE", "Age >= 19 years", category="demographic", field="age", operator=">=", value="19", unit="years",
           structured_evaluable=True)
ECOG = rule("ECOG", "ECOG 0-1", category="performance_status", field="ecog", operator="<=", value="1",
            structured_evaluable=True)
CONSENT = rule("CONSENT", "Able to give informed consent", category="consent_or_logistics")


def lab(rid, name, value="1.5", unit="g/dL", **kw):
    return rule(rid, f"{name} >= {value} {unit}", category="lab", field=f"lab:{name}", operator=">=", value=value,
                unit=unit, structured_evaluable=True, **kw)


def trial(tid, status="RECRUITING"):
    return {"trial_id": tid, "title": "synthetic", "conditions": ["SYN"], "phases": ["PHASE2"], "interventions": [],
            "overall_status": status, "eligibility_criteria": "Inclusion Criteria:\n- synthetic\n", "sex": "ALL",
            "minimum_age": None, "maximum_age": None, "healthy_volunteers": False, "brief_summary": "",
            "detailed_description": "", "synthetic": True}


def parsed(tid, rules, cohorts=(), meta=None):
    p = ParsedTrial(trial_id=tid, title="synthetic", target_population="", intervention_summary="", key_conditions=[],
                    cohorts=list(cohorts), rules=list(rules), parsing_notes="")
    p.rule_meta = {k: (v if isinstance(v, RuleMeta) else RuleMeta(**v)) for k, v in (meta or {}).items()}
    return p


@dataclass
class LLMStub:
    """Matcher LLM 대역. verdicts: rule_id -> (holds, applicable, evidence_quote) 또는 호출 시점 함수(raw_text 인자)."""
    verdicts: dict = field(default_factory=dict)
    text: dict = field(default_factory=dict)

    def structured(self, model_cls, *, system=None, user=None, **kw):
        if model_cls is MatcherOutput:
            import json
            rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
            return MatcherOutput(assessments=[self._one(r["rule_id"]) for r in rules])
        if model_cls is RecommendationDraft:
            pid = user.split("patient_id=")[1].split(" ")[0]
            return RecommendationDraft(patient_id=pid, ranked=[], excluded=[], overall_comment="stub")
        raise AssertionError(f"unexpected LLM request {model_cls}")

    def _one(self, rid):
        v = self.verdicts.get(rid)
        if callable(v):
            v = v(self.text.get("raw", ""))
        if v is None:
            return LLMAssessment(rule_id=rid, applicable=True, holds="unknown", evidence_type="absent", confidence="low",
                                 evidence="근거 없음", reasoning="stub", missing_info="stub")
        holds, applicable, quote = v
        return LLMAssessment(rule_id=rid, applicable=applicable, holds=holds, evidence_type="explicit",
                             confidence="high", evidence=quote, reasoning="stub", missing_info="")


class Tracer:
    def __init__(self):
        self.records = []

    def log(self, agent, event, **data):
        self.records.append((agent, event, data))


class Answerer:
    def __init__(self, fn=lambda q: "모름"):
        self.fn = fn
        self.asked = []

    def answer(self, questions, patient_id):
        self.asked.append(list(questions))
        return {q.question_id: self.fn(q) for q in questions}


class Env:
    """OfflineEnvironment + 임시 저장소 + 지정 Matcher 대역."""

    def __init__(self, tmp_path, verdicts=None, n=3):
        self.tmp, self.stub, self.n = tmp_path, LLMStub(dict(verdicts or {})), n
        self._saved = []

    def __enter__(self):
        for name, value in (("PARSED_DIR", self.tmp / "parsed"), ("CACHE_DIR", self.tmp / "cache"),
                            ("DEMO_MODE", True)):
            self._saved.append((name, getattr(config, name)))
            setattr(config, name, value)
        self.oe = qe.OfflineEnvironment(self.n)
        self.oe.__enter__()
        llm.structured = self.stub.structured
        return self

    def __exit__(self, *exc):
        self.oe.__exit__(*exc)
        for name, value in self._saved:
            setattr(config, name, value)
        return False


def session(trials: dict[str, list] | dict, *, raw=RAW, answerer=None, input_source="clinical_record", rounds=3,
            cohorts=None, meta=None, status=None, **kw) -> Session:
    """trials: 시험 ID -> 규칙 목록. Session을 저장소 없이 기준표를 미리 넣어 만듦."""
    cohorts, meta, status = cohorts or {}, meta or {}, status or {}
    ts = {t: trial(t, status.get(t, "RECRUITING")) for t in trials}
    s = Session(case=PatientCase(ALIAS, raw, input_source=input_source, candidate_trials=list(ts)), trials=ts,
                answerer=answerer or Answerer(), tracer=Tracer(), max_rounds=rounds, **kw)
    s.parsed = {t: parsed(t, r, cohorts.get(t, ()), meta.get(t)) for t, r in trials.items()}
    return s


def run_once(s: Session) -> Session:
    s.profile_patient()
    s.match()
    s.finalize()
    return s


def cohort_of(s: Session, ops=None) -> CohortState:
    view = SessionView.from_session(s)
    view.matched_versions = {t: view.snapshot.version for t in s.matches}
    return CohortState.from_decision_states({ALIAS: build_state(view)}, ops=ops)


def layers(s: Session, co: CohortState | None = None, policy=None, alias=None) -> dict:
    """요구된 비교 항목을 시험별로 모음."""
    co = co or cohort_of(s)
    alias = alias or ALIAS
    policy = policy or LegacyAdapterPolicy()
    acts = policy.propose(co)
    rec = s.recommendation
    ranked = [r.trial_id for r in rec.ranked] if rec else []
    out = {}
    for tid, m in s.matches.items():
        p = co.pair(alias, tid)
        out[tid] = {"eligibility": m.eligibility, "external": status_mod.external_status(m),
                    "status": p.status.value, "pending": sorted(p.pending_kinds), "ranked": tid in ranked,
                    "recommended": bool(rec) and tid in [r.trial_id for r in rec.recommended], "open": p.open,
                    "cohort": m.matched_cohort,
                    "actions": sorted((a.action_type, a.reason_code, a.source_id) for a in acts
                                      if tid in a.target_trial_ids)}
    return out


def executable(actions) -> list:
    return [a for a in actions if a.action_type not in C.NON_EXECUTING]


__all__ = [k for k in globals() if not k.startswith("_")]
