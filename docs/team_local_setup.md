# 팀원용 로컬 실행 가이드 (Phase 8)

저장소를 처음 받은 팀원 기준입니다. **Windows(PowerShell)를 먼저** 적고, macOS/Linux 명령은 그 아래에 둡니다.

> **Phase 9:** 다른 팀원 커밋(4ea12d6)의 객관식 질문·한국어 번역을 통합했습니다. 단계별 화면과 저장된 실행 다시 보기는 보안 문제로 보류했습니다(`docs/team_handoff.md` 4절).
>
> **먼저 알아 둘 것: 웹의 실제 AI 분석은 기본적으로 꺼져 있습니다.**
> - `.env`에 API 키를 넣어도 웹은 실제 Claude API를 호출하지 않습니다. 화면은 열리지만, 분석을 시작하면 "실제 AI 분석이 꺼져 있습니다"가 나옵니다(8절).
> - 이것은 비용·개인정보 안전장치이며 고장이 아닙니다.
> - API 키 없이 확인할 수 있는 방법은 9절에 있습니다.
> - **실제 환자 정보는 어떤 경로로도 넣지 마세요.**

## 1. 필요한 것

| 항목 | 버전·비고 |
|---|---|
| Python | **3.10 이상** (`pyproject.toml`). 이 저장소 테스트는 3.10.11에서 돌렸습니다. Render 설정은 3.13.5인데, 그 버전에서는 테스트하지 않았습니다 |
| Git | 아무 최신 버전 |
| 브라우저 | Edge 또는 Chrome (화면 확인, 브라우저 테스트용) |

## 2. 저장소 받기

```powershell
git clone https://github.com/seoyeon488/workseoyeon.git
cd workseoyeon
```

## 3. 가상환경

**Windows (PowerShell)**
```powershell
py -3.10 -m venv .venv            # 3.10 이상이면 됨 (py -3.12 등)
.\.venv\Scripts\Activate.ps1
```
- "스크립트를 실행할 수 없습니다" 오류가 나면, 현재 창에서만 허용합니다.
  ```powershell
  Set-ExecutionPolicy -Scope Process RemoteSigned
  ```

**macOS / Linux**
```bash
python3 -m venv ~/.venvs/ctrec && source ~/.venvs/ctrec/bin/activate
```
- macOS에서 프로젝트가 iCloud Drive 안에 있으면, 가상환경은 iCloud 밖(`~/.venvs`)에 만드세요(README 2절).

## 4. 의존성 설치

```powershell
pip install -r requirements.txt
pip install -e .                  # ctrec 패키지를 경로 설정 없이 import (PYTHONPATH=src 대신)
pip install playwright            # 선택: 브라우저 E2E 테스트용 (브라우저는 설치된 Edge/Chrome을 씀, 다운로드 없음)
```
- `requirements.txt`는 `anthropic>=1.13,<2`로 고정되어 있습니다.

## 5. 환경 변수 (`.env`)

```powershell
copy .env.example .env            # macOS/Linux: cp .env.example .env
```

`.env`는 git에 올라가지 않습니다(`.gitignore`). 기본값은 다음과 같습니다.

```
ANTHROPIC_API_KEY=            # 비워 둬도 화면·테스트는 동작. 넣어도 웹은 실제 호출하지 않음 (아래 주의)
CTREC_MODEL=claude-sonnet-5
CTREC_FALLBACKS=0
CTREC_WEB_METERING=record
CTREC_WEB_PAID_ENABLED=0
```

> **주의: CLI·스크립트에는 웹 같은 안전장치가 없습니다.** `.env`에 API 키가 있으면 다음 명령은 **실제 유료 호출을 합니다.**
> - `python -m ctrec run …`
> - 시험 기준 컴파일 스크립트(`scripts/compile_trials.py` 등)
>
> 승인된 작업이 아니면 `.env`의 API 키를 비워 두세요. 파일럿 실행기(`scripts/run_llm_pilot.py`)는 별도 승인 게이트가 있어 실제 호출을 하지 않습니다.

## 6. 서버 실행

```powershell
python -m ctrec.web               # 기본 http://127.0.0.1:8000
python -m ctrec.web --port 8080   # 포트 변경
```
- `pip install -e .`를 하지 않았다면 경로를 지정합니다.
  - PowerShell: `$env:PYTHONPATH="src"; python -m ctrec.web`
  - macOS/Linux: `PYTHONPATH=src python -m ctrec.web`
- 실제 분석이 꺼져 있으면 시작할 때 "실제 AI 분석: 꺼짐" 안내가 함께 나옵니다.

## 7. 브라우저 접속과 화면 확인

1. `http://127.0.0.1:8000`을 엽니다.
2. 맨 위에 "이 서버에서는 실제 AI 분석이 꺼져 있습니다" 안내가 보이면 기본 상태입니다.
3. 볼 수 있는 것:
   - 입력 화면: 직접 입력, 예시 사례(합성 데이터) 선택, 합성 예시 넣기, 입력 내용 확인
   - 고급 설정의 시험 목록(26개)
   - 안내 문구, 접근성(키보드 이동, 320px 화면)
4. "이 내용으로 분석 시작"을 누르면 분석은 시작되지 않고 안내만 나옵니다.
   - 실행이 만들어지지 않고, 환자 정보는 외부로 전송되지 않습니다.

## 8. 실제 AI 분석을 켜려면 (별도 승인 후에만)

웹 실제 분석을 켜는 데 필요한 조건입니다. 하나라도 빠지면 서버가 분석 시작을 거부합니다(503, 사유는 서버 로그에만).

