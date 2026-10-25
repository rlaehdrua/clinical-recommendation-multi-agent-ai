# P9 통합 후보 브랜치 보고서

작성 2026-10-25. 브랜치 `integration/contract-redesign-candidate` (worktree `C:/Users/jsw58/Desktop/workseoyeon-contract-integration`),
HEAD = 기준 커밋 `65eaa0f` (`integration/midterm-1026`). **커밋하지 않았습니다.** 모든 통합 코드는 이 worktree의 미커밋 변경입니다.
merge·rebase·push·배포·유료 API 호출·Frozen Store 수정·검수 기록 생성은 하지 않았고, 다른 worktree·브랜치는 읽기만 했습니다.

## A. 통합 결과

| 항목 | 상태 | 내용 |
|---|---|---|
| 독립 통합 브랜치·worktree | 완료 | `65eaa0f`에서 새 브랜치 + worktree 생성 (같은 이름 없음 확인) |
| P1~P8 신규 모듈·재설계 미커밋 변경 이전 | 완료 | 재설계 worktree의 변경·미추적 61개 파일을 복사 (원본에서 commit·reset·stash·checkout 없음) |
| P8 패치 적용·재현 | 완료 | 10 -> 01·02·03 v2·04·05·07·08 적용 = P8 스크래치 int와 파일 단위 동일, Inspector(62f744e 파일 + 06 v2, 36728b6 파일 + 04b) 적용 = P8 스크래치 최종 후보 full과 파일 단위 동일 (차이는 P8 보고서 사본뿐) |
| E9 수정 통합 + qalink 4개 충돌 구간 조율 | 완료 | F절. 기준 대비 차등 비교에서 의도치 않은 변경 0건 |
| 거부·모름·무응답 통합 | 완료 | 공통 판별 1곳, P9 목록 6개 + 변형 7개 보강, 사실+모름 혼합 답 회귀 테스트 |
| qalink 선별 통합 | 완료 | D절. 구조 연결 검사(라운드·목적·세션·같은 질문의 답 수정) 이전, 의미 추측 규칙은 대체 |
| Inspector 두 브랜치 + 06 v2 | 완료 (+결함 2건 수정) | E절 |
| 계약 정책 스위치 | 완료 | 기본 legacy 유지. Profiler 프롬프트 변경(01)을 정책 스위치 뒤로 옮겨 legacy 프롬프트를 기준과 글자 단위로 같게 함 |
| IR·계약 검수 패키지 포함 | 완료 | 활성 IR 0, 검수 기록 없음 (H절) |
| E2E | 완료 (브라우저·실제 LLM 제외) | I절 |
| 전체 회귀 | 완료 | J절. 실제 회귀 0건 |
| 실제 반영(커밋·병합)·임상 승인·기본 정책 변경 | 미수행 (금지·승인 항목) | L절 |

## B. Git 상태

| worktree | 브랜치 | HEAD | 상태 (P9 전후 동일 여부) |
|---|---|---|---|
| `workseoyeon` | feat/decision-inspector | 62f744e | 깨끗함, 변경 없음 |
| `workseoyeon-contract-redesign` | feat/proposition-contract-redesign | 65eaa0f | 미커밋 22항목(P7 상태 + P8 문서), 변경 없음 |
| `workseoyeon-evidence` | fix/evidence-directness-validation | acee4a3 | 깨끗함, 변경 없음 |
| `workseoyeon-midterm` | integration/midterm-1026 | 65eaa0f | 깨끗함, 변경 없음 |
| `workseoyeon-qalink` | fix/qa-evidence-link | 65eaa0f | 미커밋 7항목, 변경 없음 |
| **`workseoyeon-contract-integration` (신규)** | **integration/contract-redesign-candidate (신규)** | 65eaa0f | 미커밋: 기존 파일 수정 8 + 신규 파일 (목록 `docs/proposals/p9/status.txt`) |

- merge-base: 재설계·qalink·midterm = 65eaa0f, feat/decision-inspector(62f744e)와 65eaa0f = 64d471b, feat/inspector-record-contract(36728b6)는
  65eaa0f 계열과 고유 변경이 `cli.py`·`tests/test_cli_records.py` 2개뿐 (decision-inspector 고유 변경은 `inspector.py`·`render_inspector.py`·`test_inspector.py` 3개)
