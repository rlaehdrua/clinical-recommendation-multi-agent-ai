# 팀 병렬 개발 시작 안내 — `integration/dcvc` 기준 (계약 0.2 후보)

> 2026-11-02. 공통 기반: 브랜치 `integration/dcvc` (기준 `origin/integration/midterm-1026` `448a8c5` + DCV+C 5커밋 + 0.2 커밋).
> **현재 로컬 전용입니다(원격에 없음).** 선우가 push하기 전까지 서연·동겸은 이 브랜치를 받을 수 없습니다 → §0.

## 0. 시작 전 공통 선행 조건
1. 선우: 팀 확인 뒤 `integration/dcvc`를 원격에 push(이번 작업에서는 하지 않음). PR 대상은 `integration/midterm-1026`(midterm이 main에 들어간 뒤라면 main).
2. 각자: `git fetch origin` → `git switch -c <내 브랜치> origin/integration/dcvc` (또는 별도 worktree). 모든 PR은 `integration/dcvc`로.
3. 테스트는 API 키 없이: `env -u ANTHROPIC_API_KEY python -m pytest -q -p no:cacheprovider` (기대: 1996 passed, 7 skipped 이상, 실패 0).
4. 계약 0.2는 **PROVISIONAL**. 계약 필드가 더 필요하면 직접 고치지 말고 선우에게 "추가 요청"(기본값 있는 필드 추가만 가능).

## 1. 모두가 지킬 규칙
- **정책 엔진을 다시 만들지 않습니다.** 행동 후보·선택은 `ctrec.engine.policy.LegacyAdapterPolicy`(`propose` / `select` / `apply_outcome`)와 `contracts.PolicyEngine` 프로토콜만 씁니다. 새 알고리즘은 같은 프로토콜의 **새 클래스**로 추가하고 `tests/test_engine_policy.py`의 조건(예산 초과 0, 중복 청구 0, 결정성, 환자 1명 동등성, 안전 행동 보존)을 통과해야 합니다.
- **명제·질문 계약·결정적 평가기를 다시 만들지 않습니다.** `propositions.py`, `question_contract.py`, `evaluator.py`, `proposition_store.py`, `contract_session.py`를 쓰고, 엔진과는 `engine/contract_bridge.py`로만 연결합니다.
- 판정(적격성·기준 상태)은 판정 계층만 바꿉니다. 정책·웹·렌더러는 `CriterionResult`·`PairStatus`를 만들거나 바꾸지 않습니다.
- 질문 목적(`Action.purpose`)은 정책이 정하고 렌더러·LLM은 바꿀 수 없습니다(`engine/question_bridge.check_rendered`). 의사 참고용 답은 적격성 근거가 아닙니다.
- 보호 파일(커밋 전 로컬 수정 중에는 `test_frozen_store_and_policy_files_unchanged`가 실패함): `pipeline.py`, `agents/matcher.py`, `agents/patient_profiler.py`, `agents/recommender.py`, `decision_loop.py`, `decision_policy.py`, `decision_state.py`, `question_priority.py`, `web/access.py`, `web/retention.py`, `status.py`, `data/`.
- 실제 LLM·유료 API 실행은 별도 승인 전 금지. masked/validation 세트를 실제 LLM으로 돌리기 전에 B-1·B-4(아래) 필수.
- 환자 식별은 `eval_hygiene.AliasMap` 별칭만. 원문·환자 ID를 감사 기록·trace·화면 이벤트에 넣지 않습니다.

## 2. 서연 — WP2·WP3·WP4·WP6 + WP1 평가 진입점

### 바로 시작할 작업 (선행 조건 없음)
1. **B-1** `question_eval.run_method(..., answerer_factory=None)` 인자 추가(기본값 = 지금의 OracleAnswerer). 그 뒤 `eval_hygiene._scoped_oracle`의 모듈 속성 교체를 주입으로 바꿈.
2. **B-4** `question_eval.run_evaluation`, `eval/evaluate.py`, sweep 스크립트의 기본 경로를 `eval_hygiene.load_cases_hygienic` + `run_method_hygienic`으로.
3. **B-2** `cohort_metrics.cohort_eligibility_metrics`를 `metrics.py`에서 공개(기존 함수는 그대로).
4. **WP2** NSCLC 시험 수집·별도 `store_dir` 동결. 원격 `research/cohort-generator`(`src/ctrec/cohort/**`)가 이미 코호트 생성기를 가지고 있으니 **먼저 그 브랜치를 읽고 작성자와 겹침을 정리**하세요(이번 통합에 포함되지 않음).

