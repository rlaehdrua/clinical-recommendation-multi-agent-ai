# 공통 계약 0.2 후보 변경 기록 (`dcvc.engine.contracts/0.1` → `0.2`, PROVISIONAL)

> 2026-11-02, 브랜치 `integration/dcvc`. 승인 전 후보이며 `CONTRACT_STATUS = "PROVISIONAL"`입니다.
> 근거 검토: `docs/dev/contract_decision_review.md` (Q1–Q5, A-1~A-9). 코드: `src/ctrec/engine/contracts.py`. 테스트: `tests/test_engine_contract_v02.py`(35), `tests/test_engine_contracts.py`(0.1 표 유지).

## 1. 원칙
- 확정은 **내리기만** 하고 올리지 않습니다. 기존 판정 값(`TrialMatch.eligibility`, 기준 MET/NOT_MET/UNKNOWN)은 바꾸지 않습니다.
- 새 필드는 모두 기본값이 있어 0.1 데이터가 그대로 읽힙니다. 0.1 계약 테스트의 기대값은 하나도 바꾸지 않았습니다.
- 정책은 판정을 바꾸지 못하고(쌍 상태 전이는 `INFO_MISSING|VERIFICATION_PENDING → BLOCKED_FAILURE`만), LLM은 행동 선택·질문 목적·판정 권한을 갖지 않습니다.

## 2. 쌍 상태 (I-1)

| 입력 | 0.1 | 0.2 | 이유 |
|---|---|---|---|
| ELIGIBLE + missing | CONFIRMED_ELIGIBLE | **INFO_MISSING** | 필수 정보 없음 |
| ELIGIBLE + stale | VERIFICATION_PENDING | **INFO_MISSING** | 우선순위 통일(정보 없음 > 확인 필요 > 임상의 판단) |
| ELIGIBLE + physician_review | CONFIRMED_ELIGIBLE | **EXPERT_REVIEW** | 임상의 판단 대기 |
| ELIGIBLE + unverified / conflict | VERIFICATION_PENDING | VERIFICATION_PENDING | 같음 |
| INELIGIBLE + mismatch None / "none" | CONFIRMED_INELIGIBLE | **VERIFICATION_PENDING** | 문서 검증된 불충족만 확정 |
| INELIGIBLE + provisional / confirmed | VP / CONFIRMED | 같음 | |
| (INELIGIBLE 미확정 / ELIGIBLE 미확정) + exhausted | VP 또는 확정 | **BLOCKED_FAILURE** (INFO_MISSING·VP일 때) | 소진 규칙을 모든 판정에 같게 |
| 알 수 없는 gap 문자열 | 무시 | **ValueError** | 오타가 확정으로 새지 않게 |

- 신규 `pending_kinds(eligibility, gaps, mismatch, exhausted)`: 우선순위로 가려진 사유까지 전체 반환. 추가 종류 `exclusion_unverified`(불충족 근거 미검증), `sources_exhausted`. 확정·평가 실패면 빈 집합.
- `cohort_state.required_gaps(trial)`: 판정에 적용된 기준(`requirement == "required"`)만. **OR의 다른 갈래(`alternative_cohort`)와 절차 기준(`waivable`)은 제외** — 닫힌 갈래의 gap이 열린 경로를 미해결로 만들지 않음. 확정 판정의 근거 상충(`conflict_on_decided`)은 gap이 none이어도 `conflict`로 셈(0.1에서는 놓침).
- 결정적 평가기와의 관계: ContractSession은 평가기 결과를 `TrialMatch`에 병합한 뒤 `decision_state`가 읽으므로, 평가기가 확정한 기준은 gap none, UNVERIFIED는 `unverified`, 상충은 `conflict`로 들어옵니다. 0.2는 이 판정을 덮지 않고 쌍 수준 "확정" 표시만 보수적으로 합니다. 예: 평가기가 자기 보고 허용 기준에서 NOT_MET을 확정했어도 근거가 문서 검증이 아니면 쌍은 VERIFICATION_PENDING(판정은 여전히 INELIGIBLE).
- `PairState`에 `pending_kinds`, `mismatch` 추가.

