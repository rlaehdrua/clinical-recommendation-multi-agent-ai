# Q 최종 후보 2 상태와 P·M 의존 관계 (2026-11-07 20:35)

결론: **완료 아님.** P·M이 끝나지 않아 반영하지 않았다. 이 문서는 현재 후보 상태와 P·M 반영 절차만 정리한다.
최종 integration에는 push·merge하지 않았다. 브랜치는 모두 로컬이다.

## 브랜치

| 브랜치 | HEAD | 역할 |
|---|---|---|
| `integration/q-no-check` | 67d4d5d | 보존 (변경 없음). N+O 점검 기록: `docs/dev/q_no_check_2026_11_07.md` |
| `fix/q-o-followup-restore-criteria` | ac395e4 | O PR #35 head e5b2d31 위 **F1 별도 수정 커밋** + 실제 trial_store 회귀 테스트 |
| `fix/q-n-default-session-no-repeat` | b1eff4a | N PR #36 head a179d25 위 **O-1 반복 질문 별도 수정 커밋** |
| `integration/q-final-2` | 이 문서 커밋 | q-no-check + 위 두 수정 브랜치 병합 (최종 후보) |

두 수정 브랜치의 코드는 q-no-check에 이미 있던 수정과 같다 (`codec.py`, `question_generator.py` 차이 0줄).
따로 둔 이유: PR #35(e5b2d31)·#36(a179d25)의 원래 HEAD에는 두 수정이 없다. PR을 그대로 병합하면 수정이 빠지므로,
각 PR에 이 커밋을 올리거나 최종 통합에서 이 브랜치를 함께 병합해야 한다. q-final-2는 이미 두 커밋을 포함하므로 어느 쪽이든 중복되지 않는다.

## 수정 확인

| 수정 | q-final-2 | PR head | 회귀 테스트 (수정 전 실패 확인) |
|---|---|---|---|
| F1 follow-up 복원 시 사전 컴파일 기준표 재적재 | 있음 (`followup/codec.py` `_reload_parsed`) | #35에 없음 | `tests/test_followup_trial_store_restore.py` (O 단독, 운영 카탈로그 = 실제 trial_store, NCT04063540): 수정 없이 2 실패 / 3 통과. `tests/test_q_nsclc_followup.py` (N+O, NSCLC trial_store) 6 통과 |
| O-1 기본 Session 반복 질문 방지 | 있음 (`question_generator.screen(open_keys)`) | #36에 없음 (패치 제안 파일만) | `test_CD_failed_question_is_not_repeated[session_unknown, contract_answered]`: N head에서 xfail -> 통과, 질문 관련 테스트 94 통과 |

F1은 O의 합성 카탈로그(기준표 포함)에서는 드러나지 않는다. 위 두 회귀 테스트는 일부러 `followup_api.default_catalog()`(기준표 없는 운영 카탈로그)를 쓴다. 지우거나 합성 카탈로그로 바꾸지 말 것.

## 이번 실행 (q-final-2)

| 검사 | 결과 | 실제 LLM | 실제 브라우저 |
|---|---|---|---|
| 전체 pytest | **5435 passed, 9 skipped, 6 xfailed, exit 0** (5432 + 새 회귀 3) | 아니오 (키 제거, 대역) | 아니오 |
| N·O 관련 테스트 (followup·six_stage·q_nsclc·engine_runner) | 62 passed, 5 xfailed | 아니오 | 아니오 (TestClient) |
| 실제 NSCLC Follow-up HTTP (uvicorn 프로세스, 종료·재시작 2회, httpx 10회) | PASS: E2 None -> True, 증분 == 전체, 확정 적격 0, 재요청 409, 대기열·Inspector 일치, 다른 클라이언트 404 | 아니오 (N 시나리오 대역) | 아니오 (HTTP 클라이언트) |
| O 합성 HTTP 흐름 (`followup_demo_flow.py`) | PASS (19회, 재시작 2회) | 아니오 | 아니오 |
| WP6 `python -m ctrec.sim --oracle --bootstrap 0` | 24개 실행 decision_records 해시가 Q 기준 f34e2c4와 모두 같음, 정책 표 같음 | 아니오 | 아니오 |

### 실제 브라우저로 검증한 경로 (67d4d5d 점검, 코드는 q-final-2와 같음)

headless Chrome (Playwright 1.64.1), 외부 요청 차단, LLM은 대역:
- 기본 시험 세트 환자 화면 전체 (`scripts/capture_demo_screens.py`): 입력 -> 질문 -> 결과 -> Inspector
- NSCLC 세트 환자 화면 (NCT07680790): 질문 -> 질문 중 새로고침 복원 -> 답 -> 재평가 -> "참여 확정 아님" -> 완료 뒤 새로고침 유지
- **브라우저로 검증하지 않은 경로**: 연구진 Follow-up 화면(없음, API만), Cohort C 화면, 실제 LLM이 붙은 모든 경로

실제 LLM 호출은 이 점검 전체에서 0건이다.

## P 반영 시 (feat/p-common-engine c62f591, 20:19 커밋, 미푸시, 작업 트리에 미커밋 변경 있음)

- 텍스트 병합: q-final-2와 충돌 없음 (`git merge-tree`). 610bd49 이후 양쪽이 함께 바꾼 파일 없음
- 의미 의존: O `followup/service.py`가 `FullDCVCPolicy.propose/select`(P가 `engine/dcvc.py` 변경), `EngineRunner(session).render`(P가
  `CohortRunner`로 바꾸고 `EngineRunner` 별칭 유지)를 직접 씀. 서명은 같아 보이나 측정 필요
- P가 `tests/test_recovery_r1_r2.py`, `test_engine_runner.py`, `test_engine_baselines.py`를 바꿈: I+J 해소(EXPECTED_BLOCKED 17)와 맞는지 확인
- P-1(ILD 대체 정보원 medication_list) strict xfail 2곳: P가 고치면 XPASS로 실패하므로 표시 제거
- 반영 후 다시 실행: 전체 pytest, 위 N·O 관련 테스트, 실제 NSCLC Follow-up HTTP, O 합성 HTTP, WP6 해시 비교 (P는 WP6 결과를 바꿀 수 있으므로
  해시가 달라지면 P의 결과 보고와 대조), 브라우저 NSCLC 흐름

## M 반영 시 (브랜치 없음)

- M-1 서술 상충: `evidence_directness`, `agents/matcher._enforce_directness`(보호 파일)
- M-2 기간 조건: `tools/rule_engine.evaluate` + `matcher.match_trial` 한 줄(보호 파일)
- 고쳐지면 `test_E_conflicting_narrative_stops_confirmation`, `test_date_window_lab_is_evaluated_deterministically` XPASS -> 표시 제거
- held-out 세트 5는 코드 변경 뒤 재사용 불가 (L 기록)

## 남은 것 (변화 없음)

실제 LLM E2E, 연구진 화면, 기본 Session의 '모름' 뒤 대체 경로·새 세션 이전, 문서·임상의 입력 계약, 독립 GT·NSCLC 임상 검수,
requirements.txt cp949 설치 실패, 의존성 고정·LICENSE.
