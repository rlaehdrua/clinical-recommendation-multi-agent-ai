# 웹 실행 보안·개인정보·자원 관리 (Phase 2.5)

대상: `src/ctrec/web/` (FastAPI `app.py`, `access.py`, `retention.py`, 화면 `static/index.html`)

## 1. 실행별 접근 권한

| 항목 | 내용 |
|---|---|
| 권한 단위 | 실행(run) 하나 = 그 실행을 만든 브라우저 |
| 발급 | `POST /api/runs` 때 실행마다 새 256비트 토큰(`secrets.token_urlsafe(32)`) |
| 전달 | 쿠키 `ctrec_run`: `HttpOnly`, `SameSite=Strict`, `Path=/api/runs/<run_id>`, `Max-Age`=실행 시간 제한+보관 기간, `Secure`(HTTPS일 때) |
| 서버 보관 | 토큰의 SHA-256만 실행 정보(메모리)에 보관. 평문 저장 없음, 실행 기록이 지워지면 무효 |
| 검증 | `GET /api/runs/{id}`, `/events`, `POST /answers`, `/cancel`에서 쿠키 해시를 상수 시간 비교 |
| 실패 응답 | 없는 실행과 같은 404 `실행을 찾을 수 없거나 접근 권한이 없습니다.` (존재 여부 비노출) |

- 실행 ID는 식별자일 뿐 권한이 아닙니다(그래도 128비트 난수).
- 쿠키 경로를 실행마다 다르게 두어 여러 탭·여러 실행의 토큰이 섞이지 않습니다. 다른 실행의 토큰은 그 실행 경로에서 맞지 않아 쓸 수 없습니다.
- EventSource는 같은 출처 쿠키를 자동으로 보내므로 토큰을 URL·헤더에 넣을 필요가 없습니다.
- 새로고침하면 화면은 탭의 `sessionStorage`에 둔 실행 ID로 `GET /api/runs/{id}`를 확인한 뒤 이벤트를 처음부터 다시 받습니다. 토큰은 `localStorage`·`sessionStorage`에 두지 않으며, 스크립트도 토큰을 읽을 수 없습니다.

`Secure` 속성(`CTREC_COOKIE_SECURE`):
- `auto`(기본): 서버가 실제로 HTTPS로 받은 요청일 때만 켭니다(`request.url.scheme`).
- localhost 개발(http)에서는 꺼집니다.
- `1`이면 항상 켜고, `0`이면 항상 끕니다.

### HTTPS·프록시 신뢰 경계

- **헤더 신뢰:**
  - 앱은 `X-Forwarded-Proto`·`X-Forwarded-For`를 직접 읽지 않습니다.
  - 요청의 scheme과 클라이언트 주소는 uvicorn 프록시 미들웨어가 신뢰하는 프록시(`--forwarded-allow-ips`, 기본 `127.0.0.1`)에서 온 헤더만 반영한 값입니다.
  - 그래서 클라이언트가 임의로 보낸 `X-Forwarded-Proto: https`는 Secure 여부나 HTTPS 판정에 영향을 주지 않습니다.
- **전송 정책(가장 바깥 미들웨어, 상태 확인 `/healthz`만 예외):**
  - HTTPS는 허용합니다. HTTPS 응답에는 `Strict-Transport-Security: max-age=31536000`을 붙입니다.
  - 같은 컴퓨터(루프백 127.0.0.1, ::1)의 평문 HTTP는 개발용으로 허용합니다(`python -m ctrec.web` 기본).
  - 그 밖의 평문 HTTP는 403 `HTTPS로 접속하세요.`로 거절합니다. 운영에서 프록시 설정이 빠져 HTTPS가 인식되지 않으면 조용히 평문으로 동작하지 않고 실패합니다.
  - `CTREC_ALLOW_INSECURE_HTTP=1`: 위험을 이해한 경우에만 평문 HTTP를 허용합니다(사내망 시연 등). 이때 쿠키는 Secure가 아닙니다.
  - `CTREC_REQUIRE_HTTPS=1`(운영): HTTPS만 허용하고, 루프백 예외와 `CTREC_ALLOW_INSECURE_HTTP`를 무시합니다. 모든 프록시를 신뢰하는 배포에서는 `X-Forwarded-For`로 루프백 주소를 꾸밀 수 있기 때문입니다.