- 기타 Inspector 관련 브랜치: 위 두 개 외 없음 (`git branch --list '*inspector*'`)
- 새로 생긴 ref는 `refs/heads/integration/contract-redesign-candidate` 하나 (스냅샷 비교: `scratchpad/p9/snapshot_*.txt`)
- Frozen Store: `store_patch.verify_frozen()` 문제 0건 (FROZEN.json 26개 해시) - 통합 후보와 기존 worktree 4곳 모두. `data/` diff 없음

## C. 실제 통합 코드

### C.1 기존 파일 변경 (65eaa0f 대비)

| 파일 | 보호 | 출처 | 변경 |
|---|---|---|---|
| `src/ctrec/agents/patient_profiler.py` | 보호 | **01 v2 (P9)** | `system_prompt()`: contract 정책에서만 '확인 답변 충돌' 줄 교체. legacy는 `SYSTEM` 그대로 (sha256 기준과 같음) |
| `src/ctrec/pipeline.py` | 보호 | 02 | `QAPair(..., session_id=self.session_id)` 1줄 |
| `src/ctrec/agents/matcher.py` | 보호 | 03 v2 | `qa_guard.policy() == "contract"`일 때만 `guard_assessments` 4줄 |
| `src/ctrec/cli.py` | - | 36728b6 + 04b | CLI 실행 메타·Inspector 기록 + `session_class()` |
| `src/ctrec/web/app.py` | - | 05 | `session_class()` |
| `src/ctrec/evidence_directness.py` | - | 07 + **P9** | 어절 단위 부정 판별, 무응답 제외, **'-고·-(으)며' 절 경계, 라운드·목적·세션 연결, 답 수정 검사** |
| `src/ctrec/schemas.py` | - | 재설계 | `QAPair.session_id`·`contract_id`(직렬화 제외), `method`/`EvidenceDirectness`에 `contract` |
| `tests/test_llm_isolation.py` | - | 08 (qalink) | 반올림 오차 허용 범위 비교 |

보호 파일 변경 전후 diff는 `docs/proposals/p9/01v2-…`, `02-…`, `03v2-…` 패치 파일 그대로입니다 (합계 +21/-2줄, 판정 로직 변경은 정책 스위치 뒤).
보호 파일 검사(`test_frozen_store_and_policy_files_unchanged`)는 통합 후보에서 **설계대로 실패**합니다: 위 3개 파일이 HEAD와 다름 -
허용 범위(통합 후보 브랜치 한정, 정책 스위치 뒤 또는 기록 필드) 안의 변경이며, 테스트는 제거·완화하지 않았습니다.
**이 실패 때문에 어느 환경도 '전체 테스트 성공'이 아닙니다.**

### C.2 신규 모듈 (재설계 P1~P8, Inspector)
`propositions.py`, `condition_ir.py`, `ir_review.py`, `question_contract.py`, `proposition_store.py`, `evaluator.py`, `qa_guard.py`,
`contract_session.py`, `inspector.py`(62f744e + 06 v2 + P9), `contracts/`(IR 18 파일·계약·검증 사례), `scripts/`(초안·상태표·검수 자료 생성,
`render_inspector.py`), 테스트 21개 파일, 문서(`docs/contract_redesign.md`, `docs/review/*`, `docs/proposals/*`).

### C.3 P9에서 새로 고친 것 (P8 후보 대비 6개 파일 + 테스트 4개 파일)

