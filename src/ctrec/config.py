"""전역 설정. 환경변수(.env)로 덮어쓸 수 있습니다."""

import contextlib
import contextvars
import os
import sys
import types
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
TRIALS_DIR = DATA_DIR / "trials"
CACHE_DIR = DATA_DIR / "cache"
# 사전 컴파일(파싱 + 검증)한 시험 기준 고정 저장소 (git에 포함, 팀원 모두 같은 기준 목록 사용)
PARSED_DIR = DATA_DIR / "parsed"
OUTPUT_DIR = ROOT / "outputs"

MODEL = os.getenv("CTREC_MODEL", "claude-sonnet-5")
USE_FALLBACKS = os.getenv("CTREC_FALLBACKS", "1") == "1"
MAX_WORKERS = int(os.getenv("CTREC_MAX_WORKERS", "4"))

# 에이전트별 추론 강도(effort). 판정 정확도가 중요한 단계는 high,
# 추출·문장 생성 위주 단계는 medium으로 비용을 줄입니다.
EFFORT = {
    "orchestrator": "high",
    "screener": "high",
    "criteria_parser": "high",
    "parser_validator": "high",
    "patient_profiler": "medium",
    "matcher": "high",
    "question_generator": "medium",
    "recommender": "high",
    "explainer": "medium",
    "simulated_patient": "low",
}

# 확인 질문 라운드 최대 횟수, 라운드당 최대 질문 수
MAX_CLARIFY_ROUNDS = int(os.getenv("CTREC_MAX_CLARIFY_ROUNDS", "2"))
MAX_QUESTIONS_PER_ROUND = int(os.getenv("CTREC_MAX_QUESTIONS", "5"))
# 한 라운드에 질문을 몇 개 묶을지 (팀 결정 D4)
# single: 항상 1개 / batch: 라운드 한도까지 / adaptive: 서로 독립적이고 판정을 바꿀 질문만 함께, 나머지는 다음 라운드
QUESTION_POLICY = os.getenv("CTREC_QUESTION_POLICY", "adaptive")

# 보고서(설명 에이전트) 작성 여부. 평가 실험에서 비용을 줄일 때 0
WRITE_REPORT = os.getenv("CTREC_WRITE_REPORT", "1") == "1"

# 후보 추림: 전체 시험 풀에서 자세히 평가할 시험 수 상한
SCREEN_MAX_CANDIDATES = int(os.getenv("CTREC_SCREEN_MAX", "5"))

# 후보 추림 전 값싼 확정 제외(hard filter, 아키텍처 §27)에서 모집 종료 시험도 뺄지.
# 모집이 끝난 시험은 어차피 추천하지 않으므로 기본은 제외. 모집 종료 시험의 적격성까지 평가해야 하는 벤치마크는 0
HARD_FILTER_STATUS = os.getenv("CTREC_HARD_FILTER_STATUS", "1") == "1"

# 데모 모드(아키텍처 §67): 가상 데모 시험("synthetic": true, 예: DEMO-LUNG-001)은 이 모드에서만 추천에 나옵니다.
# 꺼져 있어도 적격성 판정(평가용)은 하고, 추천에서만 빼고 제외 사유를 남깁니다.
DEMO_MODE = os.getenv("CTREC_DEMO_MODE", "0") == "1"

# 매칭 에이전트가 일부 규칙의 판정을 빠뜨렸을 때, 빠진 규칙만 다시 요청하는 횟수
MATCHER_MISSING_RETRIES = int(os.getenv("CTREC_MATCHER_RETRIES", "1"))

# 고정 저장소에 없는 시험(대회 범위 밖, 예: 검색으로 새로 찾은 시험)의 처리 (팀 결정 D8 Option C)
# 0(기본): 파싱하지 않고 COMPILATION_PENDING(NOT_EVALUATED)으로 기록 -> scripts/compile_trials.py로 추가
# 1: 그 자리에서 파싱(검증 없음, 결과가 실행마다 달라질 수 있음). 데모·검색 시연용
LAZY_COMPILE = os.getenv("CTREC_LAZY_COMPILE", "0") == "1"

# 오케스트레이터 에이전트 루프 최대 반복 횟수
MAX_ORCHESTRATOR_STEPS = int(os.getenv("CTREC_MAX_STEPS", "15"))

# 판정 기준일(YYYY-MM-DD). 날짜 조건("28일 이내" 등) 판정과 재현성에 사용합니다.
# 지정하지 않으면 실행(Session) 시작 시점의 날짜로 한 번 고정합니다.
REFERENCE_DATE = os.getenv("CTREC_REFERENCE_DATE") or None


def reference_date() -> str:
    from datetime import date
    return date.fromisoformat(REFERENCE_DATE).isoformat() if REFERENCE_DATE else date.today().isoformat()


# ---------------------------------------------------------------------------
# 실행별 설정 덮어쓰기 (평가 실행 등). 모듈 속성을 전역으로 바꾸지 않고, 설정한 컨텍스트(와 copy_context로 넘긴
# 하위 스레드)에서만 config.<이름>이 덮어쓴 값을 돌려줌. 다른 실행·일반 웹 요청은 기본값 그대로
# ---------------------------------------------------------------------------

OVERRIDABLE = frozenset({"MODEL", "USE_FALLBACKS", "WRITE_REPORT", "QUESTION_POLICY", "MAX_QUESTIONS_PER_ROUND",
                         "LAZY_COMPILE"})
_overrides_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ctrec_config_overrides", default=None)


@contextlib.contextmanager
def overrides(**values):
    """with config.overrides(MODEL=...): 이 컨텍스트에서만 적용. 끝나면(예외 포함) 복구, 중첩 시 안쪽이 우선."""
    unknown = set(values) - OVERRIDABLE
    if unknown:
        raise KeyError(f"실행별로 바꿀 수 없는 설정: {sorted(unknown)}")
    token = _overrides_var.set({**(_overrides_var.get() or {}), **values})
    try:
        yield
    finally:
        _overrides_var.reset(token)


def active_overrides() -> dict:
    return dict(_overrides_var.get() or {})


class _ConfigModule(types.ModuleType):
    def __getattribute__(self, name):
        if name in OVERRIDABLE:
            ov = _overrides_var.get()
            if ov and name in ov:
                return ov[name]
        return super().__getattribute__(name)


sys.modules[__name__].__class__ = _ConfigModule
