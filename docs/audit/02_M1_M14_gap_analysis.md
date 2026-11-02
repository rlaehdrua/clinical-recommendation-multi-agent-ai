# 02. DCV+C 연구 통합 가능성: M1–M14 갭 분석

> 기준 커밋 `62f744e` (`feat/decision-inspector`). M1–M14는 설계 결정 사항이며, 아래 어떤 항목도 "구현 완료"로 판정하지 않습니다.
> **[확인]** = 코드로 확인 / **[추정]** = 설계 판단 / **미확인** = 확인 불가

## 0. 공통 결론 (핵심 질문 6개)

| 질문 | 판정 | 근거 |
|---|---|---|
| `pipeline.py`를 바꾸지 않고 공통 정책 엔진을 추가할 수 있는가? | **오프라인은 가능, 웹 실제 경로는 불가** | 오프라인: `question_generator.use_generator`(question_generator.py:72)로 질문 생성기를 교체할 수 있고, `decision_loop.run_loop`는 `QuestionChannel`을 주입받음(decision_loop.py:58, 262). 웹: `Session.clarify`가 LLM 생성기·`answerer` 호출·raw_text 덧붙임·재추출·재매칭을 한 함수에 묶어 둠(pipeline.py:400-442). 질문 시점은 orchestrator LLM이 정함(orchestrator.py). 따라서 **`Session`에 정책 주입 지점을 하나 추가하는 최소 변경**이나 **새 실행기(예: `PolicyDrivenSession`)**가 필요함. 권장 방식은 03 문서 §2 |
| Fact Store 업데이트 뒤 영향받은 기준만 재평가할 수 있는가? | **규칙 엔진 기준만 가능** | `dependency_graph.resolve`(dependency_graph.py:72)는 규칙 엔진 기준만 `explicit_field`로 좁게 의존시키고, LLM 기준은 `llm_all_fields`로 처리함(docstring :12-18). `incremental.update`(incremental.py:244)가 재사용 여부를 정하고 `compare_states`(:390)로 전체 평가와 같은지 검증함. 하지만 실제 matcher는 **시험 단위로 LLM을 1회 일괄 호출**(matcher.py:316-343)하고, 판정 입력도 FactStore가 아니라 재추출한 프로필임. LLM 기준을 부분 재평가하려면 기준 단위 assessor와 "LLM에 넘긴 입력 항목" 제한(입력 최소화)이 필요함 |
| trace·Inspector가 행동별 근거와 재평가 이력을 기록할 수 있는가? | **부분** | `Action`(decision_state.py:54)에 `reason_code`, `evidence_refs`, `target_criterion_ids`, `status`, `record`가 있고, `LoopResult.summary`가 trace `decision` 이벤트로 남음(pipeline.py:335). `dependency_diagnostics`도 trace에 남음. 하지만 Tracer는 스키마가 없는 dict이고(trace.py:25), Inspector는 result.json·trace를 **사후에 해석**함(inspector.py:378). "행동 → 비용 → 결과 사실 → 재평가된 기준 → 판정 변화"를 잇는 키(action_id ↔ fact_id ↔ criterion 버전)를 담은 **레코드 계약이 없음** |
| 웹과 오프라인이 같은 정책 함수를 쓸 수 있는가? | **현재는 아님. 구조적으로는 가능** | 현재 웹은 LLM orchestrator + LLM question_generator, 오프라인은 `decision_policy.candidates` + `question_priority`를 씀(01 문서 §4.2). `decision_policy.candidates(st, cfg, pending)`는 `DecisionState`만 입력으로 받는 **순수 함수**라(decision_policy.py:122) 공통 엔진의 핵심으로 재사용할 수 있음 |
| 합성 데이터의 숨긴 정답이 정책 입력으로 누출될 수 있는가? | **있음 (ID 경로)** | 숨긴 필드는 `_HIDDEN_KEYS`(pipeline.py:63-67)와 `question_eval.load_cases`(question_eval.py:120-141)에서 제거됨 [확인]. 그러나 **케이스 ID가 정답(rescue/cleanup, 대상 rule_id)을 담고 있고** profiler·orchestrator·matcher 프롬프트에 들어감(patient_profiler.py:26, orchestrator.py:161). `OracleAnswerer`는 EXACT 키가 하나만 맞아도 숨긴 사실을 모두 반환함(question_eval.py:352-358, 하위 감사)→ 회수율 과대 추정 |
| 임상적 판정과 운영상 검증 우선순위를 코드 수준에서 분리할 수 있는가? | **가능. 이미 일부 분리됨** | 판정: `matcher.decide`/`_select_cohort`(matcher.py:86-124), `status.external_status`. 우선순위: `decision_policy`(value_score는 "임상 적합성 판단에 쓰지 않음"이라고 명시, decision_policy.py:10). `DecisionState`는 판정을 **바꾸지 않고 복사만** 함(decision_state.py:1-7). 남은 일은 이 경계를 **타입·모듈 의존 방향으로 강제**하는 것(정책 모듈이 판정 결과에 쓰지 못하게 함) |

