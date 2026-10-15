# 웹 LLM 비용 통제 운영 안내 (Phase 6-3, 6-5)

웹 분석 실행의 LLM 호출을 서버 설정 한도로 통제하는 방법입니다.
- 설계 배경: `web_cost_control_design.md`
- 배포 구성(record·enforce)과 사전 점검: `web_deployment.md`

**이 통제는 실제 외부 청구액의 절대 상한을 보장하지 않습니다.** 호출 전에 보수적인 추정치로 예약하고 응답의 사용량으로 정산하는 방식입니다. 따라서 다음 경우 실제 청구액과 다를 수 있습니다.
- 응답을 받지 못한 요청: 청구 여부를 알 수 없음
- 가격표 변경
- 서버 밖에서 같은 API 키를 쓰는 경우

## 1. 모드

`CTREC_WEB_METERING`

| 값 | 동작 |
|---|---|
| `record` (기본) | 사용량을 기록만 함. 한도로 막지 않음 (`cost_cap_enforced: false`) |
| `off` | 기록하지 않음 (`UNMETERED`) |
| `enforce` | 호출 직전에 실행별 한도와 공용 한도로 예약·검사함. 한도를 넘으면 호출하지 않고 실행을 멈춤 |

- `enforce`에 필요한 설정이 하나라도 빠지거나 잘못되면 분석 시작을 거부합니다(HTTP 503). `record`로 조용히 내려가지 않습니다.
- `off`, `record`가 아닌 알 수 없는 값도 `enforce`로 취급하므로, 같은 방식으로 거부됩니다.
- Phase 5 파일럿 실행 게이트와는 별개입니다. `PAID_EXECUTION_ENABLED = False`는 그대로입니다.

## 2. 설정 (서버 환경 변수만, 사용자가 올릴 수 없음)

| 변수 | 뜻 |
|---|---|
| `CTREC_WEB_RUN_MAX_USD` | 실행 1건의 추정 비용 한도 (USD) |
| `CTREC_WEB_RUN_MAX_CALLS` | 실행 1건의 LLM 호출 횟수 한도 (재시도 포함) |
| `CTREC_WEB_RUN_MAX_INPUT_TOKENS` / `_OUTPUT_TOKENS` | 실행 1건의 입력·출력 토큰 한도 |
| `CTREC_WEB_RUN_MAX_SECONDS` | (선택) 실행 시간 한도. 웹 실행 시간 한도(기본 1시간)보다 길게 할 수 없음 |
| `CTREC_WEB_SHARED_MAX_USD` / `_CALLS` / `_INPUT_TOKENS` / `_OUTPUT_TOKENS` | 모든 실행을 합친 공용 한도 |
| `CTREC_WEB_BUDGET_PERIOD` | 공용 한도 기간: `day`(기본, UTC 날짜) / `month` / `total` |
| `CTREC_WEB_PRICE_INPUT` / `_OUTPUT` / `_CACHE_WRITE` / `_CACHE_READ` | `config.MODEL`의 100만 토큰당 단가 (USD) |
| `CTREC_WEB_PRICE_MODEL` | 단가를 확인한 모델 ID. `CTREC_MODEL`과 다르면 enforce 거부 (모델만 바꾸고 단가를 그대로 두는 실수 방지) |
| `CTREC_WEB_PRICE_CONFIRMED=1` + `CTREC_WEB_PRICE_SOURCE` | 운영자가 공식 가격표를 확인했다는 기록. 예: `CTREC_WEB_PRICE_SOURCE=anthropic-pricing-2026-10-15` |
| `CTREC_WEB_BUDGET_LEDGER` | 공용 원장 SQLite 파일 경로 |
| `CTREC_WEB_BUDGET_LEDGER_PERSISTENT=1` | 원장 경로가 재배포·재시작 뒤에도 남는 저장소임을 운영자가 확인함 |
| `CTREC_WEB_BUDGET_SINGLE_INSTANCE=1` | 이 원장을 쓰는 서버 프로세스가 하나뿐임을 운영자가 확인함 |
| `CTREC_WEB_BUDGET_MAX_RETRIES` | (선택) 우리 코드가 하는 재시도 횟수 0–3, 기본 1. SDK 자체 재시도는 항상 0 |

