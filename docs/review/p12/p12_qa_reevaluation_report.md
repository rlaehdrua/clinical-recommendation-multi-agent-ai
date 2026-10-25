# P12 Q&A 재평가 누락 해결 보고서

작성 2026-10-25. 브랜치 `fix/qa-reevaluation-safety` (worktree `C:/Users/jsw58/Desktop/workseoyeon-qa-reevaluation`).
기준 브랜치로 병합·fast-forward하지 않았습니다. push·배포·유료 API 호출 없음.

## 1. 시작 기준

- `integration/midterm-1026` = `d2496a3` (P11), 기준 worktree 깨끗함. 새 브랜치는 d2496a3에서 생성
- 다른 작업: `feat/inspector-directness-display`(worktree `workseoyeon-inspector-directness`, `inspector.py` 미커밋 수정 + 신규 테스트) - 읽기만,
  이 작업은 `inspector.py`를 건드리지 않음. 그 밖의 worktree 미커밋 변경(qalink 7, 재설계, 통합 후보)도 그대로
- `origin/main` = 9fdffc4 (b11ec29 포함), 병합·cherry-pick하지 않음

## 2. 재현된 증상 (실제 코드, 오프라인)

SYN-DEMO-A 실행(팀원 보고)과 같은 형태: 환자가 "현재 임신 중이거나 수유 중인가요?"에 "아니요"라고 답했고 Matcher도 'no'로 판정했지만 최종 '정보 부족'.

| 재현 | 결과 (수정 전) | 단계 |
|---|---|---|
| 모델이 근거를 의역 ("임신·수유 아님") | `quote_not_in_source` -> unknown | `_enforce_quote` (Matcher는 재추출 프로필만 봄 -> 답 원문을 인용할 수 없음) |
| 모델이 `Q: … A: 아니요`를 그대로 인용 | `answer_not_linked` -> unlinked | E9 짧은 답변 연결: `Session.match`가 질문 기록을 넘기지 않음 |
| Session E2E (질문 -> 답 -> 재매칭) | E4 `passes=None`, 판정 불가 1 | `test_session_reevaluation_uses_verified_answer_and_keeps_provenance`가 수정 전 코드에서 이 이유로 실패 |

데이터 흐름에서 사라지는 지점: 질문 ID·대상(`T:E4`)·라운드·`session_id`는 `Session.qa_log`(QAPair)에 있고 원문에도 `[추가 확인 정보 - 라운드 N]`
블록으로 붙지만, `Session.match -> match_trial` 호출에서 전달되지 않음. Matcher 입력(프로필)에는 답 원문이 없음. 명제 키는 기존 Session 경로에
없음(ContractSession·질문 계약에만).

## 3. 버그와 정상 UNVERIFIED의 구분

| 경우 | 분류 | 수정 후 |
|---|---|---|
| 기준이 개념군(임신·수유, 알레르기·금기)이고 질문이 같은 대상·같은 기간을 예/아니요로 물었는데 연결이 누락 | **버그 (전달 누락)** | 검증 후 확정 (`answer_verified`) |
| 기준이 개념군 밖 (EGFR ADC 치료 이력, 최근 4주 수술, 최근 항암 치료 등) | **정상 보류** - 질문과 기준의 의미 대응을 코드로 결정할 수 없음 ("EGFR TKI 치료를 받은 적이 있나요?"도 EGFR이라는 낱말은 같음) | 미확정 유지 (`answer_unverified:criterion_not_verifiable`). 승인된 질문 계약 + IR(ContractSession) 경로가 담당 |
| 질문 기간이 기준 기간을 덮지 않음, 검사 여부 질문, 다른 사건을 끼운 질문, 부정·추측 질문, 여러 질문, 대상 불명 | 정상 보류 | 미확정 + 사유 코드 |
| 모름·거부·확인 불가, 답 수정, 다른 질문의 반대 답, 처음 입력과 상충 | 정상 보류 (검토 대상) | 미확정 + 사유 코드 |
| 진료 기록이 필요한 기준 | 정상 UNVERIFIED | UNVERIFIED + 답변상 판정(`self_reported_holds`) 보존 (기존 정책) |

