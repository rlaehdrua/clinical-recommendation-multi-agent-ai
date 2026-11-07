"""오프라인 판정 (설계 §3.3): 기존 Matcher·Decision State를 그대로 쓰고, LLM 호출만 오프라인 대역으로 바꿉니다.

- 규칙 엔진이 판정하는 구조화 기준(나이·성별·ECOG·검사 수치)은 기존 경로 그대로 판정됩니다.
- Matcher가 LLM에 넘기는 기준은 OfflineMatcherClient가 답합니다. 실제 API 호출은 0건이며 (llm.use_client로 이 컨텍스트의
  클라이언트를 대역으로 고정), Matcher 외의 LLM 요청은 OfflineLLMRequest로 막습니다.
  판정기(sim.judge.CategoricalJudge)가 없으면 전부 "판정 불가" -> **규칙 엔진 범위만 평가한 시나리오** (#13 §9 위험 1).
  있으면 생성기 슬롯 연결이 있는 기준을 지금 사실로 판정하고(결정적 상한), 연결 없는 기준은 판정 불가.
- 사실은 결정적 ID·기록 시각으로 만듭니다 (같은 실행이면 같은 fact_id -> 같은 행동 지문·결과 해시).
- 원문(source_text): 진료 기록·문서 확인 사실은 처음 받은 기록 부분, 환자 답변 사실은 답변 부분(evidence.ANSWER_MARKER 뒤).
  Matcher의 근거 수준 정책(진료 기록 필요 기준을 답변만으로 확정하지 않음)이 실제 Session과 같은 방식으로 작동합니다.
"""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from .. import document_authority, evidence_basis, llm
from ..agents import matcher
from ..decision_state import DecisionState, SessionView, build_state
from ..evidence import ANSWER_MARKER
from ..fact_changes import FactSnapshot, make_snapshot, snapshot_store
from ..document_authority import ServerAttestation
from ..fact_store import PatientFact, SourceReference, VerificationEvent, document_backed
from ..fact_view import FactEvidenceView
from ..schemas import LLMAssessment, MatcherOutput
from .cohort import SimFact, SimTrial

EVALUATOR_ID = "matcher_rule_scope_offline/0.1"  # 규칙 엔진 범위 + LLM 기준 판정 불가
RECORDED_AT = "2000-01-01T00:00:00.000000+00:00"  # 결정적 기록 시각 (실제 시각을 해시에 넣지 않음)
INITIAL_HEADER = "[시뮬레이터 기록]"
SIM_BY = "simulator"


class OfflineLLMRequest(RuntimeError):
    """오프라인 실행 중 Matcher 외의 LLM 요청 (args[0]: 코드)."""


_TRIAL_TAG = re.compile(r'<trial id="([^"]+)">')


class OfflineMatcherClient:
    """anthropic 클라이언트와 같은 messages.create 인터페이스. Matcher 요청에만 답함 (네트워크 없음).

    judge(sim.judge.CategoricalJudge)가 있으면 연결된 기준은 지금 사실(self.facts)로 판정하고, 나머지는 '판정 불가'."""

    def __init__(self, judge=None, rules: dict[str, dict] | None = None):
        self.requests = 0  # Matcher가 LLM에 넘긴 요청 수 (대역이 답함, 실제 호출 아님)
        self.judge = judge
        self.rules = rules or {}  # 시험 -> {기준 ID: Rule}
        self.facts: list = []  # 지금 평가 중인 환자의 사실 (OfflineEvaluator가 설정)
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _assess(self, trial_id: str | None, rule_id: str) -> LLMAssessment:
        rule = self.rules.get(trial_id or "", {}).get(rule_id)
        if self.judge is not None and rule is not None:
            a = self.judge.assess(trial_id, rule, self.facts)
            if a is not None:
                return a
        return LLMAssessment(rule_id=rule_id, applicable=True, holds="unknown", evidence_type="absent",
                             confidence="low", evidence="근거 없음", reasoning="offline_rule_scope_only",
                             missing_info="오프라인 실행: LLM 기준은 판정하지 않음")

    def _create(self, **kw):
        system = kw.get("system")
        text = system[0]["text"] if isinstance(system, list) and system else system
        if text != matcher.SYSTEM:
            raise OfflineLLMRequest("non_matcher_llm_request")
        user = kw["messages"][0]["content"]
        rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
        m = _TRIAL_TAG.search(user)
        self.requests += 1
        out = MatcherOutput(assessments=[self._assess(m.group(1) if m else None, r["rule_id"]) for r in rules])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=out.model_dump_json())],
                               stop_reason="end_turn", usage=None)