1. `CTREC_WEB_PAID_ENABLED=1`
2. `CTREC_WEB_METERING=enforce` (record로는 불가)
3. enforce 설정 전체 (`docs/web_budget_operations.md` 2절)
   - 실행별·공용 한도
   - 가격 4종 + `CTREC_WEB_PRICE_MODEL`
   - 가격 확인 표시와 출처
   - 원장 경로 + 영속·단일 인스턴스 운영자 확인
4. 원장 초기화: `python scripts/web_budget_ledger.py init`
5. `CTREC_FALLBACKS=0`
6. `CTREC_PASSWORD` 12자 이상 (브라우저에 로그인 창이 뜸), `CTREC_ALLOW_INSECURE_HTTP`를 켜지 않음
7. `ANTHROPIC_API_KEY` (웹 전용 키 권장, Console에서 지출 한도 설정)

- 확인 명령: `python scripts/check_web_deployment.py --process-env`
- 실제 유료 실험은 파일럿 절차(`docs/llm_pilot_phase7.md`)를 먼저 따르는 것이 원칙입니다.

## 9. API 키 없이 확인할 수 있는 것 (실제 AI 응답이 아님)

**새로 만든 가짜 분석 기능은 없습니다.** 아래는 이미 있던 오프라인 경로이며, 결과는 결정적 대역(규칙 엔진 + 테스트용 가짜 응답)이라 실제 모델 품질과 무관합니다.

| 무엇을 보나 | 명령 | 2026-10-15 이 컴퓨터에서 |
|---|---|---|
| 웹 전체 흐름 (입력 → 질문 → 답변 → 재분석 → 결과·근거): 브라우저 자동 테스트, 화면은 보이지 않음 | `$env:CTREC_BROWSER_TESTS="1"; python -m pytest tests/test_web_browser.py -k full_journey` | 통과 |
| 객관식 질문·한국어 번역 화면 (테스트 대역) | `$env:CTREC_BROWSER_TESTS="1"; python -m pytest tests/test_web_browser.py -k choice_questions` | 통과 |
| 웹 흐름 (서버 API, 브라우저 없이) | `python -m pytest tests/test_web_integration.py -q` | 통과 |
| Decision Loop 질문 계획 시나리오 | `python scripts/decision_scenarios.py` | 실행됨 |
| 질문 계획 오프라인 비교 | `python scripts/eval_question_plans.py --cases M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1` | 실행됨 |
| 파일럿 실행 경로 리허설 (Fake SDK) | `$env:CTREC_FALLBACKS="0"; python scripts/run_llm_pilot.py --experiment A_pipeline --fake-sdk` | 3/3 성공 |
| 파일럿 실행 전 점검 | `$env:CTREC_FALLBACKS="0"; python scripts/pilot_preflight.py` | FAIL 0 (승인 전이라 LOCKED·PENDING) |
| 배포 설정 점검 | `python scripts/check_web_deployment.py` | BLOCKED 0 |

- 웹 화면에서 직접 "가짜 분석"을 돌리는 모드는 없습니다.
- 테스트용 대역(Fake LLM)은 테스트 코드에서만 켤 수 있고, 켜지면 화면 모델 표시가 "테스트 대역 (실제 AI 분석 아님)"으로 바뀝니다.
- 실제 분석이 꺼져 있을 때 자동으로 대역으로 바뀌지 않습니다.

## 10. 테스트 실행

```powershell
python -m pytest -q                                   # 오프라인 전체 (네트워크·유료 호출 없음, 약 3분)
$env:CTREC_BROWSER_TESTS="1"; python -m pytest tests/test_web_browser.py -q   # 브라우저 E2E (약 2분, Edge/Chrome + playwright)
```
- macOS/Linux: `CTREC_BROWSER_TESTS=1 python -m pytest tests/test_web_browser.py -q`
- `pip install -e .`를 하지 않았으면 앞에 `PYTHONPATH=src`를 붙입니다(PowerShell은 `$env:PYTHONPATH="src";`).
- 이 브랜치 기준 결과는 `docs/team_handoff.md`에 있습니다.

## 11. 서버 종료

서버를 띄운 터미널에서 `Ctrl+C`를 누릅니다.

## 12. 자주 발생하는 오류

| 증상 | 원인·해결 |
|---|---|
| 분석 시작 시 "실제 AI 분석이 꺼져 있습니다" | 정상 기본값(8절) |
| "실제 AI 분석에 필요한 서버 설정…이 갖춰지지 않아" (503) | 스위치는 켰지만 조건 미충족. 서버 로그의 `web analysis blocked: [...]` 코드를 확인 |
| `ModuleNotFoundError: No module named 'ctrec'` | `pip install -e .` 또는 `PYTHONPATH=src`. macOS는 iCloud 밖 가상환경 |
| `Activate.ps1`을 실행할 수 없음 | `Set-ExecutionPolicy -Scope Process RemoteSigned` |
| `UnicodeEncodeError: 'cp949'` | Phase 8에서 웹 시작 문구는 고침. 스크립트 출력이 깨지면 `$env:PYTHONIOENCODING="utf-8"` |
| 포트가 이미 사용 중 | `python -m ctrec.web --port 8080` |
| 브라우저 테스트가 모두 건너뜀(skipped) | `CTREC_BROWSER_TESTS=1`이 없음. 또는 `playwright` 미설치, Edge/Chrome 없음 |
| 다른 컴퓨터에서 접속하면 403 | 루프백 밖의 평문 HTTP는 거절(보안). 로컬에서 `127.0.0.1`로 접속 |
| 같은 브라우저에서 새 분석이 429 | 진행 중인 분석이 있음. 화면의 "진행 중인 분석 이어 보기"로 이어 보거나 취소 |
