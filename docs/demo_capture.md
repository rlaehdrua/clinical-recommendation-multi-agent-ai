# 시연 화면 캡처 (`scripts/capture_demo_screens.py`)

중간발표용 웹 화면 7단계를 실제 브라우저로 진행하며 PNG로 남깁니다. `ad3a222`(integration/midterm-1026) 화면 기준으로 새로 작성한 스크립트입니다.
이전에 보고된 같은 이름의 파일(삭제된 `#review` 버튼을 기다리던 버전)은 저장소·브랜치 이력 어디에도 없어 복구가 아니라 새로 만들었습니다.
그 파일이 나중에 발견되면 이 파일을 덮어쓰지 말고 비교한 뒤 통합하세요.

## 실행

```bash
pip install -e ".[dev,web]" playwright   # 브라우저는 설치된 Edge/Chrome 사용 (playwright install 불필요)
python scripts/capture_demo_screens.py                     # -> outputs/demo_screens/
python scripts/capture_demo_screens.py --out outputs/demo_screens_2 --patient SYN-DEMO-B --headed
```

출력 폴더가 비어 있지 않으면 덮어쓰지 않고 종료 코드 2로 끝납니다. `outputs/`는 git에 올라가지 않습니다.

## 생성 파일 (`outputs/demo_screens/`)

| 파일 | 화면 |
|---|---|
| `01_input.png` | 첫 화면 |
| `02_patient.png` | 합성 환자 선택 (기본 `SYN-DEMO-A`, `SYN-`으로 시작하는 환자만 허용) |
| `03_trials.png` | 고급 설정에서 시험 직접 선택 (`DEMO-LUNG-001`, `NCT04063540`) |
| `04_analysis.png` | "분석 시작" 직후 진행 화면 (확인 단계 없이 바로 시작) |
| `05_question.png` | 추가 질문과 답변 입력 |
| `06_result.png`, `06_result_detail.png` | 추천 결과 목록, 시험 상세·조건별 근거 |
| `07_inspector.png`, `07_inspector_page.png` | 개발자 진단 패널(Decision Loop), 패널이 있는 화면 전체 |
| `capture_meta.json` | 환자·시험·판정 상태·파일 목록 |

## 보장하는 것

- **오프라인 전용**: LLM은 `tests/web_harness.FakeLLM`(테스트와 같은 합성 출력)으로 바꾸고 실제 Anthropic 클라이언트 생성을 막습니다.
  `ANTHROPIC_API_KEY`·`ANTHROPIC_AUTH_TOKEN`도 프로세스에서 지웁니다. 브라우저의 외부 출처 요청은 모두 차단하고, 차단된 요청이 있으면 실패로 끝냅니다.
- **진단 패널은 `?diagnostic=1`에서만**: 01~06은 기본 주소에서 찍고, 이때 진단 패널이 보이거나 진단 이벤트가 생기면 실패합니다.
  07은 같은 입력으로 `?diagnostic=1`에서 한 번 더 실행해 찍고, 두 실행의 판정(`match_status`)이 다르면 실패합니다.
- **판정·추천 로직을 바꾸지 않음**: 웹 서버·Session·Matcher 코드는 그대로 쓰고, 결과 저장 위치만 임시 폴더로 돌립니다.
  04 화면은 대역 LLM이 즉시 답해 진행 화면이 지나가 버리므로, 질문 만들기 직전에 대역을 잠시 세웠다가(`FakeLLM.block_on`) 찍은 뒤 풀어 줍니다. 출력 내용은 같습니다.
- 화면에 나오는 결과는 합성 대역 출력이며 실제 AI 분석 결과가 아닙니다(화면 상단에 "테스트 대역 (실제 AI 분석 아님)" 표시).