| 파일 | 수정 | 이유 |
|---|---|---|
| `patient_profiler.py` | 01 -> 01 v2 (정책 스위치 뒤로) | P8의 01은 정책과 무관하게 프롬프트를 바꿔 **실제 LLM에서 legacy 기본 동작이 달라질 수 있었음** (결정적 대역 테스트로는 안 드러남, LLM 캐시 키·비용 추정 길이도 달라짐) |
| `evidence_directness.py` | 절 경계 '-고·-(으)며' | **잘못된 통과 (65eaa0f부터 있던 결함)**: "페니실린 알레르기는 없고 요오드 조영제 알레르기 있음" + 판정 '조영제 알레르기 없음' -> `direct` 통과 |
| `evidence_directness.py` | qalink 구조 연결 검사 이전 | D절 |
| `question_contract.py` | 무응답 판별 보강 | '확인할 수 없습니다'·'알 수 없습니다'·'해당 질문에 답하고 싶지 않습니다' 등이 무응답으로 판별되지 않아 '없'이 부재 근거로 읽힐 수 있었음 |
| `contract_session.py` | 계약 적용 후 판정을 trace에 기록 | ContractSession 실행을 Inspector로 열면 result.json(적용 후)과 trace(적용 전)가 달라 **'최종 판정 불일치' 오경고** |
| `inspector.py` | 계약 절 식별자 형식 검사 | 06 v2에서 명제 키·`계약@v버전`이 `<text>`로 가려져 **표시되지 않았음**, 목록 필드는 검사 없이 출력 (변조 기록의 자유 문장 노출·비목록 값에서 예외) |

## D. qalink 통합

qalink 미커밋 변경 7개 파일을 원본에서 읽기만 하고 실제 diff로 재분류했습니다.

| 분류 | 항목 | 통합 후보 |
|---|---|---|
| 이전 | 거부·모름 판별 | `question_contract.non_answer` 하나로 통합 (decision_state.DECLINED_ANSWERS 포함). qalink API 이름 `evidence_directness.non_answer`는 그것을 부르는 위임 함수로만 둠 (중복 구현 없음) |
| 이전 | 테스트 반올림 오차 수정 | 패치 08 |
| 유지 | session_id | `QAPair.session_id`(02, 직렬화 제외) + **`check(session_id=)` 세션 필터 (P9)** |
| 재구현 | round·question_id | **`Segment.round` + `_linked` 라운드 일치 (P9)**, ContractSession 명제 사실의 `r{round}:{question_id}` |
| 재구현 | purpose 격리 | **`eligibility_answers`: 적격성 목적 문답만 연결 (P9)**. 계약 경로는 계약 질문만 사실을 만듦 |
| 재구현 | 근거 출처 | 명제 사실 `source`·`contract_id@version`·IR digest (Inspector 표시) |
| 재구현(축소) | 답변 상충 | **같은 질문 문장의 답 수정만 `conflicting_answers` (P9, `answers_revised`)**. 다른 질문 사이 관계는 명제 키 |
| 대체 | `_same_topic`, `_fact_scope`·`_contains`·`_answer_relation`(기간 문자열), `question_polarity`·`answer_polarity` | 질문 계약 선택지 -> 명제 사실 (문자열로 주제·기간·극성을 추측하지 않음) |
| 제거 | `_answers_block`(Matcher 프롬프트 답변 블록), Session이 qa_log를 Matcher에 넘기는 변경, `schemas` `"ambiguous"` | 계약 정책과 충돌 (비계약 답을 확정 근거로 쓰는 경로) |
| 거부 | `_objects`의 한국어 부정 어절 제외, `_STOP`의 '둘·모두·다·both·neither' | 부재 근거의 대상 비교를 느슨하게 함 (대상어가 사라지면 '모든 대상의 부재'로 읽힘). 또 부분 문자열 판별이라 '산부인과'가 대상어에서 빠짐 |

qalink 테스트 58개를 통합 후보 스크래치 사본에서 실행 (`match_trial(session_id=)` API shim만 추가, 판정 로직 그대로):

| 결과 | P8 | P9 | 내용 |
|---|---|---|---|
| 두 정책 통과 | 20 | **24** | 라운드 불일치·답 수정 상충·목적 필터·비답변 판별 API가 새로 통과 |
| legacy만 통과 | 8 | 8 | '연결된 답변으로 확정'을 단언 -> 계약 정책에서 확정하지 않는 것이 설계 (정책 차이) |
| 두 정책 실패 - 제거·대체한 함수의 단위 테스트 | 19 | 19 | `answer_polarity` 10, `_answer_relation` 기간 8, `answers_conflict` 1 |
| 두 정책 실패 - 동작 단언 | 11 | 7 | contract: 7건 모두 **확정하지 않음**(사유 플래그만 다름 4, 프롬프트 블록 단언 1, 연결 답 확정 단언 2). legacy: **잘못된 확정 3건** (아래) |

