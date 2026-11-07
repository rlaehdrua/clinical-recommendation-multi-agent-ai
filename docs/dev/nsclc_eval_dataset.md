# NSCLC 축소 범위 평가셋 + 독립 규칙 GT (nsclc_eval_v1)

작성: 서연 (데이터·GT) · 2026-11-08 · 브랜치 `feat/nsclc-eval-gt`

> **검증 수준: 데이터 검증 완료 / 임상 검수 미완료.** 작성자는 임상 검수자가 아닙니다.
> 이 문서의 GT는 **규칙 GT**(검수표 + 합성 환자 값으로 계산)이며 임상 정답이 아닙니다. 수치는 정확도·임상 성능이 아닙니다.

## 1. 데이터 출처

| 항목 | 내용 |
|---|---|
| 프로토콜 원문 | ClinicalTrials.gov (U.S. National Library of Medicine), API v2. 시험별 URL `https://clinicaltrials.gov/study/<NCT>` |
| 조회 시점 | 2026-11-02T16:15:35Z (`data/nsclc/trials/*.json`의 `source.retrieved_at`, `data/nsclc/selection/recommended.md`) |
| 라이선스·이용 조건 | **확인 필요.** ClinicalTrials.gov 이용 약관(Terms and Conditions)과 데이터 재배포 조건을 이 작업에서 확인하지 못했습니다 |
| 파싱본 | `data/nsclc/parsed/*.json` (claude-sonnet-5, 2026-11-03 컴파일, 6개 시험). **원본 보존: 이 작업에서 바꾸지 않음** |
| 환자 | **합성**(생성기). 실제 환자 정보는 없습니다 |

프로토콜 8건 중 평가셋에 들어간 것은 파싱이 끝난 6건입니다. NCT04892953·NCT05255302 2건은 "파싱 미완료 - API 크레딧, 분할 파서 반영됨"이라 빠졌습니다(`data/nsclc/review/protocol_status.csv`).

## 2. 파일 구성: 생성 / 검토 / 정답 분리

| 단계 | 담당 | 파일 |
|---|---|---|
| 검토 (사람) | 서연 데이터 검증 | `data/nsclc/review/criteria_verification.csv`(160개 기준), `protocol_status.csv`, `overrides.json`, `expert_criteria.csv`, `expression_review.csv` |
| 입력 (검토표에서 만듦) | `scripts/build_nsclc_eval.py inputs` | `data/nsclc/eval/inputs/trial_store/*.json`(축소 시험 저장소), `slot_dictionary.json` |
| 입력 (손으로 씀) | 서연 | `inputs/generator_config.json`, `source_catalog.json`, `lab_reference.json`, `source_map.json`(원본 사본 + `sex`) |
| 생성 | 생성기 (`ctrec.cohort.generate`) | `data/nsclc/eval/generated/cohort.json`. 생성기 라벨은 GT가 아닙니다 |
| 정답 | 독립 GT (`ctrec.cohort.gt`) | `rule_gt.csv`(기준별 상태 + 근거 문장), `pair_gt.csv` |
| 묶음 | `scripts/build_nsclc_eval.py gt` | `patients.json`, `pairs.csv`, `label_summary.json`, `manifest.json` |

**GT의 독립성**
- `src/ctrec/cohort/gt.py`는 ctrec의 다른 모듈을 import하지 않습니다. 엔진 Matcher·decision_state·`derive_pair_status`, 공용 rule_engine, 생성기 truth, 시뮬레이터 모두 쓰지 않습니다.
- 테스트로 확인합니다. AST 검사와, import 후 `sys.modules`에 `ctrec`, `ctrec.cohort`, `ctrec.cohort.gt` 세 개만 있는지 봅니다.
- GT는 엔진이나 모델의 예측을 보고 고치지 않았습니다. 생성기 truth와의 비교는 일관성 점검으로만 씁니다(연결 기준 500건 이상에서 일치, 테스트).

