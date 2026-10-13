# 질문 계획 오프라인 비교 평가 (Phase 4-1)

기존 질문 생성 경로(baseline)와 Decision Loop 질문 계획(candidate)을 같은 조건에서 비교하는 오프라인 평가 틀입니다.

**결과는 오프라인 대역(LLM 없음) 기반 구조 지표입니다.** 다음을 뜻하지 않습니다.
- 실제 질문 품질 향상
- 추천 정확도 향상
- LLM 비용 절감
- 실제 사용자 질문 수 감소

| 파일 | 역할 |
|---|---|
| `src/ctrec/question_eval.py` | 정답 분리, 오프라인 대역, 두 질문 생성 방식, 답변 시뮬레이터, 공통 질문 표현, 지표, 실행기, 비용 추정·승인 게이트 |
| `scripts/eval_question_plans.py` | 실행기 CLI (전체·일부·무작위 표본 `--sample N --seed`, `--ablation`, `--out`, `--validate-only`, `--dry-run-cost`) |
| `tests/test_question_eval.py` | 정답 누출, 공정성, 매핑, 지표, 차단·게이트, 저장 테스트 |

## 1. 구조와 데이터 분리

| 구분 | 내용 |
|---|---|
| 정답 | `CaseGroundTruth`: 숨긴 사실 ID, 숨긴 기준 → 규칙 대응, 정답 라벨, 숨긴 사실 원문(답변 시뮬레이터 전용 private 필드) |
| 모델 입력 | `pipeline._case_from_dict`가 만든 `PatientCase`. `_HIDDEN_KEYS`(oracle_facts, hidden_facts, masked_* 등)를 빼며, 평가 객체에서도 oracle_facts·hidden_details를 지움 |
| 답변 | `OracleAnswerer`만 정답 원문을 봄. 질문 대상 규칙이 숨긴 기준에 **EXACT**로 대응할 때만 숨긴 사실 원문을 답으로 주고, 그 밖에는 '모름' |
| 답의 기록 | 답은 Session의 '추가 확인 정보'로 들어가 자기 보고 사실이 되며, 검증 상태로 승격되지 않음 |

## 2. 비교 방식과 공정성

- **baseline**
  - 실제 `Session.clarify → question_generator.generate_questions` 경로를 그대로 탑니다.
  - 생성기만 결정적 대역(`baseline_stub_first_unknown`)입니다. 생성기가 받는 입력(판정 보류 시험의 `unknown` 목록과 확보 단계 계획)을 받은 순서대로 고르며, 질문 하나는 기준 하나입니다.
  - 실제 LLM 생성기의 질문 묶음·우선순위와 다릅니다.
- **candidate**
  - 같은 Session을 `decision_loop="diagnostic"`으로 실행합니다.
  - 매칭 직후 진단의 ASK_USER 계획(우선순위 순)을 질문으로 바꾸는 평가용 어댑터를 씁니다.
  - 실제 추천 경로에는 연결하지 않습니다.
- **ablation_protocol_tiebreak** (`--ablation`, 평가 전용, 채택된 정책 아님)
  - 같은 Decision Policy 후보·단계·가치 점수를 쓰되, 동점만 프로토콜 순서(후보 시험 순서 → 규칙 순서)로 정합니다.
- **두 방식에 같은 것:**
  - 환자 입력과 후보 시험
  - 질문 예산: 라운드당 질문 수(`--budget`, 기본 3)와 라운드 수(`--rounds`, 기본 2)
  - 종료 조건(`Session.needs_clarification`), 답변 시뮬레이터
  - 재추출·재매칭 경로, Matcher 대역
- **같은 상태 보장:**
  - 라운드 1은 세 방식이 같은 사실 스냅샷(`fact_version`)과 같은 Matcher 판정입니다. 실행마다 확인합니다(`round1_matcher_identical`).
  - 라운드 2부터는 각자 받은 답에 따라 상태가 갈라집니다. 각 방식이 자기 질문의 결과를 반영하는 것이 정상입니다.

## 3. 공통 질문 표현 (`_record`)

| 필드 | 내용 |
|---|---|
| `case_id`, `method`, `round_id`, `question_id` | 식별 |
| `question_ref` | 비식별 참조. 질문 문장은 대역의 합성 문장이라 저장하지 않음 |
| `fact_version` | 질문 시점의 사실 스냅샷 버전 |
| `target_criterion_ids`, `target_trial_ids` | 명시 연결 (두 방식 모두 생성기가 직접 줌) |
| `target_fact_ids` | 빈 목록. 질문 생성기가 사실 ID를 주지 않으므로 꾸미지 않음 |
| `normalized_target` | `decision_state.info_key`. 구조화 필드 또는 바이오마커 표적만 묶음 (예: `biomarker:EGFR` ≠ `lab:egfr`) |
| `mapping_status`, `mapping_confidence` | `EXACT_HIT`(1.0) / `INFERRED_HIT`(0.5) / `MISS` / `UNMAPPED` |
| 그 밖 | `question_type`, `priority`, `source`, `relevance`(질문 시점 분류), `same_criterion_reasked`, `answered_reasked` |