팀원 재현 사례 중 E4(임신·수유)는 버그로 해결, E2(EGFR ADC)는 정상 보류로 남습니다.

## 4. b11ec29가 위험한 이유 (코드 근거)

1. `_enforce_quote`: 인용이 원문에 없어도 그 기준을 겨냥한 답이 원문에 있으면 **답 내용과 무관하게** 판정을 인정하고 근거를 문답으로 바꿈 -
   답 '아니요' + 모델 판정 'yes'도 인정됨(극성 미확인), 기간·대상 대조 없음. 대상(target)은 질문이 무엇을 겨냥했다는 식별 정보일 뿐 증거가 아님
2. `_answers_block`: Matcher 프롬프트에 답변 원문을 넣음 -> legacy 프롬프트·캐시 키·비용 추정이 바뀌고, 재설계에서 제거한 경로를 다시 엶
3. `matcher.DECLINED_ANSWERS`: 무응답 판별 중복 ('답변하지 않겠습니다'·'확인할 수 없습니다'가 답으로 취급됨)
4. 질문 의미 대조가 없으므로 "수술 부작용이 있었나요? 아니요"를 수술 기준에, "임신 검사를 받았나요? 아니요"를 임신 기준에 쓸 수 있음

## 5. 원인과 수정한 데이터 흐름

```
질문 생성(target=T:E4, purpose) -> 답변 -> Session.qa_log (QAPair: round·question_id·target·session_id)
  -> [수정] Session.match가 qa=qa_log, session_id를 match_trial에 전달
  -> LLM 판정 -> _enforce_evidence -> _enforce_quote / E9
  -> [수정] 되돌려진 확정 판정(quote_not_in_source / answer_not_linked)만 answer_verification.verify 실행
       통과 -> 근거 = 원문 그대로의 "Q: … A: …", flags ["answer_verified"], reasoning에 r{라운드}:{질문 ID}
       실패 -> 기존대로 판정 불가 + flags "answer_unverified:<사유>"
  -> _apply_evidence_policy (답변 = self_report: 진료 기록 필요 기준은 UNVERIFIED) -> qa_guard (contract 정책이면 UNVERIFIED)
  -> MatchStatus·trace(matcher done)·result.json
```

`verify`의 검증 항목 (모두 충족해야 인정): 같은 세션·적격성 목적·이 시험의 이 기준을 겨냥한 문답 / 원문 답변 블록에 같은 라운드의 질문·답 그대로 /
예·아니요 단답(무응답·추측 제외) / 개념군 기준 + 대상 이름 있음 + 기준 극성 확인 가능 / 질문에 개념어, 부정형·추측·조건·인과·여러 질문 아님,
검사 여부 질문과 상태 기준을 섞지 않음 / 대상: '없음'이면 질문 대상이 기준 대상을 모두 덮고, '있음'이면 질문 대상이 기준 안 (여럿이면 기준이 '또는')
/ 기간: '없음'이면 질문 기간 ⊇ 기준 기간, '있음'이면 질문 기간 ⊆ 기준 기간 (현재 ⊂ 최근 N ⊂ 평생) / 같은 기준을 겨냥한 다른 답이 모두 해석 가능하고 같은 방향 /
판정 방향은 답으로 정하고 LLM 판정이 다르면 인정하지 않음(뒤집지도 않음) / 마지막으로 기존 E9 검사 통과(처음 입력과 상충·대상 불일치 차단).

## 6. 변경 파일

| 파일 | 소유 | 변경 |
|---|---|---|
| `src/ctrec/answer_verification.py` (신규) | 백엔드 Q&A 검증 | 위 검증 |
| `src/ctrec/agents/matcher.py` (보호 파일) | 백엔드 판정 | `match_trial(qa=, session_id=)` 선택 인자, 되돌려진 판정에만 검증 호출 (+13줄) |
| `src/ctrec/pipeline.py` (보호 파일) | 백엔드 Session | `Session.match`가 `qa`·`session_id` 전달 (1줄 -> 2줄) |
| `tests/test_p12_qa_reevaluation.py` (신규) | 테스트 | 35개 |
| `docs/review/p12/p12_qa_reevaluation_report.md` | 문서 | 이 보고서 |

