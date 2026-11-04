# WP7 오프라인 감사 기록 (DecisionRecord v2) — 1·2단계

> 브랜치 `feat/wp7-decision-records` (base `integration/dcvc` `ddd9016`). 계약 `dcvc.engine.contracts/0.2` **PROVISIONAL**.
> 실제 LLM·유료 API 호출 없음. 이 문서의 수치는 오프라인 대역 실행 결과입니다.

## 1. 구성

| 파일 | 역할 | 의존 |
|---|---|---|
| `src/ctrec/engine/record_producer.py` | **공통 순수 생산기**. `StepObservation` → `StepResult(status, record, issues)`. 웹(WP8)·오프라인 공용 | 엔진 계약·`CohortState`·`FactSnapshot`만. Session·평가기·답변 시뮬레이터·LLM 모듈 import 없음(테스트로 검사) |
| `src/ctrec/offline_records.py` | **오프라인 어댑터**. `run_method_hygienic` + `factories` 주입으로 실제 Session 실행을 관찰해 생산기에 넘기고 두 파일을 씀 | 평가 경로를 쓰므로 `engine/` 밖 |
| `scripts/run_offline_records.py` | 데이터셋 실행기(사례별 폴더 + `summary.json`), 평가기 쪽 정답 누출 검사, 재실행 결정성 검사 | — |
| `tests/test_record_producer.py`, `tests/test_offline_records.py` | 생산기 24개, 어댑터 12개 | — |

수정한 기존 파일 없음 (`pipeline.py`, `decision_loop.py`, `engine/contracts.py`, `question_eval.py`, `audit.py`, `profile_facts.py`, 웹 코드 모두 그대로).

## 2. 실행 의미 (오프라인 어댑터)

- **라운드당 1문항** (`QUESTIONS_PER_ROUND = 1`, 이 경로 전용). 질문 시점의 Session을 읽어 `LegacyAdapterPolicy.propose/select`가 고른 행동 중 질문으로 만들 수 있는 **첫 행동 하나만** 묻습니다.
  - 이유: Session은 라운드마다 답을 붙인 뒤 프로필을 다시 추출하고 대상 시험을 다시 매칭합니다. 질문이 여러 개면 그 재평가 결과를 한 행동에 귀속할 수 없습니다.
  - 기존 평가(`question_eval` 기본 3문항, baseline/candidate)는 바뀌지 않습니다.
  - 달라지는 점: 같은 라운드 수에서 묻는 질문이 1/3로 줄고, 선택됐지만 묻지 않은 행동은 기록하지 않고 건수만 남깁니다(`selected_not_asked`). 그래서 이 경로의 회수율·질문 수는 기존 평가 수치와 비교할 수 없습니다.
- 첫 프로필 추출·매칭은 행동의 결과가 아니므로 기록하지 않습니다. 기록은 첫 질문부터 시작합니다.
- 청구: 실제로 질문을 보낸 행동만 `EffortLedger.charge` (응답 여부와 무관하게 요청 1회). 비용 값은 `bundles.DEFAULT_SOURCES`의 **가정값**입니다.
- 응답 결과는 `policy.apply_outcome`으로 운영 상태(ops)에 반영하고, 다음 상태를 그 ops로 만듭니다. 이때 생긴 정책 전이(`sources_exhausted`)는 기록에 정책 권한으로 남습니다.
- 질문 문장은 결정적 문구 `[engine] <info_key>`입니다 (LLM 없음, 목적·대상은 Action 그대로).
- run_id = `RUN-<별칭>`. 원래 ID가 없고 salt가 같으면 같습니다.

## 3. 산출 파일과 상태

| 파일 | 내용 |
|---|---|
| `decision_records.jsonl` | 계약 0.2로 정상 표현된 기록만 (`audit.RecordWriter`, 기존 `load_run_v2`로 읽힘) |
| `decision_record_issues.jsonl` | 경고·차단 진단 (`record_producer.IssueWriter` / `load_issues`). ID·코드·상태만 |

