"""Session·운영 상태(OpsState) JSON 저장·복원 (TRIALOG O).

서버를 다시 띄워도 사례의 판정 상태·문답·사실 이력이 그대로 이어지도록, 재평가에 필요한 Session 필드만 저장합니다.
복원은 LLM을 부르지 않습니다 (프로필·판정은 저장값, 기준표는 시험 카탈로그·고정 저장소에서 다시 읽음).
pipeline.py(보호 파일)는 바꾸지 않고 공개 필드만 읽고 씁니다.
"""

from __future__ import annotations

from types import MappingProxyType

from .. import acquisition
from ..contract_session import ContractSession
from ..engine.cohort_state import OpsState
from ..fact_store import FactStore, PatientFact
from ..pipeline import PatientCase, Session
from ..schemas import PatientProfile, QAPair, Recommendation, TrialMatch
from ..trace import Tracer

SESSION_FORMAT = "ctrec.followup_session/v1"
_CONTRACT_FIELDS = ("contract_provenance", "contract_asked")  # ContractSession에만 있는 기록 필드


class NoAnswerer:
    """복원한 Session은 clarify(동기 답변 대기)를 쓰지 않음: 답은 요청·응답 API로만 들어옴."""

    def answer(self, questions, patient_id):  # pragma: no cover - 호출되면 설계 위반
        raise RuntimeError("followup session does not collect answers synchronously")


def dump_session(s) -> dict:
    pid = s.case.patient_id
    out = {
        "format": SESSION_FORMAT,
        "session_class": type(s).__name__,
        "case": {"patient_id": pid, "raw_text": s.case.raw_text, "input_source": s.case.input_source,
                 "candidate_trials": s.case.candidate_trials},
        "initial_text": getattr(s, "initial_text", None),
        "trial_ids": list(s.trials),
        "reference_date": s.reference_date,
        "session_id": s.session_id,
        "rounds": s.rounds,
        "profile": s.profile.model_dump(mode="json") if s.profile else None,
        "matches": {t: m.model_dump(mode="json") for t, m in s.matches.items()},
        # session_id·contract_id는 QAPair.model_dump에서 빠지는 필드(exclude=True)라 따로 저장: 빠지면 복원 뒤 답변
        # 검증(answer_verification)이 이 세션의 문답으로 보지 않아 판정이 달라짐
        "qa_log": [{**p.model_dump(mode="json"), "session_id": p.session_id, "contract_id": p.contract_id}
                   for p in s.qa_log],
        "match_history": s.match_history,
        "missing_facts": {k: f.model_dump(mode="json") for k, f in s.missing_facts.items()},
        "errors": [{"item": str(e.get("item")), "error": str(e.get("error"))[:500]} for e in s.errors],
        "fact_shadow_log": s.fact_shadow_log,
        "facts": [f.model_dump(mode="json") for f in s.fact_store.facts(pid)],
        "recommendation": s.recommendation.model_dump(mode="json") if s.recommendation else None,
        "report": s.report,
    }
    for name in _CONTRACT_FIELDS:
        if hasattr(s, name):
            out[name] = getattr(s, name)
    return out


def _fact_store(pid: str, facts: list[dict]) -> FactStore:
    store = FactStore()
    loaded = [PatientFact.model_validate(f) for f in facts]
    if loaded:
        store._facts[pid] = loaded  # FactStore.load와 같은 복원 (파일 대신 사례 기록 안에 저장)
        for name in {f.field for f in loaded}:
            store._recompute(pid, name)  # 충돌 표시는 저장값이 아니라 사실들로부터 다시 계산
    return store