수정하지 않음: `web/app.py`, `web/static/*`, `tests/test_web_browser.py`, `inspector.py`, `evidence_directness.py`, `question_contract.py`,
`schemas.py`(QAPair 그대로), Frozen Store·환자 데이터. 보호 파일 2개 변경은 보호 파일 검사가 미커밋 상태에서 탐지(아래 10절) - 승인 범위 확인 필요.

## 7. API 호환성

- 웹 API·SSE·result.json 형식 변경 없음. `QAPair`·`CriterionAssessment` 스키마 그대로 (새 값은 `validation_flags` 문자열과 `reasoning` 머리말뿐)
- `match_trial` 기존 인자(`qa_log`) 의미 그대로 - E9 직접 호출 테스트(`test_evidence_directness_counterexamples`) 결과 불변
- 인자 이름 `qa`는 b11ec29와 같게 둠 (나중에 origin/main 병합 시 `pipeline.py` 호출 줄 충돌을 줄임)
- 프론트엔드 변경 필요 없음. 화면이 `answer_unverified:*` 사유를 보여 주려면 표시 문구 매핑이 필요 (선택 사항, 프론트엔드 담당)

## 8. 정상 재평가 테스트 결과 (모두 통과)

의역 근거·그대로 인용 두 경우 모두 확정 + 근거가 문답 원문 + `self_report` + `answer_verified` + `r1:Q1` / '예' 답이 질문 극성대로 존재(제외 기준 불충족) /
평생 '없음'이 현재 '없음'을 덮음 / 같은 답 재전송·순서 바뀜에도 같은 결과 / Session E2E: 질문 -> 답 -> 재매칭 -> 판정 불가 0, trace(matcher done)에도 반영,
`session_id`는 직렬화되지 않음.

## 9. 안전성 반례 테스트 결과 (모두 확정하지 않음)

| 반례 | 사유 코드 |
|---|---|
| 질문과 무관한 답 ("수술 후 부작용", "감기 증상", 임신 기준에 알레르기 질문) | `question_concept` |
| 평생 수술 이력 답으로 최근 4주 수술 기준 | `criterion_not_verifiable` |
| 최근 4주 반응 없음으로 평생 알레르기 기준 / 평생 '예'로 현재 임신 | `time_scope_mismatch` |
| 항암 치료 때문에 입원 -> 최근 항암 기준 / 임신 때문에 입원 | `criterion_not_verifiable` / `question_form` |
| 임신 검사 미실시 -> 임신 아님 | `test_vs_condition` |
| 모름·거부·확인 불가·공백·기억 안 남 | 확정 안 됨 |
| 답 수정 (라운드 1 아니요 -> 라운드 2 예), 다른 질문의 반대 답 | `conflicting_answers` (마지막 답 채택 안 함) |
| 처음 입력 "현재 임신 12주" + 답 아니요 | `directness:conflicting` |
| 다른 기준·다른 시험·다른 세션·세션 없는 기록·의료진 참고 질문·세션 ID 없음 | 확정 안 됨 |
| 기록과 원문 불일치(위조된 기록), 라운드 불일치 | `answer_not_in_source` |
| 답과 반대인 모델 판정 (허위 근거) | `judgment_direction` |
| 부정 의문문, 가능성 질문, 여러 질문 | `question_form` |
| '또는' 질문의 '예'로 단일 대상 기준 | `object_scope` |
| "Negative pregnancy test" 기준 | 확정 안 됨 |
| 해당 없음·추론 판정 상향 | 하지 않음 |
| contract 정책 (미승인 계약) | `qa_uncontracted` -> UNVERIFIED |
| 진료 기록 필요 기준 | UNVERIFIED + `self_reported_holds="no"` |

