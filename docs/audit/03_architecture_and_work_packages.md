# 03. 권장 공통 엔진 아키텍처와 작업 패키지

> 기준 커밋 `62f744e`. 이 문서의 아키텍처·작업 분할은 **제안 [추정]**이며, 근거가 된 현재 코드 사실은 01·02 문서에 있습니다.
> 담당자는 정하지 않고 **역할(Role)** 단위로만 나눕니다.

## 1. 현재 모듈 의존 구조 [확인]

```
web/app.py ──> orchestrator.py ──> pipeline.Session ──> agents/* (LLM) ──> llm.py
     │                                   │  ├─> tools/rule_engine.py, evidence.py, status.py, acquisition.py
     │                                   │  ├─> trial_store.py (Frozen)
     │                                   │  └─(진단)─> profile_facts -> fact_store
     │                                   │            ├─> incremental -> dependency_graph -> fact_view -> fact_changes
     │                                   │            └─> decision_loop -> decision_policy -> decision_state
     ├─> web/budget.py, web_metering.py, call_guard.py
     └─> web/display.py (recommender 로직 일부 중복)
question_eval.py / question_priority.py ──> pipeline.Session(run_fixed) + decision_policy  (오프라인 전용)
inspector.py ──> result.json / trace.jsonl / facts 파일 (사후 해석)
```
- 의존 방향은 대체로 한쪽으로 흐릅니다. `fact_view`·`dependency_graph`·`decision_state`가 `agents.matcher`의 분류 함수(`is_askable_unknown` 등)를 가져다 써서, 판정 규칙이 진단 계층으로 새어 들어갑니다. 이 방향 자체는 바람직합니다(판정 규칙을 한 곳에 둠).
- 변경 집중 파일(git 이력, 48커밋 기준): README.md 17, web/app.py 15, pipeline.py 14, static/index.html 12, schemas.py 9, matcher.py 8, cli.py 8, config.py 8.

## 2. 권장 아키텍처: 4계층 + 실행 어댑터

```
┌──────────── 실행 어댑터 (Execution) ─────────────┐
│  OfflineSimulator            WebRunner (web/app)  │
│  - CohortEnvironment         - WebQuestionChannel │
│  - ResponseModel(seed)       - legacy|engine 모드 │
└───────────────┬───────────────────┬───────────────┘
                │ 같은 함수 호출 │
┌───────────────▼───────────────────▼───────────────┐
│ 정책 계층 (Policy)  src/ctrec/engine/               │
│  propose(state) -> [Action]   (순수, LLM 없음)      │
│  select(cands, budget) -> Plan                      │
│  apply_outcome(state, outcome) -> state'            │
└───────────────┬────────────────────────────────────┘
                │ 읽기 전용 상태
┌───────────────▼────────────────────────────────────┐
│ 판정 계층 (Adjudication)  — 기존 matcher/rule_engine │
│  evaluate(pair, facts, scope) -> CriterionResult[]  │
│  (임상 판정 규칙의 유일한 소유자. 정책이 판정을 쓰지 못함) │
└───────────────┬────────────────────────────────────┘
┌───────────────▼────────────────────────────────────┐
│ 데이터 계층 (Data) — FactStore, Frozen TrialStore,   │
│  InformationSource 카탈로그, 정답(시뮬레이터 전용, 격리) │
└────────────────────────────────────────────────────┘
감사 계층 (Audit): 모든 계층이 DecisionRecord(스키마 있음)를 내보냄 -> trace / Inspector
```

### 핵심 원칙
1. **판정과 정책의 분리**: 정책 계층은 `CriterionResult`를 읽기만 합니다. 판정 변경은 판정 계층의 `evaluate`를 거쳐야만 일어납니다. 테스트에서 import 방향 검사(engine → agents.matcher의 쓰기 API 금지)로 강제합니다.
2. **LLM은 정책 결정에 쓰지 않음**: LLM은 판정 계층(사실 판정)과 표현(ActionRenderer)에만 씁니다.
3. **정답 격리**: 시뮬레이터의 정답·숨긴 사실은 `OfflineSimulator` 안에만 있고 정책·판정 계층으로 가는 객체에 들어가지 않습니다. 케이스 ID는 실행 전에 **불투명 별칭**으로 바꿉니다(P0-2).
4. **기존 경로 보존**: 웹은 `policy_mode="legacy"`(기본, 현재 동작)와 `"engine"`을 둡니다. 기존 테스트는 legacy로 계속 통과해야 합니다.

