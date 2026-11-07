"""임상의 판단을 인정하는 서버 측 지점 (document_authority와 같은 방식, 판정 계층 공용).

임상의 판단 기준(expert_boundary.clinician_reason)은 여기서 인정한 판단으로만 MET/NOT_MET이 됩니다.
기본 권한은 NoClinicianAuthority이고 어떤 판단도 인정하지 않습니다: 상태 문자열("clinician_decided"), decided_by 값,
요청 본문에 적힌 판단, 직접 만든 서명은 판단이 아닙니다. 실제 의료기관의 의사 인증·권한 연동은 없으므로
운영 경로의 권한은 지금 NoClinicianAuthority뿐입니다 (합성 시험 전용 권한은 ctrec.followup.verification).

판단은 기준(criterion_version)과 그때의 환자 사실(fact_version)에 묶입니다. 사실이 하나라도 바뀌면 이전 판단은
인정되지 않고(보수적) 기준은 다시 임상의 판단 대기로 돌아갑니다.
권한은 컨텍스트 값이라 use() 밖으로 새지 않습니다.
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import Literal, Protocol

from pydantic import BaseModel, Field

Decision = Literal["MET", "NOT_MET"]
JUDGED_FLAG = "clinician_judged"  # validation_flags: "clinician_judged:<judgment_id>"


class ClinicianJudgment(BaseModel):
    """임상의 한 명이 기준 하나에 내린 판단. 형식이 맞아도 현재 권한이 인정해야 효력이 있음."""
    judgment_id: str = Field(pattern=r"^CJ-[0-9a-f]{16}$")
    case_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    decision: Decision
    criterion_version: str = Field(min_length=1)  # decision_state.criterion_version
    fact_version: str = Field(min_length=1)  # 판단 때의 사실 스냅샷 버전
    clinician: str = Field(min_length=1)  # 권한이 정한 판단자 식별자 (예: synthetic-clinician:<id>)
    rationale_code: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    # 판단 근거로 든 환자 사실 (진료 기록이 필요한 기준이면 판단 때 모두 문서로 확인된 사실이어야 함). decision_state의 근거 참조가 됨
    basis_fact_ids: list[str] = []
    at: str
    signature: str = Field(min_length=1)


class ClinicianAuthority(Protocol):
    def judgment_for(self, trial_id: str, rule) -> ClinicianJudgment | None:
        """지금 사실·기준에 대해 인정되는 이 기준의 판단 (없으면 None)."""
        ...


class NoClinicianAuthority:
    """운영 기본값: 의료기관 의사 인증 연동이 없음. 어떤 판단도 인정하지 않음."""

    def judgment_for(self, trial_id: str, rule) -> ClinicianJudgment | None:
        return None


_authority: contextvars.ContextVar[ClinicianAuthority] = contextvars.ContextVar("clinician_authority",
                                                                                 default=NoClinicianAuthority())


def current() -> ClinicianAuthority:
    return _authority.get()


@contextlib.contextmanager
def use(authority: ClinicianAuthority):
    """이 컨텍스트에서만 authority로 판단. 끝나면(예외 포함) 이전 값으로 복구, 중첩 가능."""
    token = _authority.set(authority)
    try:
        yield authority
    finally:
        _authority.reset(token)


def judgment_flag(j: ClinicianJudgment) -> str:
    return f"{JUDGED_FLAG}:{j.judgment_id}"


def judged_id(a) -> str | None:
    """판정에 붙은 임상의 판단 ID (없으면 None)."""
    for f in getattr(a, "validation_flags", []) or []:
        if f.startswith(JUDGED_FLAG + ":"):
            return f.split(":", 1)[1]
    return None


__all__ = ["ClinicianAuthority", "ClinicianJudgment", "Decision", "JUDGED_FLAG", "NoClinicianAuthority", "current",
           "judged_id", "judgment_flag", "use"]
