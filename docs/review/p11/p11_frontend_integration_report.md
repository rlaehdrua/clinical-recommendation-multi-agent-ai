# P11 프론트엔드 통합 보고서

작성 2026-10-25. push·배포·유료 API 호출·검수 승인·Frozen Store 수정·다른 worktree 변경은 하지 않았습니다.
`git fetch origin`(읽기)만 원격에 접근했습니다.

## A. 프론트엔드 소스

| 후보 | HEAD | 기준(95117dc) 포함 여부 | 내용 |
|---|---|---|---|
| `origin/feat/user-frontend` | 24faa60 | **이미 포함** (5cce805 병합 경유) | 토스 디자인 언어 화면 개편, 예시 사례 한국어·핵심 요약 표시 |
| `origin/main` (fetch로 갱신: 5cce805 -> 9fdffc4) | 9fdffc4 | 미포함 (커밋 2개) | 팀원 ehdroal, 2026-10-25 |
| 그 밖의 로컬·원격 브랜치, 모든 worktree 미커밋 | - | - | 기준에 없는 웹 변경 없음 (통합 후보 worktree의 `web/app.py` 미커밋은 P8 패치 05로 이미 기준에 있음) |

`origin/main`의 미반영 커밋 2개:

| 커밋 | 종류 | 파일 | 처리 |
|---|---|---|---|
| 9fdffc4 "프로젝트 이름 Trialog와 로고, 입력 화면 정리" | 프론트엔드 | `web/static/{index.html,app.js,app.css,logo.svg}`, `web/app.py`(정적 파일 허용 목록 1줄), `tests/test_web_browser.py`, `docs/web_user_guide.md`, README | **통합** (cherry-pick -x, 작성자 보존) |
| b11ec29 "확인 질문에 답한 기준이 '정보 부족'으로 남지 않게 (매칭)" | 백엔드 판정 로직 | `agents/matcher.py`·`pipeline.py`(보호 파일), `tests/test_answer_evidence.py`, README | **보류** (B절) |

## B. 충돌

| 구분 | 건수 | 파일 | 해결 |
|---|---|---|---|
| Git 텍스트 충돌 | 2 | `README.md` (9fdffc4 반영), `agents/matcher.py` 3구간 (b11ec29 시험 병합) | README: Trialog 변경 기록만 반영하고, 보류한 b11ec29 기록 대신 보류 안내 1줄. matcher: 시험 병합에서는 양쪽 유지로 풀었으나 b11ec29 전체를 보류해 통합 브랜치에는 없음 |
| 의미적 충돌 | 2 | b11ec29 vs 재설계 Q&A 안전 정책 / b11ec29 vs 비용 반올림 테스트 | 보류 (아래) |
| 독립 변경 | 8파일 | 9fdffc4 | 그대로 반영 |

**b11ec29 보류 사유** (스크래치 clone에서 `origin/main` 시험 병합 + 양쪽 유지 해결 후 전체 테스트로 확인):
1. `_enforce_quote` 보완: LLM 인용이 원문에 없어도 그 기준을 겨냥한 답이 원문에 있으면 **답 내용(극성)과 관계없이** 판정을 인정하고 근거를
   문답으로 바꿈. 기준의 E9 직접성 검사는 출처 연결이 확인되지 않은 짧은 답을 확정 근거로 쓰지 않으므로, 팀원 테스트
   `test_answer_evidence::test_paraphrased_quote_accepted_when_rule_was_asked_and_answered`가 legacy에서 실패. 통과시키려면 E9에 qa_log를
   넘겨야 하는데, 그러면 서로 다른 질문의 상충 답 3건이 잘못 확정되는 경로가 열림(P9 보고서 D절) -> Q&A 안전성(백엔드 우선)과 양립 불가
2. `_answers_block`(Matcher 프롬프트에 답변 원문): 재설계에서 제거한 경로(P8·P9 qalink 'Matcher 프롬프트 답변 블록')를 다시 넣음
3. `matcher.DECLINED_ANSWERS` 별도 목록: 무응답 판별 중복 구현 ('답변하지 않겠습니다'·'확인할 수 없습니다' 등이 답으로 취급됨)
4. contract 모드에서 `test_llm_pilot_exec::test_5xx_retried_and_conservatively_billed`가 결정적으로 실패 (프롬프트 길이 변화로 비용 합의
   소수 넷째 자리 반올림 일치 비교가 깨짐 0.5412 vs 0.5411 - 하네스 취약성, 판정 회귀 아님)