숨긴 기준 → 규칙 대응(`map_criterion`, 평가기 전용):

| 상태 | 조건 |
|---|---|
| EXACT | 문장이 규칙 문장과 같음 |
| INFERRED | 한쪽이 다른 쪽을 포함하는 규칙이 하나 (20자 이상) |
| AMBIGUOUS | 위 조건의 규칙이 여럿 (고르지 않음) |
| INFERRED_SPLIT | 규칙 문장들이 기준 문장 안에 나뉘어 있음 |
| UNMAPPED | 그 밖 |

## 4. 지표 (분자 / 분모 / 제외)

| 지표 | 분자 | 분모 | 제외 |
|---|---|---|---|
| `coverage_exact` | 숨긴 기준의 EXACT 규칙을 대상으로 한 질문이 하나라도 있는 사례 | EXACT 정답이 있는 정상 사례 | 정답 없는 사례(full), 실패 사례(따로 보고) |
| `coverage_exact_within_budget` | 위 조건을 첫 라운드 예산(질문 수) 안에서 만족 | 같음 | 같음 |
| `coverage_exact_or_inferred` | EXACT 또는 INFERRED 규칙을 대상으로 한 사례 | 둘 중 하나라도 정답이 있는 사례 | 같음 |
| `median_questions_to_first_exact_hit` | 처음 EXACT 대상까지의 질문 수 중앙값 | EXACT로 맞힌 사례 | - |
| `duplicate_same_criterion_rate` | 이전 질문과 같은 기준을 다시 대상으로 한 질문 | 전체 질문 | - |
| `duplicate_semantic_rate` | 기준은 다르지만 같은 정보 키를 다시 묻는 질문 | 전체 질문 | - |
| `reasked_answered` | 답('모름' 아님)을 받은 기준을 다시 묻는 질문 수 | - | - |
| `irrelevant_rate` | 모든 대상이 이미 충분하거나, 확정 불충족 시험(닫힌 경로)만을 위하거나, 후보 시험 밖인 질문 | 전체 질문 | 잠정 불충족은 불필요로 세지 않고 `provisional_mismatch_targets`로 따로 셈. OR/코호트의 열린 경로는 관련 있음으로 봄 |
| `unanswered_rate` | 답이 '모름'인 질문 | 전체 질문 | - |
| `revealed_answers` | 숨긴 사실이 공개된 질문 수 | - | - |
| `gt_resolved_after` | 마지막 판정에서 정답 기준이 MET/NOT_MET이 된 수 (판정 해소, 대상 맞힘과 별개) | 정답 기준 수 | - |
| `final_label_agreement` | 마지막 시험 판정이 정답 라벨과 같은 쌍 (판정 개선, 대상 맞힘과 별개) | 라벨이 있는 쌍 | - |
| `safety.*` | 아래 4개 항목의 위반 건수 (매 라운드 Decision State로 확인, 0이어야 함) | - | - |
| `round1_matcher_identical` | 라운드 1 Matcher 판정이 방식과 무관하게 같음 | - | - |

`safety` 확인 항목:
- 상충 근거로 MET 판정
- 문서 검증 상태 사실(승격 여부)
- 불충족 근거에 NOT_MET이 아닌 기준
- NOT_EVALUATED 시험의 불충족 분류

## 5. 실행

```bash
PYTHONPATH=src python scripts/eval_question_plans.py --validate-only
PYTHONPATH=src python scripts/eval_question_plans.py --ablation              # 화면 출력만
PYTHONPATH=src python scripts/eval_question_plans.py --sample 20 --seed 7 --out outputs/eval/qplan
PYTHONPATH=src python scripts/eval_question_plans.py --dry-run-cost --max-cost-usd 50
```

- 기본 실행은 파일을 쓰지 않습니다. `--out`을 줄 때만 `summary.json`과 사례별 `cases.csv`를 씁니다. 사례·시험·기준 ID, 숫자, 상태 코드만 담고 원문은 담지 않습니다.
- 실패·취소·시간 초과 사례는 `failed_cases`로 남고 지표 분모에서만 빠집니다.
- 평가 중에는 `llm.client`, `llm.create`, `llm.plain`이 차단되어 실제 API 호출이 불가능합니다.

## 6. 유료 평가 dry-run과 승인 게이트

- `estimate_cost`는 고정 순서 실행(`run_fixed`) 기준의 단계별 호출 수 상한을 계산합니다.
  - 환자 정보: 1 + 라운드 수
  - Matcher: 시험 수 × (1 + 라운드 수) × 재시도 비율
  - 질문 생성, 답변 시뮬레이터: 라운드 수
  - 추천: 1
- 토큰 수와 모델 단가는 **가정값**입니다(`prices_confirmed=false`, `tokens_confirmed=false`). README의 환자당 $0.2–0.7은 참고값일 뿐입니다.
- `require_paid_approval`이 요구하는 조건:
  - `--approve-paid-run`
  - `CTREC_ALLOW_PAID_EVAL=1`
  - 비용 상한 이내
  - 확인된 단가
