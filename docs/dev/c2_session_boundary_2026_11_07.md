# C2 Session 내부 추천 경계 수정 (2026-11-07)

브랜치: `fix/c2-session-boundary` (로컬, push 안 함). 기준: `hold/status-boundary` `554e014` = PR #24 `ca5debf` + R5 표현 계층.
결합 검증 브랜치: `local/c2-pr24-pr25` (로컬, PR 금지).

## 결론

엔진이 확정하지 않은 쌍이 추천, LLM 입력, 저장 결과, 웹, API SSE, CLI에서 확정으로 나가는 경로를 `conservative_view.evaluate` 한 곳으로 막았습니다.

**판정 수정이 아닙니다.** `eligibility`, 기준별 판정, PairStatus, Fact Store, `Session.matches`는 바뀌지 않습니다. 낮춘 시험의 판정은 여전히 `ELIGIBLE`입니다. 지표가 읽는 `eligibility`도 그대로입니다.

## 경로별 전후 비교

합성 시나리오를 실제 `Session.run_fixed`에 태워 측정했습니다. LLM은 스텁입니다.
전 = PR #24 `ca5debf`, R5 = `554e014`, 후 = 이 브랜치. 판정과 엔진 상태는 12개 시나리오 모두 세 코드에서 같습니다.

| 상태 | 판정 / 엔진 | 항목 | 전 | R5 | 후 |
|---|---|---|---|---|---|
| 확정 적격 | ELIGIBLE / CONFIRMED_ELIGIBLE | 전 경로 | 공개 기준 충족 | 같음 | **같음** |
| 불충족 (문서 미검증) | INELIGIBLE / VERIFICATION_PENDING | 전 경로 | 공개 기준 불충족 | 같음 | 같음 |
| 상충: 날짜 다른 값 | ELIGIBLE / VERIFICATION_PENDING | Session 추천, 저장 추천, 저장 match_status | 충족 | 충족 | **기록 확인 시 충족 가능** |
| | | 웹·SSE | 충족 | 기록 확인 시 충족 가능 | 같음 |
| 상충: 바이오마커 | ELIGIBLE / VERIFICATION_PENDING | Session 추천, 저장 추천, 저장 match_status | 충족 | 충족 | **의료진 검토 필요** |
| | | 웹·SSE | 충족 | 의료진 검토 필요 | 같음 |
| 상충: 불충족 다툼 | INELIGIBLE / VERIFICATION_PENDING | 저장 match_status | 불충족 | 불충족 | **의료진 검토 필요** |
| | | 추천 | 없음 | 없음 | 없음. `held_out`으로 분리 |
| 모름 | UNCERTAIN / INFO_MISSING | 전 경로 | 추가 정보 필요 | 같음 | 같음 |
| 증거 부족 | UNCERTAIN / INFO_MISSING | 전 경로 | 추가 정보 필요 | 같음 | 같음 |
| 임상 판단 대기 | UNCERTAIN / EXPERT_REVIEW | 전 경로 | 의료진 검토 필요 | 같음 | 같음 |
| 임상 판단 항목 LLM 통과 | ELIGIBLE / CONFIRMED_ELIGIBLE | Session·저장·웹 | 충족 | 충족 | **의료진 검토 필요** |
| 미평가: 필수 기준 0개 | ELIGIBLE / NOT_EVALUATED | Session·저장·웹 | 충족 | 충족 | **의료진 검토 필요** |
| 미평가: 평가 실패 | NOT_EVALUATED | 전 경로 | 평가 실패 | 같음 | 같음 (테스트만 확인) |
| 추천 없음: 모집 종료 + 보류 | ELIGIBLE / VERIFICATION_PENDING | 저장 match_status | 충족 | 충족 | **기록 확인 시 충족 가능** |
| 보류 + 확정 | HELD ELIGIBLE, OK ELIGIBLE | 맨 위 그룹 | 둘 다 | OK만 (웹) | OK만 (전 경로) |

LLM 경로:

| 항목 | 전 | R5 | 후 |
|---|---|---|---|
| 보고서 LLM이 보류 시험을 `CRITERIA_MATCH`·`ELIGIBLE`로 받음 | 예 | 예 | **아니오** |
| 추천 LLM 요약의 `ELIGIBLE:` | 있음 | 있음 | **외부 상태로 바뀜** |
| `report.md`의 "참여 가능합니다" (보류·미확정 시험) | 남음 | 남음 | **낮춤** |
| 웹 보고서의 같은 문장 | 남음 | 남음 | **낮춤** |

## 수정 내용

