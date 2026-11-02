"""코호트 지표 (WP1 / M3): 누락 예측을 분모에 넣는 적격성 지표 - 하위 호환 이름.

B-2 이후 구현은 공개 평가 인터페이스인 ctrec.metrics에 있습니다 (metrics.cohort_eligibility_metrics,
metrics.cohort_baseline_gate). 이 모듈은 기존 import 경로를 유지하려고 같은 객체를 다시 내보내기만 합니다.
정의와 근거(감사 P1-6)는 metrics.py의 해당 절을 보세요.
"""

from __future__ import annotations

from .metrics import MISSING_FILL, cohort_baseline_gate, cohort_eligibility_metrics, fill_missing

__all__ = ["MISSING_FILL", "cohort_baseline_gate", "cohort_eligibility_metrics", "fill_missing"]
