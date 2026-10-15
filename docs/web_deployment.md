# 웹 배포 구성과 사전 점검 (Phase 6-4)

웹서비스를 Render에 배포할 때의 두 구성과 배포 전 점검 방법입니다.
- 비용 원장 운영(UNKNOWN 정산, 백업·복구): `web_budget_operations.md`
- 보안 설정: `web_security.md`

**이 문서는 배포를 승인하지 않습니다.** 실제 배포와 유료 API 호출은 별도 승인 뒤에만 합니다.

## 1. 두 가지 배포 구성

| | A. 기본 배포 구성 (record) | B. enforce 운영 구성 |
|---|---|---|
| 파일 | `render.yaml` | `deploy/render.enforce.yaml` (예시), `deploy/web-enforce.env.example` |
| `CTREC_WEB_METERING` | `record` | `enforce` |
| 비용 강제 차단 | 없음. 사용량 기록만 | 실행별·공용 한도로 호출 전 차단 (절대 상한은 아님) |
| 영속 저장소 | 필요 없음 (무료 플랜 가능) | 필수: 유료 인스턴스 + 영속 디스크 |
| 인스턴스·워커 | 1 / 1 | 1 / 1 (원장 잠금으로 두 번째 프로세스 거부) |
| 대체 모델 | `CTREC_FALLBACKS=0` | `CTREC_FALLBACKS=0` (1이면 시작 거부) |
| 가격 확인 | 필요 없음 | `CTREC_WEB_PRICE_*`, `CTREC_WEB_PRICE_MODEL`, 확인 표시·출처 필수 |
| 원장 | 없음 | 운영자가 `init`으로 한 번 생성 |

- **A는 유료 호출 허용을 뜻하지 않습니다.**
  - A 구성에 API 키를 넣으면 비용 상한 없이 호출됩니다.
  - 그래서 접속 비밀번호(`CTREC_PASSWORD`)와 Console의 키·워크스페이스 지출 한도를 반드시 함께 설정하세요.
  - 점검 도구는 "API 키 있음 + 비밀번호 없음"을 BLOCKED로 봅니다.
- 기본 구성을 enforce로 바꾸지 않았습니다. enforce의 강화된 사전 조건은 운영자가 `CTREC_WEB_METERING=enforce`를 고를 때만 검사합니다.

## 2. Render 설정 조사 결과

조사 대상:
- `render.yaml`, `requirements.txt`, `pyproject.toml`
- `src/ctrec/web/app.py`, `access.py`, `retention.py`, `budget.py`, `web_metering.py`, `config.py`

Render·uvicorn의 동작은 공식 문서를 기준으로 적었습니다(2026-10-15 확인). 실제 Render 서비스는 조회하지 않았습니다.

| 항목 | 현재 상태 | 근거·조치 |
|---|---|---|
| 인스턴스 수 | `numInstances: 1` | 실행 상태·소유권·SSE 이벤트가 프로세스 메모리(`RUNS`)에 있음. 2개 이상이면 다른 인스턴스로 간 요청이 실행을 찾지 못함 |
| 워커 수 | **수정:** `--workers 1` 명시 | uvicorn은 `--workers`가 없으면 `WEB_CONCURRENCY`를 따릅니다. Render는 새 서비스에 인스턴스 CPU 기준 값을 넣을 수 있습니다(예: Pro 2). 워커가 2개면 인스턴스 2개와 같은 문제가 생깁니다 |
| 파일시스템 영속성 | 무료 플랜: 없음 | Render 문서: 디스크 마운트 경로 밖의 파일은 재배포·재시작 때 사라짐. 영속 디스크는 유료 인스턴스에만 붙음 |
| 비용 원장 경로 | A: 없음 / B: `/var/data/ctrec/web-ledger.db` 예시 | 디스크 마운트 경로 아래여야 함. 점검 도구가 경로를 대조. 실제 마운트 여부는 서버 안에서만 확인 가능 |
| 재시작 시 데이터 | 실행 기록은 사라짐 (메모리) | 진행 중 분석은 재시작 때 끊깁니다. 화면은 재연결 실패 뒤 "기록 없음"으로 끝납니다. enforce 원장은 디스크에 남고, 진행 중 예약은 UNKNOWN이 됩니다 |
| 환경 변수 누락 | A: 기본값으로 동작 / B: 분석 시작 거부(503) | enforce는 설정 하나라도 빠지면 거부하며 `record`로 내려가지 않음 |
| health check | `/healthz` → `{"ok": true}` | 생존 확인만 함. 원장 상태를 넣지 않음: 원장 장애 때 Render가 재시작을 반복하지 않고, 분석 시작만 503으로 막도록 |
| 포트 바인딩 | `--host 0.0.0.0 --port $PORT` | Render가 `PORT`를 줌 |
| HTTPS·프록시 | `CTREC_REQUIRE_HTTPS=1`, `CTREC_COOKIE_SECURE=1`, `--proxy-headers --forwarded-allow-ips '*'` | `'*'`는 서비스가 Render 프록시 뒤에서만 접근된다는 전제에서만 안전 (`web_security.md`) |
| API 키·비밀번호 | `sync: false` (대시보드 입력) | 블루프린트·저장소에 값 없음. 점검 도구가 추적 파일의 키 형태 문자열을 검사 |
| 출력 파일 저장 | 기본 꺼짐 (`CTREC_WEB_SAVE_OUTPUTS`) | 켜면 환자 입력이 담긴 파일이 디스크에 남음. 점검 도구가 WARNING |
| 비정상 종료 후 복구 | 메모리 실행은 사라짐 | enforce 원장: OS 잠금이 풀려 재시작 가능. 남은 예약은 UNKNOWN으로 점유 유지 |
| SDK 버전 | **수정:** `anthropic>=1.13,<2` | 비용 통제가 `usage.service_tier`·`cache_creation`·`with_options(max_retries=0)`에 의존. 주 버전이 바뀌면 동작이 달라질 수 있음 |
| Python 버전 | Render 3.13.5 / 테스트 3.10.11 | **미검증:** 배포 버전에서 테스트를 돌리지 않음. 점검 도구가 WARNING |

