# Q 최종 통합 — 1차 게이트 점검 (2026-11-07 19:00 기준)

결론: **완료 아님.** 통합 대상 13개 중 M, N, O, P, 동겸 최종 프런트·WP6, 서연 NSCLC 검수·독립 GT의 6개는 이 시각에 커밋이 없다.
O·P·six-stage 작업 트리는 18:59에 만들어졌고, P는 19:00에 기반 병합 커밋만 있다.
이 문서는 지금 존재하는 입력만으로 만든 후보에 대한 측정이다. 빠진 입력이 들어오면 같은 절차로 다시 측정한다.

## 후보

- 브랜치 `integration/q-final-candidate` (로컬, 푸시 안 함), 작업 트리 `Desktop/workseoyeon-q`
- 기반 `local/l-k-check` 610bd49 = K-all 76dbbb2 (G + J + H + I, I+J `items_exhausted` 해소, EXPECTED_BLOCKED 17) + L d775bb6
- 원격 `integration/dcvc` 64a5011 (PR #20, #21, #25 병합) 위

## 입력별 실제 HEAD와 포함 여부

| 대상 | HEAD | 원격 병합 | Q 후보 포함 |
|---|---|---|---|
| integration/dcvc | 64a5011 | — | 포함 |
| #22 생성기·기준 | 5f7d66b | 미병합 (PR 열림) | 포함 (de-0 경유) |
| #24 WP7 보안 | e8f8af1 | 미병합 (PR 열림) | 포함 |
| G R5 P3 | d4e0d0f | PR 미개설 | 포함 (de-5 경유) |
| J 안전성 스택 de-0..de-4 | 67330d1 | PR 미개설 | 포함 |
| H 복구 테스트 | 69a22f2 | PR 미개설 | 포함 |
| I 복구 R1/R2 | 7f25d3f | PR 미개설 (동겸 결정 대기였음) | 포함 (76dbbb2 해소 포함) |
| L 검체 | d775bb6 | PR 미개설 | 포함 |
| M 환자값 잔여 | — | — | **브랜치 없음** |
| N 파이프라인 연결 | — | — | **브랜치 없음** (`audit/six-stage-pipeline`은 64a5011 그대로, 18:59 작업 시작) |
| O 상호작용 백엔드 | efe3354 (= K, 고유 커밋 0) | — | **내용 없음** |
| P DCV+C 공통 정책 | a42d50d (L+K 병합만) | — | **내용 없음** (작업 중) |
| 동겸 프런트·WP6 최종 | 원격 최신 0628cb5 (#20, 병합됨) | — | 이후 커밋 없음 |
| 서연 NSCLC 검수·독립 GT | 원격 최신 5f7d66b/ba870cb | — | **GT 없음** |

원격 PR 상태: `gh` 없음. `refs/pull/*` 확인 결과 최고 #25. #22, #24는 열린 채 미병합. G, J, H, I, L의 PR은 개설되지 않았다 (공개 API POST 401, 자격 증명 조회 금지).

## 1. 기능 합격 (6단계)

| 항목 | 판정 | 근거 |
|---|---|---|
| 실제 NSCLC 시험 선정·파싱 | 부분 | `data/nsclc/` 실제 시험 6개, 파싱 3회분 저장. 재파싱은 API 크레딧 차단 (기존 기록) |
| 환자 자가 입력 | 부분 | CLI·웹 입력 경로 존재. 웹 실제 분석은 `CTREC_WEB_PAID_ENABLED=0`이라 이번에 실행 안 함 |
| 기준별 판정·근거 | 오프라인 PASS | pytest E2E (`test_p8_e2e`, `test_de_safety_e2e`, `test_abc_safety_e2e`, `test_p9_e2e_paths`) |
| 부족 정보 탐지 | 오프라인 PASS | 같은 E2E |
| 연구 담당자 요청 | **FAIL** | 웹에 연구진 역할·요청 객체 없음 (`web/app.py` 라우트에 없음). O 범위 |
| 환자 응답 | 부분 | 웹 `/answers`는 있으나 요청 이력 영속 없음 |
| 문서·임상검토 라우팅 | 엔진 PASS, 제품 FAIL | 엔진·WP6에는 경로 있음. 사용자에게 라우팅되는 화면·API 없음 |
| 증분 재평가 | 오프라인 PASS | 엔진 테스트 |
| 추천·우선순위 | 오프라인 PASS | 엔진 테스트 |
| Inspector 감사·diff | 오프라인 PASS | WP7 테스트. 웹 재생 경로 미확인 |

실제 LLM 경로의 E2E는 실행하지 않았다 (API 키 제거 실행, 비용·크레딧 문제). 따라서 "실제 E2E PASS"는 어느 단계도 주장하지 않는다.

## 2. 안전성 합격

전체 pytest (트리 985cf98b5b, `night_pytest.sh` 가드 실행, API 키 제거): **5388 passed, 9 skipped, 1 xfailed, exit 0, 464초.** 실패 0.

안전 항목별 테스트 (전부 통과):

| 항목 | 테스트 모듈 (통과 수) |
|---|---|
| 근거 없는 CONFIRMED_ELIGIBLE 없음 | test_de_safety_e2e 25, test_abc_safety_e2e 45, test_expert_p0_integration 86 |
| 임상의 판단 자동 확정 없음 | test_expert_boundary 47, test_expert_boundary_wp6 25 |
| 무검증 clinical_record 확정 없음 | test_clinical_record_gate 49, test_clinical_record_evidence 24 |
| 타인·과거·다른 검체 오채택 | test_specimen 154 (회귀 세트 기준) |
| OR 대체 경로 | test_cohort_board 40, test_board_sim_diff 8 |
| no_required_criteria → NOT_EVALUATED | test_r5_no_required_criteria 51, test_r5_p3_contract 30, test_r5_de_combined 22, test_empty_criteria 25 |
| 복구 경로 막힘 없음 | test_recovery_r1_r2 32, test_pr20_recovery_paths 11, test_recovery_stress 2 |
| 6단계 오프라인 E2E | test_p8_e2e 19, test_p9_e2e_paths 5 |

"보류 상태가 확정 추천으로 출력되지 않음"과 "반복 질문 없음"은 위 E2E와 C2 경계 테스트가 덮지만, 이번에 별도 재현은 하지 않았다.

재현 범위: 회귀 세트와 held-out 세트. L 기준 held-out 5에서 누설 17/70, 과보류 13/70이 남아 있다 (연도만 있는 과거값, 소유격 타인, 맥락 단서만 있는 체액 WBC). "오채택 없음"은 성립하지 않는다.
I+J에서 NCT07087314 3쌍이 문서 행동 성공 후에도 `requested_documents_unresolved`로 남는 원인은 추적되지 않았다.

## 3. 제품 합격

| 항목 | 판정 |
|---|---|
| 브라우저 전체 흐름 | 미실행 (유료 분석 꺼짐, 수동 점검 필요) |
| 새로고침 후 요청 이력 유지 | **FAIL**: 실행 상태는 메모리, TTL 2시간, 진행 중 실행 이어 보기만 |
| 연구진/환자 역할 구분 | **FAIL**: 실행 소유권 쿠키만 있음 |
| 상태 변경 후 UI 동기화 | SSE 이벤트 경로 있음, 브라우저 미확인 |
| 오류·무응답 안내 | 테스트 있음, 브라우저 미확인 |
| Inspector 보고서·재생 | `scripts/render_inspector.py` 있음, 미실행 |
| README만으로 로컬 실행 | 미확인 (재설치 미실행) |

## 4. 연구 합격

`python -m ctrec.sim --oracle` 오프라인 13초, exit 0. 입력은 합성 `sim-demo-nsclc-6x3` (환자 6, 시험 3, 코호트 1, 시드 1), GT는 규칙 엔진에서 나온 순환 GT.

- 예산 20·45분: 모든 정책 confirmed_eligible 0
- 90분: oracle_upper만 1/1
- 400분: full_dcvc, legacy_greedy, cheapest_first, patient_order, random 모두 5/5 정확, confirmed_ineligible 6. full_dcvc는 행동 63회·306.5분으로 다른 정책(67회·313.5분)보다 조금 적다
- false eligible (confirmed − correct) 0, rule_gt_disagreements 0
- 문서 확인은 합성 가정(SimDocumentAuthority)이라 운영 성능이 아니다

결론: 이 표본에서 DCV+C가 베이스라인보다 낫다는 근거는 없다. 독립 GT가 없어 실제 NSCLC 정확도는 보고할 수 없다.

## 5. 제출 합격

| 항목 | 판정 |
|---|---|
| 전체 pytest | 위 2절 |
| 의존성 고정 | **FAIL**: `requirements.txt`는 하한만, lock 파일 없음 |
| 데이터 출처·라이선스 | README §6 있음. 저장소 LICENSE 파일 없음 |
| 의료적 면책 | README 상단 있음 |
| 비밀정보 | 실제 키 없음 (테스트용 가짜 키·가짜 PII만) |
| 환경 재설치 | 미실행 |
| 코드·README·발표물 일치 | 미확인 (발표물 없음) |

## 다음 단계

1. M, N, O, P, 동겸, 서연 결과가 커밋되면 이 후보에 병합하고 같은 측정을 반복한다.
2. 원격 병합 순서는 기존 계획대로: #24 → #22 → de-1..de-5 → H → I → L. 사용자 또는 팀원의 `gh auth login`이 필요하다.