| 대상 | 내용 |
|---|---|
| `Session.finalize` | 추천 전에 보류 계산. 추천 입력은 사본. 보고서 LLM에는 보류 시험의 판정 코드, 요약의 판정 코드, `eligibility_history`를 넘기지 않음 |
| `Session.save` | `conservative_view.save`로 일원화. 웹·CLI 개별 호출 제거. 기준 계산 전에는 파일을 쓰지 않음 |
| `result.json` | `match_status`·추천은 낮춘 값. `eligibility`·기준 판정은 그대로. 원래 외부 상태는 `engine_boundary.<trial>.external` |
| `report.md` | 사용자용. LLM 원문은 표현이 달라질 때 `report.raw.md`에 보존 |
| 추천 없음 문구 | 다툼 중인 불충족이 있으면 "모두 맞지 않았습니다"라고 쓰지 않음 |
| CLI | `held_out` 시험을 시험 ID·상태로 출력. `--redact-console`도 동일 |

### R5·패치에서 달라진 점

- `pipeline_finalize.patch`는 그대로 적용하면 보고서 LLM에 `eligibility: ELIGIBLE`, 요약의 `ELIGIBLE:`, 이력이 남아 입력 경계가 닫히지 않습니다. 같은 지점을 고치되 범위를 넓혔고, 패치 파일은 삭제했습니다.
- R5가 원래 값으로 둔 `result.json`의 `match_status`를 낮췄습니다. 저장 결과도 같은 기준이어야 하기 때문입니다.
- **R5가 일부러 뺀 필수 기준 0개 쌍을 낮췄습니다.** 엔진이 NOT_EVALUATED인데 "공개 기준 충족"으로 맨 위에 추천되어 이번 목표에 정면으로 해당합니다. 판정과 R5 P1/P2/P3 정책은 정하지 않았고 `unresolved`에 그대로 남깁니다.
- **임상 판단 항목 LLM 통과를 낮췄습니다.** 엔진은 이 쌍을 CONFIRMED_ELIGIBLE로 내므로, 이 항목만은 "엔진 미확정"이 아니라 판정 기록(`underspecified`이고 통과)을 읽어 낮춥니다.
- `_hold`가 동점 코호트 기준까지 읽습니다. G2만 가져오면 동점 쌍의 보류 사유 규칙이 비었을 것입니다.

## 보호 파일 변경 범위

| 파일 | 범위 |
|---|---|
| `src/ctrec/pipeline.py` | `finalize`, `save`만 |

`matcher`, `recommender`, `status`, `decision_state`, `decision_loop`, `decision_policy`, `patient_profiler`, `question_priority`, `web/access`, `web/retention`, `data`는 그대로입니다. `test_frozen_store_and_policy_files_unchanged`는 HEAD 기준 검사라 커밋 뒤 통과합니다. 승인 근거가 되지 않습니다.

비보호 변경: `conservative_view.py`, `engine/cohort_state.py`(G2), `schemas.py`(`held_out` 추가), `cli.py`, `orchestrator.py`, `web/app.py`, `web/static/app.js`. 보호 목록 밖이지만 `cohort_state.py`는 엔진 상태를 낮추는 변경이므로 함께 검토가 필요합니다.

## 중복 제거 내역

| 항목 | 처리 |
|---|---|
| N5 C2 `958536d` (`recommendation_hold.py`, `web/engine_hold.py`) | 제외. `conservative_view`와 중복 |
| N5 `c2_pipeline_protected.patch`, R5 `pipeline_finalize.patch` | 제외·삭제. 실제 수정으로 대체 |
| N5 G2 `abd1101 3d16f3b 49f5178` | 가져옴 (`c81a96c 7b7a5e4 d333004`). 보류가 선언 순서에 흔들리지 않게 하는 데 필요 |
| N5 `test_night_consistency_status.py` | 제외 (`recommendation_hold` 의존). G2 테스트만 이 브랜치 형식으로 이식 |
| N5 `g2_matcher_protected.patch` | 제외. 선택 코호트와 외부 상태를 바꾸고 `test_safety` 기대값 변경이 필요해 별도 결정 |
| N5 D5 패치 | 제외. PR #20 소유 |
| 웹·CLI의 `conservative_view.save` 호출 | 제거. `Session.save` 한 곳 |

## LLM 원문 잔여 위험

`_CLAIM` 정규식으로 보고서와 추천 LLM 문구를 점검합니다. 시험을 가리키지 않는 문장은 확정 시험이 없거나 보류가 있을 때 대상입니다. 제목 아래 문장은 그 제목의 시험으로 봅니다.

잡는 표현: `CRITERIA_MATCH`, `ELIGIBLE`, 공개·모든·선정 기준 충족류, 적격, 참여·등록 가능, 참여 대상, 적합.

| 남은 위험 | 내용 |
|---|---|
| 바꿔 쓴 표현 | "잘 맞는 시험입니다", "들어갈 수 있습니다", "조건에 해당합니다", 영어 "qualifies"는 통과합니다. `test_known_limit_*`로 고정 |
| 시험 ID 없이 제목으로만 지칭 | 시험을 가리키지 않는 문장으로 처리 |
| 실제 LLM 문체 | 미측정. API 없이 스텁으로만 검증. 오탐·미탐 비율을 모름 |
| 과잉 차단 | 확정 시험과 미확정 시험을 한 줄에 쓰면 줄 전체를 낮춤 |
| 제외 사유 | `excluded`의 LLM 사유는 점검하지 않음 (추천 대상이 아님) |

