# 실제 LLM 파일럿 최종 실행 계획 (Phase 5-4, 실행 전)

## Phase 5 종료 상태

| 항목 | 상태 |
|---|---|
| Phase 5 준비 작업 | **완료** (5-0 준비 → 5-1 실행 경로 → 5-2 실행 격리 → 5-3 미계측 호출 차단·웹 기록 → 5-4 최종 점검) |
| 실험 A/B 실제 실행 | **미실시** |
| 유료 API 호출 | **0회** |
| 실행 게이트 | **OFF** (`PAID_EXECUTION_ENABLED = False`, 별도 지시 전까지 켜지 않음) |
| 모델 | `claude-sonnet-5` 유지 (`PILOT_MODEL`로 고정) |
| 실험 A 첫 배치 후보 | rescue, biomarker, shared |
| 제안 상한 | 총비용 $3, 호출 72회, 전체 1,800초 (요청당 180초) |
| 실험 B | A 결과 검토 후 별도 승인 |
| 실제 실행 전 확인 | SDK 버전(훅 호환), 공식 단가, Console 워크스페이스 지출 한도 |
| 승인 기록 | 실제 실행 시 **코드 버전(커밋 해시)과 계획 해시**를 함께 남길 것 |
| Decision Loop | 현재 웹에서 **진단 전용** (판정·질문·추천에 쓰지 않음, 실제 질문은 기존 LLM 질문 생성기) |

**이 계획은 아직 실행할 수 없습니다.** 실행 가능 판정(`readiness.ready_to_execute`)이 `False`이고, 그 이유는 다음 세 가지입니다.
- 사용자 승인 없음
- 단가 사용자 확인 전
- `PAID_EXECUTION_ENABLED = False`

**사용자 결정 (Phase 5-4 마감):**
- 모델은 `claude-sonnet-5`를 유지합니다. 코드 상수 `llm_pilot.PILOT_MODEL`로 고정했고, 다른 모델의 계획·승인은 게이트가 거절합니다.
- 실험 A의 3개 사례 계획은 **승인 검토용 제안**이며, 실행 승인이 아닙니다.
- 실험 B는 A 결과를 확인한 뒤 별도로 승인합니다.
- eGFR은 후속 배치 후보입니다.

**최종 실행 시점에 다시 확인할 것 (실제 API 요청으로 확인하지 않음):**
- `claude-sonnet-5`가 아직 사용 가능한지: 레거시 상태이며, 은퇴는 2027-06-30 이후입니다.
- 공식 가격표 단가: 이 문서의 값은 2026-10-14 조회분입니다.

아래 명령은 작성만 했고 실행하지 않았습니다. 승인 확인 문구는 단가 확인 뒤 계획 해시가 정해질 때 CLI가 만듭니다. 이 문서에는 넣지 않습니다.

## 1. 모델과 단가 (공식 문서 조회: 2026-10-14)