## 3. 먼저 확정해야 할 공통 인터페이스 (구현 전 동결 대상)

| ID | 인터페이스 | 최소 필드 (제안) | 기존 코드와의 연결 |
|---|---|---|---|
| I-1 | `PairKey` / `PairStatus` | patient_alias, trial_id, status ∈ {CONFIRMED_ELIGIBLE, CONFIRMED_INELIGIBLE, VERIFICATION_PENDING, INFO_MISSING, EXPERT_REVIEW, BLOCKED_FAILURE, NOT_EVALUATED} | `TrialMatch.eligibility` + `Gap`에서 파생 (02 M5) |
| I-2 | `CriterionResult` (판정 출력) | rule_id, state(MET/NOT_MET/UNKNOWN), adjudication(rule/fact_llm/expert), evidence_fact_ids, required_evidence, evidence_satisfied, inputs_used(항목 목록), evaluator_version | `incremental.CriterionResult`(incremental.py:69), `CriterionAssessment` |
| I-3 | `Action` (정책 출력) | action_id, type, patient_alias, info_source, target_criteria, expected_cost_min, fingerprint | `decision_state.Action`(decision_state.py:54) 확장. 필드 추가만 하고 의미 변경 없음 |
| I-4 | `ActionOutcome` (실행 결과) | action_id, status(succeeded/failed/no_response/partial), new_fact_ids, cost_charged_min, latency | 신규 |
| I-5 | `InformationSource` | source_id, kind(document/patient/clinician), provides_fields, cost_model, response_model | `acquisition.POLICIES` 문자열을 구조화 |
| I-6 | `EffortBudget` | coordinator_minutes, per_patient_floor, horizon | `decision_state.Budget`과 별도 |
| I-7 | `DecisionRecord` (감사) | schema_version, run_id, step, action, outcome, facts_added, criteria_reevaluated(+재사용 사유), pair_status_before/after, policy_version | Tracer 이벤트 `decision`(pipeline.py:335)을 대체하지 않고 새 이벤트 종류로 추가. Inspector 입력 계약 v2 |
| I-8 | `PolicyEngine` 프로토콜 | `propose(CohortState) -> list[Action]`, `select(list[Action], EffortBudget) -> Plan` | `decision_policy.candidates`를 환자 단위 구현으로 감쌈 |
| I-9 | `ExperimentSpec` | policy_id, config_hash, data_version(FROZEN 해시 + 코호트 해시), seed, budget | 신규 |

## 4. 작업 패키지 (역할 단위)