- 대체 모델(`CTREC_FALLBACKS`)이 켜져 있으면 `enforce`를 거부합니다. 대체 모델의 단가를 알 수 없기 때문입니다.
- 실행별 한도는 서버 설정에서만 정합니다. 요청 본문이나 쿠키로 바꿀 수 없습니다.

## 3. 한도의 뜻과 한계

**예약 방식**
- 호출 직전에 추정한 최악의 비용을 예약합니다.
  - 입력: 요청 내용 전체의 UTF-8 바이트 수에 여유분 4,096토큰을 더합니다.
    - 요청 내용: 시스템 프롬프트, 메시지, 도구 정의, 출력 형식(모델명·`max_tokens` 제외)
    - 바이트 수: 한글 1자 = 3바이트, 이모지 = 4바이트
    - 여유분: API가 붙이는 메시지 구조, 도구 사용·구조화 출력용 지시. 가격표에 적힌 `claude-sonnet-5`의 도구 사용 시스템 프롬프트는 354·474토큰입니다.
    - 단가: 입력과 캐시 쓰기 중 비싼 쪽
  - 출력: 요청의 `max_tokens` 전체입니다. thinking 토큰도 출력 토큰에 포함되고 `max_tokens`를 넘지 않습니다.
- 예약했을 때 한도를 넘는 호출은 보내지 않습니다.
- 응답이 오면 실제 사용량으로 정산합니다. 여기에는 캐시 쓰기·읽기와 thinking 토큰이 포함됩니다.

**입력 토큰 예약은 추정입니다**
- Claude 토크나이저와 API가 덧붙이는 입력의 크기는 공개되어 있지 않습니다. 그래서 바이트 수 + 여유분이 실제 입력 토큰의 상한이라는 것을 보증할 수 없습니다.
  - 바이트 단위 BPE라면 토큰 수는 바이트 수를 넘지 않지만, 이 가정은 확인할 수 없습니다.
- 실제 입력·출력이 예약보다 크면 실제값으로 정산(공용 원장 반영)하고, 그 실행을 멈춥니다(`estimate_exceeded`, "비용 관리 오류").
- 따라서 실행별·공용 토큰·비용 한도는 **요청 1건의 추정 초과분만큼 넘을 수 있습니다.** enforce 모드는 절대적인 토큰 상한을 보장하지 않습니다.

**보내지 않고 막는 요청 옵션**
- 허용 옵션: `model`, `max_tokens`, `system`, `messages`, `output_config`, `tools`, `tool_choice`
- 위 목록 밖의 옵션은 요청을 보내지 않고 실행을 멈춥니다. 단가나 사용량 형식이 바뀔 수 있기 때문입니다.
  - 예: `service_tier`, `speed`, `inference_geo`, `betas`, `fallbacks`, `stream`, `extra_*`
- 서버 도구(웹 검색 등)도 막습니다. 토큰 외 요금이 붙고 단가표에 없기 때문입니다.

**사용량을 0으로 취급하지 않는 경우**
- 응답에 사용량이 없거나 불완전하면 0으로 보지 않습니다. 예약액을 UNKNOWN으로 남기고 실행을 멈춥니다.

**예상과 다른 응답**
- 다음 응답은 정산하지 않고 UNKNOWN으로 남기며, 실행을 멈춥니다.
  - 다른 모델이 응답함
  - 대체 모델로 처리됨(fallback)
  - service tier가 `standard`가 아니거나 없음
  - 1시간 캐시 쓰기가 있음: 설정 단가는 5분 캐시 기준
  - 지역 지정 추론(`inference_geo`가 `global`이 아님): 공식 가격표상 1.1배 단가

**한도의 한계**
- 예약은 추정치이므로, 실제 청구액은 예약 합계보다 작거나 (가격 변경 시) 다를 수 있습니다.
- 같은 API 키를 다른 프로그램(CLI, 파일럿, 다른 서버)이 쓰면 그 비용은 이 원장에 없습니다. **웹 전용 API 키**와 Anthropic Console의 키별·워크스페이스 지출 한도를 함께 쓰세요.

