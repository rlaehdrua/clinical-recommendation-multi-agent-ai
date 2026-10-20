# 실제 LLM 소규모 평가 준비 (Phase 5-0)

이 문서는 준비 상태만 정리합니다. **실제 유료 호출은 아직 하지 않았고 실행 경로도 꺼져 있습니다.**
실행 경로는 `llm_pilot.PAID_EXECUTION_ENABLED = False`로 막혀 있으며, 환경 변수나 플래그로는 켤 수 없습니다.

- 코드: `src/ctrec/llm_pilot.py`
- dry-run: `scripts/prepare_llm_pilot.py --subset representative|representative_egfr|all16`
- 테스트: `tests/test_llm_pilot.py`

## 1. 두 실험

| | 실험 A: 파이프라인 안정성 | 실험 B: 질문 계획 비교 |
|---|---|---|
| 목적 | 실제 프로파일러의 사실 추출, 실제 Matcher 판정, 출처 유지, 실패·재시도, 지연·토큰, eGFR/EGFR 구분 | 같은 상태에서 기존 LLM 질문 생성기와 Decision Loop ASK_USER 계획 비교 |
| 흐름 | 프로필 → 판정(라운드 0) → 확인 질문 1라운드(Decision Loop 계획, 결정적 정답 시뮬레이터) → 재추출·재판정 → 추천 | 프로필 → 판정(라운드 0) → 상태 지문 → 두 계획 생성 → 지문 재확인. 답변은 반영하지 않음 |
| LLM 단계 | 프로파일러, Matcher, 추천 | 프로파일러, Matcher, 질문 생성기 |
| 판단하지 않는 것 | 질문 정책의 우열 | 답변 이후의 결과 (상태가 달라지므로 비교하지 않음) |

성공 기준은 `llm_pilot.EXPERIMENTS`에 있습니다.

## 2. 실제 호출 경로 (코드 기준)

모든 호출은 `llm.create`(`structured`/`plain` 포함)를 거칩니다. 목록은 `llm_pilot.CALL_PATHS`에 있으며, 테스트가 소스의 호출 위치와 대조합니다.

통제 항목은 `llm_pilot.CONTROLS`에 정리했습니다.
- SDK 자동 재시도(`max_retries=4`)
- Matcher 누락 재요청
- 잘림·거절 시 대체 모델 사용
- 캐시
- 동시 실행
- 취소·시간 제한

## 3. 비용 추정 방법

1. **호출 수:** 단계별 호출 수는 LLM 대역으로 실제 순서대로 돌려 셌습니다.
2. **요청 크기:** 요청 문자 수(시스템 프롬프트, 사용자 입력, 출력 스키마)를 측정했습니다.
3. **토큰 환산:** 토큰은 다음 가정으로 환산했습니다.
   - 문자당 토큰 비율
   - 실제 프로필 크기
   - 호출당 출력 토큰(사고 토큰 포함)
   - 재시도율
   - 캐시 효과
4. **시나리오:**
   - low: 캐시 적중
   - base
   - worst: 모든 호출이 `max_tokens`를 다 쓰고 재시도 상한까지 감
5. **확인 전 값:** 단가, 실제 토큰, 캐시 적중, 재시도 과금, 대체 모델은 **UNKNOWN**입니다.

## 4. 안전장치

- `MeteredClient`: SDK 재시도를 끈 클라이언트를 감쌉니다. 재시도를 포함한 모든 요청이 예약과 정산을 거칩니다.
- `PilotMeter`: 다음 상한과 중단 조건을 적용합니다.
  - 호출 수, 입력·출력 토큰, 비용 상한. 호출 전에 최악값(입력 추정 + `max_tokens`)을 예약합니다.
  - 재시도 상한: 호출별 및 전체
  - 연속 실패
  - 취소, 전체 시간 제한
  - 요청별 timeout
- 승인 게이트(`approval_problems`)는 다음을 모두 요구합니다.
  - 명시 승인 기록
  - 계획 해시
  - 최종 확인 문구 `RUN <실험> <해시 12자>`
  - 확인된 단가
  - 실제 모델 일치
  - 계획 이하의 상한
  - `CTREC_FALLBACKS=0`
  - 환자 데이터 비저장
  - 출력 비식별화
  - `CTREC_ALLOW_PAID_EVAL=1`
- 이번 단계에서는 위 조건을 모두 갖춰도 `run_pilot`이 거절합니다.
- 출력에는 ID, 코드, 건수만 넣습니다. `assert_redacted`가 원문 줄, 숨긴 정답, API 키 문자열이 없는지 검사합니다.

