# M: 환자값 근거 안전성 P0 최종 (2026-11-07)

- 브랜치 `fix/m-evidence-safety-final`, worktree `Desktop/workseoyeon-m`
- 기준 `285db00` = `origin/fix/specimen-value-binding`(d775bb6, L) + `origin/pr/de-5-r5-integration`(efe3354, D+E·R5). tree 29a7ae3b는 P·Q 기준과 같음
- 값은 전부 합성. 실제 LLM 호출 0건. 어떤 수치도 임상 성능 근거가 아니며, 표현·동의어 목록은 임상 검수 전입니다.
- 문서 검증 권한(`document_authority`, 운영 기본 NoDocumentAuthority)과 임상의 판단 경계(`expert_boundary`, clinician_judgment_required)는 바꾸지 않았습니다.

## 결론

| 과제 | 결과 |
|---|---|
| M-1 서술 상충 | 같은 질환의 '있음'·'없음' 진술이 함께 있으면 `conflicting`으로 보류. N의 strict xfail `test_E_conflicting_narrative_stops_confirmation` 통과. 독립 세트 6: 상충 15건 중 7건 탐지(수정 전 1건), 정상 15건 거짓 탐지 0건 |
| M-2 검사 기간 조건 | 규칙 엔진이 검사일로 계산. 일 단위 날짜가 없거나 기간 밖·미래 날짜면 판정하지 않고 LLM에도 넘기지 않음. N의 strict xfail `test_date_window_lab_is_evaluated_deterministically` 통과. 독립 세트 6: 30/30 (추출 날짜가 맞다는 가정) |
| L 잔여 17/70 | 세트 5(이제 회귀 세트)에서 0/70. 유형별 수정은 아래 |
| 타인·과거·복사·검체 재검증 | 독립 세트 6 검사값 부정 80건: 오채택 15 → **7** |
| 과잉 보류 | 독립 세트 6 정상 80건: 13 → **12**. 세트 5 정상 12 → 9, 세트 4 11 → 11(전부 L의 검체 미기재 계약). 서술 상충 정상 0/15 → 0/15 |

## 수정 (커밋)

| 커밋 | 내용 | 파일 |
|---|---|---|
| `e5410a4` | M-2 기간 조건. `rule_engine.window_status`, `evaluate(rule, profile, reference_date)`. 기간 밖 `lab_window_outside`, 날짜 불명·미래 `lab_window_undetermined`, 둘 다 holds unknown·passes None | `tools/rule_engine.py`, `agents/matcher.py`(보호 파일, 호출 한 줄), `incremental.py`, `fact_compare.py` |
| `c77ea50` | M-1 서술 상충. 동의어 묶음(ILD/pneumonitis/간질성, 뇌전이, 심부전, 심근경색, 뇌졸중, B·C형 간염, HIV, 결핵, 자가면역, 당뇨, 경련, 부정맥, 간경변), 한국어 조사를 뗀 핵심어 비교. 다른 사람·가족력, 선별·감별·계획 문구 제외. 배제("ruled out", "배제", "음성")는 '없음'. "No A, B, or C" 목록 부정 | `evidence_directness.py` |
| `089a48a` | L 잔여: 과거(연도·월 앵커, Historic/Back in/at diagnosis/nadir/two years ago/작년, '현재 결과 없음' 기록, ECOG 과거값만), 다른 사람(roommate/next door/다른 증례 소개, 앞 문장의 다른 사람을 가리키는 his/her), 체액 문맥(LP·tap·paracentesis), 문장 단위 혈액 문맥(chem panel, blood drawn, 정맥혈, 혈당, S-), 무효(not verified, unconfirmed, suppressed, "the number"를 가리키는 무효 선언), 복사 각주(`*copied from the previous note`를 기록 전체에서) | `value_grounding.py`, `specimen.py` |
| `d06d4cb` | 회귀 130개: M-1(ed.check, Session, ContractSession), M-2(window_status, evaluate, Matcher 기준일), L 세트 5 부정 70건, 과잉 보류 목록 고정, 값 문맥 쌍 | `tests/test_m_evidence_safety.py` |

