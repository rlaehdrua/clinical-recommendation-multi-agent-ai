# 통합 인수인계 — 공통 계약·평가 위생·감사 v2·정책 엔진 기반

> 브랜치 `feat/dcvc-backend-foundation` (base `62f744e`). 작성: 선우 담당 영역(백엔드·Inspector) 야간 작업.
> 계약은 **PROVISIONAL**입니다. 아래 요청은 제안이며 각 담당자의 판단이 우선합니다.
> 기존 파일은 하나도 바꾸지 않았습니다. 아래 "제안" 항목은 각 담당 파일에 넣으면 좋을 변경입니다.

## 1. 바로 쓸 수 있는 것

```python
from ctrec.engine import contracts as C                     # I-1~I-9
from ctrec.eval_hygiene import AliasMap, load_cases_hygienic, run_method_hygienic
from ctrec.cohort_metrics import cohort_eligibility_metrics, cohort_baseline_gate
from ctrec.engine.audit import make_record, RecordWriter, load_run_v2, build_v2_view, render_v2_html
from ctrec.engine.cohort_state import CohortState
from ctrec.engine.policy import LegacyAdapterPolicy, plan_and_charge
from ctrec.engine.fairness import allocation_report
```

오프라인 평가를 별칭 + 누출 차단 + 범위 제한 oracle로 돌리는 예:
```python
from ctrec import question_eval as qe
hcs, amap, scanner = load_cases_hygienic(parsed)
with qe.OfflineEnvironment(budget):
    for hc in hcs:
        run, guard = run_method_hygienic("candidate", hc, trials, rounds, budget, scanner)
        assert run.status == "ok" and not guard.leaks
pred = amap.unalias_keys(pred_by_alias)   # 지표 계산 전에 평가기에서만 되돌림
```

## 2. 서연 (데이터·판정·실험) 요청

| ID | 대상 파일 | 요청 | 이유 / 테스트 |
|---|---|---|---|
| B-1 | `question_eval.py` | `run_method(..., answerer_factory=None)` 인자 추가 (기본값 = 지금의 `OracleAnswerer`) | 지금은 `eval_hygiene._scoped_oracle`이 모듈 속성을 잠금 안에서 바꿈. 주입 인자가 생기면 교체가 필요 없고 동시 실행도 안전. 기존 테스트 영향 없음 |
| B-2 | `metrics.py` | `cohort_metrics.cohort_eligibility_metrics`를 `metrics.py`로 옮기거나 import해서 공개. 기존 `eligibility_metrics`·`baseline_gate`는 그대로 | 감사 P1-6. 누락 예측이 분모에서 빠짐 (`tests/test_eval_hygiene.py::test_missing_predictions_counted_in_denominator`가 차이를 보여 줌: 기존 1.0 vs 누락 포함 0.5) |
| B-3 | `scripts/build_masked_benchmark.py` (+ 사례 JSON) | 숨긴 사실마다 대응 기준을 명시 (`masked_fact_targets: {fact_id: [criterion text or rule_key]}`) | 기준이 2개 이상인 17개 사례는 지금 단어 겹침으로 대응을 추정하고, 18개 사실은 공개하지 않음. `build_fact_scope`가 명시 대응이 있으면 그것을 쓰게 바꾸면 됨 (`FactScope.method="explicit"`) |
| B-4 | `question_eval.run_evaluation`, `eval/evaluate.py`, sweep 스크립트 | 실제 LLM 실행 경로의 기본값을 별칭 + `PromptLeakGuard`로 | 감사 P0-2. **실제 LLM으로 masked/validation 세트를 돌리기 전 필수** |
| B-5 | 신규 사례 ID 규칙 | 새 데이터(NSCLC 등)의 사례 ID에 정답·대상 기준을 넣지 않기. 넣는다면 실행 전에 반드시 별칭 사용 | `LeakScanner`의 ID 모양 패턴은 기존 `F-/M-/V-/S-` + `SYN|DEMO|NCT`만 알아봄 |
| B-6 | 계약 검토 | `PairStatus` 7값·UNCERTAIN 우선순위(정보 없음 > 확인 필요 > 임상의 판단), `confirmed_correct_pairs` 정의(M3), `EffortCost` 축 | `docs/engine_contracts.md` §4–5 |
| B-7 | 정보원 비용 | `bundles.DEFAULT_SOURCES`의 분 단위 값은 자리표시값. 실측·문헌값 또는 실험 조건으로 교체 | 지금 값으로 낸 분 단위 결과는 의미 없음 |
| B-8 | WP3/WP4 판정 계층 | 재평가 결과를 `contracts.CriterionResult`(adjudication rule/fact_llm/expert, decided_by)로 내보내기 + 기준별 재평가/재사용 구분 | `engine.audit.make_record(criteria_before, criteria_after, reevaluated=...)`의 입력. `incremental.update`의 `evaluated/reused`가 후보 |
| B-9 | WP2 데이터 동결 시 | NSCLC 시험 원문에 대해 `LeakScanner().scan()` 오탐 확인 ("rescue therapy" 등) | 지금 26개 시험 오탐 0. 오탐이 있으면 `check_label_tokens=False` |

