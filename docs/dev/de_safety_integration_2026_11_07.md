# D+E 안전성 통합과 환자값 잔여 우회 (2026-11-07)

기준 `origin/integration/dcvc` `64a5011`. 값은 전부 합성이고 LLM 자리는 고정 대역입니다. 실제 API 호출은 0건입니다. 어떤 수치도 임상 성능 근거가 아니며 표현은 임상 검수를 받지 않았습니다.

## 결론

- D(A+B+C)와 E의 환자값 수정, PR #22, PR #24 `e8f8af1`이 한 스택에 들어갔고 전체 테스트가 통과합니다.
- E의 잔여 8건, 과잉 보류 3건은 실제 Session에서 재현했고 전부 고쳤습니다. 이 11건과 이전 평가 문장은 이제 회귀 세트입니다.
- **고정 코드의 새 독립 평가는 부정 70건 중 8건 통과, 정상 70건 중 5건 보류입니다.** 같은 세트에서 수정 전 D+E는 15건·11건입니다. 줄었지만 닫히지 않았습니다.
- 정규식 판단은 서술형 문장을 다 닫지 못합니다. 측정 상태를 구조화 입력으로 받는 계약이 여전히 필요합니다.

## 통합

| 입력 | 처리 |
|---|---|
| PR #24 `e8f8af1` | 스택 맨 아래. D의 `a8ccbc6`은 같은 줄을 고쳐서 빼고 #24의 좁은 `info_key` 규칙을 씀 |
| PR #22 `5f7d66b` | 그대로 병합 |
| A `16af881`, R2 `d20865a`, C `3d4fbcc` | 병합 |
| B `2bf56af`, `e6f8857`, `44def3d`, `2905ad5`, `695dd04` | cherry-pick (`-x`) |
| D 후속 `a95baa9`, `6d22050`, `36be1c7` | cherry-pick (`-x`) |
| E `a329d97`, `a5a423b` | cherry-pick (`-x`) |
| D 보고서 `d291346`, E 보고서 `faa36cc` | 제외. SHA가 달라져 이 문서가 대신함 |

제외한 측정용 커밋은 D와 같습니다(`a3306c8`, `2d64494`). R5 영역은 고치지 않았습니다.

**스택 소스는 작업 브랜치 `candidate/de-round2` `ef4164c`와 파일 내용이 같습니다.** 차이는 README와 보고서뿐입니다.

첫 병합에서 제가 `git checkout --theirs`로 충돌을 풀어 C가 `tests/test_security_paths.py`에 넣은 수정까지 덮었고 테스트 2건이 실패했습니다. 충돌 구간만 푸는 방식으로 다시 만들었습니다.

## 잔여 8건, 과잉 보류 3건

각 사례를 실제 Session에 넣었습니다. 프로필 값은 12, 인용은 문장 전체, 기준은 `Hb >= 9 g/dL`(`self_report_ok`)입니다.

| 사례 | 수정 전 판정 / 사실 | 수정 후 판정 / 사실 | 원인 |
|---|---|---|---|
| `채혈 자체를 안 했어요. 템플릿 칸에 혈색소 12 가 ...` | ELIGIBLE, CE / 1 | UNCERTAIN, INFO_MISSING / 0 | 상태가 앞 문장, 값이 서술 주어 |
| `Hgb 12 under the lab section header but the order was cancelled ...` | 같음 | 같음 | 과정 전체의 상태가 다른 절 |
| `Specimen never collected ... field ...` | 같음 | 같음 | 앞 절의 상태, 값이 칸 이름 |
| `혈색소 12 항목은 ... 시행된 적 없는 검사.` | 같음 | 같음 | 값이 칸 이름, 어순이 다른 부정 |
| `혈색소 12 는 기관 표준 참고치(상한)이고 ...` | 같음 | 같음 | 값이 서술 주어 |
| `Hgb 12 belongs to the bed next door; ...` | 같음 | 같음 | 값이 서술 주어 |
| `If the transfusion works, expect Hgb 12 by morning; ...` | 같음 | 같음 | 조건절이 다른 절 |
| `비교군(건강 성인 50명) 혈색소 12 중앙값.` | 같음 | 같음 | 모집단 통계 |
| `Hgb 12 on today's CBC, verified. Pending: procalcitonin, ...` | UNCERTAIN, INFO_MISSING / 0 | ELIGIBLE, CE / 1 | `Pending:` 뒤가 주어 |
| `헤모글로빈 12 g/dL 검증. 검체 오염 의심 없음.` | UNCERTAIN, INFO_MISSING / 0 | ELIGIBLE, CE / 1 | `의심 없음`의 부정 |
| `앞선 헤모글로빈 수치(무효 처리)는 제외, 재검 결과 12 g/dL 이 유효.` | UNCERTAIN, INFO_MISSING / 1 | ELIGIBLE, CE / 1 | 재검 값이 다른 절의 검사명과 끊김 |