기존 테스트 기대값을 바꾼 것 2건 (동작 변경의 직접 결과):
- `test_dependency_graph.py::test_engine_rule_without_engine_result_is_llm_all_fields`: 기간 조건 규칙이 더는 LLM 위임이 아니므로, 비교값 없는 규칙으로 같은 의도를 고정하고 기간 조건 규칙은 `explicit_field`로 새 테스트
- `test_incremental.py::test_failed_reevaluation_blocks_stale_result`: 대역 함수가 새 `evaluate` 인자를 받도록

## 독립 held-out 6 (코드 고정 d06d4cb 뒤 1회)

코드를 본 적 없는 별도 에이전트가 쓴 세트입니다(`docs/dev/m_holdout_6_labs.json`, `docs/dev/m_holdout_6_cases.json`). 실행 전에 열어 보지 않았고, 결과를 보고 코드를 고치지 않았습니다. 작성자가 LLM이라 실제 기록의 비율이 아닙니다.

| 세트 | 기준(같은 base, M 미적용) | M d06d4cb |
|---|---|---|
| 검사값 부정 80 (오채택) | 15 | **7** |
| 검사값 정상 80 (보류) | 13 | **12** |
| 상충 15 (탐지) | 1 | **7** (그 밖 보류 2) |
| 상충 아님 15 (거짓 탐지) | 0 | **0** |
| 기간 조건 30 | 30건 모두 LLM 위임 | **30/30** |

| 부정 범주 (10건씩) | 기준 | M |
|---|---|---|
| 과거 값만 | 6 | **0** |
| 다른 사람 | 0 | 0 |
| 복사 | 1 | 1 |
| 검체 | 3 | 1 |
| 무효 | 1 | 1 |
| 계획·목표 | 0 | 0 |
| 전해 들은 값·불확실 | 3 | 3 |
| 다른 검사 | 1 | 1 |

기간 조건 세트는 검사일이 프로필에 정확히 옮겨졌다고 가정한 날짜 계산과 추정 금지 검사입니다. 원문에서 검사일을 뽑는 정확도(프로필러 LLM)는 측정하지 않았습니다.

## WP6 결정 기록 영향

| 실행 | 결과 |
|---|---|
| 합성 데모 `python -m ctrec.sim --oracle --bootstrap 0` 28회 | result_hash 28/28 같음, 정책 표 같음. 데모 시험에는 기간 조건 기준이 0개 |
| 생성기 코호트 4 시나리오 × replicate 0-4, 예산 45 (120 실행) | result_hash 다름. 기간 조건 기준 3개의 `evaluator_version`이 `legacy_decision_state:llm` -> `:rule_engine`, 쌍의 `pending_kinds`·단계 수가 바뀜. 정책 표는 neph-null의 `random` 한 행만 다름(확정 부적격 1 -> 0). 나머지 표는 같음 |

생성기 코호트의 해시 변화는 M-2의 의도된 결과입니다(기간 조건을 LLM 미판정 대신 규칙 엔진이 날짜로 판정·보류). WP6 기준 해시를 고정해 둔 문서·비교(Q의 `q/wp6_q`)는 이 브랜치 병합 뒤 다시 잡아야 합니다.

## Q 후보와의 병합

`integration/q-final-2`(f3c8d71, N·O 포함) + 이 브랜치: 병합 충돌 없음. 병합만 하면 전체 테스트에서 4건이 실패하고, 모두 이 브랜치 밖 파일의 기대값입니다. 수정은 `docs/proposals/m_q_merge_followups.patch`(Q 병합본 기준 diff):

