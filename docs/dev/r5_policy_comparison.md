# R5 정책 비교: P1 / P2 / P3

작성일: 2026-11-07. 측정 기준: `integration/dcvc` `b2100d8`. 상태: **P3 확정, P2 폐기** (2026-11-07, 동겸). 아래 측정표는 결정의 근거로 남긴 기록입니다. 확정 뒤 달라진 점은 §5와 §9에 있습니다.

## 1. 선택지 정의

정의 출처는 `docs/dev/e1_empty_criteria_safety.md` §3(`b2100d8`)입니다. P2 제안은 PR #17 리뷰(동겸), `CLINICIAN_REVIEW(no_required_criteria)`는 PR #18입니다.

| 안 | 정의 | 이번 측정 대상 |
|---|---|---|
| P1 | 현행 유지 | `b2100d8` 그대로 |
| P2 | 절차 외 판정 근거가 없으면 `UNCERTAIN` | P3 코드에서 `NOT_EVALUATED` 전환만 끈 스크래치 변형. `decide`·코호트 우선순위는 같음. 커밋하지 않음 |
| P3 | `NOT_EVALUATED` | 이 브랜치 (기존 R5 `50f5156` 이식 + 보완) |

P2b는 정의에 없던 보완안입니다. P2에 #18 조건을 '필수 기준 0개'에서 '절차 외 필수 기준 0개'로 넓혀 측정했습니다. 커밋하지 않았습니다.

## 2. 측정 방법

