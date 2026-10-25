# P8 통합 후보 구축·안정화 보고 (승인 자료)

작성 2026-10-25. 기준 `integration/midterm-1026` = `65eaa0f`. **모든 변경은 스크래치 통합 사본에서만 했고, 원본 worktree·브랜치에는
보호 파일 변경·커밋·병합을 하지 않았습니다.** 원본 재설계 worktree에는 이 문서와 `docs/proposals/p8/` 패치만 추가했습니다.
유료 API 호출 없음, Frozen Store(`data/`) 변경 없음, 검수 기록(`contracts/reviews.json`) 없음 = 활성 IR 0개.

이 문서의 '통합 후보 정책'(비계약 Q&A는 확정 근거 아님 등)은 검증 기준이며, 서비스 기본 정책 변경을 승인한 것이 아닙니다.
통합 후보의 기본값은 기존 정책(legacy)과 같습니다.

## A. P8 통합 결과

| 항목 | 상태 | 요약 |
|---|---|---|
| 1. 기존 Q&A 브랜치(qalink)와 통합 | 완료 (선별) | 거부·모름 판별 통합·E9 연결, 테스트 반올림 수정 이전. 연결 답변 확정 확장은 넣지 않음 (C절) |
| 2. 보호 파일 패치 통합 후보 | 완료 | 01~08 + 04b·06 v2, 각 기준 브랜치에 적용 확인 (B절) |
| 3. E9 '산부인과' 회귀 | 완료 | 어절 단위 부정 판별. 정상 판정 손실과 **잘못된 통과** 양방향 결함이었음 (D절) |
| 4. 기존 Q&A 정책 충돌 | 완료 (승인 대기) | 정책 선택(`CTREC_QA_POLICY`)으로 기존 테스트 기대값을 바꾸지 않고 Legacy / Contract / Migration 세 종류로 검증 (E절) |
| 5. ContractSession CLI·Web 실행 | 완료 (API 수준) | CLI `cli.main`, 웹 TestClient + 실제 실행 스레드(생성·답변·결과·취소). 브라우저 조작은 미검증 |
| 6. Inspector 계약 출처 | 완료 | 패치 06 v2: IR digest·완전성·사실 출처 표. strict 모드에서 명제 값 숨김 (개인정보 원칙) |
| 7. IR·계약 검수 준비 | 완료 (승인 없음) | 수정 필요 IR 1 -> 0, 계약 문구 결함 3건 수정, 기준별 검수 자료 생성 (F절) |
| 8. 전체 회귀·안전성 | 완료 | 환경 A~F (H절) |
| 9. 승인 자료 | 완료 | 이 문서 I·J절 |
| 실제 LLM·브라우저·임상 승인 | 미완료 (범위 밖) | 승인 항목 (I·J절) |

## B. 최종 통합 후보

### B.1 환경

| 환경 | 내용 |
|---|---|
| 스크래치 B (`int`) | 재설계 worktree(P7) + P8 비보호 변경 + 보호 파일 패치 01~05·07·08 |
| 스크래치 F (`full`, **최종 후보**) | B + `feat/decision-inspector`(inspector.py 등) + `feat/inspector-record-contract`(cli.py 3-way 병합) + 패치 06 v2 |

### B.2 패치 (`docs/proposals/p8/`, 적용하지 않음)