---

## M1. DCV 후보 선별 뒤 코호트 단위 최적화
- **재사용 가능**: 후보 선별(`screener.screen`, `hard_filter.apply`), 환자 단위 `DecisionState`·`build_state`(decision_state.py:259), 시험별 `open_paths`·`mismatch` 분류(:344), `decision_policy.candidates`(행동 후보 + value_score)
- **신규 필요**: `CohortState`(환자 N명 × 시험 M개 쌍 상태), 코호트 공통 예산을 나누는 최적화기(탐욕적·배낭형 등). 환자 간 행동 후보를 합치는 `cohort_candidates()`
- **수정·확장 파일**: 신규 `src/ctrec/cohort/` 패키지. `decision_policy.py`는 그대로 두고 감싸기만 함
- **충돌 위험**: `DecisionState`가 `session_id`/`patient_id` 하나를 전제함(decision_state.py:156-160). 확장하지 말고 **여러 개를 담는 상위 객체**로 감싸야 기존 테스트가 유지됨
- **선행조건·테스트**: M2(비용 모델), M3(목적 함수) 정의가 먼저. 테스트: 환자 1명 코호트의 결과가 기존 단일 환자 정책 순서와 같아야 함(동등성 회귀)

## M2. 코디네이터 능동 작업시간 예산
- **현재**: `Budget`(decision_state.py:138-144)이 iterations/tool_calls/cost_units/questions/llm_calls/seconds를 관리하고, `PolicyConfig.cost_*`(decision_policy.py:41-44)는 무차원 상수(ask 1.0, document 2.0 …). `question_priority.BURDEN`(:39-41)은 순서용 부담 등급. **분(minute) 단위 코디네이터 시간 모델 없음** [확인]
- **재사용**: `Budget`/`BudgetUsage` 구조, 정책의 비용 차감 위치
- **신규**: `ActionCostModel`(행동 유형 × 정보원 × 소요 분, 대기시간은 능동 시간과 분리), 코호트 예산 원장(오프라인용. 웹 `web/budget.py`는 LLM 비용 원장이므로 섞지 않음)
- **충돌 위험**: 웹 `RunBudget`(LLM 토큰·달러)과 이름·개념이 겹침. 모듈 이름을 `effort_budget` 등으로 분리 권장
- **테스트**: 예산을 넘는 행동이 선택되지 않음, 같은 문서 행동을 두 번 청구하지 않음(M7 연계)

## M3. 정확하게 확정된 적격 환자–시험 쌍 최대화
- **재사용**: `metrics.eligibility_metrics`(decided_accuracy, premature_match, false_removal), `status.external_status`의 CRITERIA_MATCH
- **신규**: 목적 함수 `confirmed_correct_pairs`(정답 ELIGIBLE이고, 시스템이 검증 근거로 ELIGIBLE 확정한 쌍), 오확정 패널티, 쌍 단위 정답 테이블(코호트용)
- **수정**: `metrics.py`에 함수 추가. **조인 방식 수정 필요**: 누락 예측을 분모에 넣어야 함(현재 내부 조인, metrics.py:107)
- **충돌**: `baseline_gate`(metrics.py:40-48)가 정확도만 봄. 목적 함수를 바꾸면 기존 게이트 의미가 달라짐 → 새 게이트를 별도로 둠
- **테스트**: 손으로 만든 소형 코호트 정답표로 지표 단위 테스트