## 3. 범위 (팀 결정 3, 160개 기준)

| 구분 | 수 | GT에서 |
|---|---|---|
| 자동 판정: 나이·성별·ECOG | 18 + 지역 조건부 1 | PASS / FAIL (조건부는 아래 §6) |
| 자동 판정: CSV 슬롯이 있는 범주형 | 23 | PASS / FAIL / NOT_APPLICABLE |
| 범위 안이지만 미연결 (검수표 '모호') | 11 | NOT_EVALUATED |
| 검사 수치 4개 (ANC·총 빌리루빈·CrCl·Hb) | **0** | 6개 시험 원문에 기준값이 없음 (§6 결정 D2) |
| 임상 전문가 판단 | 19 | NEEDS_EXPERT |
| excluded: 동의서·절차 (팀 결정 2) | 9 | 규칙 GT를 만들지 않음, 목록만 (`pair_gt.csv` `excluded_criteria`) |
| 범위 밖 | 79 | 평가하지 않음. 평가 저장소에서 뺌 (코호트 전용 기준은 OR 경로 보존용으로 남기고 연결하지 않음) |

- 범주형 범위 안 기준은 34개입니다. 지시의 30개는 CSV 인용 자동 매칭 기준이었고, 원문 대조 중 4건(NCT06928389 I9, NCT06216301 I9, NCT07098338 I8, NCT07416058 E14)을 같은 슬롯으로 바로잡아 더했습니다.
- 34개 중 23개는 연결했고 11개는 모호해서 미연결입니다.

### 자동 판정 가능 범위 vs 임상 전문가 판단 필요 범위 (기준별)

| 시험 | 자동 판정 | 조건부(지역) | 범위 안 미연결 | 전문가 판단 | excluded | 범위 밖 |
|---|---|---|---|---|---|---|
| NCT06216301 | I2, I3, I7, I9, E1, E4, E13, E16 | I1 | I8, E11, E12 | E5, E6 | I10, I11 | 12개 |
| NCT06928389 | I2, I3, I4, I8, I9, E1, E5 | - | - | I5, E8, E9, E10 | I1 | 9개 |
| NCT07027514 | I1, I2, I4, I6, E2(Cohort B), E3, E4, E9, E10, E16 | - | E7 | I7, E14, E20, E21 | I9, I10, I13, I14, I15 | 16개 |
| NCT07098338 | I1, I2, E19 | - | I7(Sub-study 1), I8(Sub-study 2), E9 | I5, I6, E1, E10 | - | 26개 |
| NCT07416058 | I1, I2, I3, I4, I6, I9, I11, E12 | - | I7, I8, E14, E15 | E5, E16, E17 | - | 13개 |
| NCT07680790 | I1, I3, I5, I6, I7 | - | - | I2, E3 | E4 | 3개 |

### 판단 기준 19건 (팀 결정 1·5, 목록: `spec.is_expert_slot`)

