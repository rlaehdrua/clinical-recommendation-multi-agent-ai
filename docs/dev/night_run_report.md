# 야간 자율 개발 보고서 — 백엔드 공통 계약·평가 안전·Inspector v2·정책 엔진 기반

> 실행일 2026-11-02 (야간, 사용자 부재). 실제 LLM/유료 API 호출 0회, 원격 push·PR 0건, `main` 변경 없음.
> 판정·추천 실행 경로(pipeline, web, schemas, agents, data)는 **한 줄도 바꾸지 않았습니다**. 모든 변경은 신규 파일입니다.

## 1. 브랜치와 커밋

| 항목 | 값 |
|---|---|
| 시작 브랜치 / HEAD | `feat/decision-inspector` / `62f744e` (작업 트리: `docs/audit/` 미추적만 있음) |
| 작업 브랜치 | `feat/dcvc-backend-foundation` (`62f744e`에서 분기, 원격 추적 없음) |
| 종료 HEAD | `7247330` |
| `origin/main`과의 관계 | 시작 시 `HEAD...origin/main` = 4 / 6 (이 브랜치에만 4, main에만 6). 병합·리베이스하지 않음 |

| 커밋 | Phase | 내용 |
|---|---|---|
| `a3ab322` | A (WP0) | 공통 계약 I-1~I-9 (PROVISIONAL) + 문서 + 테스트 |
| `9f85acb` | B (WP1) | 케이스 ID 정답 누출 차단, oracle 범위 제한, 누락 포함 코호트 지표 |
| `0c98238` | C (WP7) | DecisionRecord v2 생성·검증·저장·로드, Inspector v2 뷰 모델 |
| `7247330` | D (WP5) | 코호트 상태·비용 원장·정보원 묶음·정책 기준선·배분 관측 (+ 계약 0.1 필드 추가) |
| (이 문서 커밋) | E | 보고서·인수인계 문서 |

## 2. Phase별 상태

| Phase | 상태 | 요약 |
|---|---|---|
| A — WP0 공통 인터페이스 | **완료 (PROVISIONAL)** | 계약 모델·전이 표·파생 함수 구현, 46개 테스트. 팀 승인 전이므로 동결 아님 |
| B — WP1 평가 정답 누출 방지 | **완료 (호환 래퍼 방식)** / 기존 평가 기본 경로 전환은 **미적용** | 래퍼로 연결하고 전체 101개 사례에서 누출 0 확인. `question_eval.run_evaluation` 기본 경로는 서연 담당 파일이라 바꾸지 않음 (인수인계 B-4) |
| C — WP7 Inspector v2 | **완료 (독립 모듈)** / 실제 실행 경로의 기록 발행은 **미착수** | 로더·검증·뷰·HTML 구현. 실제 Session·웹에서 DecisionRecord를 만드는 생산자는 WP3/4/8 이후 |
| D — WP5 정책 엔진 기반 | **완료 (순수 모듈, 기준선)** / 웹 연결 **미착수(동겸 WP8)** | 단일 단계 결정적 탐욕 기준선. 다단계(horizon) 최적화는 인터페이스만 |
| E — 테스트·문서 | **완료** | 전체 테스트, 깨끗한 사본 재확인, 문서 2건 |

## 3. 신규·수정 파일

수정한 기존 파일: **없음** (`git diff --name-only 62f744e..HEAD`의 기존 파일 0개).