## 3. 판정 출처와 근거 (I-2)
- `DecidedBy`에 `contract_evaluator` 추가: 승인된 Condition IR + 질문 계약 명제 사실로 판정하는 `ctrec.evaluator`(LLM 아님). `adjudication="rule"`·`"fact_llm"`과 함께 쓸 수 있고 `"expert"`는 여전히 `clinician`만 확정.
- 신규 `EvidenceRef`: `fact_id`, `field`(`prop:<명제 키>` 등), `source_type`, `verification_status`, `contract_id`, `contract_version`. `verified`는 `document_verified`일 때만 참.
- `CriterionResult.evidence_refs` 추가. 검증: refs의 사실 ⊆ `evidence_fact_ids`; `required_evidence="clinical_record"`이고 `evidence_satisfied=True`이면 모든 ref가 문서 검증이어야 함(**미검증 명제를 검증된 임상 근거로 승격 금지**); UNKNOWN은 `evidence_satisfied=True` 불가.
- `cohort_state.criterion_result`: 확정 판정의 근거가 상충하면 `evidence_satisfied=False`.

## 4. 비용 (I-3~I-6)
- 축 5개(`coordinator_minutes`, `clinician_minutes`, `llm_calls`, `llm_cost_usd`, `patient_wait_minutes`)와 단위(분, USD) 고정. 코드 변경 없음, docstring에 다음을 명시:
  - `+`의 대기 최댓값은 **같은 단계에서 병렬로 보낸 요청**에만 유효. 순차 경과 시간은 시뮬레이터(WP6)가 따로 계산.
  - 코디네이터·임상의 예산(`engine.cost.EffortLedger`)과 웹 LLM 비용(`web/budget.py`)은 합치지 않음.

## 5. 정보원과 실험 (I-5, I-9)
- `InformationSource.cost_basis`: `assumed`(기본) / `literature` / `measured` / `elicited`. `assumed`가 아니면 `cost_reference`(ID) 필수. 기본 카탈로그 9개는 모두 `assumed`.
- 신규 `engine/experiment.py`:
  - `require_catalog_hash(spec, catalog)`: `source_catalog_hash`가 없으면 `source_catalog_hash_missing`, 다르면 `source_catalog_hash_mismatch`.
  - `with_catalog_hash(spec, catalog)`: 해시를 채운 새 spec(검증 재수행).
  - `catalog_claims(catalog)`: 가정값 비용이 있으면 `allowed_claims = "relative_policy_comparison_only"`, `absolute_time_claims_allowed = False` → 실제 병원 업무시간 절감을 주장하지 않음.
- 주의: `cost_basis` 필드가 덤프에 들어가므로 `SourceCatalog.digest()` 값이 0.1과 다름.

## 6. Action 질문 연결 (I-3)
- `purpose: "eligibility" | "physician_reference"` (기본 eligibility). 정책(`LegacyAdapterPolicy.purpose_of`)이 판정 메타데이터로 정함: 환자 질문·임상의 검토 행동이고 대상 기준이 **모두** gap `physician_review`일 때만 physician_reference. Action은 frozen이라 렌더러가 바꿀 수 없음.
  - physician_reference는 `ASK_PATIENT`·`CLINICIAN_REVIEW`에만.
- `info_key`: 기존 `decision_state` 정보 키(예: `biomarker:EGFR`)를 옮김(형식이 맞을 때만).
- `question_contract_id`(`^QC-…$`): `ASK_PATIENT` + `eligibility`에만(midterm `question_contract.render`는 항상 eligibility).
- 질문 문장 필드는 여전히 없음.
- 신규 `engine/question_bridge.py`: `render_action`(검수된 활성 계약이면 계약 문장 그대로, 아니면 문구 생성기가 문장·답 형식만 제안), `check_rendered`(목적·대상 불변, 계약 문장 불변), `qa_pair`(목적·계약 ID를 Action에서 가져옴 → physician_reference 답은 `evidence_directness.eligibility_answers`가 근거에서 제외), `attach_question_contract`(활성 계약만).

## 7. 상태 전이와 감사 (I-7)
- 직접 역전(CONFIRMED_ELIGIBLE ↔ CONFIRMED_INELIGIBLE) 금지 유지(모든 권한).
- `PairTransition.reason_code`(선택), `pending_kinds`(확정 상태에는 비어 있어야 함). 표준 사유 코드 `contradicting_evidence`(반대 근거로 확정 → 확인 대기), `evidence_reverified`(별도 확인 뒤 다시 확정) 등 `REVERSAL_REASON_CODES`.
- `DecisionRecord`: 한 기록에 같은 쌍의 전이는 1개(확정 → 대기 → 반대 확정을 한 기록에서 잇는 우회 금지). `contract_version`은 `SUPPORTED_CONTRACT_VERSIONS` 안이어야 함.
- `audit.reversal_issues(records)` (로드 시 자동): 사유 없이 확정에서 내려감 → `unexplained_confirmed_downgrade:<step>`, 새 사실 없이 반대 확정 → `reversal_without_new_evidence:<step>`.
- `make_record(..., pair_reasons=, pair_pending=)`, 정책 전이(`apply_outcome`)는 `reason_code="sources_exhausted"`와 남은 작업을 기록. v2 뷰에 사유·남은 작업 표시.

