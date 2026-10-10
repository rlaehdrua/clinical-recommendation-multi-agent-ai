"""전역 설정. 환경변수(.env)로 덮어쓸 수 있습니다."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
TRIALS_DIR = DATA_DIR / "trials"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = ROOT / "outputs"

MODEL = os.getenv("CTREC_MODEL", "claude-sonnet-5")
USE_FALLBACKS = os.getenv("CTREC_FALLBACKS", "1") == "1"
MAX_WORKERS = int(os.getenv("CTREC_MAX_WORKERS", "4"))

# 에이전트별 추론 강도(effort). 판정 정확도가 중요한 단계는 high,
# 추출·문장 생성 위주 단계는 medium으로 비용을 줄입니다.
EFFORT = {
    "orchestrator": "high",
    "criteria_parser": "high",
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

# 매칭 에이전트가 일부 규칙의 판정을 빠뜨렸을 때, 빠진 규칙만 다시 요청하는 횟수
MATCHER_MISSING_RETRIES = int(os.getenv("CTREC_MATCHER_RETRIES", "1"))

# 오케스트레이터 에이전트 루프 최대 반복 횟수
MAX_ORCHESTRATOR_STEPS = int(os.getenv("CTREC_MAX_STEPS", "15"))