| WP | 이름 | 역할 | 범위 | 소유 파일(신규 중심) | 선행 | 완료 조건 |
|---|---|---|---|---|---|---|
| WP0 | 인터페이스 동결 | 아키텍트 | I-1~I-9 스키마를 pydantic 모델과 문서로 작성 | `src/ctrec/engine/contracts.py`, `docs/engine_contracts.md` | — | 모든 WP 대표가 검토하고 승인. 모델 단위 테스트(직렬화 왕복) |
| WP1 | 평가 위생 (P0-2) | 평가 담당 | 케이스 ID 별칭화, ID 누출 검사 테스트, 메트릭 조인 수정(누락 = 오답) | `src/ctrec/eval_hygiene.py`(신규), `metrics.py`(함수 추가), tests | — | 프롬프트로 가는 모든 문자열에 원 ID·정답 토큰이 없음을 검사하는 테스트 통과. 기존 metrics 테스트 유지 |
| WP2 | NSCLC 데이터 (M11) | 데이터·임상 검수 | NSCLC 시험 수집·컴파일·검수·동결, 사실 우선 코호트 생성기, 정답 쌍 계산 | `data/nsclc/**`, `scripts/build_nsclc_cohort.py`, `src/ctrec/cohort/generator.py` | WP0(I-5 정보원 배치) | 별도 store_dir에 동결. 생성기 결정성 테스트. 라벨을 규칙으로 재계산 가능. 기존 FROZEN 테스트 영향 없음 |
| WP3 | 근거 요구·검증 (M4, M5, M6, M12) | 판정 계층 담당 | `EvidenceRequirement`, adjudication 분류, `fact_view.gate`를 판정 경로에 플래그로 연결, PairStatus 파생 | `src/ctrec/engine/adjudication.py`, `schemas.RuleMeta`(기본값 있는 필드 추가만) | WP0 | gate의 단방향성(확정 → 불가만 허용) 테스트, expert 기준 자동 확정 불가 테스트, 기존 matcher 테스트 전부 통과 |
| WP4 | 부분 재평가 (M8) | 판정 계층 담당 | 기준 단위 assessor, 입력 항목 제한, resolution 추가 | `src/ctrec/agents/criterion_assessor.py`, `dependency_graph.py`(추가) | WP0, WP3 | 오프라인에서 `compare_states` 동등 100%, 영향 밖 기준의 LLM 호출 0건 |
| WP5 | 정책 엔진 (M1, M2, M7, M9, M13) | 정책 담당 | CohortState, 비용 모델, 정보원 묶음, 실패·대체, 공정성 제약 | `src/ctrec/engine/policy.py`, `cost.py`, `bundles.py`, `fairness.py` | WP0 | 환자 1명 코호트 = 기존 `decision_policy` 순서(동등성), 묶음 비용 1회, 예산 초과 0 |
| WP6 | 오프라인 시뮬레이터·실험 (M9, M10) | 평가 담당 | CohortEnvironment, ResponseModel(seed), ExperimentSpec, 기준선·ablation 실행기 | `src/ctrec/sim/**`, `scripts/run_cohort_experiment.py` | WP0, WP1, WP2(데이터), WP5 | 같은 spec 2회 실행 결과 해시 일치, 기준선 5종 이상 실행 |
| WP7 | 감사 계층 (I-7) | 감사·관측 담당 | DecisionRecord 발행, Inspector v2 입력 계약 | `src/ctrec/engine/audit.py`, `inspector.py`(v2 로더 추가, v1 유지) | WP0 | v1 실행 기록 렌더 결과 불변(기존 test_inspector), v2 기록으로 행동 → 재평가 → 상태 변화 추적 가능 |
| WP8 | 웹 통합 (M14) | 웹 담당 | `policy_mode` 플래그, WebQuestionChannel, ActionRenderer | `src/ctrec/web/engine_runner.py`(신규), `web/app.py`(분기 몇 줄), `pipeline.py`(주입 지점 1곳) | WP5, WP7 | legacy 모드에서 기존 web 테스트 전부 통과, engine 모드 골든 비교(오프라인과 같은 행동 순서), engine 모드에서 orchestrator 호출 0건 |

### 병렬 가능 영역
- **즉시 병렬**: WP1, WP2(시험 수집·검수), WP0 (서로 파일이 겹치지 않음)
- **WP0 승인 후 병렬**: WP3, WP5, WP7 (각자 신규 모듈)
- **직렬**: WP4는 WP3 다음, WP6은 WP5와 WP2 다음, WP8은 마지막

## 5. 파일 소유권 제안