| 항목 | 값 | 출처 |
|---|---|---|
| 설정 모델 | `claude-sonnet-5` (`config.MODEL` 기본값, `render.yaml`도 같음) | 코드 |
| 실제 SDK 요청 모델 | `llm.create`가 `config.MODEL`을 그대로 보냄. 파일럿은 계획 모델을 실행 컨텍스트에 고정하고, 응답 모델이 다르면 중단 | 코드·테스트 |
| 모델 ID | `claude-sonnet-5` (Claude API). 상태: 레거시(Active), 은퇴는 2027-06-30 이후 | models/sonnet-5/overview |
| 입력 | $2 / MTok | pricing, sonnet-5/overview |
| 출력 | $10 / MTok | 같음 |
| 캐시 쓰기 | 5분 $2.50 / MTok (코드가 쓰는 `ephemeral` 기본값), 1시간 $4 / MTok (안 씀) | 같음 |
| 캐시 읽기 | $0.20 / MTok | 같음 |
| 단가 각주 | "$2/$10 … is now the standard price" (9월 인상 취소) | pricing 각주 3 |
| 긴 문맥 | 1M 전체 표준 단가 | pricing, Long context |
| 사고(thinking) 토큰 | 출력 토큰으로 과금. `max_tokens`에 포함. `usage.output_tokens_details.thinking_tokens`로 출력 중 사고 토큰 수가 보고됨 | extended-thinking |
| Sonnet 5 사고 기본값 | 적응형(adaptive). 기본 effort는 `high`(코드는 단계별 effort를 명시함) | sonnet-5/overview |
| 최대 출력 | 128K (동기 API). 코드는 호출당 16,000 | sonnet-5/overview |
| 사용량 응답 구조 | `input_tokens`, `output_tokens`, `output_tokens_details`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `service_tier`, `inference_geo` | SDK 1.13 `Usage` 필드 확인 |
| 대체 모델 | 거절 분류기·대체 모델 문서의 대상 모델 목록에 Sonnet 5가 **없음**. 대체 모델이 실행되면 `usage.iterations`에 `fallback_message` 항목이 생기고, 최상위 `usage`는 마지막 시도만 반영 | refusals-and-fallback |
| SDK 기본 재시도 | 공식 SDK는 기본 2회 재시도(`max_retries`). 프로젝트 기본 클라이언트는 4회, 파일럿은 0회 | errors |

**UNKNOWN (공식 문서에서 확인하지 못함):**
- 429·5xx·시간 초과 요청의 과금 여부 → 응답 없는 실패는 예약한 최악값으로 과금 처리
- Sonnet 5의 안전 분류기 거절 여부와 과금 → 대체 모델을 끄고, 거절은 사례 실패로 처리
- 실제 프로필 크기, 출력·사고 토큰, 캐시 적중률 → 추정치 사용

코드의 `DOCUMENTED_PRICES["claude-sonnet-5"]`는 위 문서 값입니다. 다만 `confirmed=False`이므로 승인 게이트를 통과하지 못하고, 사용자가 `--price-confirmed`와 확인 출처를 줘야 통과합니다.

## 2. 첫 배치 사례 (3개) + eGFR 추가 후보

모든 사례는 합성입니다. 예상치는 문서 단가 기준이며, low / base / worst 순입니다. worst는 모든 호출이 `max_tokens`를 다 쓰고 재시도 상한까지 가는 경우입니다.

| 사례 | 검증 목적 | 실험 A: 호출(LLM 단계) / 토큰(입력·출력, base) / 비용 | 실험 B: 호출 / 토큰(base) / 비용 |
|---|---|---|---|
| **rescue** `M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1` | 가장 긴 서술형 원문 추출, 약물 이력 속 EGFR 유전자 언급의 출처 유지, 선정 기준 질문 | 4회 (프로파일러 2, Matcher 2) / 21.7k·14.7k / $0.06 · $0.19 · $3.34 | 3회 / 16.9k·9.1k / $0.04 · $0.12 · $2.24 |
| **biomarker** `S-NCT07369063-I9-biomarker` | 바이오마커 사실과 유전자 표적 기준의 연결, 질문 대상 매핑 | 5회 (+추천 1) / 28.0k·17.6k / $0.07 · $0.23 · $3.97 | 3회 / 16.7k·9.1k / $0.04 · $0.12 · $2.27 |
| **shared** `V-DEMO-LUNG-001-NCT06908096-lab_platelets-shared` | 두 시험이 같은 검사 기준을 공유: 병렬 Matcher 계측, 정보 키 묶음, 중복 질문 | 7회 (Matcher 4) / 57.4k·27.7k / $0.13 · $0.39 · $6.55 | 4회 / 35.9k·14.1k / $0.07 · $0.21 · $3.61 |
| (추가 후보) **eGFR** `F-SYN-S005-02` | 신장 eGFR과 유전자 EGFR 혼동 회귀 | 7회 / 49.5k·27.7k / $0.12 · $0.38 · $6.24 | 4회 / 28.2k·14.1k / $0.07 · $0.20 · $3.41 |

**3개 묶음 (rescue + biomarker + shared):**