## 3. 대체 모델(CTREC_FALLBACKS) 충돌 해소

**무엇이었나**
- 이전 `render.yaml`은 `CTREC_FALLBACKS="1"`이었습니다.
- 이 값이면 `llm.create`가 beta `server-side-fallback` 요청(`fallbacks: "default"`)을 보냅니다. 모델이 안전 분류로 거절하면, 서버가 다른 모델로 다시 실행합니다.
- enforce는 이 설정을 거부합니다. 대체 모델과 그 단가를 응답 전에 알 수 없기 때문입니다.

**결정: 두 구성 모두 `CTREC_FALLBACKS=0`**

| 이유 | 설명 |
|---|---|
| 판정 품질 | 대체 모델은 이 프로젝트의 Matcher·추천 정책을 검증한 모델(`claude-sonnet-5`)이 아닙니다. 대체 실행의 판정은 검증되지 않은 판정이 됩니다 |
| 비용 | 대체 모델의 단가를 미리 알 수 없습니다. 기록 모드에서도 비용이 UNKNOWN이 됩니다 |
| 일관성 | Phase 5 파일럿도 `CTREC_FALLBACKS=0`을 요구했습니다. 웹과 파일럿의 호출 정책이 같아집니다 |

**끄면 생기는 영향**

| 거절이 일어난 단계 | 끈 뒤 동작 | 사용자에게 보이는 것 |
|---|---|---|
| Matcher (시험 하나) | 그 시험만 `RefusalError`로 실패하고 나머지는 계속 | 해당 시험이 "평가 실패"(부적격 아님), 오류 종류만 표시 |
| 환자 정보 분석, 추천, 결과 설명, 오케스트레이터 | 단계 실패 → 오케스트레이터 마무리 단계에서도 실패하면 실행 오류 | "오류" 종료. 완료·추천으로 보이지 않음 |
| 질문 생성 | 도구 오류로 오케스트레이터에 전달 | 질문 없이 진행되거나 실행 오류 (오케스트레이터 판단) |

- 의료 문장의 거절은 드뭅니다(README). 그러나 빈도는 실제 호출로 측정한 적이 없습니다(미검증).
- Bedrock·Vertex 등 다른 플랫폼은 원래 `CTREC_FALLBACKS=0`이 필요했습니다(README).
- 대체 모델을 다시 켜려면 두 가지가 필요합니다: 대체 모델의 판정 검증, 대체 모델 단가를 원장에 반영하는 설계. 이번 단계 범위 밖입니다.

## 4. 배포 사전 점검 도구

```
PYTHONPATH=src python scripts/check_web_deployment.py                      # A: render.yaml 기준
PYTHONPATH=src python scripts/check_web_deployment.py --render-yaml deploy/render.enforce.yaml --env-file <값 파일>
PYTHONPATH=src python scripts/check_web_deployment.py --process-env --probe  # 서버 환경 안(예: Render Shell)
```
- 결과는 네 가지입니다.
  - PASS: 확인됨
  - WARNING: 배포는 되지만 위험
  - BLOCKED: 이대로 배포하면 안 됨
  - UNVERIFIED: 이 도구로 확인할 수 없음