## 3. 동겸 (정책 엔진·웹·프론트엔드) 요청

| ID | 대상 | 요청 | 비고 |
|---|---|---|---|
| W-1 | WP8 `web/engine_runner.py` (신규) | `policy_mode="engine"`일 때 `CohortState.from_decision_states({alias: build_state(SessionView.from_session(s))})` → `LegacyAdapterPolicy.propose/select` → `Plan.selected`를 질문·문서 요청으로 변환 | 환자 1명이면 기존 `decision_policy` 순서와 같음(`test_single_patient_order_equals_legacy_policy`). 웹은 지금 환자 1명 Session이므로 그대로 붙일 수 있음 |
| W-2 | 별칭 | 웹 실행에서도 정책·LLM에 넘기는 환자 ID는 `AliasMap`으로 (salt는 실행별, 서버에만) | `CohortState`는 별칭이 아닌 키를 거부함 |
| W-3 | ActionRenderer | `Action` → `ClarifyingQuestion` 변환은 렌더러에서. LLM을 쓰더라도 **행동 선택에는 관여하지 않게** | 계약에 질문 문구 필드는 일부러 없음. 필요한 필드가 있으면 계약에 **추가** 요청 |
| W-4 | trace·SSE | 엔진 기록을 웹 Tracer에 남길 때 이벤트는 `engine / decision_record_v2` (`audit.to_trace`). **SSE로 내보낼지 결정 필요**: 기록에는 ID·코드·숫자만 있지만 별칭 매핑이 화면에 나가지 않게 | 지금은 웹 어디에서도 `to_trace`를 부르지 않으므로 영향 없음. app.js가 모르는 이벤트를 무시하는지 확인 필요 |
| W-5 | 웹 예산과 분리 | `EffortLedger`(코디네이터 분)와 `web/budget.py`(LLM 비용 원장)를 섞지 않기. LLM 비용은 `EffortBudget.llm_cost_usd=None`(엔진에서 관리 안 함)이 기본 | 감사 02 M2 |
| W-6 | 결과 반영 | 질문 답변·문서 결과를 받으면 `ActionOutcome`을 만들고 `policy.apply_outcome` → 운영 상태 갱신. **판정 변경은 반드시 기존 재추출·재매칭(또는 WP4 부분 재평가)을 거쳐야 함** | `apply_outcome`은 사실·판정을 바꾸지 않음. 실패·무응답이면 사실 0개 (계약이 거부) |
| W-7 | 안전 필수 행동 | `Plan.safety_unfunded`가 비어 있지 않으면 화면에 "안전 확인 미실행"을 표시하고 그 환자의 다른 행동을 진행하지 않기 | 정책이 이미 그 환자의 다른 행동을 막음 |

## 4. 공통 (Inspector v2)

- v2 기록 위치: 실행 폴더 `decision_records.jsonl` (우선) 또는 `trace.jsonl`의 `engine / decision_record_v2` 이벤트
- `load_run_v2(run_dir)` → `build_v2_view` → `render_v2_html` (외부 스크립트 없음, CSP `default-src 'none'`)
- v1 `inspector.py`와 `scripts/render_inspector.py`는 그대로입니다. v1 화면에 v2 섹션을 붙일지는 나중에 정합니다(그때 v1 렌더 결과 불변 테스트 유지)
- 기록 문제는 `line N: corrupt_json | unsupported_schema:<ver> | invalid_record:<필드 경로> | duplicate_step:<n> | not_object`, `multiple_run_ids`, `no_v2_records` 코드로만 보고합니다(값 없음)

## 5. 파일 소유 (이번 작업에서 만든 파일)

| 파일 | 소유 제안 | 다른 사람이 바꿀 때 |
|---|---|---|
| `engine/contracts.py`, `docs/engine_contracts.md` | 선우 (아키텍트) | 필드 추가는 PR 리뷰로, 의미 변경은 전원 검토 |
| `eval_hygiene.py`, `cohort_metrics.py` | 선우 → 서연에게 이관 가능 | B-1/B-2 반영 뒤 서연 소유로 옮기는 것을 제안 |
| `engine/audit.py` | 선우 | — |
| `engine/cohort_state.py`, `cost.py`, `bundles.py`, `policy.py`, `fairness.py` | 선우(기반) → 동겸(WP8 연결) | `select` 알고리즘 교체는 `PolicyEngine` 프로토콜을 지키고 `tests/test_engine_policy.py` 통과 조건 유지 |

## 6. 통합 순서 제안

1. 이 브랜치를 PR-1(WP0) / PR-2(WP1) / PR-7(WP7) / PR-5(WP5)로 나누거나, 신규 파일뿐이므로 한 PR로 묶어 `integration/dcvc`에 넣기 (커밋이 Phase별로 나뉘어 있어 cherry-pick으로 나누기 쉬움)
2. B-1·B-4를 먼저 반영 (실제 LLM 평가 전 필수)
3. B-8(판정 계층 출력) → DecisionRecord 실제 생산
4. W-1~W-7 (웹 engine 모드)