## 5. Phase 5-1: 실행 경로 연결 (유료 호출 없음)

실행 경로는 연결했지만 기본은 OFF입니다. `PAID_EXECUTION_ENABLED = False`인 동안 `authorize`가 실행 표를 만들지 않으므로 실제 SDK 클라이언트도 만들어지지 않습니다.

- 실행 입구: `scripts/run_llm_pilot.py` (기본은 계획·해시·게이트 상태만 출력, `--execute`도 거절됨)
- 실행기: `src/ctrec/llm_pilot_exec.py` (`execute`, dry-run용 `simulate`)
- Fake SDK: `src/ctrec/llm_pilot_fake.py` (실제 SDK와 같은 `messages.create`, 장애 주입)
- 테스트: `tests/test_llm_pilot_exec.py`

### 호출 흐름

```
run_pilot -> authorize (12개 조건 + 코드 스위치) -> ExecutionTicket
          -> sdk_client(ticket): anthropic.Anthropic(max_retries=0, timeout=call_timeout_s)  ※ 사용 시점에 조건 재확인
          -> execute: MeteredClient가 llm.client 자리를 대신함 -> 실험 A 또는 B -> 비식별 검사 -> (선택) 저장
```

`execute`는 유효한 실행 표가 없으면 `FakeSDKClient`만 받습니다.

### 과금 기록

| 항목 | 처리 |
|---|---|
| 사고 토큰 | `output_tokens`에 포함 (따로 보고되지 않음). 사고 블록 수만 셈 |
| 캐시 | 쓰기·읽기 토큰을 따로 기록하고 각 단가로 계산 |
| 재시도 | 각 시도마다 예약·정산. 429·4xx는 과금 없음, 응답 없이 끝난 요청(시간 초과·연결·5xx)은 예약한 최악값으로 보수적 과금 |
| 사용량 누락 | 최악값으로 과금하고 다음 요청부터 중단 |
| 대체 모델 | `fallbacks`·`betas`가 있는 요청은 보내지 않음. 응답 모델이 다르거나 `usage.iterations`에 대체 실행이 보이면 그 응답을 쓰지 않고 중단 |

결과에는 실제 사용량 비용(`cost_usd`)과 보수적 추정(`cost_estimated_usd`)이 따로 나옵니다. 단가가 확인되지 않았으면 `cost_kind`가 추정으로 표시됩니다.

### 실험별 흐름

- **A:** 프로파일러 → Matcher → Decision Loop 질문(결정적 정답 시뮬레이터) → 재추출·재판정 → 추천
  - 지표: 사실 출처·검증 상태 승격, 결정적 추출 대비 핵심 항목 일치, Matcher 누락·미평가, eGFR/EGFR, 정답 판정 일치 수
- **B:** 프로파일러 → Matcher → 상태 지문 → Decision Loop 계획 + 기존 LLM 질문 생성기
  - 두 생성기 입력 지문까지 같아야 비교에 포함 (`comparison_valid`). 다르면 무효로 셈

## 6. Phase 5-2: 실행별 LLM 격리

파일럿은 더 이상 전역 `llm.client`나 `config` 값을 바꾸지 않습니다. 실행마다 아래를 **그 실행의 컨텍스트에만** 적용합니다(contextvars).

| 장치 | 위치 | 대상 |
|---|---|---|
| `llm.use_client(c)` | `llm.py` | `llm.create`가 쓰는 클라이언트 (없으면 기존 기본 클라이언트) |
| `config.overrides(...)` | `config.py` | `MODEL`, `USE_FALLBACKS`, `WRITE_REPORT`, `QUESTION_POLICY`, `MAX_QUESTIONS_PER_ROUND`, `LAZY_COMPILE` |
| `question_generator.use_generator(fn)` | `agents/question_generator.py` | `run_method`의 질문 생성기 교체 (전역 함수 교체 제거) |

- 실행마다 새 `PilotMeter`·`MeteredClient`·실행 ID·모델·단가·상한·취소 상태를 씁니다.
- 공용 상한(`SharedBudget`: 공용 비용·동시 실행 수)은 실행별 상한과 별도 계층입니다.
- contextvars는 스레드에 자동으로 넘어가지 않습니다.
  - 하위 스레드는 `copy_context()`로 시작해야 합니다. `Session._parallel`, `run_method`, 웹 실행 스레드는 이미 그렇게 합니다.
  - `run_in_executor`·맨 `ThreadPoolExecutor.submit`은 컨텍스트를 넘기지 않아 기본 클라이언트로 갑니다(테스트로 확인).
  - `src` 안의 스레드 생성 지점은 정적 테스트로 검사합니다.