b11ec29의 목적(답한 기준이 '정보 부족'으로 남는 문제)은 재설계에서 질문 계약 + 승인 IR 경로로 해결하도록 설계되어 있습니다(활성 IR 0이라 현재는 미해결).
팀 결정 필요: (a) 계약 정책 경로로 해결 / (b) b11ec29를 E9 연결 검사(라운드·목적·세션·극성)와 함께 다시 설계 / (c) legacy 동작 변경으로 수용.

9fdffc4는 b11ec29 위에 만들어졌지만 파일 의존이 없어(README 외 충돌 없음) 단독 반영이 가능했습니다.

## C. 기능

API 계약 (프론트엔드 `app.js` 호출 vs `web/app.py` 라우트, 실제 코드 대조):

| 기능 | 프론트엔드 요청 | 백엔드 | 호환 | 조치 |
|---|---|---|---|---|
| 설정 | `GET /api/config` | 있음 | 호환 | - |
| 환자 목록·상세 | `GET /api/patients` (상세는 목록 항목을 화면에서 표시, 별도 상세 API 없음) | 있음 | 호환 | - |
| 시험 목록 | `GET /api/trials` | 있음 | 호환 | - |
| 한국어 표시 | `GET /api/ko` | 있음 | 호환 | - |
| 매칭 실행 | `POST /api/runs` | 있음 | 호환 | - |
| 진행 중 실행 | `GET /api/runs/active`, `GET /api/runs/{id}` | 있음 | 호환 | - |
| 진행·질문·결과 | `GET /api/runs/{id}/events` (SSE: status·trace·questions·answers·result·error·end·decision_diagnostic) | 같은 8종 발행 | 호환 | - |
| 답변 제출·재평가 | `POST /api/runs/{id}/answers` (재평가는 같은 실행 안에서 서버가 수행, 별도 API 없음) | 있음 | 호환 | - |
| 취소 | `POST /api/runs/{id}/cancel` | 있음 | 호환 | - |
| 추천 결과 | result 이벤트의 `matches`·`recommendation`·`usage` | 같음. ContractSession은 `contract` 필드 추가(화면은 읽지 않음) | 호환 | - |
| Inspector | 웹 화면에 없음 | 오프라인 (`scripts/render_inspector.py`, CLI 기록) | 해당 없음 | 웹 노출하지 않음 (strict 개인정보 정책) |
| 정책 선택 | 화면·요청 파라미터에 없음 | 서버 환경 변수(`CTREC_SESSION`·`CTREC_QA_POLICY`)만 | - | 요청으로 정책을 바꾸는 경로를 만들지 않음 |

호환 어댑터는 필요 없었습니다.

흐름별 상태 (실제 브라우저 Edge headless + Playwright 1.64.1, 오프라인 가상 데이터):
- A 환자 선택·B 매칭·C 추가 질문·답변·재평가: 흐름 자체는 동작 (질문 표시·답변·결과 도달). 다만 결과 화면 문구를 단언하는 브라우저 테스트는 실패 (D절, 기준에서도 같음)
- D Inspector: CLI·세션 기록 -> Inspector 화면(HTML) 테스트 통과 (웹 화면 연결은 없음, 설계)
- E 오류 처리: 빈·과다 입력, 서버 거절 메시지, 중복 제출, 모름 답·재제출 실패 복구, 새로고침·만료, 탭 재방문, SSE 끊김 복구, 예산 초과, 시간 초과 - 브라우저 통과

## D. 테스트

