# 웹 비용 통제 통합 설계 (Phase 5-2 설계, Phase 5-3 구체화)

이 문서는 설계안입니다. **웹의 호출 정책은 바꾸지 않았습니다.** 대체 모델 켜짐(`CTREC_FALLBACKS=1`), SDK 자동 재시도 4회, 비용 상한 없음은 그대로입니다.

Phase 5-3에서는 **기록 전용 계측**만 연결했습니다(`web_metering`, `GET /api/runs/{id}/metering`). 이 계측은 호출을 막지 않으므로 비용 상한을 보장하지 않습니다. 구체화한 내용은 12절에 있습니다.

## 0. 전제: 실행별 계측만으로는 공용 상한이 해결되지 않음

Phase 5-2에서 파일럿의 실행별 격리를 구현했습니다.
- `llm.use_client`, `config.overrides`, `question_generator.use_generator`
- 실행마다 새 `PilotMeter`·`MeteredClient`

이것으로 **한 실행 안**의 호출·토큰·비용은 정확히 막힙니다. 하지만 실행이 여러 개면 합계는 막지 못합니다. 예를 들어 실행당 $1 상한, 동시 20개, 시간당 20회 시작이면 시간당 최대 수십 달러가 나갈 수 있습니다.

그래서 웹에는 두 계층이 따로 필요합니다.

| 계층 | 범위 | 구현 위치 | 저장 |
|---|---|---|---|
| 실행별 상한 | 실행 1개 (`run_id`) | `PilotMeter`와 같은 예약·정산 | 메모리 (실행과 수명이 같음) |
| 공용 상한 | 서비스 전체 (시간대별) | `SharedBudget` + 영속 원장 | **영속 저장 필수** (아래 8절) |

`llm_pilot.SharedBudget`은 공용 계층의 메모리 버전 시제품입니다. 공용 비용 예약과 동시 실행 수를 맡고, 테스트(`test_llm_isolation.py`)로 실행별 계층과 분리됨을 확인했습니다. 웹에 그대로 쓰기에는 8절의 영속성이 부족합니다.

## 1. 실행별 비용 상한

- 웹 실행 시작 시 `RunMeter`(= `PilotMeter`)를 만들어 실행 스레드의 컨텍스트에 `llm.use_client(MeteredClient(...))`로 겁니다.
  - 지금 `_execute`가 `llm.set_guard`를 거는 자리와 같습니다.
  - 실행 스레드는 이미 `copy_context()`로 시작되고, Matcher 병렬 스레드도 `Session._parallel`이 컨텍스트를 넘깁니다.
- 예약 기준: 호출 전 최악값(입력 추정 + `max_tokens`)을 예약하고, 응답 뒤 실제 사용량으로 정산합니다. 오케스트레이터 루프 호출도 포함합니다.
- 설정값 (제안):
  - `CTREC_WEB_RUN_MAX_USD`: 기본값은 추정 base의 3배 수준. 실제 단가 확인 뒤 정함
  - `CTREC_WEB_RUN_MAX_CALLS`
- 동시 예약: Matcher를 `MAX_WORKERS`개 동시에 부르면 예약도 그만큼 동시에 잡힙니다. 그래서 실행별 출력 토큰 상한은 최소 (`MAX_WORKERS` + 1) × `max_tokens` 이상이어야 정상 실행이 막히지 않습니다.

## 2. 공용 전체 비용 상한

- 시간 창: 일 단위 + 월 단위 두 개를 제안합니다(`CTREC_WEB_DAILY_MAX_USD`, `CTREC_WEB_MONTHLY_MAX_USD`).
- 요청 직전에 실행별 예약을 통과한 뒤 공용 예약을 잡습니다.
  - 잠금 순서는 실행 → 공용으로 고정합니다(현재 구현과 같음).
  - 공용 예약이 실패하면 그 실행은 `shared_cost_cap`으로 멈춥니다.
- 정산: 실제 사용량 비용과 보수적 추정 비용(응답 없는 실패, 사용량 누락)을 모두 공용 원장에 반영합니다.
- 공용 상한에 가까워지면(예: 80%) 새 실행 시작을 거부하는 쪽이 사용자 경험상 낫습니다(HTTP 429 + 안내 문구). 진행 중인 실행은 자기 예약 안에서 끝나게 둡니다.
- 마지막 방어선: 코드 바깥에서 **Anthropic Console의 워크스페이스 지출 한도**를 함께 겁니다. 코드 결함이나 원장 손실에 대비한 것입니다.

