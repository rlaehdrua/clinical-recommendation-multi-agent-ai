# N 감사: 최초 6단계 파이프라인 완성도 (2026-11-07)

- 브랜치: `audit/six-stage-pipeline` (로컬, 푸시 안 함). 기준 = `origin/fix/specimen-value-binding`(d775bb6) + `origin/pr/de-5-r5-integration`(efe3354) 병합 4ebd569.
  tree 29a7ae3b가 P의 기준(a42d50d)과 같음. `origin/integration/dcvc`(64a5011)는 D+E 안전 스택이 없어 O·P와 충돌하므로 기준으로 쓰지 않음.
- N 커밋: `befb436` (기능), `2c9c4cd` (E2E 회귀), 문서 커밋은 아래 SHA 목록 참고.
- 실행 방식: 실제 Orchestrator 루프·Session/ContractSession·trial_store·rule_engine·Matcher 코드 검증·acquisition·recommender·
  FactStore·Inspector·FastAPI 앱을 그대로 쓰고 LLM만 시나리오 대역(`tests/six_stage_harness.py`). **실제 Claude 호출 0건**
  (API 크레딧 차단 상태, 별도 승인 없음). 따라서 "실제 LLM 판단 품질"은 어느 항목에서도 PASS로 보지 않음.

판정 기호: **PASS** 실제 경로 E2E로 확인 · **PARTIAL** 일부 구현, 실행 경로 누락 · **FAIL** 미구현·실행 실패 · **BLOCKED** 팀 임상 검수·외부 계약 필요.
"단위"는 기존 단위 테스트만 있고 이번 E2E로 확인하지 않은 경우.

## 1. 6단계 기능 체크리스트

### ① 임상시험 기준 입력

| 항목 | 판정 | 근거 (파일·함수 / 테스트) |
|---|---|---|
| CT.gov 실제 NSCLC 프로토콜 로딩 | **PASS** (수정 후) | 수정 전 FAIL: 런타임 시험 풀(`config.TRIALS_DIR=data/trials`)에 NSCLC 실제 시험이 없고 `data/nsclc/`는 어느 경로에도 연결 안 됨. `config.TRIAL_SETS`/`CTREC_TRIAL_SET=nsclc`, `cli --trial-set nsclc` 추가 → 웹 `/api/trials`, `_execute_run`, CLI, `trial_store.get`이 같은 세트를 읽음. `test_stage1_nsclc_trial_set_reaches_session_parser_store`, `test_web_nsclc_...`, `test_cli_trial_set_and_inspector_flag` |
| 기준 파싱·저장 | **PARTIAL / BLOCKED** | 런타임 파싱은 없음(D8: `trial_store.get`이 사전 컴파일본만 읽음, `criteria_parser`·`parser_validator` LLM은 `scripts/compile_trials.py`에서만). NSCLC 8개 중 6개만 파싱본 있음 - NCT04892953·NCT05255302는 NOT_EVALUATED(COMPILATION_PENDING). 파싱본은 WP2a 검수 전 → 임상 검수 BLOCKED (서연·동겸) |
| inclusion / exclusion | **PASS** | `matcher.match_trial` + recommender 결합. `test_H_confirmed_ineligible_persists_through_questions[exclusion]` |
| OR / 조건부(코호트) | **PARTIAL** (단위) | `matched_cohort`·코호트 기준(NCT07027514 2개, NCT07098338 3개)은 있음. OR 경로의 생성기 패치(issue 23)는 미적용. 이번 E2E에서 OR·코호트는 확인 안 함 |
| underspecified·clinician-only 분류 | **PASS** | `clinician_judgment_required` → `NEEDS_REVIEW`. `test_F_clinician_judgment_criterion_goes_to_expert_review` |
| 파싱 실패·미연결 기준의 정직한 보류 | **PASS** | 파싱본 없음 → NOT_EVALUATED(추천 제외). "Other protocol-defined criteria apply" → NEEDS_REVIEW. `test_G_...`, `test_stage1_...` |
| 임상시험 후보 선정 | **PARTIAL** (단위) | `screener.screen`(LLM) + `hard_filter`(코드)는 풀 > `SCREEN_MAX_CANDIDATES`(5)일 때만. NSCLC 세트 8개는 웹 `screen=True`에서 추림을 탐. 이번 E2E는 `screen=False`/`given`만 |

