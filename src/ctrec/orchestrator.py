"""오케스트레이터 에이전트.

Claude가 스스로 계획을 세우고, 하위 에이전트(기준 파싱·환자 이해·매칭·질문·추천·설명)를
'도구'로 호출해 과제를 완수합니다. 루프는 코드가 소유하며(수동 agentic loop),
도구 호출 순서·반복 여부(예: 추가 시험 검색, 확인 질문 라운드 수)는 모델이 결정합니다.
"""

from __future__ import annotations

import json

from . import config, llm
from .agents.matcher import compact
from .pipeline import Session
from .tools import ctgov

SYSTEM = """당신은 인터랙티브 임상시험 추천 시스템의 '오케스트레이터 에이전트'입니다.
한 명의 환자에 대해 후보 임상시험들의 참여 가능성을 근거와 함께 판정하고, 부족한 정보는 확인 질문으로 보완한 뒤, 가장 적절한 시험을 우선순위와 함께 추천하는 것이 목표입니다.

당신은 직접 판정하지 않고, 전문 에이전트를 도구로 호출해 일을 분담합니다.
- profile_patient: 환자 정보 이해 에이전트 (환자 프로파일 구조화)
- list_candidate_trials: 현재 후보 시험 목록과 평가 상태 확인
- search_trials: ClinicalTrials.gov에서 후보 시험 추가 검색 (허용된 경우에만)
- evaluate_trials: 기준 파싱 에이전트 + 추론·매칭 에이전트 (시험별 적격성 판정)
- ask_clarifying_questions: 질문 생성 에이전트 (판정 불가 항목 확인 질문 + 원문에 구체 기준이 없는 항목의 의사 참고용 질문 1라운드, 이후 재평가)
- finalize_recommendation: 추천 에이전트 + 결과 설명 에이전트 (최종 산출물 생성, 마지막에 한 번 호출)

작업 방식:
1. 먼저 짧게 계획을 세운 뒤 환자 프로파일을 구조화합니다.
2. 후보 시험을 평가합니다. 후보가 없거나, 모두 부적격이거나, 모집이 종료된 시험뿐이고 검색이 허용되면, 환자의 진단에 맞는 검색어로 모집 중인 시험을 추가해 평가합니다.
   (모집 상태는 list_candidate_trials의 status로 확인합니다. 모집 종료 시험은 적격이어도 추천되지 않습니다.)
3. evaluate_trials 결과에 physician_review 항목(예: "특정 약물 사용"처럼 원문에 구체 목록이 없는 기준)이 있으면, 의사가 참고할 수 있도록 ask_clarifying_questions로 관련 정보를 한 번 수집합니다.
   모집 중인 UNCERTAIN 시험이 있고 확인 질문으로 해소될 가능성이 있으면 ask_clarifying_questions를 호출합니다. 답변이 "모름" 위주이거나 라운드 한도에 도달하면 더 묻지 않습니다.
4. 마지막으로 finalize_recommendation을 호출합니다.
각 도구 호출 전에 왜 그 도구를 호출하는지 한두 문장으로 밝힙니다.
"""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("profile_patient", "환자 정보 이해 에이전트를 실행해 환자 프로파일을 구조화합니다. 결과로 핵심 정보와 누락 항목을 반환합니다.", {}, []),
    _tool("list_candidate_trials", "현재 후보 임상시험 목록(ID, 제목, 상태)과 각 시험의 평가 결과를 반환합니다.", {}, []),
    _tool(
        "search_trials",
        "ClinicalTrials.gov에서 모집 중인 임상시험을 검색해 후보에 추가합니다. 검색이 허용되지 않은 실행에서는 오류를 반환합니다.",
        {
            "condition": {"type": "string", "description": "질환명(영문 권장). 예: non-small cell lung cancer"},
            "intervention": {"type": "string", "description": "중재/약물 키워드, 없으면 빈 문자열"},
            "max_results": {"type": "integer", "description": "추가할 최대 시험 수(1-10)"},
        },
        ["condition", "intervention", "max_results"],
    ),
    _tool(
        "evaluate_trials",
        "기준 파싱 + 추론·매칭 에이전트로 지정한 시험들의 적격성을 판정합니다. 빈 목록이면 아직 평가되지 않은 모든 후보를 평가합니다.",
        {"trial_ids": {"type": "array", "items": {"type": "string"}}},
        ["trial_ids"],
    ),
    _tool("ask_clarifying_questions", "UNCERTAIN 시험의 판정 불가 항목에 대해 확인 질문 1라운드를 진행하고, 답변을 반영해 재평가한 결과를 반환합니다.", {}, []),
    _tool("finalize_recommendation", "추천 에이전트와 결과 설명 에이전트를 실행해 최종 추천 순위와 보고서를 생성합니다. 모든 평가가 끝난 뒤 마지막에 한 번 호출합니다.", {}, []),
]


