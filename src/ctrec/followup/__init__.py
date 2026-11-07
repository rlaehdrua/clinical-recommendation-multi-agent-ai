"""환자 -> 연구 담당자 -> 환자 -> 재평가 추가 정보 확인 흐름 (TRIALOG O).

- models : 요청·응답·평가 기록 스키마 (요청 상태 pending/answered/unknown/unreachable/requires_document/...)
- codec  : Session·운영 상태(OpsState)를 JSON으로 저장·복원 (실행을 끝내도 대기 요청·응답 이력이 남게)
- store  : 사례별 JSON 파일 저장소 (원자적 쓰기, 사례별 잠금, 개정 번호 확인)
- service: 흐름 전체 (초기 평가, DCV+C 제안, 요청 생성, 응답 기록, 증분·전체 재평가 비교, 충돌 보류, 대체 경로)

웹 계층(web/followup_api.py)은 권한·CSRF·실제 LLM 허가만 맡고 판정 규칙을 갖지 않습니다.
"""