- 실제 `Session`(`profile_patient` → `match` → `finalize`)을 지나 판정·`PairStatus`·추천·`LegacyAdapterPolicy.propose`·화면 사본을 층별로 읽었습니다.
- 합성 시험, `OfflineEnvironment`, LLM 판정은 고정 대역. 62세 여성 환자. `.env` 없는 worktree에서 `ctrec.__file__`로 import 경로를 확인했습니다.
- 불일치 코드는 `tests/test_status_consistency.py`(#19)의 규칙을 그대로 썼습니다.

## 3. 결과

표기: `PairStatus` / 추천(○ 추천 목록, × 제외) / 제안 행동. **굵게**는 안전 문제입니다.

| 시나리오 | P1 (현행) | P2 | P3 (이 브랜치) |
|---|---|---|---|
| 절차 기준만, 미확인 (R5) | NOT_EVALUATED / **○ CRITERIA_MATCH** / 검토 `no_required_criteria` | INFO_MISSING / ○ 추가 정보 필요 / 검토 `no_required_criteria` | NOT_EVALUATED / × / 검토 `no_required_criteria` (측정 당시 `evaluation_failed`, §5) |
| 절차 기준 PASS | **CONFIRMED_ELIGIBLE** / **○** / 없음 | INFO_MISSING / ○ / **행동 없음** | NOT_EVALUATED / × / 검토 |
| 절차 제외 기준 PASS | **CONFIRMED_ELIGIBLE** / **○** / 없음 | INFO_MISSING / ○ / **행동 없음** | NOT_EVALUATED / × / 검토 |
| 조건부 기준 전체 N/A | **CONFIRMED_ELIGIBLE** / **○** / 없음 | INFO_MISSING / ○ / **행동 없음** | NOT_EVALUATED / × / 검토 |
| N/A + 절차 PASS | **CONFIRMED_ELIGIBLE** / **○** / 없음 | INFO_MISSING / ○ / **행동 없음** | NOT_EVALUATED / × / 검토 |
| N/A + 절차 미확인 | **CONFIRMED_ELIGIBLE** / **○** / 없음 | INFO_MISSING / ○ / **행동 없음** | NOT_EVALUATED / × / 검토 |
| 실질 기준 없는 OR (절차/절차) | NOT_EVALUATED / **○ CRITERIA_MATCH** / 검토 | INFO_MISSING / ○ / 검토 | NOT_EVALUATED / × / 검토 |
| OR1 A 절차, B 충족 | NOT_EVALUATED(A 선택) / **○** / 검토 | CONFIRMED_ELIGIBLE(B) / ○ / 없음 | CONFIRMED_ELIGIBLE(B) / ○ / 없음 |
| OR2 A 절차, B 미확인 | NOT_EVALUATED(A) / **○ CRITERIA_MATCH** / 검토 + 선택 질문 | INFO_MISSING(B) / ○ / 필수 질문 | INFO_MISSING(B) / ○ / 필수 질문 |
| OR3 A 절차, B 불충족 | NOT_EVALUATED(A) / **○ CRITERIA_MATCH** / 검토 | INFO_MISSING(A) / ○ / 검토 | NOT_EVALUATED(A) / × / 검토 |
| OR4 공통 충족, 코호트 절차 | CONFIRMED_ELIGIBLE / ○ / 없음 | 같음 | 같음 |
| OR5 A 충족, B 불충족 | CONFIRMED_ELIGIBLE / ○ / 없음 | 같음 | 같음 |
| OR6 B 충족 먼저 선언, A 절차 | CONFIRMED_ELIGIBLE / ○ / 없음 | 같음 | 같음 |
| 정상 R7 (임상 충족, 절차 미확인) | CONFIRMED_ELIGIBLE / ○ / 없음 | 같음 | 같음 |
| R1 파싱 빔, R3 원문 없음 | NOT_EVALUATED / × / 검토 | 같음 | 같음 |
| R6 절차 제외 미확인, R8 임상 미확인 | INFO_MISSING / ○ / 필수 질문 | 같음 | 같음 |
| 절차 기준 FAIL | VERIFICATION_PENDING / × / 문서 요청 | 같음 | 같음 |

#19 불일치 규칙으로 센 결과입니다.

| 안 | 불일치가 나오는 시나리오 |
|---|---|
| P1 | 6개: `match_not_confirmed`·`ranked_closed` (R5 2개, 실질 없는 OR, OR1~OR3) |
| P2 | 5개: `no_action` (절차 PASS, 절차 제외 PASS, N/A 3종) |
| P2b | 3개: `no_action` (N/A 3종). R6에 불필요한 검토가 1건 더 붙음 |
| P3 | 0개 |

**P1의 `CONFIRMED_ELIGIBLE` 5개 행은 #19 규칙에 잡히지 않습니다.** 층끼리 일치하므로 불일치 0으로 나옵니다. 그러나 환자에 대해 평가된 임상 기준 없이 확정된 상태입니다.

### 여러 시험을 함께 추천할 때

| 후보 | P1 | P2 | P3 |
|---|---|---|---|
| R5 + R7 + R8 | R5가 `CRITERIA_MATCH` 1순위, R7과 함께 추천 | R7 추천. R5는 R8보다 앞 순위 | R7 추천, R8 후순위, R5 제외 |
| R5 + R8 | R5만 추천 | R5·R8 함께 추천, R5가 1순위 | R8만 추천, R5 제외 |

P2에서는 근거가 없는 시험이 미확인 항목이 있는 시험보다 앞에 옵니다. `unresolved_burden`이 0이기 때문입니다.

## 4. 안별 안전성 영향

| 항목 | P1 | P2 | P3 |
|---|---|---|---|
| 근거 없는 확정 | **남음** | 없음 | 없음 |
| 근거 없는 시험의 추천 | **`공개 기준 충족`으로 추천** | `추가 정보 필요`로 남음. 물어볼 질문은 없음 | 제외, "부적격 아님" 표시 |
| 담당자 확인 경로 | 일부 없음 | 5개 시나리오 없음 | 모두 있음 |
| 위험 방향 | 부적격 환자에게 확정 표시 | 추천 잔류, 방치 | 볼 수 있는 시험이 추천에서 빠짐 |
| 판정 의미 | 그대로 | 맞음: 판단 보류 | 기준을 평가한 시험이 `NOT_EVALUATED`가 됨 |
| 화면 표현 | 그대로 | `추가 정보 필요`, 그러나 줄 정보가 없음 | 아래 §5 |
| 코드 범위 | 없음 | 행동 보완에 보호 파일 필요 (§6) | `matcher`·`qa_guard`·`contract_session`·`display` |

## 5. P3의 남은 비용

확정 구현에서 "기준을 평가한 시험을 평가 실패라고 부른다"는 비용은 다음과 같이 줄었습니다.

- 해소: `display.py`의 요약과 제외 사유. "판단할 기준이 절차 기준뿐이라 판정하지 못함(부적격 아님)"으로 표시됩니다.
- 해소: 웹 배지·할 일·제외 사유·진행 카드(`app.js`), 추천 없음 사유와 제외 사유(`recommender.py`), Inspector 표시와 집계, 정책 검토 사유(`decision_policy.py`·`decision_state.py`), `DecisionRecord` 전이 사유(`record_producer.py`), 보고서 LLM 지시(`explainer.py`).
- 남음: `status.py` 라벨("평가 실패(부적격 아님)", 보호 파일). 라벨은 상태값만 받아 사유 코드를 모르지만, 이 시험은 추천 줄에 나오지 않으므로 바꾸지 않았습니다.
- #18의 `no_required_criteria` 검토는 엔진 단독 경로의 방어선으로 그대로 있고, Session 경로는 `decision_policy`가 같은 코드로 검토를 냅니다.
- `NOT_EVALUATED`는 지표에서 판정 보류(`metrics.UNDECIDED`)로, 파일럿 집계에서 평가 실패(`llm_pilot`)로 함께 세입니다. 고정 사례 영향은 0건이지만 NSCLC 시험을 동결하면 값이 생길 수 있습니다.

## 6. P2 측정에서 확인한 점

PR #17 리뷰와 PR #18은 P2의 '행동 없는 INFO_MISSING'을 #18이 해결한다고 봤습니다. **절차 기준 미확인 경로에서만 맞습니다.**

- `required_count`는 `requirement == "required"`를 셉니다. 절차 기준 PASS와 N/A는 `is_waivable_logistics`가 거짓이라 `required`로 세입니다. 그래서 개수가 0이 아니고 #18 검토가 나오지 않습니다.
- 같은 이유로 P1에서 이 경로는 #14 게이트를 지나 `CONFIRMED_ELIGIBLE`이 됩니다.
- 조건을 절차 외 기준으로 넓혀도(P2b) N/A 경로는 남습니다. `fact_compare.state`가 N/A를 `MET`로 바꾸고 `CriterionState`에 `applicable`이 없어서, 정책 쪽에서 구분할 수 없습니다. 메우려면 보호 파일 `decision_state.py`를 바꿔야 합니다.
- P2는 Q&A 경계를 건드리지 않아도 됩니다. `UNCERTAIN`은 경계·재계산을 그대로 지나기 때문입니다. P3가 `qa_guard`·`contract_session`을 고친 이유가 이 차이입니다.

## 7. 이식 중 발견한 문제

`50f5156`은 `NOT_EVALUATED`에 기준별 판정을 남기는데, `guard_match`·`recompose`·`ContractSession._contract_and_guard`는 `NOT_EVALUATED`를 '평가 자체가 없음'으로 보고 건너뜁니다. 그 결과 `CTREC_SESSION=contract`에서 계약 없는 답변에 기댄 N/A·PASS가 되돌려지지 않고 계약 평가기 결과도 반영되지 않았습니다. 기본 `Session`은 이 경로를 쓰지 않습니다.

`50f5156`은 `test_not_applicable_from_answer_is_blocked`의 입력에 나이 규칙을 넣어 이 실패를 가렸습니다. 이 브랜치는 그 입력 변경을 옮기지 않고, 기준별 판정이 없는 시험만 건너뛰게 고쳤습니다. 팀 테스트는 수정 없이 통과합니다. `fix/p0-grounding-night`·`fix/p0-evidence-semantics-day`에는 이 건너뜀 문제가 남아 있습니다.

## 8. 판정 변화 (`b2100d8` 대비, P3)

| 검증 | 결과 |
|---|---|
| 고정 사례 101건 140쌍, 실제 Session 오프라인 대역 | 판정·외부 상태·코호트·추천 **변화 0건**. 단 대역 특성상 UNCERTAIN 133, INELIGIBLE 7이라 점검력이 약함 |
| 고정 26개 시험 규칙에 시드 고정 판정 재생 15,600건 | 3,901건 변화. 3,900건은 전부 N/A로 강제한 프로파일, 1건은 난수로 절차 외 규칙 5개가 모두 N/A가 된 `NCT00195949`. 모두 `ELIGIBLE` → 판단 보류. 코호트 선택 변화 0건 |
| 고정 기준표 경로별 절차 외 규칙 | 최소 5개 |

실제 시험에서는 환자에게 적용되는 절차 외 기준이 없어질 때 판정이 바뀝니다. 실제 LLM으로는 측정하지 않았습니다.

## 9. 결정 결과

| # | 질문 | 결과 |
|---|---|---|
| 1 | R5 정책 | **P3 확정, P2 폐기** |
| 2 | '평가 실패' 표현 범위 | 사유 코드별로 구분. Inspector 문구는 확정, 나머지 출력은 같은 문구로 맞춤. `status.py` 라벨은 유지 |
| 3 | P2 처리 | 해당 없음 |
| 4 | N/A만 있는 시험 | **미결.** `no_applicable_criteria`를 유지하고 동작·Inspector 문구는 R5와 같게 둠 |
| 5 | `Sex: All` 단독 | **미결** (§10) |

의견: 안전 측면에서는 지금 코드로 공백이 없는 안은 P3뿐입니다. P2의 판정 의미가 더 정확하다는 동겸의 지적은 맞습니다. 다만 P2를 고르면 `decision_state` 변경과 추천 잔류 처리를 함께 정해야 합니다.

## 10. 남은 쟁점

- `Sex: All` 단독 시험은 P1·P3 모두 `CONFIRMED_ELIGIBLE`, 추천, 행동 없음입니다. `dd6a9fd`(`fix/p0-grounding-night`)가 고치며, `50f5156` 범위가 아니라서 넣지 않았습니다.
- 나이·성별 같은 인구통계 기준만 충족한 시험은 실질 기준으로 인정되어 확정됩니다.
- R7을 확정으로 둘지는 미결입니다.
- `coverage_gaps` 7개 시험, 규칙 0개 파싱 결과의 컴파일 차단은 미결입니다 (`e1_empty_criteria_safety.md`).
