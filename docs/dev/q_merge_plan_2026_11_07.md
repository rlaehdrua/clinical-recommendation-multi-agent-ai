# Q 최종 통합: PR 의존 관계·병합 계획·최종 후보 (2026-11-07 23:40)

결론: 최종 후보는 `q/final`이다(로컬, 이 문서 커밋). 전체 pytest 실패 0.
**`integration/dcvc`에는 병합·push하지 않았다.** 원격에 아무것도 올리지 않았다(아래는 제안).
실제 LLM 호출 0건. 차단 항목(맨 아래)이 남아 최종 제출 완료가 아니다.

## 1. 원격 현황 (2026-11-07 23:08 fetch, GitHub 공개 API로 base 확인)

- `integration/dcvc` = 290420d (#20, #21, #24, #25 병합)

| PR | base <- head | HEAD | 비고 |
|---|---|---|---|
| #22 | integration <- fix/generator-expert-unbound-labels | 55e78d6 | **다시 씀**: 이전 5f7d66b의 두 커밋은 patch-id 동일, 새 커밋 55e78d6(#23 OR 경로 public) 추가 |
| #26 | integration <- fix/r5-p3-no-required-criteria | d4e0d0f | #31(de-5)에 포함 |
| #27~#31 | de-0 <- de-1 <- ... <- de-5 | 67718c1 .. efe3354 | 사슬. **de-0(49770e0)은 PR 없음**, 내용은 옛 #22뿐 |
| #32 -> #33 | integration <- H <- I | 69a22f2, 7f25d3f | I x J 충돌 해소(9690a89)와 고정값(76dbbb2)은 **어느 PR에도 없음** |
| #34 | de-4 <- L | d775bb6 | |
| #35 / #36 / #40 | de-5 <- O / N / M | e5b2d31 / a179d25 / 751ec5b | |
| #37 | integration <- feat/wp6-expert-metric | 666a60b | Q에 처음 포함. P와 충돌(sim/runner.py import) |
| #38 / #39 | O <- web / 문서·임상의 완료 | 2d19c3b (이전 68287da + 2) / 6c2d587 | 서로를 모름 -> 충돌 |
| P | **PR 없음** (`pr/p-common-engine` 9dec3a9, 원격 브랜치만) | | 기준 610bd49가 원격에 없어서 열지 못함 |

## 2. 원격 구조 (만든 로컬 브랜치)

| 브랜치 | 내용 |
|---|---|
| `q/upstream` c1f3f53 | integration/dcvc + 모든 PR head (+ de-0, P)를 의존 순서로 병합. 자동 병합만, PR끼리 충돌한 곳 4개는 따로 표시한 해소 커밋 |
| `q/final` (이 문서) | q/upstream + Q 자체 변경만. `git diff q/upstream q/final` = Q 변경 |

`q/upstream` 병합 순서와 결과:

1. #22 55e78d6: 자동
2. de-0 49770e0: 충돌 4파일. #22가 같은 줄을 더 고쳐서 생긴 것으로, #22 쪽을 유지 → **내용 변화 0** (ba183fb)
3. #27 -> #28 -> #29 -> #30 -> #31: 자동. #26은 #31에 이미 포함
   - #26을 사슬 앞에 먼저 병합하면 #28에서 recommender.py가 충돌한다. 원래 계보처럼 de-5가 #26을 데려와야 한다.
4. #34 L, #32 H: 자동
5. #33 I: cohort_state.py 충돌. 측정했던 9690a89 해소를 그대로 적용(양쪽 파일이 9690a89 때와 같음을 확인, 7cad074). 76dbbb2 고정값을 cherry-pick (5ce16ba)
6. #40 M, #35 O, #36 N, P: 자동
7. #39: 자동
8. #38 2d19c3b: #39와 충돌 (8e18a18)
   - `require_staff`: #39의 단일 역할 검사(`_single_role`)를 먼저 하고, 그 뒤 #38의 루프백 데모 담당자 예외. 데모 예외는 검증자·임상의 권한을 주지 않는다.
   - docstring·데모 서버 옵션은 둘 다 유지한다.
9. #37: P와 import 충돌 (c1f3f53). 병합본이 실제로 쓰는 import의 합집합

**`q/upstream` 단독 전체 pytest: 17 failed, 5603 passed** — PR들만 병합하면 integration이 빨갛다. 실패 17건은 모두 Q 자체 변경이 고치는 것이다.
- O `test_followup_flow` 6, #39 테스트 6: P 근거 경로 뒤 검사 항목이 환자 질문이 아님
- #38 테스트 1: 같은 원인
- N strict xfail 2: M이 해결해서 XPASS
- recovery_stress 2: M-2

## 3. Q 자체 변경 (`q/upstream..q/final`, PR에 없음)

| 항목 | 커밋 | 파일 |
|---|---|---|
| F1 복원 기준표 재적재 | ac395e4 (+4d8a28b 원형) | followup/codec.py, tests/test_followup_trial_store_restore.py |
| 반복 질문 방지 (O-1) | b1eff4a | agents/question_generator.py, tests/test_six_stage_e2e.py |
| O -> P 공통 엔진 연결 | 40e0cb2, e6c165e, 32fd484(#39 위 재적용) | followup/service.py, scripts/followup_demo_flow.py, tests/test_followup_flow.py, tests/test_q_nsclc_followup.py |
| 문서 기록 값 투영 | 1d46f93 | followup/service.py, tests/test_document_clinician_completion.py |
| Insight 수정 | ed5a91c (#38 병합 안) | web/followup_insight.py |
| M 후속 | b4a07e9 | tests/test_six_stage_e2e.py, tests/test_recovery_stress.py |
| 완료 상태 표기·검증 데모 | a840a5f | web/static/staff.js, scripts/followup_verification_flow.py |
| **단일 판정 상태 계약** (이번) | d113f66 | evidence_basis.py, conservative_view.py, followup/service.py, web/followup_api.py, tests/test_q_state_contract.py |
| **담당자 화면 완료 안내** (이번) | 00f06ea | web/static/index.html, web/static/staff.js |
| **#38 새 테스트 P 근거 경로 맞춤** (이번) | 50bf899 | tests/test_followup_web.py |

모든 수정이 q/final에 들어 있음을 확인했다(문자열 대조 11항목, 옛 호출 `_open_actions`·직접 `apply_outcome` 0건).

### 이번 수정: 확정 적격 vs 추천 표시 불일치 (d113f66)

재현 (합성 시험 T-A·T-D, 합성 권한): 결함은 세 가지였다. 근본 원인을 고쳤고 화면에서 덮어쓰지 않았다.

1. **문서 해제 뒤 상태 표기가 남음**: 문서 확인으로 해제된 T-A가 쌍 CONFIRMED_ELIGIBLE, 판정 ELIGIBLE인데 match_status는 POTENTIAL_MATCH였다.
   - 원인: `evidence_basis.reaggregate`가 `model_copy`로 다시 집계해서 `match_status` 채우기(검증기)가 다시 돌지 않았다.
   - 수정: `status.external_status`로 다시 계산한다.
2. **임상의 판단 확정이 추천에 반영되지 않음**: 임상의 판단으로 T-D가 쌍 CONFIRMED_ELIGIBLE인데 추천은 NEEDS_REVIEW였다.
   - 원인: `conservative_view._judged_by_llm`이 인정된 임상의 판단도 LLM 판정으로 보류했다.
   - 수정: **지금 권한이 인정하는** 임상의 판단(`expert_boundary.recognized`)만 예외로 둔다. 권한 없음·변경·사실 변경으로 무효이면 그대로 보류한다.
3. **반대 방향(리뷰 C1)**: 서명 비밀 변경·검증 모드 해제 뒤 쌍은 내려가는데, 저장된 추천 화면(담당자·**환자**)은 '공개 기준 충족', 사례 목록은 확정 2를 그대로 보였다.
   - 원인: `refresh_pairs`가 쌍만 다시 계산했다.
   - 수정: 지금 권한으로 해제·판단을 다시 확인하고, 추천을 finalize와 같은 결정적 경로로 다시 만든다(LLM 없음). 사례 목록도 `refresh_pairs`를 쓴다.
- 검증: `tests/test_q_state_contract.py` 3건이 수정 전 모두 실패하고 수정 뒤 통과한다.
- evidence_routing·문서/임상의 권한·운영 503은 바꾸지 않았다.

## 4. 검증 (q/final 50bf899)

| 검사 | 결과 | 실제 LLM | 실제 브라우저 |
|---|---|---|---|
| 전체 pytest | **5638 passed, 9 skipped, 5 xfailed, exit 0** | 아니오 | 아니오 |
| q/upstream 단독 (비교) | 17 failed, 5603 passed | 아니오 | 아니오 |
| NSCLC Follow-up HTTP (재시작 2) | PASS: 증분=전체, 확정 적격 0, 재요청 409, 대기열·Inspector 일치, 타인 404 | 아니오 | 아니오 |
| 문서 검증 HTTP (`followup_verification_flow.py`) | 28 checks, 27 HTTP, 재시작 2 (다른 서명 비밀 -> 확정 안 보임, 모드 없음 -> 503) | 아니오 | 아니오 |
| O 데모 HTTP | PASS (22회) | 아니오 | 아니오 |
| WP6 | q-final-3 대비: 데모 decision_records 24/24 동일, 정책 표 공유 열 28/28 동일 (#37 열 3개 추가), 생성기 코호트 최종 쌍 상태 28/28 동일 | 아니오 | 아니오 |
| 브라우저 (#38 화면) | 환자·담당자 화면 PASS. 검증자·임상의는 화면이 없어 API로 진행. 재평가 뒤 담당자 화면에서 쌍 '적격 확정' = 추천 '공개 기준 충족' 2건, 완료 표기 '서류 검증됨(검증자)'·'의료진 판단됨', 새 안내문. 페이지 오류 0, 외부 요청 0 | 아니오 | **예** (headless Chrome) |

## 5. GitHub 제안 (승인 뒤 실행)

권장안: 두 단계 PR (검토 범위가 분명하고 기존 PR 이력이 보존됨)

| 순서 | 올릴 브랜치 | PR base <- head | 검토 대상 |
|---|---|---|---|
| 1 | `q/upstream` | `integration/dcvc` <- `q/upstream` | 기존 PR 합본. 새 코드는 해소 커밋 4개(ba183fb 내용 0, 7cad074+5ce16ba I x J, 8e18a18 #38 x #39, c1f3f53 #37 x P)와 PR이 없는 P(병합 커밋 eebd58d)뿐. 병합되면 #22·#26~#40이 조상이 되어 GitHub에서 함께 병합 처리됨 |
| 2 | `q/final` | `q/upstream` <- `q/final` | Q 자체 변경만 (표 3) |

- **두 PR은 같이 병합해야 한다.** 1만 병합하면 integration 테스트 17건이 실패한다. 2의 base를 1이 병합된 뒤 integration/dcvc로 바꾸거나, 2를 먼저 1에 병합한다.
- 개별 PR을 그대로 병합하려면 이 순서와 해소가 필요하다:
  1. #22
  2. #27 base를 integration으로 바꾸고(de-0 대신), #22와의 충돌은 #22 쪽 유지
  3. #28 -> #31 (#26 자동 포함)
  4. #34 -> #32
  5. #33 (I x J 해소 커밋 필요)
  6. #40·#35·#36
  7. P(새 PR 필요)
  8. #39
  9. #38 (#39와 충돌)
  10. #37 (P와 충돌)
  11. Q 변경
  - 그동안 integration은 9단계 이후에도 Q 변경 전까지 실패 상태다.
- 기존 작업자 브랜치·PR head는 바꾸지 않았다. Q 수정이 기존 PR에도 필요하면 별도 커밋으로 올릴 것(F1·O-1 수정 브랜치는 로컬에 이미 있음).

## 6. 최종 제출을 막는 잔여 문제

**차단 (별도 표시)**
- **M P0 안전성**:
  - 독립 held-out 6에서 서술 상충 8/15 미탐지, 전해 들은 값 3/10 오채택 (검사값 부정 7/80, 정상 보류 12/80).
  - 안전 경계 제안(범위 표현이 문턱을 가로지를 때만 보류, 질환어 양극성 + 한쪽 인용이면 임상 검토)은 새 held-out으로 측정한 뒤에 넣어야 한다.
- **서연 독립 GT·NSCLC 임상 검수 미완료**: 정확도 지표를 보고할 수 없다.
- 실제 LLM E2E 0건 (비용 승인 필요).

**필수 기능 미완**
- 검증자·임상의 화면 없음 (API만).
- 기본 웹 Session(`/api/runs`)의 '모름' 뒤 대체 경로와 새 세션 이전 (xfail 2).
- P-1 대체 정보원 오류 (xfail 2), NCT06216301 국가 조건 거짓 부적격 (xfail 1), 번들링 성능 역전 (800분 no_bundles 11.17 > dcvc 8.17).
- 리뷰 C2: 합성 사례 판별이 카탈로그 이름 거부 목록이다.
- P PR 없음. I x J 해소·고정값이 PR에 없음 (q/upstream 해소 커밋으로만 존재).
- requirements.txt 한국어 Windows 설치 실패(cp949), 의존성 고정·LICENSE 없음.