### ② 환자 프로파일 입력

| 항목 | 판정 | 근거 |
|---|---|---|
| 웹 환자 자가 입력 | **PASS** | `POST /api/runs {custom_text}` → `PatientCase(WEB-…)`. `test_web_nsclc_question_answer_reevaluation_and_result` |
| 프로필러·사실 추출 | **PASS** (대역) | `Session.profile_patient` → `patient_profiler`(LLM) → `profile_facts.ingest_profile` → FactStore. 실제 모델 추출 품질은 미확인 |
| 출처·직접성·시점·검체·소유자·유효성 | **PARTIAL** (M) | 출처(`input_source`)·직접성(`evidence_directness`)·검체(`specimen`)·문서 증명(`document_authority`) 연결됨. 시점: 기간 조건 검사 기준을 규칙 엔진이 계산하지 않음(`test_date_window_lab_is_evaluated_deterministically` xfail). 주체(본인·가족 기록 구분)는 E2E 미확인 |
| Fact Store 저장·재호출 | **PARTIAL** | 사실은 이제 판정 게이트에 쓰임(`evidence_basis.document_check`가 FactStore를 읽음). 파일 저장은 `--fact-store-dir`만. 저장한 사실을 새 Session이 불러오는 경로 없음(`test_J_new_session_can_resume_saved_facts` xfail, O 소관) |
| 입력 오류·개인정보·감사 경계 | **PASS** | 선택지 밖 답 400(`answer_problem`), 웹 SSE 프로필 제거, Inspector strict 정제(원문·답변 미포함 `test_J_saved_runs_reload_and_inspector_diff`) |

### ③ 추출·매칭

| 항목 | 판정 | 근거 |
|---|---|---|
| Parser → Matcher → Rule Engine/LLM | **PASS** | I5·I6·I7 `method=rule_engine`, 나머지 LLM. `test_A_complete_record_...` |
| 기준별 MET / NOT_MET / UNKNOWN | **PASS** | `passes` True/False/None, `holds` 보존 |
| 독립 증거·검증 상태 | **BLOCKED** | 운영 문서 권한 `NoDocumentAuthority` → 진료 기록 필요 기준은 확정 안 됨(`declared_record_unverified`). 합성 권한을 주면 I3·I5가 확정됨(`test_A_document_attested_record_values_confirm`). 서버 문서 계약 필요 |
| 선정·제외 결합 판정 | **PASS** | 나이 미달·제외 충족 → INELIGIBLE이 질문 라운드 뒤에도 유지. 기록 선언 ECOG 3 → INELIGIBLE, 출처 unknown이면 보류 (`test_H_record_value_clear_fail`) |
| 전문가 판단 무확정 | **PASS** | E3 → NEEDS_REVIEW, 통과 없음 |
| Session·웹·시뮬레이터 일관성 | **PARTIAL** (P) | 웹은 같은 Session·Orchestrator(확인). 웹·CLI 기본은 `Session`, `ContractSession`은 `CTREC_SESSION=contract`일 때만. 시뮬레이터는 별도 `OfflineEvaluator`(규칙 엔진 범위) |
| 상충 근거 → 확정 중지 | **FAIL** (M) | 원문에 "병력 없음"과 "병력 있음"이 함께 있어도 모델이 한쪽만 인용하면 ELIGIBLE. `test_E_conflicting_narrative_stops_confirmation` xfail |

### ④ 정보 부족·충돌 탐지

