# 임상의 판단 기준 자동 확정 우회 점검 (2026-11-07)

기준: `integration/dcvc` `b2100d8`, PR #20 `aca2033`, PR #22 `5f7d66b`, PR #25 `6e9ca5b`.
값은 모두 합성이고 LLM 자리는 고정 대역입니다. **스텁이 답을 고정한 최악의 경우이며, 실제 LLM의 발생 빈도는 측정하지 않았습니다.**

## 1. 재현된 자동 확정 경로

규칙: `NCT07680790:E3`, `NCT07098338:I4`(플래그 있음), `NCT07098338:I5`, `NCT07098338:E10`(플래그 없음).

| 경로 | 입력 | 수정 전 |
|---|---|---|
| 실제 Session | A 기록 출처 + LLM 통과, D 문서 표기 사실 | 8건 `CONFIRMED_ELIGIBLE` (+ `E10`의 출처 불명 입력 1건, 합계 9/20) |
| 실제 Session | B LLM 실패 | `INELIGIBLE`, `CRITERIA_MISMATCH`, 추천 제외 |
| OfflineEvaluator + 판정기 (PR #20) | C 임의 reviewed 연결, D 문서 검증 사실 | 6건 `CONFIRMED_ELIGIBLE` |
| OfflineEvaluator + 판정기 (PR #20) | 같은 연결 + 실패값 | 3건 `CONFIRMED_INELIGIBLE` |

- 기록은 `adjudication=fact_llm, decided_by=llm`입니다. 그래서 계약의 "전문가 판단 기준은 임상의만 확정" 검증이 동작할 기회가 없습니다.
- `needs_physician_review`는 `passes is None`일 때만 동작합니다.
- 규칙 엔진 경로는 재현되지 않습니다. 후보 규칙은 전부 `structured_evaluable=False`입니다.

### 파서 플래그만으로는 부족함

`slot_candidates.csv`의 임상 판단 후보 16건 중 저장 규칙에서 찾은 10건을 대조했습니다.

| 슬롯 | 저장 규칙 | `underspecified` |
|---|---|---|
| `judge.life_expectancy` | `NCT07098338:I5`, `NCT07027514:I7`, `NCT06928389:I5` | **모두 False** (`I5`는 `vague_suspected`) |
| `cm.cardiac` | `NCT07098338:E10` | False |
| `cm.cardiac` | `NCT07680790:E3` | True |
| `dx.disease_setting` | `NCT07680790:I2` | False |
| `judge.investigator_unsuitable` | 4건 | True |

저장 규칙 전체의 플래그는 49건입니다(고정 39, NSCLC 10). 나머지 후보 6건(슬롯 기준)은 파싱 저장본이 없는 `NCT05255302`, `NCT04892953`에 있어 막을 규칙 자체가 없습니다.

### E 입력 (임상의 판단 완료)

사용하지 않았습니다. `decided_by="clinician"`은 타입과 검증기에만 있고 값을 만드는 코드가 없습니다. `clinician_review` 정보원도 사실을 만들지 않습니다. 검증 가능한 계약이 없으므로 가짜 완료 상태를 만들지 않았습니다.

## 2. 차단 계층

| 계층 | 판단 |
|---|---|
| `matcher.match_trial` (보호) | **공통 경계.** 기본 Session, 웹, OfflineEvaluator가 모두 지나고, 모든 판정 경로 뒤·집계 앞입니다 |
| `decision_state.build_state` (보호) | `underspecified`가 상태에 남지 않아 여기서 복원할 수 없습니다 |
| `cohort_state` | 분류 신호가 없고, 막아도 `match.eligibility`와 추천은 그대로라 층이 어긋납니다. 채택하지 않았습니다 |
| `sim/judge.py` (PR #20) | 판정기만 막으면 Session이 남습니다. 판단 슬롯 보조 패치로만 둡니다 |
| `ContractSession` (비보호) | 판정을 다시 합성하는 자리가 있어 **실제 코드로 적용했습니다** |

## 3. 경로별 상태

| 상태 | 경로 |
|---|---|
| **코드로 해결** | `ContractSession`(`CTREC_SESSION=contract`)의 A·B·D, 계약 평가기가 낸 판정, `contract_bridge` 분류 |
| **패치 승인 후 해결** (임시 worktree 측정) | 기본 Session·웹·추천의 A·B·D, `OfflineEvaluator`의 C·D |
| **PR #20 패치 추가 시** | 미분류 기준을 판단 슬롯에 연결한 경우 `CONFIRMED_ELIGIBLE` → `INFO_MISSING` |
| **여전히 우회 가능** | 아래 |

기본 Session은 지금 이 브랜치에서도 **우회가 그대로입니다.** `tests/test_expert_boundary.py`의 `matcher_pending` 13건은 재현 테스트입니다.

### 남은 우회

1. **분류 밖 기준.** 플래그도 목록도 없는 기준은 동작하지 않습니다.
   - 표현 탐색(판단 근거가 아니라 조사용)으로 47건이 잡혔습니다. 예: `NCT07027514:E14`, `NCT06468397:E1`("in the opinion of the investigator"), `NCT05127980:I4`·`E1`("treating clinician"), `NCT00575185:E3`.
   - `NCT07027514:E14`는 strict xfail로 표시했고, 일반 슬롯에 연결하면 패치 후에도 `CONFIRMED_ELIGIBLE`임을 측정했습니다.
   - 휴리스틱으로 막지 않았습니다. 일부는 임상 판단이 아니어서 오차단이 되고, 막아도 완전하지 않습니다.
2. **재파싱.** `EXPERT_RULES`는 기준 ID로 묶습니다. ID가 바뀌면 원문 대조 테스트가 실패해 알려 주지만, 런타임에서 막지는 않습니다. NSCLC 재파싱(R4)이 대기 중입니다.
3. **신규 시험.** 파서가 플래그를 놓치면 목록에 오르기 전까지 열려 있습니다. `NCT05255302`, `NCT04892953`이 해당합니다.
4. **`vague_suspected` 5건**(목록의 `NCT06928389:I5` 제외). D6가 자동 적용을 금지한 상태이므로 건드리지 않았습니다.

## 4. 기존 판정 영향

| 측정 | 결과 |
|---|---|
| 고정 104쌍 (패치 전후) | 판정·상태·추천·행동·기준 차이 **0**. 다만 오프라인에서 LLM이 판정을 내지 않아 ELIGIBLE이 0건이라 **변별력이 없습니다** |
| 독립 대조군 표현 112건, 조합 900건 | 패치 전후 동일, 불일치 0 |
| 측정값 아닌 표현 33건 | 패치 전후 동일하게 33건 불일치. 이번 범위 밖 (N2 영역) |

**실제 비용은 104쌍에 보이지 않습니다.** 임상의 판단을 입력할 수단이 없으므로, 공통 기준에 분류 기준이 있는 시험은 적격에 닿을 수 없습니다.

- 고정 시험 **17/26개**, NSCLC **5/6개**
- 저장 라벨 중 그 17개 시험의 ELIGIBLE: `masked_labels` 55/83건, `synthetic_labels` 21/35건

실제 LLM 평가의 재현율은 그만큼 낮아질 수 있습니다. 측정은 하지 않았습니다.

## 5. 팀 결정 사항

1. **matcher 패치 승인.** 보호 파일, `docs/proposals/expert_boundary_matcher.patch`.
2. **B 입력.** LLM의 불충족도 막습니다(이번 구현). `4ad59b1`은 통과만 막았습니다. 명백한 제외 사유까지 사람에게 넘기는 비용을 받아들일지 정해야 합니다.
3. **"해당 없음" 판정.** 막습니다(이번 구현). `4ad59b1`은 유지했습니다.
4. **임상의 판단 입력 계약.** 없으면 위 시험들이 영구히 적격에 닿지 못합니다. 누가, 무엇으로, 어떤 기록을 남기는지 정해야 합니다.
5. **`EXPERT_RULES` 5건 임상 검수.** 특히 `NCT07680790:I2`(`dx.disease_setting`).
6. **표현 탐색 47건, `vague_suspected` 5건** 검수 범위.
7. **PR #20 판정기 패치.** PR #20·#22 병합 뒤, `docs/proposals/expert_boundary_judge.patch`.

## 6. 충돌

| 대상 | 결과 |
|---|---|
| PR #22, #20, #25 | 자동 병합, 파일 충돌 없음. PR #22 생성기 파일 미변경 |
| 패치 + `night/p0-value-grounding`(N2), `fix/p0-evidence-semantics-day` | `matcher.py` import 줄에서 적용 실패. 호출 줄 수동 추가 필요 |
| `hold/llm-boundary-country` | 적용됨 |
| N4 `test_a_binding_cannot_settle_an_expert_only_criterion` | 패치 후 XPASS로 실패할 것으로 예상, 미실행. 해당 xfail 제거 필요 |
| `4ad59b1` | 같은 자리. 이 패치가 포함하므로 함께 적용할 필요 없음 |
