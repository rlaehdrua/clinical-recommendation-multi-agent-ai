# 현황판–시뮬레이터 차이 자료 (이슈 #23 후속)

현황판 CONFIRMED_INELIGIBLE 398쌍 중 시뮬레이터가 INFO_MISSING인 170쌍과, 반대 방향 43쌍의 기준별 자료입니다.
기준 코드는 `origin/integration/dcvc` 64a5011이고, 합성 데이터만 쓰며 LLM 호출은 없습니다. 생성기 GT와 판정 코드는 바꾸지 않았습니다.

**이 자료는 차이가 생긴 코드 경로만 기록합니다. 어느 쪽이 맞는지는 판정하지 않습니다.** `mechanism` 값도 같은 뜻입니다.

## 재현

```bash
git switch test/pr20-regression-170-43    # 로컬 브랜치, push 전
PYTHONPATH=src python scripts/board_sim_diff.py --check    # 저장된 파일과 비교 (CRLF 무시), 다르면 exit 1
PYTHONPATH=src python scripts/board_sim_diff.py --out outputs/board_sim_diff    # 다른 폴더에 새로 생성
```

20개 코호트 900쌍을 약 15초에 처리합니다. 시각·경로·커밋을 넣지 않아 같은 입력이면 같은 바이트가 나옵니다.
입력 파일 해시는 `summary.json`의 `input_sha256`에 있습니다.

## 파일

| 파일 | 내용 |
|---|---|
| `pair_ids_board_CI_sim_IM.txt` | 170쌍 ID (`코호트|환자|시험`) |
| `pair_ids_board_IM_sim_CI.txt` | 43쌍 ID |
| `board_sim_pairs.csv` | 213쌍, 쌍당 1행 |
| `board_sim_criteria.csv` | 213쌍의 전체 기준 5,484행 |
| `summary.json` | 교차표, 기준별 집계, 입력 해시 |
| `recovery_residual.md` | PR #20 복구 회귀 결과와 남은 결함 |
| `recovery_policy.patch` | 복구 결함 R1의 최소 패치 (미적용) |

## 비교하는 세 시각

| 이름 | 입력 | 정보량 |
|---|---|---|
| `board` | 코호트 public, 근거 검사 없음 | 공개 값 |
| `sim_truth` | 정답 사실 전부를 문서 검증 기록으로 (`runner.rule_labels`와 같음) | 정답 전체 |
| `sim_public` | 공개 사실만 (`runner.run` 첫 상태와 같음) | 공개 값 |

170·43은 `board`와 `sim_truth`를 비교한 값이라 두 쪽의 정보량이 다릅니다. 그래서 `sim_public`을 함께 넣었습니다.

## 결과

900쌍의 교차표입니다.

| board | sim_truth | sim_public | 쌍 수 |
|---|---|---|---|
| CONFIRMED_INELIGIBLE | INFO_MISSING | INFO_MISSING | **170** |
| INFO_MISSING | CONFIRMED_INELIGIBLE | INFO_MISSING | **43** |
| CONFIRMED_INELIGIBLE | CONFIRMED_INELIGIBLE | VERIFICATION_PENDING | 208 |
| CONFIRMED_INELIGIBLE | CONFIRMED_INELIGIBLE | INFO_MISSING | 20 |
| INFO_MISSING | INFO_MISSING | INFO_MISSING | 378 |
| NOT_EVALUATED | INFO_MISSING | INFO_MISSING | 81 |

213쌍 모두 생성기 쌍 라벨은 INELIGIBLE입니다. 이 라벨은 검수 전 기준을 충족으로 간주한 값이라 독립 정답이 아닙니다.

### 170쌍: 시뮬레이터의 기간 근거 검사

- 상태 차이를 만든 기준은 175행이고, 모두 `sim_gate:time_window_unverified`입니다.
- 175행 모두 판정기는 불충족으로 답했고 생성기 기준 정답도 `no`입니다. Matcher 근거 검사가 UNKNOWN으로 되돌렸습니다.
- 경로: `evidence_directness.check`는 인용 줄 안의 날짜를 요구하는데 (`evidence_directness.py:714-717`), 시뮬레이터 `fact_line`은 날짜를 쓰지 않습니다 (`sim/evaluator.py`).
- 약물 기준 163행은 `lab:` 항목이 아니어서 `observed_at`도 없습니다. 기간은 값 안(`tacrolimus_2to3m`)에 들어 있습니다. 나머지 12행은 검사 기준입니다.