| 항목 | 판정 | 근거 |
|---|---|---|
| 부족한 정보와 필요한 근거 식별 | **PASS** | `acquisition.update` → `missing_facts`(open / document_requested / resolved / unresolved), `compact()`의 unknown / physician_review / unverified_document_needed |
| 상충 값 | **FAIL** (M) | 서술 상충 미탐지(위). 같은 날짜 다른 검사값은 규칙 엔진이 판정을 넘기고(`_latest` → None) LLM 경로로 감 |
| 무응답 | **PASS** | 사실을 만들지 않고 판정 그대로, unresolved. `test_CD_unknown_or_no_response_preserves_state[no_response]` |
| 과거 결과(기간 조건) | **FAIL** (M) | 기간 조건 검사 기준을 규칙 엔진이 계산하지 않고 LLM에 넘김 |
| 문서 불충분 | **PASS** | 기록 필요 기준 → `document_requested` |
| 질문 후보·대체 문서·임상검토 행동 생성 | **PARTIAL** (P) | 기본(legacy): LLM 질문 + 코드 충실도 검사(`question_generator.screen`). 문서·임상검토는 보고서 문구뿐. 엔진 모드(`CTREC_POLICY_MODE=engine`): ASK_PATIENT·REQUEST_DOCUMENT 행동 생성 확인 |
| 결정 영향·증거 상태에 따른 DCV 행동 선택 | **PARTIAL** (P) | 엔진 모드에서만. 기본 경로의 질문 순서는 LLM. `question_priority`(정보 이득 순위)는 `question_eval`(오프라인 평가)에서만 호출 |

### ⑤ 요청·응답·재평가

| 항목 | 판정 | 근거 |
|---|---|---|
| 연구 담당자의 추가 정보 요청 | **FAIL** (O, 진행 중) | 기본 웹에는 담당자 요청 API가 없고, 질문은 Orchestrator가 바로 환자 화면에 보냄. O가 `web/followup_api.py`·`followup/service.py`를 만드는 중(커밋 전) |
| 환자 직접 응답 | **PASS** | `POST /api/runs/{id}/answers` → WebAnswerer → 재추출·재매칭 |
| 모르겠다·무응답 | **PASS** (상태 보존) | 위 C/D |
| 불명확·상충 응답 | **PARTIAL** (단위) | `ContractSession.merge` 상충 → UNVERIFIED(단위 테스트). 이번 E2E 미확인 |
| 이미 실패한 질문 반복 방지 | **FAIL** (O) / 엔진 모드 PASS | 기본 Session: "모름" 뒤 다음 라운드에 같은 E2 질문이 다시 나감. ContractSession은 답을 받은 질문도 다시 물음. `test_CD_failed_question_is_not_repeated` xfail. 엔진 모드는 2라운드에 E2를 문서 요청으로 바꿈 |
| 대체 출처·문서 경로 | **PARTIAL** (O/P) | 기본: comorbidity 확보 계획의 대안이 빈 목록 → "모름" 뒤 경로 없음(`test_C_unknown_offers_alternative_source` xfail). 엔진 모드: 대체 정보원을 고르지만 간질성 폐질환 병력에 "복용 약 목록"을 고름(정보원 대응 오류, P) |
| 사실 저장 후 영향 기준 재평가 | **PASS** | 답 → 재추출 → 대상 시험 재매칭, E2 resolved, `match_history` 라운드 1. `test_B_...` (영향 범위 한정 재평가 `incremental`은 진단 전용, 기본 꺼짐) |
| 재평가 후 상태·추천·Inspector 갱신 | **PASS** | 웹 SSE result에 재평가 판정·display. Inspector 라운드별 변화 |
| 다음 실행·새 세션으로 상태 이전 | **FAIL** (O, 진행 중) | 저장만 되고 불러오지 않음. CLI는 기존 사실 파일을 덮어쓰기 위험으로 거부. O의 `followup/codec.py`가 Session 저장·복원을 만드는 중 |

### ⑥ 추천·우선순위