## 4. 공용 원장과 저장소

**SQLite 원장의 범위**
- SQLite 원장은 **단일 서버 프로세스용**입니다. 예약·정산은 `BEGIN IMMEDIATE` 트랜잭션으로 원자적으로 처리합니다.
- 같은 파일을 여는 여러 스레드·연결 사이의 경쟁은 테스트로 확인했습니다.
- 여러 인스턴스(여러 컨테이너, 여러 서버)에 걸친 전역 한도는 보장하지 않습니다. 네트워크 파일 시스템에서의 SQLite 잠금도 신뢰하지 마세요.
- **같은 호스트의 여러 프로세스도 지원하지 않습니다.** 예: `uvicorn --workers N`, 서버 2개 동시 실행
  - 서버는 원장을 열 때 옆의 잠금 파일(`<원장>.lock`)을 OS 잠금으로 쥡니다(Phase 6-5).
  - 다른 프로세스가 이미 쥐고 있으면 두 번째 프로세스는 원장을 열지 않고 분석 시작을 거부합니다(`ledger_in_use`, 503). 첫 프로세스의 진행 중 예약을 건드리지 않습니다.
  - OS 잠금이라 프로세스가 비정상 종료해도 풀립니다. 그래서 재시작을 막지 않습니다.
  - 이 잠금은 같은 호스트 안에서만 유효합니다. 네트워크 파일 시스템이나 여러 인스턴스에서는 신뢰할 수 없습니다.

**여러 인스턴스로 확장하려면**
- 원자적 조건부 갱신을 지원하는 외부 DB(예: PostgreSQL의 `SELECT … FOR UPDATE`)로 원장을 옮겨야 합니다.
- 이번 단계 범위 밖입니다.

**영속 저장소**
- 원장은 재시작 뒤에도 남아야 합니다. 컨테이너의 임시 파일 시스템에 두면 재배포 때 사용액이 0으로 돌아가므로 공용 한도가 무의미해집니다.
- **Render:** 무료 플랜의 파일 시스템은 영속적이지 않습니다. 영속 디스크(유료 Persistent Disk)를 붙이고 그 마운트 경로에 원장을 둘 때만 `CTREC_WEB_BUDGET_LEDGER_PERSISTENT=1`로 설정하세요.
  - Render 문서 기준:
    - 디스크는 유료 인스턴스에만 붙습니다.
    - 디스크가 있는 서비스는 여러 인스턴스로 늘릴 수 없습니다.
    - 마운트 경로 밖의 파일은 재배포·재시작 때 사라집니다.
    - 디스크가 있으면 무중단 배포가 꺼져, 재배포 때 몇 초 끊깁니다.
- **원장은 자동으로 만들지 않습니다(Phase 6-5).** 서버는 원장 파일이 없거나 비어 있으면 새로 만들지 않고 시작을 거부합니다(`ledger_missing`). 디스크가 마운트되지 않은 채 빈 원장이 생겨 사용액이 0으로 돌아가는 것을 막기 위해서입니다. 처음 한 번 운영자가 만듭니다.
  ```
  PYTHONPATH=src python scripts/web_budget_ledger.py init
  ```
- 이 조건들을 충족할 수 없으면 `enforce`를 쓰지 마세요. 이때는 웹에서 유료 호출을 하지 않는 편이 안전합니다.

**원장에 저장하는 것**
- 저장 항목: 예약 ID, 실행 ID, 기간 키, 모델, 부팅 ID, 상태, 추정·실제 토큰과 금액, 캐시 토큰, 사유 코드, 시각
- 저장하지 않는 것: 환자 원문, 질문·답변, 보고서, 프롬프트, API 키, 쿠키

## 5. 원장 오류 시 동작 (fail-closed)

