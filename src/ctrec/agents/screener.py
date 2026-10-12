"""후보 추림 에이전트: 전체 시험 풀을 한 번에 훑어, 자세히 평가할 후보 시험을 고릅니다.

자세한 판정(기준 파싱 + 매칭)은 시험마다 LLM을 호출하므로 비쌉니다. 그래서 먼저 시험의 제목·대상 질환·
대상군·요약만 보고 "이 환자에게 해당될 여지가 있는 시험"만 남깁니다. 세부 기준(검사 수치, 치료 이력 등)은
여기서 판단하지 않고 매칭 단계에 맡깁니다.

안전 원칙(정보 부족 ≠ 부적격):
- 다른 질환·대상군이 명백할 때만 제외하고, 애매하면 남깁니다.
- 모델이 판단을 빠뜨린 시험은 남깁니다(제외하려면 명시적 사유가 필요).
- 목록에 없는 시험 ID는 버립니다.

LLM 전에 값싼 확정 제외(hard_filter.py: 모집 종료, 나이·성별 구조화 필드의 명백한 불일치)를 코드로 먼저 적용합니다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .. import config, hard_filter, llm
from ..hard_filter import HardFiltered
from ..schemas import PatientProfile

SYSTEM = """당신은 임상시험 추천 시스템의 '후보 추림 에이전트'입니다.
<trial_pool>의 모든 임상시험에 대해, 주어진 환자가 참여 대상이 될 여지가 있는지 1차로 가려냅니다.
선택된 시험은 이후 다른 에이전트가 선정·제외 기준을 하나씩 자세히 판정합니다.

판단 기준:
- 환자가 가진 질환·상태는 환자 기록에 적힌 것(주 진단, 동반 질환, 증상, 현재 상황)으로 봅니다.
- keep: 시험의 대상 질환·상태가 환자 기록의 질환·상태와 맞고, 대상군(나이대·성별 등)이 명백히 어긋나지 않음
- exclude: 시험의 대상 질환·상태가 환자 기록에 없는 경우(예: 급성 췌장염 환자에게 편두통·망막박리·폐암 시험),
  또는 대상군이 명백히 다른 경우(예: 영아 전용 시험에 성인 환자).
  "기록에 없지만 그 질환이 있을 수도 있다"는 이유로 남기지 않습니다. 환자는 자신의 질환에 맞는 시험을 찾는 중입니다.
- exclude 사유는 두 가지뿐입니다: ① 대상 질환 자체가 다름 ② 나이·성별 범위를 명백히 벗어남(구조화 필드 기준).
- 대상 질환이 같으면 세부 대상군 차이(원인, 동반 질환, 침범 부위, 병기, 치료 이력, 재발 횟수, 검사 수치 등)로는
  exclude하지 않고 keep합니다. 이런 세부 사항은 다음 단계에서 기준별로 판정하고, 맞지 않으면 그때 부적격이 됩니다.
- 대상 질환이 맞는지 자체가 애매하면(예: 환자 증상이 시험 대상 질환을 시사하지만 확진 전) keep입니다.
- 모집 상태(완료·모집 중 등)는 고려하지 않습니다.
- relevance: keep인 시험의 적합도. high = 환자의 주 진단을 직접 대상으로 함, medium = 관련 있으나 대상이 일부만 겹침, low = 가능성은 있으나 약함.
  exclude인 시험은 low로 둡니다.