| 항목 | 판정 | 근거 |
|---|---|---|
| 확인된 적격만 확정 적격 | **PASS** | CRITERIA_MATCH는 모든 기준이 확정일 때만(`test_A_structured_only_...`). 추천 목록의 보류 시험은 match_status로 구분 |
| 미평가·보류·부적격 구분 | **PASS** | NOT_EVALUATED / NEEDS_REVIEW / INSUFFICIENT_INFORMATION / CRITERIA_MISMATCH |
| 후보별 근거·추천 이유 | **PASS** (대역) | recommender 근거 + explainer 보고서. 실제 문장 품질은 미확인 |
| 환자별 추천 / Cohort C 우선순위 | **PARTIAL** (P) | 환자별 PASS. 여러 환자×여러 시험은 시뮬레이터(`python -m ctrec.sim`, 합성 6×3)와 `/cohort` 화면(생성 코호트 읽기)만. 실제 Session 여러 개를 묶는 경로는 없음 |
| 최신 결과 저장·웹·SSE·CLI·Inspector | **PARTIAL** | SSE·CLI PASS. CLI `--inspector` 추가(PASS). 웹 저장은 기본 꺼짐, 웹 화면에서 Inspector 연결 없음(`web/app.py`는 O가 수정 중이라 손대지 않음) |

## 2. 실제 실행 흐름 A-J

| 흐름 | 판정 | 실행 결과 |
|---|---|---|
| A 완전한 정보 → 매칭 → 추천 | **BLOCKED** (운영) / PASS (연결) | 운영: UNCERTAIN(기록 필요 기준 미검증). 합성 문서 권한: I3·I5 확정. 기준이 구조화 기준뿐이면 ELIGIBLE → 추천 → 보고서 |
| B 부족 → 요청 → 응답 → 재평가 | **PASS** (Session·웹) / **FAIL** (담당자 요청 단계) | E2 질문 → "둘 다 해당 없음" → E2 통과·resolved. 연구 담당자 요청 단계는 없음(O) |
| C 모르겠다 → 대체 문서 | **FAIL** (기본) / **PARTIAL** (엔진) | 기본: unresolved, 대안 없음, 같은 질문 반복. 엔진: 문서 요청으로 바뀌나 정보원 대응이 틀림 |
| D 무응답 → 반복 방지·상태 보존 | **PARTIAL** | 상태 보존 PASS, 반복 방지 FAIL(기본) |
| E 상충 → 확정 중지 → 해결 뒤 재평가 | **FAIL** (M) | 상충을 잡지 않음. 해결한 기록으로는 ELIGIBLE(재평가 경로 자체는 동작) |
| F 임상 판단 → EXPERT_REVIEW | **PASS** | NEEDS_REVIEW |
| G 미연결 필수 기준 → 확정 적격 금지 | **PASS** | NEEDS_REVIEW, CRITERIA_MATCH 없음 |
| H 확정 부적격 근거 → 부적격 유지 | **PASS** | 나이·제외 충족 INELIGIBLE이 2라운드 내내 유지. 기록 선언 ECOG 3 INELIGIBLE |
| I 여러 환자×여러 시험 → Cohort C | **PARTIAL** (P) | `python -m ctrec.sim --budgets 45 --seeds 1 --bootstrap 0`: 6개 정책 audit complete, 확정 적격 0(문서 권한 없음 가정), 합성 코호트 6×3만 |
| J 이전 결과 재호출 → Inspector 차이 | **PASS** (수정 후) / 새 세션 이전 FAIL | `inspector.compare_runs`: 저장한 두 실행을 다시 읽어 E2 `passes None → True`, 다른 환자는 비교 거부 |

## 3. 에이전트·도구 실사용

