# Agentic Decision Loop (Phase 3, 진단 전용)

환자 사실과 검증된 기준표로 Decision State를 만들고, 다음 행동(질문·문서 요청·검토·종료 등)을 코드 정책으로 고르는 루프입니다.

**현재는 진단(shadow) 전용입니다.** 기존 Matcher 판정, 질문 선정, 추천 순위·상태, 웹 추천 결과를 바꾸지 않으며, LLM과 네트워크를 부르지 않습니다. 결과는 임상시험 참여 가능 여부의 확정이 아니라, 다음에 무엇을 확인하면 좋을지에 대한 참고 계획입니다.

| 모듈 | 역할 |
|---|---|
| `src/ctrec/decision_state.py` | Action·DecisionState·EvaluationRecord 스키마, Session 사본(`SessionView`), 상태 구성(`build_state`) |
| `src/ctrec/decision_policy.py` | 결정적 행동 후보·우선순위(`candidates`), 안전 규칙 확인(`validate`), 설정값(`PolicyConfig`) |
| `src/ctrec/decision_loop.py` | 실행기(`execute`), 루프 제어(`run_loop`), Phase 2 연계(`staleness`), Session 연결(`DecisionTracker`) |
| `scripts/decision_scenarios.py` | 고정 기준표 26개 시험·471개 기준 시나리오 10개 (읽기만) |

## 1. Decision State

| 필드 | 내용 |
|---|---|
| `session_id`, `patient_id` | 실행 식별자 |
| `fact_version`, `fact_ids` | 사실 스냅샷 버전(`fact_changes`, 내용 기반)과 사실 ID. 환자 원문은 담지 않음 |
| `trials[tid]` | 기존 판정(`eligibility`·`match_status`, 바꾸지 않음), 모집 여부, 코호트별 결과, 판정 당시 사실 버전, `stale` |
| `trials[tid].criteria[]` | 기준별 상태(아래) |
| `not_evaluated_trials` | 평가 실패 시험 (MET/NOT_MET으로 바꾸지 않음) |
| `conflicting_fields` | 단일 값 항목의 상충 상태 (값은 넣지 않음) |
| `unverified_fact_ids` | 문서 검증(`document_verified`)이 아닌 사실. `clinical_record` 선언도 여기에 포함 |
| `question_history` | 질문 이력 (대상 기준, 모름·거부 여부) |
| `action_history`, `pending_actions` | 실행한 행동 이력, 미완료 행동 |
| `stop_reason`, `iteration` | 종료 사유, 반복 횟수 |
| `budget`, `usage` | 반복·도구·비용·질문·LLM·시간 상한과 사용량 |

기준별 상태(`CriterionState`):

| 필드 | 내용 |
|---|---|
| `status` | MET / NOT_MET / UNKNOWN / NEEDS_FULL_EVALUATION |
| `requirement` | 필수와 선택의 구분 (아래 값) |
| `gap` | 정보 부족의 종류 (아래 값) |
| `conflict_on_decided` | 확정 판정의 근거 항목에 상충 사실이 있음 |
| `info_key`, `fields` | 정보 묶음 키와 그 항목 |
| `evidence_refs` | 근거 fact_id |
| `asked`, `declined` | 질문한 횟수, 모름·거부 여부 |
| `acquisition_status` | 기존 D7 확보 기록의 상태 |
| `record` | 입력 계측(EvaluationRecord) |

`requirement` 값:
- `required`: 판정에 적용되는 기준(공통 + 선택된 코호트)
- `alternative_cohort`: 다른 코호트 전용. OR 구조의 다른 갈래입니다.
- `waivable`: 등록 시 확인하는 절차 기준

`gap` 값:
- `missing`: 질문으로 확인 가능
- `unverified`: 답은 있으나 진료 기록이 필요
- `physician_review`: 원문에 구체 기준이 없음
- `conflict`: 근거 항목의 사실 값이 맞지 않음
- `stale`: 사실이 바뀌어 기준선 LLM 판정을 다시 쓸 수 없음

정보 묶음 키(`info_key`)는 구조화 필드(`ecog`, `lab:hemoglobin` 등)나 바이오마커 표적(`biomarker:EGFR`)으로만 시험 사이를 묶습니다. 그 밖의 기준은 묶지 않으며(`criterion:<시험>:<기준>`), 문장 유사도로 묶지 않습니다.

## 2. 행동(Action)