| 파일 | 기준 | 대상 | 내용 | 보호 |
|---|---|---|---|---|
| 10-redesign_p8_nonprotected | 재설계 worktree (P7 상태) | 신규 모듈·테스트·초안·문서 | 정책 선택, 비답변 판별 통합, 한정어 over_5d, 초안 수정, 검수 자료, P8 테스트 | 아님 |
| 01-patient_profiler | 65eaa0f | Profiler 프롬프트 | P7과 같음 (최근 답 우선 제거) | 보호 |
| 02-pipeline_session_id | 65eaa0f | Session.clarify | P7과 같음 (qalink와 동일 변경) | 보호 |
| 03-matcher_qa_policy | 65eaa0f | match_trial 끝 | **v2**: `qa_guard.policy() == "contract"`일 때만 경계. 기본 동작 불변 (P7 03 대체) | 보호 |
| 04-cli_session_class | 65eaa0f | cli.py | P7과 같음 | 아님 |
| 04b-…on_inspector-record-contract | 36728b6 | cli.py | 04를 Inspector 기록 브랜치 cli.py에 맞춘 것 (인접 줄 충돌 해소) | 아님 |
| 05-web_session_class | 65eaa0f | web/app.py | P7과 같음 | 아님 |
| 06-inspector_contract_provenance | 62f744e | inspector.py | **v2**: IR digest·완전성·명제 사실 출처 표, strict에서 값 숨김, 활성 IR 없음 안내 | 아님 |
| 07-evidence_directness_negation_non_answer | 65eaa0f | E9 | 어절 단위 부정 판별 + 비답변 차단 | 아님 (qalink 수정 중 파일) |
| 08-test_llm_isolation_rounding | 65eaa0f | 테스트 | qalink와 같은 반올림 허용 오차 수정 | 아님 |
| 00-integration-candidate-full | 65eaa0f | 전체 | 최종 후보 F 재현용 (참고) | - |

### B.3 적용 순서 (승인 후, 각 단계 커밋 단위로 되돌릴 수 있음)

1. 재설계 worktree 내용 커밋 + 10 (비보호, 기본 동작 불변)
2. 08 (테스트 하네스), 07 (E9: qalink 담당자와 조율 - 4개 hunk 충돌, C절)
3. 02, 01, 03 v2 (보호 파일 승인 절차)
4. 04 (또는 Inspector 기록 브랜치를 먼저 합쳤으면 04b), 05
5. `feat/decision-inspector` 병합 후 06 v2
6. 스모크: 기본값(legacy) 전체 테스트 -> `CTREC_SESSION=contract` 전체 테스트 (H절의 예상 실패만 있는지)

### B.4 적용 검증 (스크래치, 새 clone)

- 재설계 worktree(P7 상태) + 10 + 01·02·03·04·05·07·08 = 환경 B와 파일 단위 동일
- 04b: `36728b6`에 `git apply --check` 통과 / 06 v2: `62f744e`에 통과
- 65eaa0f + 00 = 환경 F와 파일 단위 동일

## C. qalink 선별 통합 결과

| qalink 변경 | 분류 | 처리·근거 |
|---|---|---|
| QAPair `session_id` (exclude) | 유지 | 재설계에 같은 정의. `schemas.py` 수동 병합 시 한 줄 |
| `Session.clarify` session_id 기록 | 유지 | 패치 02 (qalink와 같은 변경) |
| `Session.match` -> Matcher qa_log·session_id 전달 | 제거 | 비계약 답변 확정 경로를 여는 변경. 계약 정책에서는 쓸모없고, legacy에서 도입하면 아래 라운드·목적·상충 검사가 함께 필요 |
| match_trial·E9 `session_id` 필터 | 재설계에서 대체 | ContractSession은 자기 qa_log만 사용, 장부는 세션·issued 검사 (`test_other_session_and_forged_store_facts_are_not_used`) |
| question_id + round 연결 | 재설계에서 대체 | 장부 사실 ref `contract:QC@vN#digest;q:rR:QID`, 계약 digest·문장 검증 |
| 거부·모름·무응답 (`non_answer`) | **이전 (통합)** | `question_contract.non_answer` 하나로 합침(qalink 표현 + 화제어 붙은 거부). E9가 사용 -> legacy에서도 "답변하지 않겠습니다"가 부재 근거로 통과하던 경로 차단 |
| purpose별 격리 (`eligibility_answers`) | 재설계에서 대체 | 경계가 목적과 무관하게 비계약 답변 확정 차단, 장부는 계약 답만 사실화 |
| polarity (`answer_polarity` 등) | 재설계에서 대체 | 계약 선택지가 극성을 고정 |
| `_same_topic`·`_answer_relation`·`answers_conflict` | 제거 | 문자열 의미 동일성 휴리스틱은 최종 근거로 쓰지 않음. 명제 함의 + 장부 상충으로 대체 |
| 시간 범위 문자열 (`_fact_scope`·`_contains`) | 재설계에서 대체 | TimeScope + `entails` |
| 직접성 값 `"ambiguous"` | 제거 | 위 휴리스틱 전제 |
| `_objects`: 부정 형태소가 든 한국어 어절은 대상어 아님 | 추가 검증 필요 | 대상 불일치 오탐을 줄이는 변경(보수 방향 완화). P8 부정 판별 수정과 겹침 -> E9 소유자 검토 |
| `_STOP` 추가 ("둘", "모두", both ...) | 제거 (조건부) | 연결 답 "둘 다 아니에요" 확정용 |
| Matcher `<answers_for_rules>` 프롬프트 | 제거 | 비계약 답변 노출·비용 증가, 경계가 어차피 하향 |
| `docs/evidence_directness.md` 변경 | 제거 | qalink 정책 설명 |
| `tests/test_llm_isolation.py` 반올림 | **이전** | 패치 08 |

