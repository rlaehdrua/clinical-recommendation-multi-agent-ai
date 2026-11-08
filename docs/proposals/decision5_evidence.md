# 결정 5 기대값 변경 3건 근거 (동겸·선우 검토용)

작성: 서연 (데이터) · 2026-11-08 · 기준 `integration/dcvc` `4f81460` + PR #43 `c3cc3d5` · 재현 스크립트·쌍 목록: `docs/proposals/decision5_evidence_pairs.json`

> 데이터 검증 완료 / 임상 검수 미완료. 읽기 전용 재현이며 남의 코드는 바꾸지 않았습니다.

## 요약 (5분 판단용)

| 테스트 | 바뀌는 값 | 결론 | 근거 한 줄 |
|---|---|---|---|
| `test_cohort_board.py::test_default_data_no_confirmed_eligible_on_trials_with_unbound_rules` | 대조군 확정 적격 81 → 1 | **테스트 의도: 정상 결과 / 현황판 집계 의미: 확인 필요** | 환자를 고정하면 81쌍 전부 EXPERT_REVIEW로 이동(다른 상태로 간 쌍 0), 게이트 후 확정 적격은 전·후 0. 다만 EXPERT_REVIEW 183쌍 모두 미연결 기준이 남은 시험(9·14개)의 쌍이라 '판단 기준만 남음'이 아님 (독립 GT는 NOT_EVALUATED) |
| `test_recovery_stress.py::test_all_exhausted_pairs_are_blocked_and_blocks_are_real` | BLOCKED 19·21 → 16·24 | **정상 결과** (기대값 갱신은 확인 필요: 고정 수치가 난수에 민감) | 환자를 고정하면 BLOCKED 변화 0쌍. 변화는 재생성으로 환자가 바뀐 효과뿐이며, 들고 난 쌍은 전부 결정 5 기준이 없는 NCT07087314. underspecified 문제와 뿌리가 다름 |
| `test_sim_expert_metric.py::test_expert_slots_match_the_generator_definition_when_present` | 사본 6개 ≠ spec 8개 | **정상 결과** | 사본 대신 `cohort.spec` import. 패치 3개 함께 적용 시 시뮬레이터 테스트 5개 파일 + 위 두 테스트 146 passed |

공통 원인: 결정 5로 두 슬롯이 판단 슬롯이 되면 생성기가 그 값을 뽑지 않습니다. 그러면 난수 소비 순서가 바뀌어 같은 시드에서도 **다른 합성 환자**가 생깁니다. 두 고정 수치 테스트의 변화에는 (a) 재분류 효과와 (b) 환자 재추출 효과가 섞여 있어, 아래에서 둘을 분리했습니다.
- (a) 환자 고정: 결정 5 전 코호트에 두 기준(NCT06125405 E11 `cm.serious_organ_disease`, NCT06530004 E5 `cm.immune_onc`)만 판단 기준으로 표시(값·정답 제거)
- (b) 재생성: 결정 5 후 생성기로 다시 생성 (= 이 브랜치의 실제 상태)

## 1. cohort_board: 대조군 확정 적격 81 → 1

- 판단 기준으로 바뀐 기준: {'NCT06125405': ['E11'], 'NCT06530004': ['E5'], 'NCT07087314': []}
- 환자 고정 (a): 대조군(미연결 게이트 없음)의 결정 5 전 확정 적격 81쌍 → 결정 5 후 {'EXPERT_REVIEW': 81}
  - NCT06530004: 72쌍, 원인 기준 E5, 이동 EXPERT_REVIEW
  - NCT06125405: 9쌍, 원인 기준 E11, 이동 EXPERT_REVIEW
- 테스트 의도 '미연결 기준 쌍을 확정하지 않음': 게이트 적용 후 확정 적격 전 0 / 후 0 → **유지**
- 재생성 (b): 대조군 확정 적격은 {'NCT07087314': 1} 1쌍입니다. NCT07087314는 결정 5 기준이 없고, 재생성으로 새로 뽑힌 환자 1명이 연결 기준을 모두 통과해서 생긴 쌍입니다. 게이트가 이 쌍을 NOT_EVALUATED로 막습니다(테스트 의도대로 동작).

현황판 상태별 총계 (환자 고정, 게이트 적용 = 현황판 화면)

| 상태 | 결정 5 전 | 결정 5 후 | 차이 |
|---|---|---|---|
| CONFIRMED_INELIGIBLE | 398 | 368 | -30 |
| EXPERT_REVIEW | 0 | 183 | +183 |
| INFO_MISSING | 421 | 349 | -72 |
| NOT_EVALUATED | 81 | 0 | -81 |