- 단계 상태: `recorded` / `recorded_with_warnings` / `contract_violation`. **contract_violation 단계는 정상 기록을 쓰지 않고** 원래 before/after를 차단 진단(severity `blocking`)에 남깁니다. 그 단계의 다른 실제 변화(정상 전이·기준 변화·결과와 귀속 사실)도 `withheld_pair_transition` / `withheld_criterion_change` / `withheld_outcome` 진단으로 남겨 보류 때문에 빠지지 않게 합니다.
- **용어 구분**: 단계 상태 `contract_violation`(감사 기록 보류)과 쌍 상태 `PairStatus.BLOCKED_FAILURE`(확보 수단 소진에 따른 운영상 보류)는 다른 개념입니다. BLOCKED_FAILURE 전이는 계약상 정상 전이이며 기록의 `PairTransition`(정책 권한)으로 남습니다.
- 실행 상태: `OfflineRunResult.complete`는 (실행 성공, 미지원 0, 차단 0, 파일 정합성 문제 0)일 때만 참. 차단이 있으면 `audit_status = "incomplete_contract_violation"`.
- `verify_run`이 두 파일을 다시 읽어 확인: run_id 일치, 단계 = 질문 수 = 재매칭 라운드 수, 기록 단계 ∪ 위반 단계 = 전체 단계(겹침 없음), 단계별 action_id 일치, 파일 건수 = 생산 결과, 그리고 **실행 중 관찰한 전후 상태와 대조**해 (1) 상태가 바뀐 모든 쌍이 기록의 `PairTransition` 또는 위반 진단에 있는지 (2) 실제 새 사실이 모두 `facts_added` 또는 진단 `fact_ids`에 있는지.
- **현재 Inspector v2(`load_run_v2`)는 진단 파일을 읽지 않습니다.** 차단 단계를 화면에 띄우려면 `audit.py` 확장이 필요합니다(팀 승인 대기, 이번 단계에서 하지 않음).

## 4. 사실 귀속

- 새 사실 = 전후 FactStore 스냅샷 차이. 기록에 넣는 사실은 **명시적 출처**가 있는 것뿐입니다: 실행기가 연결한 ID, 또는 `source_reference.kind == "answer"`이고 `ref`가 질문의 출처 ref와 같은 사실. 인용문 일치만으로는 귀속하지 않습니다.
- 현재 `profile_facts`는 답에서 나온 사실을 `ref="raw_text:answers"`로만 남겨 질문을 알 수 없습니다. 그래서 어댑터는 `source_ref=None`을 넘기고, **`facts_added`는 항상 비어 있습니다** (임의로 채우지 않음).
- 귀속하지 못한 실제 사실은 `unattributed_fact` 경고, 무응답 표지("모름")를 인용한 사실은 `decline_marker_fact` 경고로 남습니다. 어느 쪽도 지우지 않으며 `fact_version_before/after`는 실제 값입니다.

## 5. masked 101건 결과 (2026-11-02, salt `wp7-offline`, rounds=2)

| 항목 | 값 |
|---|---|
| 실행 / 정상 감사 완료 | 101 / 101 |
| 단계 = 질문 = DecisionRecord | 109 (사례당 1단계 93건, 2단계 8건) |
| 진단 | 109, 모두 경고: `decline_marker_fact` 101, `unattributed_fact` 8. 차단 0 |
| 결과 | `no_response` 101, `succeeded` 8 |
| 실제 새 사실 / 귀속 | 109 / 0 |
| 정책 전이 (`sources_exhausted`) | INFO_MISSING→BLOCKED_FAILURE 103, VERIFICATION_PENDING→BLOCKED_FAILURE 1 |
| 기준 재평가 / 판정 변화 / 판정 쌍 상태 변화 | 2750 / 0 / 0 |
| `verify_run` 문제 · 정답 누출(평가기 검사) · 재실행 불일치(fact_id 정규화) | 0 · 0 · 0 |
| 독립 대조 (2026-11-03): 관찰된 BLOCKED_FAILURE 전이 → 기록의 정책 권한 PairTransition | 104 → 104 (누락 0) |
| 독립 대조: 실제 새 사실 → `facts_added` / 진단 | 109 → 0 / 109 (누락 0) |
| 재실행 비교: 원본 그대로 동일 / fact_id 정규화 후 동일 | 0 / 101. 원본 차이는 FactStore uuid가 들어가는 필드뿐: 기준 `evidence_fact_ids`, 진단 `fact_ids`, 진단 `issue_id` |

- 대부분 1단계에서 끝나는 이유 (코드 근거, §7): 첫 답이 "모름"이면 **이 어댑터가** 그 행동을 `no_response`로 해석해 `policy.apply_outcome`에 넘깁니다. `apply_outcome`은 `patient_interview`를 환자 단위로 실패 처리하고, 대체 정보원이 없어 그 행동의 대상 시험을 소진(BLOCKED_FAILURE)으로 둡니다. 이후 그 환자의 모든 환자 질문 행동은 `_convert`에서 DEFER(`sources_exhausted`)가 되어 실행이 끝납니다. 즉 결과는 **WP7의 무응답 해석 + WP5의 정보원 단위 소진 규칙**이 합쳐진 것입니다.
- 판정 변화가 0인 이유: Matcher 대역이 LLM 기준을 모두 '판정 불가'로 두어 확정 상태가 생기지 않습니다. 직접 역전·판정 변화 경로는 생산기 단위 테스트(고정 입력)와 어댑터 차단 연결 테스트로만 검증했습니다. **실제 LLM 경로의 판정 변화·역전 추적은 미검증**입니다.