**legacy에 남은 위험 (감추지 않음):** `match_trial(qa_log=...)`을 직접 넘기면 서로 다른 질문의 상충 답("수술 받은 적 있나요? 아니요" +
"최근 4주 안에 수술을 받았나요? 예" 등 3건)으로 확정됩니다. 현재 서비스 경로(`Session.match`)는 qa_log를 넘기지 않아 도달하지 않습니다
(P8 7건 -> P9 3건). qalink처럼 넘기도록 바꾸면 이 3건이 실제 경로가 되므로, 그 변경은 계약 정책 없이 넣지 않아야 합니다.

## E. Inspector 통합

- 통합: feat/decision-inspector(62f744e) `inspector.py`·`render_inspector.py`·`test_inspector.py` + feat/inspector-record-contract(36728b6)
  `cli.py`(실행 메타·기록)·`test_cli_records.py` + 06 v2 + P9 수정
- 기록 형식: **v1** = 기존 Session·CLI 기록(result.json에 `contract` 없음) - 계약 절 없음, 기존 화면 그대로 / **v2** = ContractSession 기록 -
  계약 절 표시. Inspector 스키마는 `ctrec.inspector/v1` 그대로 (계약 절은 선택 필드)
- 표시: 판정 상태(시험·기준), 기준별 판정 근거·방법·결과(decided / undecided / partial_ir_not_used / unverified / conflict),
  IR 버전·digest(12자리), 명제 키, 질문 계약 `ID@v버전`, 질문(`r라운드:질문ID`), 사실 ID, 사실 출처, 정정(superseded) 여부, 라운드
- provenance 검증: 명제 키(`propositions._KEY` 문법)·계약 참조·digest·코드는 형식이 맞을 때만 표시, 아니면 `<text>`로 가리고 형식 문제로 셈.
  provenance·facts가 객체·목록이 아니면 읽지 않고 형식 문제로 남김 (예외 없음). 형식 문제 수는 계약 절까지 포함해 다시 셈
- 라운드 판정: ContractSession `contract applied` 기록(판정 포함)을 그 라운드의 최종 판정으로 읽음 -> 계약 실행의 '기록 불일치' 오경고 0건
- 개인정보: strict - 명제 값 `(strict: 숨김)`, 계약 절에 질문 문장·답변 문장 없음 (질문은 ID로만). synthetic_details - 명제 값(예/아니요)만 추가.
  변조 기록에 넣은 자유 문장 8종이 화면에 나오지 않음 (`test_tampered_provenance_fields_are_masked_not_rendered`)
- 테스트: `test_inspector.py`(기존) + `test_cli_records.py` + `test_p9_inspector_contract.py`(16) + E2E -> 환경 E 183 통과 / 5 건너뜀(심볼릭 링크) x 두 모드

## F. E9 및 무응답 수정

### F.1 qalink와의 4개 충돌 구간 조율

| 구간 | P8 07 | qalink | 통합 |
|---|---|---|---|
| `_Evidence` | `non_answer` 필드 | 같은 필드 | 같음 |
| `_evidence_units` | 답변 줄 전체 무응답 -> 제외, 짧은 답은 기존 연결 규칙 | 긴 답 극성 재해석(`answer_polarity`), 무응답 | P8 유지 + `_linked`에 qalink 라운드·목적 조건 |
| `_objects` | 기존 규칙(낱말 전체가 부정어일 때만 제외) | 한국어 부정 형태소 포함 어절 제외 | **기존 규칙 유지** (D절 '거부') |
| `check()` | 무응답 -> `hedged/non_answer` | `session_id` 필터, `answers_conflict`, `says_concept` | 무응답 + `session_id` 필터 + `answers_revised`. `says_concept`(질문의 개념어로 답의 개념을 채움)는 확정을 늘리므로 넣지 않음 |

### F.2 의도치 않은 변경 검사 (차등 비교, `scratchpad/p9/diff_e9.py`)
65eaa0f의 E9 모듈과 통합 후보를 저장소 환자 원문 203건 x 잠긴 기준 456개로 비교: `window_days` 차이 0, `_objects` 5,045문장 x 개념 3종 차이 0,
개념군 기준 `check()` 23,220건(holds 2 x applicable 2) 판정 차이 0. 즉 실제 데이터에서 객체 비교·기간 검사가 바뀐 경우 없음.