- **Phase 4-1에서는 모든 조건을 갖춰도 유료 실행 경로를 제공하지 않습니다.**

## 7. 한계

`question_eval.LIMITATIONS`를 함께 보세요.
- Matcher 대역은 LLM 판정 기준을 모두 '판정 불가'로 둡니다. 그래서 숨긴 사실을 공개해도 LLM 기준은 해소되지 않고(`gt_resolved_after`=0), 라벨 일치도 낮습니다. 판정 개선은 오프라인으로 측정할 수 없습니다.
- 대역 상태에서는 대부분의 시험이 판정 보류로 남아 '불필요 질문' 지표가 두 방식을 가르지 못했습니다(0).
- 숨긴 기준 90개 중 18개는 EXACT가 아닙니다(INFERRED 8, UNMAPPED 10). 기준표 파서가 한 기준을 여러 원자 규칙으로 나눈 경우가 많습니다.
- 답변 시뮬레이터는 질문 문장 품질을 보지 않고, 대상 규칙 일치만 봅니다.

## 8. Phase 4-2: 동점 처리 정책 검증 (평가 전용)

**Phase 3 Decision Policy는 바꾸지 않았습니다.** `src/ctrec/question_priority.py`는 평가 코드에서만 쓰는 동점 처리 정책 A–E입니다.
- 다섯 정책 모두 같은 후보, 같은 단계(tier), 같은 가치 점수를 씁니다.
- 같은 단계·같은 점수의 동점만 다르게 정합니다.
- 어느 정책도 정답을 보지 않습니다.

정책 정의는 파일 머리말을 보세요.

- **검증셋:** `scripts/build_validation_set.py` → `data/patients/masked_validation/validation_cases.json`
  - LLM 없이 결정적으로 만든 합성 100개입니다(seed 고정).
  - 개발셋 정답 기준 재사용 0개, 선정 56 / 제외 50.
  - 사례 유형: single, cohort(OR 경로), shared(여러 시험 공통 정보), provisional(나이로 잠정 불충족인 시험 포함).
- **독립성 한계:**
  - 모집 중 시험 23개를 개발셋도 모두 써서 시험 단위로 겹칩니다.
  - 숨김 대상은 층화 무작위 선택이며, "판정을 가르는 기준"이라는 임상 판단이 아닙니다.
  - 확정 불충족 사례는 만들 수 없습니다(문서 검증 경로 없음).
- **비교 실행:** `scripts/eval_question_priority.py`
  - 개발셋과 검증셋을 따로 평가합니다.
  - 정답 기준 단위로 선정·제외, 기준 유형, 사례 유형별 결과를 냅니다.
  - 개발셋에는 매핑 실패 원인 분석과 완화 매핑 민감도 분석을 함께 냅니다.

## 9. Phase 4-3: 평가 충실도 (fidelity)

Phase 4-2의 정책 실행 경로는 평가기가 상태를 다시 만들었습니다(`generator_factory`). 이 경로는 다시 매칭되지 않은 시험의 오래된 LLM 판정 처리를 빠뜨려, 실제 루프와 질문이 달랐습니다(검증셋 2개·보충 세트 2개 사례).

Phase 4-3부터 정책 후보는 다음 경로로 실행합니다.
- Session 안의 **실제 Decision Loop**(`DecisionTracker → run_loop`)를 그대로 돌립니다.
- `question_priority.tie_order`가 루프가 부르는 `candidates` 결과에서 **우선순위가 같은 ASK_USER 묶음의 순서만** 바꿉니다(평가 중에만 바꿔 끼우고 원래 함수로 되돌림).
- 상태 구성, 후보 생성, 안전 검사, 지문, 예산, 종료 판단은 모두 실제 코드입니다. 기본 코드와 동작은 바뀌지 않습니다.
- D(균형)는 루프의 실행 이력을 보고, 이번 루프에서 덜 물은 종류부터 고릅니다.
- `R_random_1..3`은 동점 안을 seed로 섞는 기준선입니다.

`scripts/eval_question_fidelity.py`는 다음을 사례별로 비교합니다.
- 정책 A(순서 변경 없음)와 실제 루프: 후보 목록, 단계, 점수, 우선순위, 제외 사유, 선택된 행동, 예산, 종료 사유, 사실 버전, 오래된 판정 처리, 질문
- 실행마다 새로 만들어지는 사실 ID는 등장 순서 번호로 바꿔 비교합니다.

보충 합성 세트(`data/patients/masked_validation/supplement_cases.json`, `--supplement`)도 추가했습니다.
- cohort 10, confirmed 15, biomarker 2
- **임상적 유효성은 검증되지 않았습니다.**
- confirmed 사례는 평가용 상태 구성(`setup_verified_fields`)으로 나이 사실을 문서 검증된 것으로 두어, 확정 불충족을 만듭니다.