qalink 테스트 58개를 통합 후보에서 실행 (비교 환경, qalink API shim: session_id 필터·non_answer 이름만 연결):

| 결과 | 개수 | 내용 |
|---|---|---|
| 두 정책 모두 통과 | 20 | 인용 변조, 다른 기준·시험 답, 세션 기록, 비답변 9종, 질문만 인용, 기록 필요 기준 등 차단 쪽 단언 |
| legacy만 통과 | 8 | '연결된 답변으로 확정' 단언 -> 계약 정책에서는 설계상 실패 (정책 차이 A) |
| 제거된 함수 단위 테스트 | 19 | `answer_polarity` 10, `_contains` 8, `eligibility_answers` 1 (대체) |
| 두 정책 모두 실패 | 11 | 계약 정책: 9건은 **확정하지 않음**(사유 플래그만 다름), 2건은 연결 답 확정·프롬프트 블록 단언(A). legacy: 7건은 match_trial에 qa_log를 직접 넘기면 확정됨 |

legacy 7건(답 수정·상충·평생 없음+최근 있음·대상 겹침·의사 참고용 답·라운드 불일치)은 **현재 Session이 Matcher에 qa_log를 넘기지
않아 서비스 경로에서는 도달하지 않습니다**(`match_trial` 호출부 2곳 모두 qa_log 없음). qalink의 전달 변경을 legacy에 도입할 때만
qalink의 상충·라운드·목적 검사가 필요합니다. 통합 후보는 둘 다 도입하지 않습니다.

## D. E9 수정 결과 (패치 07)

- 원인: `_NEGATION`이 `부인`·`음성`·`없` 등을 부분 문자열로 찾음. '산부인과'(婦人)의 '부인'을 부정어(否認)로 읽음.
- 영향 (P7에서는 정상 판정 손실만 보고했으나 **양방향**이었음):
  - 기록 "현재 임신 12주로 산부인과 추적 중" + 판정 '임신 중' -> polarity_mismatch로 판정 불가 (정상 판정 손실)
  - 같은 기록 + 잘못된 판정 '임신 아님' -> 극성이 '없음'과 맞는다고 보아 **direct 통과 (잘못된 통과)**
- 수정: 한국어 부정을 어절 단위로 판별. 특정 단어 예외가 아니라 세 부류 규칙.
  1. 문법 부정 형태소 없·않·아니: 붙여 쓴 기록("이상없음", "임신아님")이 흔해 어절 안 어디서나 부정
  2. 단, '없'이 굳어 다른 뜻이 된 사전 등재 부사·형용사(끊임없이·틀림없이·변함없이 등)는 부정 아님
  3. 한자어 부정 명사 否認(부인)·陰性(음성): 뒤가 어절 끝이거나 그 명사의 서술·조사 어미일 때만 부정 (산부인과·부인과·부인암·부인께서·음성인식은 아님)
  - 영어 부정어와 띄어 쓴 '안'·'못'은 그대로, 대상어 비교(`_objects`)의 낱말 제외 규칙도 그대로