def _reload_parsed(s, data: dict) -> None:
    """카탈로그가 기준표를 주지 않은 시험(운영 카탈로그 = trial_store)은 고정 저장소에서 다시 읽음.

    처음 Session은 parse_criteria에서 trial_store로 기준표를 채우지만, 복원한 Session은 비어 있음. 비어 있으면
    decision_state가 그 시험의 기준을 보지 않아 계획(제안)이 비고, 담당자가 고른 제안이 모두 proposal_not_current가 됨.
    trial_store.load는 사전 컴파일본만 읽음 (LLM 없음). 처음에 평가되지 않은 시험(NOT_EVALUATED)은 처음과 같이 비워 둠."""
    from .. import trial_store
    for tid, m in data["matches"].items():
        if tid in s.parsed or tid not in s.trials or m.get("eligibility") == "NOT_EVALUATED":
            continue
        try:
            s.parsed[tid] = trial_store.load(s.trials[tid])
        except trial_store.CompilationPending:
            pass


def restore_session(data: dict, trials: dict[str, dict], parsed: dict | None = None, tracer: Tracer | None = None):
    if data.get("format") != SESSION_FORMAT:
        raise ValueError(f"unsupported session format: {data.get('format')!r}")
    c = data["case"]
    pid = c["patient_id"]
    missing = [t for t in data["trial_ids"] if t not in trials]
    if missing:
        raise ValueError(f"trial catalog lacks {len(missing)} stored trial(s)")
    case = PatientCase(patient_id=pid, raw_text=data.get("initial_text") or c["raw_text"],
                       input_source=c["input_source"], candidate_trials=c.get("candidate_trials"))
    # 만든 때의 Session 종류로 복원 (환경 변수 CTREC_SESSION이 나중에 바뀌어도 같은 판정 경로)
    cls = {"ContractSession": ContractSession, "Session": Session}[data["session_class"]]
    s = cls(case=case, trials={t: trials[t] for t in data["trial_ids"]}, answerer=NoAnswerer(),
            tracer=tracer or Tracer(None, verbose=False), reference_date=data["reference_date"],
            session_id=data["session_id"], fact_store=_fact_store(pid, data["facts"]))
    s.case.raw_text = c["raw_text"]  # ContractSession은 생성 시 원문을 처음 입력으로 잡으므로 처음 입력으로 만든 뒤 바꿈
    if parsed:
        s.parsed.update({t: p for t, p in parsed.items() if t in s.trials})
    _reload_parsed(s, data)
    s.rounds = data["rounds"]
    s.profile = PatientProfile.model_validate(data["profile"]) if data["profile"] else None
    s.matches = {t: TrialMatch.model_validate(m) for t, m in data["matches"].items()}
    s.qa_log = [QAPair.model_validate(p) for p in data["qa_log"]]
    s.match_history = list(data["match_history"])
    s.missing_facts = {k: acquisition.MissingFact.model_validate(f) for k, f in data["missing_facts"].items()}
    s.errors = list(data.get("errors", []))
    s.fact_shadow_log = list(data.get("fact_shadow_log", []))
    s.recommendation = Recommendation.model_validate(data["recommendation"]) if data.get("recommendation") else None
    s.report = data.get("report")
    for name in _CONTRACT_FIELDS:
        if name in data and hasattr(s, name):
            setattr(s, name, data[name])
    return s


def dump_ops(ops: OpsState) -> dict:
    return {"failed_sources": sorted(ops.failed_sources), "attempts": dict(sorted(ops.attempts.items())),
            "exhausted_trials": sorted(ops.exhausted_trials),
            "failed_items": sorted([list(x) for x in ops.failed_items]),
            "exhausted_items": sorted(ops.exhausted_items)}


def load_ops(d: dict | None) -> OpsState:
    if not d:
        return OpsState()
    return OpsState(failed_sources=frozenset(d.get("failed_sources", [])),
                    attempts=MappingProxyType(dict(d.get("attempts", {}))),
                    exhausted_trials=frozenset(d.get("exhausted_trials", [])),
                    failed_items=frozenset(tuple(x) for x in d.get("failed_items", [])),
                    exhausted_items=frozenset(d.get("exhausted_items", [])))