| 실험 | 호출 (base / worst) | 입력·출력 토큰 (base) | 비용 (low / base / worst) | 제안 상한 |
|---|---|---|---|---|
| A | 19.3 / 72 | 98k · 60k | $0.25 / $0.80 / $13.54 | **$3**, 72회, 입력 981k, 출력 241k |
| B | 12 / 42 | 63k · 32k | $0.15 / $0.45 / $7.93 | **$2**, 42회, 입력 590k, 출력 130k |

- base 추정이 상한 안에 들어오므로 3개를 유지합니다. worst는 상한($3 / $2)이 그 전에 실행을 멈춥니다.
- eGFR을 넣으면 A base는 약 $1.18로 상한 안이지만, 첫 배치 "3개 이하" 원칙에 따라 다음 배치로 미룹니다.
- 호출 수는 Fake SDK로 실제 실행 경로를 돌려 확인했습니다. 3개 묶음에서 A는 16회, B는 10회입니다. base 추정치에는 재시도·누락 재요청 여유분이 들어 있습니다.

## 3. 실행 순서

1. **실험 A, 3개** (rescue, biomarker, shared): 실제 프로파일러·Matcher·재추출·추천 검증
2. 결과 검토 (아래 5절의 중단 기준). 하나라도 해당하면 **멈추고 다음 실험으로 가지 않음**
3. **실험 B, 같은 3개**: 질문 계획 비교. A 결과가 안정적일 때만, 별도 승인으로
4. 다음 배치(eGFR, cohort, confirmed)는 별도 판단으로

공통 조건:
- **전용 프로세스**: 웹 서버와 다른 프로세스에서 실행합니다. 프로세스 보호(fail-closed)가 켜져 있으면 같은 프로세스의 웹 호출이 막힙니다.
- 실행 시간 제한: 전체 1,800초, 요청당 180초
- 재시도: 호출당 2회, 전체 5회. 연속 실패 3회면 중단
- 결과 저장: 기본 끔. 저장할 때만 `--save-results`를 쓰고, 비식별 검사를 거쳐 원자적으로 저장합니다.

## 4. 실행 명령 (작성만 했고 실행하지 않음)

**1단계: 계획 해시와 확인 문구 생성** (사용자가 단가를 확인한 뒤. API 호출 없음)

```bash
PYTHONPATH=src CTREC_FALLBACKS=0 python scripts/run_llm_pilot.py --experiment A_pipeline \
  --cases M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1 S-NCT07369063-I9-biomarker \
          V-DEMO-LUNG-001-NCT06908096-lab_platelets-shared \
  --price-source "<공식 가격표 확인 기록: URL·날짜·확인자>" --price-confirmed \
  --max-cost-usd 3 --max-calls 72 --run-deadline-s 1800
```

출력의 `plan.plan_hash`, `plan.confirmation_phrase`, `readiness`를 확인합니다.

**2단계: 실행** (`PAID_EXECUTION_ENABLED`를 켜는 별도 코드 변경과 승인 뒤. 같은 인자에 승인 정보를 더함)

```bash
PYTHONPATH=src CTREC_ALLOW_PAID_EVAL=1 CTREC_FALLBACKS=0 python scripts/run_llm_pilot.py --experiment A_pipeline \
  --cases M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1 S-NCT07369063-I9-biomarker \
          V-DEMO-LUNG-001-NCT06908096-lab_platelets-shared \
  --price-source "<1단계와 같음>" --price-confirmed --max-cost-usd 3 --max-calls 72 --run-deadline-s 1800 \
  --approved-by "<승인 기록>" --confirm "<1단계 출력의 confirmation_phrase>" --execute
```

실험 B는 `--experiment B_question_plan --max-cost-usd 2 --max-calls 42`로 바꿔 같은 두 단계를 따로 거칩니다.

사전 리허설(유료 호출 없음): 같은 명령에 `--execute` 대신 `--fake-sdk`를 주면, 같은 프로세스 구성으로 Fake SDK 실행이 됩니다.

## 5. 중단 조건과 성공 기준