수정 전에는 8건이 `CRITERIA_MATCH`로 추천에 올랐습니다. 수정 후 8건은 `INSUFFICIENT_INFORMATION`, 사실 저장 0건입니다. ContractSession, 짧은 인용, LLM 인용, 부적격 방향, 합성 문서 대조군에서도 같습니다 (`tests/test_value_context.py`).

## 구조 변경 (`value_grounding.py`, `profile_facts.py`)

금지어 추가보다 판단 구조를 바꾼 부분입니다.

| 구조 | 내용 | 양성 대조 |
|---|---|---|
| 표지의 귀속 | 다른 절의 상태도 다른 대상, 과거 검체, 재검이 표시되지 않으면 이 값을 서술함. 앞 절 방향도 봄 | `Hb 12. heparin 오더 취소됨`, `첫 검체는 clot 으로 무효. 재채혈 Hb 12` |
| 과정 절 | 채혈·검체처럼 과정 전체를 말하는 절은 검사명 없이도 구간의 값에 미침 | `CT 시행 안 함. Hb 12` |
| 값이 주어·칸 | `혈색소 12 항목은`, `Hgb 12 belongs to`. 측정을 긍정하는 서술만 허용 | `Hb 12 g/dL는 정상 범위입니다`, `Hgb 12 was drawn this morning` |
| 문장 범위 조건 | 앞선 조건절은 다른 절의 값까지 덮음 | `Hb 12; if it falls below 8, transfuse` |
| 값이 없다는 절 | `no PCT this time`은 같은 검사의 과거 값을 현재 근거에서 뺌 | `PCT 0.05 today; no CRP this time.` |
| 재검·라벨 연결 | 문장에 이 검사만 있을 때 재검 값과 `라벨: 값`을 그 검사에 연결 | 다른 검사가 있으면 연결 안 함 |
| 전언·수식어 | 나이·ECOG의 `per mother`, `보호자 말로는`, `Bone age`, `직접 빌리루빈` | `기증자 40세, 환자 62세` |

**계약 변경 1건.** 과거로 표시된 값만 있으면 이제 현재 값의 근거가 아닙니다. `test_lab_value_semantics.py`의 기대값을 바꾼 유일한 기존 테스트입니다. 예전에는 `이전 검사 Hb 12 g/dL`이 유일한 측정값이면 인정했습니다. 기간 조건이 있는 기준에서 과거 값을 쓰려던 경우라면 임상 판단이 필요합니다.

자가보고 허용과 문서 필수의 구분은 바꾸지 않았습니다. 유효한 값이어도 문서 필수 기준은 서버 문서 없이 확정되지 않습니다.

## 독립 평가

코드를 본 적 없는 별도 에이전트가 쓴 문장입니다. 검사는 값 근거 층(`ground_lab`, `ground`)이고 표본이 작으며 작성자가 LLM이라 실제 기록의 비율이 아닙니다.