## 검증

| 대상 | 결과 |
|---|---|
| 이 브랜치 전체 pytest | 최종 커밋 기준 수치는 작업 보고 참조 (직전 코드 커밋 `d6c4705`: 2745 passed, 9 skipped, exit 0) |
| PR #24·#25 결합 (`local/c2-pr24-pr25`) | PR #24는 이미 포함, PR #25 충돌 없이 병합. `d6c4705` 기준 2773 passed, 9 skipped, exit 0 |
| 변이 검사 | 20종 전부 검출 |
| 고정 사례 259쌍 전후 | 판정·기준 판정·외부 상태·추천 차이 0 |

**고정 사례의 한계:** 259쌍에 ELIGIBLE이 하나도 없고 보류도 0건입니다. 회귀가 없다는 뜻일 뿐, 실제 시험에서 확정 적격이 보존된다는 증거가 아닙니다. 확정 적격 보존은 합성 대조군으로만 확인했습니다.

**다른 후보와의 충돌 (머지 시뮬레이션):** PR #20과 `pr/fact-verification-history`는 `README.md`만 충돌, PR #22, N2, N3, `fix/expert-judgment-boundary`, `candidate/p0-expert-boundary`는 충돌 없음.
`candidate/p0-expert-boundary` 결합 실행: 3457 passed, 1 failed. 실패는 `test_every_path_uses_the_same_status[clinical_llm_pass]` 1건으로, 그 후보의 matcher가 이 쌍을 이미 UNCERTAIN으로 막아 기대 판정이 달라진 것입니다. 결합 시 기대값을 바꾸면 되고 `_judged_by_llm`은 그때 다시 평가해야 합니다.

## 비용

고정 시험 26개 중 17개에 `underspecified` 규칙이 있습니다 (471개 중 39개). 실제 LLM이 이 규칙을 통과시키면 이제 "의료진 검토 필요"로 표시되므로, 이 시험들에서 "공개 기준 충족" 표시가 줄어듭니다. 실제 빈도는 측정하지 못했습니다.

`metrics`의 `eligibility`는 그대로지만 `ranked`와 `recommended`는 `result.json`을 읽습니다. 보류가 생기면 표시 순서와 맨 위 그룹 지표가 달라질 수 있습니다.

## 미해결 상태

| 항목 | 상태 |
|---|---|
| 판정 자체 | 고치지 않음. 낮춘 쌍의 근본 원인(`clinical_record` 무검증 확정, 임상 판단 경계)은 N2·P0 후보·R2 소관 |
| 문서 검증 전 불충족 | 낮추지 않음. `exclusion_not_document_verified`로만 기록 (R2 축) |
| `POTENTIAL_MATCH` + 상충 | 확정은 아니지만 낙관적 문구. 그대로 |
| `EXPERT_RULES` 대상 규칙 | `underspecified`가 아니면 이 계층도 못 잡음. 저장된 `judge.life_expectancy` 3건이 해당. P0 후보가 필요 |
| G2 잔여 | 선택 코호트와 외부 상태는 선언 순서를 따름 (matcher 패치 미적용). 동점 UNCERTAIN 쌍의 표시는 순서에 달릴 수 있음 |
| 원본 기록 | `Session.matches`, `trace.jsonl`, Inspector의 라운드별 값은 원래 값 유지 (감사 목적). Inspector의 최종 외부 상태만 낮춘 값 |
| 웹 이벤트 `eligibility` 필드 | API 소비자에게 원래 값이 보임 (의도적 보존). 진행 중 추적 이벤트는 '중간 판정' 표시 |
| 오케스트레이터 도구 응답 | `evaluate_trials`는 추천 전 단계라 원래 상태를 받음. 최종 응답만 낮춘 값 |
| 질문 생성 입력 | 원래 상태 유지. 질문 라운드 동작을 바꾸지 않기 위함 |
| 브라우저 렌더링 | 서버 이벤트까지만 확인. `held_out` 시험은 웹에서 '그 밖에 평가한 시험'으로 보임 |
| D5, R5 P1/P2/P3, OR 동점 정책 | 정하지 않음 |

## 팀 승인 필요

1. `pipeline.py` `finalize`, `save` 변경 승인.
2. `result.json` `match_status`를 낮춘 값으로 저장하는 계약 변경. 원래 값은 `engine_boundary.external`로 이동.
3. 필수 기준 0개 쌍과 임상 판단 항목 LLM 통과 쌍을 "의료진 검토 필요"로 표시할지.
4. `report.md`를 코드가 줄 단위로 바꾸는 방식과 `_CLAIM` 표현 목록.
5. `time_dependent`를 보류로 볼지, `POTENTIAL_MATCH` 범례 문구 (R5에서 넘어온 항목).
6. 실제 API로 추천·보고서 LLM 품질 검증 여부.
7. 병합 순서: PR #24가 먼저. 이 브랜치는 PR #24 위에 쌓여 있습니다.
