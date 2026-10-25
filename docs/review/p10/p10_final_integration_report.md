# P10 최종 통합 보고서

작성 2026-10-25. push·배포·유료 API 호출·검수 승인 생성·Frozen Store 수정·다른 worktree 변경은 하지 않았습니다.

## A. 실제 통합 상태

| 항목 | 값 |
|---|---|
| 통합 브랜치 | `integration/contract-redesign-final` (worktree `C:/Users/jsw58/Desktop/workseoyeon-contract-final`) |
| 기준 | `integration/midterm-1026` = `65eaa0f` (작업 시작·종료 시점 모두 같음, 다른 작업자 갱신 없음) |
| 기준 브랜치 반영 | **하지 않음 - 승인 대기** (I절 차단 사유) |
| 변경 | 65eaa0f 대비 89개 파일, +22,108 / -19줄 (커밋 6 + 이 보고서 커밋) |

커밋 (오래된 순):

| 커밋 | 내용 | 파일 |
|---|---|---|
| c1a205a | Proposition key·Condition IR·검수 레지스트리·Question Contract·초안(contracts/)·생성 스크립트·schemas | 28 |
| d299fd7 | Deterministic Evaluator·Proposition Ledger·qa_guard·ContractSession | 4 |
| ed193e0 | E9 부정어(어절 단위)·절 경계(-고·-(으)며)·무응답·라운드/목적/세션 연결·답 수정 상충 | 1 |
| 237a815 | 정책 스위치 연결: matcher(03v2)·patient_profiler(01v2)·pipeline(02)·web | 4 |
| 866d45a | Inspector(62f744e) + CLI 실행 기록(36728b6) + 계약 출처 화면 | 5 |
| be43129 | 테스트·설계 문서·검수 자료·P7~P9 보고서와 패치 | 47 |

커밋 단위 조정: `cli.py`는 Inspector 기록(36728b6)과 Session 선택이 한 파일이라 Inspector 커밋에 넣었습니다. 중간 커밋은 단독으로
전체 테스트를 통과하도록 나눈 것이 아니라 의존 순서(기반 -> 평가기 -> E9 -> 연결 -> Inspector -> 테스트)로 나눴습니다.
통합 후보 worktree의 미커밋 변경 91개 파일을 모두 옮겼고(바이트 단위 동일 확인), 제외한 것은 재현용 전체 패치 사본 2개
(`docs/proposals/p8|p9/00-integration-candidate-full.patch`, 0.8MB·2.1MB - 커밋 내용과 중복)뿐입니다. 이 두 파일은 통합 후보 worktree에 남아 있습니다.

## B. 코드 리뷰 결과

검토 범위: 65eaa0f 대비 전체 diff, 보호 파일, 판정 안전성(P10 §3.2 항목), Inspector, 코드 품질.

| 구분 | 결과 |
|---|---|
| 판정 안전성 | 잘못된 Q&A의 ELIGIBLE·INELIGIBLE 확정, 질문 의미·기간·극성 불일치, 거부·모름·확인 불가, 다른 세션·다른 시험 기준, 계약 ID·명제 키 위조, IR digest 불일치, 의료기록 요구, 최초 정보 보존: 모두 테스트 근거 있음(F절, P9 보고서 I절) - contract 정책에서 확정 없음 |
| Inspector | v1/v2 호환, 계약 출처·IR digest 표시, strict 숨김, synthetic 표시, 최종 판정-trace 일치: 테스트 근거 있음 |
| 중복 구현 | 없음 (무응답 판별 1곳, E9의 `non_answer`는 위임) |
| import cycle | 없음 (P9: 모듈 63개 개별 import) |
| 임시 코드·스크래치 경로 | 코드·테스트·스크립트에 없음. P8·P9 보고서(문서)에만 작업 당시 스크래치 경로가 기록으로 남아 있음 |
| 테스트 전용 코드의 운영 유입 | 없음 (qalink 비교용 API shim은 스크래치 사본에만) |
| 미사용 함수 | 신규·변경 모듈 9개에서 없음 |
| 줄바꿈 | 커밋된 파일 중 CRLF 저장은 기존 `data/labels/*.csv` 3개뿐(기존 그대로). 신규 파일은 LF로 정규화됨 |

이번 단계에서 새로 발견한 코드 결함: 없음. (P9에서 발견·수정한 6건은 P9 보고서 C.3: Profiler 프롬프트의 정책 무관 변경,
'-고' 절 경계 잘못된 통과, 무응답 표현 누락, Inspector 식별자 가림, ContractSession trace 불일치 오경고, qalink API 이름)

남은 위험: J절.

## C. 보호 파일 변경

보호 파일 목록(`tests/test_llm_pilot_exec.py` PROTECTED) 중 바뀐 것은 3개이며, 각각 P9 승인 대상 패치(`docs/proposals/p9/`)와 줄 단위로 같습니다.

