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
