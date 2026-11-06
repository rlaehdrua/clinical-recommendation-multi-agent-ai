"""시뮬레이터 입력 코호트: public(정책·평가기가 보는 부분)과 oracle(환경·지표만 보는 정답)으로 나뉩니다.

- SimTrial: 시험 저장소 형식 그대로(ParsedTrial + 시험 dict + 규칙 메타). 기존 Matcher가 그대로 판정합니다.
- SimPatient.public: 처음 알려진 사실 (출처·검증 상태 포함). 정책은 이 사실로 만든 CohortState만 봅니다.
- OraclePart.truth: 환자의 실제 사실 전체. 정보원 행동이 성공하면 환경이 여기서 값을 꺼내 새 사실로 만듭니다.
  OraclePart는 정책·평가기에 넘기지 않습니다 (설계 §9 oracle 격리).

환자 키(합성 ID)는 AliasMap(salt)으로 별칭으로 바꾼 뒤에만 실행에 들어갑니다. salt·원래 키는 기록에 남기지 않습니다.
생성기(src/ctrec/cohort)의 슬롯 코호트를 이 형식으로 옮기는 변환은 슬롯 -> 사실 항목 대응표(설계 §2.1, Q-G1) 합의 뒤에 둡니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..engine.contracts import canonical_hash
from ..eval_hygiene import AliasMap
from ..schemas import ParsedTrial, RuleMeta

# 환자가 직접 답할 수 있는 항목 (검사 수치·바이오마커·진단명은 문서로만 확인). 정보원 카탈로그의 provides_fields와 교집합
PATIENT_KNOWN_FIELDS = frozenset({"age", "sex", "ecog", "medication", "treatment_history", "other_fact",
                                  "reproductive"})


class SimFact(BaseModel):
    """사실 하나 (FactStore.add 입력과 같은 항목). verified는 public 사실의 문서 검증 여부."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    value: bool | int | float | str
    unit: str | None = None
    observed_at: str | None = None
    source_type: Literal["clinical_record", "self_report"] = "clinical_record"
    verified: bool = False
    patient_knows: bool | None = None  # None = 항목 기본값 (PATIENT_KNOWN_FIELDS)

    def known_by_patient(self) -> bool:
        if self.patient_knows is not None:
            return self.patient_knows
        return self.field in PATIENT_KNOWN_FIELDS


@dataclass(frozen=True)
class SimTrial:
    parsed: ParsedTrial
    trial: dict
    rule_meta: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))

    @property
    def trial_id(self) -> str:
        return self.parsed.trial_id

    def parsed_with_meta(self) -> ParsedTrial:
        p = self.parsed.model_copy(deep=True)
        p.rule_meta = {k: RuleMeta.model_validate(v) for k, v in self.rule_meta.items()}
        return p

    def digest_obj(self) -> dict:
        return {"parsed": self.parsed.model_dump(mode="json"), "trial": self.trial,
                "rule_meta": {k: RuleMeta.model_validate(v).model_dump(mode="json")
                              for k, v in sorted(self.rule_meta.items())}}


@dataclass(frozen=True)
class SimPatient:
    key: str  # 합성 ID (실행에는 별칭만 들어감)
    public: tuple[SimFact, ...] = ()


@dataclass(frozen=True)
class OraclePart:
    truth: MappingProxyType  # 환자 키 -> tuple[SimFact, ...]


@dataclass(frozen=True)
class SimCohort:
    cohort_id: str
    reference_date: str
    trials: tuple[SimTrial, ...]
    patients: tuple[SimPatient, ...]
    oracle: OraclePart
    salt: str = "sim-cohort"

    def __post_init__(self):
        keys = [p.key for p in self.patients]
        if len(keys) != len(set(keys)):
            raise ValueError("환자 키 중복")
        tids = [t.trial_id for t in self.trials]
        if len(tids) != len(set(tids)):
            raise ValueError("시험 ID 중복")
        missing = sorted(set(keys) - set(self.oracle.truth))
        if missing:
            raise ValueError(f"정답 사실이 없는 환자 {len(missing)}명")

    def aliases(self) -> dict[str, str]:
        """환자 키 -> 별칭 (같은 salt면 같은 별칭)."""
        am = AliasMap(salt=self.salt)
        return {p.key: am.alias(p.key) for p in self.patients}

    def store_hash(self) -> str:
        """시험 저장소 해시 (ExperimentSpec.data_version.frozen_store_hash)."""
        return canonical_hash([t.digest_obj() for t in self.trials])

    def digest(self) -> str:
        """코호트 해시 (data_version.cohort_hash): public·oracle 사실, 기준일, 별칭 salt."""
        return canonical_hash({
            "cohort_id": self.cohort_id, "reference_date": self.reference_date, "salt": self.salt,
            "patients": [{"key": p.key, "public": [f.model_dump(mode="json") for f in p.public],
                          "truth": [f.model_dump(mode="json") for f in self.oracle.truth[p.key]]}
                         for p in self.patients]})


def make_cohort(cohort_id: str, reference_date: str, trials: list[SimTrial],
                patients: list[tuple[str, list[SimFact], list[SimFact]]], salt: str = "sim-cohort") -> SimCohort:
    """patients: [(키, public 사실, 정답 사실)]."""
    return SimCohort(cohort_id, reference_date, tuple(trials),
                     tuple(SimPatient(k, tuple(pub)) for k, pub, _ in patients),
                     OraclePart(MappingProxyType({k: tuple(tr) for k, _, tr in patients})), salt)


__all__ = ["SimFact", "SimTrial", "SimPatient", "OraclePart", "SimCohort", "make_cohort", "PATIENT_KNOWN_FIELDS"]
