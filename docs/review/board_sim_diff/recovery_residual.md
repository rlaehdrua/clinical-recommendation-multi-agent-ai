# PR #20 복구 정책 회귀 결과와 남은 결함 (동겸 전달용)

기준: `origin/integration/dcvc` 64a5011 (PR #20 0628cb5 병합 f18f829, PR #25 병합 포함).
합성 데이터만 사용했고 LLM 호출은 없습니다. `policy.py`·`pipeline.py`는 이 브랜치에서 고치지 않았습니다.

## 144d226 회귀 결과

| 대상 | 결과 |
|---|---|
| aca2033 (수정 전) | 4 failed, 1 passed (결함 테스트 4건 실패, 대조 통과) |
| 64a5011 (병합본) | 5 passed |

기존 테스트의 기대값은 그대로입니다. 리뷰 결함 1(정보원 실패가 시험 전체를 막음)과 결함 2(연락 불가 뒤 문서 전환)는 해결됐습니다.
`tests/test_pr20_recovery_paths.py`로 failed_sources, UNREACHABLE, BLOCKED_FAILURE, DEFER, 대체 문서 요청, 공백 있는 검사 키, 회복 제거(ablation)도 확인했고 모두 통과합니다.

## 결함 R1: 답한 문서가 있으면 항목이 소진되지 않음 (policy.py, 수정 요청)

이 결함은 strict xfail 테스트 2건으로 고정했습니다. 패치를 적용하면 XPASS로 실패하므로, 그때 `xfail` 표시를 지워 주세요.

**재현 입력.** 시험 하나, 기준 하나(`category="medication"`, 구조화 필드 없음, `criterion:T1:I9`). 정보원이 다음처럼 답합니다.

| 경우 | patient_interview | medication_list | medical_record |
|---|---|---|---|
| R1a | UNREACHABLE | 답함, gap 남음 | no_answer |
| R1b | UNREACHABLE | 답함, gap 남음 | 답함, gap 남음 |

**기대 결과.** 정책이 내는 행동이 DEFER `sources_exhausted`뿐이므로 쌍은 BLOCKED_FAILURE입니다.

**실제 결과.** `exhausted_items`에 항목이 들어가지 않아 쌍이 INFO_MISSING으로 남고, 더 실행할 행동도 없습니다.

**호출 경로.**

- `_convert`는 `blocked = ops.failed_items | tried`로 이미 요청한 문서를 대체에서 뺍니다 (`policy.py:188`).
- R1a: `apply_outcome`의 항목 단위 분기는 `failed_items`만 넘깁니다. 그래서 답한 문서가 대체로 남아 소진되지 않습니다 (`policy.py:405-408`).
- R1b: 마지막 결과가 성공이면 `failure_scope`가 None이라 소진 계산 분기에 들어가지 않습니다 (`policy.py:376-408`).

**최소 패치.** `docs/review/board_sim_diff/recovery_policy.patch` (`git apply`로 적용).

**측정.** 시뮬레이터 4개 코호트 80쌍, legacy_greedy, seed 0, horizon 30.

| 응답 설정 | 쌍 상태 | 수정 전 | 패치 후 |
|---|---|---|---|
| 기본 응답 모델 | BLOCKED_FAILURE | 17 | 20 |
| 기본 응답 모델 | 모든 항목이 소진 보류인데 BLOCKED가 아닌 쌍 | 3 | 0 |
| 환자 연락 불가 100% | BLOCKED_FAILURE | 0 | 22 |
| 환자 연락 불가 100% | 모든 항목이 소진 보류인데 BLOCKED가 아닌 쌍 | 14 | 0 |
| 환자 연락 불가 100% | VERIFICATION_PENDING | 8 | 0 |

패치를 임시 트리에 적용한 전체 pytest는 2481 passed, 9 skipped, 1 xfailed, 2 failed입니다. 실패 2건은 R1 xfail 테스트의 XPASS(strict)로, 패치가 듣는다는 신호입니다. 그 외 회귀는 없습니다.

**확인이 필요한 영향.**

- 패치 뒤 환자 연락 불가 설정에서 VERIFICATION_PENDING 8쌍이 BLOCKED_FAILURE로 바뀝니다. `OBTAINABLE_GAPS`에 `unverified`가 있어 현재 정의와는 맞지만, 의도한 동작인지는 동겸 판단이 필요합니다.
- 쌍 상태가 바뀌므로 WP6 `result_hash`와 지표가 달라집니다.

## 결함 R2: Session이 정책의 남은 문서 요청을 실행하지 않음 (pipeline.py, 승인 필요)

**재현 입력.** `[ECOG, HB, *KEEP_LOOP_OPEN]`, `LegacyAdapterPolicy(rephrase=True)`, 모든 답 "모름".

**기대 결과.** ECOG의 `medical_record` 문서 요청이 실행됩니다.

**실제 결과.** 12라운드에서 끝납니다 (한도 14). 종료 시점에 `propose`에는 그 요청이 남아 있습니다.

**호출 경로.** `Session.needs_clarification`과 `uncertain`이 `acquisition.is_open`만 보고 정책의 남은 행동은 보지 않습니다 (`pipeline.py:382-417`). base부터 같은 동작이고 PR #20에서 생긴 회귀가 아닙니다. 기존 테스트의 `KEEP_LOOP_OPEN`이 이 조건을 피해 갑니다.

**패치.** 만들지 않았습니다. `pipeline.py`는 보호 파일이고, 루프 조건에 엔진 모드의 남은 행동을 넣는 방식은 승인 후 설계해야 합니다. strict xfail 테스트 1건으로 고정했습니다.

## 결함으로 분류하지 않은 관찰

- `requested_documents_unresolved` 보류는 소진으로 세지 않습니다. 실패가 없는 경로이고 현재 코드 주석의 의도와 맞아 R1 패치에 넣지 않았습니다.
- 문서 연락 불가 100% 설정에서 17쌍은 환자 질문이 ASK로 남지만 시뮬레이터의 반복 금지로 실행되지 않고 INFO_MISSING으로 끝납니다. 이 17쌍의 원인은 따로 추적하지 않았습니다.
- `_cut_off_items`는 실패한 행동의 대상 시험만 봅니다. 다른 시험의 항목이 빠질 수 있는 구조지만, 이번 139개 잔여 항목은 모두 실패 대상 시험에 있어 재현되지 않았습니다.
