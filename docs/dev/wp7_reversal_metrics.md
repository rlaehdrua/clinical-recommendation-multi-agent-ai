# WP7 확정 판정 역전 지표 (`engine.reversal_metrics`)

> 계약 `dcvc.engine.contracts/0.2` **PROVISIONAL**. 지표 정의의 기준은 모듈 docstring입니다. 이 문서는 출력을 읽는 법과
> 입력 처리 규칙을 정리합니다. 이 수치는 판정 상태 변경 건수이며 **오판 건수가 아닙니다**.

## 1. 사용

```python
summary, problems = compute_for_runs(run_dirs)   # 폴더마다 따로 계산해 합침
write_metrics(summary, path, problems)           # problems는 load_problems 키로 함께 저장
```

수치는 반드시 `problems`와 함께 봅니다. 폴더 하나가 손상돼도 나머지는 계산하므로, 예외가 없다는 것이 입력이 온전하다는 뜻은 아닙니다.

## 2. 출력 항목

| 항목 | 의미 |
|---|---|
| `runs` | 정상 기록이 있는 run_id 수 (기록이 없는 폴더는 세지 않음) |
| `pairs_observed` | 기록에 실제로 나타난 환자×시험 쌍 수 (§3) |
| `confirmed_*` | 이력이 이어진 확정 역전 수 (방향별) |
| `reversals_by_path` | 위 수의 경유 유형 내역 |
| `direct_reversal_violations` | 차단 진단 `direct_confirmed_reversal` 수. 정상 지표에 포함하지 않음 |
| `unverified_reversals` | 끊김을 건너 반대 확정에 도달한 수. `unverified_by_reason`이 사유별 내역 |
| `history_gaps` | 이력 끊김 수 (원인 무관) |
| `gaps_withheld_by_violation` | `history_gaps` 중 보류 전이 진단으로 연결된 끊김. 역전에 도달하지 않은 끊김도 포함 |
| `duplicate_records_ignored` / `conflicting_steps` | 내용이 같은 중복 줄 수 / 내용이 달라 제외한 단계 수 |
| `load_problems` | `write_metrics`에 넘긴 로드 문제 (§6) |

## 3. `pairs_observed`는 분모가 아닙니다

- 전체 평가 대상 쌍 수가 **아닙니다**. `PairTransition`이 기록된 쌍만 셉니다.
- 적격성 정확도·역전율의 분모로 직접 쓰면 안 됩니다. 기록이 없는 쌍(전이가 없었거나 차단·누락된 쌍)이 빠져 있습니다.
- 관찰되지 않은 쌍을 알려면 별도 평가 계층의 대상 목록과 대조해야 합니다.

## 4. 미확인 역전의 사유

정상 역전은 이력이 이어진 경우만입니다. 끊김을 건넌 역전은 `verified=False`이고 사유는 다음 중 하나입니다.

| 사유 | 조건 |
|---|---|
| `withheld_by_violation` | 끊김이 같은 쌍의 차단 진단으로 빠짐없이 이어지고, 그중 `withheld_pair_transition`이 있음 |
| `missing_record` | 진단으로 이어지지 않는 끊김 |
| `conflicting_records` | 진단으로 이어지지 않고, 끊긴 구간에 충돌로 제외한 단계가 있음 |

- 한 구간에 사유가 여럿이면 `conflicting_records` > `missing_record` > `withheld_by_violation` 순으로 남습니다. 뒤따르는 보류 진단이 앞선 기록 누락을 가리지 않게 하기 위함입니다.
- **설명으로 인정하는 진단**: 같은 (run_id, 환자, 시험)이고, step이 끊긴 구간 안이며, before/after가 직전 after에서 끊긴 뒤 before까지 step 순서로 이어져야 합니다. 일부만 잇거나 상태가 다른 진단, 다른 쌍의 진단, 경고 진단, 기준 단위 진단은 설명이 아닙니다.
- 끊김이 `direct_confirmed_reversal`만으로 이어지면 위반 쪽에서 이미 셌으므로 미확인으로 세지 않습니다. 직접 위반과 보류 전이가 이어진 끊김은 위반은 위반대로 세고 anchor를 위반의 after로 옮긴 뒤, 그 뒤 구간만 `withheld_by_violation`으로 다룹니다.
- 진단에만 있는 전이는 이력에 넣지 않습니다. `via`에는 정상 기록에서 관찰한 상태만 있습니다.

알려진 제약
- 쌍 단위 before/after를 담은 다른 차단 진단(`transition_rejected`, `policy_transition_disagrees_with_state`, `state_change_without_execution`)은 설명으로 쓰지 않으므로 그 끊김은 `missing_record`입니다.
- 첫 기록 앞이나 마지막 기록 뒤의 보류 전이는 끊김이 생기지 않아 분류되지 않습니다. `downgraded_pending_*`은 정상 기록의 마지막 상태 기준입니다.

## 5. 계산 함수와 파일 로더의 차이

| | `compute_reversal_metrics` | `audit.parse_records` (`load_run_v2`) |
|---|---|---|
| 같은 (run_id, step) | 내용을 비교: 같으면 1건, 다르면 둘 다 제외 | 내용 비교 없이 첫 줄을 남기고 뒤 줄은 `line N: duplicate_step:S` |

로더 결과만 넘기면 충돌한 첫 줄이 정상 기록처럼 계산되고 `conflicting_steps`는 항상 0이 됩니다. 그래서 `load_run_inputs`가 `duplicate_step`으로 빠진 줄을 원본에서 다시 읽어 함께 넘기고, 판단은 `compute_reversal_metrics`가 합니다. `load_run_inputs`가 돌려주는 기록에는 중복 줄이 포함될 수 있습니다.