def fact_id(*parts) -> str:
    return "F-" + hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def fact_line(f: SimFact | PatientFact) -> str:
    """원문 한 줄 = 사실의 인용문. 나이·성별·ECOG는 Matcher가 원문에서 찾는 표기(matcher._FIELD_TEXT)를 따름."""
    v = f.value
    if isinstance(v, float) and v.is_integer() and f.field in ("age", "ecog"):
        v = int(v)  # 생성기 값 15.0 -> "나이 15세" (환자값 근거 검사가 원문 표기와 맞춰 봄)
    if f.field == "age":
        return f"나이 {v}세"
    if f.field == "sex":
        return f"성별 {v}"
    if f.field == "ecog":
        return f"ECOG {v}"
    if f.field.startswith("lab:"):
        num = f"{v:g}" if isinstance(v, float) else str(v)
        return f"{f.field[4:]} {num}{' ' + f.unit if f.unit else ''}"
    return f"{f.field}: {v}"


class SimDocumentAuthority:
    """합성 문서 정보원. 운영 권한이 아니며 OfflineEvaluator의 평가 범위 안에서만 켜집니다.

    시뮬레이터의 문서 요청이 값을 확인하면 여기서 증명을 발급합니다. 그래서 시뮬레이터도 Session과 같은 요건을 지납니다:
    증명 없는 document_verified 사실, 값이 다른 사실로 옮긴 증명은 인정되지 않습니다."""

    def _sign(self, document_id: str, digest: str) -> str:
        return hashlib.sha256(f"{SIM_BY}|{document_id}|{digest}".encode()).hexdigest()

    def issue(self, f: PatientFact, origin: str) -> ServerAttestation:
        digest = document_authority.fact_digest(f)
        return ServerAttestation(document_id=origin, sha256=hashlib.sha256(origin.encode()).hexdigest(), fact_digest=digest,
                                 verifier=SIM_BY, signature=self._sign(origin, digest))

    def valid(self, attestation: ServerAttestation, fact) -> bool:
        return (attestation.verifier == SIM_BY and attestation.fact_digest == document_authority.fact_digest(fact)
                and attestation.signature == self._sign(attestation.document_id, attestation.fact_digest))


SIM_AUTHORITY = SimDocumentAuthority()


def _verified_event(f: PatientFact, origin: str) -> VerificationEvent:
    return VerificationEvent(from_status=f.verification_status, to_status="document_verified", reason="sim_document_check",
                             by=SIM_BY, document_reference=origin, attestation=SIM_AUTHORITY.issue(f, origin), at=RECORDED_AT)


def make_fact(alias: str, f: SimFact, *, seq: int, origin: str, source_type: str, verified: bool,
              ref_kind: str, ref: str | None) -> PatientFact:
    """SimFact -> PatientFact (결정적 ID). verified면 합성 문서 증명을 함께 남김."""
    fid = fact_id(alias, seq, origin, f.field, f.value, f.unit, source_type)
    out = PatientFact(fact_id=fid, patient_id=alias, field=f.field.strip().lower(), value=f.value, unit=f.unit,
                      source_type=source_type, source_reference=SourceReference(kind=ref_kind, ref=ref, quote=fact_line(f)),
                      verification_status="unknown", observed_at=f.observed_at, recorded_at=RECORDED_AT, version=1)
    return verify_fact(out, origin) if verified else out


def verify_fact(f: PatientFact, origin: str) -> PatientFact:
    """이미 있는 사실을 문서로 확인함 (FactStore.set_verification과 같은 기록: 사유·기록자·문서 참조). 값·출처는 그대로."""
    return f.model_copy(update={"verification_status": "document_verified",
                                "verification_history": [*f.verification_history, _verified_event(f, origin)]})