## 3. 동시 실행 제한

- 기존 제한은 유지합니다: `MAX_RUNS`(20, 보관 실행 수), `MAX_ACTIVE_PER_CLIENT`(1), `MAX_STARTS_PER_HOUR`(20).
- 추가로, 동시에 **진행 중인** 실행 수 상한을 둡니다(`SharedBudget.run_slot`, 예: 4). 최대 동시 API 요청은 진행 중 실행 수 × `MAX_WORKERS`입니다.
- 필요하면 프로세스 전역 세마포어로 진행 중 요청 수를 따로 묶습니다. 이 경우 rate limit(429) 감소 효과도 있습니다.
- `render.yaml`은 `numInstances: 1`을 전제로 합니다. 인스턴스를 늘리면 공용 상한은 인스턴스 간 공유 저장소(DB 등)로 옮겨야 합니다.

## 4. SDK 자동 재시도 처리

- 현재 `llm.client()`는 `max_retries=4`입니다. SDK가 429·5xx·연결 오류를 최대 5번 보내도 호출 수와 비용 집계에 1번으로 보입니다.
- 제안: 웹에서 계측할 때는 `max_retries=0`인 클라이언트를 `MeteredClient`로 감싸고, 재시도는 래퍼에서만 합니다. 이렇게 하면 시도마다 예약·정산되고 호출별·전체 재시도 상한이 적용됩니다.
- SDK 재시도를 그대로 두는 대안도 있습니다. 이때는 예약을 (1 + 4) × 최악값으로 잡아야 해서 실행이 지나치게 일찍 막히고, 실제 시도 횟수는 여전히 보이지 않습니다. 권하지 않습니다.

## 5. 대체 모델 정책

- 현재 웹은 `CTREC_FALLBACKS=1`입니다. 거절되면 서버가 대체 모델로 다시 실행하며, 대체 모델과 그 단가는 응답을 받아야 압니다.
- `MeteredClient`는 지금 `fallbacks`가 붙은 요청을 보내지 않습니다(파일럿 정책). 그래서 웹에 그대로 붙이면 웹의 대체 모델 동작이 바뀝니다. 아래 둘 중 하나를 사용자가 정해야 합니다.
  1. **웹 계측 시 대체 모델 끔**: 거절은 `RefusalError`로 처리됩니다(지금도 대체 모델까지 거절하면 같은 처리). 단순하고 비용이 확정적입니다.
  2. **대체 모델 허용 + 모델별 확인 단가표**: 대체 모델 후보 전부의 단가를 확인해 둡니다. `usage.iterations`로 실제로 돈 모델을 확인해 그 단가로 정산하고, 예약은 후보 중 가장 비싼 단가로 잡습니다. 단가표에 없는 모델이 응답하면 그 실행을 멈춥니다.
- 결정 전까지는 웹 정책을 바꾸지 않습니다.

## 6. 비용 초과 시 SSE 이벤트

기존 이벤트(`status`, `trace`, `questions`, `result`, `end`)에 다음을 더합니다. 내부 오류 문구나 요청 내용은 넣지 않습니다.

| 이벤트 | 시점 | 내용 |
|---|---|---|
| `budget` | 호출 정산마다 (빈도 제한: 예 2초당 1회) | `scope`(run/shared), `spent_actual_usd`, `spent_estimated_usd`, `limit_usd`, `price_confirmed` |
| `budget_warning` | 실행 예산 80% 도달 | `scope`, 남은 비율 |
| `budget_exceeded` | 예약 실패로 중단 | `scope`, 이유 코드(`cost_cap`·`shared_cost_cap`·`call_cap`·`usage_missing` 등) |
| `end` | 마지막 | `outcome: "budget_exceeded"` 추가 (기존 completed/cancelled/timed_out와 같은 자리) |

`_END_MESSAGE`에 다음 문구를 추가합니다: `"budget_exceeded": "비용 한도에 도달해 실행을 중단했습니다."` 중단 시점까지의 부분 결과는 보내지 않습니다. 지금 취소·시간 초과 때 결과를 보내지 않는 정책과 같습니다.

## 7. 사용자에게 실제 비용과 추정 비용 구분 표시

