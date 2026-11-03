# WP6 오프라인 시뮬레이터·실험 실행기 설계 (초안)

> 2026-11-03 · 작성: 서연 · 상태: **설계 초안, 코드 변경 없음**
> 기준: `origin/integration/dcvc` `b0a429a`. 계약 0.2 **PROVISIONAL**.
> 목표 연구 항목: M9(실패·무응답·대체 정보원), M10(같은 조건의 기준선·ablation 비교) — `docs/audit/02_M1_M14_gap_analysis.md`.
> 완료 조건(`docs/audit/03_architecture_and_work_packages.md` WP6): 같은 spec을 2회 실행하면 결과 해시가 같고, 기준선 5종 이상을 실행할 수 있어야 함.
> 실제 LLM·유료 API 호출은 이 설계 범위 밖입니다. 오프라인 실행은 LLM 호출 0건입니다.

---

## 0. 선행 조건과 현재 상태

| 선행 | 상태 | WP6에서 쓰는 것 |
|---|---|---|
| WP0 계약 I-1~I-9 | 0.2 PROVISIONAL | `ExperimentSpec`, `DataVersion`, `EffortBudget`, `Action`/`ActionOutcome`, `PairStatus`, `DecisionRecord` |
| WP1 평가 진입점 | PR #1 병합 | 별칭(`eval_hygiene.AliasMap`), `PromptLeakGuard`, `metrics.cohort_eligibility_metrics`(B-2) |
| WP2 NSCLC 데이터 | 시험 8개 선정, 슬롯 결정 초안(`feat/wp2a-nsclc-trials`, `docs/dev/wp2b_slot_decisions.md`), **임상 검수 전** | 시험 저장소(별도 `store_dir`), 슬롯 사전, 사전 분포 |
| WP5 정책 엔진 | 통합됨 (`LegacyAdapterPolicy`, `EffortLedger`, `SourceCatalog`) | 정책 프로토콜 `propose`/`select`/`apply_outcome` |
| WP7 감사 | PR #2 병합, PR #3(역전 지표) 리뷰 중 | `record_producer`, `reversal_metrics` |
| 코호트 생성기 | `origin/research/cohort-generator`의 `src/ctrec/cohort/**` (신증후군 소아 예시, 통합 전) | public/oracle 분리, `truth.holds3`, `validate` |

**WP6을 다시 만들지 않는 것**: 정책 엔진(WP5), 명제·질문 계약·결정적 평가기, 코호트 생성기 본체. WP6은 이것들을 연결하는 **환경과 실행기**만 만듭니다(`team_parallel_start.md` §1).

---

## 1. 구성 요소

```text
ExperimentSpec ──► Runner ──────────────────────────────────────────────► manifest.json + results/
                    │                                                       (spec_hash, result_hash,
                    ├─ load: 코호트(public/oracle), 시험 저장소, SourceCatalog     catalog_claims, metrics)
                    ├─ require_catalog_hash(spec, catalog)   ← 실행 전 필수
                    └─ 단계 반복 (horizon_steps 또는 종료 조건까지)
                         policy.propose / select (public만 봄)
                         └─► CohortEnvironment.execute(action)
                                ResponseModel(seed) → status·지연·드러난 슬롯
                                oracle 값 → 새 사실 (실패·무응답·모름이면 0개)
                             ◄── ActionOutcome
                         판정 계층 재평가 (오프라인 결정적 평가기, §3.3)
                         policy.apply_outcome (운영 상태만)
                         record_producer → DecisionRecord v2
```

