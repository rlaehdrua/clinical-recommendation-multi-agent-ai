# P13 P12 Q&A 재평가 + Inspector 직접성 표시 통합 보고서

작성 2026-10-25. push·배포·유료 API 호출·IR/계약 승인·Frozen Store 수정·다른 worktree 변경 없음. 프론트엔드 파일·`web/app.py` 수정 없음.

## 1. 작업 시작 시 Git 상태

| 브랜치 | HEAD | worktree (`C:\Users\jsw58\Desktop\산학협력 더미\…`) | 미커밋 |
|---|---|---|---|
| `integration/midterm-1026` (기준) | d2496a3 | `workseoyeon-midterm` | 없음 |
| `fix/qa-reevaluation-safety` (P12) | 315d0be | `workseoyeon-qa-reevaluation` | 없음 |
| `feat/inspector-directness-display` | af6b6c3 | `workseoyeon-inspector-directness` | 없음 |
| `integration/frontend-backend-final` | af6b6c3 | `workseoyeon-frontend-integration` | 없음 |

기준 브랜치에 새 커밋 없음 (d2496a3 그대로). 그 밖의 worktree(qalink 미커밋 7, 재설계 61, 통합 후보 91 등)는 읽지도 바꾸지도 않음.

## 2. 커밋 그래프

```
*   5220219 merge: P12 QA re-evaluation safety (fix/qa-reevaluation-safety, 315d0be)
|\
| * 315d0be docs: add P12 QA re-evaluation report
| * 86252d2 fix: use verified yes/no answers when re-evaluating answered criteria
* | af6b6c3 feat: show evidence directness diagnostics in decision inspector
|/
* d2496a3 (기준) docs: add P11 frontend integration report
```

- af6b6c3은 d2496a3의 자식 커밋 1개. `feat/inspector-directness-display`와 `integration/frontend-backend-final`이 같은 커밋을 가리키는 것은
  프론트엔드 통합 브랜치가 그 커밋으로 fast-forward됐기 때문 (별도 변경 없음) -> **한 번만 반영**
- P12(315d0be)는 d2496a3에서 분기. 두 작업의 수정 파일은 겹치지 않음

## 3. af6b6c3 구성

`src/ctrec/inspector.py` (+113/-10), `tests/test_inspector_directness.py` (신규 10개). 작성자 jsw5855, 2026-10-25 18:39.
기준 상세 표에 기록된 `evidence_directness`·`validation_flags`·`llm_judgment`(허용 키만)를 표시하고 라운드 변화를 판정·근거 변화와 분리해 셈.
trace에 필드가 없으면 '기록 없음'(0이 아님).

## 4. P12 감사 결과

| 항목 | 결과 |
|---|---|
| Session이 질문 기록을 matcher에 전달 | `Session.match`가 `qa=list(self.qa_log)`, `session_id` 전달 (기존 Session·ContractSession 모두 이 경로) |
| 같은 세션·라운드만 연결 | `session_id` 일치 + 원문 답변 블록의 라운드 일치 필요 (세션 없는 기록 제외) |
| criterion·질문 ID 보존 | target `T:R` 일치, 근거 = 원문 그대로의 `Q: … A: …`, reasoning에 `r{라운드}:{질문 ID}`, flags `answer_verified` |
| 예/아니요 단답만 | 긴 서술·모름·거부·추측은 대상 아님 |
| 극성·기간·대상 | 부정형·조건·인과·여러 질문 제외, 기간·대상 포함 관계 검사, 개념군 기준만 |
| 인용 검증 우회 여부 | 인용 검증·짧은 답 연결에서 **되돌려진** 확정 판정에만 실행. 근거를 원문 문답으로 바꿀 뿐 LLM 인용을 인정하지 않음 |
| E9 마지막 통과 | 필수 (`evidence_directness.check`, 처음 입력과의 상충 포함) |

잘못된 판정 방지 재확인 (테스트 근거): 무관한 질문, 다른 기준·시험·세션·목적, 평생 vs 최근 4주, **EGFR TKI vs EGFR ADC**(P13 신규 테스트,
개념군 밖이라 `criterion_not_verifiable`), 항암 치료 vs 그로 인한 입원, 임신 vs 임신 검사, 모름·거부·무응답·확인 불가, 답 수정·다른 질문의 반대 답,
원문과 상충, 허위 근거(판정 방향 불일치), 미승인 계약(contract 정책 UNVERIFIED) - 모두 확정하지 않음.

**legacy 동작 변화 (명시):** 개념군 기준(임신·수유, 알레르기·금기 중 대상 이름이 있는 것)에서, 모델이 확정했지만 인용 검증에서 되돌려지던 판정이
위 검증을 통과한 예/아니요 답으로 확정됨. 새 근거가 처음 입력보다 우선하지 않음(처음 입력과 상충하면 E9 `conflicting`으로 확정 안 함).
모델 판정과 답의 방향이 다르면 인정하지도 뒤집지도 않음. 그 밖의 판정은 같음 (기존 테스트 결과 변화 0).