| 에이전트 | 종류 | 실제 호출 | 계약 | 오류·시간 초과 |
|---|---|---|---|---|
| 오케스트레이터 | LLM 도구 루프 (`llm.create`, 도구 7개) | 웹 항상, CLI agent 모드 | 도구 스키마 strict | 도구 예외 → `is_error` 결과(종류만 trace), 루프가 못 끝내면 고정 순서로 마무리. 취소·시간 초과 `llm.guard` → `RunCancelled` |
| 후보 추림 | LLM + 코드 hard filter | 풀 > 5일 때만 | `ScreeningOutput` | 실패 시 오케스트레이터 오류 결과 |
| 기준 파싱·파서 검증 | LLM | **런타임 호출 없음** (사전 컴파일만, `LAZY_COMPILE=1`일 때만 즉석) | `ParsedTrial`, `ValidationOutput` | 파싱본 없음 → NOT_EVALUATED |
| 환자 이해 | LLM | 처음 + 답변 라운드마다 (ContractSession은 처음만) | `PatientProfile` | 실패 → 오케스트레이터 오류 → 마무리 단계에서 재시도 |
| 매칭 | 규칙 엔진(코드) + LLM | 시험마다 | `MatcherOutput` 화이트리스트, 누락 기준 재요청 | 시험 단위 실패 → NOT_EVALUATED |
| 검증 | **코드** (LLM 아님): `evidence_directness`, `answer_verification`, `evidence_basis`, `qa_guard`, `expert_boundary`, `value_grounding` | 매칭마다 | - | - |
| 질문 | LLM(기본) 또는 정책 엔진 + `question_bridge`(엔진 모드, LLM 없음) | 라운드마다 | `QuestionSet` + 코드 충실도 검사 | 질문 없으면 라운드 종료 |
| 추천 | LLM 근거 문장 + 코드 그룹·순서 | finalize | `RecommendationDraft` (LLM 순서는 무시) | - |
| 설명 | LLM 보고서 | finalize (`WRITE_REPORT`) | 자유 문서 | - |

| 도구(요구 이름) | 실제 대응 | 실행 경로 호출 | 판정 |
|---|---|---|---|
| rule_engine | `tools/rule_engine.evaluate` | `matcher.match_trial` | PASS |
| term_lookup | 이름 붙은 도구 없음. `rule_engine.canonical_lab`(검사명 동의어), `evidence_directness.CONCEPTS`(E9 개념군) | 매칭 중 | PARTIAL: 일반 용어 사전 없음. 새로 만들 근거(판정 오류 사례)는 이번에 찾지 못해 형식상 추가하지 않음 |
| date_calc | 판정 기준일 고정(`Session.reference_date`), 나이 계산(`_evaluate_age`) | 매칭 중 | PARTIAL: 검사 기간 조건 계산 없음(M 소관 xfail) |
| unit_convert | `rule_engine._same_scale` | 규칙 엔진 검사 판정 | PASS |
| evidence_check | `evidence_directness`, `evidence_basis.document_check`, `answer_verification` | 매칭마다 | PASS |
| info_gain | `question_priority`, `decision_policy` | 오프라인 평가(`question_eval`)만. 엔진 모드 정책은 자체 순서 | FAIL(기본 경로) - P 공통 엔진이 대체할 자리라 N은 연결하지 않음 |
| rank | `recommender` 표시 순서(코드), `engine.baselines` | finalize / 시뮬레이터 | PASS |
| ask_user | `Session.clarify` → `Answerer`(WebAnswerer·Interactive·Simulated) | 오케스트레이터 `ask_clarifying_questions` | PASS |

## 4. N 커밋 (즉시 수정)

| SHA | 내용 |
|---|---|
| `befb436` | `config.TRIAL_SETS`/`CTREC_TRIAL_SET`, `cli --trial-set`·`--inspector`, `inspector.compare_views`/`compare_runs`, `render_inspector.py diff` |
| `2c9c4cd` | `tests/six_stage_harness.py`, `tests/test_six_stage_e2e.py` (흐름 A-J, 웹, CLI) |

수정 중 직접 낸 회귀 하나를 Inspector 기존 테스트로 잡았음: 새 상수가 Inspector의 `_CRITERION_KEYS` 허용 목록을 가려 기준 행이 비었음 → `_COMPARE_CRITERION_KEYS`로 분리(커밋 전 수정).

## 5. 다른 작업 소관 결함 (재현 테스트 + 최소 수정 제안)

모두 `tests/test_six_stage_e2e.py`의 xfail(strict). 고쳐지면 XPASS로 실패하므로 그 작업에서 표시를 지우면 됩니다.

