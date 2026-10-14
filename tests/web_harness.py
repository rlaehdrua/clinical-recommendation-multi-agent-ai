"""웹 통합 테스트용 오프라인 대역 (pytest가 수집하지 않는 도우미 모듈).

실제 FastAPI 앱·오케스트레이터 루프·Session·Matcher·추천 코드를 그대로 쓰고, LLM 호출만 대역으로 바꿉니다.
- 오케스트레이터(llm.create): 도구 호출 순서를 고정 (환자 프로파일 -> 시험 평가 -> 확인 질문 -> 최종 추천)
- 에이전트(llm.structured / llm.plain): 합성 출력. 환자 정보는 합성 데이터만 씀
API 키·네트워크 없이 재현됩니다. 실제 Claude API와의 연결은 검증하지 않습니다.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from types import SimpleNamespace

from ctrec import config, llm
from ctrec.schemas import (ClarifyingQuestion, Fact, LabValue, LLMAssessment, MatcherOutput, PatientProfile,
                           QuestionSet, RecommendationDraft)

TRIAL_IDS = ["DEMO-LUNG-001", "NCT04063540"]
SYN_TEXT = "합성 환자. 58세 남성 비소세포폐암 4기. ECOG 1. 2026-09-10 Hb 12.1 g/dL."
SYN_ANSWER = "합성답변-EGFR-양성"
SCRIPT = ["profile_patient", "evaluate_trials", "ask_clarifying_questions", "finalize_recommendation"]


def _tool_use(name: str, args: dict | None = None):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=args or {},
                                                    id=f"tu_{uuid.uuid4().hex[:8]}")],
                           stop_reason="tool_use")


class FakeLLM:
    """LLM 호출을 기록하고 합성 출력을 돌려줌. fail_on: 이 출력 형식 요청에서 예외(오류 처리 확인용)."""

    def __init__(self, fail_on: str | None = None, fail_message: str = "합성 오류", block_on: str | None = None):
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.fail_message = fail_message
        # block_on: 이 출력 형식의 첫 요청에서 release가 설정될 때까지 멈춤 (진행 중인 외부 호출 흉내, 취소 경합 확인용)
        self.block_on = block_on
        self.entered = threading.Event()
        self.release = threading.Event()

    def create(self, *, system, messages, tools=None, **k):
        llm.guard()  # 실제 llm.create와 같은 취소 확인 지점
        self.calls.append("orchestrator")
        if self.fail_on == "orchestrator":
            raise RuntimeError(self.fail_message)
        step = (len(messages) - 1) // 2
        if step < len(SCRIPT):
            return _tool_use(SCRIPT[step], {"trial_ids": []} if SCRIPT[step] == "evaluate_trials" else {})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="완료")], stop_reason="end_turn")

    def structured(self, model_cls, *, system, user, **k):
        llm.guard()
        name = model_cls.__name__
        self.calls.append(name)
        if self.fail_on == name:
            raise RuntimeError(self.fail_message)
        if self.block_on == name and not self.entered.is_set():
            self.entered.set()
            self.release.wait(20)
        if model_cls is PatientProfile:
            answered = SYN_ANSWER in user
            return PatientProfile(
                patient_id="ignored", age=58, sex="male", primary_diagnosis="non-small cell lung cancer",
                disease_stage="IV", diagnoses=[], labs=[LabValue(name="hemoglobin", value=12.1, unit="g/dL",
                                                                 date="2026-09-10", source_quote="Hb 12.1 g/dL")],
                biomarkers=[Fact(description="EGFR positive", date=None, source_quote=SYN_ANSWER)] if answered else [],
                medications=[], treatment_history=[], ecog=1, other_facts=[], missing_or_ambiguous=[])
        if model_cls is MatcherOutput:
            rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
            return MatcherOutput(assessments=[LLMAssessment(
                rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent", confidence="low",
                evidence="근거 없음", reasoning="synthetic", missing_info="확인 필요") for r in rules])
        if model_cls is QuestionSet:
            return QuestionSet(questions=[ClarifyingQuestion(
                question_id="Q1", question="EGFR 검사 결과를 아시나요?", answer_type="free_text", target=[],
                purpose="eligibility", why="합성 질문")])
        if model_cls.__name__ == "ScreeningOutput":  # 후보 추림: TRIAL_IDS 중 풀에 남은 시험만 keep
            ids = re.findall(r'<trial id="([^"]+)"', system)
            return model_cls(trials=[{"trial_id": t, "decision": "keep" if t in TRIAL_IDS else "exclude",
                                      "relevance": "high", "reason": "합성 추림"} for t in ids])
        if model_cls is RecommendationDraft:
            pid = user.split("patient_id=")[1].split(" ")[0]
            return RecommendationDraft(patient_id=pid, ranked=[], excluded=[], overall_comment="합성 요약")
        raise AssertionError(f"예상하지 않은 LLM 출력 형식: {name}")

    def plain(self, *, system, user, **k):
        llm.guard()
        self.calls.append("plain")
        return "# 합성 보고서\n\n오프라인 대역이 만든 보고서입니다."


def install(monkeypatch, tmp_path, fake: FakeLLM | None = None) -> FakeLLM:
    """LLM 대역 설치, 결과 저장 위치를 임시 폴더로, 실행 목록 초기화."""
    from ctrec.web import app as web

    fake = fake or FakeLLM()
    monkeypatch.setattr(llm, "create", fake.create)
    monkeypatch.setattr(llm, "structured", fake.structured)
    monkeypatch.setattr(llm, "plain", fake.plain)
    monkeypatch.setattr(llm, "client", lambda: (_ for _ in ()).throw(AssertionError("실제 API 클라이언트 사용 금지")))
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web, "ANSWER_TIMEOUT_S", 20)
    web.RUNS.clear()
    web.CLIENT_STARTS.clear()
    for name in ("CTREC_WEB_SAVE_OUTPUTS", "CTREC_REQUIRE_HTTPS", "CTREC_ALLOW_INSECURE_HTTP", "CTREC_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)  # 기본값(저장 안 함, 쿠키 auto)에서 시작
    return fake


def browser(app=None, base_url: str = "https://testserver", client: tuple[str, int] = ("198.51.100.20", 50000)):
    """실제 화면처럼 동작하는 HTTP 클라이언트 하나 = 브라우저 하나 (쿠키 보관, 상태 변경 요청에 확인 헤더).

    기본은 운영과 같은 HTTPS 접속(외부 주소). 로컬 개발(http + 루프백)은 base_url·client로 바꿈.
    """
    from fastapi.testclient import TestClient

    from ctrec.web import app as web
    return TestClient(app or web.app, base_url=base_url, client=client, headers={"X-CTREC-Request": "1"})


def wait_for(run, kind: str, timeout: float = 20.0) -> dict:
    """실행 이벤트 중 kind가 나올 때까지 대기 (서버 메모리의 실행 기록을 직접 확인)."""
    end = time.time() + timeout
    while time.time() < end:
        with run.lock:
            for ev in run.events:
                if ev["type"] == kind:
                    return ev
        time.sleep(0.02)
    raise AssertionError(f"이벤트 {kind}가 오지 않음: {[e['type'] for e in run.events]}")


def sse_events(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


class SDKAdapter:
    """FakeLLM을 실제 SDK 모양(messages.create / beta.messages.create)으로 감싼 기본 클라이언트 대역.

    llm.create·llm.structured를 바꾸지 않고 llm._client 자리에 넣으므로 실제 llm.create 경로(웹 기록기 포함)를 탐.
    usage=False면 응답에 사용량이 없음. requests: 받은 요청 인자(비교용, 테스트 안에서만)."""

    max_retries = 0

    def __init__(self, fake: FakeLLM | None = None, usage: bool = True, on_call=None):
        from ctrec import llm_pilot  # 단계 구분용 (시스템 프롬프트 앞부분)
        from ctrec.schemas import MatcherOutput, PatientProfile, QuestionSet, RecommendationDraft
        self._stage_of = llm_pilot.stage_of
        self._cls = {"patient_profiler": PatientProfile, "matcher": MatcherOutput, "question_generator": QuestionSet,
                     "recommender": RecommendationDraft}
        self.fake, self.usage, self._on_call = fake or FakeLLM(), usage, on_call
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _usage(self):
        return SimpleNamespace(input_tokens=100, output_tokens=20, cache_creation_input_tokens=0,
                               cache_read_input_tokens=7) if self.usage else None

    def _create(self, **kw):
        with self._lock:
            self.requests.append(kw)
        if self._on_call:
            self._on_call(kw)
        system = kw["system"]
        text_system = system[0]["text"] if isinstance(system, list) else system
        if kw.get("tools"):
            r = self.fake.create(system=system, messages=kw["messages"], tools=kw["tools"])
            r.model, r.usage = kw.get("model"), self._usage()
            return r
        user = kw["messages"][0]["content"]
        if (kw.get("output_config") or {}).get("format"):
            text = self.fake.structured(self._cls[self._stage_of(system)], system=text_system,
                                        user=user).model_dump_json()
        else:
            text = self.fake.plain(system=text_system, user=user)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=self._usage(), model=kw.get("model"))


def install_sdk(monkeypatch, tmp_path, sdk: "SDKAdapter") -> "SDKAdapter":
    """install과 같지만 LLM 함수는 그대로 두고 기본 클라이언트(llm._client)만 대역으로 (실제 llm.create 경로)."""
    from ctrec.web import app as web

    monkeypatch.setattr(llm, "_client", sdk)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web, "ANSWER_TIMEOUT_S", 20)
    web.RUNS.clear()
    web.CLIENT_STARTS.clear()
    for name in ("CTREC_WEB_SAVE_OUTPUTS", "CTREC_REQUIRE_HTTPS", "CTREC_ALLOW_INSECURE_HTTP", "CTREC_COOKIE_SECURE",
                 "CTREC_WEB_METERING"):
        monkeypatch.delenv(name, raising=False)
    return sdk