- 오프라인 전용 전역 교체는 남아 있습니다: `OfflineEnvironment`, 비용 측정용 `_recording`, `tie_order`. 파일럿 실제 경로에서는 쓰지 않습니다.
- 웹 비용 통제 설계: `docs/web_cost_control_design.md`

## 7. Phase 5-3: 미계측 호출 차단과 웹 기록 전용 계측

### 파일럿 fail-closed (`call_guard`)

`execute`는 기본으로 `process_guard=True`입니다. 실제 SDK 실행에서는 끌 수 없습니다. 실행 동안 프로세스 전체에 다음을 적용합니다.

| 지점 | 통과 조건 | 막는 것 |
|---|---|---|
| `llm.client()` / `llm.current_client()` (전역 관문) | 지금 컨텍스트의 클라이언트가 **등록된 MeteredClient** | 컨텍스트 없는 스레드, 기본 클라이언트, 등록되지 않은 대리 객체 |
| SDK `SyncAPIClient.request` / `_attempt_request` | 같은 스레드에서 MeteredClient가 **바로 이 SDK 클라이언트**로 보내는 중이라는 표시(mark) | 표시 없는 직접 SDK 사용, SDK 자동 재시도(요청별 `max_retries>0`, 재시도 회차>0), 미들웨어(클라이언트 측 대체 모델) |
| SDK `AsyncAPIClient.request` | 없음 (전부 차단) | async SDK 사용 |

- 관문은 contextvar가 아니라 **모듈 전역 상태**입니다. 그래서 컨텍스트를 넘기지 않은 스레드에서도 작동합니다.
- 막힌 호출이 하나라도 생기면 그 시점에 진행 중인 파일럿은 `unmetered_call_blocked`로 중단합니다(어느 실행 것인지 알 수 없으므로).
- 실행이 모두 끝나면 SDK 훅을 걷고 관문을 끕니다. 그 뒤의 CLI·웹은 기존 동작 그대로입니다.

**막지 못하는 것:**
- SDK를 쓰지 않는 직접 HTTP 요청
- 다른 프로세스
- `call_guard`나 SDK 내부를 다시 바꿔 끼우는 코드
- 다른 제공자의 SDK

따라서 유료 파일럿은 **전용 프로세스**에서 실행합니다.

### 웹 기록 전용 계측 (`web_metering`)

- 웹 실행마다 `RecordingClient`를 실행 컨텍스트에 겁니다. 호출 인자·응답·예외·재시도·대체 모델 동작은 바꾸지 않습니다.
- 기록은 소유자 전용 `GET /api/runs/{id}/metering`으로 확인합니다. 기존 SSE와 상태 응답은 바뀌지 않습니다.
- 내용: 단계별 호출, 토큰, 캐시, 재시도(보일 때만), 응답 모델, 실행 시간, 계측 상태(`FULLY_METERED` / `PARTIALLY_METERED` / `UNMETERED` / `USAGE_UNKNOWN`)
- 비용은 항상 `UNKNOWN`이고 `cost_cap_enforced: false`입니다.
- 끄기: `CTREC_WEB_METERING=off` (끄면 상태는 `UNMETERED`로 표시됩니다)

## 8. Phase 5-4: 최종 준비

- 최종 실행 계획: `docs/llm_pilot_final_plan.md` (모델·단가 공식 확인, 사례, 상한, 명령, 중단·성공 기준)
- 전용 프로세스 리허설: `scripts/run_llm_pilot.py --fake-sdk`
  - 승인 게이트만 빼고 실제 실행 경로를 탑니다(프로세스 보호, 상한, 원자적 저장, 중단 시 exit 130).
  - 테스트: `tests/test_pilot_process.py`
- 계측 보강:
  - 사고 토큰: 공식 `usage.output_tokens_details.thinking_tokens`를 기록합니다.
  - `service_tier`가 standard가 아니면 중단합니다(확인한 단가와 등급이 다름).
- 단가: `DOCUMENTED_PRICES`는 공식 문서 값이지만 `confirmed=False`입니다. CLI는 사용자가 단가를 확인하기 전에는 확인 문구를 만들지 않고, `readiness.ready_to_execute=False`로 표시합니다.