| 구성 요소 | 파일 (예정) | 책임 | 하지 않는 것 |
|---|---|---|---|
| `CohortEnvironment` | `src/ctrec/sim/environment.py` | oracle을 쥐고 행동을 실행해 `ActionOutcome`과 새 사실을 돌려줌 | 판정·정책 결정 |
| `ResponseModel` | `src/ctrec/sim/response_model.py` | 정보원별 성공·부분·실패·무응답·지연을 seed로 결정 | 사실 값 결정 (값은 oracle) |
| `OfflineEvaluator` | `src/ctrec/sim/evaluator.py` | 공개·확보된 사실만으로 기준 상태 재계산 (LLM 없음) | 숨긴 값 읽기 |
| `Runner` | `src/ctrec/sim/runner.py`, `scripts/run_cohort_experiment.py` | spec 검증, 단계 반복, 기록·지표·manifest | 정책 내부 변경 |
| 기준선 정책 | `src/ctrec/sim/baselines.py` | `PolicyEngine` 프로토콜의 **새 클래스**들 | `LegacyAdapterPolicy.select`·`EffortLedger` 규칙 수정 |

---

## 2. 입력과 `ExperimentSpec` 대응

| `ExperimentSpec` 필드 | WP6에서 채우는 값 | 비고 |
|---|---|---|
| `policy_id`, `policy_version`, `policy_config` | 기준선·제안 정책 ID와 설정 | `config_hash`로 비교 |
| `data_version.frozen_store_hash` | NSCLC 시험 저장소(`data/nsclc/...` 별도 `store_dir`)의 FROZEN 해시 | 기존 `data/parsed` FROZEN과 섞지 않음 |
| `data_version.cohort_hash` | 생성기 `config_hash`(슬롯 사전·카탈로그·생성 설정) + 시나리오 ID + replicate | 같은 코호트 = 같은 해시 |
| `data_version.source_catalog_hash` | `engine.bundles.SourceCatalog.digest()` | **필수**. 없거나 다르면 실행 거부(`require_catalog_hash`) |
| `seed` | 실행 seed. ResponseModel 난수의 뿌리 | 정책은 결정적이어도 응답 모델은 seed를 씀 |
| `budget` | `EffortBudget`(코디네이터·임상의 분, 환자별 하한·상한, `horizon_steps`) | 생성기 `budgets_min`(30/60/120/240)을 예산 격자로 사용 |
| `ablations` | §5의 ablation 이름 | 정렬된 뒤 해시됨 |

### 2.1 정보원 카탈로그 두 개를 맞추기 (핵심 쟁점)

지금 형식이 다른 카탈로그가 두 개 있습니다.

| | 엔진 `engine/bundles.SourceCatalog` (`InformationSource`) | 생성기 `cohort/spec.SourceCatalog` (`SourceSpec`, 통합 전) |
|---|---|---|
| ID 예 | `patient_interview`, `lab_report`, `pathology_report`, `medication_list`, `medical_record`, `clinician_review` | `S_phone`, `S_lab`, `S_path`, `S_med`, `S_summary`, `S_chart`, `S_md` |
| 비용 | `EffortCost` 5축 + `per_item_minutes` + `cost_basis` | `cost_min`, `cost_range`, `basis`(문자열) |
| 드러나는 것 | `provides_fields`(사실 항목 이름: `lab:*`, `biomarker`…) | `reveals`(슬롯 패턴: `lab.*`, `bm.*`…) |
| 대체 | `alternatives` | 없음 |

**제안**: 실험의 비용·청구·해시는 **엔진 카탈로그 하나만** 기준으로 합니다(`source_catalog_hash`가 가리키는 것). 생성기 카탈로그는 "어느 정보원이 어느 슬롯을 드러내는가"에만 쓰고, 다음 대응표를 데이터 파일로 둡니다(`data/nsclc/cohort/source_map.json`, 예정).

| 생성기 | 엔진 | 슬롯 → 사실 항목 |
|---|---|---|
| `S_phone` | `patient_interview` | `repro.*`, `med.*`, `tx.*` → `reproductive`, `medication`, `treatment_history` |
| `S_lab` | `lab_report` | `lab.*` → `lab:<표준 검사명>` |
| `S_path` | `pathology_report` | `bm.*`, `dx.histology` → `biomarker:*`, `diagnosis` |
| `S_med` | `medication_list` | `med.*` → `medication` |
| `S_summary`, `S_chart` | `medical_record` | `tx.*`, `dx.*`, `cm.*`, `ecog`, `other.*` |
| `S_md` | `clinician_review` | `judge.*` (정답 없음, §3.4) |

