# O: 환자 → 연구진 → 환자 → 재평가 백엔드 (2026-11-07)

브랜치 `feat/o-interactive-followup`, 기준 `pr/de-5-r5-integration`(efe3354). API 계약은 `docs/dev/followup_api_contract.md`에 있습니다.

## 1. 조사 결과 (작업 전 상태)

| 필요한 것 | 기존 코드 | 빠진 것 |
|---|---|---|
| 확인 질문 | `Session.clarify` + `WebAnswerer`: 실행 스레드가 브라우저 답을 최대 30분 메모리에서 기다림 | 실행이 끝나면 질문·답이 사라짐. 연구 담당자 역할 없음 |
| 질문 고르기 | WP8 `engine_runner`: 정책 엔진이 Session 안에서 질문 선택 | 담당자 검토 단계 없음. 문서·임상 검토는 기록만 남김 |
| 사실·출처 | `FactStore` shadow, `profile_facts` (답변에서만 확인된 사실 = self_report) | 저장 안 함 (Session 메모리에만 있음) |
| 충돌 | `FactStore.relation` / `conflicts`, decision_state gap `conflict` | 해결 경로(요청) 없음 |
| 증분 재평가 | `dependency_graph`·`incremental` (진단 전용) | 실제 재평가 경로와 연결 안 됨 |
| '모름' | `LegacyAdapterPolicy` 항목 실패 → 문서 정보원 | 웹에서는 한 실행 안에서만 동작 |
| 요청 상태 | 없음 | pending/answered/unknown/... 전체 |

## 2. 추가한 것 (새 파일만, 보호 파일 pipeline.py·matcher.py 변경 없음)

- `src/ctrec/followup/models.py`: 요청 4종(`ask_patient`, `document_review`, `conflict_resolution`, `clinician_review`)과
  상태 9개. 완료 상태(문서 확인 완료·임상의 판단 완료)는 일부러 두지 않았습니다.
- `codec.py`: Session·OpsState JSON 저장·복원 (LLM 호출 없음).
  `QAPair.session_id`·`contract_id`는 `model_dump`에서 빠지는 필드라 따로 저장합니다. 빠지면 복원 뒤 답변 검증 결과가
  달라집니다. 이 차이는 전체 재평가 대조가 처음 잡았습니다.
- `store.py`: 사례마다 JSON 파일 하나. 원자적 쓰기, 사례별 잠금, 개정 번호 확인을 합니다.
- `service.py`: 흐름 전체. 담당자는 DCV+C 제안(FullDCVCPolicy)을 보고 요청을 만들고, 환자 답은 clarify와 같은 형식으로 원문에 붙어
  사실을 다시 추출합니다. 증분 재평가(영향 분석)와 전체 재평가를 대조하고, 다르면 전체 결과를 씁니다. 사실 충돌은 보류와 충돌 확인 요청으로,
  '모름'·연락 불가·문서 없음은 대체 경로로 이어집니다. 매 평가 뒤 대상 기준이 해소된 요청은 `superseded`로 닫힙니다.
  모든 사례를 묶은 Cohort C 대기열도 여기서 계산합니다.
- `web/followup_api.py`: `/api/followup/*`. 환자는 사례 쿠키, 담당자는 공유 키로 인증합니다. CSRF 확인과 실제 LLM 허가·기록·예산 경로는
  `/api/runs`와 같습니다. 담당자 헤더가 붙은 응답 요청은 403(대리 기입 금지)입니다. `app.py`는 2줄만 바꿨습니다.
- `scripts/followup_demo_server.py`(오프라인 합성 서버), `scripts/followup_demo_flow.py`(실제 HTTP, 서버 강제 종료·재시작 2회)

## 3. 검증

