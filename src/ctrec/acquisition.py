"""부족한 정보(Missing Fact)의 확보 순서 - 팀 결정 D7 (주요 유형만 명시 정책).

판정 불가 항목마다 무엇을 어떤 순서로 확보할지(preferred -> alternatives), 무엇을 이미 시도했는지(attempted_actions),
끝내 확보하지 못하면 어떻게 되는지(consequence_if_unresolved)를 관리합니다.

- 명시 정책은 판정을 자주 막는 4개 유형(바이오마커, 검사 수치, 복용 약, 치료 이력)에만 둡니다.
  나머지 유형은 환자·보호자에게 한 번 직접 묻고, 답이 없으면 미해결로 둡니다.
- 사다리의 모든 단계를 시도해도 판정 불가면 미해결(unresolved)로 표시하고 같은 항목을 다시 묻지 않습니다.
- 미해결이어도 시험은 판정 보류(UNCERTAIN)로 추천 후보에 남고, 보고서에 결과(consequence)를 적습니다.
- 자기 보고 단계로 얻은 답은 기록 확인이 필요할 수 있습니다(근거 수준 D2, evidence.py).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel

from .schemas import ClarifyingQuestion, TrialMatch


class AcquisitionPolicy(BaseModel):
    preferred: str
    alternatives: list[str]
    consequence: str


POLICIES: dict[str, AcquisitionPolicy] = {
    "biomarker": AcquisitionPolicy(
        preferred="병리·유전자 검사 결과지에 적힌 결과를 그대로 알려 달라고 요청",
        alternatives=["결과지가 없으면 담당 의료진에게 들은 결과를 묻기 (자기 보고, 기록 확인 필요)"],
        consequence="판정 보류 유지. 등록 전 검사 결과지 확인 또는 시험 기관 스크리닝 검사 필요",
    ),
    "lab": AcquisitionPolicy(
        preferred="최근 혈액검사 결과지의 수치와 검사일을 요청",
        alternatives=["결과지가 없으면 대략적인 수치나 의료진 설명을 묻기 (자기 보고, 기록 확인 필요)"],
        consequence="판정 보류 유지. 시험 기관 스크리닝 검사로 확인",
    ),
    "medication": AcquisitionPolicy(
        preferred="처방전·약 봉투에 적힌 현재 복용 약 이름과 용량을 요청",
        alternatives=["기억나는 약 이름이나 약의 용도(예: 혈압약)를 묻기"],
        consequence="판정 보류 유지. 등록 전 담당 의료진이 복용 약 목록 확인",
    ),
    "treatment_history": AcquisitionPolicy(
        preferred="진료 요약·퇴원 요약지에 적힌 치료 이름과 기간을 요청",
        alternatives=["기억나는 치료 종류와 대략적인 시기를 묻기"],
        consequence="판정 보류 유지. 등록 전 치료 기록 확인",
    ),
}

DEFAULT_POLICY = AcquisitionPolicy(
    preferred="환자·보호자에게 직접 확인",
    alternatives=[],
    consequence="판정 보류 유지. 담당 의료진 확인 필요",
)


def policy_for(category: str) -> AcquisitionPolicy:
    return POLICIES.get(category, DEFAULT_POLICY)


class MissingFact(BaseModel):
    key: str  # "<trial_id>:<rule_id>"
    category: str
    criterion: str
    missing_info: str | None
    preferred_acquisition: str
    alternative_acquisitions: list[str]
    attempted_actions: list[str] = []
    consequence_if_unresolved: str
    status: Literal["open", "resolved", "unresolved"] = "open"

    def steps(self) -> list[str]:
        return [self.preferred_acquisition, *self.alternative_acquisitions]

    def next_step(self) -> str | None:
        steps = self.steps()
        return steps[len(self.attempted_actions)] if len(self.attempted_actions) < len(steps) else None


def update(facts: dict[str, MissingFact], matches: Iterable[TrialMatch]) -> None:
    """최신 판정으로 갱신: 새 미확인 항목 추가, 해소된 항목 resolved, 모든 단계를 시도한 항목 unresolved."""
    from .agents.matcher import is_askable_unknown

    current: dict[str, tuple] = {}
    for m in matches:
        for a in m.assessments:
            if is_askable_unknown(a):
                current[f"{m.trial_id}:{a.rule_id}"] = (a, m)
    for key, (a, _) in current.items():
        if key not in facts:
            p = policy_for(a.category)
            facts[key] = MissingFact(
                key=key, category=a.category, criterion=a.criterion_text, missing_info=a.missing_info,
                preferred_acquisition=p.preferred, alternative_acquisitions=list(p.alternatives),
                consequence_if_unresolved=p.consequence,
            )
    for key, f in facts.items():
        if key not in current:
            if f.status != "unresolved":
                f.status = "resolved"
        elif f.next_step() is None:
            f.status = "unresolved"
        else:
            f.status = "open"


def is_open(facts: dict[str, MissingFact], key: str) -> bool:
    """아직 확보를 시도할 단계가 남은 항목인가 (기록에 없는 항목은 열린 것으로 봄)."""
    f = facts.get(key)
    return f is None or f.status == "open"


def plan(facts: dict[str, MissingFact]) -> dict[str, dict]:
    """질문 생성 에이전트에 넘길 이번 라운드 확보 단계."""
    return {k: {"category": f.category, "step": f.next_step(), "attempted": f.attempted_actions}
            for k, f in facts.items() if f.status == "open" and f.next_step()}


def record_attempts(facts: dict[str, MissingFact], questions: list[ClarifyingQuestion], round_no: int) -> None:
    """이번 라운드 질문이 겨냥한 항목에 시도한 단계를 기록."""
    for q in questions:
        if q.purpose != "eligibility":
            continue
        for key in q.target:
            f = facts.get(key)
            if f is not None and f.status == "open" and f.next_step():
                f.attempted_actions.append(f"라운드 {round_no}: {f.next_step()} (Q {q.question_id})")


def report_section(facts: dict[str, MissingFact], open_trials: set[str]) -> str:
    """보고서에 코드가 고정으로 붙이는 '확보하지 못한 정보' 섹션 (추천 후보 시험만)."""
    items = [f for f in facts.values()
             if f.status in ("unresolved", "open") and f.key.split(":")[0] in open_trials]
    if not items:
        return ""
    lines = ["## 확보하지 못한 정보와 그 영향", "",
             "아래 정보는 판정에 필요하지만 확인하지 못했습니다. 해당 시험은 판정 보류로 남습니다.", ""]
    for f in items:
        tried = "; ".join(f.attempted_actions) or "시도하지 않음(질문 한도)"
        lines.append(f"- **{f.key}** {f.criterion[:100]} - 시도: {tried} → {f.consequence_if_unresolved}")
    return "\n".join(lines) + "\n\n"
