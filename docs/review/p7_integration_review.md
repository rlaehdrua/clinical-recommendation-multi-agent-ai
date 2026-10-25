# P7 통합 검증·정책 충돌 검토 (승인 자료)

작성 범위: `feat/proposition-contract-redesign` worktree (미커밋). 보호 파일 패치는 스크래치 사본에서만 적용·검증했고
원본에는 적용하지 않았습니다. IR·질문 계약은 모두 미승인입니다. 유료 API는 호출하지 않았습니다.

## 1. qa_guard의 위치 (의미 검증 아님)

`qa_guard`는 **확정 근거의 출처**만 판별하는 보수적 안전 경계입니다. 질문이 기준의 명제를 제대로 물었는지는 판별하지 않으며,
그 역할은 질문 계약·명제 키·평가기가 맡습니다.

P7에서 출처 판별을 바꿨습니다.

| 모드 | 사용처 | 판별 방법 | 알려진 한계 |
|---|---|---|---|
| 구조 모드 | ContractSession | `initial_text`(처음 입력 문자열)와 `qa_log`(QAPair의 질문·답 문자열)에서 인용 조각을 직접 찾음 | 인용이 없는 프로필 필드는 matcher의 `evidence_source`에 의존 |
| 머리말 모드 | 기존 Session (패치 03) | `[추가 확인 정보` 머리말 앞뒤로 나눔 | 처음 입력 안에 머리말이 있으면 그 뒤를 답변으로 봄 (과잉 하향, 안전한 방향) |

P7 이전 구현(원문을 `Q:`/`A:` 줄로 해석)은 다음 정상 판정을 잘못 하향했습니다 (분류 B, 수정 완료).
- 처음 입력이 문진표처럼 `Q:/A:` 형식이면 **확인 답변이 없어도** 기록 판정을 하향
- 답변이 기록 문구를 반복하면 기록이 뒷받침하는 판정을 하향

## 2. 보호 파일·타 브랜치 패치 검토표

| 패치 | 대상 | 적용 확인 | qalink 충돌 | Inspector 브랜치 | 정책 변화 | 필요 승인 |
|---|---|---|---|---|---|---|
| 01 patient_profiler | 프롬프트 1문장 | worktree ✓ | 적용 가능 | 적용 가능 | 충돌 시 최근 답 우선 -> 둘 다 기록 (불변 조건 9) | 보호 파일 변경 승인, LLM 출력 변화는 실제 LLM 확인 필요 |
| 02 pipeline session_id | `Session.clarify` QAPair | worktree ✓ | **충돌** (qalink에 같은 변경) | 적용 가능 | 없음 (메모리 필드, 저장 형식 동일) | qalink 병합 시 불필요 |
| 03 matcher qa_guard | `match_trial` 끝 | worktree ✓, qalink ✓ | 텍스트 충돌 없음, **정책 충돌** (qalink 테스트 10건) | 적용 가능 | 기존 Session에서도 비계약 답변 확정 -> UNVERIFIED | 정책 승인 + 기존 테스트 4건(+qalink 10건) 기대값 변경 승인 |
| 04 cli session_class | `_run_case` | worktree ✓ | 적용 가능 | 적용 가능 (36728b6 이후 cli.py) | 기본 동작 동일, `CTREC_SESSION=contract`일 때만 | 진입점 변경 승인 |
| 05 web session_class | 실행 생성 | worktree ✓ | 적용 가능 | 적용 가능 | 위와 같음 | 웹 변경 승인 |
| 06 inspector 출처 | `build_view`·`render_html` | Inspector 브랜치 ✓ (테스트 110 passed, 4 skipped) | 해당 없음 | 대상 | 화면 표시만, v1 기록에는 섹션 없음 | Inspector 작업자 확인 |

추가 충돌: 이 브랜치의 `schemas.py`(Literal 2곳, QAPair `session_id`·`contract_id`)는 qalink의 `schemas.py`와 텍스트 충돌
(같은 위치에 `"ambiguous"` Literal, 같은 `session_id` 줄). 수동 병합 필요.

보안·출처: 03은 판정을 올리지 않고 하향만 함. 04·05는 환경 변수로만 선택되며 기본값은 기존 Session.
ContractSession의 출처 기록(`result()["contract"]`)에는 환자 답의 명제 값이 들어감 (기존 result.json의 qa_log와 같은 보존 정책 대상).