### F.3 회귀 사례 (`tests/test_p9_e9_regression.py` 97개, 맞는 방향 `direct` + 반대 방향 확정 안 됨)
산부인과(婦人), 부인함(否認), 임신 아님, 임신하지 않음, 검사 음성, 검사 양성, 부작용 없음, 서로 다른 대상의 부정(페니실린 vs 조영제 -> `object_not_matched`),
한 문장 긍정·부정(-고 / -(으)며 / -지만), 문진표 Q:/A: 부재·존재, 의료기록 원문(영문 β-hCG negative), 추가 Q&A 답변(산부인과에서 임신 확인).
'보고'·'사고'·'최고' 같은 명사의 '고'는 절 경계가 아님. 기존 P8 E9 테스트 41개·비답변 56개도 그대로 통과.

- 질환 없음·치료받지 않음: 개념군 밖 기준(예: "Prior surgery")은 E9가 극성을 보지 않는 기존 범위(`unchecked`) - K절 위험으로 기록

### F.4 무응답
'답변하지 않겠습니다'·'답변 거부합니다'·'모르겠습니다'·'기억나지 않습니다'·'확인할 수 없습니다'·'해당 질문에 답하고 싶지 않습니다' +
'답하고 싶지 않아요'·'답변을 원하지 않습니다'·'확인이 어렵습니다'·'알 수 없습니다'·'대답하기 싫어요'·'그 질문에는 답변 안 함'·'날짜는 확인할 수 없어요'
-> 무응답, 두 개념 x 두 방향 모두 `non_answer`로 확정 안 됨. 사실 + 모름 혼합("임신 중이에요. 몇 주인지는 모르겠어요")은 무응답이 아니고
사실 절은 `direct`, 모르는 절은 `hedged`. '잘 모르겠지만 아닌 것 같아요'는 추측으로 확정 안 됨. 기록의 거부 진술("BCG 치료를 거부함")은 사실.

## G. 정책 비교 (Legacy / Contract)

| 구분 | legacy (기본, 서비스 정책) | contract (`CTREC_QA_POLICY=contract`) |
|---|---|---|
| 비계약 Q&A 확정 | 인용·E9·근거 수준 검사 통과 시 확정 | 양방향 UNVERIFIED (기존 Session은 머리말 모드, ContractSession은 구조 모드) |
| 최초 입력·기록 정상 판정 | 유지 | 유지 |
| 승인 계약 + 승인 IR | 해당 없음 | 결정적 판정 (기간·기준일·출처·digest·명제 키·계약 버전 검증) |
| 상충 사실 | E9 상충 검사 | 명제 상충 -> 확정 안 함 + 기록-계약 상충 표지 |
| 상태·cohort·summary | 기존 | `recompose`로 재계산 |
| Profiler 프롬프트 | 65eaa0f와 같음 | 확인 답변 충돌 시 어느 쪽도 고르지 않음 (01 v2) |

같은 입력 6건(`test_qa_policy_migration.py`): L1·L2·L3·N2(정확하지만 비계약 질문)는 contract에서 UNVERIFIED (기능 제한, 승인 계약·IR로 회복 테스트 있음),
L4·N1(질문이 기준과 의미상 다름)은 legacy가 **잘못 확정**하고 contract가 차단 (안전성 개선). contract가 legacy보다 확정을 더하는 경우 없음.

정책 전환 시 영향 (환경 D·D2 실패 = 기대값이 legacy 정책을 기록한 기존 테스트):
- 기존 Q&A 테스트 4건 (`test_evidence_directness::test_clause_scope_keeps_certain_clause_and_blocks_unknown_clause`,
  `test_evidence_directness_counterexamples::test_short_answer_needs_question_target_link`, `…::test_linked_answer_polarity_must_match`,
  `test_stage05::test_quote_check_uses_clarification_answers`) - 기대값을 바꾸지 않았음. 전환 결정 시에만 기대값 변경 승인 필요