| 확인 | 결과 |
|---|---|
| `tests/test_followup_flow.py` (TestClient, 합성 NSCLC `sim.demo` T-A/T-B/T-C, LLM 오프라인 대역) | 10 passed |
| 실제 HTTP (`followup_demo_flow.py`, uvicorn 프로세스 kill → 재시작 2회, 호출 19번) | 통과. 재시작 전후 revision·요청·응답·평가·쌍 상태·제안이 같음 |
| 전체 pytest, 첫 커밋 fb6a0dd | 5023 passed, 9 skipped, 1 xfailed, exit 0 (기준 5015 + 새 테스트 8) |
| 전체 pytest, 최종 코드 커밋 7f03627 | 5025 passed, 9 skipped, 1 xfailed, exit 0 (기준 5015 + 새 테스트 10) |
| WP6 `python -m ctrec.sim --oracle --bootstrap 0`, 기준 efe3354 대비 | 26개 실행의 decision_records.jsonl이 모두 같음. manifest는 commit 필드만 다름 |
| Q 후보(`integration/q-final-candidate` f34e2c4) 위 시험 병합 (로컬 `local/o-on-q`) | 충돌 없음. 038d6fd 병합 트리 전체 5397 passed(5388 + 9), exit 0. 7f03627 재병합 뒤 followup + engine_runner 25 passed. 실제 HTTP 흐름 같은 결과 |

실제 HTTP 흐름의 결과 (합성 데이터):
- 처음 상태: T-A, T-B, T-C 모두 `INFO_MISSING`, 추천 순서 T-A > T-B > T-C
- 나이 "80 years old" → T-C `INFO_MISSING → VERIFICATION_PENDING` (부적격, 자기 보고라 `provisional`, 확정 아님). 추천에서 T-C가 빠지고 excluded로 감
- 혈소판 답 → T-A I3가 `UNVERIFIED`(진료 기록 필요, 자기 보고만으로 확정하지 않음)
- 같은 답 재전송 → `duplicate: true`, 개정 번호 그대로. 다른 답 → 409
- 혈색소 '모름' → 시스템이 `document_review`(lab_report, `alternative_source_after_no_answer`)를 만듦. 같은 환자 질문은 다시 만들 수 없음(409)
- EGFR 답에 "ECOG 3"이 함께 옴 → 처음 입력의 ECOG 1과 `contradictory` → 충돌 확인 요청. ECOG 기준이 있는 쌍에 `conflict`가 생기고, 제안 1순위는 `VERIFY_RECORD`(안전 필수)
- 모든 평가에서 증분 재평가 == 전체 재평가 (`match`). 어느 평가에서도 `CONFIRMED_ELIGIBLE`이 나오지 않음

## 4. 한계 (그대로 보고)

- 이 합성 시험 3개에서는 증분 재평가가 거의 모든 시험을 다시 평가합니다. 정보가 없어 LLM 경로로 간 기준은 의존 그래프에서
  "모든 항목에 의존"이라 새 문답마다 다시 평가 대상이 되기 때문입니다. 재사용이 실제로 일어나는지는 규칙 엔진으로만 판정된
  시험에서만 보이고, 비용 절감 효과는 측정하지 않았습니다.
- 오프라인 대역은 판정 품질을 보여 주지 않습니다. 답 → 사실 추출은 정규식 대역(가장 나중 언급)이고, 실제 LLM 추출은 실행하지 않았습니다(유료 승인 필요).
  실제 LLM에서는 전체 재평가 대조 때문에 매칭 비용이 두 배이고, LLM 비결정성 때문에 `mismatch_full_adopted`가 자주 나올 수 있습니다.
- 문서 확인·임상의 판단 입력 계약이 없어 `requires_document`·`requires_clinician`은 닫히지 않습니다. 그래서 환자 답만으로는 확정 상태가 나오지 않습니다.
- 한 프로세스만 지원(파일 잠금 없음), 보관 기간 정책 없음, 환자 재접속(쿠키 분실) 경로 없음, 평가는 HTTP 요청 안에서 동기로 실행
- 엔진 질문 문구는 기준 원문(영문)을 그대로 쓰는 `free_text`입니다 (`engine_runner._wording`).
- 동겸이 로컬에서 만들고 있는 API는 볼 수 없었습니다. 원격 브랜치에는 `src/ctrec/web/`을 바꾼 것이 없었습니다.

## 5. 병합

PR 기준은 `pr/de-5-r5-integration`입니다. Q 후보에는 위와 같이 충돌 없이 병합되고, 원격에 직접 병합하지 않았습니다.