def same_value(f: PatientFact, s: SimFact) -> bool:
    return f.field == s.field.strip().lower() and f.value == s.value and (f.unit or None) == (s.unit or None)


def renumber(facts: list[PatientFact]) -> list[PatientFact]:
    """같은 항목 안의 순번·직전 사실 연결 (FactStore.add와 같은 규칙)."""
    out, last = [], {}
    count: dict[str, int] = {}
    for f in facts:
        count[f.field] = count.get(f.field, 0) + 1
        out.append(f.model_copy(update={"version": count[f.field], "previous_fact_id": last.get(f.field)}))
        last[f.field] = f.fact_id
    return out


def on_record(f: PatientFact) -> bool:
    """처음 받은 기록 부분에 쓸 사실: 진료 기록 출처이거나 문서로 확인된 사실 (환자 답변이었어도 문서 확인 뒤에는 기록)."""
    return f.source_type == "clinical_record" or document_backed(f)


def source_text(facts: list[PatientFact]) -> str:
    record = [f.source_reference.quote for f in facts if on_record(f)]
    answers = [f.source_reference.quote for f in facts if not on_record(f)]
    text = "\n".join([INITIAL_HEADER, *dict.fromkeys(record)])
    if answers:
        text += ANSWER_MARKER + "]\n" + "\n".join(dict.fromkeys(answers))
    return text


class OfflineEvaluator:
    """사실 목록 -> DecisionState (전체 재평가). 결정적이며 LLM 호출 0건."""

    def __init__(self, trials: tuple[SimTrial, ...], reference_date: str, judge=None):
        self.trials = trials
        self.reference_date = reference_date
        self.client = OfflineMatcherClient(judge, {t.trial_id: {r.rule_id: r for r in t.parsed.rules} for t in trials})

    @property
    def evaluator_id(self) -> str:
        j = self.client.judge
        return EVALUATOR_ID if j is None else f"{EVALUATOR_ID}+{j.judge_id}{'+draft' if j.include_draft else ''}"

    def snapshot(self, alias: str, facts: list[PatientFact]) -> FactSnapshot:
        return make_snapshot(alias, renumber(facts))

    def evaluate(self, alias: str, facts: list[PatientFact],
                 trial_ids: tuple[str, ...] | None = None) -> tuple[DecisionState, FactSnapshot]:
        """trial_ids: 이 환자의 후보 시험 (None = 전체)."""
        with document_authority.use(SIM_AUTHORITY):  # 합성 문서 증명은 이 범위에서만 유효
            snap = self.snapshot(alias, facts)
            store = snapshot_store(snap)
            text = source_text(snap.facts)
            profile = FactEvidenceView(store, alias).to_profile()
            parsed, tdict, matches = {}, {}, {}
            self.client.facts = list(snap.facts)
            with llm.use_client(self.client):
                for t in self.trials:
                    if trial_ids is not None and t.trial_id not in trial_ids:
                        continue
                    p = t.parsed_with_meta()
                    parsed[t.trial_id], tdict[t.trial_id] = p, dict(t.trial)
                    # Session과 같은 게이트: input_source는 선언일 뿐이고 document_check가 합성 문서 증명을 확인
                    matches[t.trial_id] = matcher.match_trial(
                        profile, p, tdict[t.trial_id], reference_date=self.reference_date, source_text=text,
                        input_source="clinical_record",
                        document_check=evidence_basis.document_check(store, alias, p, self.reference_date))
            view = SessionView(session_id=alias, patient_id=alias, snapshot=snap, matches=matches, parsed=parsed,
                               trials=tdict, reference_date=self.reference_date,
                               matched_versions={t: snap.version for t in matches})
            return build_state(view), snap


__all__ = ["OfflineEvaluator", "OfflineMatcherClient", "OfflineLLMRequest", "EVALUATOR_ID", "SIM_AUTHORITY", "fact_id", "fact_line",
           "make_fact", "verify_fact", "same_value", "source_text"]