### O-1 실패한 질문 반복 (⑤, 흐름 C·D)
- 재현: `test_CD_failed_question_is_not_repeated[session_unknown]`, `[contract_answered]`
- 패치: `docs/proposals/n_o1_no_repeat_failed_question.patch` (보호 파일 아님, 확인됨)
- 입력: NCT07680790, 처음 입력에 간질성 폐질환 정보 없음. 1라운드 E2 질문 → "모름"(또는 ContractSession에서 선택지 답). 아직 열린 E1이 있어 2라운드가 열림
- 현재: 2라운드에 같은 `NCT07680790:E2` 질문이 다시 나감 (`missing_facts[E2].status == "unresolved"`인데도)
- 원인: `pipeline.Session.clarify`가 `question_generator.generate_questions` 결과를 `acquisition` 상태로 거르지 않음. `question_generator.screen`은 "지금 판정 불가" 여부만 봄
- 제안(보호 파일 `pipeline.py` 대신): `question_generator.screen(questions, matches, open_keys=None)`에 열린 항목 집합을 받아 `target`에서 `open_keys` 밖을 빼고, `Session.clarify`가 `{k for k in missing_facts if acquisition.is_open(...)}`를 넘김. O의 `followup/service.py`가 같은 규칙("'모름'으로 끝난 항목의 환자 질문은 다시 만들지 않음")을 이미 갖고 있으므로 기본 Session 경로에도 같은 함수를 쓰면 됨
- 기대: 2라운드 질문의 target에 E2 없음

### O-2 "모름" 뒤 대체 경로 없음 (⑤, 흐름 C)
- 재현: `test_C_unknown_offers_alternative_source`
- 현재: `acquisition` comorbidity 계획 `alternatives=[]` → unresolved 후 아무 행동 없음
- 제안: 기본 경로도 O의 대체 경로(문서 검토 → 임상 검토)를 쓰거나, 최소한 unresolved 항목을 `document_requested`/임상 검토 대기로 표시
- 기대: `alternative_acquisitions` 비어 있지 않음 또는 status `document_requested`

### O-3 새 세션으로 상태 이전 없음 (⑤, 흐름 J)
- 재현: `test_J_new_session_can_resume_saved_facts`
- 현재: `Session(fact_store_dir=...)`는 저장만 함. `FactStore.load`를 부르는 Session 경로 없음
- 제안: O의 `followup/codec.py` 복원 경로가 들어오면 이 테스트를 그 API로 바꿈 (Session 기본 동작에 자동 로드를 넣는 것은 CLI의 기존 사실 파일 거부 규칙과 충돌하므로 권하지 않음)

### P-1 엔진 모드 대체 정보원 대응 오류 (④⑤)
- 입력: 위 C 흐름 + `CTREC_POLICY_MODE=engine`, 모든 답 "모름"
- 현재: 2라운드 E2(간질성 폐질환 병력) 대체 행동이 `REQUEST_DOCUMENT · medication_list(복용 약 목록)`
- 기대: 진료 기록(`medical_record`) 또는 임상 검토. 파일: `engine/policy.py` `item_alternatives` / `engine/bundles.py` SourceCatalog의 comorbidity 대응
- 참고: 기본 경로에서 `question_priority`(정보 이득)가 쓰이지 않음. P 공통 엔진(`engine/loop.py`, 진행 중)이 기본 경로가 되면 해소

### M-1 원문 서술 상충 미탐지 (③④, 흐름 E)
- 재현: `test_E_conflicting_narrative_stops_confirmation`
- 입력: 원문 "스테로이드 치료가 필요했던 간질성 폐렴 병력은 없습니다. 이전 병원 기록: 2025년에 스테로이드 치료가 필요했던 간질성 폐렴 병력이 있습니다." 모델이 앞 문장만 인용해 E2 no
- 현재: ELIGIBLE (CRITERIA_MATCH)
- 기대: 확정 안 함 (E2 `evidence_directness="conflicting"`, passes None)
- 파일: `evidence_directness` (conflicting 판정이 같은 대상의 긍정·부정 서술을 원문 전체에서 찾지 않음), `agents/matcher._enforce_directness`