- reason: 한국어 한 문장. exclude면 무엇이 어긋나는지 구체적으로 적습니다.
- <trial_pool>의 모든 trial_id에 대해 정확히 하나씩 반환합니다.
"""


class ScreenedTrial(BaseModel):
    trial_id: str
    decision: Literal["keep", "exclude"]
    relevance: Literal["high", "medium", "low"]
    reason: str = Field(description="한국어 한 문장")


class ScreeningOutput(BaseModel):
    trials: list[ScreenedTrial]


class ScreeningResult(BaseModel):
    """코드 검증을 거친 추림 결과 (result.json에 저장)."""
    pool_size: int
    selected: list[ScreenedTrial]
    excluded: list[ScreenedTrial]
    over_limit: list[ScreenedTrial] = []  # 해당 가능하지만 상한(max_candidates)을 넘어 자세히 평가하지 않은 시험
    kept_by_default: list[str] = []      # 모델이 판단을 빠뜨려 안전하게 남긴 시험
    hard_filtered: list[HardFiltered] = []  # LLM 전에 코드가 확정 제외한 시험 (모집 종료, 나이·성별 명백한 불일치)


def trial_card(t: dict) -> str:
    return (
        f"<trial id=\"{t['trial_id']}\">\n"
        f"title: {t.get('title', '')}\n"
        f"conditions: {', '.join(t.get('conditions') or [])}\n"
        f"keywords: {', '.join((t.get('keywords') or [])[:8])}\n"
        f"population: sex={t.get('sex')}, minimum_age={t.get('minimum_age')}, maximum_age={t.get('maximum_age')}\n"
        f"summary: {(t.get('brief_summary') or '')[:500]}\n"
        f"eligibility (excerpt): {(t.get('eligibility_criteria') or '')[:500]}\n"
        "</trial>"
    )


def _patient_summary(p: PatientProfile, raw_text: str = "") -> str:
    lines = [
        f"age: {p.age}, sex: {p.sex}",
        f"primary_diagnosis: {p.primary_diagnosis}",
        f"disease_stage: {p.disease_stage}",
    ]
    if p.diagnoses:
        lines.append("other diagnoses: " + "; ".join(d.description for d in p.diagnoses[:10]))
    if p.treatment_history:
        lines.append("treatment history: " + "; ".join(t.description for t in p.treatment_history[:6]))
    if raw_text:
        lines.append(f"original record:\n{raw_text[:3000]}")
    return "\n".join(lines)


_ORDER = {"high": 0, "medium": 1, "low": 2}


def screen(profile: PatientProfile, pool: dict[str, dict], max_candidates: int, raw_text: str = "") -> ScreeningResult:
    pool_size = len(pool)
    pool, filtered = hard_filter.apply(pool, profile)
    if not pool:
        return ScreeningResult(pool_size=pool_size, selected=[], excluded=[], hard_filtered=filtered)
    ids = list(pool)
    # 시험 풀은 환자가 바뀌어도 같으므로 시스템 프롬프트에 넣어 프롬프트 캐싱으로 재사용
    system = SYSTEM + "\n<trial_pool>\n" + "\n".join(trial_card(pool[t]) for t in ids) + "\n</trial_pool>"
    out = llm.structured(
        ScreeningOutput,
        system=system,
        user=f"<patient>\n{_patient_summary(profile, raw_text)}\n</patient>\n\n위 환자에 대해 <trial_pool>의 모든 시험을 판단하세요.",
        effort=config.EFFORT["screener"],
    )
    judged: dict[str, ScreenedTrial] = {}
    for s in out.trials:
        if s.trial_id in pool and s.trial_id not in judged:  # 화이트리스트 + 중복 제거
            judged[s.trial_id] = s
    missing = [t for t in ids if t not in judged]
    for t in missing:
        judged[t] = ScreenedTrial(trial_id=t, decision="keep", relevance="low",
                                  reason="추림 에이전트가 판단을 반환하지 않아 안전하게 후보로 남김")
    keep = sorted((judged[t] for t in ids if judged[t].decision == "keep"),
                  key=lambda s: (_ORDER[s.relevance], ids.index(s.trial_id)))
    return ScreeningResult(
        pool_size=pool_size,
        selected=keep[:max_candidates],
        excluded=[judged[t] for t in ids if judged[t].decision == "exclude"],
        over_limit=keep[max_candidates:],
        kept_by_default=missing,
        hard_filtered=filtered,
    )


def report_section(r: ScreeningResult) -> str:
    """report.md에 코드가 고정으로 붙이는 '후보 추림' 섹션."""
    lines = ["## 후보 시험 추림", "",
             f"전체 {r.pool_size}개 시험 중 {len(r.selected)}개를 골라 자세히 평가했습니다.", ""]
    lines += [f"- **{s.trial_id}** ({s.relevance}): {s.reason}" for s in r.selected]
    if r.over_limit:
        lines += ["", f"해당 가능성이 있지만 상한을 넘어 자세히 평가하지 않은 시험 {len(r.over_limit)}개:"]
        lines += [f"- {s.trial_id} ({s.relevance}): {s.reason}" for s in r.over_limit]
    if r.hard_filtered:
        lines += ["", f"<details><summary>모집 종료·나이·성별이 명백히 맞지 않아 먼저 뺀 시험 {len(r.hard_filtered)}개</summary>", ""]
        lines += [f"- {h.trial_id}: {h.reason}" for h in r.hard_filtered]
        lines += ["", "</details>"]
    if r.excluded:
        lines += ["", f"<details><summary>대상이 달라 제외한 시험 {len(r.excluded)}개</summary>", ""]
        lines += [f"- {s.trial_id}: {s.reason}" for s in r.excluded]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n\n"