- 비답변: 답 전체가 거부·모름·무응답인 확인 답변은 어느 쪽 근거도 아님 (`non_answer` flag, legacy 경로에도 적용)
- 회귀 테스트: `test_e9_negation_lexical.py` 41개 (실제 부정 18종, 다른 낱말 속 글자열 13종, 양방향, 공존 절, 다른 개념, 환자 진술·의료진 서술),
  `test_p8_non_answer.py` 56개, P7 발견 테스트를 수정 확인 테스트로 전환
- 한계: 띄어 쓴 '음성'(음성 녹음)·'부인'(부인 동반)은 구분 불가 -> 이전처럼 부정 (확정을 막는 쪽). E9는 보조 검사이며 의미 판정기가 아님

## E. 정책 충돌 정리

| 구분 | 기존 정책 (legacy, 서비스 기본) | 계약 정책 (contract, 통합 후보 검증 기준) |
|---|---|---|
| 비계약 확인 답변을 인용한 확정 | 인용·E9·근거 수준 검사를 통과하면 확정 | 양방향 모두 UNVERIFIED (답변상 판정 보존, 기록·재질문·의료진 확인용) |
| 처음 입력·진료 기록 근거 판정 | 유지 | 유지 (구조 모드: 처음 입력을 답변으로 오인하지 않음) |
| 계약 + 승인 IR | 해당 없음 | 결정적 확정 (IR 해시·계약 버전·명제 키·출처·기간·기준일 검증) |
| clinical_record 기준 | 자기 보고는 UNVERIFIED | 같음 |
| 선택 | `CTREC_QA_POLICY` 없음 | `CTREC_QA_POLICY=contract` (기존 Session) / `CTREC_SESSION=contract` (ContractSession, 항상 경계) |

같은 입력 6건 비교 (`tests/test_qa_policy_migration.py`):

| 사례 | 종류 | 질문 -> 답 | 기준 | legacy | contract | 의미 |
|---|---|---|---|---|---|---|
| L1 | B-free | 수술 받은 적 있나요? -> 손목 수술은 했는데 … 몰라요 | Prior wrist surgery | 충족 | UNVERIFIED | 정상 판정 손실 (자유 서술) |
| L2 | B | 수술 받은 적 있나요? -> 예 | Prior surgery | 충족 | UNVERIFIED | 정상 판정 손실 (계약 승인 시 회복) |
| L3 | B | 수술 받은 적 있나요? -> 아니요 | Prior surgery | 불충족 | UNVERIFIED | 정상 제외 손실 |
| L4 | A | 간염 치료 중인가요? -> 치료 받은 적 없어요 | Active hepatitis B/C | 해당 없음 확정 | UNVERIFIED | **잘못된 확정 차단** |
| N1 | A | 수술 부작용 있었나요? -> 없었어요 | Major surgery ≤4주 | 해당 없음 확정 | UNVERIFIED | **잘못된 통과 차단** (legacy 기본 경로에서 실제로 통과함) |
| N2 | B | 최근 4주 큰 수술? -> 받지 않았어요 | Major surgery ≤4주 | 해당 없음 확정 | UNVERIFIED | 기능 제한. 기준점(첫 투여 전 vs 답변 시점)은 IR 승인으로만 같다고 봄 |

- 사례 A(질문이 기준과 의미상 다름): 계약 정책의 차단은 안전성 개선
- 사례 B(정확한 질문, 계약 없음): 안전성 오류가 아니라 계약 도입 전 기능 제한. 승인된 계약·IR이 있으면 같은 기준이 결정적으로 확정됨
  (`test_case_b_is_recovered_by_approved_contract`), 승인 전이면 계약 질문을 하지 않고 미확정 (`test_case_b_without_approval_stays_unverified`)
- 계약 정책은 확정을 더하지 않음(단조성): legacy가 미확정인 것을 contract가 확정하는 사례 없음
- 기존 테스트 4건: **기대값을 바꾸지 않았습니다.** 기본 정책(legacy)에서 그대로 통과하고, 계약 정책 실행(환경 C)에서만 실패합니다(정책 차이 A)
- qalink 테스트 10건(P7): C절 표의 '연결 답변 확정' 단언 8 + 2. 정책 결정 사항
- 보호 파일 검사 1건(`test_frozen_store_and_policy_files_unchanged`): 보호 파일 패치를 적용한 환경에서 설계대로 실패 (D). 원본에는 보호 파일 변경 없음