| 시험 | 기준 | 판단 슬롯 | 원문 | 파서 underspecified |
|---|---|---|---|---|
| NCT06216301 | E5 | `tx.recent_radiotherapy` | Received radiation therapy to the lung that is > 30 Gy within 6 months of randomization. | false |
| NCT06216301 | E6 | `tx.recent_radiotherapy` | Has received prior radiotherapy within 2 weeks of randomization. Subjects must have recovered ... | false |
| NCT06928389 | I5 | `judge.life_expectancy` | Expected life expectancy of at least 3 months. | false |
| NCT06928389 | E8·E9·E10 | `cm.cardiac` | History of myocarditis, cardiomyopathy, and malignant arrhythmia. | false |
| NCT07027514 | I7 | `judge.life_expectancy` | Have a life expectancy > 12 weeks | false |
| NCT07027514 | E14 | `cm.cardiac` | Have congestive heart failure, active coronary artery disease, ... | false |
| NCT07027514 | E20·E21 | `judge.investigator_unsuitable` | Have other systemic conditions ... / psychiatric problems ... unsuitable | true |
| NCT07098338 | I5 | `judge.life_expectancy` | Life expectancy ≥ 12 weeks | false |
| NCT07098338 | I6 | `other.tumor_tissue` | Provision of acceptable tumour tissue | false |
| NCT07098338 | E1 | `judge.investigator_unsuitable` | As judged by the investigator, any severe or uncontrolled systemic diseases ... | true |
| NCT07098338 | E10 | `cm.cardiac` | Uncontrolled or significant cardiac disease | false |
| NCT07416058 | E5·E17 | `cm.cardiac` | NYHA class III or IV heart failure / Clinically significant cardiovascular disease, including but not limited to: | false |
| NCT07416058 | E16 | `judge.investigator_unsuitable` | Any other serious medical condition ... in the opinion of the investigator ... | true |
| NCT07680790 | I2 | `dx.disease_setting` | ... or recurrent disease not amenable to curative surgery or definitive radiotherapy ... | false |
| NCT07680790 | E3 | `cm.cardiac` | Participants must not have clinically significant cardiac disease | true |