- **Render 배포(`render.yaml`):**
  - `--proxy-headers --forwarded-allow-ips '*'`, `CTREC_REQUIRE_HTTPS=1`, `CTREC_COOKIE_SECURE=1`을 씁니다.
  - `'*'`는 서비스가 Render 프록시를 통해서만 접근된다는 전제에서만 안전합니다. 다른 플랫폼에서는 실제 프록시 주소로 좁혀야 합니다.

CSRF·Origin:
- 상태를 바꾸는 POST(`/api/runs`, `/answers`, `/cancel`)는 `X-CTREC-Request: 1` 헤더를 요구합니다. 다른 사이트의 폼이나 단순 요청은 이 헤더를 보낼 수 없고, 스크립트가 보내려면 CORS 사전 요청이 필요한데 서버가 허용하지 않습니다.
- `Origin`이 있으면 `Host`와 같아야 하며, `Origin: null`은 거절합니다.
- `SameSite=Strict` 쿠키는 다른 사이트에서 시작한 요청에 붙지 않습니다.

Basic 인증(`CTREC_PASSWORD`)과의 관계:
- 서비스 입장 제한(공용 비밀번호)은 그대로 먼저 적용됩니다.
- 실행 소유권은 그 위에 별도로 적용되므로, 같은 비밀번호를 쓰는 사람끼리도 서로의 실행을 볼 수 없습니다.

### 남는 한계 (정식 사용자 인증이 아님)

1. 브라우저 단위 권한이라 사용자 신원을 확인하지 않습니다. 같은 브라우저 프로필을 쓰는 사람은 같은 소유자입니다.
2. 쿠키를 잃으면(쿠키 삭제, 다른 기기, 시크릿 창 종료) 실행에 다시 접근할 수 없습니다. 복구 수단이 없습니다.
3. 화면에 XSS가 생기면 스크립트가 같은 출처 요청을 보낼 수 있습니다. HttpOnly라 토큰 자체는 읽히지 않지만 권한은 행사될 수 있습니다. 현재 화면은 모든 값을 `esc()`로 이스케이프하고, 보고서는 DOMPurify로 정리합니다(CDN).
4. 같은 브라우저 실행 수 제한은 `ctrec_client` 쿠키 기준이라 쿠키를 지우면 우회됩니다. 전체 동시 실행 상한(`CTREC_WEB_MAX_RUNS`)은 그대로 적용됩니다. IP 단위 제한은 없습니다.
5. 실행 상태가 서버 메모리에 있어 서버 재시작 시 사라집니다. 인스턴스가 여럿이면 동작하지 않습니다(`render.yaml`의 `numInstances: 1`).
6. 서버 접근 로그를 켜면(uvicorn 기본 `info`) 요청 경로의 실행 ID가 로그에 남습니다. 쿠키·본문은 남지 않습니다. 배포 명령은 `--log-level warning`입니다.

## 2. 환자 데이터 저장

### 이전 동작 (Phase 2.5 이전): 모든 웹 실행이 `outputs/web/<run_id>/`에 저장되고 지워지지 않음

| 파일 | 담긴 민감정보 |
|---|---|
| `result.json` | 환자 ID, 구조화 프로필(나이·성별·진단·병기·바이오마커·검사·투약·치료력과 원문 인용), 기준별 판정 근거(원문 인용), 질문·답변 전체, 판정 이력, 확보 시도 기록, 추천과 근거, 오류 문구(`repr`) |
| `report.md` | 결과 설명 보고서(환자 상태·판정 근거 서술) |
| `trace.jsonl` | 모든 실행 기록: 도구 호출 입력, 프로필 누락 항목, 질문·답변, 추천 문장, 오류 문구 원문 |

### 현재 정책

