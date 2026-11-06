"""정보원 응답 모델 (설계 §3.2). 성공·실패·"모름"·지연을 seed로 정합니다. 값은 정하지 않음 (값은 oracle).

- 파라미터는 모두 **가정값**(basis: assumed)이며 manifest에 그대로 남깁니다. 정책에는 보이지 않습니다 (계약 원칙 5).
- 난수는 행동마다 독립 스트림: derive(seed, 환자 별칭, 정보원, 행동 지문, 시도 횟수). 정책이 행동 순서를 바꿔도 같은
  (환자, 정보원, 행동, 시도 횟수)는 같은 결과를 받습니다 (공통 난수 - 정책 간 차이가 응답 운이 아니라 선택에서 나오게).
  시도 횟수는 그 정보원에 같은 행동을 보낸 횟수입니다(Runner). 설계 §3.2의 OpsState.attempts[지문]은 정보원을 가리지 않아,
  환자에게 한 번 더 물은 설정에서는 같은 문서 요청이 다른 난수를 받으므로 정보원별로 셉니다 (11-06).
- 바꿔 묻기(REPHRASE): 지문이 달라 첫 질문과 독립된 난수를 받고, "모름" 확률은 p_no_answer_rephrased (없으면 p_no_answer).
  환자 기본값 0.1은 "쉬운 말·단서를 넣어 다시 물으면 이해·기억 실패가 절반으로 준다"는 **가정값**입니다. 환자가 원래 모르는
  값(patient_knows=False)은 말을 바꿔도 드러나지 않습니다 (환경이 강제).
- 실패 범위: 연락·접근 불가는 정보원 단위(unreachable), 항목을 모름·문서에 항목 없음은 항목 단위(no_answer).
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from ..engine.contracts import canonical_hash


class SourceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    p_unreachable: float = Field(ge=0, le=1)  # 정보원 전체 실패
    p_no_answer: float = Field(ge=0, le=1)  # 연락은 됐지만 그 항목을 모름 (알 수 있는 값이 있어도)
    p_no_answer_rephrased: float | None = Field(default=None, ge=0, le=1)  # 바꿔 묻기의 "모름" (None = p_no_answer)
    latency_scale: tuple[float, float] = (0.5, 1.5)  # 지연 = 예상 대기(patient_wait_minutes) x 균등(a, b)


DEFAULT_RESPONSES = {
    "patient": SourceResponse(p_unreachable=0.05, p_no_answer=0.2, p_no_answer_rephrased=0.1),
    "document": SourceResponse(p_unreachable=0.1, p_no_answer=0.0),
    "clinician": SourceResponse(p_unreachable=0.0, p_no_answer=0.0),
    "system": SourceResponse(p_unreachable=0.0, p_no_answer=0.0, latency_scale=(0.0, 0.0)),
}


def uniform(*parts) -> float:
    """해시 기반 [0, 1) 난수 (생성기 derive_seed와 같은 방식, 결정적)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:13], 16) / float(1 << 52)


class ResponseModel:
    basis = "assumed"

    def __init__(self, seed: int, responses: dict[str, SourceResponse] | None = None):
        self.seed = int(seed)
        self.responses = dict(DEFAULT_RESPONSES if responses is None else responses)

    def params(self, kind: str) -> SourceResponse:
        return self.responses[kind]

    def draw(self, alias: str, source_id: str, fingerprint: str, attempt: int, what: str) -> float:
        return uniform(self.seed, alias, source_id, fingerprint, attempt, what)

    def describe(self) -> dict:
        return {"basis": self.basis, "seed": self.seed,
                "responses": {k: v.model_dump(mode="json") for k, v in sorted(self.responses.items())},
                "hash": canonical_hash({k: v.model_dump(mode="json") for k, v in sorted(self.responses.items())})}


__all__ = ["ResponseModel", "SourceResponse", "DEFAULT_RESPONSES", "uniform"]
