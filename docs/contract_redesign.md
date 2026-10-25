# Q&A 판정 재설계: Proposition Key + Question Contract + Condition IR + Deterministic Evaluator

브랜치 `feat/proposition-contract-redesign` (시작: `integration/midterm-1026` = `65eaa0f`). 미커밋, 미승인 작업입니다.
보호 파일(`pipeline.py`, `matcher.py`, `patient_profiler.py`, `decision_*`, `status.py`, `data/`)은 바꾸지 않았고,
연결이 필요한 곳은 `docs/proposals/*.patch`로 남겼습니다.

## 1. 데이터 흐름

```
처음 입력 ──> Patient Profiler (처음 입력만, 답변 뒤 재추출 없음: ContractSession)
                 └─> profile ──> rule_engine / Matcher(LLM) ──> _enforce_* · E9 직접성 · 근거 수준(UNVERIFIED)
                                                               └─> qa_guard: 답변에 기댄 확정 -> UNVERIFIED
확인 질문 ─┬─ 계약 질문 (question_contract.render: 문장·선택지 고정, LLM 없음)
           │      └─ 답 ── map_answer(선택지 일치) ──> PropositionLedger ──> fact_store "prop:<명제 키>"
           └─ 비계약 질문 (기존 question_generator) ── 답은 원문 기록으로만 (확정 근거 아님)
승인된 IR(ir_review) + 명제 사실 ──> evaluator (함의·기간·근거 수준·3값 논리) ──> CriterionAssessment(method="contract")
                                                         └─ merge(기존 판정과 상충이면 UNVERIFIED) ──> recompose(decide·코호트)
```

## 2. 모듈

| 모듈 | 역할 |
|---|---|
| `propositions.py` | 개념 표(상위 개념 = 승인된 포함 관계), 한정어, 양상(event/condition/measurement), 시간 범위(current/window+anchor/ever), 명제 키 정규화, 함의 `entails` |
| `condition_ir.py` | IR 형식(atom/compare/and/or/not/unsupported), 원문 구간, Frozen 해시, completeness, 근거 수준, anchor_policy, 내용 digest |
| `ir_review.py` | 사이드카(`contracts/`) 로딩, 활성화 판정(형식·Frozen 해시·원문 일치·사람 승인 digest), 검수 기록(자동화 이름·사유 없는 승인 거부) |
| `question_contract.py` | 계약(문장·선택지·선택지별 명제 단언), 형식·모순 검사, 렌더링, 변조 검사(verify_asked), 답 매핑 |
| `proposition_store.py` | 계약 답 -> FactStore 명제 사실, 장부가 기록한 사실만 신뢰(issued), 세션 격리, 명시적 정정만, 자유 서술은 후보로만 |
| `evaluator.py` | 승인 IR + 명제 사실 -> 기존 `CriterionAssessment` (확정·미확인·UNVERIFIED·상충) |
| `qa_guard.py` | 계약 없는 답변에 기댄 확정을 UNVERIFIED로 되돌림(출처 판별만, 의미 검증 아님), 시험 판정 재계산(`recompose`). 구조 모드(initial_text·qa_log)·머리말 모드. `policy()`: 기존 Session 경로의 정책 선택(CTREC_QA_POLICY=legacy 기본 / contract) |
| `contract_session.py` | `Session` 상속 어댑터: 프로필 격리 + 경계 + 결정적 평가 + 계약 질문 + 출처 기록. `session_class()` (CTREC_SESSION=contract) |
| `scripts/build_contract_drafts.py` | P6 초안 생성기 (`--check`로 동기 확인) |
| `scripts/report_contract_drafts.py` | 검수 준비 상태표 (`docs/review/contract_draft_review.md`) |
| `scripts/build_review_package.py` | 기준별 검수 자료 (`docs/review/ir_review_package.md`): 사람이 읽는 IR, 승인 대상 digest, 계약 선택지별 명제 사실, 임상 검토 질문·필요 증거, 표현 불가 사유·확장 요건 |

## 3. 상태 매핑 (기존 체계 재사용)

| 평가 결과 | CriterionAssessment | decision_state gap · 외부 상태 |
|---|---|---|
| 확정 | holds yes/no, passes, method="contract", evidence_directness="contract" | none |
| 사실 없음·부분 IR·기준 시점 미승인 | 평가기는 결정하지 않음 -> 기존 판정 유지 | 기존 |
| 진료 기록 필요 + 자기 보고 | holds unknown, needs_verification, self_reported_holds | unverified · POTENTIAL_MATCH 또는 INSUFFICIENT_INFORMATION |
| 계약 답끼리 상충 / 기록과 계약 상충 | holds unknown, needs_verification, flag `conflicting_contract_answers` / `contract_vs_record_conflict` | unverified |
| 계약 없는 답변에 기댄 확정 (경계) | holds unknown, needs_verification, self_reported_holds, flag `qa_uncontracted` | unverified · 같은 비계약 질문 반복 안 함 |