| 시험 | 기준 | 슬롯 | 행 수 |
|---|---|---|---|
| NCT07087314 | I11 | med.steroid_sparing_recent | 73 |
| NCT06125405 | I8 | med.steroid_sparing_recent | 43 |
| NCT06125405 | I9 | med.steroid_sparing_recent | 29 |
| NCT06125405 | I11 | med.steroid_sparing_recent | 12 |
| NCT07087314 | I7 | lab.hemoglobin + med.transfusion_or_gf_7d | 9 |
| NCT06125405 | I10 | med.steroid_sparing_recent | 6 |
| NCT07087314 | I9 | lab.absolute neutrophil count | 2 |
| NCT07087314 | I8 | lab.platelets | 1 |

### 43쌍: 정보량 차이

- 상태 차이를 만든 기준은 45행이고, 모두 `info_asymmetry:hidden_slot_in_truth_view`입니다. 숨긴 슬롯 값을 `sim_truth`만 봅니다.
- `sim_public`은 43쌍 모두 INFO_MISSING으로 현황판과 같습니다. 같은 정보로 비교하면 이 방향의 차이는 없습니다.

### 그 밖에 확인된 차이

- `sim_public`은 현황판 CONFIRMED_INELIGIBLE 398쌍을 하나도 확정하지 않습니다 (VERIFICATION_PENDING 208, INFO_MISSING 190). 현황판은 공개 값을 문서 검증으로 보고, 시뮬레이터 공개 사실은 `verified=False`이기 때문입니다.
- 상태가 일치하는 228쌍 중 20쌍은 불통과 기준이 겹치지 않습니다. 현황판은 공개 값, `sim_truth`는 숨긴 값으로 탈락합니다. 이 20쌍은 CSV에 없습니다.
- 기본 데이터에는 코호트 OR 경로가 없어 `trial_cohorts`, `rule_cohort`, `board_path`, `sim_matched_cohort`가 5,484행 모두 비어 있습니다. PR #25의 `issue23_generator_or_paths.patch`가 적용되지 않은 상태입니다. 기준 내부의 OR은 `binding_combine=any`로 표시됩니다.

## 결정이 필요한 것

1. 170쌍을 좁히려면 어디를 고칠지 정해야 합니다. 후보는 `fact_line`과 슬롯 사실에 날짜를 넣기, 값에 기간이 든 슬롯을 근거 검사에서 인정하기, 현황판 쪽에 같은 근거 검사 넣기입니다. 어느 쪽이든 판정 경로가 바뀌므로 이 브랜치에서는 고치지 않았습니다.
2. `sim_public`과 `board`의 확인 수준 차이(문서 검증 간주 여부)를 맞출지 정해야 합니다.

## 주요 열 (`board_sim_criteria.csv`)

| 열 | 뜻 |
|---|---|
| `differs` | `board_passes`와 `sim_passes`가 다름 (연결된 기준만) |
| `drives_status` | 쌍 상태 차이를 만든 기준 |
| `mechanism` | 차이가 생긴 코드 경로 |
| `slot_visibility` | 슬롯별 `public`, `hidden`, `absent` |
| `generator_sources`, `engine_sources` | 그 슬롯을 드러내는 정보원 |
| `board_holds`, `board_gap` | 현황판의 성립 여부와 남은 작업. `unknown`이면 현황판 UNKNOWN |
| `sim_holds`, `sim_state`, `sim_gap` | Matcher 최종 판정, 엔진 기준 상태, 남은 작업 |
| `sim_raw_judge_holds` | 근거 검사 전 판정기 답 (규칙 엔진 판정은 빈 값) |
| `sim_evidence_source`, `sim_evidence_directness`, `sim_validation_flags` | 증거 상태 |
| `sim_public_*` | 공개 사실만 넣었을 때의 값 |
| `binding_status`, `binding_used_by_judge` | 슬롯 연결 검수 상태와 판정기 사용 여부 |
| `binding_also_slots`, `binding_combine` | 기준 내부 조건 (`any` = OR) |
| `generator_rule_truth` | 생성기 기준 정답 (미연결 기준은 빈 값) |
