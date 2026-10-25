# P14 프론트엔드 최종 통합 및 백엔드 잔여 문제 보고서

작성 2026-10-25. 통합 브랜치 `integration/frontend-qa-p14` (worktree `산학협력 더미\workseoyeon-frontend-p14`, 591dea1에서 생성).
push·배포·유료 API 호출·IR/계약 승인·Frozen Store 수정·다른 worktree 변경 없음. `web/app.py` 수정 없음.

## 1. 원격 커밋 검증

- `git fetch origin feat/frontend-qa` (원격 참조만, 로컬 main 불변). `origin/feat/frontend-qa` = e49c2c8 (작성자 ehdroal, 10-25 19:29), 부모 9fdffc4 (조상에 b11ec29)
- 변경 파일 5개가 팀원 보고와 같음: `web/static/index.html`(+1/-1), `app.css`, `app.js`(+3/-2, 표시 문구만), `tests/test_web_browser.py`, README(+15, 삭제 0)
- matcher·pipeline·판정 로직·`web/app.py` 변경 없음. 새 의존성·외부 호출·`innerHTML`·`eval` 추가 없음 (테스트의 `fetch('/api/trials')`는 같은 서버)
- 브라우저 테스트 기대값 변경 검토: 삭제된 단언 23개는 개편된 화면(질문 하나씩·상세 화면 분리·바뀐 문구)에 맞춘 대응 단언 33개로 바뀜.
  느슨해진 곳 1개: 시험 수를 고정값 26 대신 `/api/trials` 개수와 비교 -> data/trials 오염을 이 테스트로는 잡지 못함 (Python 테스트 `test_config_trials_patients_shape`가 26을 계속 검사)

## 2. cherry-pick 결과

`git cherry-pick -x e49c2c8` -> `e1ce81e` (충돌 없음, 작성자 보존, `(cherry picked from commit e49c2c8…)`). b11ec29·9fdffc4는 들어오지 않음
(`git merge-base --is-ancestor b11ec29 HEAD` 거짓). 9fdffc4 내용은 P11에서 0b7048c로 이미 반영됨.

## 3. 최종 변경 파일 (591dea1 대비)

| 파일 | 출처 | 변경 |
|---|---|---|
| `web/static/{index.html,app.css,app.js}`, `tests/test_web_browser.py`, README | e49c2c8 (원형) | 질문 중 취소 버튼, 대비 WCAG AA, '맞지 않음'에도 판정 주체 표시, 브라우저 테스트 갱신 |
| `src/ctrec/config.py`, `src/ctrec/tools/ctgov.py` | P14 (C) | 검색 결과 저장 경로 분리 `CTGOV_CACHE_DIR = data/cache/ctgov` |
| `src/ctrec/agents/matcher.py` (보호 파일) | P14 (B) | 일반 문구 missing_info -> 원문 기준 문장 (+13줄) |
| `tests/outputs_guard.py`(신규), `tests/test_llm_pilot.py`, `tests/test_llm_pilot_exec.py`, `tests/test_pilot_process.py` | P14 (격리) | outputs 쓰기 검사를 '수집 시점과 같음'으로 |
| `tests/test_p14_backend_followups.py` (신규 14개), 이 보고서 | P14 | |

## 4. 브라우저 E2E (Edge headless, Playwright 1.64.1 스크래치 설치, 오프라인 가상 데이터)

| 모드 | 결과 |
|---|---|
| 기본 (legacy) | **32 / 32 통과** (두 번 실행: cherry-pick 직후, 백엔드 수정 후) |
| ContractSession + contract | 31 / 32 - `test_choice_questions_and_korean_titles`: 답변이 Profiler 재추출 입력에 들어가는지 확인하는 테스트. ContractSession은 설계상 처음 입력만으로 프로필을 만듦 (Python 테스트 2건과 같은 옵트인 정책 차이) |

팀원 보고 항목은 테스트로 확인됨: 질문 한 화면 하나·'다음' 이동, 질문 중 취소 버튼, 답변 제출, 결과 목록·상세 화면 전환, 근거 상세, 재평가 후 결과 갱신,
'질문 1개 반영'·'답변 반영 결과', 맞지 않음 조건의 AI 판정 문구, 명도 대비 4.5:1(4개 화면 크기·다크 모드 포함).
P11 기준선 18건 실패가 모두 해소됨.

## 5. NCT04063540 재평가 (A)

| 확인 | 결과 |
|---|---|
| 환자·질문·답 | 웹 하네스: 합성 환자(58세 남성 NSCLC), 질문 "EGFR 검사 결과를 아시나요?" (`target=[]`, 자유 서술), 답 "EGFR 양성입니다" |
| NCT04063540 | 편두통 + 아밀로라이드 시험, 기준 19개(규칙 엔진 3, LLM 16). 질문과 관련된 기준 없음 |
| Session.match에 qa_log | 전달됨 (P12) |
| 문답 원문 기록 | `qa_log`·원문 답변 블록에 기록 |
| 재매칭 | 수행 (답변 뒤 MatcherOutput 재호출) |
| P12 검증 | 질문이 어느 기준도 겨냥하지 않아 대상 없음 |
| 가짜 LLM | 모든 기준에 항상 `holds="unknown"`, `missing_info="확인 필요"` (고정 출력) |
| UI | 새 결과 표시 ('질문 1개 반영') |