`audit.py`는 바꾸지 않았으므로 같은 충돌 입력을 다음과 같이 다르게 다룹니다.

| 지점 | `audit` 첫 기록 우선 | 역전 지표 |
|---|---|---|
| 충돌한 첫 줄 | 정상 기록으로 사용 | 사용하지 않음 (충돌한 줄 모두 제외) |
| 충돌 단계를 건넌 역전 | 첫 줄 내용대로 이어짐 | 끊김이 되며 `conflicting_records` 등 미확인 |
| `reversal_issues` 권고 (`reversal_without_new_evidence` 등) | 첫 줄 기준으로 계산 | 그 메시지를 그대로 `problems`에 전달 (첫 줄 기준임에 주의) |
| Inspector 표 | 첫 줄을 정상 행으로 표시 | 해당 없음 (§7) |

주의: `compute_reversal_metrics`에 `load_run_v2().records`를 직접 넘기면 이미 첫 줄만 남아 충돌을 탐지할 수 없습니다. 충돌 탐지는 `load_run_inputs` / `compute_for_runs` 경로에서만 됩니다. 또 뒤 줄이 스키마 검증에 실패하면 `invalid_record`로만 보고되고 충돌로 판정되지 않습니다(남은 첫 줄은 사용).

## 6. 입력별 처리 (`load_run_inputs`)

| 입력 상태 | 처리 | `problems` |
|---|---|---|
| `decision_records.jsonl` 있음 | 그 파일만 사용 (trace는 읽지 않음) | 줄 단위 문제 |
| 기록 파일 없음, trace에 v2 이벤트 있음 | `load_run_v2` 폴백 | `records_from_trace` |
| 위 + 진단 파일 없음 | 정상 역전은 계산. 직접 위반·`withheld_by_violation`은 계산 불가 | `issues_file_absent:direct_violations_and_withheld_not_computable` |
| 기록 파일 없음, trace 없음 또는 v2 이벤트 없음 (현재의 실제 trace 포함) | **0건 실행으로 단정하지 않음. 그 폴더의 역전 지표는 미검증** | `no_v2_records` |
| 기록 파일이 비어 있음 (trace가 있어도 폴백하지 않음) | 위와 같음 | `no_v2_records` |
| 실행 폴더가 아님, 링크, 기록 파일 읽기 실패 | 진단만 읽고 계속 | `records_unreadable` |
| 기록 줄 손상 | 그 줄만 제외 | `line N: corrupt_json` 등 |
| 진단 줄 손상 | 그 줄만 제외 | `issues line N: corrupt_json` / `not_object` / `invalid_issue:<필드>` / `duplicate_issue` |
| 진단 파일이 링크·폴더 밖, 읽기 실패 | 진단 없이 계속 | `issues_file_link` / `issues_file_unreadable` |
| 충돌 단계 있음 | 그 단계 제외 | `conflicting_steps:<수>` |

- **실제 trace와 `to_trace` 산출물의 차이**: 형식은 같은 `Tracer` 줄(`t`, `agent`, `event` + 필드)이지만, `to_trace`만 `engine / decision_record_v2` 이벤트와 `record` 필드를 남깁니다. `cli.py`, `web/app.py`, `scripts/render_inspector.py`의 실제 trace에는 이 이벤트가 없고(`to_trace` 호출 경로 없음, 2026-11-05 `integration/dcvc` `0b402c6` 기준), 에이전트별 이벤트만 있어 쌍 상태 전이를 얻을 수 없습니다. 따라서 **실제 trace만 있는 실행은 역전 지표를 계산할 수 없고 `no_v2_records`로 미검증 표시됩니다.** 디스크의 실제 trace 1건(30줄, v2 이벤트 0)으로 확인했습니다.
- **trace 대체 경로의 정보**: `to_trace`는 `DecisionRecord` 전체를 남기므로 정상 역전·경유 유형·`new_evidence`는 파일과 같습니다(테스트로 출력 전체를 대조). 차단 단계는 trace에도 없으므로 위반 분류는 진단 파일이 있어야 합니다. 이 폴백은 앞으로 trace로 남기는 실행을 위한 것입니다.
- **진단 파일이 없을 때**: `decision_records.jsonl`이 있는 실행에서는 문제로 보고하지 않습니다. `IssueWriter`는 첫 진단을 쓸 때 파일을 만들므로, 진단 없는 정상 실행도 파일이 없기 때문입니다. 다만 진단 기능 이전 실행과는 구분되지 않습니다.
- **`load_issues`는 바꾸지 않았습니다.** `IssueWriter` 이어 쓰기와 `offline_records.verify_run`은 손상 시 멈추는 쪽이 맞습니다. 지표 전용 줄 단위 리더는 `reversal_metrics._load_issues_partial`입니다.
- **개인정보**: 문제 메시지에는 줄 번호·코드·알려진 필드 이름만 남깁니다. `audit`의 `unsupported_schema:<값>`, `invalid_record:<필드>`는 파일 내용(추가 필드의 키 포함)이 섞일 수 있어 코드만 남깁니다.

## 7. 후속 과제

- **Inspector의 충돌 기록 표시** (이번 PR 범위 밖): 충돌 단계의 첫 줄이 Inspector 표에 정상 행으로 보이고, 지표에서는 그 단계가 제외됩니다. Inspector에서 충돌 단계를 구분해 표시할지는 Inspector 작업에서 결정합니다.