- 생성기의 `cost_min`·`cost_range`는 엔진 카탈로그의 **민감도 분석 범위**로만 옮깁니다. 기본값은 엔진 카탈로그입니다. 두 값이 다르면 manifest에 둘 다 남깁니다.
- 대응표 자체도 해시해서 `cohort_hash`에 넣습니다(대응이 바뀌면 다른 실험).
- 생성기 브랜치 작성자와 합의해야 합니다(§8 Q-G1).

### 2.2 계약 필드 대응 (I-4 `ActionOutcome` · I-5 `InformationSource`)

**I-5 `InformationSource` ← 생성기 `SourceSpec` + `ResponseModel` 설정**

| I-5 필드 | WP6에서 채우는 값 | 생성기 쪽 대응 |
|---|---|---|
| `source_id` | 엔진 ID (`lab_report` 등) | `S_lab` 등 → §2.1 대응표 |
| `kind` | document / patient / clinician / system | `evidence`: record → document, self_report → patient, physician → clinician |
| `provides_fields` | 사실 항목 이름 패턴 | `reveals`(슬롯 패턴) → 슬롯-항목 대응 |
| `request_cost` | `EffortCost` (능동 분, 예상 대기) | `cost_min` → `coordinator_minutes` (민감도 범위 `cost_range`) |
| `per_item_minutes` | 엔진 카탈로그 값 | 없음 (생성기는 정보원당 비용만) |
| `can_verify` | document·clinician = True, patient = False | `evidence == "record"` |
| `alternatives` | 엔진 카탈로그 값 | 없음 |
| `cost_basis`, `cost_reference` | `assumed` (문헌 근거가 있으면 `literature` + ID) | `basis` 문자열 (예: Penberthy 2012 인용) → `cost_reference` ID로 옮길지 §8 Q-G1 |

`ResponseModel` 파라미터(성공률·부분·무응답·지연)는 I-5에 넣지 않습니다. I-5는 정책이 보는 카탈로그이고, 응답 확률은 정책에 보이면 안 되는 시뮬레이터 내부 값이기 때문입니다(계약 설계 원칙 5).

**I-4 `ActionOutcome` ← `CohortEnvironment.execute` 결과**

| I-4 필드 | 값 | 규칙 |
|---|---|---|
| `action_id` | 실행한 `Action.action_id` | 같아야 함 (계약 검사) |
| `status` | `ResponseModel` 결과: succeeded / partial / failed / no_response / not_executed | DEFER·FINALIZE는 not_executed |
| `new_fact_ids` | 드러난 oracle 값으로 만든 사실 ID | failed·no_response·not_executed·"모름"은 빈 값 (계약 검사). partial은 1개 이상 |
| `cost_charged` | 능동 분 (§3.2.1) | not_executed면 0. 실패·무응답도 청구 |
| `charge_keys` | `charge_key(alias, source_id)` | 같은 (환자, 정보원) 중복 청구 0 |
| `latency_minutes` | `ResponseModel`의 지연 | 경과 시간 축 (§3.2.1) |
| `error_code` | failed·no_response일 때 필수 (예: `source_no_response`, `source_failed`) | "모름"의 코드는 동겸 `answer_unknown` 확정 후 (§7) |
| `resolved_criteria` | 이 행동으로 값이 확보된 대상 기준 | 판정은 재평가가 정함 (보고만) |


---

## 3. 시뮬레이션

### 3.1 단계