필드:
- 식별·분류: `action_id`, `action_type`, `reason_code`, `human_readable_reason`
- 대상: `target_trial_ids`, `target_criterion_ids`, `required_fact_fields`, `evidence_refs`
- 순서: `priority`(작을수록 먼저), `tier`, `value_score`와 `score_terms`
- 계획: `prerequisites`, `expected_information_gain`, `estimated_cost`
- 결과: `status`, `outcome`, `fingerprint`, `created_at`

| 행동 | 의미 | 실행 범위 |
|---|---|---|
| ASK_USER | 환자에게 확인 질문 | DIAGNOSTIC_ONLY: 질문 후보만 기록(`proposed_not_dispatched`). 답변 공급자(`QuestionChannel`)를 주입하면 EXECUTABLE이며, 이때도 작업용 사본 저장소에만 자기 보고 사실을 더함 |
| REQUEST_DOCUMENT | 진료 기록·검사 결과지 요청 | REQUIRES_EXTERNAL_INPUT: 필요한 문서와 이유만 기록, 검증 완료로 표시하지 않음 |
| VERIFY | 근거 사실의 출처·검증 상태·충돌 확인 | EXECUTABLE: 코드 확인만, 승격 없음. 해소할 수 없으면 `requires_review` |
| RUN_TOOL | 허용된 도구 실행 | EXECUTABLE: 허용 목록 `deterministic_reevaluation`(Phase 2 규칙 엔진 재계산)만. 그 밖은 거절 |
| REPARSE | 새 입력 재추출 | DIAGNOSTIC_ONLY: LLM이 필요해 필요성만 기록 |
| RETRIEVE | 추가 임상시험 검색 | DIAGNOSTIC_ONLY: 네트워크 요청 없음, 고정 기준표 변경 없음 |
| DEFER / NEEDS_REVIEW / FINALIZE | 보류 / 의료진·담당자 검토 / 이번 평가 단계 종료 | EXECUTABLE: 상태와 미해결 항목 기록. 참여 가능 여부를 확정하지 않음 |

## 3. 우선순위 정책 (`decision_policy.py`)

| 단계 | 내용 |
|---|---|
| 1 | 상충 사실: VERIFY 뒤에도 남으면 NEEDS_REVIEW. 확정 판정의 근거가 상충하면 `conflict_on_decided_criterion`. 사실 변경 뒤 규칙 엔진 기준 재계산(RUN_TOOL) |
| 2 | 필수 기준의 부족한 정보 (ASK_USER) |
| 3 | 필수 기준의 검증되지 않은 근거 (REQUEST_DOCUMENT) |
| 4 | 이미 물었지만 미해결: 답했으면 REPARSE 한 번 뒤 DEFER, 모름·거부·확보 단계 소진이면 DEFER. 다시 묻지 않음 |
| 5 | 같은 단계 안에서 여러 시험·기준에 영향을 주는 정보가 앞섬 (`value_score`) |
| 6 | 선택·영향이 작은 정보(다른 코호트 전용, 이미 불충족인 시험, 모집 종료), 의료진 검토, LLM 재판정 보류, 추가 검색 |
| 7 | FINALIZE |

`value_score`는 다음과 같이 계산합니다. 가중치는 `PolicyConfig`로 관리합니다.

```
w_trials × 결과가 바뀔 수 있는 시험 수 + w_required × 필수 기준 수 + w_other × 그 밖의 기준 수
- penalty_asked × 이미 물은 횟수 - 비용
```

- '결과가 바뀔 수 있는 시험'은 모집 중이고 아직 열린 경로가 있으며, 그 정보가 그 경로의 기준에 쓰이는 시험의 개수입니다. 확률이 아니라 구조적 개수입니다.
- 불충족 확정 수준(`TrialState.mismatch`)은 질문 가치 계산에만 쓰고, 기존 판정은 바꾸지 않습니다.
  - 경로: 코호트 하나(공통 + 그 코호트 기준). 코호트가 없으면 시험 전체가 한 경로입니다.
  - 경로의 필수 기준 중 NOT_MET이 있고, 그 근거 사실이 상충하지 않으며 문서 검증(`document_verified`)됐으면 confirmed입니다.
  - NOT_MET 근거가 문서로 검증되지 않았으면(자기 보고, 출처 미상, `clinical_record` 선언뿐) provisional입니다.
  - NOT_MET이 없거나 근거가 상충하면 열린 경로입니다.
  - 열린 경로가 하나라도 있으면 시험은 `none`입니다. OR/코호트의 다른 경로가 살아 있으면 시험 전체를 제외하지 않습니다.
  - UNKNOWN, NOT_EVALUATED, LLM 미평가는 불충족으로 세지 않습니다.