## 8. midterm 계약 계층 연결 (`engine/contract_bridge.py`)
| 기존 모듈(재사용) | 엔진 쪽 |
|---|---|
| `evaluator.evaluate` → `CriterionAssessment(method="contract")` + provenance | `criterion_result_from_assessment` → `CriterionResult(decided_by="contract_evaluator")` |
| `proposition_store.PropFact` | `evidence_ref` → `EvidenceRef(field="prop:<키>", 검증 상태, 계약 ID/버전)` |
| `ContractSession.matches` + `contract_provenance` + `ledger` | `criterion_overrides_from_session` → `CohortState.from_decision_states(criterion_overrides=…)` |
| `ContractRegistry.deciding` | `contract_candidates`(행동 대상 기준의 미확인 명제를 정할 활성 계약) |

안전 규칙: 판정 값이 기존 상태와 다르면 `OverrideError`; expert 기준을 평가기가 확정했으면 `BridgeError`; 출처의 사실이 장부에 없으면 `BridgeError`; 원래 환자 ID 거부. 평가기·명제·질문 계약 로직은 다시 구현하지 않았습니다.

## 9. 미결정 (승인 회의 안건)
1. `RuleMeta.adjudication`(M12) 정의 — 그전까지 `provisional_adjudication`(underspecified → expert, structured_evaluable → rule, 그 밖 → fact_llm).
2. `decision_state._record`의 `contract → llm` 기록을 고칠지(보호 파일) 또는 overrides 방식을 유지할지.
3. 웹 기본 실행 경로(`CTREC_SESSION` 기본값) — 지금은 기존 Session.
4. 쌍 상태 "확정 부적격"을 문서 검증 불충족으로만 정의한 것(자기 보고 허용 기준 포함)이 M3·M5 연구 정의와 맞는지.
5. 정보원 비용 실측·문헌값과 `cost_reference` ID 체계.
6. 승인 시 `CONTRACT_STATUS = "APPROVED"`로 바꾸고 이 문서를 확정본으로.

## 0.2 후보 추가 (2026-11-04, WP5): 실패 범위

- 문제: 환자가 질문 하나에 "모름"이라고 답해도 `ActionOutcome(no_response)` 하나로 그 환자의 `patient_interview` 정보원 전체가 실패 처리되어, 다른 문진 질문까지 중단되고 대상 시험이 `BLOCKED_FAILURE`가 됨. masked 101건 오프라인 실행의 `BLOCKED_FAILURE` 104건이 모두 이 경로.
- 계약 추가(필드 변경 없음, 상수·함수만): `NO_ANSWER = "no_answer"`(항목 단위), `UNREACHABLE = "unreachable"`·`DECLINED_ALL = "declined_all"`(정보원 단위), `ITEM_FAILURE_CODES`, `SOURCE_FAILURE_CODES`, `failure_scope(status, error_code)`. 모르는 코드는 정보원 단위(기존 동작). `no_answer`는 `record_producer.NO_ANSWER_ERROR`와 같은 값.
- 운영 상태: `OpsState.failed_items`(정보원, 항목 키), `OpsState.exhausted_items`(남은 정보원이 없는 항목). 항목 키는 `decision_state.info_key`(구조화 필드·바이오마커는 시험 사이에서 공유).
- 쌍 소진(Q-D2): 판정에 필요한 기준의 남은 gap이 모두 얻을 수 있는 정보(missing·stale·unverified)이고 그 항목이 전부 소진됐을 때만 `BLOCKED_FAILURE`. 일부만 소진됐거나 임상의 판단·상충이 남아 있으면 기존 상태 유지.
- 무응답 판별은 `engine.answers.answer_outcome`(= `question_contract.non_answer`) 하나로 통일. 실행기는 이 함수로 `error_code`를 정함.