- **실제**: 응답의 `usage` × 단가. 단가가 확인된 경우에만 "실제"로 표시하고, 아니면 "추정 단가 기준"으로 표시합니다.
- **보수적 추정**: 응답 없이 끝난 요청(시간 초과·연결 오류·5xx)이나 사용량이 빠진 응답은 예약한 최악값으로 잡습니다. 화면에는 "최대 추정" 등으로 따로 보입니다.
- 사고(thinking) 토큰은 출력 토큰에 포함된다고 안내합니다. 캐시 읽기·쓰기는 별도 줄로 표시합니다.
- 표시 값은 실행 화면에만 둡니다. 서버 로그에는 실행 ID와 숫자만 남깁니다.

## 8. 서버 재시작 시 예산 상태

- 메모리에 둔 `SharedBudget`은 재시작하면 사라집니다. Render 무료 플랜은 15분간 접속이 없으면 잠들기 때문에, 사실상 수시로 재시작됩니다.
- 제안:
  - 공용 원장을 영속 저장소에 둡니다. 단일 인스턴스면 SQLite 파일, 인스턴스가 여럿이면 DB를 씁니다.
  - 기록 항목은 시간 창 키, 확정 비용, 미정산 예약(실행 ID, 예약액, 시각)입니다. 환자 정보는 넣지 않습니다.
  - 시작할 때 **미정산 예약은 전부 사용한 것으로 확정**합니다(보수적). 재시작 전에 보낸 요청이 과금됐을 수 있기 때문입니다.
  - Render 무료 플랜의 디스크는 재배포하면 사라지므로, 영속 디스크나 외부 저장소가 필요합니다. 그때까지는 2절의 Console 지출 한도가 사실상 유일한 공용 상한입니다.
- 진행 중이던 실행은 재시작과 함께 사라집니다(현재와 같음). 다시 시작하면 새 실행 ID와 새 실행 예산을 받습니다.

## 9. 취소 및 타임아웃 통합

- 웹의 `run.stop_reason`(취소·`RUN_TIMEOUT_S`)을 `RunMeter`의 `cancel`/deadline으로 연결합니다. 이렇게 하면 `llm.guard`와 예약 확인이 같은 판단을 씁니다.
- 요청별 timeout: `MeteredClient`가 SDK 요청에 넘깁니다. 이미 보낸 요청은 중단할 수 없으므로, 응답이 오면 정산하고 다음 요청 전에 멈춥니다. 응답이 없으면 보수적 과금으로 처리합니다.
- 실행 종료(완료·취소·시간 초과·예산 초과·예외) 시:
  - 실행별 예약을 모두 정산하거나 해제합니다(`finally`).
  - 공용 슬롯을 반납합니다(`run_slot`의 `finally`).
  - 컨텍스트 클라이언트를 복구합니다(`use_client`의 `finally`).

## 10. 결과 비저장 기본값

- 웹은 지금도 결과 파일 저장이 기본 꺼짐입니다(`CTREC_WEB_SAVE_OUTPUTS`). 비용 원장을 추가해도 이 기본값은 바꾸지 않습니다.
- 비용 원장에는 실행 ID, 시각, 금액, 토큰 수, 이유 코드만 넣습니다. 환자 원문, LLM 응답, 질문·답변은 넣지 않습니다.

## 11. 통합 순서 제안

1. 단가 확인(모델, 대체 모델 정책 결정)
2. 웹 실행에 `RunMeter` 연결(이 문서 1·4·9절). 처음에는 상한을 넉넉히 두고 기록만 합니다.
3. SSE `budget` 이벤트와 화면 표시(6·7절)
4. 영속 공용 원장(2·8절)과 Console 지출 한도
5. 상한을 실제 값으로 조이기

## 12. Phase 5-3 구체화

### 12.1 현재 상태 (구현됨)

| 구분 | 파일럿 | 웹 |
|---|---|---|
| 계측 | `MeteredClient` (예약·정산·상한) | `RecordingClient` (기록만) |
| 미계측 호출 | **차단 (fail-closed)**: `call_guard` 배타 모드 | **관측**: `call_guard` 관측 모드 → `PARTIALLY_METERED`·`UNMETERED` |
| SDK 재시도 | 금지 (클라이언트 `max_retries=0` + SDK 훅) | 기존 그대로(4회). 실제 SDK로 보낸 요청이면 재시도 수를 기록 |
| 대체 모델 | 요청에 붙으면 거부, 응답에 보이면 중단 | 기존 그대로. 응답 모델 이름을 기록 |
| 비용 | 확인 단가 × 사용량 (단가 미확인이면 추정으로 표시) | 항상 `UNKNOWN` (확인된 단가표 없음) |

