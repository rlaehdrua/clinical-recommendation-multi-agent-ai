# 라벨러 안내 (초안 v1, 승인 전)

> TEMPLATE / DRAFT. 이 문서는 임상 승인(CL-1) 전 초안입니다. 승인된 주석 규약 `gt/protocol/annotation_protocol_v1.md`가 생기면 그 문서가 우선합니다.
> 이 안내에는 엔진(TRIALOG)의 판정, 인용 평가 결과, 플래그, 화면, 동의어 사전 내용이 없습니다. 라벨 작업 중에도 그런 자료를 찾아보거나 요청하지 마세요.

## 1. 하는 일

여러분은 사례를 **혼자서, 다른 라벨러와 상의하지 않고** 판단합니다. 받는 파일은 두 가지뿐입니다.

- 라벨 시트 (`*_label_sheet*.csv` 또는 `.json`): 사례 원문과 빈 라벨 칸
- 승인된 주석 규약

받지 않는 것: 프로그램의 판정 결과, 다른 라벨러의 시트, 과거 평가 세트. 이런 것이 시트에 보이면 작업을 멈추고 GT 책임자에게 알려 주세요.

## 2. 공통 칸

| 칸 | 쓰는 법 |
|---|---|
| `labeler_id` | 받은 라벨러 ID (예: L-A). 다른 사람 ID를 쓰지 않습니다 |
| `labeled_at` | 라벨한 시각, ISO 8601 (예: `2026-11-20T14:05:00+09:00`) |
| `confidence` | 1 = 확신 없음, 2 = 보통, 3 = 확실. 3 미만이면 판정자가 다시 봅니다 |
| `evidence_start`, `evidence_end` | 근거가 된 원문 부분의 문자 위치 (0부터, 끝은 포함하지 않음). 모르면 비워 둡니다 |
| `needs_expert`, `needs_expert_reason` | 상급 임상의 판단이 필요하다고 보면 `true`와 사유 |
| `note` | 자유 메모 |

## 3. 문장 라벨 (G-U)

한 행 = 원문 하나와, 그 원문에서 쓰려는 인용(`quote`) 또는 값(`field`, `value`, `unit`).

판단 순서 (값): 이 값은 **이 환자 본인의**, **기준 기간 안의**, **실제로 측정된**, **유효한**, **해당 검체의** 값인가?

| 칸 | 값 | 뜻 |
|---|---|---|
| `decision` | `accept` / `hold` | `accept`: 이 인용·값만으로 기준을 확정해도 됨. `hold`: 확정하면 안 됨 |
| `category` | 규약의 범주 이름 | 왜 그렇게 판단했는지의 범주 |
| `hearsay` | true/false | 남에게 들은 값, 기억에 의존한 값, 불확실하다고 적힌 값 |
| `approx` | true/false | 범위·추정 표현 ("약", "~", "10-12") |
| `conflict` | true/false | 같은 개념에 '있음'과 '없음' 진술이 함께 있음. `true`이면 `decision`은 `hold` |
| `threshold_crossing` | true/false/빈칸 | 범위·추정 값의 범위가 기준 문턱을 가로지름. 전해 들은 값이 문턱을 가로지르면 `hold` |
| `concept_cluster` | 규약의 묶음 이름 | 상충일 때 해당 질환 묶음 (임상의가 정한 목록) |
| `source_priority_note` | 자유 | 진술 출처의 우선순위 (예: 진료 기록 > 환자 진술). **라벨에는 반영하지 않고** 여기에만 적습니다 |

## 4. 기준 라벨 (G-C)

한 행 = 시험의 기준 하나.

| 칸 | 쓰는 법 |
|---|---|
| `interpretation` | 기준이 무엇을 요구하는지 한두 문장 |
| `scoped` | 국가·지역 범위가 있는 기준인가 |
| `scope_unit` | 범위가 기관 국가인지(`institution_country`), 환자 거주지인지(`patient_residence`), 알 수 없는지(`unclear`) |
| `countries` | ISO 국가 코드, 세미콜론 구분 (예: `US`) |
| `applies_when` | 그 국가 **안**에서 적용(`inside`)인지 **밖**에서 적용(`outside`)인지 |
| `applicability_condition` | 그 밖의 적용 조건 |
| `needs_clinical_judgement` | 기록만으로 정할 수 없고 임상 판단이 필요한가 |
| `structured_*` | 구조화할 수 있으면 필드·연산자·값·단위. 아니면 비워 둡니다 |

## 5. 쌍 라벨 (G-P)

환자 기록 하나와 시험 하나. 먼저 **기준마다** `MET` / `NOT_MET` / `UNKNOWN_FROM_RECORD` / `NEEDS_CLINICIAN`을 정하고 근거 위치를 남깁니다. 쌍 라벨(`ELIGIBLE` / `INELIGIBLE` / `NOT_DETERMINABLE` / `NEEDS_CLINICIAN`)은 승인된 집계 규칙(`aggregation_rule_version`)으로 계산합니다. 직관으로 쌍 라벨을 먼저 정하지 마세요.

## 6. 끝나면

시트를 GT 책임자에게만 보냅니다. 다른 라벨러와 결과를 비교하지 않습니다. 불일치는 판정자가 `adjudication_log.csv`에서 결정합니다.