## 3. 기존 테스트 충돌 분석

### 3.1 정책 변경(A)으로 기대값이 바뀌는 기존 테스트 4건 (패치 03 적용 시) — 기대값을 바꾸지 않았음

| 테스트 | 입력 | 기존 기대 | 새 결과 | 변경 이유 | 안전성 영향 |
|---|---|---|---|---|---|
| `test_evidence_directness.py::test_clause_scope_keeps_certain_clause_and_blocks_unknown_clause` | 'Prior wrist surgery', 답 "손목 수술은 했는데 …몰라요" | I1 통과(passes=True) | 판정 불가, 답변상 'yes' 보존 (UNVERIFIED) | 비계약 답변 | 잘못된 통과 위험 감소. 올바른 판정일 가능성이 높은 사례라 **정상 판정 손실** |
| `test_evidence_directness_counterexamples.py::test_short_answer_needs_question_target_link` | 'Prior surgery', "Q: 수술 받은 적 있나요? A: 예" (연결됨) | 연결 시 통과 | 판정 불가 (UNVERIFIED) | 연결(target)은 의미 증거가 아님 (불변 조건 2) | 질문이 기준과 같아 **정상 판정 손실**. 계약(QC-SURGERY 계열) 승인 시 회복 |
| `…::test_linked_answer_polarity_must_match` 두 번째 단언 | 같은 질문 '아니요', 판정 no | 불충족(passes=False) | 판정 불가 | 위와 같음 | **정상 제외 손실** (잘못된 제외 방지와 맞바꿈) |
| `test_stage05.py::test_quote_check_uses_clarification_answers` | 'Active hepatitis B or C', 답 "간염 치료 받은 적 없어요" | holds=no 확정 | 판정 불가 | 치료 이력 답으로 활동성 감염 부재를 확정하던 사례 | **잘못된 통과 차단** (안전성 향상) |

권장: 4건 모두 정책 승인 후 기대값 변경. 단 위 2·3번은 '질문이 기준을 정확히 물은 경우'의 손실이므로, 승인과 함께 해당 기준의
질문 계약 검수를 우선 진행해 손실을 줄일 것.

### 3.2 그 밖의 실패

| 항목 | 분류 | 원인 | 처리 |
|---|---|---|---|
| `test_llm_pilot_exec.py::test_frozen_store_and_policy_files_unchanged` | D | 보호 파일 미커밋 변경 검사 (설계대로) | 승인·커밋 절차로만 해소 |
| 신규 대조 테스트 14건 (+P7 2건) | C | '경계 전 기존 결과는 확정' 단언이 패치 03 환경에서 성립하지 않음 | 수정 완료: `MATCHER_GUARDED` 감지로 환경별 기대값 선택. 두 환경 모두 224 passed |
| `test_web_security.py::test_cancel_while_waiting_for_answers` | C | 웹 실행 스레드 타이밍 (최대 10초 대기) | 반복 실행 결과는 G절 |
| (옵트인 검증) `CTREC_SESSION=contract`에서 `test_web_integration::test_full_flow…`, `test_team_integration::test_all_answer_types_reach_reanalysis…` | A | ContractSession은 답변 뒤 프로필을 다시 추출하지 않음 (Profiler 우회 차단 설계) | 기본값(기존 Session)에서는 통과. 옵트인 모드 채택 시 이 두 테스트 기대값 결정 필요 |

## 4. Q&A 안전성 검증 (결정적 대역, `test_contract_p7_safety.py` 외)