- confirmed 시험의 기준은 '결과가 바뀔 수 있는 시험'에서 뺍니다. 질문 하나로 확정된 불충족이 풀린다고 가정하지 않습니다.
- provisional 시험의 기준은 확정 제외도 열린 시험도 아니므로 `w_provisional`(기본 0.5)로만 셉니다. 대신 불충족 근거를 확인하는 REQUEST_DOCUMENT(`exclusion_basis_unverified`, 정보 키별로 묶음)를 제안합니다.
- 이 점수는 행동 순서에만 쓰며, 임상 적합성 판단에 쓰지 않습니다.
- 정보 충분성은 하나의 숫자 임계값이 아니라 시험·기준별 상태로 판단합니다.

안전 규칙(`validate`·`build_state`·실행기):
- UNKNOWN을 NOT_MET으로, NOT_EVALUATED를 MET/NOT_MET으로 바꾸지 않습니다.
- 자기 보고나 `clinical_record` 선언을 검증된 기록으로 승격하지 않습니다. 진료 기록이 필요한 기준의 자기 보고 답은 UNVERIFIED가 되어 REQUEST_DOCUMENT로 갑니다.
- 상충 사실 중 하나를 고르지 않습니다. 상충 근거로 MET을 만들지 않습니다(사실 기반 재계산은 근거 게이트가 판정 불가로 둠).
- 여러 값 항목(진단·약물)이 공존하는 것은 상충이 아닙니다. 같은 바이오마커 표적의 상반된 결과만 상충으로 봅니다.
- 이미 해결된 정보나 모름·거부한 정보에 대한 질문, 허용되지 않은 도구, 선행 조건이 없는 행동은 거절합니다.

## 4. 루프와 종료 조건 (`run_loop`)

1. 상태 구성 (사실·재계산 결과·질문 이력이 같으면 캐시 사용)
2. 후보 생성
3. 안전 규칙·예산 확인
4. 가장 앞선 후보 실행
5. 사실 변화 확인
6. 종료 확인

행동 지문(fingerprint)으로 같은 상태에서 같은 행동을 반복하지 않습니다.
- 지문에는 행동 종류, 정보 키, 대상, 도구, 사실 버전이 들어갑니다.
- 질문·문서 요청·보류·검색은 사실 버전과 무관하게 한 번만 합니다.

| 종료 사유 | 조건 |
|---|---|
| `sufficient_information` | FINALIZE 시점에 미해결 항목이 없음 |
| `needs_review` / `awaiting_external_input` / `awaiting_user_answers` / `user_declined` | FINALIZE 시점의 미해결 종류 (`finalize_reasons`에 모두 기록) |
| `no_valid_action` / `repeat_risk` | 유효한 후보 없음 / 남은 후보가 모두 이미 한 행동 |
| `no_information_change` | 질문을 보냈는데 사실이 바뀌지 않은 연속 횟수가 `max_no_change`(기본 2)에 도달 |
| `iteration_limit` / `tool_call_limit` / `cost_limit` / `time_limit` | `Budget`: 반복 30, 도구 10, 비용 30, 시간 5초 (질문 5개는 넘는 질문만 거절) |
| `cancelled` / `timeout` | 웹 실행 취소·시간 제한. `llm.guard()`와 같은 확인 지점을 반복마다 확인 |
| `tool_failure` | 실행기 예외. 종류만 기록하고 안전하게 멈춤 |

## 5. 사실·의존성 연계 (Phase 2 재사용)

