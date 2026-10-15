# 실제 LLM 파일럿 준비와 웹서비스 품질 검증 (Phase 7)

Phase 5 파일럿 계획(`llm_pilot_final_plan.md`)을 그대로 재사용합니다. 이 문서는 Phase 7에서 다시 확인한 것, 보강한 것, 실행 순서를 정리합니다.

| 항목 | 상태 |
|---|---|
| 실제 유료 호출 | **0회** (승인 전) |
| `PAID_EXECUTION_ENABLED` | `False` 유지 |
| 승인 게이트·계획 해시·사례 풀 | Phase 5 그대로. 완화·확장 없음 (`PILOT_CASE_POOL` 6개 유지) |
| 실험 A 첫 배치 | Phase 5 제안 그대로: 3개 사례, $3, 72회, 1,800초 |
| 실험 B | A 결과를 검토하고 별도 승인한 뒤에만 |
| 성능 수치 | **없음.** 실제 모델 응답을 받지 않았습니다. 아래 숫자는 비용·호출 추정과 Fake SDK 실행 경로 확인뿐입니다 |

## 1. 모델·SDK 호환성

공식 문서 확인일은 2026-10-15이고, 실제 API 호출은 하지 않았습니다. "문서 확인"과 "실제 호출 확인"을 나눠 적습니다.