## 4. 불변 조건과 구현 위치

| # | 조건 | 위치 | 테스트 |
|---|---|---|---|
| 1 | 계약 없는 Q&A는 확정 근거가 아님 | `qa_guard`, `ContractSession.match` | `test_contract_p1_guard.py`, `test_contract_p5_session.py` |
| 2 | target·condition_id는 의미 증거가 아님 | 평가기는 target을 보지 않음. 계약 digest·질문 문장 검증 | `test_tampered_question_answer_not_recorded`, 긴 답 다른 target 테스트 |
| 3 | 계약·명제 키로 의미 연결 | `question_contract`, `proposition_store` | P3 테스트 |
| 4 | 다른 명제는 승인된 함의로만 공유 | `propositions.entails` | `test_different_propositions_never_merged` 외 |
| 5 | 기간·기준일·한정어 생략 금지 | anchor_policy 없으면 미확정, 시점 이동 규칙 | `test_anchor_without_policy_is_undecided`, `test_stale_negative_answer...`, 경계 테스트 |
| 6 | 미승인·해시 불일치 IR은 결정적 확정에 사용 안 함 | `ir_review.ir_record` | P2 검수 테스트 |
| 7 | Profiler 우회 불가 | `ContractSession.profile_patient` (재추출 없음), 경계(evidence_source) | `test_profile_reextraction_bypass_blocked` |
| 8 | clinical_record 기준은 자기 보고로 확정 안 함 | `evaluator` (UNVERIFIED), 기존 `_apply_evidence_policy` | `test_record_required_gives_unverified...` |
| 9 | 상충 시 최신 답 자동 채택 안 함 | `evaluator` leaf conflict, `PropositionLedger.correct`(명시적) | `test_correction_is_explicit_only` |
| 10 | 기존 Holds·MatchStatus·gap 재사용 | 위 3절 | P5 테스트 |
| 11 | 잘못된 제외도 방지 | 경계·평가기 모두 양방향 | `test_wrong_exclusion_is_also_blocked`, `test_recent_surgery_does_not_imply_major` |
| 12 | 미확정은 기존 미확인 상태 | 위 3절 | |

## 5. IR·계약 검수 절차 (사람)

1. `contracts/ir/<trial>.json`의 초안을 원문과 대조 (구간·기간·기준 시점·한정어·예외·근거 수준).
2. 기간 기준은 기준 시점 근사(`anchor_policy`)를 팀 정책에 따라 추가할지 결정. 추가하면 내용이 바뀌므로 digest가 바뀝니다.
3. 승인 기록: `Registry().record_review("ir", "<trial>:<rule>", ir.digest(), "approved", reviewer="<사람 이름>", note="<사유>", clinical_review_confirmed=True)`.
   자동화 이름(claude, llm, bot 등)과 사유 없는 승인은 거부됩니다. 계약도 같은 방식(`"contracts"`).
4. 내용을 고치면 이전 승인은 효력이 없고 다시 검수해야 합니다.

## 6. 정책 선택 (P8)

| 설정 | 기존 Session 경로 (matcher, 패치 03 v2) | ContractSession |
|---|---|---|
| `CTREC_QA_POLICY` 없음 / `legacy` (기본, 현재 서비스 정책) | 기존과 같음 (확인 답변 인용 확정은 인용·E9·근거 수준 검사만) | 항상 경계 적용 (구조 모드) |
| `CTREC_QA_POLICY=contract` (그 밖의 값도 contract) | 계약 없는 답변에 기댄 확정 -> UNVERIFIED (머리말 모드) | 같음 |
| `CTREC_SESSION=contract` | - | CLI·웹 실행이 ContractSession 사용 (패치 04·05) |

정책 비교 테스트: `tests/test_qa_policy_migration.py` (Legacy Regression / Contract Policy Regression / Migration Comparison).
사례 A(질문이 기준과 의미상 다름)는 계약 정책에서 막는 것이 안전성 개선, 사례 B(정확한 질문이지만 계약 없음)는 계약 도입 전의
기능 제한이며 승인된 계약·IR로 회복됨(`test_case_b_is_recovered_by_approved_contract`).

E9 보조 검사 (P8, 패치 07): 부정 표현을 어절 단위로 판별(산부인과의 '부인', 음성 녹음의 '음성', 끊임없이의 '없'은 부정 아님),
답 전체가 거부·모름·무응답인 확인 답변은 어느 쪽 확정 근거도 아님(`question_contract.non_answer` 하나로 판별).

### P9 통합 후보 (`integration/contract-redesign-candidate`)