- `staleness`는 시험별 기준선 판정 당시의 스냅샷과 현재 스냅샷을 `fact_changes.detect_changes`로 비교합니다. 그다음 `dependency_graph.resolve`·`analyze_impact`로 영향받는 기준을 찾습니다.
- **LLM 기준은 입력 항목을 검증할 수 없어 사실이 바뀌면 항상 영향받습니다.** 기준선 LLM 판정은 다시 쓰지 않고 NEEDS_FULL_EVALUATION으로 둡니다. LLM 공급자가 없으므로 DEFER(`llm_reevaluation_required`)로 미완료를 남깁니다.
- 규칙 엔진이 구조적으로 판정할 수 있는 기준(구조화 필드·연산자)은 RUN_TOOL로 사실 기반 재계산을 시도합니다. 근거 게이트를 거치므로 자기 보고는 UNVERIFIED, 상충은 판정 불가가 됩니다. 영향받지 않은 규칙 엔진 기준은 재사용합니다.
- `EvaluationRecord`는 판정 계측 인터페이스입니다.
  - 기록하는 값: 실제 사용한 fact_id·입력 항목, 사실 버전, 기준 버전, 입력 지문, 엔진·모델, 생성 시점
  - 규칙 엔진만 `inputs_verified=True`입니다. LLM 판정은 `used_fields=None`입니다.
  - `narrowable()`은 기록이 없거나 입력이 검증되지 않았으면 False이므로, 기록 없이는 의존성을 좁힐 수 없습니다.

## 6. Session·웹 연결과 활성화

- 웹 경로는 그대로입니다: FastAPI → Orchestrator → Session → Matcher → SSE.
- `Session(decision_loop="diagnostic")`(기본 `"off"`)이면 `match()`가 끝날 때마다 `DecisionTracker.observe`를 실행합니다(Phase 2 진단과 같은 확인 지점).
  - Session 사본(`SessionView.from_session`)만 읽으며, 새 Session을 만들거나 다시 매칭하지 않습니다.
  - 결과는 `session.decision_diagnostics`와 `result()["decision_diagnostic"]`(켰을 때만)에 둡니다.
  - 실패하면 `error_type`만 기록하고 추천 흐름은 계속됩니다.
- 웹에서는 `POST /api/runs`에 `"decision_diagnostic": true`(기본 false)를 보냅니다.
  - 진단 결과는 SSE `decision_diagnostic` 이벤트로 따로 보냅니다(trace와 섞지 않음). 기본 off이면 이벤트 흐름이 이전과 같습니다.
  - 화면은 주소에 `?diagnostic=1`일 때만 접힌 "개발자 진단" 영역에 표시하며, 판정·추천 화면은 바꾸지 않습니다.
- 실행 소유권·취소·저장 기본 off 정책은 그대로입니다. 취소되면 다음 확인 지점에서 진단도 멈춥니다.

## 7. 실제 판정 경로로 바꾸기 전에 필요한 검증

1. 실제 LLM 판정 공급자 연결과 `EvaluationRecord` 기록 (Matcher가 실제 입력 항목을 남기도록). 그 전까지는 LLM 기준을 재사용하지 않습니다.
2. 질문 생성 연결:
   - ASK_USER 계획을 기존 질문 생성 에이전트의 대상(`target`)으로 넘기는 경로를 만듭니다.
   - 기존 질문 선정과 나란히 비교하는 평가(정보 가리기 평가셋 `scripts/build_masked_benchmark.py`)로 질문 수와 해소율을 비교합니다.
3. 문서 업로드·검증 경로 (REQUEST_DOCUMENT를 실제로 처리), 의료진 검토 대기열.
4. 실제 LLM 비용·지연 측정과 루프 예산 조정.
5. 의료진 검토를 거친 우선순위 가중치 확정 (현재 값은 설명 가능한 기본값이며 임상 검증 전).

## 8. 알려진 한계

- 기준선 LLM 판정은 진단 시나리오에서 오프라인 대역(모두 판정 불가)이라, 실제 LLM에서는 후보와 순서가 달라질 수 있습니다.
- 정보 묶음은 구조화 필드·바이오마커 표적에만 적용합니다. 자유 텍스트 기준(동반질환 등)은 기준마다 따로 질문 후보가 됩니다.
- 진단 모드는 질문을 보내지 않으므로, 루프 안에서 사실이 바뀌는 흐름은 답변 공급자(시뮬레이션)로만 확인했습니다.
- 진단 오버헤드는 실제 기준표 기준 실행 라운드마다 약 0.05–0.25초(합성 시나리오 측정)이며, 켰을 때만 듭니다.
- 실제 시스템에는 아직 문서 검증 경로가 없어 거의 모든 사실이 문서 검증 전입니다. 그래서 실제 기준표에서 불충족은 대부분 provisional로 분류됩니다(시나리오 8: 나이 불충족 21개 모두 provisional). confirmed 경로는 합성 테스트(`tests/test_decision_value.py`)로 검증했습니다.
