# PR #20 잔여 복구 결함 R1·R2 수정 (동겸 검토용)

기준: `origin/integration/dcvc` 64a5011 위의 `test/pr20-regression-170-43` 69a22f2.
합성 데이터만 사용했고 LLM 호출은 없습니다. `pipeline.py`를 포함한 보호 파일은 고치지 않았습니다.
재현 입력과 호출 경로는 `docs/review/board_sim_diff/recovery_residual.md`에 있습니다.

## R1: 문서 상태 계약

`policy.py`에서 항목의 정보원 상태를 두 함수로 모았습니다. `_convert`와 `apply_outcome`은 이 값으로만 대체를 판단합니다.

| 상태 | 위치 |
|---|---|
| 실패한 정보원 | `ops.failed_sources` |
| 실패한 (정보원, 항목) + 사용한 문서 | `spent_sources(ops, item)` |
| 남은 대체 문서 | `remaining_documents(source, fields, info_key, item, ops)` |

소진 판단은 `_cut_off_items` 하나입니다. 다음 계획이 소진 보류(`sources_exhausted`, `item_sources_exhausted`)를 내는 항목만 소진합니다.

H의 임시 패치(`board_sim_diff/recovery_policy.patch`)와 다른 점은 네 가지입니다.

1. 패치의 `and failed` 조건을 없앴습니다. 연락 불가 없이 환자 "모름" 뒤 문서 둘이 gap을 남기는 경로도 정책은 소진 보류를 내는데, 패치로는 쌍이 열린 채 남습니다.
2. 같은 항목에 실행할 행동이 남아 있으면 소진하지 않습니다 (`_cut_off_items`의 `live`).
3. 쌍 소진은 미검증 불충족 근거의 문서 확인(`basis:*`)까지 끝나야 합니다 (`cohort_state.items_exhausted`). 전에는 이 요청이 남아 있어도 BLOCKED가 되어 `full_dcvc` 후보에서 빠질 수 있었습니다. 시뮬레이터에서는 재현되지 않았고, 합성 테스트로 고정했습니다.
4. 정보원 실패 분기의 대체 후보에서도 사용한 문서를 뺍니다.

바꾸지 않은 정책: 실패 없이 요청 문서만 다 쓴 `requested_documents_unresolved`는 소진이 아닙니다. 웹의 문서 입력은 문서 검증으로 올라가지 않으므로(`engine_runner` W-3), 이것을 소진으로 세면 코디네이터가 값을 넣은 쌍이 BLOCKED가 됩니다.

### 측정

시뮬레이터 4개 코호트 80쌍, legacy_greedy, seed 0, horizon 30. 잔류는 모든 항목이 소진 보류인데 BLOCKED가 아닌 쌍입니다.

| 응답 설정 | BLOCKED 전 | BLOCKED 후 | 잔류 전 | 잔류 후 | `result_hash` |
|---|---|---|---|---|---|
| 기본 응답 | 17 | 20 | 3 | 0 | 코호트 4개 중 2개 변경 |
| 환자 연락 불가 100% | 0 | 22 | 14 + VP 8 | 0 | 4개 변경 |
| 문서 연락 불가 100% | 0 | 0 | 0 | 0 | 변경 없음 |
| 전체 연락 불가 100% | 29 | 29 | 0 | 0 | 변경 없음 |

H는 INFO_MISSING만 세어 14로 적었습니다. VERIFICATION_PENDING까지 세면 수정 전 잔류는 22쌍입니다.

모든 `apply_outcome` 직후 불변식도 확인했습니다 (legacy_greedy·full_dcvc, 4개 응답 설정). BLOCKED로 넘어가는 순간 그 시험에 아직 실행하지 않은 환자 질문·문서 요청이 남은 경우는 0건이고, 정책 전이로 기록되지 않은 BLOCKED 전이도 0건입니다.

### VERIFICATION_PENDING -> BLOCKED 8쌍의 근거

`recovery_residual.md`는 `OBTAINABLE_GAPS`의 `unverified`와 연결해 적었지만, 실제 원인은 다릅니다. 8쌍 모두 다음과 같습니다.

- 시험 NCT07087314, 코호트 neph-null-r00 1쌍, r01 1쌍, r02 2쌍, r03 4쌍. `eligibility=INELIGIBLE`, `mismatch=provisional`, 규칙 GT는 ineligible.
- VP였던 이유: 불충족 근거가 나이 기준 I1 하나이고, 그 근거가 문서로 검증되지 않았습니다 (`exclusion_unverified`). 남은 gap은 전부 `missing`이고 `unverified` gap은 없습니다.
- 남은 항목 19~20개는 모두 소진입니다. 환자는 연락 불가, 항목마다 실행할 문서가 남지 않았고, 정책이 낸 행동도 소진 보류뿐입니다.
- `basis:age` 문서 확인도 끝났습니다. BLOCKED 조건에 근거 문서 소진을 넣은 뒤에도 8쌍이 그대로 BLOCKED입니다.