## M4. 기준별 근거 요구 수준과 출처·시점·충돌 관리
- **재사용 (강함)**: `evidence.TYPE_POLICY`(evidence.py:22-35), `RuleMeta.required_evidence`, FactStore의 `source_type`·`verification_status`·`observed_at`·`relation`(fact_store.py:75-150), `fact_view.gate`(ungrounded / conflict / observation_window / self_report_only …, fact_view.py:354)
- **갭**: 실제 판정 경로는 FactStore를 쓰지 않음(문자열 위치 기반 `_evidence_source`, matcher.py:257). 근거 요구 수준이 이진(`self_report_ok`/`clinical_record`)이라 "최근 N일 이내 문서" 같은 **시점 요구를 기준 메타데이터로 표현하지 못함**(`time_window_days`는 Rule에만 있음)
- **신규**: `EvidenceRequirement`(수준 + 최대 경과일 + 허용 정보원 목록), 실제 판정에 `fact_view.gate` 적용(플래그로 전환)
- **수정 파일**: `schemas.RuleMeta`(필드 추가는 기본값으로 하위 호환), `evidence.py`, Frozen 기준표 재컴파일이 필요할 수 있음 → **FROZEN 해시 테스트(test_decisions.py:253)와 충돌**하므로 store_patch 절차(store_patch.py)를 따라야 함
- **테스트**: gate는 확정을 판정 불가로 낮추기만 하고 올리지 않음(기존 성질 유지), 기간 밖 사실은 판정 불가

## M5. 검증 필요 상태와 미해결 쌍의 구분
- **재사용**: `CriterionAssessment.needs_verification`·`self_reported_holds`(schemas.py:192-194), `Gap` = missing/unverified/physician_review/conflict/stale(decision_state.py:49), `MatchStatus` POTENTIAL_MATCH / INSUFFICIENT_INFORMATION / NEEDS_REVIEW(schemas.py:19)
- **갭**: 쌍(환자–시험) 수준의 상태 머신이 없음. `TrialMatch.eligibility`는 4값이고 "검증만 남음 / 정보 부족 / 전문가 검토 대기 / 행동 실패로 보류"를 구분하지 않음
- **신규**: `PairStatus` 열거형 + 전이 규칙(코호트 계층). 기존 `eligibility`는 바꾸지 않고 파생시킴
- **테스트**: 모든 (eligibility, gap 집합) 조합 → PairStatus 매핑 표 테스트

## M6. 비임상적 검증은 자동화, 임상의 판단은 별도
- **재사용**: `needs_physician_review`(matcher.py:82), `underspecified_state`(schemas.py:67), `NEEDS_REVIEW` 행동, `RuleMeta.review`(파싱 검증 결과 자동 판정 금지), decision_loop `EXECUTOR_MODES`(VERIFY = 코드 확인, decision_loop.py:36-46)
- **갭**: "비임상 검증"(문서 존재·날짜·출처 일치 확인)을 실행하는 실행기가 없고 `REQUEST_DOCUMENT`는 기록만 함(decision_loop.py:214-216). 임상의 판단 큐 없음
- **신규**: `DocumentVerifier`(시뮬레이터: 숨긴 문서 사실 공개 / 웹: 업로드·수동 확인 입력), `ClinicianReviewQueue`(판정 권한 분리: 큐 결과만 `document_verified`나 판정으로 반영)
- **충돌**: `FactStore.set_verification`(fact_store.py:210)은 `document_reference`와 사유를 요구함. 자동 검증기가 이 계약을 지켜야 함
- **테스트**: 자동 검증기가 임상 판단 기준(`underspecified`, category=diagnosis 해석)을 확정하지 못함

## M7. 문서·정보원 단위 행동 묶음과 비용 중복 방지
- **재사용**: `info_key`(decision_state.py:217-227: 구조화 필드·바이오마커 표적으로만 시험 사이를 묶음), `fingerprint` 중복 차단(decision_policy.py:54), `acquisition.POLICIES`(category → 선호 정보원 문구, acquisition.py:30-51)
- **갭**: 정보원(병리 보고서, 혈액검사지, 처방 목록 등) 개념이 **문자열 안내문에만 있음**. "문서 1건이 기준 N개를 해결" 구조 없음
- **신규**: `InformationSource` 카탈로그(문서 유형 → 제공 가능 항목 → 비용), `ActionBundle`(같은 정보원의 행동 묶음 + 비용 1회 청구)
- **테스트**: 같은 문서로 해결되는 기준 3개 → 비용 1회, 같은 환자의 같은 문서 재요청 차단