| 상황 | 동작 |
|---|---|
| 설정 누락·오류 | 분석 시작 거부 (503, 일반 안내 문구만) |
| 원장 파일 없음·빈 파일 (`ledger_missing`) | 분석 시작 거부 (503). 새로 만들지 않음 |
| 다른 프로세스가 원장 사용 중 (`ledger_in_use`) | 분석 시작 거부 (503) |
| 원장 파일을 열 수 없음, 스키마 버전 불일치, 무결성 검사 실패, 정산 상태인데 실제 금액이 없는 행 | 분석 시작 거부 (503) |
| 원장 잠금을 10초 안에 얻지 못함 | 예약 실패 → 요청을 보내지 않고 실행 중단 (`budget_error`) |
| 예약 기록 실패 (디스크 가득 참, 디렉터리 쓰기 불가, 실행 중 원장 삭제) | 요청을 보내지 않고 실행 중단 (`budget_error`). 원장을 다시 만들지 않음 |
| 실행 중 정산 기록 실패·상태 전이 불일치(중복 정산 등) | 해당 실행 중단, 종료 상태 `budget_error` ("비용 관리 오류") |

- 응답을 받은 뒤 정산 기록에 실패한 예약은 원장에 `reserved`로 남습니다.
  - 남은 동안 예약액을 계속 점유합니다.
  - 서버가 재시작되면 UNKNOWN으로 바뀝니다.
- 원장 파일을 지우거나 빈 파일로 바꾸면 서버는 시작을 거부합니다. 운영자가 `init`으로 새로 만들면 사용액이 0부터 다시 셉니다. 원장 파일은 지우지 말고 8절의 방법으로 백업하세요.

화면과 API에는 원장 경로·구조·다른 사용자의 사용량을 보이지 않습니다.

## 6. 응답을 받지 못한 요청 (UNKNOWN)

**대상**
- 타임아웃, 연결 끊김, 5xx 응답, 실행 중 중단처럼 요청이 서버에 닿았을 수 있는 실패

**처리 방식**
- 예약액을 돌려주지 않고 **UNKNOWN** 상태로 남깁니다.
- UNKNOWN 금액은 운영자가 정산할 때까지, 예약한 기간(`day`면 그 UTC 날짜)의 공용 한도에서 계속 차감됩니다. 다음 기간으로 넘어가지는 않습니다.
- 재시도하면 새 예약을 잡습니다. 따라서 중복 청구 가능성도 한도에 반영됩니다.
- 요청 전에 거절된 것이 확실한 실패(4xx, 429 등)는 예약을 해제합니다.

**재시작**
- 서버가 재시작되면, 이전 프로세스가 남긴 `reserved` 예약을 시작할 때 `unknown`(`orphaned_on_restart`)으로 바꿉니다.
- 기존 UNKNOWN은 그대로 유지됩니다. 자동으로 풀지 않습니다.

**UNKNOWN 사유 코드**

| 코드 | 뜻 |
|---|---|
| `response_lost` | 타임아웃·연결 끊김·5xx: 응답을 받지 못함 |
| `interrupted` | 요청 도중 프로세스 중단 |
| `orphaned_on_restart` | 재시작 때 이전 프로세스가 남긴 진행 중 예약 |
| `restored_from_backup` | 백업 복구 때 진행 중이던 예약 |
| `usage_missing` | 응답에 사용량이 없음 |
| `model_mismatch`, `fallback_detected`, `service_tier_mismatch`, `cache_ttl_mismatch`, `inference_geo_mismatch` | 단가를 확인할 수 없는 응답 |

**운영자 정산 절차**
1. UNKNOWN 목록을 확인합니다. 예약 ID, 실행 ID, 사유, 발생 시각(UTC), 경과 시간, 예약 금액·토큰이 나옵니다.
   ```
   PYTHONPATH=src python scripts/web_budget_ledger.py unknown
   ```
2. Anthropic Console의 사용량·청구 내역에서 해당 시각(앞뒤 몇 분)의 `claude-sonnet-5` 요청을 확인합니다.
   - 웹 전용 API 키를 쓰면 대조가 쉽습니다.
   - 외부 청구 내역을 자동으로 가져오는 기능은 없습니다.
3. 결과에 따라 정산합니다.
   - 청구되었으면:
     ```
     PYTHONPATH=src python scripts/web_budget_ledger.py resolve <예약 ID> --cost <USD> --input <N> --output <N>
     ```
   - 청구되지 않았으면:
     ```
     PYTHONPATH=src python scripts/web_budget_ledger.py resolve <예약 ID> --not-billed
     ```
