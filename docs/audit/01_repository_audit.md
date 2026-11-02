# 01. TRIALOG 저장소 감사 보고서

> 이 보고서는 감사 전용입니다. 보고서 파일 생성 외에 코드·데이터·설정을 바꾸지 않았습니다.
> 표기: **[확인]** = 소스·테스트·실행으로 직접 확인 / **[추정]** = 근거 기반 판단이나 실행으로 증명하지 않음 / **미확인** = 확인하지 못함

## 1. 감사 시점과 대상

| 항목 | 값 |
|---|---|
| 감사일 | 2026-11-02 |
| 브랜치 | `feat/decision-inspector` (원격 추적 브랜치 없음) |
| HEAD | `62f744ec887cd937889fd96622bd70b19703bfd2` (`fix: validate inspector record provenance against run metadata`) |
| `main`과의 관계 | `origin/main...HEAD` = 6 / 4 (origin/main에만 6커밋, HEAD에만 4커밋) |
| 로컬 `main` | `64d471b`, `origin/main`보다 6커밋 뒤 |
| 작업 트리 | 감사 시작 시 깨끗함 (`git status` 출력 없음) |
| 무시된 기존 산출물 | `outputs/inspector/…`(10-24 생성), `.pytest_cache/`, `.venv/`, `src/ctrec.egg-info/` |
| 기타 | 로컬 브랜치 약 50개, 외부 worktree 다수 (`git branch -vv`의 `+` 표시). 이번 감사는 **HEAD 한 곳만** 다룸 |
| 환경 | Windows 11, Python 3.10.11 (.venv), anthropic 1.17.0, pydantic 2.14.1, fastapi 0.162.0, pytest 9.1.1 |

## 2. 디렉터리와 핵심 모듈 구조

추적 파일은 246개이고, `src/ctrec`의 Python은 약 13,000줄입니다(정적 JS 라이브러리 제외).

```
src/ctrec/
  pipeline.py          Session (환자 1명의 상태 + 6단계 실행), PatientCase, load_patients          535줄
  orchestrator.py      LLM 도구 호출 루프 (agent 모드, 웹 기본)                                       214
  cli.py               `ctrec run|batch` (agent / fixed 모드)                                          219
  schemas.py           Rule, ParsedTrial, PatientProfile, CriterionAssessment, TrialMatch 등           306
  agents/              criteria_parser, parser_validator, patient_profiler, screener, matcher,
                       question_generator, recommender, explainer, answerers
  tools/rule_engine.py 결정적 규칙 엔진 (age/sex/ecog/lab, 단위 환산)                                  295
  tools/ctgov.py       ClinicalTrials.gov v2 API
  trial_store.py       Frozen 기준표 로드/저장/잠금                                                    130
  store_patch.py       잠긴 기준표 패치 제안·승인·후보 저장소                                         411
  evidence.py          기준별 근거 요구 수준(TYPE_POLICY), 입력 출처                                   69
  status.py            외부 표현 상태(CRITERIA_MATCH …)                                                64
  acquisition.py       판정 불가 항목별 확보 시도 상태(MissingFact)                                    162
  hard_filter.py       나이·성별·모집상태 하드 필터
  fact_store.py        Patient Fact Store (사실 단위, 버전·충돌·검증 상태)                             286
  profile_facts.py     PatientProfile -> FactStore 어댑터                                              147
  fact_changes.py      사실 스냅샷·변경 집합                                                           238
  fact_view.py         사실 기반 근거 보기 + 근거 게이트                                               401
  fact_compare.py      사실 기반 Matcher 비교(진단)                                                    167
  dependency_graph.py  사실->항목->기준->시험 의존 그래프                                              245
  incremental.py       부분 재평가 + 전체 평가 동등성 비교(진단)                                       491
  decision_state.py    DecisionState / Action / Budget (진단)                                          369
  decision_policy.py   결정적 행동 후보·우선순위                                                       236
  decision_loop.py     행동 실행 루프 (기본 NoDispatch, 진단)                                          382
  question_priority.py 오프라인 질문 순서 정책 A–E, R                                                  204
  question_eval.py     오프라인 질문 정책 비교 환경                                                    755
  metrics.py           적격성·사실 확보·추림 지표                                                      246
  inspector.py         실행 기록 -> 정적 HTML (읽기 전용)                                              1334
  trace.py             Tracer (trace.jsonl + listener)                                                 38
  llm.py / call_guard.py / web_metering.py   LLM 호출·사용량·계측
  llm_pilot*.py        LLM 파일럿 준비(유료 실행은 코드 상수로 비활성)
  web/                 FastAPI app, budget(SQLite 원장), access, retention, paid_gate, display, static/
scripts/   26개 (컴파일·벤치마크 생성·평가·sweep·파일럿·Inspector 렌더)
eval/      evaluate.py, pilot/
data/      trials 26, parsed 26 + FROZEN.json, patients(provided/synthetic/masked/masked_validation), labels
tests/     56개 파일, 1,072개 테스트
```