| 파일 | 변경 | legacy 영향 | contract 영향 |
|---|---|---|---|
| `agents/patient_profiler.py` (01v2) | `system_prompt()` - contract 정책에서만 확인 답변 충돌 줄 교체 | 없음 (프롬프트 sha256이 65eaa0f와 같음, `test_p9_default_policy`) | 충돌 시 어느 쪽도 고르지 않음 |
| `pipeline.py` (02) | `QAPair.session_id` 기록 (직렬화 제외) | 판정·출력 형식 불변 | 세션 격리 근거 |
| `agents/matcher.py` (03v2) | `policy()=="contract"`일 때만 `guard_assessments` | 없음 | 비계약 답변 의존 확정 -> UNVERIFIED |

`evidence_directness.py`는 PROTECTED 목록 밖이지만 판정 안전 장치라 별도 검토: 기본 정책에도 적용되는 의도된 수정이며, 65eaa0f 모듈과의
차등 비교(환자 원문 203건 x 기준 456개, 23,220건)에서 판정 변화 0건 - 기존 정상 판정이 사라지는 회귀 없음.

보호 파일 검사(`test_frozen_store_and_policy_files_unchanged`): 통합 전 후보(미커밋)에서는 위 3개 파일을 탐지해 실패, 통합 브랜치(커밋 후)에서는
작업 트리가 HEAD와 같아 통과합니다. **이 통과는 변경 승인을 뜻하지 않습니다** - 검사는 HEAD 대비 미커밋 변경만 봅니다. 기준 반영 시
65eaa0f 대비 변경은 위 3개 파일이며, 이 범위가 P10 §4 통합 정책(legacy 기본, contract 옵트인)에 해당하는지를 승인자가 위 표로 확인해야 합니다.

## D. qalink·Inspector

- qalink: 브랜치를 병합하지 않고 유효 기능만 반영 - 무응답 판별(공통 모듈), session_id 기록·필터, 라운드 일치, 적격성 목적 격리,
  같은 질문 답 수정 상충, 테스트 반올림 오차 수정. 대체·제거: 극성·주제·기간 문자열 추측, Matcher 프롬프트 답변 블록, Session이 qa_log를
  Matcher에 넘기는 변경. qalink 테스트 58개(통합 브랜치, API shim): legacy 32 / contract 24 통과 - P9와 같음 (분류는 P9 보고서 D절)
- Inspector: 두 브랜치 파일 + 06 v2 + P9 수정. 충돌은 `cli.py` 1곳(실행 기록 메타 + Session 선택, 의미 단위로 둘 다 유지)
- 다른 작업자 변경: qalink worktree 미커밋 7개 파일·재설계 worktree 미커밋 22항목·Inspector 브랜치 모두 그대로

## E. 정책

- 기본: `CTREC_QA_POLICY` 없음 = legacy, Session 클래스 = 기존 Session (`test_defaults_keep_legacy_policy_and_base_session`)
- contract 옵트인: `CTREC_QA_POLICY=contract`(기존 Session 경계) / `CTREC_SESSION=contract`(ContractSession) - 환경 C·D에서 정책 차이 외 실패 없음
- 활성 IR: **0** (IR 57·계약 49 모두 unreviewed, 검수 기록 파일 없음)

## F. 전체 테스트