웹 계측 상태의 판정 기준은 `web_metering` 모듈 설명과 아래 최종 보고 F항에 있습니다.

### 12.2 실행별 비용 상한 (웹 enforce 모드, Phase 6-3에서 구현)

- `CTREC_WEB_METERING`에 `enforce` 값을 추가합니다. 이 모드에서는 `RecordingClient` 대신 `MeteredClient`를 쓰고, 내부 SDK 클라이언트는 `max_retries=0`으로 만듭니다.
- 상한 값:
  - `CTREC_WEB_RUN_MAX_USD`
  - `CTREC_WEB_RUN_MAX_CALLS`: 오케스트레이터 최대 단계(`MAX_ORCHESTRATOR_STEPS=15`) + 시험 수 × (1 + `MATCHER_MISSING_RETRIES`) × 라운드 + 여유분
  - 출력 토큰 상한: 최소 (`MAX_WORKERS` + 1) × `max_tokens`
- 상한 초과 시 실행을 `budget_exceeded`로 끝냅니다(6절 SSE). 웹의 공용 슬롯 제한(`MAX_RUNS` 등)은 그대로 둡니다.
- 먼저 enforce를 "그림자 모드"로 운영합니다. 실제로 막지는 않고 막았을 시점만 기록해 오탐률을 봅니다.

### 12.3 공용 영속 비용 원장 (Phase 6-3에서 단일 인스턴스 SQLite로 구현)

```
ledger_window(window_key TEXT PRIMARY KEY,      -- 예: day:2026-10-14, month:2026-10
              limit_usd REAL, committed_usd REAL NOT NULL DEFAULT 0)
reservation(reservation_id TEXT PRIMARY KEY, run_id TEXT, window_key TEXT,
            amount_usd REAL, created_at REAL, state TEXT)   -- open / settled / expired
```

- **예약:** 한 트랜잭션 안에서 `committed + SUM(open) + amount <= limit`를 확인한 뒤 open 예약을 추가합니다(SQLite는 `BEGIN IMMEDIATE`).
- **정산:** 예약을 settled로 바꾸고, `committed_usd`에 실제 비용 또는 보수적 추정을 더합니다.
- **기록하지 않는 것:** 환자 정보, 요청·응답 내용. 실행 ID, 금액, 시각, 이유 코드만 남깁니다.

### 12.4 서버 재시작 시 미정산 예약

- 시작할 때 `state='open'`인 예약은 모두 **예약액 그대로 committed로 확정**하고 expired로 표시합니다. 재시작 직전에 보낸 요청이 과금됐을 수 있기 때문입니다.
- 확정한 건수와 금액을 서버 로그에 남깁니다(숫자만).
- 진행 중이던 웹 실행은 메모리 상태라 사라집니다(현재와 같음).

### 12.5 여러 프로세스 동시 실행

- **`call_guard`는 프로세스 단위입니다.** 다른 프로세스의 호출은 보지도 막지도 못합니다. 그래서 유료 파일럿은 **전용 프로세스**에서 실행해야 합니다. 같은 프로세스에서 웹이 돌면 웹 호출이 막힙니다(테스트로 확인).
- **SQLite 원장:** 같은 호스트·같은 디스크의 여러 워커가 파일 잠금으로 함께 쓸 수 있습니다.
- **인스턴스가 여럿이면** 공유 DB(Postgres 등)의 원자적 갱신이 필요합니다. 예: `UPDATE ... SET committed = committed + x WHERE committed + open + x <= limit`
- 현재 `render.yaml`은 `numInstances: 1`을 전제로 합니다(실행 상태가 메모리에 있음).

### 12.6 모델별 단가

- 단가표 파일을 둡니다(예: `data/pricing.json`). 항목: `model`, `input`, `output`, `cache_write_5m`, `cache_write_1h`, `cache_read`, `confirmed`, `source`(출처·확인일).
- 사용 규칙:
  - 응답의 `model`을 단가표와 정확히 맞춥니다(별칭 + 날짜 스냅샷만 허용, `llm_pilot.same_model`).
  - 표에 없는 모델이 응답하면: enforce 모드에서는 중단, 기록 모드에서는 비용 `UNKNOWN`.