| 파일·디렉터리 | 소유 역할 | 다른 역할의 수정 규칙 |
|---|---|---|
| `src/ctrec/engine/contracts.py` | 아키텍트 | 동결 후 변경은 모든 WP 검토 PR로만 |
| `src/ctrec/pipeline.py` | 웹 담당 (WP8) | **주입 지점 1곳 외 변경 금지**. 동시 편집 금지 |
| `src/ctrec/web/app.py`, `web/static/*` | 웹 담당 | 다른 WP 수정 금지 |
| `src/ctrec/schemas.py` | 판정 계층 담당 | 기본값 있는 필드 추가만. 의미 변경 금지 |
| `src/ctrec/agents/matcher.py`, `tools/rule_engine.py`, `evidence.py` | 판정 계층 담당 | 기존 함수 시그니처 유지 |
| `fact_*.py`, `dependency_graph.py`, `incremental.py` | 판정 계층 담당 | 추가만 |
| `decision_*.py`, `question_priority.py` | 정책 담당 | 기존 함수는 유지하고 engine에서 감쌈 |
| `inspector.py` | 감사 담당 | v1 경로 수정 금지 |
| `metrics.py`, `question_eval.py`, `eval/` | 평가 담당 | 함수 추가만 |
| `data/parsed/*`, `FROZEN.json` | 데이터 담당 | store_patch 절차로만 |
| `data/nsclc/**` | 데이터 담당 | — |
| `README.md` | 문서 담당(순번제) | 섹션별 분리 권장 (충돌 1순위 파일) |

## 6. PR 의존성과 통합 순서

```
PR-1 WP0 contracts ─┬─> PR-4 WP3 adjudication ─> PR-6 WP4 criterion assessor ─┐
PR-2 WP1 eval hygiene ┤                                                          │
PR-3 WP2a NSCLC trials ┼─> PR-5 WP5 policy engine ─┬─> PR-8 WP6 simulator/experiments
                       │                           │                             │
                       └─> PR-7 WP7 audit records ─┴──────────> PR-9 WP8 web integration
```
1. 통합 브랜치 `integration/dcvc`를 `main` 최신에서 만듭니다. 현재 HEAD(`feat/decision-inspector`)는 `origin/main`과 6/4로 갈라져 있으므로 **먼저 이 브랜치의 main 병합 여부를 결정**해야 합니다(미확인: 팀의 병합 계획).
2. PR은 WP 하나에 하나씩 만들고, 작게(가능하면 신규 파일 위주) 유지합니다.
3. PR마다 전체 테스트(현재 1,072건)가 통과해야 합니다. 환경 의존 테스트 5+1건(01 문서 P2-5)은 **먼저 별도 PR로 고치거나 CI 사전 조건(`outputs/` 없음, `.git` 있음)으로 명시**합니다.

## 7. 기존 테스트 보존 전략
- **동결 회귀선**: 현재 HEAD의 테스트 결과(1062 통과, 환경 실패 5, skip 5)를 기준선으로 기록합니다(04 문서).
- **legacy 기본값**: 새 기능은 모두 플래그로 꺼진 상태가 기본입니다. 기존 Session 기본값(`fact_compare="off"`, `decision_loop="off"` 등)과 같은 방식입니다.
- **보호 파일 테스트**: `tests/test_llm_pilot_exec.py:484` `test_frozen_store_and_policy_files_unchanged`는 `PROTECTED` 목록(pipeline.py, matcher.py, patient_profiler.py, recommender.py, decision_*.py, question_priority.py, web/access.py, web/retention.py, status.py, data/)의 **작업 트리와 HEAD 사이 diff가 비어 있어야** 통과합니다 [확인]. 그래서 커밋된 변경은 막지 않지만, 위 파일을 **커밋하기 전 로컬에서 수정하는 동안에는 이 테스트가 실패**합니다(병합 중에는 병합 대상 변경과 같은지 비교, :486-497). WP3/WP4/WP8 개발자는 이 동작을 알아야 하고, 보호 범위를 계속 쓸지는 팀이 정해야 합니다.
- **동등성 테스트**: (a) 환자 1명 코호트 = 기존 정책, (b) 부분 재평가 = 전체 재평가, (c) 오프라인 = 웹 engine 모드의 행동 순서.
- **Frozen 데이터 분리**: NSCLC 데이터는 별도 store_dir에 둬서 `test_decisions`의 FROZEN 해시 테스트에 영향을 주지 않습니다.