팀원 b11ec29 테스트 10개를 이 브랜치에서 실행(스크래치 사본): 7 통과(모름·다른 기준·다른 시험·의료진 질문·원문에 없는 문답·unknown 상향 금지·
빈 블록), 3 실패는 설계 차이 - EGFR ADC 의역 인정(개념군 밖: 검증 불가), 프롬프트 답변 블록(도입 안 함), `matcher.DECLINED_ANSWERS`(중복 목록 도입 안 함).

## 10. 전체 테스트

명령: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ANTHROPIC_API_KEY= .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`

| 환경 | P11 기준 | P12 (커밋 전) | 차이 |
|---|---|---|---|
| legacy | 1660 / 0 / 6 | 1694 / 1 / 6 | +35 신규 통과. 실패 1 = 보호 파일 검사(matcher·pipeline 미커밋 탐지, 커밋 후 통과 - 승인과 별개) |
| ContractSession + contract | 1654 / 6 / 6 | 1688 / 7 / 6 | 정책 차이 6 동일 + 보호 파일 검사 |
| 기존 Session + contract | 1656 / 4 / 6 | 1690 / 5 / 6 | 정책 차이 4 동일 + 보호 파일 검사 |

(통과 / 실패 / 건너뜀) 기존 테스트 중 결과가 바뀐 것 없음 - P0 A~E 반례, E9·무응답, P8~P10 계약·Inspector·E2E·웹 보안 포함. 커밋 후 결과는 최종 응답에 기록.

## 11. Inspector·Frozen Store

- Inspector 코드 변경 없음. 화면은 기존 `validation_flags`·근거·판정 열로 `answer_verified` / `answer_unverified:*`와 문답 원문 근거를 그대로 보여 줌
  (strict 모드의 답변 원문 숨김 정책은 Inspector 쪽 그대로)
- Frozen Store: `verify_frozen()` 문제 0, `data/` 변경 없음

## 12. 해결된 문제와 남은 문제

- 해결: 개념군 기준에서 질문 대상·기간이 기준과 맞는 예/아니요 답이 연결 누락으로 '정보 부족'에 남던 문제 (Session 경로, 출처 보존)
- **legacy 동작 변화 (명시)**: 위 경우 기존에는 판정 불가였던 기준이 확정됨. 그 밖의 판정은 같음 (전체 테스트 결과 불변)
- 남음: 개념군 밖 기준(EGFR ADC 등)은 답해도 미확정 - 질문 계약(QC-EGFR-ADC-EVER 등) + IR 임상 검수 후 ContractSession으로 해결.
  긴 서술 답은 기존 인용 경로(모델이 원문을 인용해야 함)에 그대로 의존. 알레르기 질문은 '…한 적이 있나요'처럼 이력을 묻는 형태여야 '없음'을 인정
- 실제 LLM 출력으로는 미검증 (결정적 대역)

## 13. 팀원과 병합 시 주의

- `origin/main`(b11ec29)과 `agents/matcher.py`·`pipeline.py`에서 텍스트 충돌 예상. b11ec29의 `_enforce_quote` 보완·`_answers_block`·`DECLINED_ANSWERS`는
  이 브랜치의 검증과 겹치므로 하나만 남겨야 함 (권장: 이 브랜치의 검증, b11ec29 테스트 중 안전성 7개는 그대로 통과)
- `pipeline.py` 호출의 `qa=self.qa_log`는 같은 이름 (이 브랜치는 `session_id`도 넘김)
- `inspector.py`는 다른 작업(`feat/inspector-directness-display`)이 수정 중 - 이 브랜치는 건드리지 않음
- 화면의 사유 문구 표시는 프론트엔드 담당 (API 변경 없음)

## 14. 최종 상태

브랜치 `fix/qa-reevaluation-safety`, 기준 d2496a3 위 로컬 커밋 (커밋 해시는 최종 응답과 `git log`). 기준 브랜치에 반영하지 않음.