## 3. 기능별 구현 상태

상태 기준: **완료** = 실제 실행 경로에 연결되고 테스트 있음 / **부분** = 일부만 구현됐거나 실행 경로에 연결되지 않음 / **진단 전용** = 구현과 테스트는 있으나 판정·질문·추천에 영향을 주지 않는 shadow 경로 / **미구현**

| # | 기능 | 상태 | 진입점·호출 경로 | 입력 → 출력 | 테스트 | 문제·통합 위험 |
|---|---|---|---|---|---|---|
| 1 | 환자 입력 | 완료 | `pipeline.load_patients` (pipeline.py:88), `_case_from_dict` (:70). 웹: `start_run` (web/app.py:594), `custom_text` 또는 `patient_id` | txt/JSON → `PatientCase` | test_stage0, test_evidence_source, web 테스트 | 환자 원문은 **자유 텍스트 한 덩어리**(`raw_text`)이고, 답변도 이 텍스트에 이어 붙임(pipeline.py:437) |
| 1b | Patient Fact Store | **진단 전용** | `Session._shadow_record` (pipeline.py:231)가 `profile_facts.ingest_profile` 호출 | `PatientProfile` → `PatientFact`(출처·검증·버전·충돌) | test_fact_store, test_profile_facts, test_session_fact_shadow | 주석(pipeline.py:148-150)대로 판정·질문·추천은 **FactStore를 읽지 않고 LLM이 다시 추출한 `PatientProfile`을 씀**. 기본은 메모리에만 저장 |
| 2 | 시험 수집·파싱·Frozen Store | 완료 (오프라인) | `scripts/fetch_trials.py` → `tools/ctgov.py`, `scripts/compile_trials.py` → `criteria_parser` + `parser_validator` → `trial_store.save`. 실행 시 `trial_store.get` (trial_store.py:123) | CT.gov JSON → `ParsedTrial` + `RuleMeta` | test_decisions (FROZEN 해시 검증 :253-256), test_validator_fields, test_store_patch | 런타임 `trial_store.load`(:107)는 `trial_hash`만 확인하고 **FROZEN.json 파일 해시는 확인하지 않음**(해시는 테스트에서만 확인). `LAZY_COMPILE=1`이면 즉석 LLM 파싱으로 넘어감(:128) |
| 3 | 후보 검색·매칭 | 완료 | `Session.screen` (pipeline.py:187) → `screener.screen`(LLM) + `hard_filter`. agent 모드에서는 `search_trials`(ctgov) | 시험 풀 → `ScreeningResult` | test_screening | 웹의 `allow_search`/`_search_trials` 경로는 **테스트 없음**(웹 하위 감사) |
| 4 | 기준별 적격성 판단 | 완료 | `Session.match` (pipeline.py:285) → `matcher.match_trial` (matcher.py:345): 검토 대상 → 규칙 엔진 → 남은 규칙을 **시험당 LLM 1회**로 일괄 판정 → `_enforce_evidence`/`_enforce_quote` → `_apply_evidence_policy` → `_select_cohort` → `decide` | `PatientProfile` + `ParsedTrial` → `TrialMatch` | test_decisions, test_safety, test_unit_conversion, test_recommender | 시험 적격성은 코드가 결정(matcher.py:86)하여 LLM이 최종 판정을 내리지 않음. LLM 판정의 **기준 단위 개별 호출은 없음**(부분 재평가 제약, 02 문서 M8) |
| 5 | Evidence directness / provenance / freshness | 부분 | 근거 요구 수준: `evidence.required_evidence` (evidence.py:41). 근거 출처: `matcher._evidence_source` (matcher.py:257, **문자열 위치 기반**). 인용 원문 대조: `_enforce_quote`. 사실 기반 게이트: `fact_view.gate` (fact_view.py:354, 진단 전용) | assessment → `evidence_source`, `needs_verification` | test_evidence_source, test_fact_view, test_fact_target_disambiguation | **freshness**: 규칙 엔진은 `time_window_days`가 있으면 LLM에 넘김(rule_engine.py:273). 관찰 기간 게이트는 진단 경로(fact_view.py:381-386)에만 있음. 실제 경로에서 검사값의 유효 기간은 LLM 판단에 의존 [확인]. 시험 정보의 freshness(CT.gov 재조회 주기)는 미구현 (`retrieved_at` 기록만 있음) |
| 6 | 근거 충돌·정보 불충분 | 부분 | 불충분: `holds=unknown` → `UNCERTAIN`, `NOT_EVALUATED`(pipeline.py:112). 충돌: `FactStore.relation`/`conflicts` (fact_store.py:135, 236), `fact_view` 상태 contradictory 등 | — | test_fact_store, test_fact_compare | 충돌 감지는 FactStore(진단)에만 있음. 실제 판정은 재추출한 프로필 하나만 보므로 **입력 간 충돌을 판정에 반영하지 않음**. `rule_engine._latest`는 같은 날 다른 값이면 판정하지 않음(:139) |
| 7 | 질문 생성·문서 확인·응답 | 완료 (질문) / 부분 (문서) | `Session.clarify` (pipeline.py:400) → `question_generator.generate_questions`(LLM) → `answerer.answer`. 답변 구현: `WebAnswerer` (web/app.py:242), `Interactive`, `Simulated`, `FactReveal`, `NoAnswerer` | `TrialMatch[]` → `ClarifyingQuestion[]` → `QAPair[]` | test_choice_questions, test_question_*, web 테스트 | **문서 확인 요청은 표시만 함**(`needs_verification` → 추천의 `verification_needed`). 문서를 받아 검증하는 경로는 없음. `REQUEST_DOCUMENT`도 기록만 함(decision_loop.py:214-216) |
| 8 | Fact Store 업데이트·재평가 | 부분 | 실제: 답변 텍스트 추가 → `profile_patient()` **LLM 전체 재추출** → 대상 시험 **전체 재매칭**(pipeline.py:437-441). 진단: `incremental.DependencyTracker` (incremental.py:467), `_run_dependency_diagnostic` (pipeline.py:338) | — | test_incremental, test_dependency_graph, test_session_dependency | 부분 재평가는 진단에서만 쓰임. LLM 기준은 `llm_all_fields`(모든 항목 의존)여서 재사용할 수 없음 |
| 9 | 다중 라운드·상태 관리 | 완료 (단일 환자) | `run_fixed` (pipeline.py:466), `needs_clarification` (:395). agent 모드에서는 LLM이 라운드 수를 결정(`max_rounds` 상한). 웹 상태는 프로세스 메모리 `RUNS` dict(web/app.py:209-211) | — | test_stage05, web 통합 테스트 | 상태가 **환자 1명 Session 단위로만** 있고 코호트(다수 환자) 상태 객체가 없음. 웹은 단일 프로세스·단일 인스턴스 전제(render.yaml) |
| 10 | Inspector·trace·감사 로그 | 완료 (읽기 전용) | `Tracer.log` (trace.py:25), `inspector.load_run`/`build_view` (inspector.py:100, 378), `scripts/render_inspector.py` | result.json + trace.jsonl + facts + run_meta → HTML | test_inspector (4개 skip: 심볼릭 링크) | Tracer는 **스키마가 없는 dict 이벤트**임(agent/event 문자열). Inspector·웹 JS가 이벤트 이름에 결합됨. 웹 UI에는 Inspector가 없음 |
| 11 | FastAPI·프런트엔드 | 완료 | web/app.py 14개 라우트, `_execute_run` (:423)이 `Orchestrator(session).run()` (:443) 호출. SSE `/api/runs/{id}/events` | — | test_web_* 9개 파일 (browser 1건은 `CTREC_BROWSER_TESTS=1`일 때만) | `CTREC_PASSWORD`가 비어 있으면 인증 없음(app.py:87). `/api/patients`가 번들 환자 원문 전체를 반환(:542). display.py·app.js에 recommender 로직 일부가 중복됨(하위 감사) |
| 12 | 평가·합성 데이터·테스트 | 부분 | `metrics.py`, `eval/evaluate.py`, `question_eval.run_method`, scripts/*sweep* | labels CSV ↔ result.json | test_masked_benchmark, test_question_eval 등 | 라벨은 LLM 생성이고 임상의 검토 없음(metrics.py:30-34). **케이스 ID가 정답을 담고 있고 LLM 프롬프트에 들어감**(P0-2). 질환이 섞여 있어 NSCLC 전용이 아님 |

### 문서에만 있는 기능 (docs/ARCHITECTURE.md 대비, 코드 grep 0건) [확인]
- §10 Medical Terminology Layer (UMLS/SNOMED/RxNorm 등): 코드 없음. 동의어는 `rule_engine.LAB_ALIASES`(rule_engine.py:20)와 `fact_view` 바이오마커 표적 사전 수준
- §30 Active/Exploration Pool, §47 STABLE_WITHIN_SEARCH_SCOPE, §38 Counterfactual Decision Impact: 없음
- §56 Freshness(시험 정보 재조회): 없음 (`retrieved_at` 기록만)
- §58 Multi-user/Async: 웹은 메모리 단일 프로세스
- §51 Policy Engine: `decision_policy`가 진단 전용으로만 존재. 실제 질문 경로는 LLM `question_generator`

## 4. 주요 호출 흐름

### 4.1 웹 (실제 사용자 경로)
```
POST /api/runs (web/app.py:594)
  -> CSRF·paid gate·원장·screen_pool -> 스레드 _execute (:391) -> _execute_run (:423)
     Session(answerer=WebAnswerer, decision_loop="diagnostic"|"off")   (:437-440)
     Orchestrator(session).run()                                       (:443, orchestrator.py:158)
        LLM 도구 루프: profile_patient / screen_trials / evaluate_trials(=Session.match)
                     / ask_clarifying_questions(=Session.clarify) / finalize_recommendation
        모델이 finalize하지 않으면 고정 순서로 마무리                    (orchestrator.py:205-214)
     Session.clarify: question_generator(LLM) -> WebAnswerer (SSE "questions", 0.2초 간격 폴링,
                      시간 초과 시 "모름") -> raw_text에 이어 붙임 -> profile_patient(LLM 재추출) -> match(LLM 재판정)
     Session.finalize: recommender(LLM + 코드 검증) -> explainer(LLM)
```
### 4.2 CLI / 오프라인
- `ctrec run|batch --mode agent|fixed` (cli.py:80-95): `Orchestrator` 또는 `Session.run_fixed`
- `question_eval.run_method`: `run_fixed` + `use_generator`로 질문 생성기를 교체(question_generator.py:72). 후보 정책은 `question_priority.generator_factory`(:182)가 `decision_policy.candidates`를 호출
- `scripts/decision_scenarios.py`는 `decision_loop.run_loop`를 직접 호출
- **결론 [확인]**: 오프라인에서 평가하는 질문 정책(decision_policy/question_priority)과 웹 질문 경로(LLM question_generator + LLM orchestrator)가 **서로 다른 코드**임

## 5. 테스트 결과

| 실행 | 결과 |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -rs` (저장소, `ANTHROPIC_API_KEY` 제거) | **1062 passed, 5 failed, 5 skipped** (180.8초) |
| 실패 5건 재실행: 같은 3개 파일을 `git archive HEAD` 깨끗한 사본에서 실행 | 100 passed, **1 failed** (`test_frozen_store_and_policy_files_unchanged`: 사본에 `.git`이 없어 `git diff` 실패) |

- 실패 5건(`test_llm_pilot.py:472`, `test_llm_pilot_exec.py:117`, `:438`, `test_pilot_process.py:82` ×2)은 모두 `assert not (ROOT/"outputs").exists()`가 원인입니다. 감사 전(10-24)부터 있던 gitignore 대상 `outputs/inspector/`가 있어 실패했고, 깨끗한 사본에서는 통과했습니다 → **환경 원인이며 코드 결함 아님 [확인]**. 다만 이 테스트들은 실행 산출물 폴더가 있으면 실패하도록 짜여 있어 **환경 의존적**입니다(P2-5).
- skip 5건: 브라우저 테스트(`CTREC_BROWSER_TESTS=1` 필요) 1건, Windows에서 심볼릭 링크를 만들 수 없어 4건(test_inspector.py:331).
- 실제 LLM API는 호출하지 않았습니다(키 제거, 테스트는 FakeLLM/stub 사용). **실제 모델 판정 품질과 임상적 정확성은 검증 범위 밖**입니다.
- 자세한 명령·출력은 04_audit_evidence.md에 있습니다.

### 테스트가 없거나 약한 핵심 경로
- 웹 `allow_search` / `Orchestrator._search_trials` (CT.gov 검색)
- 웹 다중 라운드(2라운드 이상), 답변 시간 초과 시 자동 "모름", 웹 경유 orchestrator fallback-finalize
- `eval/evaluate.py` main, `budget_sweep`, `question_policy_sweep`, `eval_question_priority`, `generate_synthetic_patients`, `fetch_trials`, `parse_consistency`, `review_store`, `screen_budget`
- `SimulatedPatientAnswerer` 직접 테스트 없음 (간접 커버 여부 미확인)
- **patient_id/case ID가 프롬프트로 정답 정보를 넘기지 않는지 확인하는 테스트 없음** (`test_hidden_facts_never_reach_pipeline`은 `raw_text`만 검사, tests/test_masked_benchmark.py:25-33)
- 실제 LLM 출력 기반 회귀(골드셋) 테스트 없음

## 6. 확인된 결함과 기술 부채

1. **평가 정답 누출 경로(케이스 ID)** [확인]: masked 101건 중 72건의 ID에 `rescue`/`cleanup`(=ELIGIBLE/INELIGIBLE 정답, metrics.py:9-11 정의)이 들어 있고, validation 100건 중 96건의 ID에 대상 기준 ID(`-I9-` 등)가 들어 있습니다. 이 ID가 `patient_profiler` 프롬프트(patient_profiler.py:26), orchestrator 첫 메시지(orchestrator.py:161), 그리고 `profile.model_dump_json()`을 통해 matcher(matcher.py:321)와 question_generator·recommender 프롬프트에 그대로 들어갑니다. 오프라인 stub 평가에는 영향이 없지만, **실제 LLM으로 masked 세트를 batch/sweep하면 결과가 오염될 수 있습니다**.
2. **실행 경로와 평가 경로의 정책 불일치** [확인]: 4.2 결론 참고.
3. **Fact Store가 판정 경로와 분리됨** [확인]: 판정은 매 라운드 LLM이 다시 추출한 프로필에 의존합니다. 사실 이력·충돌·검증 상태가 판정에 반영되지 않습니다.
4. **재평가가 전체 재추출·재매칭** [확인]: 라운드마다 profiler 1회 + 대상 시험 수만큼 matcher LLM 호출이 생깁니다. 비용이 크고 판정이 비결정적입니다.
5. **근거 출처 판정이 문자열 위치 기반** [확인]: `evidence.split_source`(evidence.py:66)가 `ANSWER_MARKER` 문자열로 원문과 답변을 나눕니다. 환자 원문에 같은 표식이 들어 있으면 출처가 잘못 분류될 수 있습니다 [추정].
6. **런타임 Frozen 무결성 미확인** [확인]: trial_store.py:107-120.
7. **시험 풀이 NSCLC 연구에 맞지 않음** [확인]: 26개 중 실제 NSCLC 시험은 0개입니다(`DEMO-LUNG-001`은 합성). 나머지는 췌장염·그레이브스병·망막박리·소아 신증후군 등입니다.
8. **trace 스키마 부재** [확인]: `Tracer.log(agent, event, **data)`가 자유 형식이라 행동 단위 근거를 구조적으로 기록할 계약이 없습니다.
9. **웹/표시 중복 로직**: `web/display.py` `exclusion_kind`(:43-57)가 recommender.py:280-296을 다시 구현했고, app.js `DECLINED`(:10)가 decision_state.py:51을 복사했으며, trace 메시지를 `split(":")`로 파싱합니다(app.js:418). 출처는 웹 하위 감사이고 줄 번호 일부는 미확인.
10. **평가 조인**: `metrics.eligibility_metrics`가 (patient, trial) 내부 조인을 해서, 누락된 예측이 분모에서 빠지고 감점되지 않습니다. `baseline_gate`는 정확도만 비교합니다(metrics.py:40-48, 105-131).
11. **설정 파싱 취약성**: web/app.py:62-63 `int(os.getenv(...))`에 잘못된 값이 들어가면 import 단계에서 오류가 납니다(하위 감사).
12. 미사용·보존 코드: `question_eval`의 Phase 4-2 `factories` 경로("비교용으로만 보존"), `scripts/eval_question_plans.py`는 대부분 대체됨. TODO/FIXME 표식은 0건입니다.

## 7. 위험 목록

### P0 (연구 결과의 타당성이나 안전에 직접 영향, 통합 전 해결 필요)
- **P0-1** 오프라인 정책 평가 결과가 웹 실제 경로에 적용된다는 근거가 없음 (경로 불일치, §4.2). DCV+C 연구의 핵심 주장(M14)과 정면으로 충돌함.
- **P0-2** 케이스 ID를 통한 정답 누출 (§6-1). 실제 LLM 평가를 하기 전에 ID 익명화가 필요함.
- **P0-3** 연구 대상(NSCLC) 시험·코호트 데이터 없음 (§6-7). M11 데이터셋 구축이 다른 모든 실험의 선행조건임.

### P1 (통합 시 결함·비용 위험)
- **P1-1** Fact Store가 판정 경로와 분리되어, 사실 기반 재평가(M8)·충돌 관리(M4)를 바로 쓸 수 없음
- **P1-2** LLM 판정이 시험 단위 일괄 호출이라 기준 단위 부분 재평가를 할 수 없음
- **P1-3** 상태가 환자 1명 Session 단위이고 코호트 상태·예산이 없음 (M1/M2)
- **P1-4** trace·Inspector에 행동 단위 기록 계약(스키마) 없음
- **P1-5** 문서 확인(REQUEST_DOCUMENT)이 실행·검증되지 않고 표시만 됨 (M6/M9)
- **P1-6** 평가 지표가 누락 예측을 감점하지 않음 (§6-10)
- **P1-7** `pipeline.py`·`web/app.py`·`schemas.py`에 변경이 몰림 (git 이력 14/15/9회). 여러 사람이 동시에 수정하면 충돌 위험이 큼

### P2 (품질·운영)
- **P2-1** 비밀번호 미설정 시 웹 인증 없음, 무차별 대입 제한 없음 (web/app.py:87)
- **P2-2** `/api/patients`가 번들 환자 원문 전체를 반환 (web/app.py:542). 번들 데이터의 비식별·합성 여부는 미확인
- **P2-3** 런타임 Frozen 해시 미확인
- **P2-4** 근거 출처의 문자열 표식 의존
- **P2-5** 테스트 5건이 `outputs/` 존재 여부에 의존하고, 1건은 `.git`에 의존 (CI·사본 환경에서 실패 가능)
- **P2-6** 웹·JS 중복 로직, trace 메시지 문자열 파싱
- **P2-7** `deploy/web-record.env.example`의 `ANTHROPIC_API_KEY=`·`CTREC_PASSWORD=` 줄은 값이 비어 있고 같은 줄에 주석이 붙어 있음 [확인, 값은 노출하지 않음]. 줄 끝 주석을 지원하지 않는 env 로더에서는 주석 문자열이 값으로 읽힐 수 있음 [추정]
