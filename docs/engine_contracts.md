# DCV+C 공통 엔진 계약 (WP0) — **PROVISIONAL**

> 상태: **팀 승인 전 임시 계약** (`ctrec.engine.contracts.CONTRACT_STATUS == "PROVISIONAL"`, 버전 `dcvc.engine.contracts/0.2` 후보, 0.1 기록도 읽음).
> **0.2 변경 요약은 맨 아래 §7, 자세한 내용은 `docs/dev/contract_v02_changelog.md`.** 아래 §1–§6은 0.1 원문이며, §4의 `derive_pair_status` 설명과 §5의 열린 질문은 §7이 우선합니다.
> 근거: `docs/audit/03_architecture_and_work_packages.md` §3 (I-1~I-9). 승인 전까지는 필드 **추가만** 하고 의미·이름은 바꾸지 않는 것을 원칙으로 합니다.
> 코드: `src/ctrec/engine/contracts.py` / 테스트: `tests/test_engine_contracts.py`

## 1. 이름 공간

기존 모델과 이름이 겹치지만 **다른 모듈**입니다. 같은 파일에서 함께 쓸 때는 모듈 이름으로 구분합니다.

| 엔진 계약 | 기존 모델 | 관계 |
|---|---|---|
| `contracts.Action` | `decision_state.Action` | 어댑터(`engine.policy.LegacyAdapterPolicy`)가 기존 행동을 읽어 새로 만듦. 기존 모델은 그대로 |
| `contracts.CriterionResult` | `incremental.CriterionResult`, `schemas.CriterionAssessment` | 판정 계층 출력의 요약. 기존 판정 값을 복사만 함 |
| `contracts.PairStatus` | `TrialMatch.eligibility` (4값) | `derive_pair_status()`로 파생. eligibility는 바꾸지 않음 |
| `contracts.EffortBudget` | `decision_state.Budget`, `web/budget.py` RunBudget | 별개. 코디네이터 시간 예산. 웹 LLM 비용 원장과 섞지 않음 |

```python
from ctrec.engine import contracts as C
from ctrec.decision_state import Action as LegacyAction
```

## 2. 공통 원칙

1. **불투명 환자 별칭**: 모든 계약은 `patient_alias`(`^PA-[0-9a-f]{16}$`)만 받습니다. 원래 환자·케이스 ID(예: `M-…-rescue-1`, `V-…-I9-single`)는 형식 검사에서 거부됩니다. 별칭 생성은 `ctrec.eval_hygiene.AliasMap`.
2. **판정과 정책의 분리**: 모든 모델은 `frozen=True`, `extra="forbid"`, 시퀀스는 `tuple`. 정책 코드는 받은 판정 객체를 고칠 수 없고, 계약에 없는 필드(예: `success_probability`)를 넣을 수 없습니다.
3. **판정 권한**: `CriterionResult`에서
   - MET/NOT_MET이면 `decided_by`(rule_engine/llm/clinician) 필수, UNKNOWN이면 `none`
   - `adjudication="expert"` 기준은 `decided_by="clinician"`일 때만 MET/NOT_MET
   - `adjudication="rule"` 기준을 LLM이 확정했다고 기록할 수 없음
4. **비용 축 분리** (`EffortCost`): `coordinator_minutes`(능동 작업), `clinician_minutes`, `llm_calls`, `llm_cost_usd`, `patient_wait_minutes`(응답 대기 — 능동 시간 아님, 예산에서 빼지 않음). 모두 음수 금지. 합산 시 대기는 최댓값(동시 대기)입니다.
5. **정보원 단위 청구**: `InformationSource.request_cost`는 (환자, 정보원)당 한 번, `per_item_minutes`는 같은 요청 안 항목마다. 실제 청구 키는 `ActionOutcome.charge_keys`에 남깁니다.
6. **확률 없음**: 온라인 우선순위에 근거 없는 성공 확률을 쓰지 않습니다. `Action.value_score`는 구조적 점수(영향 받는 시험·기준 수)입니다.
7. **사유는 코드로만**: `reason_code`, `error_code`는 `^[a-z0-9_]{1,64}$`. 자유 서술·환자 원문이 감사 기록에 들어가지 않습니다.

## 3. 계약 목록