**자동 중단** (그 실행을 즉시 멈추고 남은 사례는 건너뜀):
- 상한: `cost_cap`, `call_cap`, `input_token_cap`, `output_token_cap`, `retry_cap`, `consecutive_failures`
- 응답 이상: `usage_missing`, `model_mismatch`, `fallback_detected`, `service_tier_mismatch`
- 보호 장치: `unmetered_call_blocked`, `sdk_hidden_retry`
- 시간·사용자: `timeout`, `cancelled`

**실행 후 수동 중단 기준** (하나라도 해당하면 다음 실험·배치로 가지 않음):
- 실패·중단된 사례가 하나라도 있음 (`status`가 failed/aborted/skipped)
- `TruncatedError`, `RefusalError`, Matcher `not_evaluated > 0`, `missing_after_retry > 0`
- 검증 상태 부당 승격 > 0
- 답변 사실 중 `self_report`가 아닌 것 > 0
- 원문에 없는 인용인데 출처를 추정한 것 > 0
- eGFR이 EGFR로 연결된 것 > 0
- 사례당 실제 비용이 base 추정의 2배 초과, 또는 사고 토큰이 출력의 대부분을 차지해 `max_tokens`(16,000)에 근접

**성공 기준**: `llm_pilot.EXPERIMENTS`의 `success_criteria`

| 실험 | 성공 기준 |
|---|---|
| A | 모든 사례 성공, 판정 누락 0, 승격 0, 출처 추정 0, eGFR/EGFR 연결 0, 상한 이내, Frozen Store 무결 |
| B | 모든 사례 `comparison_valid`(같은 상태·같은 입력). 질문 대상 매핑·중복·관련성·예산 안 적중을 보고 |

## 6. 실행 후 확인할 지표

- 사례별: 상태, 단계별 호출, 입력·출력·사고 토큰, 캐시 쓰기·읽기, 실제 비용(`cost_usd`)과 보수적 추정(`cost_estimated_usd`), 재시도, 지연, 실패 종류
- 실험 A: 사실 수, 출처·검증 분포, 결정적 추출 대비 핵심 항목 일치, Matcher 판정 분포, 숨긴 기준 규칙 상태, 정답 판정 일치 수, eGFR/EGFR
- 실험 B: 두 계획의 질문 수, 매핑, 중복, 불필요 대상, 예산 안 적중과 첫 적중 순위, 겹침
- 공통: `stop_reason`, `frozen_store_intact`, 출력 비식별 검사 통과. 단가 확인 상태에 따라 `cost_kind`가 실제인지 추정인지

## 7. 계획 해시와 재승인 조건

계획 해시(`llm_pilot.plan_hash`)는 아래 항목 전체의 SHA-256입니다. **어느 하나라도 바뀌면** 해시와 확인 문구(`RUN <실험> <해시 12자>`)가 바뀌므로, 이전 승인과 확인 문구는 재사용할 수 없습니다(게이트가 해시 불일치와 문구 불일치로 거절).

| 항목 | 해시 반영 위치 |
|---|---|
| 모델 | `model` (+ 고정 모델 `PILOT_MODEL` 검사) |
| 사례 목록 (순서 포함) | `case_ids` |
| 실험 종류 | `experiment`, `rounds`, `question_budget` |
| 가격표 | `price` (입력·출력·캐시 쓰기·캐시 읽기·확인 여부·출처) |
| 비용 상한 | `limits.max_cost_usd` |
| 호출 수 상한 | `limits.max_calls` |
| 토큰 상한 | `limits.max_input_tokens`, `limits.max_output_tokens`, `limits.max_tokens_per_call` |
| 시간 제한 | `limits.run_deadline_s`, `limits.call_timeout_s` |
| 재시도 정책 | `limits.max_retries_per_call`, `limits.max_total_retries`, `limits.max_consecutive_failures` |
| 데이터 처리 | `save_patient_data`, `redact_outputs` |