1. `CohortState`를 public 값만으로 만듭니다. oracle은 정책·평가기에 넘기지 않습니다(생성기 `policy_view()` 원칙).
2. `policy.propose` → `policy.select`(예산 안에서 고름). `Plan.safety_unfunded`가 있으면 그 환자의 다른 행동은 진행하지 않습니다(WP8 규칙과 같음).
3. 선택된 행동마다 `CohortEnvironment.execute(action)`:
   - 정보원 = `action.source_id`. `ResponseModel`이 status와 지연을 정합니다.
   - `succeeded`/`partial`: 그 정보원이 드러내는 슬롯 중 행동의 `required_fields`에 해당하는 oracle 값을 사실로 만듭니다(`partial`은 일부만).
     출처는 정보원 종류에 따라 `self_report`(환자) 또는 `clinical_record` + 문서 검증(`document_verified`, `can_verify=True`일 때)으로 둡니다.
   - `failed`/`no_response`: 새 사실 0개, `error_code` 필수(계약이 강제). 비용은 청구합니다(M9 테스트 조건).
   - "모름"(환자 질문): 새 사실 0개. 표현 방식은 동겸 WP5 `answer_unknown` 확정 전까지 TODO(§7).
4. 판정 계층 재평가(§3.3) → 쌍 상태 변화.
5. `policy.apply_outcome`(운영 상태만: 소진, 대체 정보원 순서).
6. `record_producer`로 `DecisionRecord` v2 기록. 차단 진단(`direct_confirmed_reversal` 등)은 진단 파일에 남깁니다.
7. 종료: `horizon_steps` 도달, 예산 소진, 고를 행동 없음, 모든 쌍이 확정이나 `EXPERT_REVIEW`. 종료 사유는 manifest에 남깁니다.

### 3.2 `ResponseModel`

- 정보원별 파라미터: 성공률, 부분 응답률, 무응답률, 지연 분포, (환자) "모름" 비율. **모두 가정값**이며 manifest에 `basis: assumed`로 남깁니다.
- **난수는 행동마다 독립 스트림**: `derive_seed(spec.seed, patient_alias, source_id, attempt_no)`. 정책이 행동 순서를 바꿔도 같은 (환자, 정보원, 시도 번호)는 같은 결과를 받습니다(공통 난수). 정책 간 차이가 응답 운이 아니라 선택 차이에서 나오게 하기 위함입니다.
  생성기의 `derive_seed(base_seed, scenario_id, replicate)`와 같은 방식(해시 기반)을 씁니다.
- 대체 정보원: 실패·무응답 뒤 `InformationSource.alternatives`의 다음 정보원을 **정책이** 고릅니다. 환경은 대체를 자동으로 실행하지 않습니다.
- 경과 시간: `EffortCost.__add__`의 대기 최댓값은 같은 단계의 병렬 요청에만 맞습니다. 달력 경과 시간은 **단계별 최대 대기의 합**으로 Runner가 따로 계산합니다(계약 0.2 변경 기록).

### 3.2.1 능동 시간과 경과 시간 분리 (M2)

M2(코디네이터 능동 작업시간 예산)의 원칙대로, 시간은 두 축으로 따로 셉니다. 둘을 더하지 않습니다.

| 축 | 무엇 | 출처 필드 | 예산 차감 | 결과표 열 |
|---|---|---|---|---|
| 능동 시간 | 코디네이터·임상의가 실제로 일한 분 | `EffortCost.coordinator_minutes`, `clinician_minutes` (청구는 `ActionOutcome.cost_charged`) | **예** (`EffortBudget.coordinator_minutes`·`clinician_minutes`, `EffortLedger`) | 능동 분 (코디네이터 / 임상의) |
| 경과 시간 | 요청부터 응답까지 기다린 시간 | `EffortCost.patient_wait_minutes`(예상), `ActionOutcome.latency_minutes`(실제, `ResponseModel`이 정함) | **아니요** | 달력 경과 시간 = 단계별 `max(latency_minutes)`의 합 |

- 실패·무응답도 능동 시간은 청구합니다(요청 작업은 했음). 경과 시간은 무응답 타임아웃까지 셉니다.
- LLM 비용(`llm_calls`, `llm_cost_usd`)은 세 번째 축이며 오프라인 실행에서는 0입니다. 웹 `web/budget.py` 원장과 합치지 않습니다.
- 정책 비교표에는 능동 시간과 경과 시간을 각각 보고합니다. "능동 시간을 줄였지만 경과 시간은 늘었다" 같은 맞바꿈이 드러나게 하기 위함입니다.

### 3.3 오프라인 판정 (LLM 없음)