### M-2 검사 기간 조건 날짜 계산 없음 (②④, date_calc)
- 재현: `test_date_window_lab_is_evaluated_deterministically`
- 입력: DEMO-LUNG-001 I9 "within 28 days ... hemoglobin >= 9.0 g/dL", Hb 12.1 g/dL 2026-10-20, 기준일 2026-11-07
- 현재: `tools/rule_engine.evaluate` lab 분기에서 `time_window_days`가 있으면 None → LLM 위임
- 제안: `evaluate(rule, patient, reference_date=None)`로 받아 검사일이 해석되고 기간 안이면 판정, 기간 밖이면 unknown(재검 필요), 날짜 없으면 지금처럼 None. 호출자 `matcher.match_trial`(보호 파일)이 `ref`를 넘김
- 기대: 기간 안의 값은 결정적으로 판정

### 팀·외부 (BLOCKED)
- 서버 문서 계약(`document_authority` 운영 어댑터): 없으면 진료 기록 필요 기준은 운영에서 확정되지 않음 (A, ③ 독립 증거)
- NSCLC 파싱본 임상 검수(서연·동겸) 및 NCT04892953·NCT05255302 컴파일
- 임상의 판단 입력 계약 (EXPERT_REVIEW 해소 수단)

## 6. 웹·시뮬레이터·Inspector 연결 상태

| 경로 | 상태 |
|---|---|
| 웹 → Orchestrator → Session | 연결됨. 기본 `Session`, `CTREC_SESSION=contract`이면 ContractSession, `CTREC_POLICY_MODE=engine`이면 질문을 엔진이 고름 |
| 웹 → NSCLC 시험 | `CTREC_TRIAL_SET=nsclc`로 연결 (이번 수정) |
| 웹 → Inspector | 연결 없음. 웹 결과 저장은 기본 꺼짐, `/api/replays`는 보안 문제로 비활성. `web/app.py`는 O가 수정 중이라 N은 손대지 않음 |
| CLI → Inspector | `--inspector`로 연결 (이번 수정), 실행 간 비교 `render_inspector.py diff` |
| 시뮬레이터 | `python -m ctrec.sim` 단독 (합성 코호트, `OfflineEvaluator`). 실제 Session과 같은 정책 객체(`LegacyAdapterPolicy`)를 쓰나 평가기는 다름. P의 `engine/loop.py`가 두 경로의 공통 단계를 만드는 중 |

## 7. 전체 pytest

- 기준(병합 tree 29a7ae3b, 수정 전): 5330 passed, 9 skipped, 1 xfailed (357s)
- N tip 2c9c4cd (tree 기준, 문서 커밋은 테스트 변화 없음): **5353 passed, 9 skipped, 7 xfailed** (368s, exit 0). 늘어난 23 passed와 xfail 6개가 새 E2E
- 새 E2E 단독: 23 passed, 6 xfailed (로그 docs/dev/n_six_stage_e2e_log.txt)
- O-1 제안 패치 확인: 임시 worktree에서 적용 시 반복 xfail 2개 XPASS, 질문 관련 테스트(test_p14_q1_question_screen·test_question_fidelity·test_choice_questions) 92개 통과

## 8. 다음 통합 최소 변경 목록

1. N 브랜치 `befb436`+`2c9c4cd`: `config.py`·`cli.py`·`inspector.py`·`scripts/render_inspector.py`·새 테스트 2개. O·P의 진행 중 파일(`web/app.py`, `web/engine_runner.py`, `sim/runner.py`, `followup/`, `engine/loop.py`)과 겹치지 않음
2. O 병합 시: O-1·O-3 xfail을 O API 기준으로 바꾸고 표시 제거. 기본 웹 경로(`/api/runs`)에도 반복 방지 적용 여부 결정
3. P 병합 시: P-1 정보원 대응 확인, 엔진 모드를 기본으로 할지 결정 (기본이 되면 ④ DCV 행동 선택·info_gain 항목이 PASS 후보)
4. M: M-1·M-2 (`evidence_directness`, `tools/rule_engine.evaluate` + `matcher.match_trial` 한 줄)
5. 배포 설정: NSCLC를 쓰는 데모·평가 환경에 `CTREC_TRIAL_SET=nsclc` (기본값은 바꾸지 않음)
