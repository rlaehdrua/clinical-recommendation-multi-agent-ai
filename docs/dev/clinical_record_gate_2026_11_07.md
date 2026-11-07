# 미검증 clinical_record 확정 차단 (2026-11-07)

기준 `origin/integration/dcvc` `b2100d8`. 입력: N2 `229969c`, expert_boundary `8bf1574`, P0 후보 `16af881`, R2 `d20865a`, PR #20 `aca2033`, PR #22 `5f7d66b`, PR #25 `6e9ca5b`.

**로컬 통합 후보입니다.** push, PR, 병합은 하지 않았습니다. 값은 합성이고 LLM 자리는 고정 대역입니다. 실제 API 호출은 0건입니다.

## 결론

- `input_source="clinical_record"`, `document_verified` 상태 문자열, 문서 참조 문자열은 더 이상 진료 기록 필요 기준을 확정하지 않습니다. 기본 Session, ContractSession, OfflineEvaluator가 같은 게이트를 지납니다.
- 운영에는 서버 문서 검증 계약이 없습니다. 그래서 가짜 검증을 넣지 않고 운영 권한을 "아무것도 인정하지 않음"으로 두었습니다.
- **대가가 큽니다.** 저장 시험 32개(고정 26, NSCLC 6) 전부에 진료 기록 필요 기준이 있으므로, 문서 계약이 생기기 전에는 운영 경로에서 이 시험들의 `CONFIRMED_ELIGIBLE`이 0입니다.

## 설계

| 층 | 변경 |
|---|---|
| `document_authority.py` (신규) | 검증을 인정하는 유일한 지점. 기본 `NoDocumentAuthority`. 컨텍스트 값이라 `use()` 밖으로 새지 않음 |
| `fact_store.document_backed` | 마지막 이력의 증명 + 값 묶음(`fact_digest`) + 현재 권한의 인정을 모두 요구 |
| `fact_view.tier`, `gate` | `document_backed` 기준. 통과는 값마다 문서 필요 (`document_coverage`). 선언된 불충족은 잠정 유지 |
| `matcher.match_trial` (보호) | `evidence_basis.enforce`를 N2 정책 뒤, Q&A·임상의 경계 앞에서 적용. `document_check`가 없으면 보류 |
| `pipeline.Session` (보호) | `document_check` 전달 |
| `decision_state` (보호) | MET인데 문서 범위가 안 되면 gap `unverified`. overlay, 저장된 ELIGIBLE도 같은 요건 |
| `engine/contracts` | `EvidenceRef.verified`는 문서 ID 필요. `clinical_record` 충족은 참조 필수 |
| `cohort_state`, `proposition_store`, `contract_bridge` | 같은 판단을 전달. 미증명 계약 사실은 clinical_record가 아님 |
| `sim/evaluator` | `SimDocumentAuthority`를 평가 범위에서만 켬. 시뮬레이터 문서 행동만 증명을 발급 |
| `recommender` | 선언 값을 "환자 답변상"으로 부르지 않음 |

R2 대안 A는 문서 참조 문자열을 인정하므로 그대로 쓰지 않았습니다. B의 형태를 따르되 `AUTHORITY` 전역 변수 대신 컨텍스트 값을 쓰고, 증명을 사실 값에 묶었습니다.

## R2 전체 실패 2건

`tests/test_session_fact_compare.py`의 두 건입니다.

| 테스트 | 원인 | 처리 |
|---|---|---|
| `test_deterministic_comparison_keeps_results_identical` | A는 판정만 보류하고 `fact_view.gate`는 선언을 통과시켜 두 층이 갈라졌습니다. **기대값이 아니라 패치가 틀렸습니다.** | `gate`를 같은 요건으로 고쳐 기대값 변경 없이 통과 |
| `test_strict_mode_reports_declared_records` | 기준선부터 보류되므로 `met_to_unknown` 변화가 사라집니다. **기대값이 낡았습니다.** | `UNKNOWN` 양쪽 + `declared_unverified`로 명시 변경 |

R2가 계산한 27건은 N2 + R5 기준이고, R5는 이번 구성에 없습니다. 이번 트리에서 게이트 직후 실패한 83건 중 1건은 커밋 전 보호 파일 검사였고, 나머지 82건은 세 갈래입니다.
- 대조군이 "문서 확인됨"을 뜻하던 테스트: 합성 권한 증명으로 이동 (`tests/document_support.py`). 단언 유지.
- 게이트가 주제가 아닌 단위 테스트: `document_check` 대역 주입. 단언 유지.
- 선언 확정 자체를 기록한 테스트: 보류로 변경하고 증명 대조군 추가.