4. 확인할 수 없으면 정산하지 말고 UNKNOWN으로 두세요. 그러면 공용 한도에서 보수적으로 계속 차감됩니다.

**정산 전후 상태**
- 정산 전: 예약 금액 전체가 해당 기간의 공용 한도를 점유합니다.
- 정산 후: `--cost`면 `settled`(입력한 실제 금액만 점유), `--not-billed`면 `released`(점유 없음).
- 이미 정산된 예약은 다시 정산할 수 없습니다(명령이 실패로 끝남).
- 조회·정산 명령은 원장 잠금이나 재시작 복구를 하지 않으므로, 실행 중인 서버와 함께 쓸 수 있습니다.

## 7. 운영자 정기 점검

- `scripts/web_budget_ledger.py summary`로 다음을 확인합니다.
  - 현재 기간 사용량
  - 상태별 행 수
  - UNKNOWN 점유 금액과 가장 오래된 UNKNOWN 시각
- 원장 사용액과 Console 청구액을 대조합니다.
  - 원장이 계속 작게 나오면 같은 키를 다른 곳에서 쓰는지 확인하세요.
  - 원장이 계속 크게 나오면 예약 추정이 보수적이라는 뜻입니다(정상).
- 원장을 백업합니다(8절). 파일을 직접 복사하지 말고 `backup` 명령을 쓰세요.

## 8. 백업과 복구

**백업 (서버 실행 중에도 가능)**
```
PYTHONPATH=src python scripts/web_budget_ledger.py backup /var/data/ctrec/backups/ledger-YYYYMMDD.db
```
- SQLite 온라인 백업 API로 트랜잭션 단위의 일관된 사본을 만듭니다. 파일을 직접 복사하면 쓰는 도중의 사본이 될 수 있어 쓰지 않습니다.
- 손상된 원장은 백업하지 않습니다. 기존 백업 파일은 덮어쓰지 않습니다.
- 백업 옆에 체크섬 파일(`<백업>.sha256`)을 만들고, 사본의 무결성(`integrity_check`)과 스키마를 확인합니다.
- 백업은 같은 디스크 말고 다른 곳에도 보관하세요(디스크 자체 장애 대비).
- Render 디스크 스냅샷은 디스크 전체를 되돌립니다. Render 문서는 데이터베이스 복구에 디스크 스냅샷 대신 논리 백업을 권합니다.

**검증**
```
PYTHONPATH=src python scripts/web_budget_ledger.py verify <백업 파일>
```

**복구 (서버를 멈춘 뒤에만, 명시적 명령으로만)**
```
PYTHONPATH=src python scripts/web_budget_ledger.py restore <백업 파일> --confirm
```
- `--confirm`이 없으면 아무것도 하지 않습니다.
- 다음 백업은 거부합니다:
  - 체크섬 파일이 없거나 맞지 않음
  - SQLite 무결성 실패
  - 스키마 불일치
  - 원장이 아닌 파일
- 서버가 원장을 쓰는 중(원장 잠금을 쥔 프로세스가 있음)이면 거부합니다.
- 기존 원장은 지우지 않고 `<원장>.before-restore-<시각>`으로 보관합니다. 행을 지우는 명령은 없습니다.
- 백업 시점에 진행 중이던 예약은 UNKNOWN(`restored_from_backup`)으로 바꿉니다. 기존 UNKNOWN은 그대로 둡니다.
- **주의:** 백업 시점 이후의 예약·정산은 복구한 원장에 없습니다. 그동안 쓴 금액이 공용 한도에서 빠집니다.
  - 복구 뒤에는 Console 청구 내역으로 그 기간 사용액을 확인하세요.
  - 필요하면 그 기간의 공용 한도를 낮춰 재시작하세요.

## 9. 모델·가격 변경

