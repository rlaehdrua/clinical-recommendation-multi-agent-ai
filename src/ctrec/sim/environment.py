"""코호트 환경 (설계 §3.1 단계 3). oracle을 쥐고 행동을 실행해 실행 결과와 새 사실을 돌려줍니다. 판정·정책 결정은 하지 않음.

실행 규칙
- 정보원이 없거나 실행하지 않는 행동(DEFER·FINALIZE): not_executed.
- system 정보원: LLM이 드는 정보원(system_reparse)은 오프라인에서 실행하지 않음(not_executed, 청구 없음).
  그 밖(system_check·trial_registry)은 실행되지만 환자 사실을 드러내지 않음.
- clinician(임상의 검토): 실행되지만 oracle에 임상 판단 정답이 없으므로 사실·판정을 만들지 않음 (EXPERT_REVIEW 유지).
- patient·document: 응답 모델로 연락 불가(정보원 단위 실패) -> 항목 없음·"모름"(항목 단위) -> 응답 순서로 정함.
  응답하면 그 정보원이 드러낼 수 있는 정답 사실 중 행동이 요구한 항목(required_fields)만 새 사실로 만듦.
  환자 답변은 self_report. 문서는 같은 값의 기존 사실이 있으면 그 사실을 문서 확인(document_verified)으로 바꾸고
  (FactStore.set_verification과 같은 방식 - 같은 값을 새 사실로 겹쳐 넣지 않음), 없거나 값이 다르면 clinical_record 새 사실
  (can_verify 정보원이면 문서 확인 상태). 값이 다른 새 사실은 기존 사실과 상충으로 남습니다.
- 실패·무응답·"모름"은 새 사실 0개 (계약이 강제). 대체 정보원은 정책이 고르며 환경이 자동으로 실행하지 않음.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Literal

from ..engine.bundles import SourceCatalog
from ..engine.contracts import NO_ANSWER, NON_EXECUTING, UNREACHABLE, Action
from ..fact_store import PatientFact
from .cohort import SimCohort, SimFact
from .evaluator import make_fact, same_value
from .response_model import ResponseModel

Execution = Literal["answered", "declined", "failed", "not_executed"]
OFFLINE_LLM_DISABLED = "offline_llm_disabled"


@dataclass(frozen=True)
class EnvResult:
    execution: Execution
    facts: tuple[PatientFact, ...] = ()  # 새 사실
    verified: tuple[str, ...] = ()  # 문서로 확인한 기존 사실 ID
    error_code: str | None = None
    latency_minutes: float = 0.0
    note: str | None = None  # 미실행 사유 등 (코드)


class CohortEnvironment:
    def __init__(self, cohort: SimCohort, catalog: SourceCatalog, responses: ResponseModel):
        self.catalog = catalog
        self.responses = responses
        self._alias = cohort.aliases()
        self._truth = {self._alias[k]: tuple(v) for k, v in cohort.oracle.truth.items()}
        self._criterion_slots = dict(cohort.criterion_slots)

    def revealable(self, alias: str, source_id: str, action: Action) -> list[SimFact]:
        """그 정보원이 드러낼 수 있는 정답 사실 중 행동이 요구한 것.

        요구: 대상 기준에 생성기 슬롯 연결이 있으면 그 슬롯의 사실만 (LLM 기준의 유형별 후보 항목 - diagnosis·lab:* 등 -
        전체를 드러내지 않음). 연결이 없으면 행동의 required_fields와 같은 항목.
        정보원: 사실에 sources가 있으면 그 목록(생성기 소스 대응), 없으면 카탈로그 provides_fields + 환자가 아는 항목."""
        src = self.catalog[source_id]
        fields = set(action.required_fields)
        slots = {s for k in action.target_criteria for s in self._criterion_slots.get(k, ())}
        out = []
        for f in self._truth.get(alias, ()):
            wanted = f.slot in slots if (slots and f.slot is not None) else f.field in fields
            if not wanted:
                continue
            if f.sources is not None:
                if source_id not in f.sources:
                    continue
            elif not any(fnmatch(f.field, p) for p in src.provides_fields) or (
                    src.kind == "patient" and not f.known_by_patient()):
                continue
            out.append(f)
        return out

    def execute(self, action: Action, attempt: int, step: int, source_ref: str,
                current: list[PatientFact] = ()) -> EnvResult:
        src = self.catalog.get(action.source_id)
        if src is None or action.action_type in NON_EXECUTING:
            return EnvResult("not_executed", note="no_source")
        if src.kind == "system":
            if src.request_cost.llm_calls:
                return EnvResult("not_executed", note=OFFLINE_LLM_DISABLED)
            return EnvResult("answered")
        p = self.responses.params(src.kind)
        a, sid, fp = action.patient_alias, src.source_id, action.fingerprint
        lo, hi = p.latency_scale
        latency = src.request_cost.patient_wait_minutes * (lo + (hi - lo) * self.responses.draw(a, sid, fp, attempt,
                                                                                                    "latency"))
        if src.kind == "clinician":
            return EnvResult("answered", latency_minutes=latency)
        if self.responses.draw(a, sid, fp, attempt, "reach") < p.p_unreachable:
            return EnvResult("failed", error_code=UNREACHABLE, latency_minutes=latency)
        found = self.revealable(a, sid, action)
        if not found or self.responses.draw(a, sid, fp, attempt, "answer") < p.p_no_answer:
            return EnvResult("declined", error_code=NO_ANSWER, latency_minutes=latency)
        if src.kind == "patient":
            facts = tuple(make_fact(a, f, seq=step, origin=sid, source_type="self_report", verified=False,
                                    ref_kind="answer", ref=source_ref) for f in found)
            return EnvResult("answered", facts, latency_minutes=latency)
        new, verified = [], []
        for f in found:
            same = [c.fact_id for c in current if same_value(c, f) and c.verification_status != "document_verified"]
            if same and src.can_verify:
                verified += same
            elif not any(same_value(c, f) for c in current) or not src.can_verify:
                new.append(make_fact(a, f, seq=step, origin=sid, source_type="clinical_record",
                                     verified=src.can_verify, ref_kind="document", ref=source_ref))
        return EnvResult("answered", tuple(new), tuple(dict.fromkeys(verified)), latency_minutes=latency)


__all__ = ["CohortEnvironment", "EnvResult", "OFFLINE_LLM_DISABLED"]