| 세트 | 대상 코드 | 부정 통과 | 정상 보류 | 지위 |
|---|---|---|---|---|
| 이전 2차 (90건) | `6125d72` | 8/45 | 3/45 | 회귀로 전환 |
| 개발용 (119건) | `6125d72` | 15/61 | 18/58 | 코드를 맞춘 세트 |
| 3차 (140건) | `30b7173` 고정 | **14/70** | **12/70** | 측정 후 수정에 사용. 회귀로 전환 |
| 3차, 줄바꿈 보정 사본 | `30b7173` | 15/70 | 8/70 | 작성자가 `\n`을 글자로 쓴 15건을 고친 사본 |
| 4차 (140건) | `6125d72` (수정 전) | 15/70 | 11/70 | 비교 |
| **4차 (140건)** | **`ef4164c` 고정 (최종)** | **8/70** | **5/70** | **코드 고정 후 1회 실행. 맞춘 적 없음** |

회귀 세트의 현재 값은 일반 성능이 아닙니다. 4차 세트는 `docs/dev/value_context_holdout_4.json`에 두었고, 이 결과를 보고 코드를 고치면 다시 독립이 아닙니다.

### 4차에서 남은 우회 8건

| 문장 | 유형 |
|---|---|
| `Same ward, bed 4 (not our patient): creatinine 3.4 mg/dL, ...` | 타인 표현이 표현 목록 밖 |
| `Tumour board minutes: the case discussed before ours had ANC 0.6 ...` | 타인 표현이 목록 밖 |
| `보호자(아내) 나이 58세. 환자 기록은 별도 확인 필요.` | 괄호 뒤 나이의 주체 |
| `Urine creatinine 140 mg/dL (spot urine ...); serum not drawn.` | **검체 종류가 다른 검사** |
| `CSF albumin 30 mg/dL, ...; serum albumin unknown.` | **검체 종류가 다른 검사** |
| `WBC 12.0 given verbally by phone; written report not yet received, unverified.` | `unverified`, 구두 보고 |
| `지방혈증 검체, 총빌리루빈 5.1 mg/dL 간섭으로 보고 불가, 재채혈 예정.` | `보고 불가` |
| `... Form legend: ECOG 0 = fully active; physician has not scored ...` | 서식 범례 |

소변·뇌척수액 값을 혈청 기준에 쓰는 경로는 우선순위가 높습니다.

### 4차에서 남은 과잉 보류 5건

`Repeat INR was not needed ...: INR 1.0.`, `Roommate's WBC is high; ours (the patient) is 7.2`, `our patient ECOG 2 ...; the neighbouring case (ECOG 0)`, `Urine creatinine 140 (spot); serum creatinine 1.3`, `WBC after smear review confirmed 5.9`. 모두 보류 쪽이라 사람 확인으로 넘어갑니다. `tests/data`의 `known_over_hold` 8건도 알고 남긴 과잉 보류입니다.

## 경로별 E2E

`tests/test_de_safety_e2e.py`, `tests/test_value_context.py`, D의 `tests/test_abc_safety_e2e.py`가 함께 봅니다.

| 사례 | 기본 Session | ContractSession | WP6 | 저장·웹 표시·CLI·오케스트레이터 | Fact Store |
|---|---|---|---|---|---|
| 정상 자가보고 적격 | ELIGIBLE / CE | 같음 | 같음 | `CRITERIA_MATCH`로 동일 | 저장 |
| 문서 필수 기준 | UNCERTAIN / VERIFICATION_PENDING | 값 경로만 확인 | 같음 | `POTENTIAL_MATCH`로 동일 | 저장, 합성 증명 대조군만 CE |
| 전문가 판단 기준 | UNCERTAIN / EXPERT_REVIEW | D와 같음 | 같음 | `NEEDS_REVIEW`로 동일 | 해당 없음 |
| 미측정·대기·무효 | UNCERTAIN / INFO_MISSING | 같음 | 원문 표현이 닿지 않음 | `INSUFFICIENT_INFORMATION`으로 동일 | 저장 안 함 |
| 다른 사람의 수치 | 같음 | 같음 | 원문 표현이 닿지 않음 | 같음 | 저장 안 함 |
| 명백한 부적격 | INELIGIBLE | 같음 | 같음 | 추천 제외 | 저장 |
| 모름·무응답 | UNCERTAIN / INFO_MISSING | D와 같음 | 비교 제외 (D와 같음) | 동일 | 없음 |
| 상충 답변 | UNCERTAIN, 보류 | 값 경로만 확인 | D와 같음 | 동일 | 값은 유지, 판정 보류 |
| 갱신 후 재평가 | 무효 기록 뒤 유효 답변은 확정, 유효 기록 뒤 "시행된 적 없음" 답변은 보류 | 해당 없음 | 해당 없음 | 해당 없음 | 답변 값 저장 |