- ContractSession 옵트인 2건 (`test_team_integration::test_all_answer_types_reach_reanalysis_as_stored_values`,
  `test_web_integration::test_full_flow_questions_answers_and_result`): 답변 뒤 프로필 재추출 없음 -> 비계약 답(검사 수치 등)이 판정에 반영되지 않음
- 운영 영향: 계약 정책 기본화 시 검수 전까지 비계약 답변 기반 판정이 모두 UNVERIFIED (POTENTIAL_MATCH 증가). 공유가 많은 계약
  (QC-PREGNANT-NOW 8기준, QC-LACTATING-NOW 8기준) 검수가 회복 효과가 가장 큼

## H. IR·계약 상태

- IR 57: 임상 전문가 검토 13 / 수정 필요 0 / 검수 준비 완료 37 / 현재 IR로 표현 불가 7 - **모두 `unreviewed`, 활성 0**
- 질문 계약 49: 임상 검토 9 / 검수 준비 완료 40 - **모두 `unreviewed`**
- `contracts/`에 검수 기록(reviews.json 등) 없음. 생성하지 않았음
- 검수 자료: `docs/review/ir_review_package.md`(기준별 원문·사람이 읽는 IR·승인 대상 digest·질문·선택지·명제 대응·기간·출처 요건·미지원 조건·
  임상 질문·체크리스트), `docs/review/contract_draft_review.md`(상태표). 생성 스크립트 3개 `--check` 최신
- 승인 없이 결정적 판정이 켜지지 않음: `test_active_only_with_human_approval_of_current_digest`, `test_frozen_hash_mismatch_invalidates`,
  `test_case_b_without_approval_stays_unverified`, `test_cli_contract_session_run_and_inspector`(저장소 초안으로 실행 -> 활성 0, 계약 질문 0)

IR 검수 담당자가 확인할 항목: 원문 대비 IR 논리(AND/OR/NOT, 함의), 기간·기준 시점(anchor)과 근사 정책, 출처 요건(self_report_ok / clinical_record),
질문 문구·선택지가 명제와 1:1인지(모름 선택지 포함), 미지원 조건(unsupported)이 원문에 실제로 없는지, 검증 사례, 승인 대상 digest.
임상 검토 13 IR·9 계약의 질문과 필요 증거는 검수 자료 '임상 전문가 검토 필요' 절.

## I. E2E 결과 (결정적 대역, 실제 LLM 아님)

| 경로 | 결과 | 테스트 |
|---|---|---|
| 처음 입력 -> Profiler -> Matcher -> 계약 질문 -> ContractSession -> Fact Store(명제 장부) -> 평가기 -> CriterionAssessment -> MatchStatus -> cohort -> Inspector | 통과 | test_p8_e2e 1, test_p9_inspector_contract |
| 기본 CLI (v1 기록 -> Inspector) / Contract CLI (`CTREC_SESSION=contract` -> v2 기록 -> load_run) | 통과 | test_p8_e2e 8 |
| 계약 실행 파일 왕복(result.json·trace.jsonl -> load_run -> 화면), 기록 불일치 경고 0 | 통과 | test_p9_e2e_paths |
| 기본 Web (legacy) / 기존 Session + contract 정책 Web / Contract Web + 대기 중 취소 | 통과 | test_p9_e2e_paths, test_p8_e2e 9 |
| Inspector v1 / v2, strict / synthetic | 통과 | E절 |
| P0 A~E 반례, 잘못된 통과·제외 4방향 | 확정 안 됨 | test_contract_p1_guard, test_p8_e2e |
| 정확한 계약 질문 결정적 판정 / 정확한 비계약 질문 UNVERIFIED | 통과 | test_p8_e2e 1·2, test_qa_policy_migration |
| 세션 격리, 답변 거부, 상충 사실, 기준일 경계 | 통과 | test_contract_p3_p4, test_p8_e2e, test_p9_e9_regression |
| IR hash 불일치, 계약 ID·명제 키 위조, 저장소 위조 사실 | 차단 | test_contract_p2_ir, test_contract_p7_safety, test_p8_e2e 7 |
| 최초 입력 보존, 의료기록 요구 조건, 상태·cohort 재계산 | 통과 | test_contract_p7_safety, test_p8_e2e |