- 구조화 기준(`rule`): 기존 규칙 엔진(`tools/rule_engine`)으로 재계산합니다. 생성기 `truth.py`가 이미 같은 함수를 씁니다.
- `fact_llm` 기준(WP2b 후보 기준 약 44%): 오프라인에서는 LLM 대신, 생성기 슬롯 사전의 검수된 연결(`categorical`)을 **확보된 사실에만** 적용하는 결정적 평가기를 씁니다. 3값 논리이며, 모르는 값이면 UNKNOWN입니다.
  - 이 평가기는 "LLM이 완벽하게 읽었다면"의 상한입니다. 실제 LLM 판정 오류는 반영하지 않습니다. 결과 해석에 명시합니다.
  - 숨긴 값은 읽지 않습니다. oracle 정답 계산(`truth.holds3` + 숨긴 값)과는 입력이 다릅니다.
- `expert` 기준과 `judge.*` 슬롯: 자동 확정하지 않고 `EXPERT_REVIEW`로 둡니다. `clinician_review` 행동이 성공하면 oracle에 정답이 없으므로 **판정하지 않고** 상태만 남깁니다(정답 없는 기준).
- WP2b D4 `UNRESOLVED`(기준값 없는 "Adequate … function"): 정답 라벨 없음. 정확도 지표에서 빼고 따로 셉니다.
- WP4 부분 재평가: 들어오면 `rule` 기준만 부분 재평가하고 나머지는 전체 재평가 폴백입니다(WP2b D6). ablation "부분 재평가 끔"은 항상 전체 재평가입니다. 두 결과의 판정은 같아야 합니다(`compare_states`).

### 3.4 정답(oracle)과 라벨

- 쌍 라벨은 생성기가 만듭니다: 정답 계산에 쓴 기준이 모두 통과하면 ELIGIBLE, 하나라도 불통과면 INELIGIBLE, 정답 없는 기준(`expert`, `UNRESOLVED`, 검수 전 `unbound`)이 결과를 가를 수 있으면 **"판정 불가"**.
- 생성기는 검수 전 기준을 "충족으로 간주"(`assume_pass`)합니다. WP6 지표에서는 이런 쌍을 따로 표시하고 기본 지표에서 뺍니다. 검수 전 데이터로 성능을 주장하지 않기 위해서입니다.

---

## 4. 지표

| 축 | 지표 | 출처 |
|---|---|---|
| 정확성 | 쌍 단위 적격/부적격 혼동표 (라벨 있는 쌍만), 누락 = 오답으로 보는 지표 | `metrics.cohort_eligibility_metrics`(B-2) |
| **안전** | 확정 적격인데 정답 부적격인 수(**0이어야 함**), 확정 부적격인데 정답 적격인 수 | 쌍 상태 × 라벨 |
| 진척 | 단계·예산별 확정 쌍 비율, 남은 `pending_kinds` 분포 | `PairTransition` |
| 비용 | 5축(코디네이터·임상의 분, LLM 호출·USD, 환자 대기) + 달력 경과 시간 | `EffortLedger`, Runner |
| 계약 | 예산 초과 0, 중복 청구 0, 차단 진단 수 | `EffortLedger`, `record_producer` |
| 역전 | 확정 역전(경유 유형별), 직접 역전 위반, 미확인 역전 | PR #3 `reversal_metrics` (병합 후) |
| 공정성 | 환자별 배정 시간의 Gini, `safety_unfunded` 환자 수 | `engine/fairness.allocation_report` |
| 환자 부담 | 환자당 질문 수, "모름" 응답 수 | Runner |

- 주장 범위: `catalog_claims(catalog)`를 manifest에 그대로 남깁니다. 가정 비용이 하나라도 있으면 분 단위 결과는 **정책 간 상대 비교에만** 씁니다(`relative_policy_comparison_only`).
- 반복: 시나리오 replicate(생성기 `replicates`, `dev_replicates`로 dev/test 분리)와 응답 seed를 반복합니다. 신뢰구간은 환자 단위 부트스트랩으로 구합니다. **test replicate는 정책 조정에 쓰지 않습니다.**