- `confirmed`가 아닌 단가로 계산한 값은 화면에 "추정"으로만 표시합니다.

### 12.7 SDK 재시도

- **기록 모드:** SDK 재시도를 그대로 둡니다. `call_guard`의 SDK 시도 훅으로 재시도 수를 셉니다. SDK가 아닌 대역이면 `UNKNOWN`입니다.
- **enforce 모드:** `max_retries=0` + 래퍼 재시도로 바꿉니다. 시도마다 예약·정산하고, 응답 없이 끝난 시도는 보수적으로 과금합니다.

### 12.8 대체 모델

- **기록 모드:** 기존 그대로입니다. `fallbacks` 요청 수와 응답 모델을 기록합니다.
- **enforce 모드:** 사용자 결정이 필요합니다(5절의 두 안).
  - 허용하려면 대체 모델 후보 전부의 확인 단가가 있어야 합니다.
  - 예약은 후보 중 최고 단가로 잡습니다.
  - `usage.iterations`로 실제로 돈 모델을 확인해 정산합니다.

### 12.9 예산 초과 SSE 이벤트

- 6절의 `budget`, `budget_warning`, `budget_exceeded`는 enforce 모드에서만 보냅니다.
- 기록 모드는 SSE를 바꾸지 않습니다(새 이벤트 없음, 테스트로 확인). 계측 정보는 소유자 전용 `GET /api/runs/{id}/metering`으로만 제공합니다.

### 12.10 사용자에게 표시할 비용 정보

| 표시 | 조건 |
|---|---|
| 계측 상태 배지 | FULLY / PARTIALLY / UNMETERED / USAGE_UNKNOWN. FULLY가 아니면 "일부 호출이 기록되지 않음" 또는 "사용량 확인 불가"를 함께 표시 |
| 호출 수·토큰·캐시 | 항상 (기록된 값만) |
| 비용 | 확인 단가가 있고 FULLY일 때만 "실제 사용량 기준"으로 표시. 그 밖에는 "알 수 없음" 또는 "추정" |
| 비용 상한 | enforce 모드에서만 "상한 $X 적용". 기록 모드에서는 상한 문구를 쓰지 않음 (`cost_cap_enforced: false`) |

### 12.11 Render 환경 저장소 선택

- 무료 플랜 웹 서비스는 15분간 접속이 없으면 잠들고 로컬 디스크가 유지되지 않습니다. 그래서 무료 플랜에서는 SQLite 원장을 쓸 수 없습니다.
- 선택지 (가격과 조건은 Render 공식 문서로 확인 필요):
  1. **유료 인스턴스 + 영속 디스크 + SQLite**: 단일 인스턴스 전제에 맞고 가장 단순합니다.
  2. **관리형 Postgres**: 인스턴스를 늘려도 쓸 수 있습니다. 무료 DB의 보존 기간 제한은 확인이 필요합니다.
  3. **외부 저장소 없음**: Anthropic Console의 워크스페이스 지출 한도만 사용합니다. 실행별·일별 세분화는 안 되지만 코드나 원장이 실패해도 작동합니다.
- 권고: 어느 안이든 3번(Console 지출 한도)을 기본 방어선으로 같이 둡니다.

## 13. Phase 6-3 구현 상태

- 구현: `src/ctrec/web/budget.py`
  - `enforce` 모드
  - 실행별 한도: 비용, 호출, 입력·출력 토큰, 시간
  - SQLite 공용 원장: 원자적 예약·정산, UNKNOWN 보류, 재시작 복구
  - SDK 재시도 0 + 래퍼 재시도
  - 대체 모델 거부
- 운영 도구: `scripts/web_budget_ledger.py`
- 운영 안내: `web_budget_operations.md`
- 위 설계와 다른 점
  - "그림자 모드"는 만들지 않았습니다. `record`가 기존 기록 전용 모드 역할을 합니다.
  - 예산 초과는 별도 `budget_*` SSE 이벤트 대신 기존 `end` 이벤트의 종료 상태로 알립니다: `budget_exceeded` / `usage_unknown` / `budget_error`
- 남은 일: 여러 인스턴스를 위한 외부 DB 원장 (단일 인스턴스 밖의 전역 한도는 보장하지 않음)