| 파일 | 줄 | 역할 |
|---|---|---|
| `src/ctrec/engine/__init__.py` | 8 | 패키지 설명 |
| `src/ctrec/engine/contracts.py` | ~460 | I-1~I-9 계약 (PairKey/PairStatus/전이, CriterionResult, EffortCost, InformationSource, Action, ActionOutcome, EffortBudget, DecisionRecord, PolicyEngine/Plan, ExperimentSpec) |
| `src/ctrec/eval_hygiene.py` | ~315 | AliasMap, LeakScanner, PromptLeakGuard, ScopedOracleAnswerer, build_fact_scope, load_cases_hygienic, run_method_hygienic |
| `src/ctrec/cohort_metrics.py` | 60 | 누락 예측 포함 지표, confirmed_correct_pairs, cohort_baseline_gate |
| `src/ctrec/engine/audit.py` | ~315 | make_record, RecordWriter, load_records/load_run_v2/records_from_trace, build_v2_view, chain, render_v2_html |
| `src/ctrec/engine/cohort_state.py` | 130 | CohortState, PairState, OpsState (기존 DecisionState 읽기 전용 사본) |
| `src/ctrec/engine/bundles.py` | 152 | SourceCatalog(기본 정보원, 자리표시 비용), route, alternatives, bundle |
| `src/ctrec/engine/cost.py` | 81 | EffortLedger (공유 분 예산, 정보원 단위 1회 청구, 초과 거부) |
| `src/ctrec/engine/policy.py` | ~215 | LegacyAdapterPolicy (propose/select/apply_outcome), plan_cohort, plan_and_charge |
| `src/ctrec/engine/fairness.py` | 51 | allocation_report, gini |
| `docs/engine_contracts.md` | — | 계약 설명·열린 질문 |
| `docs/dev/night_run_report.md`, `docs/dev/integration_handoff.md` | — | 이 보고서, 인수인계 |
| `tests/test_engine_contracts.py` | 46 테스트 | 직렬화 왕복, 필수값, 음수 비용, frozen, 전이 전수, 판정 권한, gap 조합 전수 |
| `tests/test_eval_hygiene.py` | 18 테스트 | 별칭, 누출 검사, 실행 경로 누출 0, oracle 범위, 누락 분모 |
| `tests/test_engine_audit.py` | 14 테스트 (1 skip) | 생성·저장·재로딩, 연결, 실패·부분·무응답, 버전·손상, 민감정보, v1 무변경 |
| `tests/test_engine_policy.py` | 36 테스트 | 예산 초과 0, 중복 청구 0, 결정성, 실패 뒤 사실 생성 0, 정답 격리, 1명 동등성, 임상 판단, 안전 행동, 공정성 |

## 4. 테스트

모든 명령은 `ANTHROPIC_API_KEY`를 제거하고 실행했습니다 (`env -u ANTHROPIC_API_KEY .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -rfs`).

| 실행 | 결과 |
|---|---|
| 작업 전 기준선 (새 브랜치, 코드 변경 전) | **1062 passed, 5 failed, 5 skipped** (150.5초) — 감사 04 문서와 같음 |
| Phase A `tests/test_engine_contracts.py` | 43 passed → 계약 수정 뒤 46 passed |
| Phase B `tests/test_eval_hygiene.py` | 1차 2 failed → 수정 → 18 passed (아래 4.1) |
| Phase C `tests/test_engine_audit.py` + `tests/test_inspector.py` | 13 passed 1 skipped / 110 passed 4 skipped (v1 회귀 없음) |
| Phase D `tests/test_engine_policy.py` | 36 passed |
| **작업 후 전체** | **1175 passed, 5 failed, 6 skipped** (158.2초) = 기준선 1062 + 신규 113 |
| 깨끗한 사본(`git archive HEAD`) 재실행: 파일럿 3개 파일 + 신규 4개 파일 | 213 passed, 1 failed, 1 skipped |

- 실패 5건은 작업 전과 **같은 5건**이고 원인도 같습니다: `assert not (ROOT / "outputs").exists()` (기존 사용자 산출물 `outputs/inspector/`가 있음). 깨끗한 사본에서는 통과했습니다. `outputs/`는 지우지 않았습니다.
- 깨끗한 사본의 1건 실패는 `test_frozen_store_and_policy_files_unchanged` (사본에 `.git`이 없음, 감사 때와 같음). 원 저장소에서는 통과합니다.
- 추가 skip 1건: `test_engine_audit.py`의 심볼릭 링크 거부 테스트 (Windows에서 링크를 만들 수 없음, 기존 Inspector 4건과 같은 이유).
- **기존 테스트 영향: 없음** (새 실패 0, 기존 통과 테스트 수 동일).

