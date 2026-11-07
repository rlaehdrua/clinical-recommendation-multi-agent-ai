"""WP6 오프라인 시뮬레이터·실험 실행기 (docs/dev/wp6_experiment_design.md). 실행 코드만 둡니다.

정책은 engine/(PolicyEngine 구현, 웹과 같은 클래스 - M14)에 있고, 여기에는 환경·응답 모델·오프라인 평가기·Runner·지표만
있습니다. 정답(oracle)은 CohortEnvironment와 Runner의 지표 계산만 읽습니다. 실제 LLM·네트워크 호출은 0건입니다.
"""