**분류: C (가짜 LLM 고정 출력 + 기준을 겨냥하지 않는 대역 질문)**. 백엔드 연결 버그(A 유형) 아님. 실제 웹 경로 확인: E9(임신·수유)를 겨냥한 예/아니요 질문과
의역 판정을 내는 대역으로 바꾸면 웹 실행에서 E9가 `answer_verified`로 확정되고 근거는 문답 원문 (`test_web_reevaluation_reflects_verified_targeted_answer`).
'모름'은 확정 안 됨. 정보 부족 수를 줄이려고 판정 조건을 바꾸지 않았음.

## 6. missing_info (B)

생성 위치: LLM 출력(`LLMAssessment.missing_info`), 코드가 판정을 되돌릴 때 `a.missing_info or "확인 필요: <LLM 설명>"`, 직접성 검사
`"기준에 직접 대응하는 확인 필요: <기준>"`, 근거 수준 정책, qa_guard·평가기 고정 문구, 판정 누락 시 기준 원문.
'필요: 확인 필요'는 하네스 대역 값이 그대로 화면에 간 것. 실제 모델도 일반 문구를 낼 수 있어, 판정 불가 기준의 missing_info가 비었거나 일반 문구
(확인 필요·정보 부족·unknown 등)뿐이면 `이 기준을 확인할 정보 (원문): <기준 문장>`으로 바꿈. 구체적인 문구는 그대로, 확정된 기준은 건드리지 않음,
새 의료 요구사항·LLM 호출 없음, API·스키마 그대로. **해결**.

## 7. CT.gov 저장 정책과 환경 오염 (C)

- 원인 확인: 웹·오케스트레이터 `search_trials` -> `ctgov.search_studies`가 검색 결과를 `data/trials/<NCT>.json`에 저장(같은 NCT 파일 덮어쓰기 포함).
  웹은 시작 시 `data/trials/*.json`을 모두 시험 목록·실행 풀로 읽음 -> 컴파일되지 않은 검색 시험이 목록에 섞이고 실행하면 `COMPILATION_PENDING`
- 수정: `search_studies` 결과는 `data/cache/ctgov/`(이미 .gitignore 대상)에 저장 (출처·retrieved_at 보존, 실행 중 메모리 사용은 그대로).
  CLI `--nct`(`fetch_study`, 사람이 고른 시험 추가)는 기존대로 `data/trials`. 기존 `data/trials` 파일은 삭제·이동하지 않음
- 잠긴 기준표(26개 시험·471개 기준) 불변

## 8. 테스트 격리

오염 재현(스크래치 사본에 검색 형식 시험 5개 + `outputs/web/old-run`): 수정 전 10건 실패 -> 수정 후 5건.
- 해결 5건 (`outputs/`): `test_llm_pilot`, `test_llm_pilot_exec` 2, `test_pilot_process` 2 - '테스트가 저장소 outputs/에 쓰지 않음'을 수집 시점 상태와 비교 (의도 그대로, 로컬 산출물 무시)
- 남김 5건 (`data/trials` 여분 파일): `test_decisions` 2, `test_web_integration` 3 - 실제 문제(목록에 미컴파일 시험)를 잡는 검사라 완화하지 않음. 원인(검색 저장)은 막음.
  **팀원 로컬 조치 필요**: 이전 실행으로 생긴 `data/trials`의 미추적 시험 파일(`git status`의 `??`)을 `data/cache/ctgov/`로 옮기면 통과
- 팀원 보고 11건 중 10건을 독립 재현 (모두 환경 원인, 코드 회귀 아님). 나머지 1건은 재현하지 못함 - 팀원 로컬의 여분 파일 내용(6개)이 달라서일 수 있으나 확인 안 됨

## 9. Legacy / Contract 회귀 (통과 / 실패 / 건너뜀, 커밋 전)

| 환경 | P13 | P14 | 비고 |
|---|---|---|---|
| legacy | 1710 / 0 / 6 | 1723 / 1 / 6 | +14 신규. 실패 1 = 보호 파일 검사(matcher 미커밋, 커밋 후 통과 - 승인과 별개) |
| ContractSession + contract | 1704 / 6 / 6 | 1717 / 7 / 6 | 정책 차이 6 동일 + 보호 파일 검사 |
| 기존 Session + contract | 1706 / 4 / 6 | 1719 / 5 / 6 | 정책 차이 4 동일 + 보호 파일 검사 |

P12 재평가 35개, P13 Inspector 통합 5개, 웹 보안, P0 A~E 포함 전부 통과. 기존 테스트 결과 변화: outputs 검사 4곳 수정(격리) 외 없음.

## 10. Frozen Store

`verify_frozen()` 문제 0, 26개 해시 기록값과 일치, `data/` 변경 없음.

## 11~12. 기준 브랜치 반영과 최종 HEAD

최종 응답과 `git log`에 기록 (이 보고서 커밋 뒤 fast-forward).

## 13. 남은 위험

- 팀원 로컬 `data/trials` 여분 파일 (위 8절 수동 이동)
- ContractSession 모드 브라우저 1건·Python 2건 (옵트인 정책 차이, 프로필 재추출 없음)
- 개념군 밖 기준은 답해도 미확정 (질문 계약·IR 임상 검수 필요), 실제 LLM 미검증
- origin/main b11ec29는 여전히 보류 (matcher·pipeline 충돌 예정)

## 14. 다음 작업 우선순위

1. 팀원 로컬 `data/trials` 정리 확인 2. IR·질문 계약 임상 검수(공유 많은 QC-PREGNANT-NOW·QC-LACTATING-NOW, EGFR ADC) 3. 실제 LLM 섀도 검증
4. b11ec29 처리 결정 및 원격 main과의 정리 5. trace에 직접성 필드 기록(Inspector 라운드 비교)