### 4.1 Phase B에서 고친 것
1. 보충 검증 세트 ID가 `S-` 접두사·`-confirmed-` 접미사를 써서 ID 모양 검사가 놓침 → 패턴 확장.
2. 누출 차단 예외를 파이프라인이 시험 단위로 잡아 `NOT_EVALUATED`로 남기고 실행은 `ok`가 됨 → `run_method_hygienic`이 누출이 한 번이라도 있으면 실행 전체를 `error / PromptLeakError`로 표시.

### 4.2 전체 데이터 오프라인 확인 (테스트 밖, 스크래치 스크립트)
masked 101개 사례 × baseline·candidate, 2라운드, 라운드당 질문 3개, 오프라인 대역:

| 항목 | 결과 |
|---|---|
| 기존 경로에서 원래 ID가 프롬프트에 들어간 사례 | **101 / 101** (감사 P0-2 재현) |
| 별칭 경로의 누출 | **0건 / 프롬프트 1,002개** (실행 202개, 모두 ok) |
| 공개된 답변 수 (범위 제한 / 기존) | baseline 53 / 57, candidate 23 / 27 |
| 범위 제한으로 공개가 줄어든 실행 | baseline 4, candidate 3 |
| 기준 대응을 정하지 못해 공개하지 않는 숨긴 사실 | 18개 |
| 26개 시험 원문·기준표에서 검사기 오탐 | 0건 |

해석: 기존 oracle은 기준 하나만 맞아도 다른 기준의 사실까지 공개해 회수율을 높게 잡았습니다. 범위 제한 답변기는 반대로 대응을 정하지 못한 18개 사실을 공개하지 않으므로 **회수율을 낮게 잡을 수 있습니다**. 정확한 값에는 데이터 생성 단계의 사실→기준 대응표가 필요합니다(인수인계 B-3).

## 5. 구현 요약과 설계 판단

- **이름 충돌 회피**: 계약은 `ctrec.engine.contracts`에 있고 기존 `decision_state.Action`, `incremental.CriterionResult`와 다른 모듈입니다. 기존 행동 유형은 `LEGACY_ACTION_TYPES`로 1:1 대응합니다.
- **판정 권한 분리**: 계약 모델은 모두 frozen + extra 금지입니다. 전문가 판단 기준은 임상의만 확정할 수 있습니다. 정책 권한의 쌍 상태 전이는 `INFO_MISSING|VERIFICATION_PENDING → BLOCKED_FAILURE`뿐입니다. `apply_outcome`은 운영 상태만 바꿉니다.
- **비용 축 분리**: 코디네이터 분 / 임상의 분 / LLM 호출·USD / 환자 대기(능동 시간 아님)를 나눴습니다. 요청 비용은 (환자, 정보원)당 1회 청구합니다.
- **결정성**: 기존 정책의 행동 ID는 uuid라 엔진 행동 ID를 `<별칭>:<지문>`으로 만들었습니다. 같은 상태면 같은 Plan이 나옵니다(테스트).
- **환자 1명 동등성**: 4개 시나리오에서 엔진 제안 순서 = 기존 `decision_policy.candidates` 순서이고, 예산이 무제한이면 선택 순서 = 기존 실행 행동 순서입니다. 이를 위해 계약 `Action`에 `priority`·`sequence`를 **추가**했습니다(하위 호환).
- **안전 행동 보존**: tier 1(상충 확인·상태 재계산)은 먼저 선택합니다. 예산이 모자라면 `safety_unfunded`로 보고하고, 그 환자의 나머지 행동은 `blocked_by_unfunded_safety`로 막습니다.
- **확률 없음**: 정책 모듈에 `random`·`probab`·`oracle`·`label` 문자열과 평가·LLM 모듈 직접 import가 없음을 AST 검사로 확인합니다.

