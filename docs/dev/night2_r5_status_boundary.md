# 2차 야간 R5: 엔진·Session·웹·추천 상태 경계 (2026-11-07)

기준: `origin/integration/dcvc` = `b2100d8`. 참조: `night/status-consistency-audit` `c0981b9`(읽기 전용), `f45ab65`.
브랜치: `night2/status-boundary` (로컬, push 안 함).

## 결론

C2는 **표현 계층에서만** 막았습니다. 엔진 판정을 고친 것이 아닙니다.

- `eligibility`, 기준별 판정, Fact Store, PairStatus, `Session.matches`, `Session.recommendation`은 바뀌지 않습니다.
- 추천에서 내려간 시험의 판정은 여전히 `ELIGIBLE`입니다. 불충족 표시를 낮춘 시험의 판정도 여전히 `INELIGIBLE`입니다.
- 사용자가 읽는 웹, CLI, 저장 보고서, 오케스트레이터 응답은 표현 사본을 씁니다. 원천 기록은 원래 값을 유지합니다.

## 표현상 보류와 원천 판정 변경의 차이

| 구분 | 표현상 보류 (이번 수정) | 원천 판정 변경 (안 함) |
|---|---|---|
| 바뀌는 값 | 사본의 `match_status`, 추천 그룹, 순서, 문구 | `eligibility`, 기준 판정, 사실, PairStatus |
| 평가·지표 | 영향 없음 (`metrics`는 `result.json`의 원래 값을 읽음) | 영향 있음 |
| 질문 라운드 | 영향 없음 | 영향 있음 |
| LLM 입력 | 영향 없음. 승인 패치에서만 바뀜 | 영향 있음 |
| 되돌리기 | 표현 계층 호출만 제거 | 판정 재실행 필요 |

## 경로별 C2 상태

| 경로 | 이번 브랜치 | N5 | 비고 |
|---|---|---|---|
| 웹 result 이벤트, SSE 재생 | **해결** | 해결 | 상태, 그룹, 순서, 시험별 문구 |
| 웹 진행 중 표시 | **해결** | 미해결 | `CRITERIA_MATCH`를 '중간 판정'으로 표시 |
| 웹 '추천 없음'·제외 분류 | **해결** | 미해결 | 다툼 중인 불충족을 맞지 않음으로 세지 않음 |
| CLI 출력 | **해결** | 미해결 | |
| 저장 `report.md` | **대부분 해결** | 미해결 | 원본은 `report.raw.md`에 보존 |
| 웹 보고서 사본 | **대부분 해결** | 안내문만 | 코드가 쓴 줄과 시험을 가리키는 줄을 고침 |
| 오케스트레이터 종료 응답 | **해결** | 미해결 | 모델이 받는 상태 |
| 저장 `result.json` | **표식만** | 미해결 | 원래 값 유지 + `engine_boundary` 추가, 보류 시 `conservative_view.json` 생성 |
| `Session.recommendation` | **미해결** | 미해결 | 승인 패치 필요 |
| 추천·보고서 LLM 입력 | **미해결** | 미해결 | 승인 패치 필요 |
| 보고서의 시험 미지칭 LLM 문장 | **미해결** | 미해결 | 맨 위 안내가 우선한다고 표시 |
| Inspector, `trace.jsonl`, `eligibility_history` | **미해결** | 미해결 | 연구·감사 기록이라 의도적으로 원래 값 유지 |
| 웹 이벤트의 `eligibility` 필드 | 의도적 유지 | 유지 | 화면에 쓰지 않지만 API 소비자에게는 보임 |

`/cohort` 현황판은 Session 결과를 쓰지 않고 공개 코호트로 엔진 상태를 직접 계산하므로 이 경로에 해당하지 않습니다.

## 코드 수정 범위

| 파일 | 내용 |
|---|---|
| `src/ctrec/conservative_view.py` (신규) | 판단(`evaluate`)과 표현 사본(`build`, `adjust_display`, `save`) |
| `src/ctrec/web/app.py` | 결과 전송, 저장, 진행 중 표시 |
| `src/ctrec/web/static/app.js` | '추천 없음' 분류 문구 1개 |
| `src/ctrec/cli.py` | 출력과 저장 |
| `src/ctrec/orchestrator.py` | 종료 응답 |

보호 파일, PR #20 소스, N2·R2 환자값·근거 코드는 바꾸지 않았습니다.

`Session`을 상속해 `finalize` 안에서 `self.matches`를 잠시 바꾸면 보호 파일을 고치지 않고도 LLM 입력을 바꿀 수 있습니다. 보호 규칙을 우회해 추천 동작을 바꾸는 방식이므로 택하지 않았습니다.

## 상태 교차 점검

| 판정 / 외부 상태 | 엔진 상태 | 표시 | 판단 |
|---|---|---|---|
| ELIGIBLE / CRITERIA_MATCH | CONFIRMED_ELIGIBLE | 그대로 | 일치 (대조군) |
| ELIGIBLE / CRITERIA_MATCH | VERIFICATION_PENDING (conflict) | 낮춤 | 불일치 → 표현 보류 |
| ELIGIBLE / CRITERIA_MATCH, 필수 기준 0개 | NOT_EVALUATED | 그대로 | **불일치 유지.** R5 P2/P3 미결 |
| INELIGIBLE / CRITERIA_MISMATCH, 문서 검증 | CONFIRMED_INELIGIBLE | 그대로 | 일치 (대조군) |
| INELIGIBLE / CRITERIA_MISMATCH, 근거가 상충뿐 | VERIFICATION_PENDING | 의료진 검토 필요, 추천 안 함 | 불일치 → 표현 보류 |
| INELIGIBLE / CRITERIA_MISMATCH, 문서 검증 전 | VERIFICATION_PENDING (exclusion_unverified) | 그대로 | **불일치 유지.** R2와 같은 축 |
| UNCERTAIN / INSUFFICIENT_INFORMATION | INFO_MISSING | 그대로 | 일치 |
| UNCERTAIN / POTENTIAL_MATCH, 자기 보고 + 상충 | VERIFICATION_PENDING (conflict) | 그대로 | 확정은 아니나 "충족 가능"이 낙관적 |
| NOT_EVALUATED | NOT_EVALUATED | 그대로 | 일치 |