## F. IR·계약 검수 패키지

자료: `docs/review/contract_draft_review.md`(상태표), `docs/review/ir_review_package.md`(기준별: 원문, 사람이 읽는 IR, 승인 대상 digest,
선택지별 명제 사실, 검증 사례, 임상 검토 질문·필요 증거, 표현 불가 사유·확장 요건, 체크리스트). 둘 다 생성 스크립트 `--check`로 동기 테스트.
읽기용 사본: `docs/review/p8/ir_review_package.md`.

| 대상 | P7 | P8 | 변경 |
|---|---|---|---|
| IR 57 | 임상 12 / 수정 1 / 준비 37 / 표현 불가 7 | 임상 13 / 수정 0 / 준비 37 / 표현 불가 7 | NCT06756191:E1 수정 -> 임상 검토 |
| 계약 49 | 임상 6 / 준비 43 | 임상 9 / 준비 40 | QC-AMPHOTERICIN-COURSE 신규(대체), 2건 문구 수정 후 임상 확인 |

- 수정 필요 IR 1건 (NCT06756191:E1, 폐 털곰팡이증 + 암포테리신 B 5일 초과): '5일 초과'를 한정어 `over_5d`로 표현하고 3지 선택 계약
  (5일 초과 / 5일 이하 / 받은 적 없음)으로 대체. 남은 쟁점(투여가 PM 치료 목적이어야 하는지, 5일이 누적인지)은 원문에 없어 unsupported로
  남김 -> 부분 IR(결정적 판정에 쓰이지 않음). 임상 검토 후 complete 전환 가능
- 검수 준비 완료 37 IR·43 계약 재검증: IR 논리(명제·기간·함의·검증 사례) 오류 없음. **환자에게 보이는 계약 문구 결함 3건** 수정:
  1. 약제 계약 6개 목적격 조사 오류("리툭시맙(rituximab)를") -> 받침으로 을/를 선택 (전 계약 조사 검사 테스트 추가)
  2. QC-TRIAL-PARTICIPATION-LAST: '참여한 시기'가 시작 시점으로 읽혀 지금도 참여 중인 환자가 '3개월보다 전'을 고를 수 있음 (**잘못된 통과**) -> 마지막 참여 시점 + 현재 참여 안내
  3. QC-CANCER-VACCINE-EVER: '암 백신'이 예방 백신(자궁경부암 HPV 등)으로 읽힐 수 있음 (**잘못된 제외**) -> 치료용 명시
- 초안이 바뀐 IR·계약은 digest가 바뀌며, 표·검수 자료를 다시 생성했습니다. 승인된 것이 없으므로 무효화되는 승인은 없습니다.

승인 필요 항목 (AI가 승인하지 않음):
- 임상 검토 IR 13 (질문·필요 증거: 검수 자료 '임상 전문가 검토 필요' 절), 임상 검토 계약 9
- 기준 시점 근사 정책(first_dose·enrollment·randomization·screening -> 답변 시점) 결정 후 기준별 anchor_policy 추가
- 검수 준비 완료 37 IR·40 계약의 사람 검수 (체크리스트 7항목)
- 표현 불가 7: 확장 요건(미래 의향 양상, 절차 요건 표기, 면역조절 치료 약제 목록 등) 채택 여부

## G. E2E 검증 결과 (`tests/test_p8_e2e.py` 19개 + 기존 P5·P7·이전 테스트)