## 6. 아직 해결되지 않은 문제 (위험 순)

1. **실제 실행 경로 미연결**: 웹·CLI는 여전히 LLM orchestrator·question_generator를 씁니다(감사 P0-1 그대로). 정책 엔진은 오프라인 순수 모듈뿐입니다. → 동겸 WP8
2. **평가 기본 경로가 아직 별칭을 쓰지 않음**: `question_eval.run_evaluation`, `eval/evaluate.py`, sweep 스크립트는 기존 경로입니다. 실제 LLM으로 masked 세트를 돌리기 **전에** `run_method_hygienic`을 쓰거나 B-4를 반영해야 합니다. 이미 실제 LLM으로 만든 masked 평가 결과가 있다면 오염 가능성이 있습니다(이 저장소에서는 실제 실행 기록 0회로 알려져 있음).
3. **PromptLeakGuard·범위 제한 oracle은 모듈 속성을 교체**합니다. oracle 교체는 잠금으로 직렬화했지만 guard는 잠그지 않으므로, 여러 스레드에서 평가를 동시에 돌리면 안 됩니다. answerer 주입 인자(B-1)가 생기면 교체는 필요 없습니다.
4. **사실→기준 대응이 단어 겹침 추정**입니다 (기준 2개 이상인 17개 사례). 모호한 18개 사실은 공개하지 않습니다(과소 추정 쪽).
5. **정답 토큰 검사(`rescue|cleanup`)의 오탐 가능성**: 지금 26개 시험에는 0건이지만, NSCLC 시험 원문에 "rescue therapy" 같은 표현이 있으면 오탐이 납니다. WP2 데이터 동결 때 다시 검사하고, 필요하면 `LeakScanner(check_label_tokens=False)`로 원래 ID 문자열 검사만 쓰세요.
6. **정보원 비용 값은 자리표시값**입니다(`bundles.DEFAULT_SOURCES`). 실험 결과의 분 단위 수치에는 의미가 없습니다.
7. **DecisionRecord 생산자 없음**: 판정 계층이 기준 전후 판정과 재평가·재사용 구분을 줄 수 있어야 실제 기록이 생깁니다(WP3/WP4). `incremental.update`의 `reused/evaluated` 상태가 입력 후보입니다.
8. **정책 선택은 단일 단계**입니다(`horizon_steps` 미사용). 다단계 계획·배낭형 최적화는 같은 `select` 인터페이스로 바꿔 끼우면 됩니다.
9. 정책 모듈은 `decision_state`를 거쳐 `agents.matcher`의 분류 함수를 **간접 import**합니다(기존 구조). 직접 LLM 호출 경로는 없습니다.
10. 환경 의존 테스트 6건(감사 P2-5)은 그대로입니다. 이번 작업 범위 밖이라 고치지 않았습니다.

## 7. 다음 날 확인할 것

1. 브랜치 `feat/dcvc-backend-foundation`의 커밋 5개를 검토하고, push·PR 여부를 결정해 주세요(자동으로 하지 않음).
2. `docs/engine_contracts.md` §5의 열린 질문 5개 — 팀 승인이 나면 `CONTRACT_STATUS`를 바꿉니다.
3. `docs/audit/` 보고서 4개는 **미추적 상태로 두었습니다**(사용자 파일이라 커밋하지 않음). 커밋할지 정해 주세요.
4. 이 브랜치는 `feat/decision-inspector` 위에 있고 `origin/main`보다 6커밋 뒤입니다. 통합 브랜치 계획(감사 03 §6)에 맞춰 리베이스·병합 순서를 정해야 합니다. 신규 파일뿐이라 충돌 위험은 낮습니다.
5. 인수인계 문서의 서연·동겸 요청 항목을 전달해 주세요.

## 8. `git status --short` (보고서 커밋 직전)

```
?? docs/audit/
```
(보고서 커밋 뒤에도 `docs/audit/`만 미추적으로 남습니다.)
