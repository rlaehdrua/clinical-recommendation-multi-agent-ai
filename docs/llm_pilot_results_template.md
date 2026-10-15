# 실제 LLM 파일럿 결과 기록 템플릿 (Phase 7)

실제 실행 뒤 이 양식을 복사해 채웁니다.
- 숫자는 `--save-results`로 저장한 비식별 결과 JSON(`llm_pilot_exec.execute` 출력)에서 옮깁니다.
- 환자 원문·LLM 응답 문장·API 키는 이 문서에 적지 않습니다.
- 사람 검토 결과는 사례 ID와 판정 코드, 짧은 사유만 적습니다.
- 숫자 칸은 **실제 실행 결과만** 채웁니다. 추정치나 Fake 실행 값을 넣지 마세요.

## 0. 실행 기록

| 항목 | 값 |
|---|---|
| 실험 | A_pipeline / A2a / A2b / B_question_plan |
| 실행 일시 (UTC) | |
| 실행 커밋 | |
| 계획 해시 (`plan_hash`) | |
| 확인 문구 | `RUN <실험> <해시 12자>` |
| 승인 기록 (`--approved-by`) | |
| 단가 출처·확인자 | |
| 모델 (요청 / 응답) | claude-sonnet-5 / |
| SDK 버전 | |
| 실행 전 점검 (`pilot_preflight.py`) | PASS __ / LOCKED 0 / PENDING 0 / FAIL 0 |
| 상한 | 비용 $__, 호출 __, 입력 __, 출력 __, 전체 __초 |
| Console 지출 한도 | |

## 1. 실행 요약 (결과 JSON)

| 항목 | 값 |
|---|---|
| `status` / `stop_reason` | |
| `case_status` | |
| 호출 수 (`totals.calls`) / 재시도 | |
| 입력 / 출력 / 사고 토큰 | |
| 캐시 쓰기 / 읽기 토큰 | |
| 실제 비용 `cost_usd` / 보수적 추정 `cost_estimated_usd` | |
| Console 청구 대조 | 일치 / 차이 $__ |
| 경과 시간 | |
| `frozen_store_intact` | |
| 비식별 검사 통과 | |

## 2. 사례별 (실험 A·A2)

| 사례 | 유형 | 상태 | 호출 | 실제 비용 | base 추정 대비 | 판정 누락 | 승격 | 출처 추정 | eGFR/EGFR | 정답 판정 일치 |
|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | |

## 3. 정량 지표 (`llm_pilot_cases.CRITERIA`)

| 영역 | 지표 | 통과 조건 | 결과 | 통과 |
|---|---|---|---|---|
| 추출 | `unlocated_not_unknown` | 0 | | |
| 추출 | `answer_facts_not_self_report` | 0 | | |
| 추출 | `promoted_to_document_verified` | 0 | | |
| 추출 | 핵심 항목 일치 (`agreed/checked`) | 1 | | |
| 추출 | `kidney_egfr_linked_to_EGFR` | 0 | | |
| Matcher | `not_evaluated`, `missing_after_retry` | 0 | | |
| Matcher | 정답 판정 일치 (`label_agreement/labelled_trials`) | E·G·A·B 사례 1 | | |
| 안전 | `TruncatedError`·`RefusalError` | 0 | | |
| 비용 | 보수적 비용 ≤ 상한 | | | |
| 비용 | 사례 비용 ≤ base 추정 × 2 | | | |
| 비용 | 사고 토큰이 16,000에 근접하지 않음 | | | |

## 4. 실험 B (질문 계획 비교)

| 항목 | Decision Loop | 기존 LLM 생성기 |
|---|---|---|
| 유효 비교 / 무효 | | |
| 질문 수 | | |
| 예산 안 적중 / 첫 적중 순위 | | |
| 중복 기준 / 중복 정보 키 | | |
| 불필요 대상 | | |
| 매핑 안 된 질문 | | |
| 두 계획 겹친 정보 키 | | |

## 5. 사람 검토 (승인된 검토자)

검토 결과는 사례 ID와 판정 코드만 적습니다. 코드: OK / ISSUE / N/A

| 사례 | 원문에 없는 사실 | 수치·단위 | 검사·진술 구분 | 충돌 임의 해결 | 질문 이해 가능성 | 필수 조건 우선 | 반복 질문 | 근거 인용 일치 | 추천 이유 이해 가능 | 사유 (짧게) |
|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | |

## 6. 수동 중단 기준 확인 (`llm_pilot_final_plan.md` 5절)

- [ ] 실패·중단·건너뛴 사례 없음
- [ ] 잘림·거절·판정 누락 없음
- [ ] 승격·출처 추정·eGFR/EGFR 연결 0
- [ ] 사례 비용 base 추정 2배 이내, 사고 토큰 여유
- [ ] 다음 실험(B 또는 A2) 진행 여부: 진행 / 중단 (사유)