## 5. 기준선과 ablation (5종 이상)

| ID | 종류 | 내용 | 구현 |
|---|---|---|---|
| `random` | 기준선 | 후보 행동 중 seed로 무작위 선택 (예산 안) | 새 `PolicyEngine` |
| `patient_order` | 기준선 | 환자 순서대로 한 명씩 끝까지 | 새 클래스 |
| `cheapest_first` | 기준선 | 예상 비용이 낮은 행동부터 | 새 클래스 |
| `legacy_greedy` | 기준선(현행) | `LegacyAdapterPolicy` 그대로 | 기존 |
| `oracle_upper` | 상한 참고 | 숨긴 값을 아는 선택 (**상한 계산용**, 정책 비교표와 분리) | 새 클래스, oracle 접근은 이 클래스만 |
| 제안 정책 | 후보 | WP5 후속 정책 (동겸) | 새 클래스 |
| `-bundles` | ablation | 정보원 묶음 끔 (생성기 `grouping=per_slot`과 같은 효과) | `policy_config` |
| `-partial_reeval` | ablation | 부분 재평가 끔 (항상 전체) | 평가기 설정 |
| `-evidence_level` | ablation | 근거 수준(문서 검증 요구) 끔 — 안전 지표가 나빠지는지 보는 용도 | 평가기 설정 |
| `-fairness_floor` | ablation | `per_patient_floor_minutes = 0` | `EffortBudget` |
| `null` 시나리오 | 대조 | 생성기 영 시나리오(환자당 시험 1개, 균일 비용, 묶음 없음) | 코호트 |

- 모든 정책은 같은 코호트·같은 응답 seed·같은 카탈로그(`source_catalog_hash` 동일)로 돌립니다(M10).
- `tests/test_engine_policy.py`의 조건(예산 초과 0, 중복 청구 0, 결정성, 환자 1명 동등성, 안전 행동 보존)을 새 클래스도 통과해야 합니다.

## 6. 결정성·manifest

- 결과 해시 = 단계별 `DecisionRecord` 직렬화 + 지표의 정규화 해시(`canonical_hash`). 같은 spec 2회 → 같은 해시(완료 조건).
- 시간·경로·호스트 이름은 해시에 넣지 않습니다(manifest에는 따로 기록).
- manifest 필드: `spec`, `spec_hash`, `result_hash`, `contract_version`, `catalog_claims`, `source_map_hash`, `generator_config_hash`, 종료 사유별 수, 실행 환경(파이썬·패키지 버전), 코드 커밋.
- 실행 전 검사: `require_catalog_hash`, 계약 버전 지원 여부, 코호트 `validate()` 통과, LLM 키가 있어도 오프라인 실행은 LLM 호출 0건(검사로 강제).

## 7. 계약 0.2 확정·다른 WP 결정 후 (의존)

1. **안건 1 `RuleMeta.adjudication`**: §3.3의 rule/fact_llm/expert 나누기와 오프라인 평가기 범위가 바뀜. 결정 전에는 `contract_bridge.provisional_adjudication`과 WP2b 후보값을 씀.
2. **안건 5 비용 실측·문헌값**: `cost_basis`가 `assumed`가 아니게 되면 분 단위 절대 주장 가능 여부가 바뀜.
3. **동겸 `answer_unknown`**: TODO — "모름"을 `ActionOutcome`에 어떻게 기록할지, 항목 단위 소진을 쌍 단위 `exhausted`로 어떻게 합칠지. 확정 전에는 "모름" = 새 사실 0 + 별도 카운트만 남김.
4. **PR #3 병합**: 역전 지표 열은 병합 뒤 추가.
5. **WP4 부분 재평가**: 들어오기 전에는 `-partial_reeval`과 기본이 같음(ablation 비활성으로 표시).
6. **생성기 통합**: `research/cohort-generator`가 `integration/dcvc`에 들어와야 WP6 코드를 시작할 수 있음.

## 8. 확인 질문