| 경로 | 결과 | 테스트 |
|---|---|---|
| 관련 없는 부정 답 -> 제외 기준 FALSE | 차단 (UNVERIFIED, 답변값 보존), 두 모드 | `test_unrelated_answer_never_confirms_any_direction` |
| 관련 없는 긍정 답 -> 제외 기준 TRUE | 차단 | 같음 |
| 관련 없는 답 -> 포함 기준 TRUE / FALSE | 차단 | 같음 |
| 처음 입력만으로 내린 판정 (4방향) | **유지** | `test_record_based_judgments_are_kept` |
| 답변이 기록 문구 반복 / 문진표 형식 처음 입력 | 유지 (P7 수정 전에는 하향) | `test_answer_repeating_record_phrase…`, `test_intake_form…` |
| 줄바꿈·머리말을 넣은 답변 | 답변으로 판별 (차단) | `test_multiline_and_header_injected_answers_are_answers` |
| 같은 환자 다른 세션 | 사실 공유 안 함 | `test_same_patient_other_session_facts_do_not_leak` |
| 저장소에 직접 넣은 위조 사실 | 무시 (장부가 기록한 사실만) | `test_forged_fact_in_store_is_ignored` |
| 계약 ID·명제 키 위조 | 평가기가 거부 (선택지 단언 대조) | `test_forged_proposition_key_or_contract_id…` |
| 계약 문구 변조 | 사실 기록 안 함 | P3 `test_tampered_question_answer_not_recorded` |
| 정정·상충 | 명시적 정정만, 상충은 UNVERIFIED | P3 `test_correction_is_explicit_only` |
| Profiler 우회 | ContractSession에서 차단 | P5 `test_profile_reextraction_bypass_blocked` |
| Rule Engine 구조화 필드 | 답변 유래 값이면 차단 | `test_profile_field_from_answer_only…` |
| IR 해시·원문 불일치 | 비활성 | P2 |
| 시험별 기준일 | 근사 승인된 시험만 판정, 다른 기준 시점은 미확인 | `test_trial_specific_anchor_policy` |
| 기간 경계 (57개 초안 전 기간 명제) | N일 참·N+1 미확인·N 거짓·N-1 미확인 | P6 `test_window_boundaries` |
| AND/OR/NOT·부분 미지원 | Kleene 논리, 부분 IR은 확정 안 함 | P3·P4·P6 |
| 하향 후 코호트·요약·외부 상태 | 재계산 일관 | `test_recompose_reselects_cohort…` |
| 평가기 vs 기존 판정 | 같은 방향 -> 평가기, 반대 -> 상충(UNVERIFIED), 기록 확정 + 같은 방향 자기 보고 -> 기록 유지 | `test_merge_rules` |
| 승인 IR 없음 / 사실 없음 | 기존 판정 유지 | P5 `test_contract_session_matches_base…`, `test_active_ir_without_facts…` |
| 진료 기록 필요 기준 | 자기 보고로 충족되지 않음 (UNVERIFIED) | P4 `test_record_required…`, `test_merge_rules` |

발견 사항 (기존 E9 모듈, 이 브랜치에서 고치지 않음): `evidence_directness`가 '산부인과'의 '부인'을 부정어로 읽어
기록 "현재 임신 12주로 산부인과 추적 중"의 제외 판정을 판정 불가로 만듦 (`test_existing_e9_negation_word_finding`).
안전한 방향이지만 정상 판정 손실이며 E9 정책 소유자 결정이 필요합니다.

## 5. qalink(`fix/qa-evidence-link`) 미커밋 변경 처리 계획

| 변경 | 분류 | 근거 |
|---|---|---|
| QAPair `session_id` (exclude) | 재설계에 이미 반영 (동일 정의) | 텍스트 충돌만 해소 |
| `Session.clarify`에 session_id 기록 | 재설계에 이미 반영 (ContractSession, 패치 02) | 02와 중복 |
| `Session.match`가 qa_log·session_id를 Matcher에 전달 | 정책 충돌로 별도 결정 필요 | 비계약 답변으로 확정을 허용하는 것이 목적 -> 불변 조건 1과 충돌 |
| Matcher 프롬프트 `<answers_for_rules>` | 정책 충돌로 별도 결정 필요 | 경계가 확정을 하향하므로 남기면 '답변상 판정' 표시용, 없애면 비용·노출 감소 |
| `non_answer` (거부·모름 판별) | 재설계에 이미 반영 (`question_contract.non_answer`, 독립 구현) | 두 구현 통합 필요 (중복) |
| `answer_polarity`·`question_polarity` | 새 구조에서 불필요 (계약은 선택지로 극성 고정) | 비계약 확정 허용 정책을 유지할 때만 필요 |
| `answers_conflict`·`_answer_relation`·`_same_topic`·`_contains` | 새 구조에서 불필요 | 명제 함의(`propositions.entails`)와 장부 상충으로 대체. `_same_topic`은 P0 반례 E의 원인 |
| `eligibility_answers` 목적 필터 | 새 구조에서 불필요 | 경계가 목적과 무관하게 차단 (긴 답 우회 C4 포함) |
| `"ambiguous"` 직접성 값 | qalink 정책 유지 시에만 필요 | |
| `tests/test_llm_isolation.py` 반올림 허용 오차 수정 | 재설계에 아직 필요 (독립 버그 수정) | 판정 정책과 무관, 별도 승인 |
| `tests/test_qa_evidence_link.py` | 정책 충돌 10건 / 차단 쪽 테스트는 경계와 양립 | qalink 사본 + qa_guard + 패치 03: 10 failed (모두 '연결된 답변으로 확정' 단언) |