| ID | 모델 | 핵심 필드 | 검증 |
|---|---|---|---|
| I-1 | `PairKey`, `PairStatus` | patient_alias, trial_id / 7개 상태 | 별칭 형식, 전이 표 `ALLOWED_TRANSITIONS`, `check_transition(before, after, authority)` |
| I-2 | `CriterionResult` | pair, rule_id, state, adjudication, decided_by, required_evidence, evidence_satisfied, evidence_fact_ids, inputs_used, gap, evaluator_version, criterion_version | 판정 권한 (§2-3) |
| I-3 | `Action` | action_id, action_type, patient_alias, source_id, target_trial_ids, target_criteria, expected_cost, reason_code, tier, value_score, priority, sequence, safety_required, fingerprint, legacy_action_id, policy_version | target_criteria의 시험 ⊆ target_trial_ids, 키 형식 |
| I-4 | `ActionOutcome` | action_id, status(succeeded/partial/failed/no_response/not_executed), new_fact_ids, cost_charged, charge_keys, latency_minutes, error_code | 실패·무응답·미실행 → 사실 0개, 실패·무응답 → error_code 필수, 부분 성공 → 사실 ≥1, 미실행 → 비용 0 |
| I-5 | `InformationSource` | source_id, kind, provides_fields, request_cost, per_item_minutes, can_verify, alternatives | 자기 자신을 대체로 둘 수 없음 |
| I-6 | `EffortBudget` | coordinator_minutes, clinician_minutes, llm_cost_usd, per_patient_floor_minutes, per_patient_cap_minutes, horizon_steps | floor ≤ cap, floor ≤ 전체 |
| I-7 | `DecisionRecord` (+ `CriterionChange`, `PairTransition`) | schema_version `ctrec.decision_record/v2`, run_id, step, action, outcome, facts_added, criteria, pairs, policy_version, evaluator_version, fact_version_before/after | outcome.action_id = action.action_id, facts_added ⊆ outcome.new_fact_ids, 결과 없이 재평가 금지, 재사용 기준은 판정 불변, 다른 환자 쌍 금지, 전이 표 |
| I-8 | `PolicyEngine` (Protocol), `Plan`, `SkippedAction` | `propose(state) -> list[Action]`, `select(candidates, budget, ledger) -> Plan` | `runtime_checkable` |
| I-9 | `ExperimentSpec`, `DataVersion` | experiment_id, policy_id/version, policy_config, data_version(FROZEN 해시, 코호트 해시), seed, budget, ablations | `spec_hash()`는 experiment_id 제외·ablation 정렬 후 해시 |

## 4. PairStatus

| 상태 | 뜻 |
|---|---|
| CONFIRMED_ELIGIBLE | 열린 경로의 필수 기준이 모두 충족, 남은 확인 없음 |
| CONFIRMED_INELIGIBLE | 불충족 근거가 확정 (자기 보고만이면 아님) |
| VERIFICATION_PENDING | 정보는 있으나 문서 확인·상충 해결이 남음 |
| INFO_MISSING | 판정에 필요한 정보 없음 (질문·문서로 확보 가능) |
| EXPERT_REVIEW | 임상의 판단만 남음 |
| BLOCKED_FAILURE | 확보 행동이 실패·무응답으로 소진되어 보류 |
| NOT_EVALUATED | 평가 실패 (부적격 아님) |

`derive_pair_status(eligibility, gaps, mismatch, exhausted)` — UNCERTAIN일 때 다음에 필요한 작업 종류 순: 정보 없음(missing/stale) > 확인 필요(unverified/conflict) > 임상의 판단(physician_review). 확보 수단 소진(exhausted)은 INFO_MISSING·VERIFICATION_PENDING만 BLOCKED_FAILURE로 바꿈(임상의 판단 대기는 그대로). UNCERTAIN은 어떤 gap 조합으로도 확정 상태가 되지 않습니다(테스트로 전수 확인).

전이 규칙:
- 같은 상태 유지는 항상 허용. 열린 상태(INFO_MISSING 등)와 NOT_EVALUATED에서는 어디로든 갈 수 있음
- 확정 상태에서 반대 확정으로 바로 가지 않음 → VERIFICATION_PENDING / EXPERT_REVIEW / NOT_EVALUATED를 거침
- `authority="policy"`는 `INFO_MISSING|VERIFICATION_PENDING → BLOCKED_FAILURE`(운영상 보류)만 가능. 그 밖의 판정 변화는 판정 계층(`authority="adjudication"`)만

## 5. 승인 시 결정이 필요한 항목 (열린 질문)