- BLOCKED가 있으면 종료 코드 2입니다.
- Windows 콘솔에서 한글이 깨지면 `PYTHONIOENCODING=utf-8`을 붙이세요.

**검사 항목**
- 인스턴스·워커, 포트, 프록시, 로그 수준, health check
- 플랜·디스크·원장 경로(enforce)
- 예산 모드, 모델 ID(확인된 가격표에 있는지), 대체 모델 충돌
- API 키·비밀번호 설정 여부(값은 출력하지 않음), HTTPS·쿠키, 출력 파일 저장
- Python 버전
- enforce 필수 설정 전체(`budget.load_config`와 같은 검사)
- 설정 단가와 공식 가격 비교
- 원장 디렉터리 쓰기 권한, 원장 무결성(읽기 전용 검사)과 UNKNOWN 건수
- 블루프린트·추적 파일의 비밀값, SDK 버전 고정

**항상 UNVERIFIED인 것 (이 도구가 추정하지 않음)**
- 실제 디스크 마운트와 재배포 뒤 보존
- 대시보드에만 있는 비밀값(`--env-file`·`--process-env`로 주지 않으면)
- 이 API 키로 모델을 실제 호출할 수 있는지
- 가격표의 최신성

현재 저장소 기준 결과 (2026-10-15, 이 컴퓨터):
- A(`render.yaml`): PASS 14 / WARNING 2 / BLOCKED 0 / UNVERIFIED 3
  - WARNING: 프록시 `'*'` 전제, Python 버전 차이
  - UNVERIFIED: 모델 접근, API 키, 비밀번호
- B(예시 파일 그대로): BLOCKED 1 (`unconfirmed:CTREC_WEB_PRICE`) — 가격 확인 표시를 일부러 0으로 두었기 때문입니다.

## 5. 배포 절차

**A. 기본 구성 (record)**
1. `check_web_deployment.py`에서 BLOCKED 0을 확인합니다.
2. 대시보드에 `ANTHROPIC_API_KEY`(웹 전용 키)와 `CTREC_PASSWORD`(12자 이상)를 넣습니다. 둘 다 `--env-file`로 미리 점검할 수 있습니다.
3. Console에서 키·워크스페이스 지출 한도를 설정합니다. A에는 서버 쪽 비용 상한이 없습니다.
4. 배포 뒤 `/healthz`와 첫 화면을 확인합니다.

**B. enforce 구성**
1. `web_budget_operations.md` 10절의 확인 목록을 모두 끝냅니다.
2. `deploy/render.enforce.yaml`의 플랜 ID·디스크 크기를 현재 Render 값으로 확인한 뒤 `render.yaml`로 옮깁니다(별도 승인).
3. 배포 뒤 Render Shell에서 원장을 만들고 확인합니다.
   ```
   PYTHONPATH=src python scripts/web_budget_ledger.py init
   PYTHONPATH=src python scripts/check_web_deployment.py --process-env --probe
   ```
4. 재배포를 한 번 한 뒤 `summary`가 같은지 비교해 디스크 영속성을 확인합니다.

## 6. 탭을 닫았다 다시 연 경우 (Phase 6-6)

- 실행 ID는 탭 저장소(sessionStorage)에만 둡니다. 그래서 탭을 닫으면 사라집니다.
- 같은 브라우저는 동시에 실행 1개만 할 수 있습니다. 그래서 전에는 탭을 닫으면 진행 중 분석으로 돌아갈 수도 없고, 그 분석이 끝날 때까지(최대 1시간) 새 분석도 시작할 수 없었습니다.
- 새 API `GET /api/runs/active`를 추가했습니다.
  - 이 브라우저(클라이언트 쿠키)가 시작한 **진행 중** 실행의 ID만 돌려줍니다.
  - 끝난 실행은 알려 주지 않습니다.
  - 내용 접근에는 여전히 실행별 소유권 쿠키가 필요합니다.
- 화면 동작:
  - 새 탭에서 "진행 중인 분석 이어 보기" 버튼을 보여 줍니다.
  - 버튼을 눌러야만 이어 봅니다. 공용 컴퓨터에서 자동으로 열리지 않게 하기 위해서입니다.
  - 새 분석이 429로 거절되면 같은 버튼을 보여 줍니다.
