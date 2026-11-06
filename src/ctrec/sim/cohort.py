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
    slot: str | None = None  # 생성기 슬롯 ID (생성기 코호트에서 옮긴 사실)
    sources: tuple[str, ...] | None = None  # 이 사실을 드러낼 수 있는 엔진 정보원 (None = 카탈로그·항목 기본 규칙)

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
    trials: tuple[str, ...] | None = None  # 후보 시험 (None = 코호트의 모든 시험)


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
    # "<시험>:<기준>" -> 그 기준이 보는 슬롯 (생성기 연결). 환경은 이 슬롯의 사실만 드러냄 (유형별 후보 항목 전체가 아님)
    criterion_slots: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    judge: object | None = field(default=None, compare=False)  # LLM 기준 대신 판정하는 결정적 판정기 (sim.judge)

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
        unknown = sorted({t for p in self.patients for t in (p.trials or ())} - set(tids))
        if unknown:
            raise ValueError(f"코호트에 없는 후보 시험 {unknown}")

    def trials_of(self, p: SimPatient) -> tuple[SimTrial, ...]:
        return self.trials if p.trials is None else tuple(t for t in self.trials if t.trial_id in p.trials)

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
            "patients": [{"key": p.key, "trials": list(p.trials) if p.trials is not None else None,
                          "public": [f.model_dump(mode="json") for f in p.public],
                          "truth": [f.model_dump(mode="json") for f in self.oracle.truth[p.key]]}
                         for p in self.patients],
            "criterion_slots": {k: list(v) for k, v in sorted(self.criterion_slots.items())},
            "judge": self.judge.digest() if self.judge is not None else None})


def make_cohort(cohort_id: str, reference_date: str, trials: list[SimTrial],
                patients: list[tuple], salt: str = "sim-cohort", criterion_slots: dict | None = None,
                judge=None) -> SimCohort:
    """patients: [(키, public 사실, 정답 사실)] 또는 [(키, public, 정답, 후보 시험)]."""
    return SimCohort(cohort_id, reference_date, tuple(trials),
                     tuple(SimPatient(p[0], tuple(p[1]), tuple(p[3]) if len(p) > 3 and p[3] is not None else None)
                           for p in patients),
                     OraclePart(MappingProxyType({p[0]: tuple(p[2]) for p in patients})), salt,
                     MappingProxyType({k: tuple(v) for k, v in (criterion_slots or {}).items()}), judge)


__all__ = ["SimFact", "SimTrial", "SimPatient", "OraclePart", "SimCohort", "make_cohort", "PATIENT_KNOWN_FIELDS"]