명령: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ANTHROPIC_API_KEY= .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -rfEs`

| 환경 | 통합 전 (P9 후보, 미커밋) | 통합 후 (final 브랜치) | 실패 분류 (통합 후) |
|---|---|---|---|
| A. 기준 65eaa0f | 1028 / 0 / 1 | 1028 / 0 / 1 | - |
| B. legacy 기본 | 1659 / 1 / 6 | **1660 / 0 / 6** | - (통합 전 1 = 보호 파일 검사) |
| C. ContractSession + contract | 1653 / 7 / 6 | 1654 / 6 / 6 | 정책 차이 6 |
| D. 기존 Session + contract | 1655 / 5 / 6 | 1656 / 4 / 6 | 정책 차이 4 |
| E. Inspector 5개 파일 x 두 모드 | 183 / 0 / 5 | 183 / 0 / 5 | - |
| F. qalink 테스트 58 (shim) | legacy 32 / contract 24 통과 | 같음 | 제거 함수 19, 정책 차이 8, 동작 단언 7 |
| P0 A~E 반례 (`test_counterexamples_never_confirm…` 6 + `test_e_compound…` 1) | 통과 | 7/7 통과 x legacy·contract | - |
| 안전성 묶음 (P1 경계·정책 이전·E9·무응답 5개 파일) | - | 229 통과 x 두 정책 | - |
| 웹 타이밍 반복 (취소 2 + 계약 웹 흐름 + P9 경로, 3모드 x 20회) | 0/60 실패 | 0/60 실패 | - |

(통과 / 실패 / 건너뜀)

- **실제 회귀: 0건.** 정책 차이 6건(contract 정책에서 legacy 기대값을 기록한 기존 테스트 4 + ContractSession 옵트인의 프로필 재추출 없음 2)은
  기대값을 바꾸지 않았습니다. 환경 문제: 이번 실행 없음. 테스트 하네스 문제: 없음
- 건너뜀 6: 브라우저 E2E 1(playwright 없음), 심볼릭 링크 5

## G. E2E

| 경로 | 결과 |
|---|---|
| CLI 기본 (v1 기록 -> Inspector) / CLI `CTREC_SESSION=contract` (v2 기록 -> load_run) | 통과 |
| Web HTTP: 기본 / 기존 Session + contract 정책 / ContractSession + 대기 중 취소 | 통과 (TestClient + 실제 실행 스레드) |
| Inspector: 계약 실행 파일 왕복, 기록 불일치 0, strict·synthetic | 통과 |
| 실제 브라우저 | **미검증** (playwright 미설치, 설치하지 않음) |
| 실제 LLM | **미검증** (결정적 대역만, 유료 API 호출 없음) |

## H. Frozen Store 및 Git 보존

- `store_patch.verify_frozen()` (FROZEN.json 26개 해시): 통합 브랜치·기존 worktree 4곳 모두 문제 0, `data/` diff 없음
- 작업 전후 스냅샷(모든 worktree의 HEAD·변경 파일 해시·브랜치 ref): 기존 worktree 5곳 변경 없음. 새 ref는
  `integration/contract-redesign-candidate`(P9)와 `integration/contract-redesign-final`(P10)뿐
- 기존 작업 브랜치에 커밋하지 않음, push 없음

## I. 기준 브랜치 반영 및 Rollback

**반영하지 않은 이유:** `integration/midterm-1026`은 `workseoyeon-midterm` worktree에 체크아웃되어 있습니다. 반영(fast-forward)하려면
그 worktree에서 merge를 실행해 파일을 바꿔야 하고, worktree 없이 ref만 옮기면 그 worktree가 HEAD와 어긋납니다. 그 worktree를 누가 쓰는지
이 환경에서 확인할 수 없어 P10 §7("사용 중이면 직접 수정하지 않음")에 따라 멈췄습니다. 그 밖의 반영 조건(기준 HEAD 불변, 충돌 없음,
보호 파일 범위, Frozen Store, 실제 회귀 0)은 충족합니다.

승인 후 반영 (기준 HEAD가 여전히 65eaa0f이면 fast-forward, 충돌 없음):

```
git -C C:/Users/jsw58/Desktop/workseoyeon-midterm status --short          # 비어 있어야 함
git -C C:/Users/jsw58/Desktop/workseoyeon-midterm merge --ff-only integration/contract-redesign-final
```

기준이 그 사이 바뀌었다면 final 브랜치를 새 HEAD 위에 다시 검증(`git merge` 또는 재적용 후 전체 테스트)한 뒤 반영합니다.

Rollback:
- 반영 전: final 브랜치를 쓰지 않으면 끝 (`git worktree remove C:/Users/jsw58/Desktop/workseoyeon-contract-final` 후
  `git branch -D integration/contract-redesign-final`). 기준 브랜치는 그대로
- fast-forward 반영 후: 기준 브랜치에서 `git revert --no-edit 65eaa0f..<반영된 HEAD>` (커밋별 되돌림, 역순 자동). 기록을 지우는 reset은 쓰지 않음
- 부분 되돌림: 기능 단위 커밋이라 예를 들어 정책 연결만 되돌릴 때 `git revert 237a815` (legacy 동작은 원래 바뀌지 않음)

## J. 후속 작업

- 임상 검수: IR 57(임상 검토 13·준비 37·표현 불가 7)·계약 49(임상 9·준비 40), 기준 시점 근사 정책 - 검수 기록은 사람만 생성
- 실제 LLM 섀도 비교 (유료 실행 승인 필요): Profiler(contract 문구), Matcher의 비계약 답변 인용, 질문 생성기
- 브라우저 검증 (playwright 또는 수동)
- contract 기본 전환 검토: 정책 차이 6건의 기대값 변경 승인, UNVERIFIED 증가 수용, 공유 많은 계약(QC-PREGNANT-NOW·QC-LACTATING-NOW) 우선 검수
- legacy 잔여 위험: E9 개념군 밖 기준의 '없음' 근거는 검사하지 않음, qa_log를 Matcher에 넘기는 변경을 넣으면 서로 다른 질문 상충 3건이 잘못 확정
- 기존 브랜치 정리: `fix/qa-evidence-link`(미커밋 7파일)·`feat/proposition-contract-redesign`(미커밋 22항목)·통합 후보 브랜치 보존/종료 결정