**Phase 5-4 마감 점검에서 보완한 점** (실행은 켜지 않은 상태에서 최소 수정, 회귀 테스트 `tests/test_pilot_final_checks.py`):
1. **승인 상한을 계획 상한과 완전히 같게 요구:** 실행은 승인 쪽 상한으로 돕니다. 그런데 이전 게이트는 6개 항목만 "계획 이하인지" 확인했습니다. 그래서 시간 제한, 연속 실패, 호출당 `max_tokens`가 계획보다 느슨한 승인도 통과할 수 있었습니다. 이제 상한의 모든 항목이 같아야 통과합니다.
2. **모델 고정:** 파일럿 모델이 `CTREC_MODEL` 환경 변수를 그대로 따랐기 때문에, 다른 모델로 설정된 환경에서도 계획과 승인이 가능했습니다. 이제 `PILOT_MODEL`이 아니면 게이트가 거절합니다.

**해시에 들어가지 않는 것** (실행 전 따로 확인):
- 에이전트 프롬프트와 코드 버전 → 실행할 커밋 해시를 승인 기록에 남깁니다.
- `PAID_EXECUTION_ENABLED` 상태
- SDK 버전

## 8. 실제 실행 전 체크리스트

상태 표시:
- `VERIFIED`: 이번 단계에서 코드·테스트로 확인함 (Fake SDK·가짜 HTTP 기준. 실제 API로 확인한 항목은 없음)
- `PENDING_USER_APPROVAL`: 사용자가 정하거나 승인해야 함
- `PENDING_RUNTIME_VERIFICATION`: 실제 실행 환경이나 실행 시점에서만 확인할 수 있음

| # | 항목 | 상태 | 근거 / 남은 일 |
|---|---|---|---|
| 1 | 실제 사용자 승인 (실험·사례·상한) | PENDING_USER_APPROVAL | 승인 기록(`--approved-by`) 필요 |
| 2 | `PAID_EXECUTION_ENABLED`를 켜는 별도 코드 변경 | PENDING_USER_APPROVAL | 지금은 `False` (테스트로 고정) |
| 3 | 모델 ID 고정 (코드·설정) | VERIFIED | `config.MODEL`, `render.yaml`, `.env.example`, `PILOT_MODEL`, 요청 모델, 응답 모델 검증, 단가표 모델 |
| 4 | 모델 사용 가능 여부 (실행 시점) | PENDING_RUNTIME_VERIFICATION | 실행 직전 공식 모델 문서로 확인 |
| 5 | 공식 가격표 재확인 | PENDING_RUNTIME_VERIFICATION | 2026-10-14 조회 값을 실행 직전에 다시 확인 |
| 6 | 입력·출력·캐시 단가 확인 | PENDING_USER_APPROVAL | 문서 값($2/$10/$2.50/$0.20)은 `confirmed=False`. `--price-confirmed`와 출처 필요 |
| 7 | Console 워크스페이스 지출 한도 | PENDING_USER_APPROVAL | 코드 밖 설정이라 이 저장소에서 확인할 수 없음 |
| 8 | 파일럿 전용 프로세스 | VERIFIED | 별도 OS 프로세스로 Fake SDK E2E, 동시 웹 실행 무영향 (`test_pilot_process.py`) |
| 9 | 실제 환자 데이터 미사용 | VERIFIED | 게이트가 합성 사례 6개(`PILOT_CASE_POOL`) 밖을 거절, 6개 모두 사례 파일의 `synthetic: true`를 테스트로 확인 |
| 10 | 승인된 합성 사례만 사용 | VERIFIED | 같은 게이트, 사례 목록·순서가 해시에 포함 |
| 11 | SDK 자동 재시도 비활성화 | VERIFIED | 재시도가 켜진 클라이언트를 거부, SDK 훅이 재시도 회차를 차단(가짜 HTTP로 실제 SDK 객체 확인). 단 실제 클라이언트 생성 줄(`sdk_client`)은 스위치가 꺼져 있어 실행된 적 없음 |
| 12 | SDK 내부 훅 호환 (실행 환경의 SDK 버전) | PENDING_RUNTIME_VERIFICATION | anthropic 1.13.0에서 확인. `requirements.txt`·`pyproject.toml`은 버전을 고정하지 않으므로 실행 환경에서 `tests/test_call_guard.py`를 먼저 통과시킬 것 |
| 13 | 대체 모델 비활성화 | VERIFIED | `CTREC_FALLBACKS=0` 필수, 실행 중 대체 모델 끔, `fallbacks` 요청 거부, 대체 모델 응답이 보이면 중단 |
| 14 | 계측 우회 차단 | VERIFIED | 컨텍스트 없는 스레드·스레드 풀·`run_in_executor`·직접 SDK 사용·async SDK 차단. 막지 못하는 것: SDK 밖 직접 HTTP, 다른 프로세스 |
| 15 | 비용·호출·토큰 상한 | VERIFIED | Fake SDK로 같은 프로세스·별도 프로세스 모두 확인. 실제 토큰 수에 따른 동작은 #19 |
| 16 | 계획 해시 확인 | VERIFIED | 구현상 모든 항목이 해시에 반영되고, 변경 시 재승인이 필요함 (7절, 22개 변경 테스트). 실제 실행 계획의 해시는 단가 확인 뒤 생성 |
| 17 | 최종 확인 문구 | PENDING_USER_APPROVAL | 단가 확인 전에는 CLI가 만들지 않음. 계획 해시가 정해진 뒤 생성 |
| 18 | 결과 저장 기본 OFF | VERIFIED | `save_to=None` 기본, `outputs/` 미생성, 저장 시 원자적 쓰기 |
| 19 | 비식별화 | VERIFIED (합성·Fake 기준) / PENDING_RUNTIME_VERIFICATION (실제 응답) | 결과에는 ID·코드·건수만 남음. 원문·키·개인정보 형태를 검사. 실제 LLM 응답에서 다시 확인 필요 |
| 20 | 실행 중단 조건 | VERIFIED (Fake) / PENDING_RUNTIME_VERIFICATION | 자동 중단 이유 코드를 각각 재현. 실제 API 오류 양상은 실행 때 확인 |
| 21 | 실행 후 결과 검토 | PENDING_RUNTIME_VERIFICATION | 5·6절 기준으로 검토하고, 통과해야 실험 B 승인 검토 |
| 22 | 실행 커밋 기록 | PENDING_USER_APPROVAL | 실행할 커밋 해시를 승인 기록에 포함 (프롬프트·코드는 해시 밖) |