| 환경 | 기준 95117dc (P10 병합 후) | 통합 브랜치 | 비고 |
|---|---|---|---|
| 백엔드 전체 legacy | 1660 / 0 / 6 | **1660 / 0 / 6** | 숫자 같음 (9fdffc4는 브라우저 테스트만 수정, 기본 실행에서 건너뜀) |
| ContractSession + contract | 1654 / 6 / 6 | 1654 / 6 / 6 | 정책 차이 6 (같음) |
| 기존 Session + contract | 1656 / 4 / 6 | 1656 / 4 / 6 | 정책 차이 4 (같음) |
| 안전성·Inspector·웹 보안 묶음 (P1 경계·E9·무응답·정책 이전·web_security·Inspector·E2E 9개 파일) | - | 372 / 0 / 4 | P0 A~E 포함 |
| **실제 브라우저** `test_web_browser.py` (legacy / ContractSession) | 14 / 18 / 0 | 14 / 18 / 0 x 두 모드 | 팀원 원본 9fdffc4도 14 / 18 - **실패 18건은 세 곳 모두 같은 테스트 (통합으로 생긴 실패 0)** |

(통과 / 실패 / 건너뜀)

브라우저 실패 18건 (기존 문제, 프론트엔드 소유):
- 결과 화면 단언 불일치: 화면 개편(5cce805) 후 결과 카드가 시험 ID 대신 제목을 보이고 추천 카드를 그림 -> 테스트의 이전 화면 기대값과 다름
  (`test_full_journey_with_evidence`, `test_no_recommendation`, `test_result_text_without_internal_codes_and_originals_kept` 등)
- 명도 대비 검사 (기준 4.5:1, `test_responsive_contrast…` 4건): 주 버튼 3.04:1·단계 번호 3.31:1은 기준에서도 같음(기준에서는 같은 스타일의
  '입력 내용 확인' 버튼). **9fdffc4가 새로 더한 대비 문제 1건**: 로고 옆 보조 문구 `.brand .tag` 4.19~4.31:1 - 디자인 소유자 확인 필요(수정하지 않음)
- 질문 입력 칸 보이지 않음 시간 초과 (`test_question_format_validation_and_unknown`, `test_choice_questions…` 등)
기대값을 바꾸지 않았습니다. 이 18건은 이번 P11 이전부터 기준·팀 main에 있던 상태이며, 브라우저 테스트는 기본 실행에서 건너뛰어 지금까지 드러나지 않았습니다.

보안 (브라우저·HTTP): CSP·외부 자원 없음, 보고서 XSS 정화(CSP 하), 오류 상세 숨김(HTTP 통과, 브라우저 테스트는 위 화면 단언으로 실패), 다른 프로필 차단(HTTP 통과),
세션 혼합·잘못된 실행 ID(web_security 통과), Inspector strict(통과). 실제 LLM 호출 없음 (`test_full_journey_real_llm_code_path`는 가짜 SDK).

## E. Git

- 통합 브랜치: `integration/frontend-backend-final` (worktree `C:/Users/jsw58/Desktop/workseoyeon-frontend-integration`), 95117dc에서 생성
- 커밋: `0b7048c` 9fdffc4 cherry-pick (작성자 ehdroal 보존, `(cherry picked from commit 9fdffc4…)`), 이 보고서 커밋
- 기준 브랜치 반영: 보고서 커밋 후 fast-forward (결과는 최종 응답과 커밋 이력에 기록)

## F. 보존

- Frozen Store: 26개 해시 기록값과 일치, `verify_frozen()` 문제 0 (전후 동일)
- 다른 worktree: 변경 없음 (스냅샷 비교). qalink 미커밋 7파일, 재설계 미커밋, Inspector 브랜치 그대로
- 원격 브랜치 `origin/main`·`origin/feat/user-frontend`: 읽기만 (fetch). 팀원 브랜치에 커밋하지 않음
- 공용 venv 변경 없음: Playwright는 스크래치 폴더(`pip install --target`)에만 설치

## G. 남은 위험

- b11ec29 보류: 기준 브랜치가 팀 `origin/main`보다 1커밋 뒤(b11ec29)이고 9fdffc4는 다른 해시로 들어감 -> 다음에 origin/main을 병합하면
  9fdffc4 변경은 같은 내용으로 정리되고 b11ec29만 남음 (그때 B절 결정 필요)
- 브라우저 테스트 18건 실패 (기존): 결과 화면 기대값 갱신과 명도 대비 수정은 프론트엔드 작업자 결정
- 실제 LLM 미검증, 임상 검수(활성 IR 0), contract 기본 전환 미결정, 배포 전 검증(브라우저 수동 점검·운영 설정) 필요
- 기본 정책 legacy 유지, contract는 서버 환경 변수 옵트인만