**확인한 공식 가격 (2026-10-15, https://platform.claude.com/docs/en/about-claude/pricing)**

| 항목 (`claude-sonnet-5`, USD / 100만 토큰) | 값 |
|---|---|
| 기본 입력 | $2 |
| 5분 캐시 쓰기 | $2.50 |
| 1시간 캐시 쓰기 | $4 (웹은 쓰지 않음, 응답에 있으면 UNKNOWN) |
| 캐시 읽기 | $0.20 |
| 출력 | $10 |
| 긴 입력 | 1M 문맥 전체 같은 단가 (가격표 기준) |
| 지역 지정 추론 `inference_geo: "us"` | 1.1배 (웹은 보내지 않음, 응답에 있으면 UNKNOWN) |
| Batch | 50% 할인 (웹은 쓰지 않음) |

- 가격표 각주: $2/$10은 출시 때 한시 가격이었으나 이제 표준 가격이며, 2026-09-01로 예정됐던 인상은 시행되지 않는다고 적혀 있습니다.
- 이 표는 문서로 확인한 값입니다. 실제 API 호출·청구로 확인하지 않았습니다. 이 API 키로 모델을 실제로 쓸 수 있는지도 확인하지 않았습니다.
- `scripts/check_web_deployment.py`는 설정 단가가 이 표보다 낮으면 BLOCKED, 높으면 WARNING으로 표시합니다.
- 배포 직전에 가격표를 다시 확인하고, 확인한 사람만 `CTREC_WEB_PRICE_CONFIRMED=1`로 바꾸세요. 예시 파일(`deploy/web-enforce.env.example`)은 일부러 0입니다.

**변경 절차**
- 단가는 `config.MODEL` 하나에만 적용됩니다. 응답 모델이 다르면 정산하지 않고 UNKNOWN으로 남깁니다.
- 가격표가 바뀌거나 모델을 바꾸면 다음 순서로 합니다.
  1. 공식 가격표를 확인합니다.
  2. `CTREC_WEB_PRICE_*`와 `CTREC_WEB_PRICE_MODEL`을 갱신합니다.
  3. `CTREC_WEB_PRICE_SOURCE`에 확인 날짜·출처를 적습니다.
  4. 재배포합니다.
- 이미 정산된 행은 다시 계산하지 않습니다.
- 모델 변경은 이번 단계 범위 밖입니다. 현재 모델은 `claude-sonnet-5`입니다.

## 10. Render 배포 전 확인 목록 (enforce)

- [ ] `scripts/check_web_deployment.py --render-yaml <enforce 블루프린트> --env-file <값 파일>`에서 BLOCKED 0. UNVERIFIED 항목은 직접 확인
- [ ] 웹 전용 API 키를 쓰고, Console에서 키·워크스페이스 지출 한도를 설정함
- [ ] 영속 디스크를 붙이고 `CTREC_WEB_BUDGET_LEDGER`를 그 마운트 경로 아래로 지정함
- [ ] 배포 뒤 Render Shell에서 `init`으로 원장을 만들고 `check_web_deployment.py --process-env`로 확인
- [ ] 인스턴스 수 1 (자동 확장 끔), `CTREC_WEB_BUDGET_SINGLE_INSTANCE=1`
- [ ] 2절의 필수 변수를 모두 설정하고, 공식 가격표를 확인한 뒤 `CTREC_WEB_PRICE_CONFIRMED=1`, `CTREC_WEB_PRICE_SOURCE` 기록
- [ ] `CTREC_FALLBACKS=0` (Phase 6-4부터 기본 `render.yaml`도 0)
- [ ] uvicorn `--workers 1` (WEB_CONCURRENCY를 따르지 않도록 명시)
- [ ] `CTREC_WEB_METERING=enforce`
- [ ] 배포 뒤 `summary`로 원장이 열리는지 확인함. 시작 거부(503)가 나오면 서버 로그의 설정 문제 코드(`missing:…`, `unconfirmed:…`, `ledger_missing` 등)를 확인
- [ ] 재배포 전후에 `summary`를 비교해 원장이 남아 있는지 확인함 (디스크 영속성의 실제 확인)
- [ ] 백업 위치와 주기, UNKNOWN 정산 담당자와 점검 주기를 정함

이 조건 중 하나라도 충족할 수 없으면 웹에서 유료 호출을 켜지 마세요.