## 5. Inspector 감사 결과

| 항목 | 결과 |
|---|---|
| 새 판정 수행 | 하지 않음 - 기록 값 표시. LLM 원래 판정과 최종 해당 여부가 다르면 '다름' 표시만 |
| 직접성 표시 | 기록된 `evidence_directness` 그대로 |
| P12 표지 | `answer_verified`, `answer_unverified:<사유>`(콜론 포함) 모두 코드 형식으로 그대로 표시 - '<text>'로 가려지지 않음 (테스트) |
| 판정 불변 | Inspector 화면 데이터의 passes·holds·직접성·사유가 result.json 판정과 같음, strict/synthetic 같은 값 (테스트) |
| v1/v2 호환 | 필드 없는 기록은 '기록 없음' (af6b6c3 테스트), 계약 절(v2) 기존 테스트 통과 |
| XSS·escape | 기존 Inspector 테스트·af6b6c3 테스트 통과 (자유 문장 비노출) |
| strict | 근거(문답 원문)·질문·답변 문장 비노출 (P13 테스트) |
| 누락 필드 | 보수적으로 '기록 없음' / 라운드 비교 '기록되지 않음' |

남은 제한(이번에 바꾸지 않음): `pipeline.trace_match`가 trace에 직접성 필드를 싣지 않아 라운드별 직접성 변화는 '기록되지 않음'으로 표시됨.
최종 판정의 직접성·사유는 result.json에서 보임.

## 6. 충돌 및 해결

Git 텍스트 충돌 0. 의미적 충돌 0 (P12 표지를 Inspector가 그대로 표시, 같은 기준에 다른 상태 표시 없음, 질문 ID·출처 유실 없음 - P13 테스트).

## 7. 통합 과정에서 수정한 파일

- `tests/test_p13_qa_inspector.py` (신규 5개): 재평가 결과와 Inspector 표시 일치, EGFR TKI/ADC 혼동 방지, strict/synthetic 판정 동일, P12 사유 코드 형식
- 이 보고서
- 코드 수정 없음 (af6b6c3·P12 그대로)

## 8. 테스트 결과 (통과 / 실패 / 건너뜀)

| 환경 | P12 | P13 통합 후보 | 차이 |
|---|---|---|---|
| legacy 전체 | 1695 / 0 / 6 | **1710 / 0 / 6** | +10 Inspector 직접성(af6b6c3), +5 P13 통합. 기존 결과 변화 없음 |
| ContractSession + contract | 1688 / 6 / 6 | 1704 / 6 / 6 | 정책 차이 6 동일 |
| 기존 Session + contract | 1690 / 4 / 6 | 1706 / 4 / 6 | 정책 차이 4 동일 |
| 핵심 묶음 (Inspector 3·웹 보안·P1 경계·P12·E9 4개 파일) | - | 448 / 0 / 4 | P0 A~E 포함 |
| 실제 브라우저 (Edge, Playwright 스크래치 설치) | 14 / 18 | 14 / 18 | **실패 18건이 P11 기준선과 같은 목록** (새 실패 0) |

## 9. Legacy와 Contract 정책 차이

contract 정책 실패 6·4건은 P9부터 같은 정책 차이(legacy 기대값을 기록한 기존 테스트 4 + ContractSession 옵트인 2). P12 재평가 확정은 contract 정책에서
`qa_uncontracted`로 UNVERIFIED (미승인 계약은 결정적 판정을 만들지 않음). 기본 정책은 legacy, 활성 IR 0.

## 10. Frozen Store

`verify_frozen()` 문제 0 (26개 해시), `data/` 변경 없음.

## 11. 프론트엔드 영향

d2496a3 대비 바뀐 파일: `agents/matcher.py`, `pipeline.py`, `answer_verification.py`, `inspector.py`, 테스트 3개, 문서 2개. `web/` 아래 변경 없음 ->
API 경로·응답 구조·SSE 이벤트 종류 그대로, Inspector는 웹에 노출되지 않음 (오프라인 도구). 브라우저 E2E 새 실패 없음.

## 12~13. 기준 브랜치 반영과 최종 상태

반영 결과·최종 HEAD는 최종 응답과 `git log`에 기록 (이 보고서 커밋 뒤 fast-forward).
통합 브랜치 `integration/qa-inspector-p13`, worktree `산학협력 더미\workseoyeon-qa-inspector-p13` (P12·Inspector 브랜치를 바꾸지 않으려고 새로 만듦).

## 14. 미해결 위험과 다음 작업

- 개념군 밖 기준(EGFR ADC 등)은 답해도 미확정 - 질문 계약·IR 임상 검수 후 ContractSession
- `origin/main` b11ec29와 matcher·pipeline 충돌 예정 (P12 보고서 13절)
- 브라우저 E2E 18건(기존, 프론트엔드 소유), trace의 직접성 필드 부재, 실제 LLM 미검증
- 원격 공유 전: 보호 파일(matcher·pipeline) 변경 승인 확인