| 경로 | 결과 | 수준 |
|---|---|---|
| 처음 입력 -> Profiler -> 후보 -> 계약 질문 -> 선택지 답 -> 장부 -> 평가기 -> 병합 -> 상태 -> Inspector | 통과 | 세션 (`ContractSession.run_fixed`) |
| 정확한 계약 질문의 정상 판정, 두 시험 공유(한 번 질문) | 통과 | 세션 |
| 정확하지만 비계약 질문 -> UNVERIFIED, POTENTIAL_MATCH, 답 기록 보존, Inspector에 경계 표지 | 통과 | 세션 + Inspector |
| 잘못된 통과·제외 4방향 x 두 정책 | 모두 미확정 | 세션 |
| A~E 반례 | 미확정 | 매처 수준 (P1, 두 모드) |
| 처음 입력 정상 판정 보존 (E9 수정 포함, 활성 계약 있어도) | 통과 | 세션 |
| 진료 기록 필요 기준 + 계약 답 -> UNVERIFIED, Inspector outcome unverified | 통과 | 세션 + Inspector |
| 기간 경계 (4주보다 전 -> 해당 없음 / 4주 이내 수술 != 큰 수술 -> 미확정), 기준 시점 근사 없음 -> 판정 안 함 | 통과 | 세션 |
| 다른 세션 사실·같은 세션 ID로 저장소에 넣은 위조 사실 무시 | 통과 | 세션 |
| 계약 ID·명제 키 위조, 질문 문구 변조, 상충 사실, 코호트·요약 재계산 | 통과 | 단위·매처 (P3·P5·P7) |
| Inspector: 계약 출처 표, strict 값 숨김 / 합성 실행 값 표시, v1(계약 없는) 기록에 섹션 없음 | 통과 | Inspector |
| CLI `CTREC_SESSION=contract` 실행 -> result.json contract 절 -> `load_run` -> Inspector | 통과 | CLI (오프라인 대역) |
| 웹: 실행 생성 -> 질문 -> 답변 -> 결과(contract 절) / 대기 중 취소 | 통과 | HTTP (TestClient + 실행 스레드) |

미검증: 실제 브라우저 화면 조작·렌더링(`tests/test_web_browser.py`는 `CTREC_BROWSER_TESTS=1` + playwright가 필요한데 이 환경에
playwright가 없어 실행하지 않음, 패키지 설치는 하지 않음), 실제 LLM(Profiler·Matcher·질문 생성기) 출력,
승인된 실제 저장소 IR로 하는 실행(활성 IR 0개라 테스트 사본 사이드카로만 확인).

## H. 전체 테스트 결과