1. `PairStatus` 7값과 UNCERTAIN 우선순위 순서가 연구 정의(M5)와 맞는지 — 서연
2. `EffortCost` 축(코디네이터/임상의/LLM/대기)과 단위(분, USD)가 실험 설계와 맞는지 — 서연·동겸
3. `InformationSource` 기본 카탈로그(`engine.bundles.DEFAULT_SOURCES`)의 분 단위 값은 **자리표시값**입니다. 실측·문헌값으로 교체 필요 — 서연
4. 웹 `QuestionChannel` 어댑터가 `Action` → 질문 문구로 바꿀 때 필요한 추가 필드 — 동겸
5. 확정 → 반대 확정 직접 전이 금지 규칙을 유지할지

## 6. 하위 호환 규칙

- 새 필드는 기본값과 함께 **추가만**. 필드 제거·이름 변경·의미 변경은 `contract_version`을 올리고 모든 WP 검토
- `DecisionRecord.schema_version`이 다른 기록은 v2 로더가 거부하고 issue로 보고 (`engine.audit.load_records`)

## 7. 0.2 후보 변경 기록 (2026-11-02, PROVISIONAL 유지)

| 영역 | 변경 | 하위 호환 |
|---|---|---|
| 버전 | `CONTRACT_VERSION = dcvc.engine.contracts/0.2`, `SUPPORTED_CONTRACT_VERSIONS = {0.1, 0.2}`. 목록 밖 버전의 `DecisionRecord`는 거부 | 0.1 기록 그대로 읽음 |
| I-1 쌍 상태 | `derive_pair_status` 안전 경계: ELIGIBLE이라도 필수 기준 gap이 있으면 확정 아님(우선순위 정보 없음 > 확인 필요 > 임상의 판단). INELIGIBLE은 `mismatch == "confirmed"`일 때만 확정(None·none·provisional → VERIFICATION_PENDING). 소진 규칙은 모든 판정에 같게 적용. 알 수 없는 gap 값은 거부. 판정에 적용된 필수 기준이 0개(`required_criteria=0`)면 ELIGIBLE이라도 NOT_EVALUATED(2026-11-05). 신규 `pending_kinds()` | 의미 변경(확정을 내리기만 함). 0.1 테스트 표의 기대값은 모두 유지 |
| I-1 코호트 | `cohort_state.required_gaps`: 판정에 적용된 기준만(OR의 다른 갈래·절차 기준 제외) + 확정 판정의 근거 상충(`conflict_on_decided`)을 conflict로 셈. `PairState.pending_kinds`, `PairState.mismatch` | 필드 추가 |
| I-2 판정 출처 | `decided_by`에 `contract_evaluator`(승인 IR + 질문 계약 명제 사실, LLM 아님). `EvidenceRef`(사실 ID·`prop:<명제 키>`·출처·검증 상태·계약 ID/버전), `CriterionResult.evidence_refs`. 진료 기록 필요 기준은 문서 검증 안 된 근거로 `evidence_satisfied=True` 불가. UNKNOWN은 근거 충족 불가 | 필드 추가(기본 빈 값) |
| 비용 | 축 5개·단위(분, USD) 고정. 대기 최댓값 합산은 같은 단계 병렬 요청에만 유효(문서화). 순차 경과 시간은 시뮬레이터가 계산 | 변경 없음 |
| I-5 정보원 | `cost_basis`(assumed 기본 / literature / measured / elicited) + `cost_reference`(가정값이 아니면 필수). 기본 카탈로그는 모두 assumed | 필드 추가. 카탈로그 해시 값은 바뀜 |
| I-9 실험 | `engine.experiment.require_catalog_hash`·`with_catalog_hash`·`catalog_claims`(가정값이 있으면 상대 비교만) | 신규 함수 |
| I-3 Action | `purpose`(eligibility 기본 / physician_reference — 정책이 gap으로 정함), `info_key`, `question_contract_id`(적격성 환자 질문 전용). 질문 문장 필드 없음 | 필드 추가 |
| I-7 기록 | `PairTransition.reason_code`·`pending_kinds`. 한 기록에 같은 쌍 전이 1개만. `audit.reversal_issues`: 확정에서 내려갈 때 사유 없음 / 새 근거 없이 반대 확정 → issue | 필드 추가. 직접 역전 금지(§4)는 그대로 |
| 연결 계층 | `engine.contract_bridge`(평가기·ContractSession → CriterionResult, 출처 보충), `engine.question_bridge`(Action → 질문, 목적·대상 불변, 검수 계약 우선) | 신규 모듈 |

§5 열린 질문의 처리: 1·2·4·5는 위 표대로 0.2 후보에 반영, 3(정보원 비용 실측값)은 미결정으로 남기고 가정값 표시와 해시 기록으로 대신합니다. 승인 전까지 `CONTRACT_STATUS`는 `PROVISIONAL`입니다.