재현: `PYTHONPATH=src python scripts/run_offline_records.py --out <DIR> --set masked --repeat`

## 6. 알려진 의존·제약

- `integration/dcvc`의 `eval_hygiene.load_cases_hygienic`은 **빈 `AliasMap`을 버리고 무작위 salt로 새로 만듭니다** (WP1 PR #1에서 수정, 미병합). 그래서 실행기·테스트는 맵을 미리 채워 넘깁니다. PR #1 병합 뒤에는 필요 없습니다.
- 오프라인 대역 `stub_profiler`는 "A: 모름" 줄을 기타 사실로 추출합니다. 실제 profiler에서도 무응답 문장이 사실이 되는지는 미확인입니다.

## 7. 정책 소진 조건 (코드 근거, WP5 수정 없음)

| 단계 | 위치 | 동작 |
|---|---|---|
| "모름" 판정 | `decision_state.DECLINED_ANSWERS`(:51), `_declined`(:229, strip+casefold) | 무응답·거부 표지 목록 |
| no_response 해석 | **WP7** `record_producer.is_declined`, `offline_records._Recorder.settle` | 표지면 `ActionOutcome(status="no_response", error_code="no_answer")`. 기존 정책(`decision_policy.py:150-155`)은 같은 상황을 해당 정보 항목(info_key)만 DEFER(`user_declined_or_exhausted`)로 다룸 |
| 실패 기록 키 | `engine/policy.py` `apply_outcome` | `failed_sources`에 `action.source_id` 추가 (`OpsState`는 환자 별칭 단위) → **정보원 전체**가 그 환자에 대해 실패 |
| 소진 범위 | 같은 함수 | `catalog.alternatives(source_id, failed)`가 비면 `exhausted_trials.update(action.target_trial_ids)` → 그 행동의 대상 시험만 BLOCKED_FAILURE (`derive_pair_status`가 INFO_MISSING·VERIFICATION_PENDING만 바꿈) |
| 이후 행동 | `engine/policy.py` `_convert` | 정보원이 실패 목록에 있고 대체가 없으면 행동을 DEFER(`sources_exhausted`)로 바꿈 → 그 환자의 **모든** 환자 질문이 실행 대상에서 빠짐 |
| 대체 정보원 | `engine/bundles.py` `DEFAULT_SOURCES` | `patient_interview`는 `alternatives=()` (환자 종류 정보원은 이것 하나). 문서 정보원은 `medical_record`로 대체 |

실행 근거 (masked 101): BLOCKED_FAILURE 전이 104건 모두 정책 권한·사유 `sources_exhausted`, 모든 실행 행동의 정보원이 `patient_interview`, 위반 0. 다른 정보원이 있는데 막힌 경우는 없음(환자 질문 행동은 모두 `patient_interview`로 배정되고 대체가 정의되어 있지 않음).

## 8. 팀 요청·결정 사항

1. **서연 (profile_facts)**: 답변에서 나온 사실에 질문별 출처 `source_reference.ref`(예: `qa:round1:Q1`)를 남기도록 요청. 이것이 없으면 정상 기록의 `facts_added`가 항상 비어 추적성이 불완전합니다. 무응답 표지를 사실로 저장하지 않을지도 함께 결정.
2. **공정현/팀 (audit.py)**: `load_run_v2`가 `decision_record_issues.jsonl`을 함께 읽어 `issues`에 합치는 확장 승인 (Inspector v2에서 차단 단계가 보이도록).
3. **동겸 (WP5 정책) + 팀**: (a) 환자의 "모름"을 연락 실패와 같은 `no_response`로 볼지(현재 WP7 해석), 항목 단위 무응답으로 볼지 (b) 무응답 한 번에 `patient_interview` 전체가 그 환자에 대해 실패 처리되는 규칙이 의도인지. 계약 `OutcomeStatus`에는 '답했지만 모름'이 따로 없음. 오프라인 실험의 질문 수·회수율에 크게 영향.
4. 직접 역전: 계약 금지 유지 + 차단 진단(현재) vs 계약 변경.
5. 선택됐지만 묻지 않은 행동의 기록 여부(`proposed`/`not_executed` 지원은 있음, 지금은 건수만).
6. 라운드당 1문항 제한을 WP7 실험의 표준으로 둘지, 다문항 라운드의 귀속 규칙을 따로 정할지.
7. 웹(WP8)이 `record_producer`를 import하는 방식과 진단 파일 형식 합의.