안전성 단언을 약화한 것은 없습니다. 삭제한 테스트는 `mark` 도우미(대안 C용)와 "게이트가 어디에도 연결되지 않음"을 고정한 R2 테스트 2건이며, 후자는 연결 확인 테스트로 바꿨습니다.

남은 strict xfail 3건은 이번 범위 밖입니다: C2 표시 2건, 분류 신호 밖 임상 판단 1건.

## N2와 전문가 판단 패치의 matcher 충돌

- `git merge` 충돌은 없습니다. 제안 패치는 import 줄 때문에 `git apply`가 실패합니다 (N2가 `value_grounding`을 추가). 3-way도 실패해 수동 적용이 필요합니다.
- 의미 충돌도 없습니다. 순서는 N2 정책 → 문서 게이트 → Q&A → 임상의 경계 → 집계입니다. 임상의 경계가 마지막이라 문서 대기가 아니라 `physician_review`로 끝납니다.

## 측정

재현: `scripts/measure_declared_record.py` (게이트 전 트리는 `WT`로 지정). 기준선은 같은 PR 조합에 게이트만 없는 `9d704b2`입니다.

| 입력 (고정 26개 시험, 최악 LLM 대역) | 기준선 CE | 후보 CE |
|---|---|---|
| `unknown`, `self_report` | 0 | 0 |
| `clinical_record` 선언 | 5 | **0** |
| 상태 문자열 위조 | 5 | **0** |
| 문서 참조 문자열 | 5 | **0** |
| 합성 서버 증명 (대조군) | - | 5 |

Session과 ContractSession 값이 같습니다. R2의 12와 다른 이유는 하네스가 다르고 임상의 경계가 먼저 들어갔기 때문입니다. 비율이 아니라 상한 측정입니다.

| 측정 | 기준선 | 후보 |
|---|---|---|
| 고정 사례 140쌍 판정·상태·추천 | 기준 | **차이 0** (단 ELIGIBLE 0쌍이라 적격 쪽 근거가 아님) |
| 시뮬레이터 36행 CE 합계 | 25 | 25 |
| 시뮬레이터 36행 CI 합계 | 38 | 43 |
| 행동 수·순서가 바뀐 행 | - | 18/36 |

## 재현 검증

게이트 층을 하나씩 끈 변이 6종(Matcher, decision_state, 권한 확인, 상태 문자열, 시뮬레이터 연결, gate)이 모두 회귀 테스트에서 실패했습니다.

## 팀 결정 필요

1. `CONFIRMED_ELIGIBLE`의 뜻과, 운영에서 CE 0을 받아들일지.
2. 서버 문서 계약: 저장소, 발급, 서명, 키. `DocumentAuthority` 어댑터의 소유자.
3. 선언된 불충족을 잠정 부적격으로 유지하는 비대칭.
4. `EvidenceRef.verified`와 `CriterionResult` 강화 (공유 계약 변경).
5. WP6의 `SimDocumentAuthority` 가정: 결과는 문서 계약이 있는 미래를 측정합니다.
6. `web/cohort_board`의 `mismatch="confirmed"` 가정 (아래).

## 남은 경로

- **`web/cohort_board.py`:** Matcher를 거치지 않는 합성 코호트 현황판입니다. 공개 값을 "문서 검증된 불충족"으로 간주하고 전 기준이 연결되면 CE도 냅니다. 요청 범위 밖이고 PR #25 소관이라 바꾸지 않았습니다.
- **기준 분류:** 게이트는 `required_evidence`를 믿습니다. 저장 메타가 `self_report_ok`이면 적용되지 않습니다. `other` 유형의 LLM 제안이 여기 해당합니다.
- **측정값 아닌 표현 11건:** `self_report_ok` 기준에서는 N2 표지 공백이 그대로입니다.
- **실제 LLM:** 인용이 사실로 이어지는 비율은 측정하지 못했습니다. 연결되지 않으면 문서가 있어도 보류됩니다.
- **임상의 입력:** 입력 계약이 없어 임상의 전용 기준은 계속 `EXPERT_REVIEW`입니다.
- **표시:** 보류된 항목의 `evidence_source`는 `record`로 남습니다. 웹 `display._said`는 답변 값으로 해석합니다. 웹 입력은 선언이 없어 지금 닿지 않습니다.