경로별 한계입니다.

- **WP6**는 프로필러를 거치지 않고 구조화 사실을 받습니다. 표현 판단은 WP6에 닿지 않으며, 사실이 없을 때 Session과 같은 쪽으로 보류되는지만 봅니다.
- **웹**은 `cv.build`가 만드는 응답을 봅니다. FastAPI 실제 흐름은 D의 대역 시나리오로만 돌고, 측정값이 아닌 문장을 HTTP로 넣어 보지는 않았습니다. 브라우저 테스트는 다시 돌리지 않았습니다.
- **보고서**는 고정 대역 문구입니다. 실제 LLM 보고서가 값을 어떻게 쓰는지는 재지 않았습니다.
- 표의 "D와 같음"은 D 테스트가 그대로 통과했다는 뜻이고 새 사례를 만든 것은 아닙니다.

## 회귀

| 비교 | D → D+E (`6125d72`) | D+E → 최종 (`ef4164c`) |
|---|---|---|
| 오프라인 사례 140쌍 | 차이 0 | 차이 0 |
| 고정 104쌍 | 차이 0 | 차이 0 |
| N2 대조군 표현 112건 | 43 유지 | 차이 0 |
| N2 조합 900건 (적격 274, 부적격 626) | 유지 | 차이 0 |
| N2 측정값 아닌 33건 | 오류 0 유지. 표시만 `POTENTIAL_MATCH`에서 `INSUFFICIENT_INFORMATION`으로 낮아짐 | 차이 0 |
| PR #20 WP6 36행 | 차이 0, CE 25, CI 43, 거짓 CE 0 | 차이 0 |

고정 104쌍과 140쌍에는 확정 적격이 없어 적격 보존의 근거가 아닙니다. 조합 900건과 회귀 세트의 양성이 그 근거입니다.

### 전체 테스트