실제 입력에서는 `document_verified`가 거의 생기지 않으므로, 문서 검증 전 불충족 행은 대부분의 불충족 시험에 해당합니다. 적격 쪽은 문서 없이 CONFIRMED_ELIGIBLE이 되는데 불충족 쪽은 문서가 있어야 확정이라는 비대칭도 여기서 드러납니다. R2 소유이므로 기록만 했습니다.

엔진 대조 결과는 시험마다 `engine_boundary`에 남습니다. 남은 불일치는 `unresolved`의 `no_required_criteria_policy_open`과 `exclusion_not_document_verified`로 표시됩니다.

## 증거 상태별 결과 (`clinical_record`, 실제 Session)

| 증거 | 사실 관계 | 원천 판정 | 표시 |
|---|---|---|---|
| 같은 값, 다른 날짜 | `consistent` | ELIGIBLE | 그대로 |
| 다른 값, 다른 날짜, 최신 통과 | `time_dependent` | ELIGIBLE | 기록 확인 시 충족 가능 |
| 다른 값, 다른 날짜, 최신 탈락 | `time_dependent` | INELIGIBLE | 의료진 검토 필요, 추천 안 함 |
| 다른 값, 같은 날짜 또는 날짜 없음 | `contradictory` | UNCERTAIN | 원래도 확정 아님 |
| 상반된 바이오마커, 별개 변이 | `not_compared` | ELIGIBLE | 의료진 검토 필요. 둘을 구분하지 못함 |
| 다른 표적 바이오마커 | `not_compared` | ELIGIBLE | 그대로 |
| 기록과 답변 상충, 같은 날짜 | `contradictory` | UNCERTAIN | 원래도 확정 아님 |
| 기록과 답변 상충, 다른 날짜 | `time_dependent` | UNCERTAIN | 원래도 확정 아님 |
| 마지막 매칭 뒤 저장소에 생긴 출처 상충 | `contradictory` | ELIGIBLE | 의료진 검토 필요 |

마지막 행은 저장소에 사실을 직접 넣어 만든 상태입니다. 실제 흐름에서 생기는지는 확인하지 못했습니다.

## 최소 승인 패치

`docs/dev/night2_patches/pipeline_finalize.patch`: `pipeline.finalize`만 수정합니다. 보호 파일이므로 적용하지 않았습니다.

- **내용:** `recommend` 호출 전에 `evaluate`로 보류를 구해 외부 상태만 낮춘 사본을 넘깁니다. `self.matches`는 그대로입니다.
- **효과:** `Session.recommendation`, 저장 `result.json`의 `recommendation`, 추천·보고서 LLM 입력이 해결됩니다. 시험 미지칭 LLM 문장도 입력 단계에서 사라지는 방향입니다.
- **필수 테스트:** `test_session_recommendation_itself_is_held`, `test_report_llm_is_not_told_the_held_trial_is_a_match`의 xfail 해제.
- **주의:** LLM 입력이 바뀌므로 실제 API 품질 검증은 별도로 필요합니다. 이 패치는 스텁으로만 검증했습니다.

## N5와의 충돌·중복

| 항목 | 결과 |
|---|---|
| N5 전체를 병합 | `web/app.py` 충돌 |
| C2 커밋 `958536d` | **이 브랜치와 중복.** 이 브랜치가 범위를 포함하므로 제외 권장 |
| G2 커밋 `abd1101 3d16f3b 49f5178` | 충돌 없이 얹힘. 이 브랜치에는 넣지 않음 (OR 동점 정책 미결) |
| `tests/night_consistency_support.py` | N5와 내용이 같음 |
| N5의 `test_night_consistency_status.py` | `recommendation_hold`를 불러오므로 `958536d`를 빼면 수정 필요 |
| N5 승인 패치 `c2_pipeline_protected.patch` | 이 브랜치의 `pipeline_finalize.patch`로 대체 |

## 팀 결정 필요

1. `pipeline_finalize.patch` 승인 여부.
2. 표시 정책: 날짜 차이를 "기록 확인 시 충족 가능"으로 볼지. 범례의 "답변상 충족" 문구도 함께 정해야 합니다.
3. 다툼 중인 불충족 표시: 지금은 추천에 넣지 않고 "의료진 검토 필요"로 둡니다.
4. 문서 검증 전 불충족의 표시. R2의 적격 쪽 결정과 같이 정해야 합니다.
5. `time_dependent`를 상충으로 볼지. 지금은 두 값이 모두 통과해도 보류합니다.
6. R5 P2/P3, OR 동점 정책은 정하지 않았습니다.

## 측정하지 않은 것

- 실제 LLM 입력에서의 C2 빈도. 반복 검사값이 흔하면 보류가 많아질 수 있습니다.
- 실제 LLM이 쓰는 표현. 라벨 문자열만 교체하므로 "참여 가능" 같은 다른 확정 표현은 잡지 못합니다.
- 브라우저 화면 렌더링. 서버 이벤트와 `app.js` 문구까지만 확인했습니다.