| 항목 | 내용 |
|---|---|
| Profiler 프롬프트 (01 v2) | 확인 답변 충돌 줄은 `CTREC_QA_POLICY=contract`에서만 바뀜(`patient_profiler.system_prompt`). legacy 프롬프트는 65eaa0f와 같음 |
| E9 절 경계 | 있음·없음 서술 뒤의 '-고'·'-(으)며'도 절 경계 ("페니실린 알레르기는 없고 조영제 알레르기 있음") |
| E9 구조 연결 (qalink) | 답변 줄 라운드, 적격성 목적 문답만(`eligibility_answers`), `check(session_id=)` 세션 필터, 같은 질문의 답 수정(`answers_revised`) -> `conflicting_answers` |
| 무응답 판별 | '확인할 수 없습니다'·'알 수 없습니다'·'해당 질문에 답하고 싶지 않습니다' 등 추가. `evidence_directness.non_answer`는 `question_contract.non_answer`를 그대로 부름 |
| ContractSession trace | 계약 적용 후 판정을 `contract applied` 기록에 포함 -> Inspector가 그 라운드의 최종 판정으로 읽음 (기록 불일치 오경고 해소) |
| Inspector 계약 절 | 명제 키·`계약@v버전`·IR digest는 형식 검사 후 표시(이전에는 `<text>`로 가려짐), 형식이 다르면 가리고 형식 문제로 셈 |

옮기지 않은 qalink 규칙: 질문 극성 추론(`question_polarity`·`answer_polarity`), 질문 주제·기간 추측(`_same_topic`·`_fact_scope`·
`_answer_relation`), Matcher 프롬프트 답변 블록(`_answers_block`), Session이 qa_log를 Matcher에 넘기는 변경, `_objects`의
한국어 부정 어절 제외(부재 근거의 대상 비교를 느슨하게 함). 다른 질문 사이의 관계는 질문 계약의 명제 키가 정합니다.

## 7. 패치 제안 (`docs/proposals/`, 적용하지 않음)

P8 통합 후보 패치는 `docs/proposals/p8/`(적용 순서·검증은 `docs/review/p8/p8_integration_report.md`). 아래는 P7 패치입니다
(03은 P8의 `03-matcher_qa_policy.patch`가, 06은 P8 v2가 대체).

| 파일 | 대상 | 내용 |
|---|---|---|
| `01-patient_profiler.patch` | 보호 | Profiler 프롬프트의 "최근 답변 우선" 제거 (불변 조건 9) |
| `02-pipeline_session_id.patch` | 보호 | 기존 Session의 QAPair에 session_id 기록 |
| `03-matcher_qa_guard.patch` | 보호 | `match_trial` 끝에서 `qa_guard` 적용 (기존 Session에도 경계) |
| `04-cli_session_class.patch` | cli.py (inspector-record-contract와 충돌 가능) | `CTREC_SESSION=contract`로 ContractSession 선택 |
| `05-web_session_class.patch` | web/app.py | 같은 선택을 웹 실행에 |
| `06-inspector_contract_provenance.patch` | `feat/decision-inspector`의 inspector.py | 계약 출처 화면 데이터·HTML 표·기준 필드 추가 (v1 기록에는 섹션 없음) |

### 패치 적용 영향

P7 검토 결과는 `docs/review/p7_integration_review.md` (패치별 검토표, 기존 테스트 4건 승인 자료, 안전성 검증, qalink 처리 계획).
IR·계약 검수표는 `docs/review/contract_draft_review.md` (`scripts/report_contract_drafts.py`로 생성).

## 8. 알려진 한계

- 계약은 아직 수치 답(measurement)을 사실로 만들지 않습니다. `compare` 노드는 평가기에서 미확인입니다.
- 진단 기능(dependency_diagnostic, decision_loop)은 기존 `Session.match` 안에서 경계 적용 전 판정으로 계산됩니다(기본 off).
- 개념 표는 초안 57개에 필요한 개념만 담았습니다. 개념·상위 관계 추가는 코드 변경과 검수가 필요합니다.
- 질문 계약은 답변 시점 기준(assessment)만 다룹니다. 미래 시점 기준(first_dose 등)은 기준별 근사 승인이 필요합니다.
- E9는 개념군(임신·수유, 알레르기·금기) 기준만 극성·대상을 봅니다. 그 밖의 기준(예: "Prior surgery")의 '없음' 근거는
  `unchecked`로 LLM 판정을 그대로 둡니다 (legacy 경로의 남은 위험, 계약 정책·질문 계약이 대체).
- E9 부정어 판별은 표기 판별입니다. 띄어 쓴 '음성'(陰性/音聲)과 '부인'(否認/婦人, 예: "부인 동반 내원")은 구분하지 못해 부정으로 둡니다.
- 기존 Session(legacy)은 Matcher에 qa_log를 넘기지 않습니다. qalink처럼 넘기도록 바꾸면 라운드·목적·답변 상충 검사도 함께 필요합니다
  (`docs/review/p8/p8_integration_report.md` C절).
