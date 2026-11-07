# G+J 결합과 PR 준비 (2026-11-07)

기준 `origin/integration/dcvc` `64a5011`. 값은 합성이고 LLM 자리는 고정 대역입니다. 실제 API 호출은 0건입니다. 어떤 수치도 임상 성능 근거가 아닙니다.

## 결합

`pr/de-5-r5-integration`은 `pr/de-4-value-context` `67330d1`에 G `d4e0d0f`를 병합했습니다.

| 커밋 | 내용 |
|---|---|
| `06fb69e` | 병합. `recommender.py`, `web/app.py`, `app.js` 충돌은 양쪽 유지 |
| `b74815f` | 테스트 3건 계약 정리, 결합 테스트 추가. `src` 변경 없음 |
| `4fc465e` | `conservative_view`의 `no_required_criteria_policy_open` 코드를 P3 확정에 맞게 변경 |

## 실패 3건

셋 다 재현했고 판정 코드 결함이 아니라 테스트 계약 충돌입니다. 안전 판정은 바꾸지 않았고 xfail을 추가하지 않았습니다.

| 테스트 | 원인 | 처리 |
|---|---|---|
| `test_c2_session_boundary::test_every_path_uses_the_same_status[no_required]` | 정책 미정 시기의 `ELIGIBLE`, `NEEDS_REVIEW`, 추천 후보를 고정 | P3 값으로 변경. 판정·표시 `NOT_EVALUATED`, 추천 제외 |
| `test_night2_status_boundary::test_no_required_criteria_is_held_while_the_policy_stays_open` | 같음 | 이름과 기대값 변경. 사유 코드와 추천 제외 단언 추가 |
| `test_r5_no_required_criteria::test_document_verified_ineligibility_stays_confirmed` | 상태 문자열로 확정을 만듦. 게이트 뒤에는 서버 증명 필요 | `attest_declared` 사용. 문자열만으로는 확정되지 않는 대조 테스트 추가 |

## 동시 적용

`match_trial`은 값 근거, 문서, 임상의 경계를 먼저 적용하고 그 결과로 코호트와 `no_evidence_code`를 판단합니다. `tests/test_r5_de_combined.py` 22건이 이를 고정합니다.

| 사례 | 결과 |
|---|---|
| 절차 기준만, 문서 증명이나 LLM yes가 있어도 | `NOT_EVALUATED`, 추천 제외, `needs_review` |
| 절차 + 문서 필수 값 | `UNCERTAIN`, `VERIFICATION_PENDING` |
| 절차 + 측정값 아닌 문장 | `UNCERTAIN`, `INFO_MISSING` |
| 절차 + 임상의 판단 | `UNCERTAIN`, `EXPERT_REVIEW` |
| 절차 + 불충족 | `INELIGIBLE` |
| 절차 + 자가보고 허용 정상 값 | `ELIGIBLE`, `CONFIRMED_ELIGIBLE`. ContractSession은 `qa_uncontracted`로 보류 (D+E와 같음) |
| 절차 코호트와 보류된 실질 코호트 | 선언 순서와 무관하게 실질 코호트 |
| WP6 평가기 | 위와 같은 쪽 |

변이 6종(R5 끔, R5가 보류를 덮음, 문서 게이트 끔, 임상의 경계 끔, 제외 종류 변경, 코호트 우선순위 반전)을 모두 이 파일이 잡았습니다.

## 전체 테스트

| 트리 | 결과 |
|---|---|
| G+J `4fc465e` | 5015 passed, 9 skipped, 1 xfailed, exit 0 |
| H `69a22f2` | 2489 passed, 9 skipped, 3 xfailed |
| I `7f25d3f` | 2525 passed, 9 skipped |
| G+J+H+I 로컬 확인 `76dbbb2` | 5073 passed, 9 skipped, 1 xfailed, exit 0. 기대값 1건 조정 포함, 아래 참고 |

## E2E (conftest 없는 새 프로세스)

생성기 시나리오 4개의 legacy_greedy·full_dcvc와 데모, 총 9개 실행입니다.

| 항목 | `64a5011` → D+E | D+E → G+J | G+J → G+J+H+I |
|---|---|---|---|
| WP6 `result_hash` | 9개 모두 변경 | 변경 없음 | 1개 변경 |
| WP6 쌍 상태 수 | 같음 | 같음 | 같음 |
| 현황판 쌍 상태 수 | 같음 | 같음 | 같음 |
| 문서 권한 기본값 | 모듈 없음 | `NoDocumentAuthority` | `NoDocumentAuthority` |

정상 대조군은 결합 테스트와 D+E E2E가 봅니다. 현황판 `web/cohort_board.py`는 게이트 밖이라 값이 같은 것은 영향이 없다는 뜻이 아니라 닿지 않는다는 뜻입니다. FastAPI로 측정값 아닌 문장을 넣는 흐름과 브라우저 테스트는 돌리지 않았습니다.

## PR #22, #24

스택 맨 아래에 둘 다 있고 위 결과에 포함됩니다. G, I는 각각 #22, #24와 충돌이 없습니다. `pr/de-5`와 #13은 README에서 충돌합니다.

## I 결합에서 나온 사항

`items_exhausted` 1곳이 충돌합니다. `decisive_criteria`와 `basis` 조건을 함께 유지해야 합니다.

기본 응답의 BLOCKED가 20에서 17로 줄었습니다. `NCT07087314` 3쌍의 lab 3개가 문서 게이트 뒤 `unverified`로 남고, 행동이 `requested_documents_unresolved`로 끝나 INFO_MISSING에 머뭅니다. 확정 쪽 변화는 아닙니다. 이 코드를 소진으로 볼지는 동겸 님 결정이라 정책을 고치지 않았고, 로컬 확인 브랜치에서만 측정값을 17로 조정했습니다. 문서 요청이 succeeded인데 값이 unverified로 남는 이유는 끝까지 추적하지 못했습니다.

## 남은 문제

D+E 보고서의 P0와 팀 결정 항목은 그대로입니다. 이 작업이 닫은 것은 "R5 결합 테스트 3건"뿐입니다.