권장: qalink의 판정 확장(연결 답변 확정)은 병합하지 않고, 세션 격리·거부 답 판별·테스트 오차 수정만 선별 이전.
결정 전까지 qalink 브랜치는 그대로 보존.

## 6. 최종 통합 실행 계획 (모두 승인 필요)

| 순서 | 변경 | 승인 | 예상 시간 | 롤백 |
|---|---|---|---|---|
| 1 | 정책 결정: 불변 조건 1(비계약 답변 확정 금지) 채택 여부, qalink 처리 | 팀 | 회의 1회 | - |
| 2 | 이 브랜치 신규 모듈·테스트·문서·초안 커밋 (보호 파일 없음, 기본 동작 불변) | 커밋 승인 | 0.5시간 | 커밋 되돌리기 |
| 3 | `schemas.py` qalink와 수동 병합 (qalink 일부 이전 시) | 코드 검토 | 1~2시간 | 커밋 되돌리기 |
| 4 | 패치 01·03 + 기존 테스트 4건 기대값 변경 (승인 기록 표 작성, 보호 파일 검사 절차) | 정책·보호 파일 승인 | 2~3시간 (+ 실제 LLM 확인은 별도) | 해당 커밋 되돌리기 (데이터 변경 없음) |
| 5 | 패치 04·05 (옵트인 진입점), 옵트인 모드 웹 테스트 2건 기대값 결정 | 진입점 승인 | 1~2시간 | 환경 변수 미설정 = 기존 동작 |
| 6 | 기준 시점 근사 정책 결정 -> 검수자가 IR anchor_policy 추가 | 임상·팀 | 정책 회의 + IR당 10~15분 | 검수 기록 'rejected' 추가 |
| 7 | IR·계약 사람 검수 (임상 검토 12 + 계약 6 우선, 이후 검수 준비 완료 37·43) | 임상 검수자 | IR당 15~30분, 계약당 10~15분 (약 30~45시간) | 'rejected' 기록 |
| 8 | 패치 06 (Inspector 브랜치에서) | Inspector 작업자 | 0.5시간 | 커밋 되돌리기 |
| 9 | 섀도 비교·실제 LLM 확인 (P7 잔여) | 유료 실행 승인 | 별도 계획 | - |

## 7. 미해결 위험

- 실제 LLM: Profiler(패치 01) 출력 변화, Matcher가 비계약 답변을 어떻게 인용하는지, 질문 생성기의 기준 이탈 빈도는 결정적 대역으로만 확인.
- 임상: 기준 시점 근사, '큰 수술'·'발열'·면역요법 범위 등 12개 IR과 6개 계약의 의미 판단, 자기 보고 '임신 아님'의 충분성.
- 정상 판정 손실: 불변 조건 1은 '질문이 기준을 정확히 물은 비계약 답변'으로 내린 올바른 판정도 하향함 (3.1의 2·3번). 계약 검수 전까지 UNVERIFIED가 늘어남.
- 옵트인 모드 UX: 답변 뒤 프로필이 갱신되지 않아, 비계약 답(예: 검사 수치)이 판정·화면에 반영되지 않음.
- 머리말 모드 한계: 처음 입력 안에 '[추가 확인 정보' 머리말이 있으면 과잉 하향 (기존 Session 경로, 안전한 방향).
- 기존 E9 부정어 오탐('산부인과').