| 트리 | 결과 |
|---|---|
| `de-0-dependencies` (#24 + #22) | 2801 passed, 9 skipped, exit 0 |
| `de-1-value-expert` | 3539 passed, 9 skipped, 3 xfailed, exit 0 |
| `de-2-record-gate` | 3627 passed, 9 skipped, 3 xfailed, exit 0 |
| `de-3-output-boundary` | 3804 passed, 9 skipped, 1 xfailed, exit 0 |
| D+E 수정 전 `6125d72` | 4293 passed, 9 skipped, 1 xfailed, exit 0 |
| `de-4-value-context` 최종 | 4912 passed, 9 skipped, 1 xfailed, exit 0 |
| 최종 + R5 `d4e0d0f` | 3 failed, 4982 passed, 9 skipped, 1 xfailed, exit 1. 아래 참고 |

추가한 xfail은 없습니다. 9 skipped는 Windows 심볼릭 링크 8건과 브라우저 모듈 1건, xfail 1건은 분류 신호 밖 임상 판단으로 D와 같습니다.

### 변이 검사

변이 72종 중 65종이 적용됐고 62종이 실패로 잡혔습니다. 3종(`앞 절 무주어`, `가정 표현`, `지난 검체 상태`)은 다른 규칙이 같은 사례를 막아 동작이 바뀌지 않는 동등 변이입니다. 처음에는 7종이 살아남아 테스트를 추가했습니다. 적용되지 않은 7종은 코드가 바뀐 E의 예전 변이입니다. 이 7종이 보던 동작은 이번에 추가한 변이가 대부분 대신합니다. D의 경계 변이 9종은 그대로 잡힙니다.

## PR 결합

| 조합 | 결과 |
|---|---|
| `64a5011 + #24 + #22` | 2801 passed, exit 0 |
| #24 충돌 | D의 `a8ccbc6`과 같은 줄. 스택은 #24를 쓰고 `a8ccbc6`을 뺌 |
| R5 `d4e0d0f` | `recommender.py`, `web/app.py`, `app.js` 충돌은 양쪽 유지로 풀림. 그 뒤 **테스트 3건 실패** |

R5 결합 실패 3건은 코드 결함이 아니라 양쪽 계약이 겹치는 테스트입니다. 다만 G 담당자가 정리해야 합니다.

| 테스트 | 내용 |
|---|---|
| `test_c2_session_boundary::test_every_path_uses_the_same_status[no_required]` | C가 예전 동작(`ELIGIBLE`)을 고정. R5는 `NOT_EVALUATED` |
| `test_night2_status_boundary::test_no_required_criteria_is_held_while_the_policy_stays_open` | 같음 |
| `test_r5_no_required_criteria::test_document_verified_ineligibility_stays_confirmed` | 문자열 `set_verification`으로 확정을 만듦. 게이트 뒤에는 서버 증명이 필요 |

## PR 분리

쌓는 순서입니다. 각 PR의 base는 바로 아래 브랜치이고 `integration/dcvc`에 직접 병합하지 않습니다.

| 순서 | 브랜치 | 내용 | 의존성 |
|---|---|---|---|
| 0 | `pr/de-0-dependencies` | #24 `e8f8af1` + #22. PR을 새로 열지 않고 두 PR이 병합되면 사라짐 | 없음 |
| 1 | `pr/de-1-value-expert` | N2 값 근거 + 임상의 경계. `matcher.py` 포함 | 0 |
| 2 | `pr/de-2-record-gate` | R2 + 문서 게이트. `matcher`, `pipeline`, `decision_state` 포함, 팀 결정 필요 | 1 (`evidence_basis.py`, 값 근거) |
| 3 | `pr/de-3-output-boundary` | C + 상충 보류 + E2E + WP6 표시 | 2 (게이트로 낮아진 쌍의 보류), #24 |
| 4 | `pr/de-4-value-context` | E + 이번 구조 수정 + 회귀 세트 | 1. 3과 무관한 파일이지만 테스트가 2·3의 도구를 씀 |

보호 파일 변경은 1~3에 있고 4에는 없습니다.

## 남은 문제

| 등급 | 내용 | 담당 |
|---|---|---|
| P0 | 측정값 표현 우회 4차 8/70. 측정 상태 구조화 입력 계약 없음 | 계약 소유자 + 서연 님 임상 검수 |
| P0 | 검체 종류가 다른 값(소변·뇌척수액)이 혈청 기준 값으로 인정됨 | N2 담당. 수정하면 새 평가 세트 필요 |
| P0 | 서버 문서 계약 없음. 운영 CE는 문서 필수 기준이 있는 시험에서 0 | 팀 결정 + 계약 소유자 |
| P0 | 미분류 전문가 기준. 검수 큐 108행 미검수 | 임상 검수자 |
| P1 | 과거 값 단독 불인정(계약 변경)의 임상 타당성 | 서연 님 |
| P1 | `value_grounding.py` 표현 목록과 구조 규칙 임상 검수 전 | 서연 님 |
| P1 | R5 결합 테스트 3건 | G 담당 |
| P1 | `web/cohort_board.py`는 게이트 밖, OR 생성기 패치 미적용, #23 열림 | 현황판 소유자 |
| P1 | WP6 CE는 합성 문서 가정 | WP6 담당 |
| P1 | R1 복구 패치 미적용 | 동겸 님 |
| P2 | `Session.profile` 값이 보고서·LLM 입력에 보이는 경로, `not_measured` 보류의 감사 기록 | N2 담당 |
| P2 | D 보고서의 다른 P1·P2 | D 보고서 담당자별 |

D 보고서 `docs/dev/abc_integration_2026_11_07.md`는 이 스택에 넣지 않았습니다. 원문은 커밋 `d291346`에 있습니다.