- **Q-G1 (생성기 브랜치 작성자)** 생성기 `SourceCatalog`를 엔진 카탈로그 + 대응표(§2.1)로 바꾸는 데 동의하나요? 아니면 생성기 카탈로그에서 엔진 카탈로그를 만드는 변환을 둘까요?
- **Q-G2 (생성기)** NSCLC용 슬롯 사전·생성 설정은 `data/nsclc/cohort/`에 두면 되나요? (지금 예시는 `data/cohort/`의 신증후군 소아)
- **Q-G3 (생성기)** `assume_pass`(검수 전 기준을 충족으로 간주)를 WP6에서 끌 수 있는 옵션이 있나요? 없으면 라벨을 "판정 불가"로 바꾸는 후처리를 WP6에서 할까요?
- **Q-D1 (동겸)** 기준선 정책 클래스(`random`, `patient_order`, `cheapest_first`)를 WP6(`src/ctrec/sim/baselines.py`)에 둬도 되나요, WP5 쪽에 둘까요?
- **Q-D2 (동겸)** `answer_unknown` 표현이 정해지면 `ResponseModel`의 "모름" 결과를 그대로 맞추겠습니다. 예상 시점이 있나요?
- **Q-D3 (동겸)** 정책이 대체 정보원을 고를 때 시도 번호(`attempt_no`)를 `Action`에서 알 수 있나요? (공통 난수 스트림 키에 필요)
- **Q-J1 (공정현)** 오프라인 평가기를 `sim/` 아래 새 모듈로 두는 것이 "결정적 평가기를 다시 만들지 않는다" 규칙과 충돌하나요? 충돌하면 `evaluator.py`를 그대로 쓸 수 있는 범위를 정해 주세요.
- **Q-J2 (공정현)** NSCLC 시험 저장소 FROZEN 해시를 `frozen_store_hash`에 넣는 방식(별도 `store_dir`)으로 진행해도 되나요?

## 9. 테스트 계획 (구현 단계)

- 같은 spec 2회 → `result_hash` 동일. seed만 바꾸면 응답 결과가 달라짐.
- `source_catalog_hash` 없음·불일치 → 실행 거부.
- 실패·무응답·모름 → 새 사실 0, 비용은 청구.
- 정책·평가기에서 oracle 접근 0 (`oracle_upper` 제외). 별칭 외 환자 ID 누출 0.
- **oracle import·접근 금지 테스트**: 생성기 `OraclePart`의 세 필드 `hidden`(숨긴 슬롯 실제 값), `truth`(기준 실제 성립), `pair_label`(쌍 정답)은 `CohortEnvironment`와 Runner의 지표 계산만 읽습니다.
  - 정적 검사: `src/ctrec/sim/baselines.py`, `sim/evaluator.py`, `engine/**`에 `OraclePart`·`Cohort.oracle` import가 없고 `.hidden`·`.truth`·`.pair_label` 속성 참조도 없음(AST 검사). 예외는 `oracle_upper` 클래스 하나이며 허용 목록으로 명시합니다.
  - 동적 검사: 정책·평가기에는 `policy_view(cohort)`(PublicPart)만 넘깁니다. `PublicPart`에 `hidden`·`truth`·`pair_label` 키가 없음을 확인합니다. 정책이 받은 객체에 oracle 값(숨긴 슬롯 값)이 문자열로도 나타나지 않음을 확인합니다(`PromptLeakGuard`와 같은 방식).
- seed 고정: 같은 `spec.seed`면 `ResponseModel` 결과가 같고, 정책 순서를 바꿔도 같은 (환자, 정보원, 시도 번호)는 같은 결과(공통 난수, §3.2).
- 확정 적격 ∧ 정답 부적격 = 0 (기본 정책, 근거 수준 켬).
- 환자 1명·시험 1개 작은 사례에서 완전탐색 결과와 시뮬레이터 결과 일치(생성기 원칙과 같음).
- 오프라인 실행 LLM 호출 0건.
- 테스트는 `PYTHONUTF8=1 env -u ANTHROPIC_API_KEY`로 실행, 전체 실패 0.