### 선행 조건이 있는 작업
| 작업 | 선행 조건 |
|---|---|
| WP3 `RuleMeta.adjudication`(rule/fact_llm/expert) | 계약 0.2 회의 안건 1 결정. 결정 전까지 `contract_bridge.provisional_adjudication` 사용 |
| WP3·WP4 판정 계층이 `CriterionResult` 생산 | 0.2 계약. ContractSession 기준은 `contract_bridge.criterion_overrides_from_session` 재사용 |
| WP4 기준 단위 재평가 → `audit.make_record(criteria_before/after, reevaluated, pair_reasons=…)` | WP3. 확정에서 내려갈 때 `reason_code="contradicting_evidence"`, 다시 확정할 때 `"evidence_reverified"` + 새 사실 |
| WP6 실험 실행기 | WP2 데이터 + `engine.experiment.require_catalog_hash` 실행 전 호출, 결과 manifest에 `catalog_claims` 기록 |

### 변경 가능 / 금지
- 가능: `question_eval.py`, `metrics.py`(추가만), `eval/**`, `scripts/build_masked_benchmark.py`, `data/nsclc/**`(별도 store_dir), 신규 `src/ctrec/engine/adjudication.py`, 신규 `src/ctrec/agents/criterion_assessor.py`, `dependency_graph.py`(추가만), 신규 `src/ctrec/sim/**`, `scripts/run_cohort_experiment.py`, `schemas.RuleMeta`(기본값 있는 필드 추가만), `eval_hygiene.py`·`cohort_metrics.py`(B-1/B-2 뒤 소유 이관)
- 금지: `engine/contracts.py`·`engine/policy.py`·`engine/cost.py`·`engine/bundles.py`·`engine/cohort_state.py`(요청만), `web/**`, 기존 `data/parsed/*`·`FROZEN.json`(store_patch 절차 외), `evaluator.py`·`question_contract.py`·`propositions.py`의 의미 변경(필요하면 midterm 담당과 합의)

### 테스트 조건
- 전체 테스트 실패 0. 별칭 경로 누출 0(`test_eval_hygiene.py`). expert 기준 자동 확정 불가. 부분 재평가 = 전체 재평가(`compare_states`). 같은 ExperimentSpec 2회 결과 해시 동일, `source_catalog_hash` 없으면 실행 거부. FROZEN 해시 테스트 불변.

### Claude Code 프롬프트 초안 (서연)
```text
TRIALOG 저장소, 브랜치 origin/integration/dcvc에서 새 브랜치 feat/wp1-eval-entry를 만들어 작업한다.
먼저 docs/dev/team_parallel_start.md, docs/dev/contract_v02_changelog.md, docs/dev/integration_handoff.md §2,
src/ctrec/eval_hygiene.py, src/ctrec/question_eval.py를 읽어라.

목표 (순서대로)
1. B-1: question_eval.run_method에 answerer_factory=None 인자를 추가한다(기본은 현재 OracleAnswerer와 같은 동작).
   eval_hygiene.run_method_hygienic이 모듈 속성 교체 대신 이 인자로 ScopedOracleAnswerer를 넘기게 바꾼다.
2. B-4: question_eval.run_evaluation, eval/evaluate.py, sweep 스크립트의 기본 경로를 load_cases_hygienic + PromptLeakGuard로 바꾼다.
   기존 결과와 비교할 수 있게 별칭 없는 경로는 명시적 옵션으로만 남긴다.
3. B-2: cohort_metrics.cohort_eligibility_metrics를 metrics.py에서 공개한다(기존 eligibility_metrics·baseline_gate는 그대로).

제약
- src/ctrec/engine/** 의 정책 엔진과 계약은 수정하지 않는다(필요한 필드는 보고만).
- evaluator.py, question_contract.py, propositions.py, contract_session.py의 판정 의미를 바꾸지 않는다.
- 보호 파일(pipeline.py, agents/matcher.py, decision_*.py 등)은 수정하지 않는다.
- ANTHROPIC_API_KEY를 제거하고 테스트한다. 실제 LLM·유료 API를 호출하지 않는다.
- 전체 테스트 실패 0, 기존 테스트 기대값을 바꾸면 이유를 문서에 남긴다.
- 커밋은 작업 단위로 나누고 push·PR은 하지 않는다. 끝나면 변경 파일·테스트 결과·남은 위험을 docs/dev/에 보고서로 남긴다.
```