- 기본값: 저장하지 않습니다(`CTREC_WEB_SAVE_OUTPUTS` 미설정 또는 0). 질문·답변·추천은 메모리의 실행 기록과 SSE로만 전달됩니다. 실행 기록은 정상 완료 2시간 뒤(`CTREC_WEB_RUN_TTL_S`), 취소·오류·시간 초과 2분 뒤(`CTREC_WEB_ENDED_TTL_S`) 메모리에서 지웁니다.
- 선택 저장: `CTREC_WEB_SAVE_OUTPUTS=1`일 때만 위 세 파일과 표식 파일 `.ctrec-web-run`(run_id, 생성 시각)을 씁니다.
- 자동 삭제: `CTREC_WEB_OUTPUT_TTL_S`(기본 86400초, 최소 60초)가 지난 폴더를 실행 시작 시 최대 10분에 한 번 정리합니다.
- 삭제 안전장치:
  - `outputs/web/` 바로 아래에 있어야 합니다.
  - 이름이 실행 ID 형식(`YYYYMMDD-HHMMSS-<32자리 16진수>`)이어야 합니다.
  - 표식 파일의 `run_id`가 폴더 이름과 같아야 합니다.
  - 폴더·표식 파일·내부 어디에도 심볼릭 링크나 정션(reparse point)이 없어야 합니다.
  - 실제 경로의 부모가 `outputs/web`이어야 합니다.
  - 하나라도 맞지 않으면 지우지 않습니다.
- CLI·평가용 출력(`outputs/<환자>/` 등)은 이 정책과 무관합니다.
- 화면으로 보내는 오류 이벤트와 오류 실행 기록에는 예외 종류만 들어갑니다. 서버 로그에는 실행 종료 상태와 예외 종류만 남깁니다.

### 기존 파일 처리 (자동 삭제하지 않음)

이 정책 이전에 만든 폴더는 표식이 없어서(그리고 실행 ID 형식도 달라서) 자동 정리 대상이 아닙니다. 정리하려면 다음 절차를 따르세요.

1. 운영자가 `outputs/web/` 목록과 보존 필요 여부를 확인합니다.
2. 승인을 받은 뒤 해당 폴더를 수동으로 삭제합니다. 남겨야 하면 접근이 제한된 위치로 옮깁니다.
3. 배포 서버(Render)의 디스크는 재배포 시 초기화되지만, 그 전까지는 남아 있습니다.

## 3. 실행 취소·시간 제한·자원

상태 전이(실행 잠금 안에서만 바뀌고, 끝 상태는 한 번만 정해짐):

```
running ──취소 요청──▶ cancelling ──작업 스레드 종료──▶ cancelled
running ──결과 전송──▶ completed      running ──예외──▶ error      running ──시간 초과 확인──▶ timed_out
```

- **취소 API:** `POST /api/runs/{id}/cancel`(소유자, CSRF 확인). 대기 중인 질문을 즉시 해제하고, 이후 답변은 409로 거절합니다. 끝났거나 이미 취소 중이면 409입니다.
- **협력적 취소:** 스레드를 강제로 종료하지 않습니다. 다음 지점에서 취소와 시간 초과를 확인합니다.
  - 모든 LLM 호출 직전: `llm.create`의 `llm.guard()`. 실행별 contextvars라 같은 실행의 병렬 하위 스레드에도 적용됩니다.
  - 답변 대기 중: 0.2초 간격으로 확인합니다.
  - 답변 직후와 오케스트레이터 종료 후.
- **결과 덮어쓰기 방지:**
  - 결과 이벤트 전송과 `completed` 확정을 같은 잠금 구간에서 합니다.
  - 그 뒤에 들어온 취소 요청은 409이며 상태는 `completed`로 남습니다.
  - 취소가 먼저 확정되면(`cancelling`) 결과를 보내지 않고 `cancelled`로 끝납니다.
  - 따라서 `result` 이벤트가 있으면 `end.state`는 항상 `completed`이고, 없으면 `completed`가 아닙니다.
- **종료 이벤트:** 모든 경로에서 마지막 이벤트는 `end` 하나이며 `state`(completed / cancelled / timed_out / error)를 담습니다.
- **시간 제한:**
  - `CTREC_WEB_RUN_TIMEOUT_S`(기본 3600초): 실행 전체. 다음 확인 지점에서 `timed_out`으로 끝납니다.
  - `CTREC_WEB_ANSWER_TIMEOUT_S`(기본 1800초): 답변 대기. 넘으면 기존처럼 '모름'으로 진행합니다.