## M8. 의존성 기반 부분 재평가
- **재사용 (강함)**: `dependency_graph.DependencyGraph`·`analyze_impact`, `incremental.update`·`compare_states`·`DependencyTracker`, `fact_changes.detect_changes`. 진단 경로에서 전체 평가와 동등성을 검증하는 장치가 이미 있음(incremental.py:390)
- **갭**: (1) 실제 판정 입력이 FactStore가 아님 (2) LLM 기준은 전체 항목 의존 (3) matcher가 시험 단위 일괄 호출이라 기준 하나만 LLM으로 다시 판정하는 API가 없음
- **신규**: `assess_criterion(rule, fact_view_subset)`(LLM 기준 단위 판정, 입력 항목을 `LLM_CATEGORY_FIELDS`로 **제한해서 넘기고 그 제한을 의존성으로 기록**). 이렇게 해야 `llm_all_fields`를 좁힐 근거가 생김 [추정]
- **수정**: `agents/matcher.py`에 함수 추가(기존 `match_trial`은 유지), `dependency_graph.resolve`에 새 resolution 추가
- **충돌**: matcher.py는 변경이 잦은 파일(8회)이고 테스트가 많음. 새 함수는 별도 모듈(`agents/criterion_assessor.py`)에 두는 것을 권장
- **테스트**: 오프라인에서 부분 재평가 결과 = 전체 재평가 결과(기존 `compare_states` 활용), 영향 밖 기준은 LLM 호출 0건

## M9. 검증 행동의 실패·미응답·대체 정보원
- **재사용**: `acquisition.MissingFact` 상태와 `alternatives` 단계(acquisition.py:64-125), DEFER `user_declined_or_exhausted`(decision_policy.py:153-160), `DECLINED_ANSWERS`, 종료 사유 `tool_failure`/`timeout`
- **갭**: 시뮬레이터에 **확률적 미응답·실패 모델이 없음**(OracleAnswerer는 결정적). 대체 정보원은 안내문 문자열이라 행동으로 바꿀 수 없음
- **신규**: `ResponseModel`(정보원별 성공률·지연·부분 응답, seed 고정), 실패 시 대체 정보원으로 넘어가는 정책 규칙
- **테스트**: seed가 같으면 결과도 같음, 실패한 행동의 비용은 청구하되 사실은 추가하지 않음

## M10. 같은 조건의 기준선·ablation 비교
- **재사용**: `question_eval.run_method`(같은 케이스·시험·예산·oracle·Session 경로에서 baseline/candidate/ablation 실행, question_eval.py:7-14), `question_priority` A–E·R_random(seed), `scripts/budget_sweep.py`, `question_policy_sweep.py`
- **갭**: 비교 단위가 환자별 질문 순서뿐임. 코호트 예산 배분 정책 비교, 고정 seed의 실패 모델, 반복 실행 신뢰구간이 없음. 실제 LLM sweep은 비결정적(temperature 제어 없음)
- **신규**: `ExperimentSpec`(정책·예산·seed·데이터 버전 해시), 결과 manifest, 기준선 정책 모음(무작위 / 환자 순서대로 / 탐욕 1단계 / 제안 정책 / ablation: 묶음 끔·부분 재평가 끔·근거 수준 끔)
- **테스트**: 같은 spec을 두 번 실행하면 결과 해시가 같음

## M11. NSCLC 단일 질환군, 사실 우선 합성 코호트
- **현재**: 시험 26개 중 실제 NSCLC 시험 0개(`DEMO-LUNG-001`은 합성) [확인]. 합성·masked 환자는 다질환(하위 감사)
- **재사용**: `scripts/fetch_trials.py`(CT.gov 조회), `compile_trials.py` + `parser_validator` + `store_patch`(검수·잠금), `build_masked_benchmark.py`의 `oracle_facts`/`hidden_facts` 구조(사실 우선 설계와 맞음), `fact_view` 바이오마커 표적 사전(EGFR, PD-L1 등)
- **신규**: NSCLC 시험 세트(예: 20–40개) 수집·컴파일·검수·동결, **사실 우선 생성기**(먼저 사실 테이블 → 정답 쌍 라벨을 규칙으로 계산 → 그다음 서술문 생성), 정보원별 사실 배치(어떤 사실이 어떤 문서에 있는지)
- **충돌**: FROZEN.json에 시험이 추가되면 `test_decisions` 해시 테스트와 기존 masked 세트의 `candidate_trials` 가정이 바뀜 → **별도 저장소 디렉터리**(`data/nsclc/…`)에 두고 `store_dir` 인자(trial_store 함수들이 이미 지원)로 분리
- **테스트**: 생성기는 결정적이어야 함. 라벨은 코드(규칙)로 다시 계산 가능해야 함. 서술문이 정답 이외의 정보를 담지 않는지 검사