## 3. 동겸 — WP5 인수·WP8 웹 통합·프론트엔드

### 바로 시작할 작업 (선행 조건: 0번만)
1. **WP5 인수**: `engine/policy.py`, `cost.py`, `bundles.py`, `cohort_state.py`, `fairness.py`와 `tests/test_engine_policy.py`, `tests/test_engine_contract_v02.py`를 읽고 소유를 넘겨받음. 알고리즘 교체는 새 `PolicyEngine` 클래스로.
2. **WP8 골격** `src/ctrec/web/engine_runner.py`(신규): `policy_mode="engine"`일 때
   `CohortState.from_decision_states({alias: build_state(SessionView.from_session(s))}, criterion_overrides={alias: contract_bridge.criterion_overrides_from_session(alias, s)})`
   → `LegacyAdapterPolicy.propose/select` → `Plan.selected` 중 `question_bridge.renderable(a)`인 행동만 `question_bridge.render_action`으로 질문화 → 답은 `question_bridge.qa_pair`로 기록 → `ActionOutcome` → `policy.apply_outcome`.
   **판정 변경은 반드시 기존 재추출·재매칭(또는 ContractSession.match)을 거침.** `legacy`가 기본값.
3. **화면 표시**: 쌍 상태 7개 라벨 + `pending_kinds`("추가로 남은 확인"), `Plan.safety_unfunded`가 있으면 "안전 확인 미실행" 표시 후 그 환자의 다른 행동 진행 금지.

### 선행 조건이 있는 작업
| 작업 | 선행 조건 |
|---|---|
| 웹 기본 경로를 ContractSession으로 바꿀지 | 팀 결정 (지금 `web/app.py:437`의 `session_class()`는 `CTREC_SESSION=contract`일 때만 ContractSession) |
| 계약 질문 자동 연결 | ContractSession 실행 상태에서 `contract_bridge.contract_candidates(action.target_criteria, s.contract_provenance, s._contracts)` → `question_bridge.attach_question_contract` |
| `engine / decision_record_v2` trace·SSE 노출 | 별칭 salt 수명 결정(A-9). 화면 이벤트에 별칭 매핑 노출 금지 |
| `research/cohort-generator`의 `web/app.py`·`static/*` 변경과 정렬 | 해당 브랜치 작성자와 합의 (이번 통합에 포함 안 됨) |

### 변경 가능 / 금지
- 가능: 신규 `src/ctrec/web/engine_runner.py`, `web/app.py`의 `policy_mode` 분기(몇 줄), `web/static/*`, `engine/fairness.py`(관측 지표 추가), 새 `PolicyEngine` 구현 클래스(신규 파일), `pipeline.py`·`ContractSession`의 **주입 지점 1곳**(보호 파일 — 팀 공지 후)
- 금지: `engine/contracts.py`(추가 요청만), `LegacyAdapterPolicy.select`의 선택 규칙과 `EffortLedger` 청구 규칙 직접 수정, 판정 계층(`agents/matcher.py`, `tools/rule_engine.py`, `evidence*.py`, `evaluator.py`), `web/budget.py`와 `EffortLedger` 통합, `Action.purpose`를 렌더러에서 바꾸기, 질문 계약 문장 변경