명령: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ANTHROPIC_API_KEY= python -m pytest -q -p no:cacheprovider` (유료 API 없음)

| 환경 | 통과 | 실패 | 건너뜀 | 실패 내역 |
|---|---|---|---|---|
| A. 원본 재설계 worktree (P7 상태, 보호 패치 없음) | 1253 | 0 | 1 | - |
| B. 스크래치 통합 (패치 01~05·07·08 + P8), 기본 정책 | 1371 | 1 | 2 | D: 보호 파일 검사 |
| F. 최종 후보 (B + Inspector 두 브랜치 + 06 v2), 기본 정책 | 1533 | 1 | 6 | D |
| C. F + `CTREC_SESSION=contract` + `CTREC_QA_POLICY=contract` | 1527 | 7 | 6 | 정책 A 4건(기존 Q&A 정책 테스트), 옵트인 A 2건(프로필 재추출 없음), D |
| C2. B + `CTREC_QA_POLICY=contract` (기존 Session + 경계) | 1367 | 5 | 2 | 정책 A 4건, D |
| D. 기준 65eaa0f | 1028 | 0 | 1 | - |
| E. qalink 사본 (미커밋 7파일 그대로) | 1085 | 1 | 1 | D (qalink의 pipeline·matcher 미커밋 변경) |
| E2. F + qalink 테스트 58개 (API shim) | legacy 28 / contract 20 | 30 / 38 | - | C절 표 |

- 건너뜀: 브라우저 E2E 1(playwright 없음), Inspector 심볼릭 링크 테스트 4(이 환경에서 링크 생성 불가), B·C2의 test_p8_e2e 모듈(Inspector 없음)
- 환경 C의 웹 테스트(재개·오류 처리·동시 답변·세션 격리 포함)는 ContractSession으로 위 2건 외 모두 통과
- 웹 타이밍 테스트 반복: 취소 2종 + 계약 모드 웹 흐름, 환경 F 기본 20회·계약 모드 20회 -> 실패 0/40. P7의 1회 실패는 전체 실행 부하 중에만
  관찰됨 (P7 단독 반복 0/30·0/20, 기준 사본 0/30 포함) -> 스레드 대기 시간 의존(환경 요인)으로 판단, 코드 결함 근거 없음
- 신규 P8 테스트: E9 41, 비답변 56, 정책 이전 16, 검수 자료 6, E2E 19 (계약 관련 전체 225 + P8 138)
- Frozen 해시 26/26 일치, `data/` 변경 없음, 모든 worktree·브랜치 ref 작업 전후 동일 (스냅샷 비교)

## I. 통합 승인 체크리스트 (실제 브랜치 반영 전)

- [ ] 정책: 계약 정책을 서비스 기본으로 할지 결정 (현재 후보는 기본 legacy, 선택 시 contract). 결정 전에는 03 v2를 넣어도 동작 불변
- [ ] qalink: '연결 답변 확정' 확장을 넣지 않는 결정, qalink 브랜치 처리(보존·종료) 결정, E9 패치 07과 qalink E9 변경의 4개 hunk 조율
- [ ] 보호 파일 승인: 01(Profiler 프롬프트), 02(session_id), 03 v2(정책 선택) - 보호 파일 검사 테스트 절차에 따라
- [ ] 기존 테스트 4건: 기본 정책 유지 시 변경 불필요. 계약 정책을 기본으로 바꿀 때만 기대값 변경 승인 (E절 표가 근거)
- [ ] 옵트인 모드 테스트 2건(`test_web_integration::test_full_flow…`, `test_team_integration::test_all_answer_types…`): ContractSession은 답변 뒤 프로필을 다시 추출하지 않음 -> 옵트인 채택 시 기대값 결정
- [ ] Inspector 작업자 확인: 06 v2 (strict 값 숨김 포함)
- [ ] CLI: Inspector 기록 브랜치를 먼저 합치면 04 대신 04b
- [ ] `schemas.py` qalink와 수동 병합 (같은 session_id 줄, `"ambiguous"` Literal은 넣지 않음)
- [ ] 임상 검수(F절)와 기준 시점 정책 - 승인 전까지 결정적 판정 0건 (현재 상태 유지)
- [ ] 실제 LLM 섀도 비교 (유료 실행 승인)
- [ ] 반영 후 스모크: 기본값 전체 테스트, `CTREC_SESSION=contract` 전체 테스트, Frozen 해시 26개, 브라우저 수동 확인

## J. 잔여 위험 및 다음 단계

- 실제 LLM: Profiler(패치 01) 출력, Matcher가 비계약 답변을 어떻게 인용하는지, 질문 생성기의 기준 이탈 빈도는 결정적 대역으로만 확인
- 정상 판정 손실: 계약 정책은 사례 B(정확한 비계약 질문)의 올바른 판정도 하향 -> 계약 정책을 기본으로 하면 검수 전까지 UNVERIFIED 증가.
  우선 검수 대상: 공유가 많은 계약(QC-PREGNANT-NOW 8개 기준, QC-LACTATING-NOW 8개)
- 기존 Session의 계약 정책(03 v2)은 머리말 모드이고 Profiler 재추출 경로는 막지 않음 -> 완전한 계약 정책은 ContractSession
- 옵트인 UX: 답변 뒤 프로필이 갱신되지 않아 비계약 답(예: 검사 수치)이 판정·화면에 반영되지 않음
- E9 표기 판별 한계(띄어 쓴 '음성'·'부인'), qalink `_objects` 변경 미검증
- 임상: 기준 시점 근사, 임상 검토 IR 13·계약 9, 자기 보고 '임신 아님'의 충분성
- 브라우저 화면: 계약 출처 표·UNVERIFIED 표시의 실제 렌더링 미확인
- 다음 단계: 정책 회의(I절 1·2) -> 커밋·보호 파일 승인 -> 임상 검수 우선순위 -> 섀도 비교
