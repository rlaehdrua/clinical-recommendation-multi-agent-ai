"""DCV+C 공통 엔진 (PROVISIONAL). 기존 판정·웹 경로와 분리된 신규 계층입니다.

- contracts : 공통 인터페이스 I-1~I-9 (팀 승인 전, PROVISIONAL)
- audit     : DecisionRecord(v2) 생성·검증·저장·로드, Inspector v2 뷰 모델
- cohort_state, cost, bundles, policy, fairness : 정책 엔진 기반 (순수 함수, LLM·네트워크 없음)

이 패키지는 기존 모듈(pipeline, web, schemas, agents.matcher)을 바꾸지 않습니다.
"""