### 테스트 조건
- `policy_mode="legacy"`에서 기존 웹 테스트 전부 통과(기본값 불변).
- engine 모드: 같은 DecisionState → 오프라인과 같은 행동 ID 순서(골든), orchestrator LLM 호출 0, 실패·무응답이면 새 사실 0, `apply_outcome` 뒤 판정 불변, `safety_unfunded` 환자의 다른 행동 미진행, physician_reference 답이 판정 근거로 쓰이지 않음, 원래 환자 ID·별칭 매핑이 SSE·화면에 없음.

### Claude Code 프롬프트 초안 (동겸)
```text
TRIALOG 저장소, 브랜치 origin/integration/dcvc에서 새 브랜치 feat/wp8-engine-runner를 만들어 작업한다.
먼저 docs/dev/team_parallel_start.md, docs/dev/contract_v02_changelog.md, docs/dev/integration_handoff.md §3,
src/ctrec/engine/{policy,cohort_state,contract_bridge,question_bridge}.py, src/ctrec/web/app.py(_execute_run 주변),
src/ctrec/contract_session.py를 읽어라.

목표
1. src/ctrec/web/engine_runner.py(신규)를 만든다. policy_mode="engine"일 때 현재 Session(또는 ContractSession)을
   CohortState로 감싸고(criterion_overrides는 contract_bridge.criterion_overrides_from_session) LegacyAdapterPolicy로
   행동을 고른 뒤, question_bridge.render_action/qa_pair로 질문과 답 기록을 만든다. 기본값은 policy_mode="legacy".
2. 답변 뒤에는 ActionOutcome을 만들고 policy.apply_outcome으로 운영 상태만 갱신한다. 판정 변경은 반드시 기존
   재추출·재매칭(ContractSession이면 match)을 거친다.
3. 화면에 쌍 상태와 pending_kinds, safety_unfunded("안전 확인 미실행")를 표시한다.

제약
- 정책 엔진(select·EffortLedger 규칙)과 engine/contracts.py를 수정하지 않는다. 필요한 계약 필드는 보고만 한다.
- 정책 엔진을 다시 구현하지 않는다. 다른 알고리즘이 필요하면 PolicyEngine 프로토콜의 새 클래스로 만든다.
- 명제·질문 계약·결정적 평가기를 다시 구현하지 않는다. LLM은 질문 문구 표현에만 쓰고 행동 선택·purpose·판정에 관여하지 않는다.
- web/budget.py(LLM 비용)와 EffortLedger(코디네이터 시간)를 합치지 않는다.
- 보호 파일(pipeline.py 등)은 주입 지점 1곳 외 수정하지 않는다. 수정 전 팀에 알린다.
- 환자 원래 ID·별칭 매핑을 SSE·화면·trace에 내보내지 않는다.
- ANTHROPIC_API_KEY를 제거하고 테스트한다. legacy 모드에서 기존 웹 테스트 전부 통과, engine 모드 골든 비교와
  orchestrator 호출 0건 테스트를 추가한다.
- 커밋은 작업 단위로, push·PR은 하지 않는다. 끝나면 docs/dev/에 보고서를 남긴다.
```

## 4. 선우 — WP0 계약 승인·WP7 Inspector 검토·최종 품질
1. `integration/dcvc` 검토 후 push (팀 확인).
2. 계약 0.2 승인 회의: `docs/dev/contract_v02_changelog.md` §9 안건 6개.
3. WP7: Inspector v1 화면에 v2 섹션(사유·남은 작업·`reversal_issues`)을 붙일지 결정. v1 렌더 결과 불변 테스트 유지.
4. 각 PR: 전체 테스트, 누출 검사, 보호 파일 테스트, 계약 버전 확인.

## 5. 병렬 순서
```
지금        서연 B-1·B-4·B-2, WP2 조사      동겸 WP5 인수, WP8 골격(legacy 기본)      선우 push·0.2 승인
0.2 승인 뒤  서연 WP3 adjudication           동겸 WP8 engine 모드 + 화면
WP3 뒤      서연 WP4 → DecisionRecord 생산   선우 WP7 연결 검토
WP2·WP5 뒤  서연 WP6 실험                   동겸 WP8 마무리
```