- 파서 `underspecified`가 false인 판단 기준이 14건입니다. 엔진의 `needs_physician_review`는 `underspecified`를 보므로, 그대로 두면 이 기준들은 EXPERT_REVIEW가 아니라 확보 시도 → BLOCKED로 갈 수 있습니다(#20 리뷰 2).
  - 평가 저장소에서는 판단 기준을 `underspecified=true`로 표시했습니다(`eval_derivation.underspecified_set_for_expert`). #37 `mark_expert`와 같은 처리이며, 원본 파싱본은 그대로입니다.
- 검체·시점 요구조건이 있는 범위 안 기준은 `data/nsclc/review/expert_criteria.csv`에 판단 기준 후보로 표시했습니다(예: NCT07416058 I8 PD-L1 중앙검사실·클론·검체 2년 이내).
- `cm.serious_organ_disease`·`cm.immune_onc`(팀 결정 5)는 신증후군 슬롯입니다. NSCLC 기준에는 쓰이지 않습니다.

## 4. GT 정의

규칙 수준 (기준 하나, `rule_gt.csv`의 `status`와 `basis`)
- `PASS` / `FAIL`: 슬롯 값을 검수표의 연산자·값과 비교한 결과입니다. 제외 기준은 성립하면 FAIL입니다.
- `NOT_APPLICABLE`: 적용 조건(예: 여성만)에 해당하지 않는 경우입니다. **통과로 세지 않습니다.**
- `NEEDS_EXPERT`: 판단 기준입니다. 값·정답을 만들지 않습니다.
- `NOT_EVALUATED`: 범위 안 미연결(모호)이거나, 지역 조건이라 국가를 모르는 경우입니다.

쌍 수준 (`pair_gt.csv`의 `gt_label`. `runner_label`은 #20·#37 runner 문자열)

| GT 라벨 | runner 라벨 | 조건 |
|---|---|---|
| `INELIGIBLE` | `ineligible` | **모든** OR 경로(공통 + 코호트 전용 기준)에 FAIL이 있음 |
| `NOT_EVALUATED` | `unlabeled` | 살아 있는 가장 나은 경로에 다음 중 하나가 있음: NOT_EVALUATED 기준 / 실질 경로가 아님(코호트 시험인데 그 코호트 전용 기준이 범위 안에 없음) / 실질 통과 기준(인구학·절차 외 PASS)이 없음 |
| `PENDING_EXPERT_ELIGIBLE` | `eligible_pending_expert` | 그 밖에 NEEDS_EXPERT가 남음 (판단 기준 외 확인 완료) |
| `CONFIRMED_ELIGIBLE` | `eligible` | 범위 안 판단 기준이 하나도 없음. **6개 시험 모두 판단 기준이 있어 0건이 정상** |

**이름 매핑 (하나로 통일)**

| 출처 | 이름 | 같은 개념 |
|---|---|---|
| 팀 결정 1 | 전문가 판단 대기 적격 | ← 이 이름으로 통일 |
| 독립 GT | `PENDING_EXPERT_ELIGIBLE` | GT 라벨 |
| #37 | `eligible_pending_expert` (라벨), `correct_expert_ready` (지표) | 라벨 / 시스템 EXPERT_REVIEW ∧ GT 라벨 |
| 역할 정리 문서 | "Expert-ready" | 저장소·원격 브랜치에서 원문 미확인 |
| 독립 GT `score()` | `expert_ready_with_gt_pending` | 주 지표 = 시스템 EXPERT_REVIEW ∧ GT 전문가 판단 대기 적격 |

PR #26(R5)과 다른 점은 보고만 합니다(코드 미변경).
- R5의 실질 기준은 `applicable ∧ category ≠ consent_or_logistics`로, 나이·성별도 실질 기준으로 칩니다. 이 GT는 인구학 기준만 통과한 경우를 적격 근거로 보지 않습니다.
- 절차 기준도 R5는 `consent_or_logistics`만 뺍니다. 이 GT는 팀 결정 2에 따라 피임 동의·생검 의향도 excluded로 둡니다.

## 5. 평가셋 (seed·해시)

| 항목 | 값 |
|---|---|
| 설정 | `data/nsclc/eval/inputs/generator_config.json`, 시나리오 `nsclc-eval-v1`, 기준일 2026-11-07 |
| 시드 | base_seed 20261107 → 코호트 seed 266646441971616 |
| 설정 해시 (`config_hash`) | `a549da3b1171fd52` (생성기 `cohort-gen-2`) |
| 내용 해시 (`manifest.content_hash`) | `ec277a2796e37bfce63387bc93bd8f830d3675a53ff4832271fa67a130e3d469` (줄바꿈 LF 정규화) |
| 규모 | 80명 × 3시험 = 240쌍 (시험당 37~43쌍) |
| 결정성 | 같은 설정으로 2회 생성 → 같은 해시 (빌드 단계와 테스트에서 확인) |

**라벨 분포** (목표는 `generator_config.json`의 `design_targets`. 사전 분포만 조정했고 GT는 고치지 않음)

| 시험 | 확정 적격 | 전문가 판단 대기 적격 | 부적격 | NOT_EVALUATED |
|---|---|---|---|---|
| NCT06216301 | 0 | 0 | 6 | 31 |
| NCT06928389 | 0 | 13 | 30 | 0 |
| NCT07027514 | 0 | 0 | 27 | 11 |
| NCT07098338 | 0 | 0 | 6 | 34 |
| NCT07416058 | 0 | 0 | 31 | 11 |
| NCT07680790 | 0 | 10 | 30 | 0 |
| **합계 (240)** | **0** | **23 (9.6%)** | **130 (54%)** | **87 (36%)** |

- 목표 대비: 판단 대기 적격의 목표 "10% 이상"에 못 미쳤습니다(9.6%). 부적격(30-60%)과 NOT_EVALUATED(20-40%)는 목표 범위 안입니다.
- 판단 대기 적격은 범위 안 미연결 기준이 없는 두 시험(NCT06928389, NCT07680790)에서만 나올 수 있습니다.
- 숨긴 값의 확보 경로(쌍이 아니라 환자×슬롯 단위, `patients.json`의 `hidden_case`)
  - 환자 답변 가능 203
  - 문서로만 117 (환자가 모르는 조직형·유전자 등)
  - 검사지로만 32 (TSH)
  - 모름·무응답·연락 불가는 실행 때 응답 모델(가정값)이 정합니다.

## 6. 재현 방법

```bash
export PYTHONPATH=$PWD/src PYTHONIOENCODING=utf-8
# 2단계 검수표 (선우 검수 큐는 원격에만 있음)
git show origin/feat/p-common-engine:docs/dev/expert_rules_review_queue_2026_11_07.csv > /tmp/queue.csv
python scripts/nsclc_review_table.py /tmp/queue.csv
python scripts/nsclc_expression_review.py
# 3·4단계 입력 -> 생성(validate + 2회 해시) -> 독립 GT
python scripts/build_nsclc_eval.py all
# runner 연결 확인 (시뮬레이터 코드 변경 없음)
python scripts/check_nsclc_eval_runner.py --policies no_action,full_dcvc --budget 400 --horizon 10
python scripts/check_nsclc_eval_runner.py --policies full_dcvc --budget 6000 --horizon 80
python -m pytest -q tests/test_nsclc_eval_gt.py
```

**동겸 runner 연결**
- `python -m ctrec.sim` CLI는 시험을 `data/parsed`에서 읽으므로 이 평가셋을 바로 쓸 수 없습니다.
- 라이브러리 호출로는 됩니다: `to_sim_cohort(cohort, sd, smap, trials=load_trials(ids, store_dir=data/nsclc/eval/inputs/trial_store, trials_dir=data/nsclc/trials))`.
- `RunResult.final`의 쌍 키(`<별칭>|<시험>`)가 `pair_gt.csv`의 `sim_pair_key`와 같습니다(테스트).
- 실행 사례 (seed 1, 시뮬레이터 `290420d` + #22, #37 미포함)

  | 정책 | 예산·horizon | 최종 상태 | EXPERT_REVIEW ∧ GT 판단 대기 | 확정 부적격 ∧ GT 부적격 | GT가 부적격 아님 + 확정 부적격 | 확정 적격 |
  |---|---|---|---|---|---|---|
  | no_action | 400분·10 | 정보 부족 155 / 검증 대기 85 | 0 | 0 | 0 | 0 |
  | full_dcvc | 400분·10 | 정보 부족 147 / 검증 대기 93 | 0 | 0 | 0 | 0 |
  | full_dcvc | 6000분·80 | 확정 부적격 122 / EXPERT_REVIEW 21 / 정보 부족 94 / 검증 대기 3 | 21 / 23 | 122 / 130 | 0 | 0 |

## 7. 결정 로그

팀 결정 (11-07, 그대로 따름)

| # | 결정 | 이 작업에서 |
|---|---|---|
| 1 | 판단 기준 GT: 나머지로 판정 → 전문가 판단 대기 적격 | `PENDING_EXPERT_ELIGIBLE`, 확정 적격과 분리 |
| 2 | 동의서·절차 기준 excluded, R7 현행 | 9개 excluded. 평가 저장소에는 남겨 엔진은 R7대로 처리 |
| 3 | 축소 범위 | §3 |
| 4 | 미연결 쌍 NOT_EVALUATED | 범위 안 미연결 기준이 있는 경로는 NOT_EVALUATED |
| 5 | `cm.serious_organ_disease`·`cm.immune_onc` 판단 기준 편입 | `spec.EXPERT_SLOTS_EXTRA`. 임상 검수 미완료 |
| 6 | S_summary·S_chart N:1 보류 | NSCLC 카탈로그에서 S_summary를 쓰지 않음 |

이 작업의 보수적 결정

| # | 막힌 지점 | 선택 | 이유 |
|---|---|---|---|
| D1 | 선우 NSCLC 분류표(17·63·58·18·4)를 찾지 못함 | `p4a_unbound.tsv` + 원문 대조로 분류. 선우 판단 기준 검수 큐(`feat/p-common-engine`, NSCLC 32행)는 `sunwoo_queue` 열로 참고 | 저장소·원격 브랜치 어디에도 그 분류표가 없음 |
| D2 | 검사 수치 4개의 기준이 6개 시험에 없음 | 슬롯·단위·참고 범위만 정의(`lab_reference.json`), 연결 0 | 기준값을 지어내지 않음. 기준값은 파싱 미완료 2개 시험에만 있음 |
| D3 | "Age ≥22 in the USA" (지역 조건, 전수 조사 1건) | 국가 슬롯을 추가하지 않고 **조건부 표시**. 22세 미만이면 NOT_EVALUATED. 파싱본·엔진 입력은 그대로, GT에서만 적용(`overrides.json`) | 국가를 생성하면 근거 없는 값이 생김. 조건부 표시는 거짓 부적격·거짓 적격을 모두 만들지 않음 |
| D4 | 슬롯이 원문을 다 표현하지 못하는 범위 안 기준 11개 | 연결하지 않음 → NOT_EVALUATED | 부분 표현으로 PASS를 만들면 거짓 적격 가능 |
| D5 | CSV가 판단 기준으로 분류한 NCT07027514 I13 "Willingness to provide fresh tumor biopsy" | excluded(절차 동의) | 원문은 생검 '의향' - 팀 결정 2. 임상 검수 요청(`expression_review.csv`) |
| D6 | 판단 기준인데 파서 `underspecified=false` | 평가 저장소에서만 true로 표시 | 엔진이 EXPERT_REVIEW로 보내게 (#37 `mark_expert`와 같음). 원본 보존 |
| D7 | 범위 밖 코호트 전용 기준을 빼면 엔진이 OR 경로를 잃음 | 평가 저장소에 남기고 연결하지 않음 | 빼면 NCT07027514 Cohort A가 사라져 거짓 부적격 6쌍(실측) → 남긴 뒤 0쌍 |
| D8 | NSCLC 대응표에 `sex` 없음 | 평가용 사본에만 추가 | 원본 대응표(#20)는 담당 외 데이터라 바꾸지 않음 |
| D9 | NSCLC-NOS·재발 병기 | 슬롯 값에서 뺌 | 비편평 해당 여부·병기 축이 불확실 (`expression_review.csv`) |
| D10 | 결정 5가 현황판 테스트 고정 수치(72+9)를 바꿈 | 현황판 테스트를 고치지 않고 수정 제안 패치(`docs/proposals/decision5_board_test.patch`) | 담당 외 코드. 이 브랜치에서 그 테스트 1건 실패 |
| D11 | 인구학 기준만 통과한 쌍 | NOT_EVALUATED (`no_substantive_pass`) | 이전 지시(인구학만은 확정 안 함). R5와 다름(§4) |

## 8. 주장 가능 / 주장 불가

**주장 가능**
- 실제 NSCLC 프로토콜 6개의 축소 범위 기준 72개를 원문과 1:1로 대조했습니다(일치 60 / 모호 11 / 불일치 1). 데이터 검증 결과이며 임상 검수 전입니다.
- 독립 규칙 GT는 엔진 판정 코드와 분리되어 있고(테스트), 생성기 truth와 연결 기준에서 같은 결론을 냅니다.
- 이 합성 평가셋에서 정책이 "판단 기준 외 확인을 마치고 EXPERT_REVIEW에 도달한 쌍"을 GT와 대조해 셀 수 있습니다. 정책 간 상대 비교만 가능합니다.
- 실행 사례에서 확정 적격 0, GT가 부적격이 아닌데 확정 부적격 0이었습니다. 이 평가셋·이 실행 조건에 한합니다.

**주장 불가**
- 임상 정확도, 임상 성능, 실제 환자 적격성.
- **전체 프로토콜 적격성.** 범위 밖 79개 기준을 평가하지 않았고, ClinicalTrials.gov 기준은 전체 프로토콜의 요약입니다(NCT07680790 E4 "Other protocol-defined criteria apply").
- 확정 적격 관련 지표. 6개 시험 모두 판단 기준이 있어 확정 적격 GT가 0입니다.
- 검사 수치(ANC·빌리루빈·CrCl·Hb) 기준 처리. 이 평가셋에 해당 기준이 없습니다.
- 실제 업무 시간·비용 절감. 비용·응답 확률은 가정값입니다.
- 사전 분포가 실제 NSCLC 집단을 대표한다는 것. 라벨 분포를 위한 설계값입니다.