| 항목 | 코드 | 공식 문서 | 실제 호출 확인 |
|---|---|---|---|
| 모델 ID | `config.MODEL = "claude-sonnet-5"` (`CTREC_MODEL`), 파일럿은 `PILOT_MODEL` 고정 | `claude-sonnet-5`, Active(legacy), 은퇴는 2027-06-30 이후. 현재 Sonnet은 Sonnet 5.5 | 미확인 |
| 요청 형식 | `messages.create(model, max_tokens=16000, system=[{text, cache_control: ephemeral}], messages, output_config={effort, format?}, tools?)` | `output_config.effort`는 Sonnet 5 지원(기본 `high`, `low`~`max`). `output_config.format`(json_schema) 구조화 출력은 Sonnet 5 지원 목록에 있음 | 미확인 |
| 금지 파라미터 | `temperature`·`top_p`·`top_k`·`thinking` 안 보냄 (테스트로 확인) | Sonnet 5는 `temperature`·`top_p`·`top_k`를 기본값이 아닌 값으로 보내면 400 | — |
| thinking | 파라미터를 보내지 않음 → 적응형(adaptive) 기본 | Sonnet 5 사고 방식은 적응형이고 effort로 조절. 사고 토큰은 출력 토큰·`max_tokens`에 포함 | 미확인 |
| 구조화 출력 제한 | `PatientProfile` 합집합 타입 14개(`$ref` 펼침), 선택 필드 0, 미지원 키워드 0 | 요청당 합집합 ≤16, 선택 ≤24, `minimum`·`maxLength` 등 미지원 → 400 | 미확인. **한도에 가까움**: 선택 필드 2~3개를 더하면 400 가능 (회귀 테스트로 감시) |
| 거절·잘림 | `stop_reason=refusal` → `RefusalError`, `max_tokens` → `TruncatedError` | 둘 다 스키마에 맞지 않는 출력이 될 수 있음. 거절도 과금됨 | 미확인 |
| enum 대소문자 | Pydantic이 엄격히 검증 | enum·const 값의 대소문자가 유지된다고 보장하지 않음 | 미확인. 실패하면 검증 오류로 사례 실패 (조용히 통과하지 않음) |
| 프롬프트 캐시 | 시스템 프롬프트에 `ephemeral`(5분) | Sonnet 5 최소 캐시 길이 1,024토큰. 짧으면 오류 없이 캐시 안 됨. `usage.cache_creation`에 5분·1시간 구분 | 미확인 (적중률 UNKNOWN) |
| service tier | 요청에 안 넣음 | `usage.service_tier` (standard/priority/batch) | 미확인. **Phase 7부터 `standard`가 아니거나 없으면 중단** |
| 사용량 구조 | `input_tokens`, `output_tokens`, `output_tokens_details.thinking_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `cache_creation`, `service_tier`, `inference_geo` | SDK 1.13.0 `Usage` 필드와 일치 | — |
| SDK 재시도 | 파일럿·웹 enforce: SDK 0회 + 래퍼 재시도. 웹 record: 기본 클라이언트 4회 | 공식 SDK 기본 2회 | — |
| 대체 모델 | `CTREC_FALLBACKS=0` (Phase 6-4부터 기본 배포도 0) | — | — |
| 단가 | `DOCUMENTED_PRICES`: $2 / $10 / 5분 캐시 쓰기 $2.50 / 읽기 $0.20 (`confirmed=False`) | 같은 값. 1시간 캐시 쓰기 $4, `inference_geo: "us"` 1.1배, 1M 문맥 같은 단가 | 미확인 (청구서로 확인 필요) |
| SDK 버전 | `anthropic>=1.13,<2` (Phase 6-4) | — | 1.13.0에서 훅·필드 확인 (가짜 HTTP) |

## 2. Phase 5 파일럿 재사용과 Phase 7 보강

**그대로 재사용한 것**
- 승인 게이트 `approval_problems`·`authorize` (승인 기록, 계획 해시, 확인 문구, 단가 확인, 상한 완전 일치, 모델 고정, `CTREC_FALLBACKS=0`, `CTREC_ALLOW_PAID_EVAL=1`, 사례 풀)
- `PAID_EXECUTION_ENABLED = False`
- 계획 해시와 재승인 조건
- `PilotMeter`(예약·정산·상한·재시도·연속 실패·시간), `SharedBudget`, `MeteredClient`, `call_guard` 배타 보호
- 파일럿 전용 프로세스, Fake SDK(`llm_pilot_fake`), 비식별 검사, 결과 저장 기본 꺼짐

**Phase 7에서 찾은 결함과 수정 (모두 더 엄격하게 바꾼 것. 게이트·상한은 그대로)**

| # | 결함 | 수정 |
|---|---|---|
| 1 | 파일럿 입력 예약이 "글자 수 = 토큰 수"였습니다. 한글 1자는 3바이트라서 실제 토큰이 예약보다 클 수 있고, 시스템 프롬프트·메시지 외 요청 내용과 API가 붙이는 입력도 빠져 있었습니다. Phase 6-3에서 웹에서 고친 문제와 같습니다 | 웹과 같은 `request_tokens_bound`(요청 전체 UTF-8 바이트 + 4,096 여유분)를 씁니다. 실제 입력·출력이 예약보다 크면 실제값으로 정산하고 다음 호출부터 중단합니다(`estimate_exceeded`). 상한에 닿기 전이라도 멈춥니다 |
| 2 | `usage.service_tier`가 없을 때 standard로 보고 정산했습니다 | `standard`가 아니면 중단합니다(`service_tier_mismatch`) |
| 3 | 1시간 캐시 쓰기($4)와 지역 지정 추론(1.1배)을 확인하지 않았습니다 | `cache_ttl_mismatch`, `inference_geo_mismatch`로 중단하고, 그 응답은 쓰지 않습니다 |
| 4 | 요청 옵션을 확인하지 않았습니다. 예: `temperature`는 Sonnet 5에서 400, `service_tier`·`speed`는 단가가 바뀜 | 허용 목록 밖 옵션과 서버 도구는 보내지 않고 중단합니다 |
| 5 | 같은 검사가 웹(`web/budget.py`)과 파일럿에 따로 있었습니다 | `llm_pilot`의 `request_problem`·`response_problem`·`usage_problem`·`request_tokens_bound` 하나를 둘 다 씁니다 |

- 계획 해시는 바뀌지 않았습니다. 실험 A 첫 배치 계획 해시(단가 확인 전)는 `a8d2485f27cd…`로 Phase 5와 같습니다. 상한 값이 같기 때문입니다. `chars_per_token` 이름은 해시 호환을 위해 유지했고, 뜻은 "바이트당 토큰"으로 바뀌었습니다.
- Fake SDK 리허설 결과(유료 호출 없음, 실행 경로·상한 동작 확인용):
  - 실험 A: 3개 성공, 요청 16회. 수정 전과 같습니다.
  - 실험 B: 3개 성공(비교 유효 3), 요청 10회.

## 3. 파일럿 사례 구성 (모두 합성)

`src/ctrec/llm_pilot_cases.py`에 유형별 표와 불러오기 함수가 있습니다. 새 사례는 `scripts/build_phase7_pilot_cases.py`로 결정적으로 만들었습니다(`eval/pilot/`).

| 유형 | 사례 | 묶음 | 확인하려는 약점 |
|---|---|---|---|
| A 명확한 충족 | `F-SYN-DEMO-A` | Phase 7 후보 | 적합 사례를 근거 없이 보류·불충족으로 바꾸지 않는지 |
| B 명확한 불일치 | `F-SYN-DEMO-B` (치료하지 않은 뇌전이) | Phase 7 후보 | 제외 기준 위반을 적합으로 표시하지 않는지 |
| | `S-NCT06125405-E5-confirmed-NCT04063540` | Phase 5 풀 | 확정 불충족 시험에 묻지 않음, 승격 없음 |
| C 필수 정보 부족 | rescue, biomarker, shared (숨긴 기준) | **실험 A 첫 배치** | 질문으로 되찾기 |
| | `S-NCT05127980-E1-cohort` | Phase 5 풀 | 열린 코호트 경로만 묻는지 |
| D 질문 후 판정 변화 | `M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1` | **실험 A 첫 배치** | 숨긴 충족 근거를 되찾은 뒤 판정이 바뀌는지 |
| E 정보 충돌 | `P7-SYN-DEMO-A-conflict-ecog` (ECOG 1 vs 2), `P7-SYN-DEMO-A-conflict-plt` (같은 날 혈소판 185 vs 85) | **새 사례** (Phase 7 후보) | 한쪽으로 임의 해결해 적합으로 확정하지 않는지 (기대: UNCERTAIN) |
| F 바이오마커 | `S-NCT07369063-I9-biomarker` | **실험 A 첫 배치** | 바이오마커 사실과 표적 기준 연결 |
| G 수치·단위 | `V-…-lab_platelets-shared` | **실험 A 첫 배치** | 같은 검사 기준 공유, 병렬 Matcher |
| | `F-SYN-S005-02` (eGFR) | Phase 5 풀 | 신장 eGFR ≠ 유전자 EGFR |
| | `P7-SYN-DEMO-A-units-hb` (Hb 88 g/L) | **새 사례** (Phase 7 후보) | 88 g/L = 8.8 g/dL < 9.0. 규칙 엔진은 g/L↔g/dL를 환산하지 않고 LLM에 넘기므로 모델의 단위 처리를 봄 (기대: INELIGIBLE) |
| H 판정 보류 | `V-NCT07516951-I7-prov-NCT07087314` | Phase 7 후보 | 정보가 거의 없음: 잠정 불충족을 확정으로 보지 않음, 절차 기준은 연구기관 확인으로 남김 |

- 새 사례 3개는 기존 합성 환자 `F-SYN-DEMO-A`에서 **한 줄만** 더하거나 바꾼 대조 사례입니다. 원본(정답 ELIGIBLE)과 결과가 다르면 그 한 줄 때문입니다.
- 기대 판정은 평가 기준이며 의료적 결론이 아닙니다(`eval/pilot/phase7_labels.csv`).
- 기존 합성 집합(개발 101, 검증 100, 보충 27)에는 정보 충돌 사례가 없었습니다(자동 검색 0건). 그래서 E 유형은 새로 만들었습니다.
- Phase 7 후보 6개는 `PILOT_CASE_POOL` 밖입니다. **지금 게이트는 이 사례들을 거절합니다**(테스트로 확인). 실행하려면 사용자 승인 뒤 풀에 넣는 별도 코드 변경이 필요합니다.

## 4. 평가 기준

`llm_pilot_cases.CRITERIA`에 정리했습니다. 자동 지표는 결과 JSON(`llm_pilot_exec.execute`)에서 세는 값입니다.

| 영역 | 정량 지표 (통과 조건) | 사람이 직접 검토 |
|---|---|---|
| 환자 정보 추출 | `unlocated_not_unknown`=0 (입력에 없는 사실의 대리 지표), `answer_facts_not_self_report`=0, `promoted_to_document_verified`=0, 결정적 추출 대비 핵심 항목 일치율=1, `kidney_egfr_linked_to_EGFR`=0 | 원문에 없는 수치·날짜·약물이 생기지 않았나 / 수치·단위를 그대로 옮겼나 / 검사 결과와 환자 진술 구분 / 충돌 기록을 임의 해결하지 않았나 |
| 추가 질문 | (B) 중복 기준·정보 키=0, 불필요 대상=0, 매핑 안 된 질문 수·예산 안 적중·첫 적중 순위 보고. (A) 질문 수 ≤ 예산 | 이해할 수 있는 문장인가 / 필수 조건을 먼저 물었나 / 원문에 있는 정보를 다시 묻지 않았나 |
| Matcher·추천 | 판정 누락=0, 잘림·거절=0, 정답 판정 일치(A 적합 유지, B 불충족, E 확정 안 함, G 환산 후 불충족) | 명확한 불일치를 적합으로 / 정보 부족을 부적격으로 오판하지 않았나 / 근거 인용이 원문과 일치하나 / 추천 이유가 이해 가능하고 참여 확정처럼 쓰지 않았나 |
| 웹 경험 | Edge E2E(Fake): 질문 표시, 답변 반영, 진행 상태, 오류 종료, 비용 한도 차단 | 실제 응답 길이·지연에서의 진행 표시 (실제 웹 유료 호출은 별도 승인) |
| 비용·안전 | 보수적 비용 ≤ 상한, `stop_reason` 없음, 사례 비용 ≤ base 추정 2배, 사고 토큰이 16,000에 근접하지 않음, 고정 기준표 무결 | Console 사용량과 대조 |

- 결과 파일에는 원문·응답 문장이 없습니다(비식별).
- 사람 검토는 승인된 검토자가 Fake가 아닌 실제 실행의 `--save-results` 없이 재현한 세션에서 따로 봐야 합니다. 이 절차는 별도로 정해야 합니다(남은 결정).

## 5. 실험 계획

추정은 `measure_offline`(LLM 대역으로 실제 순서 측정) × 문서 단가입니다. low / base / worst 순입니다. worst는 모든 호출이 `max_tokens`를 다 쓰고 재시도 상한까지 가는 경우입니다. **단가는 사용자 확인 전입니다.**

### Experiment A (Phase 5 계획 그대로, 먼저 실행)

| 항목 | 값 |
|---|---|
| 목적 | 실제 프로파일러 추출·출처, Matcher 판정 안정성, 승격 오류, 바이오마커 연결, 호출·토큰·비용, 오류·중단 처리 |
| 대상 | rescue, biomarker, shared (유형 C·D·F·G) |
| 호출 수 | Fake 실행 경로 16회 / base 19.3 / worst 72 |
| 토큰 (base) | 입력 약 98k, 출력 약 60k |
| 비용 | $0.25 / **$0.80** / $13.54 |
| 최대 비용 (상한) | **$3** (호출 72, 입력 981k, 출력 241k). worst는 상한이 먼저 멈춤 |
| 시간 | 전체 1,800초, 요청당 180초 |
| 중단 조건 | 자동: 상한 6종, 응답 이상(사용량 누락·다른 모델·대체 모델·등급·1시간 캐시·지역 추론·**추정 초과**), 보호 장치, 시간·취소. 수동: `llm_pilot_final_plan.md` 5절 |
| 평가 지표 | 4절 표 |
| 기록 형식 | `docs/llm_pilot_results_template.md` + `--save-results`(비식별 JSON) |

### Experiment A2 (Phase 7 후속 배치 후보, A 결과 확인 뒤 별도 승인 + 사례 풀 변경 필요)

| 묶음 | 사례 | 호출 (Fake / base / worst) | 비용 (low / base / worst) | 제안 상한 |
|---|---|---|---|---|
| A2a 충돌·단위 | conflict-ecog, conflict-plt, units-hb (E·G) | 12 / 14.5 / 54 | $0.18 / $0.58 / $10.00 | $2, 54회 (Phase 5 A 상한 $3 이하) |
| A2b 명확·보류 | F-SYN-DEMO-A, F-SYN-DEMO-B, provisional (A·B·H) | 14 / 16.9 / 63 | $0.22 / $0.69 / $11.73 | $3, 63회 |

- 첫 배치 원칙에 따라 한 번에 3개 이하로 나눴습니다.
- A2a가 더 중요합니다. 실제 모델의 충돌·단위 처리는 다른 방법으로 볼 수 없기 때문입니다.

### Experiment B (A 결과 확인 전에는 진행하지 않음)

| 항목 | 값 |
|---|---|
| 목적 | 같은 상태·같은 판정에서 웹의 기존 LLM 질문 생성기와 Decision Loop 질문 계획 비교 (답변 반영 없음) |
| 대상 | A와 같은 3개 |
| 호출 수 | Fake 10 / base 12 / worst 42 |
| 토큰 (base) | 입력 약 63k, 출력 약 32k |
| 비용 | $0.15 / **$0.45** / $7.93 |
| 최대 비용 | **$2** (호출 42) |
| 시간 | 1,800초 / 요청당 180초 |
| 중단 조건 | A와 같음 + `state_diverged`(두 계획의 입력 상태가 다르면 비교 무효) |
| 평가 지표 | 질문 수, 매핑, 중복, 불필요 대상, 예산 안 적중·첫 적중 순위, 두 계획 겹침 |
| 기록 형식 | 결과 템플릿 B절 |

- **A의 질문은 Decision Loop 계획 + 결정적 정답 시뮬레이터입니다. 따라서 A는 웹 질문 생성기의 품질을 재지 않습니다.** 웹 질문 생성기를 실제 모델로 재는 것은 B뿐입니다.

### 실행 순서

1. 사용자: 단가 확인, Console 지출 한도, 실행 커밋 결정
2. 실행 전 점검
3. 승인
4. `PAID_EXECUTION_ENABLED`를 켜는 별도 코드 변경(승인 후)
5. A 실행 → 결과 검토(수동 중단 기준)
6. A2a·A2b 여부 결정 (풀 변경 승인)
7. B 승인 → B 실행

## 6. 실행 전 점검 (`scripts/pilot_preflight.py`)

```
PYTHONPATH=src CTREC_FALLBACKS=0 python scripts/pilot_preflight.py --experiment A_pipeline
```

기존 장치를 불러 점검만 합니다(새 안전장치 없음). API 키 값은 출력하지 않습니다.

| 점검 | 방법 | 현재 결과 (2026-10-15, 이 컴퓨터) |
|---|---|---|
| API 키 | 있는지만 | PENDING_RUNTIME (없음) |
| 모델 ID | `config.MODEL == PILOT_MODEL` | PASS |
| 모델 사용 가능 | 문서만 | PENDING_RUNTIME |
| 대체 모델 | `CTREC_FALLBACKS=0` | PASS |
| SDK | `anthropic>=1.13,<2` | PASS (1.13.0) |
| 구조화 출력 스키마 | 공식 제한 대조 | PASS (최대 14/16) |
| 사례 | 풀 안, `synthetic=true` | PASS |
| 상한 | Phase 5 제안 이하 | PASS ($3·72회·1,800초) |
| 계획 해시 | `build_plan` | PASS (`a8d2485f27cd…`, 단가 확인 전) |
| 단가 | 문서 값, `confirmed=False` | PENDING_USER |
| 승인 게이트 / 실행 스위치 | `approval_problems`, `PAID_EXECUTION_ENABLED` | LOCKED (정상) |
| 승인 없이 실행 시도 | `run_pilot(plan, None)` | PASS: 거절, 실제 클라이언트 생성 0 |
| 네트워크 차단 | 파일럿 배타 보호 중 실제 SDK 객체(가짜 HTTP)로 직접 요청 | PASS: 차단 1, 전송 0 |
| Frozen Store | `verify_frozen` | PASS |
| Fake SDK 실행 | `llm_pilot_exec.execute` | PASS (3개 성공, 16회) |
| 출력 비식별 | `assert_redacted` | PASS |
| 실패 시 중단 | 첫 응답 사용량 누락 | PASS (`usage_missing`, 이후 요청 0) |
| Console 지출 한도 | 코드 밖 | PENDING_USER |
| 실행 커밋 | `git rev-parse` | 커밋 뒤 기록 |

`ready_to_execute`는 PASS만 있을 때 True입니다. 지금은 LOCKED·PENDING 때문에 False입니다.

## 7. 웹서비스와 파일럿 승인 구조의 관계

**Phase 5 파일럿 승인 게이트는 웹 실행에 적용되지 않습니다.** 웹은 별도 구조를 씁니다(테스트 `test_web_runs_do_not_use_the_pilot_approval_gate`).

| 항목 | 파일럿 (`run_llm_pilot.py`) | 웹 (`ctrec.web.app`) |
|---|---|---|
| 실행 허가 | 승인 기록 + 계획 해시 + 확인 문구 + 단가 확인 + `CTREC_ALLOW_PAID_EVAL=1` + `PAID_EXECUTION_ENABLED` (지금 False) | **없음.** `ANTHROPIC_API_KEY`가 있으면 누구든 분석을 시작하면 호출됨. 접근은 `CTREC_PASSWORD`(선택)로만 제한 |
| 비용 통제 | `PilotMeter` 실행 상한 + `SharedBudget`(메모리) | record(기본): **상한 없음**, 기록만. enforce: 실행별·공용 SQLite 원장 상한 |
| SDK 재시도 | 0 (래퍼 재시도, 호출마다 예약) | record: 기본 클라이언트 **SDK 자동 재시도 4회**(기록 호출 수에 안 보일 수 있음). enforce: 0 + 래퍼 |
| 요청·응답 검사 | 공용 검사 (Phase 7) | enforce: 같은 공용 검사. record: 검사 없이 기록 |
| 사례 | 합성 6개 풀만 | 사용자가 입력한 아무 텍스트 (개인정보 입력 금지 안내만) |
| 질문 생성 | A: Decision Loop 계획 + 정답 시뮬레이터. B: 기존 생성기와 비교 | 기존 LLM 질문 생성기 (Decision Loop는 진단 전용) |
| Matcher 병렬 | `copy_context` 스레드 풀, 배타 보호로 미계측 호출 차단 | 같음. enforce는 배타 보호, record는 관찰만 |
| 오류·취소 | `PilotStop`(RunCancelled), 사례 중단 → 나머지 건너뜀 | `RunCancelled`·`BudgetStop`, 종료 상태 구분 표시 |
| 사용량 누락 | 중단 | record: `USAGE_UNKNOWN` 표시하고 계속. enforce: UNKNOWN 점유 + 중단 |
| 서버 재시작 | 별도 프로세스, 결과 저장 기본 꺼짐 | 진행 중 실행은 사라짐. enforce 원장은 UNKNOWN으로 보존 |
| 환자 정보 저장 | 저장 안 함 + 출력 비식별 검사 | 기본 저장 안 함(`CTREC_WEB_SAVE_OUTPUTS`), 브라우저에도 원문 없음 |

**결론**
- 웹에 실제 API 키를 넣는 순간 Phase 5 승인 없이 유료 호출이 가능합니다.
- record 모드에서는 서버 쪽 비용 상한도 없습니다.
- 이번 단계에서는 웹에 키를 넣지 않았고, 웹 유료 호출을 켜지 않았습니다.
- 웹 유료 호출을 허용하기 전에 결정할 것: 웹 전용 실행 스위치(기본 꺼짐)를 둘지, 또는 enforce + 비밀번호 + Console 한도를 필수로 할지(M절).

## 8. 남은 차단 요인

1. 사용자 승인과 `PAID_EXECUTION_ENABLED`를 켜는 별도 코드 변경
2. 단가 사용자 확인(`--price-confirmed` + 출처), Console 워크스페이스·키 지출 한도
3. 실행 커밋 확정: 이 Phase 7 변경을 커밋한 해시를 승인 기록에 남김
4. 실제 실행 환경의 SDK 버전에서 `tests/test_call_guard.py`·`tests/test_pilot_phase7.py` 통과
5. 실제 응답에서만 확인 가능한 것:
   - `usage.service_tier`가 실제로 오는지 (없으면 첫 호출 뒤 중단: 안전하지만 실험 실패)
   - 입력 예약 추정이 실제보다 큰지
   - 구조화 출력 400·enum 대소문자
6. A2(Phase 7 후보) 실행에는 `PILOT_CASE_POOL` 확장 승인이 필요
7. 웹 유료 호출 허용 구조(7절)는 별도 결정
8. 사람 검토 절차: 원문·응답을 볼 수 있는 검토자와 방식

## 출처

- [Models overview](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Claude Sonnet 5](https://platform.claude.com/docs/en/models/sonnet-5/overview)
- [Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Pricing](https://platform.claude.com/docs/en/about-claude/pricing)