판단: 올바른 결과입니다. 시도할 정보원이 없으므로 대기 상태로 두면 할 일이 있는 것처럼 보입니다. `derive_pair_status`의 소진 규칙(VP도 BLOCKED로)과 같고, 확정이 아니므로 잘못된 CONFIRMED는 생기지 않습니다. `pending_kinds`에 `exclusion_unverified`와 `sources_exhausted`가 함께 남아 사유도 보존됩니다.

## R2: Session 루프

`Session.uncertain`은 `acquisition`의 열린 항목만 봅니다. 그래서 마지막 열린 항목을 묻고 나면 정책에 남은 문서 요청이 실행되지 않고, `clarify`도 건너뜁니다.

`web/engine_runner.py`의 `activate`가 실행 동안 그 Session 인스턴스의 `uncertain`만 `recovery_uncertain`으로 바꿉니다. `pipeline.py`는 고치지 않습니다.

- 이어 가는 행동은 문서 요청과 바꿔 묻기뿐입니다. 일반 환자 질문으로 루프를 넓히지 않습니다.
- 라운드·질문·작업시간 한도는 그대로입니다. 같은 행동(`_repeat_key`, 시뮬레이터와 같은 기준)은 한 실행에서 다시 하지 않습니다.
- 한도로 실행하지 못한 행동은 `EngineRunner.unexecuted`, trace `engine/actions_not_executed`, 보고서 끝 절에 남습니다. 사유 코드는 `round_limit`, `question_budget`, `over_budget`, `session_stopped`입니다. 쌍은 BLOCKED가 되지 않습니다.
- 라운드가 있으면 잘린 행동은 다음 라운드 계획에 그대로 다시 나옵니다.
- `ContractSession.clarify`는 `super().uncertain()`을 직접 불러 인스턴스 교체를 거치지 않습니다. `engine_recovery_pending` 훅 한 줄을 더했습니다. `contract_session.py`는 보호 파일이 아닙니다.
- 바꿔 묻기가 첫 질문과 같은 문장으로 나가던 문제도 `_wording`에서 고쳤습니다.

## PR #20 소유 변경과의 충돌

병합 시뮬레이션(`git merge-tree`, 64a5011 대상)은 충돌이 없습니다. 파일별 영향은 다음과 같습니다.

| 파일 | PR #20 소유 | 변경 성격 |
|---|---|---|
| `engine/policy.py` | 예 (140줄 변경) | `_convert`·`apply_outcome` 대체 판단을 상태 계약으로 교체. 행동 생성 규칙은 그대로 |
| `engine/cohort_state.py` | 아니요 | `items_exhausted`에 근거 문서 조건 추가 |
| `web/engine_runner.py` | 아니요 (WP8) | 루프 교체, 기록 |
| `contract_session.py` | 아니요 | 훅 한 줄 |

`sim/`은 고치지 않았지만 `policy.py` 결과가 바뀌어 WP6 `result_hash`가 위 표처럼 달라집니다. `POLICY_VERSION`은 그대로 두었으므로 올릴지 동겸 판단이 필요합니다.

다른 후보 브랜치와는 파일이 겹칩니다. `fix/c2-session-boundary`는 `cohort_state.py`, `candidate/abc-safety-latest`는 `cohort_state.py`와 `contract_session.py`, `fix/non-measured-values`는 `contract_session.py`를 고칩니다.

## 남은 문제

- 웹 기본 `MAX_CLARIFY_ROUNDS`는 2입니다. 환자 "모름" -> 첫 문서에서 끝나고, 두 번째 문서는 대부분 `unexecuted`로 남습니다. 라운드를 늘릴지는 팀 결정입니다.
- 기록은 trace와 보고서에 남고 `Session.result()`에는 없습니다. 넣으려면 `pipeline.py` 승인이 필요합니다.
- 다음 실행으로 이어 가는 경로는 없습니다. 운영 상태는 실행별 메모리에만 있습니다.
- 웹 에이전트 모드는 LLM 오케스트레이터가 `clarify` 호출 여부를 정합니다. `run_fixed`로만 검증했고, 일찍 끝나면 `session_stopped`로 기록되는 것까지만 코드로 확인했습니다.
- 실제 Session에는 연락 불가를 만드는 실행기가 없습니다. 웹에서 실제로 나는 실패는 "모름"뿐입니다.
- 불충족 근거만 남은 쌍(열린 gap 없음)은 근거 문서가 소진돼도 VP로 남습니다. 기존 동작 그대로입니다.
- 시뮬레이터 horizon 종료에는 `unexecuted` 기록이 없습니다. 지표와 해시가 바뀌므로 넣지 않았습니다.