## J. 전체 테스트

명령: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ANTHROPIC_API_KEY= .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -rfEs`
(스크립트 `scratchpad/p9/run_env.sh`, 유료 API 없음). 환경 A·B·F는 새 clone(스크래치)에 원본 미커밋 파일을 복사해 실행 (원본 worktree에서 실행하지 않음).

| 환경 | 통과 | 실패 | 건너뜀 | 실패 분류 |
|---|---|---|---|---|
| A. 기준 65eaa0f | 1028 | 0 | 1 | - |
| B. 재설계 브랜치 (P7 상태 + P8 문서) | 1253 | 0 | 1 | - |
| C. 통합 후보 - legacy 기본 | 1659 | 1 | 6 | 보호 파일 검사 1 |
| D. 통합 후보 - ContractSession + contract 정책 | 1653 | 7 | 6 | 정책 차이 4, 옵트인 정책 차이 2, 보호 파일 검사 1 |
| D2. 통합 후보 - 기존 Session + contract 정책 | 1655 | 5 | 6 | 정책 차이 4, 보호 파일 검사 1 (1회차: 웹 타이밍 1 추가 - 환경 문제, 아래) |
| E. Inspector 통합 테스트 (5개 파일) x legacy / contract | 183 / 183 | 0 / 0 | 5 / 5 | - |
| F. qalink 사본 (미커밋 7파일 그대로) | 1085 | 1 | 1 | 보호 파일 검사 1 (qalink 자체의 pipeline·matcher 미커밋 변경) |
| F2. 통합 후보 + qalink 테스트 58 (API shim) | legacy 32 / contract 24 | 26 / 34 | - | D절 표 (제거 함수 19, 정책 차이 8, 동작 단언 7) |

- 실제 회귀: 0건. 테스트 하네스 문제: 0건 (qalink 테스트 실행에 API shim 1개 - 스크래치 사본에서만)
- 환경 문제: D2 1회차 `test_web_security::test_cancel_during_inflight_call_stops_at_next_checkpoint` (취소 직후 `run.done` 경합).
  단독 5회, 반복 3모드 x 20회(취소 2종 + 웹 계약 흐름 + P9 경로 5개), D2 2회차 전체 실행 모두 통과 -> 실패 1/62, 전체 실행 부하 중에만 (P7과 같은 양상)
- 건너뜀: 브라우저 E2E 1 (`CTREC_BROWSER_TESTS=1` + playwright 필요, 설치하지 않음 -> **브라우저 미검증**), 심볼릭 링크 5 (이 환경에서 생성 불가)
- P9 신규 테스트 126개: E9 회귀 97, Inspector 16, E2E 경로 5, 기본 정책 8
- import cycle: `ctrec` 모듈 63개를 각각 새 인터프리터에서 import -> 실패 0

실패 테스트 목록: C·D·D2·F 공통 `test_llm_pilot_exec::test_frozen_store_and_policy_files_unchanged`(보호 파일 검사) + G절의 정책 전환 영향 6건
(D: 6건 모두, D2: 기존 Q&A 4건) + D2 1회차 웹 타이밍 1건(환경).

## K. 미해결 위험

- 실제 LLM 미검증: Profiler(01 v2 contract 문구), Matcher가 비계약 답변을 인용하는 방식, 질문 생성기의 기준 이탈 빈도는 결정적 대역으로만 확인
- 브라우저 화면 미검증 (playwright 없음). 웹은 HTTP 수준(TestClient + 실행 스레드)까지
- 임상 검토 미완료: 결정적 판정 0건 상태. 기준 시점 근사 정책(first_dose 등 -> 답변 시점) 미결정
- legacy 경로: E9는 개념군(임신·수유, 알레르기·금기) 밖 기준의 '없음' 근거를 판정하지 않음 (예: 기록 "수술 받지 않음" + 판정 근거 검사 `unchecked`) -
  LLM 판정이 그대로 남음. 계약 정책·질문 계약이 대체 경로
- legacy 경로: qa_log를 Matcher에 넘기는 변경을 넣으면 서로 다른 질문 상충 3건이 잘못 확정됨 (D절)
- E9 표기 판별 한계: 띄어 쓴 '음성'(陰性/音聲)·'부인'(否認/婦人)은 부정으로 둠 (확정을 막는 쪽)
- 계약 정책의 기능 제한: 정확하지만 비계약인 질문도 확정하지 않음 (의도된 현재 제한)
- 옵트인 UX: ContractSession은 답변 뒤 프로필을 다시 추출하지 않음
- 웹 취소 타이밍 테스트: 부하 중 1/62 실패 (환경 요인으로 판단, 코드 결함 근거 없음)

## L. 최종 통합 승인 체크리스트 (실제 반영 전 결정)

- [ ] 통합 후보 브랜치를 커밋할지·어떤 단위로 나눌지 (권장: 재설계 모듈 / 보호 파일 01v2·02·03v2 / E9·무응답 / Inspector / 문서·테스트)
- [ ] 보호 파일 승인: 01 v2(Profiler, contract 정책만), 02(session_id), 03 v2(matcher 정책 스위치) - 승인 시 보호 파일 검사 절차 갱신 방식 결정
- [ ] 서비스 기본 정책: legacy 유지 / contract 전환 - 전환 시 G절 6개 테스트의 기대값 변경 승인과 UNVERIFIED 증가 수용
- [ ] E9 수정을 legacy에도 적용하는 것 승인 (안전성 수정: 잘못된 통과 감소, 산부인과 정상 판정 회복. 실제 데이터 차등 비교 변화 0)
- [ ] qalink: '연결 답변 확정'·의미 추측 규칙을 넣지 않는 결정, `fix/qa-evidence-link` 브랜치 처리(보존·종료), schemas `"ambiguous"` 제외
- [ ] Inspector 작업자 확인: 06 v2 + P9 수정(식별자 형식 검사, contract applied 판정 읽기), `cli.py` 04b 병합 결과
- [ ] 임상 검수: IR 57·계약 49 (검수 기록은 사람만), 기준 시점 근사 정책
- [ ] 실제 LLM 섀도 비교 (유료 실행 승인 필요), 브라우저 수동·자동 확인
- [ ] 반영 후 스모크: 기본 전체 테스트, contract 전체 테스트, Frozen 26 해시, 웹 취소 반복

## M. Rollback

통합 후보는 커밋되지 않은 별도 worktree·브랜치라 다른 작업에 영향이 없습니다. 사용하지 않을 경우:

```
git worktree remove --force C:/Users/jsw58/Desktop/workseoyeon-contract-integration   # 미커밋 변경과 함께 삭제
git branch -D integration/contract-redesign-candidate                                # 커밋 없음: 65eaa0f를 가리키는 ref만 삭제
```

삭제 전 보존이 필요하면 `docs/proposals/p9/00-integration-candidate-full.patch`(65eaa0f 기준 전체, 재현 검증됨)를 다른 곳에 복사해 두면
`git apply`로 언제든 재구성할 수 있습니다. 원본 worktree·브랜치·Frozen Store는 바뀐 것이 없어 되돌릴 것이 없습니다.
부분 반영 후 되돌릴 때는 L절의 커밋 단위별 `git revert`.

## 부록

- 패치 (`docs/proposals/p9/`): `00-integration-candidate-full.patch`(65eaa0f -> 통합 후보 전체, 이 보고서 폴더·p9 패치 폴더 제외),
  `01v2-patient_profiler_policy_gated.patch`, `02-pipeline_session_id.patch`, `03v2-matcher_qa_policy.patch`, `diffstat.txt`, `status.txt`, `SHA256SUMS.txt`.
  재현: 새 clone 65eaa0f + 00 = 통합 후보 worktree (파일 단위 동일, 검증 스크립트 `scratchpad/p9/make_p9_patches.sh`)
- 통합 전후 diff 요약: `docs/proposals/p9/diffstat.txt` (기존 파일 8개 수정, 신규 파일은 `status.txt`)
- 작업 스크립트 (스크래치): `apply_p9_e9.py`, `apply_p9_clause.py`, `diff_e9.py`, `run_qalink.sh`, `qalink_shim.py`, `run_env.sh`, `flaky.sh`, `snapshot_*.txt`
