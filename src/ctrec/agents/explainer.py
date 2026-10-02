"""결과 설명 에이전트: 최종 판정·추천의 근거를 사람이 읽을 수 있는 보고서로 작성."""

from __future__ import annotations

import json

from .. import DISCLAIMER, config, llm
from ..schemas import PatientProfile, QAPair, Recommendation, TrialMatch

SYSTEM = """당신은 임상시험 추천 결과를 설명하는 '결과 설명 에이전트'입니다.
다른 에이전트들의 판정·추천 결과를 연구진과 환자가 모두 이해할 수 있는 한국어 Markdown 보고서로 정리합니다.

보고서 구성:
1. 환자 요약 (3~5줄, 판정에 쓰인 핵심 정보만)
2. 최종 추천: recommendation.recommended(1순위, 공동 1순위면 모두)와 selection_reason을 먼저 제시합니다.
   이어서 차순위 후보 표 (순위 | 시험 ID | 제목 | 판정 | 모집 상태 | 한 줄 사유). 추천이 없으면(no_recommendation_reason 존재) 표 대신 그 이유를 그대로 적습니다
3. 시험별 판단 근거: 추천 순위대로, 결정적인 기준 몇 개를 "기준 → 환자 근거 → 판정" 형식으로 제시. 판정 불가 항목은 무엇을 확인하면 되는지 명시
4. 제외된 시험과 제외 사유 (모집 종료로 제외된 시험은 적격성과 별개로 "현재 참여 불가"임을 분명히 구분)
5. 확인 질문과 답변 기록 (있는 경우), 그리고 그 답변이 판정을 어떻게 바꿨는지. 의사 참고용(physician_reference) 문답은 판정용 문답과 구분해 표시
6. 남은 확인 사항

원칙:
- 입력에 있는 판정 결과와 근거만 사용합니다. 판정을 바꾸거나 새로운 의학적 주장을 추가하지 않습니다.
- 치료 효과를 약속하거나 참여를 권유하는 표현을 쓰지 않습니다.
- 면책 고지와 '의사 확인 필요 사항' 섹션은 코드가 자동으로 붙이므로 작성하지 않습니다.
"""


def explain(
    profile: PatientProfile,
    matches: list[TrialMatch],
    rec: Recommendation,
    trials: dict[str, dict],
    qa_log: list[QAPair],
    match_history: list[dict],
) -> str:
    payload = {
        "patient": profile.model_dump(),
        "recommendation": rec.model_dump(),
        "trials": {tid: {"title": t.get("title"), "overall_status": t.get("overall_status"),
                         "url": (t.get("source") or {}).get("url")} for tid, t in trials.items()},
        "matches": [m.model_dump() for m in matches],
        "qa_log": [qa.model_dump() for qa in qa_log],
        "eligibility_history": match_history,
    }
    body = llm.plain(
        system=SYSTEM,
        user=f"<results>\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n</results>\n\n보고서를 작성하세요.",
        effort=config.EFFORT["explainer"],
    )
    head = f"# 임상시험 추천 보고서 - {profile.patient_id}\n\n> {DISCLAIMER}\n\n"
    if rec.no_recommendation_reason:
        # 추천이 없는 이유는 LLM 문장과 별개로 코드가 맨 앞에 고정 표기
        head += f"## 추천 결과: 추천 가능한 임상시험 없음\n\n**{rec.no_recommendation_reason}**\n\n"
    elif rec.recommended:
        titles = {tid: t.get("title", "") for tid, t in trials.items()}
        tie = " (공동 1순위)" if len(rec.recommended) > 1 else ""
        head += f"## 최종 추천{tie}\n\n" + "\n".join(
            f"- **{r.trial_id}** ({r.eligibility}) {titles.get(r.trial_id, '')}" for r in rec.recommended
        ) + f"\n\n{rec.selection_reason}\n\n"
    return f"{head}{body.strip()}\n\n{_physician_section(rec)}---\n{DISCLAIMER}\n"


def _physician_section(rec: Recommendation) -> str:
    """원문에 구체 기준이 없는 항목: 의사가 최종 판단할 때 참고할 사항 (코드가 고정 기재)."""
    items = [(r.trial_id, n) for r in rec.ranked for n in r.physician_review]
    if not items:
        return ""
    lines = ["## 의사 확인 필요 사항 (참고)", "",
             "아래 기준은 공개된 프로토콜 원문에 구체적인 목록·기준값이 없어 시스템이 판정하지 않았습니다. "
             "환자에게서 수집한 관련 정보를 함께 적었으니, 참여 여부를 최종 결정할 때 담당 의사가 "
             "전체 프로토콜과 대조해 확인하세요.", ""]
    lines += [f"- **{tid}** - {note}" for tid, note in items]
    return "\n".join(lines) + "\n\n"