상태가 바뀐 쌍 (환자 고정, 게이트 적용)

| 전 → 후 | 쌍 | 판단 |
|---|---|---|
| INFO_MISSING->EXPERT_REVIEW | 89 | 정상 (판단 기준 대기) |
| NOT_EVALUATED->EXPERT_REVIEW | 81 | 정상 (판단 기준 대기) |
| CONFIRMED_INELIGIBLE->INFO_MISSING | 17 | 정상 (아래 설명) |
| CONFIRMED_INELIGIBLE->EXPERT_REVIEW | 13 | 정상 (판단 기준 대기) |

- `CONFIRMED_INELIGIBLE → INFO_MISSING·EXPERT_REVIEW` 30쌍: 결정 5 전에는 **판단 기준이 된 E5·E11의 불충족만으로** 확정 부적격이었습니다(30쌍 모두 다른 불충족 기준 없음).
  - 판단 기준의 값으로 자동 확정하지 않는 것이 결정 5(판단 기준은 담당의 판단)의 뜻이므로 정상입니다.
  - 다른 기준이 아직 미확인이면 INFO_MISSING(17쌍), 모두 확인됐으면 EXPERT_REVIEW(13쌍)입니다.
  - 근거 기준 분포: NCT06530004 E5 → INFO_MISSING 14 · EXPERT_REVIEW 13, NCT06125405 E11 → INFO_MISSING 3
- `NOT_EVALUATED → EXPERT_REVIEW` 81쌍: 위 대조군 81쌍. 미연결 게이트보다 판단 기준이 먼저 적용되어 EXPERT_REVIEW가 됩니다. 미연결 기준이 남아 있다는 사실은 화면 `unbound_rules` 수로 계속 보입니다.
- `INFO_MISSING → EXPERT_REVIEW` 89쌍: 남은 미확인이 판단 기준뿐인 쌍입니다. 정상입니다.
- 다른 상태로 이동한 쌍(BLOCKED·확정 적격 등): 0

**확인 필요 (현황판 담당) - EXPERT_REVIEW와 미연결 기준**
- 결정 5 후 EXPERT_REVIEW 183쌍은 모두 NCT06530004(미연결 9개)·NCT06125405(미연결 14개)의 쌍입니다.
  - 판단 기준이 생기면 판정 결과가 UNCERTAIN이 되어, 미연결 게이트(ELIGIBLE일 때만 NOT_EVALUATED)가 적용되지 않습니다.
  - 그래서 physician_review gap이 먼저 EXPERT_REVIEW를 만듭니다.
