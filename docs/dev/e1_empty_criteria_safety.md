# E-1 빈 임상시험 기준 안전 메모

작성일: 2026-11-05, 갱신: 2026-11-07. 상태: 빈 기준 차단 병합(#17). R5는 **P3로 확정**(2026-11-07, 동겸)하고 P2는 폐기. R7·누락 기준은 **팀 결정 대기** (`docs/dev/r5_policy_comparison.md`).

## 1. 문제

평가할 기준이 0개인 시험을 `decide([])`가 ELIGIBLE로 처리해, 판정 근거 없이 `CONFIRMED_ELIGIBLE`이 되고
외부 표시 `CRITERIA_MATCH`로 추천 1순위에 올랐습니다.

- 도달 경로: 사전 컴파일(파서가 빈 규칙 -> 검증기·저장소가 막지 않음), `CTREC_LAZY_COMPILE=1`, 원문 기준이 없는 시험.
- 고정 기준표 26개 시험은 해당 없음: 모든 평가 경로에 절차 외 기준이 최소 5개 있습니다 (`tests/test_empty_criteria.py`).

## 2. 수정 (`src/ctrec/agents/matcher.py`)

| 입력 | 수정 전 | 수정 후 |
|---|---|---|
| `match_trial`, 규칙 0개 | ELIGIBLE | `NOT_EVALUATED` |
| `match_trial`, 적용 경로에 실질 기준 없음 | ELIGIBLE | `NOT_EVALUATED` |
| `qa_guard.recompose`, 재계산 뒤 실질 기준 없음 | ELIGIBLE | `NOT_EVALUATED` (`match_trial`과 같은 규칙) |
| `decide`, 실질 기준 없음 (`[]` 포함) | ELIGIBLE | `UNCERTAIN` (진단용 `fact_compare._rebuild`와 `match_trial`을 거치지 않는 호출의 방어선) |
| `_select_cohort`, 같은 판정의 경로 | 선언 순서 영향 | 실질 기준이 있는 경로 우선 |

`evaluation_error`에는 코드만 남깁니다. 시험 원문과 환자 정보는 넣지 않습니다.

| 코드 | 뜻 |
|---|---|
| `criteria_parse_empty` | 원문 기준 또는 구조화 조건(나이·성별)이 있는데 파싱 결과가 빔 |
| `no_source_criteria` | 원문에도 평가할 조건이 없음 |
| `no_required_criteria` | 규칙은 있지만 적용 경로의 기준이 절차 기준뿐 |
| `no_applicable_criteria` | 절차 외 기준이 있지만 이 환자에게 모두 해당 없음 |

실질 기준은 이 환자에게 적용되는 절차 외 기준입니다(`matcher.is_substantive`). 절차 기준의 통과와 조건부 기준의 '해당 없음'은
기준 단위 판정으로 남지만 시험 단위 적격의 근거가 되지 않습니다. 불충족 근거나 확인할 항목이 있으면 기존 판정을 따릅니다.

원칙: 근거가 없다는 사실은 부적격이 아닙니다. 확정을 보류하고 추천에서 빼며 "부적격 아님"으로 표시합니다. 새 상태는 없습니다.
사유 코드에 따라 검토 행동과 문구가 다릅니다.

| 코드 | 뜻 | 정책 검토 사유 | Inspector 문구 |
|---|---|---|---|
| `criteria_parse_empty`, `no_source_criteria`, 예외 | 평가 실패 | `CLINICIAN_REVIEW(evaluation_failed)` | 평가 실패 |
| `no_required_criteria`, `no_applicable_criteria` | 평가는 했으나 판단할 기준 없음 | `CLINICIAN_REVIEW(<그 코드>)` | 판정할 필수 기준 없음 · 담당자 확인 필요 |

기준별 판정이 있는 `NOT_EVALUATED`는 Q&A 경계와 계약 평가기를 그대로 거칩니다. 건너뛰는 것은 기준별 판정이 없는 평가 실패뿐입니다
(`qa_guard.evaluation_failed`).

## 3. 해결한 것과 해결하지 않는 것

### A. 절차 기준만 남은 시험 (R5) - P3 확정

- 이전: 통과 0건인데도 ELIGIBLE, `CRITERIA_MATCH`, 추천 1순위. PR #14를 더하면 코호트 상태만 `NOT_EVALUATED`였습니다.
- 현재: 판정, 외부 표시, 코호트 상태가 모두 `NOT_EVALUATED`, 추천에서 제외, `CLINICIAN_REVIEW(no_required_criteria)`.
- 절차 기준 불충족(`INELIGIBLE`)과 절차 제외 기준 미확인(`UNCERTAIN`, R6)은 P3 대상이 아닙니다. 불충족 근거나 물어볼 항목이 있으므로 기존 판정을 따릅니다.
- 코호트(OR): 절차 기준만 있는 경로가 선언 순서로 정상 경로를 밀어내지 않습니다. 다른 경로가 정보 부족이면 그 경로의
  질문으로 진행하고, 다른 경로가 불충족이면 `NOT_EVALUATED`입니다 (부적격으로 확정하지 않음).
- 동작은 `tests/test_r5_no_required_criteria.py`, 출력 경로별 일관성은 `tests/test_r5_p3_contract.py`가 고정합니다. P1·P2와의 측정 비교는 `docs/dev/r5_policy_comparison.md`에 있습니다.

### B. 임상 기준은 충족, 절차 기준만 미확인 (R7) - 미해결 정책 사례

- 조건: 절차 외 기준이 모두 통과, 절차 선정 기준만 미확인.
- 현재 동작: `CONFIRMED_ELIGIBLE`, `CRITERIA_MATCH`. 외부 요약에 "절차 기준 확인 필요"가 붙습니다.
- 이번 수정에서 바꾸지 않습니다. 정상 대조군으로 유지하지만, 확정 상태가 맞는지는 아직 결정되지 않았습니다.
  (`test_stage05.py`의 기존 테스트가 면제 규칙을 고정)
- 논점: `PairStatus` 정의의 "남은 확인 없음"과 절차 미확인이 맞는 표현인가. 바꾸려면 계약 검토가 필요합니다.

### C. 일부 기준의 `coverage_gaps`

- 조건: 파서가 기준 일부를 빠뜨렸고 검증기가 누락 후보를 감지했지만 규칙으로 복구하지 못함.
- 현재 동작: 저장을 막지 않습니다. 남은 규칙이 모두 통과하면 빠진 기준과 무관하게 ELIGIBLE이 됩니다.
- 고정 기준표: 26개 중 7개에 `coverage_gaps` 기록이 있습니다. 실제 누락인지 오탐인지는 **확인하지 않았습니다.**
- 필요한 결정: 7개 시험의 임상 검토 여부, 저장 차단 기준 (`scripts/compile_trials.py`, `trial_store.save`).

## 4. 팀 합의가 필요한 항목

R5는 합의를 마쳤습니다.

| 항목 | 결정 |
|---|---|
| R5 정책 | P3. P2 폐기 (2026-11-07, 동겸) |
| 절차 기준만 있는 시험 | Session에서도 `NOT_EVALUATED`, 추천 제외, 담당자 검토 |
| Inspector 문구 | `no_required_criteria`: "판정할 필수 기준 없음 · 담당자 확인 필요". `criteria_parse_empty`·`no_source_criteria`: 기존 "평가 실패" |

아직 남은 항목입니다.

1. `no_applicable_criteria`를 별도 코드로 둘지. 이번 PR은 후보의 코드를 유지하고 동작과 Inspector 문구를 `no_required_criteria`와 같게 했습니다.
2. 인구통계 기준(나이·성별)만 충족한 시험을 확정해도 되는지 (현재 실질 기준으로 인정)
3. R7을 확정 상태로 둘지
4. `coverage_gaps`가 남은 시험의 저장·판정 처리, 기존 7개 시험 검토
5. 규칙 0개 파싱 결과를 컴파일 단계에서 거부할지 (`criteria_parser`, `parser_validator`, `trial_store`는 이번 범위 밖)

결정이 나면 3절의 테스트와 이 문서를 함께 바꿉니다.