## M12. 규칙으로 판정 가능한 기준과 전문가 판단 기준의 분리
- **재사용**: `Rule.structured_evaluable`·`underspecified`, `RuleMeta.review`·`underspecified_state`, `CriterionAssessment.method`(rule_engine / llm), `dependency_graph` evaluator 구분
- **갭**: 3분류(규칙 판정 / LLM 사실 판정 / 전문가 판단)가 명시 필드로 없음. `method`는 결과 기록일 뿐 기준의 성격 분류가 아님
- **신규**: `RuleMeta.adjudication`(rule | fact_llm | expert) + 검수 도구. 전문가 기준은 정책 목적함수에서 "자동 확정 불가"로 다룸
- **테스트**: expert 기준은 어떤 경로로도 MET/NOT_MET이 자동 확정되지 않음

## M13. 효율성 최적화와 환자 간 공정성 감시
- **현재**: 공정성 지표 없음(`accuracy_by_group`은 case_type별 정확도뿐) [확인]
- **신규**: 환자별 행동·예산 배분 분포, 환자별 확정 쌍 비율, 최소 보장(floor) 제약, Gini·최솟값 감시 지표. 하위 그룹 정의는 합성 속성(나이·성별·정보원 접근성)으로 한정
- **충돌**: 없음(신규)
- **테스트**: 효율 최대 정책이 특정 환자를 0 배분하면 감시 지표가 경보를 냄

## M14. 오프라인 시뮬레이터와 웹이 공유하는 공통 정책 엔진
- **재사용**: `decision_policy.candidates`/`validate`(순수 함수), `decision_loop.run_loop` + `QuestionChannel` 프로토콜(decision_loop.py:58), `WebAnswerer`(웹 답변 채널)
- **갭**: 웹은 LLM orchestrator가 질문 시점을 정하고 LLM이 질문을 만듦. 행동 → 질문 문구 변환(WHAT/HOW 분리, ARCHITECTURE §42)이 없음. 오프라인 질문 생성기는 stub
- **신규**: `PolicyEngine` 인터페이스(03 문서), `ActionRenderer`(행동 → 질문/문서 요청 문구, LLM 사용 가능하나 **행동 선택에는 관여하지 않음**), 웹 `QuestionChannel` 어댑터, 웹 실행 모드 플래그(`policy_mode = "legacy" | "engine"`)
- **수정**: `web/app.py:_execute_run`(:423-447), `pipeline.Session`(주입 지점), 프런트엔드 질문 폼 계약(`ClarifyingQuestion` 형식 유지 시 JS 변경 최소화)
- **충돌**: 01 문서 P1-7 참고(app.py·pipeline.py 변경 집중). 기존 웹 테스트(web_harness의 고정 orchestrator 스크립트)는 legacy 모드로 유지
- **테스트**: **같은 `DecisionState` 입력 → 오프라인과 웹 어댑터가 같은 행동 ID 순서**(골든 파일 비교). 웹 경로에서 엔진 모드일 때 orchestrator LLM 호출 0건

## 요약 표

| M | 재사용도 | 신규 규모 | 핵심 선행조건 |
|---|---|---|---|
| M1 | 중 | 대 | M2, M3, M5 |
| M2 | 하 | 중 | 행동·정보원 정의(M7) |
| M3 | 중 | 소 | 정답 쌍 테이블(M11) |
| M4 | 상(진단 경로) | 중 | FactStore를 판정 입력으로(M8) |
| M5 | 상 | 소 | — |
| M6 | 중 | 중 | M4, 검증 권한 계약 |
| M7 | 중 | 중 | 정보원 카탈로그(M11과 함께) |
| M8 | 상(규칙 기준) / 하(LLM 기준) | 대 | 기준 단위 assessor |
| M9 | 중 | 중 | M7, seed 정책 |
| M10 | 상 | 중 | ExperimentSpec |
| M11 | 중(도구) / 없음(데이터) | 대 | 시험 수집·검수 인력 |
| M12 | 중 | 소 | 검수 기준 합의 |
| M13 | 없음 | 소 | M1 결과 구조 |
| M14 | 중 | 대 | 공통 인터페이스 동결(03 문서) |