- EXPERT_REVIEW는 '판단 기준 외 확인 완료'로 읽히는 상태입니다. 그러나 이 쌍들은 미연결(검수 전) 기준이 남아 있어 그 뜻이 아닙니다.
  - 독립 규칙 GT(PR #43 `src/ctrec/cohort/gt.py`)는 이런 쌍을 NOT_EVALUATED로 둡니다(범위 안 미연결 기준이 있으면 판단 대기 적격이 아님).
- 화면에는 `unbound_rules` 수가 함께 보이므로 정보는 남아 있습니다. 다만 상태별 총계(EXPERT_REVIEW 183)만 보면 과대 해석될 수 있습니다.
- 제안 (결정은 현황판 담당자, 코드 변경 없음):
  - (a) 미연결 기준이 남은 쌍은 판단 기준 여부와 무관하게 NOT_EVALUATED (GT와 같은 뜻)
  - (b) EXPERT_REVIEW 유지 + 총계를 '미연결 남음'과 '판단 기준만 남음'으로 나눠 표시
- 이 점은 결정 5 이전에도 있던 구조입니다(판단 슬롯 6개 기준). 결정 5는 해당 쌍 수를 늘렸을 뿐입니다.

대조군 확정 적격 81쌍 전체 (환자 고정)

| # | 쌍 (코호트\|환자\|시험) | 전 (대조군) | 후 (대조군) | 게이트 전 | 게이트 후 | 원인 기준 |
|---|---|---|---|---|---|---|
| 1 | `neph-null-r00|P011|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 2 | `neph-null-r01|P020|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 3 | `neph-null-r04|P002|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 4 | `neph-null-r04|P019|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 5 | `neph-k3-catalog-source-r00|P002|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 6 | `neph-k3-catalog-source-r00|P006|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 7 | `neph-k3-catalog-source-r00|P014|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 8 | `neph-k3-catalog-source-r00|P017|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 9 | `neph-k3-catalog-source-r01|P001|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 10 | `neph-k3-catalog-source-r01|P005|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 11 | `neph-k3-catalog-source-r01|P008|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 12 | `neph-k3-catalog-source-r01|P013|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 13 | `neph-k3-catalog-source-r01|P014|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 14 | `neph-k3-catalog-source-r01|P016|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 15 | `neph-k3-catalog-source-r01|P018|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 16 | `neph-k3-catalog-source-r01|P019|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 17 | `neph-k3-catalog-source-r02|P002|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 18 | `neph-k3-catalog-source-r02|P005|NCT06125405` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E11 |
| 19 | `neph-k3-catalog-source-r02|P008|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 20 | `neph-k3-catalog-source-r02|P009|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 21 | `neph-k3-catalog-source-r02|P010|NCT06125405` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E11 |
| 22 | `neph-k3-catalog-source-r02|P016|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 23 | `neph-k3-catalog-source-r02|P020|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 24 | `neph-k3-catalog-source-r03|P002|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 25 | `neph-k3-catalog-source-r03|P004|NCT06125405` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E11 |
| 26 | `neph-k3-catalog-source-r03|P007|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 27 | `neph-k3-catalog-source-r03|P008|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 28 | `neph-k3-catalog-source-r03|P010|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 29 | `neph-k3-catalog-source-r03|P012|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 30 | `neph-k3-catalog-source-r03|P013|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 31 | `neph-k3-catalog-source-r03|P017|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 32 | `neph-k3-catalog-source-r04|P005|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 33 | `neph-k3-catalog-source-r04|P009|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 34 | `neph-k3-catalog-source-r04|P011|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 35 | `neph-k3-catalog-source-r04|P012|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 36 | `neph-k3-catalog-source-r04|P015|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 37 | `neph-k3-catalog-perslot-r00|P001|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 38 | `neph-k3-catalog-perslot-r00|P005|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 39 | `neph-k3-catalog-perslot-r00|P009|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 40 | `neph-k3-catalog-perslot-r00|P011|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 41 | `neph-k3-catalog-perslot-r00|P017|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 42 | `neph-k3-catalog-perslot-r00|P019|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 43 | `neph-k3-catalog-perslot-r01|P001|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 44 | `neph-k3-catalog-perslot-r01|P004|NCT06125405` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E11 |
| 45 | `neph-k3-catalog-perslot-r01|P005|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 46 | `neph-k3-catalog-perslot-r01|P012|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 47 | `neph-k3-catalog-perslot-r01|P016|NCT06125405` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E11 |
| 48 | `neph-k3-catalog-perslot-r02|P009|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 49 | `neph-k3-catalog-perslot-r02|P010|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 50 | `neph-k3-catalog-perslot-r02|P015|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 51 | `neph-k3-catalog-perslot-r02|P016|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 52 | `neph-k3-catalog-perslot-r02|P019|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 53 | `neph-k3-catalog-perslot-r02|P020|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 54 | `neph-k3-catalog-perslot-r03|P001|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 55 | `neph-k3-catalog-perslot-r03|P004|NCT06125405` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E11 |
| 56 | `neph-k3-catalog-perslot-r03|P004|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 57 | `neph-k3-catalog-perslot-r03|P007|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 58 | `neph-k3-catalog-perslot-r03|P009|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 59 | `neph-k3-catalog-perslot-r03|P011|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 60 | `neph-k3-catalog-perslot-r03|P015|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 61 | `neph-k3-catalog-perslot-r03|P016|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 62 | `neph-k3-catalog-perslot-r03|P020|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 63 | `neph-k3-catalog-perslot-r04|P012|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 64 | `neph-k3-catalog-perslot-r04|P013|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 65 | `neph-k3-catalog-perslot-r04|P014|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 66 | `neph-k3-catalog-perslot-r04|P020|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 67 | `neph-k2-enroll1-r00|P001|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 68 | `neph-k2-enroll1-r00|P002|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 69 | `neph-k2-enroll1-r00|P010|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 70 | `neph-k2-enroll1-r00|P013|NCT06125405` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E11 |
| 71 | `neph-k2-enroll1-r01|P001|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 72 | `neph-k2-enroll1-r01|P005|NCT06125405` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E11 |
| 73 | `neph-k2-enroll1-r01|P006|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 74 | `neph-k2-enroll1-r01|P018|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 75 | `neph-k2-enroll1-r02|P016|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 76 | `neph-k2-enroll1-r03|P008|NCT06125405` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E11 |
| 77 | `neph-k2-enroll1-r03|P014|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 78 | `neph-k2-enroll1-r03|P015|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 79 | `neph-k2-enroll1-r04|P001|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 80 | `neph-k2-enroll1-r04|P014|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |
| 81 | `neph-k2-enroll1-r04|P018|NCT06530004` | CONFIRMED_ELIGIBLE | EXPERT_REVIEW | NOT_EVALUATED | EXPERT_REVIEW | E5 |

## 2. recovery_stress: BLOCKED 19·21 → 16·24

| 조건 | default BLOCKED | patient_unreachable BLOCKED |
|---|---|---|
| 결정 5 전 (기준) | 19 | 21 |
| (a) 환자 고정, 시뮬레이터 사본 그대로 | 19 | 21 |
| (a) 환자 고정 + 시뮬레이터 사본 교체 | 19 | 21 |
| (b) 재생성, 사본 그대로 (= 현재 테스트 실패 상태) | 16 | 24 |
| (b) 재생성 + 사본 교체 (= 패치 3개 적용 상태) | 16 | 24 |

- **환자 고정 (a): BLOCKED에서 빠진 쌍 0, 들어온 쌍 0** (두 시나리오 모두, 80쌍 같은 키). 결정 5의 재분류는 BLOCKED를 바꾸지 않습니다.
- 재생성 (b): 80쌍 중 같은 쌍 키는 28개입니다. 나머지는 환자의 후보 시험이 달라져 비교 대상이 없습니다.
  - default: 빠진 쌍 13 (이동 {'(쌍 없음)': 12, 'CONFIRMED_INELIGIBLE': 1}, 시험 {'NCT07087314': 13}) / 들어온 쌍 10 (이전 {'(쌍 없음)': 10}, 시험 {'NCT07087314': 10})
  - patient_unreachable: 빠진 쌍 15 (이동 {'(쌍 없음)': 13, 'CONFIRMED_INELIGIBLE': 2}, 시험 {'NCT07087314': 15}) / 들어온 쌍 18 (이전 {'(쌍 없음)': 17, 'CONFIRMED_INELIGIBLE': 1}, 시험 {'NCT07087314': 18})
- 들고 난 쌍은 **모두 NCT07087314**(결정 5 기준이 없는 시험)입니다.
  - BLOCKED 원인은 미연결·결손 기준 다수가 정보원 소진으로 끝난 것이며(`gaps`: E1~E15·I4~I12 missing), 결정 5 슬롯과 무관합니다.
  - 따라서 **'판단 기준인데 underspecified=false라 확보 시도 후 소진' 문제와 같은 뿌리가 아닙니다.** 재생성으로 다른 환자가 뽑힌 효과입니다.
- 사본 교체(C)는 BLOCKED에 영향이 없습니다. C_regen = B_regen, C_fixed = B_fixed.
  - 확정 부적격은 C_fixed에서 23 → 19(default)로 줄어듭니다. 판단 기준 값으로 확정하지 않게 되기 때문입니다(1번의 30쌍과 같은 이유).
- 확인 필요 (동겸): 이 테스트의 BLOCKED 고정 수치는 합성 환자 난수에 민감합니다.
  - 생성 설정·판단 슬롯 목록이 바뀔 때마다 기대값이 움직입니다.
  - 불변식 검사(소진 쌍이 BLOCKED 아닌 채 남지 않음, 기록 없는 BLOCKED 0, 실행 가능한 행동이 남은 BLOCKED 0)는 모든 조건에서 통과합니다.
  - 고정 수치 대신 불변식 위주로 두거나, 시드·설정 해시와 함께 기대값을 고정하는 방법을 검토할 수 있습니다.

## 3. sim_expert_metric: 판단 슬롯 사본 교체

- 패치 `docs/proposals/decision5_sim_expert_slots.patch`: `from_generator`의 사본 상수를 지우고 `cohort.spec`의 `EXPERT_SLOT_PREFIX`·`EXPERT_SLOTS`·`is_expert_slot`을 import합니다. 공개 이름(`FG.EXPERT_SLOTS` 등)은 그대로입니다.
- 패치 3개를 함께 적용한 브랜치 `feat/nsclc-eval-gt-decision5-patches`에서 시뮬레이터 테스트 5개 파일 전체(`test_sim_document_gate`, `test_sim_expert_metric`, `test_sim_generator`, `test_sim_runner`, `test_sim_stats_oracle`)와 `test_recovery_stress`, `test_cohort_board`: **146 passed**
- 전체 테스트 결과는 H 보고 참조

## 재현

```bash
# 4f81460 + #43 체크아웃에서 (파일 변경 없음, 스크립트 안에서만 EXPERT_SLOTS를 바꿔 실행)
python docs/proposals/decision5_evidence_scripts/g1_board.py .    # 현황판 전·후
python docs/proposals/decision5_evidence_scripts/g2_stress.py . out.json   # 복구 스트레스 5개 조건
```
스크립트 원본은 `docs/proposals/decision5_evidence_scripts/`에 함께 두었습니다.