## 9. 다음 단계의 작업 범위

**현재 Decision Loop는 진단 전용입니다.**
- `Session`의 기본값은 `decision_loop="off"`입니다. 웹은 개발자 진단을 요청했을 때만 `"diagnostic"`으로 켜며, 판정·질문·추천에는 쓰지 않습니다.
- 실제 웹의 확인 질문은 기존 LLM 질문 생성기(`question_generator.generate_questions`)가 만듭니다.
- 오프라인 평가와 실험 A에서만 평가용 어댑터(`question_generator.use_generator`)가 Decision Loop 계획을 질문으로 넣습니다. 이것은 실제 웹 질문 경로에 연결된 것이 아닙니다.

### A. 실제 LLM 파일럿 (별도 승인 후)

실험 A를 소규모(위 3개 사례)로 실행합니다. 검증 항목:
- 실제 프로파일링 정확성
- 사실 출처(provenance)
- Matcher 판정 안정성
- 검증 상태 승격 오류
- 바이오마커 연결
- 호출·토큰·비용
- 오류·중단 처리

### B. 질문 계획 비교 (A가 안정적일 때, 별도 승인 후)

같은 초기 상태에서 기존 LLM 질문 생성기와 Decision Loop 질문 계획을 비교합니다. 비교 항목:
- 질문 중복
- 질문 관련성
- 질문 예산 안 적중

### C. 웹서비스 통합 (A·B 결과를 바탕으로)

- Decision Loop를 실제 질문 생성 경로에 연결할지 결정
- 웹 실행별 비용 상한 (`web_cost_control_design.md`의 enforce 모드)
- 공용 영속 비용 원장
- 취소·타임아웃 통합
- 예산 초과 안내
- 질문·답변 UX
- 추천 결과와 근거 표시
- 실제 사용자 흐름 E2E 검증