- **슬롯 회수:** 동시 실행 슬롯은 작업 스레드가 실제로 끝난 뒤(`done`)에만 반환합니다. 취소를 요청한 것만으로는 반환하지 않습니다.
- **요청 제한:**
  - 본문 256KB
  - 환자 정보 2만 자, 답변 2천 자, 답변 50개, 시험 ID 200개
  - 확인 질문 라운드는 설정값 이하
  - 같은 브라우저 동시 1개, 시간당 20개
  - 전체 보관 20개

### 취소 후에도 비용이 늘 수 있는 구간

1. **이미 보낸 API 호출:** 취소 시점에 진행 중인 호출(Matcher 병렬 호출은 최대 `CTREC_MAX_WORKERS`개)은 끝까지 진행되고 비용이 청구됩니다.
2. **SDK 내부 재시도:** 호출 하나 안의 재시도(`max_retries=4`)는 SDK 안에서 일어나므로 그 사이에는 확인하지 않습니다.
3. **멈춘 호출:** 응답이 오지 않는 호출은 SDK 시간 제한까지 스레드와 슬롯을 점유합니다. 시간 제한도 협력적이라 그 호출을 끊지 못합니다.
4. **ClinicalTrials.gov 검색:** 이 HTTP 요청(유료 아님)은 확인 지점이 없어, 진행 중이던 요청은 끝까지 갑니다.

## 4. 화면 보안 헤더·정적 자원 (Phase 6-2)

### 보안 헤더

모든 응답에 붙습니다. 거절 응답(403·401·413)도 포함되도록 가장 바깥 미들웨어에서 처리합니다.

| 헤더 | 값 | 이유 |
|---|---|---|
| Content-Security-Policy | `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'; manifest-src 'self'` | 같은 서버의 파일만 실행·적용합니다. 인라인 스크립트, 인라인 스타일, `eval`, 외부 출처는 허용하지 않습니다 |
| X-Content-Type-Options | `nosniff` | MIME 추측 방지 |
| Referrer-Policy | `no-referrer` | 외부 링크로 실행 주소가 새지 않음 |
| X-Frame-Options | `DENY` | 다른 사이트에 끼워 넣기 금지 (CSP `frame-ancestors`의 구형 브라우저 대응) |
| Cross-Origin-Opener-Policy | `same-origin` | 새 창과의 창 참조 분리 |

### 화면 구조

화면 코드는 세 파일로 나뉩니다: `index.html`(마크업), `app.css`, `app.js`.
- 인라인 `<script>`, `<style>`, `style=` 속성, `on*=` 이벤트 속성이 없습니다(테스트로 확인).
- 정적 파일은 `GET /static/<이름>`으로 제공합니다. 정해 둔 4개 파일만 열리며 경로 조작은 불가합니다.

### 외부 라이브러리

- CDN을 쓰지 않습니다. marked 12.0.2와 DOMPurify 3.4.16을 로컬에서 제공합니다.
- 버전, 라이선스, 원본 tarball sha512, 파일 sha256은 `src/ctrec/web/static/VENDOR.md`에 있고, 테스트가 sha256을 대조합니다.
- 웹폰트는 쓰지 않습니다.

### 외부 글 표시

- 질문, 근거, 시험 제목 등은 이스케이프한 뒤 글자로만 표시합니다.
- AI 보고서 마크다운은 두 겹으로 처리합니다.
  1. 안에 섞인 HTML을 해석하지 않고 글자로 출력합니다(marked 렌더러).
  2. 그 결과를 다시 DOMPurify로 정화합니다(`style` 제거).
- 외부 링크는 `https://*.clinicaltrials.gov`만 링크로 만들고 `rel="noopener noreferrer"`를 붙입니다.

### 오류 원문

화면으로 보내는 결과 사본에서 평가 오류(`evaluation_error`, 단계 실패 `errors`)는 **예외 이름만** 남깁니다. 예외 메시지에는 요청 내용이 섞일 수 있기 때문입니다. 판정 값과 서버 Session은 바꾸지 않습니다(`web/display.py`의 `redact_errors`).

### 한계

- Trusted Types(`require-trusted-types-for`)는 아직 적용하지 않았습니다. 화면이 `innerHTML`로 이스케이프된 템플릿을 넣기 때문입니다.
- 결과 사본에는 여전히 환자 원문 일부가 들어 있습니다(추출된 프로필, 근거 인용). 이는 실행 소유자에게만 전달됩니다.