| 파일 (Q에만 있음) | 병합 직후 | 후속 |
|---|---|---|
| N `tests/test_six_stage_e2e.py` | M-1·M-2 strict xfail 2건 XPASS(strict) | xfail 표시 2개 삭제 (해결됨) |
| K/PR20 `tests/test_recovery_stress.py` | `EXPECTED_BLOCKED` 고정값 17→19 (default), 22→21 (patient_unreachable) | 값 갱신. 같은 테스트의 안전 불변식(소진 쌍이 BLOCKED 아닌 채 남지 않음, 기록 없는 차단 0, 실행 가능한 행동이 남은 차단 0)은 그대로 통과 |

Q 단독(f3c8d71)에서는 `test_recovery_stress`가 통과하므로 이 차이는 M-2(기간 조건 기준을 규칙 엔진이 날짜로 판정)의 결과입니다.

## 남은 오류와 한계

| 등급 | 내용 |
|---|---|
| P0 | 서술 상충 8/15 미탐지 (세트 6): 동의어 묶음 밖 질환(hepatitis vs hepatitis B, malignancy vs adenocarcinoma, transplant, bleeding disorder vs hemophilia, autoimmune vs psoriasis, ILD vs UIP·특발성 폐섬유증), 짧은 한국어 인용 위치 실패 2건. 정규식 동의어로는 닫히지 않음: 질환 용어 사전(SNOMED/ICD 묶음) 계약이 필요 |
| P0 | 전해 들은 값·불확실 3/10 통과 ("Pt thinks ... around 10", "환자 말로는 ... 들었다고 함", "Self-reported age 67 or 68") |
| P1 | 복사 표시가 줄 머리 기호(`※ ... 그대로 옮겨옴`)일 때, 흉수 Hgb, 다른 검사의 용혈 표시 뒤 값, PT를 INR로 읽음 각 1건 |
| P1 | 정상 보류 12/80: 표·파이프 표기, 한국어 '절대호중구수', 'Donor ... Recipient (our patient)' 같은 대조 문장, 날짜 앞머리 'Collected 2026-10-25' |
| P1 | M-2는 프로필의 검사일(LLM 추출)을 씀. 추출이 틀리면 날짜 계산도 틀림. 다만 날짜가 원문에 일 단위로 있으면 값 근거 층(`_same_date`)이 원문 날짜와 대조 |
| P1 | 기간 밖·날짜 불명 기준은 이제 LLM에 넘기지 않으므로 보류가 늘 수 있음 (세트 6 정상 쪽 영향은 측정 범위 밖) |
| 정책 | 동의어 묶음, 다른 사람·과거 표현 목록, 검체 문맥 규칙은 임상 검수 전. `specimen.MULTI_SPECIMEN` 계약(L)은 그대로 |

## 테스트

| 트리 | 결과 |
|---|---|
| 기준 285db00 (= 29a7ae3b, 수정 전) | 5330 passed, 9 skipped, 1 xfailed (N 감사 때 같은 tree) |
| M d06d4cb (코드 고정) | **5461 passed, 9 skipped, 1 xfailed**, exit 0 (285s). 새 테스트 130 + 기대값을 바꾼 기존 2 |
| Q `integration/q-final-2` f3c8d71 + M, 병합만 | 2 failed (`test_recovery_stress` 고정값), 5566 passed, 9 skipped, 4 xfailed. 이와 별도로 N의 M xfail 2건은 XPASS(strict) |
| Q + M + `m_q_merge_followups.patch` | **5568 passed, 9 skipped, 4 xfailed**, exit 0 (494s) |
| N strict xfail (Q+M에서 `--runxfail`) | `test_E_conflicting_narrative_stops_confirmation`, `test_date_window_lab_is_evaluated_deterministically` 2 passed |
| J/L 회귀 (`test_value_context`, `test_specimen`, `test_lab_value_semantics`, `test_p0_*`, `test_value_grounding`) | 전체 실행 안에서 통과 |
| held-out 측정 | `PYTHONPATH=src python scripts/eval_m_holdout.py docs/dev` |