class Orchestrator:
    def __init__(self, session: Session):
        self.s = session
        self.done = False

    # ------------------------------------------------------------ 도구 구현
    def _profile_patient(self, _):
        p = self.s.profile_patient()
        return {
            "age": p.age, "sex": p.sex, "primary_diagnosis": p.primary_diagnosis,
            "disease_stage": p.disease_stage, "ecog": p.ecog,
            "biomarkers": [b.description for b in p.biomarkers],
            "treatment_history": [t.description for t in p.treatment_history],
            "missing_or_ambiguous": p.missing_or_ambiguous,
        }

    def _list_candidate_trials(self, _):
        return [
            {
                "trial_id": tid, "title": t.get("title"), "status": t.get("overall_status"),
                "conditions": t.get("conditions"),
                "evaluation": self.s.matches[tid].summary if tid in self.s.matches else "미평가",
            }
            for tid, t in self.s.trials.items()
        ]

    def _search_trials(self, args):
        if not self.s.allow_search:
            raise PermissionError("이번 실행에서는 시험 검색이 허용되지 않았습니다(--search 옵션). 주어진 후보만 사용하세요.")
        n = max(1, min(int(args["max_results"]), 10))
        found = ctgov.search_studies(args["condition"], intervention=args["intervention"] or None, max_results=n)
        added = []
        for rec in found:
            if rec["trial_id"] not in self.s.trials:
                self.s.trials[rec["trial_id"]] = rec
                added.append({"trial_id": rec["trial_id"], "title": rec["title"]})
        self.s.tracer.log("orchestrator", "search", message=f"{args['condition']} -> {len(added)}건 추가")
        return {"added": added}

    def _evaluate_trials(self, args):
        if self.s.profile is None:
            self.s.profile_patient()
        ids = [t for t in args["trial_ids"] if t in self.s.trials] or [
            t for t in self.s.trials if t not in self.s.matches
        ]
        if not ids:
            return {"message": "평가할 시험이 없습니다."}
        return [compact(m) for m in self.s.match(ids)]

    def _ask_clarifying_questions(self, _):
        return self.s.clarify()

    def _finalize_recommendation(self, _):
        if not self.s.matches:
            raise RuntimeError("평가된 시험이 없습니다. evaluate_trials를 먼저 호출하세요.")
        self.s.finalize()
        self.done = True
        rec = self.s.recommendation
        return {"recommended": [r.trial_id for r in rec.recommended],
                "ranked": [(r.rank, r.trial_id, r.eligibility) for r in rec.ranked],
                "no_recommendation_reason": rec.no_recommendation_reason}

    # ------------------------------------------------------------ 루프
    def run(self) -> None:
        s = self.s
        intro = (
            f"환자 ID: {s.case.patient_id}\n"
            f"후보 시험 수: {len(s.trials)}\n"
            f"ClinicalTrials.gov 추가 검색 허용: {s.allow_search}\n"
            f"확인 질문 최대 라운드: {s.max_rounds}\n\n"
            "작업을 시작하세요."
        )
        messages: list[dict] = [{"role": "user", "content": intro}]

        for step in range(config.MAX_ORCHESTRATOR_STEPS):
            response = llm.create(
                system=SYSTEM, messages=messages, tools=TOOLS,
                effort=config.EFFORT["orchestrator"],
            )
            messages.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    s.tracer.log("orchestrator", "plan", message=block.text.strip().replace("\n", " ")[:300])

            if response.stop_reason != "tool_use":
                break

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                s.tracer.log("orchestrator", "tool_call", tool=block.name, input=block.input, message=block.name)
                handler = getattr(self, f"_{block.name}", None)
                try:
                    if handler is None:
                        raise ValueError(f"unknown tool {block.name}")
                    out = handler(block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(out, ensure_ascii=False, default=str)})
                except Exception as e:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Error: {e}", "is_error": True})
                    s.tracer.log("orchestrator", "tool_error", tool=block.name, message=repr(e))
            messages.append({"role": "user", "content": results})
            if self.done:
                break

        # 안전장치: 모델이 마무리하지 못했으면 코드가 남은 단계를 수행
        if not self.done:
            s.tracer.log("orchestrator", "fallback_finalize", message="남은 단계를 고정 순서로 마무리")
            if s.profile is None:
                s.profile_patient()
            pending = [t for t in s.trials if t not in s.matches]
            if pending:
                s.match(pending)
            if s.matches:
                s.finalize()
