# P0-1·P0-2 독립 평가 사례 (정답 쪽)

작성: 서연 (데이터) · 2026-11-08 · 측정 기준 `integration/dcvc` `4f81460`

> 데이터 작성 / 임상 검수 미완료. 합성 문장이며 실제 기록의 빈도·분포를 대표하지 않습니다. 결과는 정확도가 아닙니다.

## 1. 작성 원칙과 독립성

- 기대값은 원문 문장의 처리 원칙으로만 정했습니다. 엔진이나 모델의 출력을 보고 정하지 않았습니다.
- 판정 구현 코드(`ctrec.value_grounding`, `ctrec.evidence_directness` 등)는 열어 보지 않았습니다.
- 다만 평가 스크립트의 입력 형식(`scripts/eval_m_holdout.py`)과 held-out 6 사례 문장은 읽었으므로, held-out 6과 완전히 독립은 아닙니다.
- 기대 처리 값
  - `accept`: 그 값을 사실로 채택
  - `hold`: 채택하지 않고 보류
  - `conflict`: 상충으로 탐지. 보류도 안전한 처리로 셉니다(`ok`). 탐지 여부는 따로 봅니다.

| 원칙 | 기대 |
|---|---|
| 같은 항목·같은 시점에 다른 값 | hold (어느 값도 채택하지 않음) |
| 시점이 다른 값 중 이후 값으로 대체된 과거 값 | hold |
| 시점이 다른 값 중 가장 최근 날짜 값 | accept |
| 부정 뒤 번복 | conflict |
| 의사 말을 기억으로 전함 / 가족 전언 / 불확실 표현 | hold |
| 확실한 본인 값(자기 보고 허용 항목) / 날짜 있는 문서 값 | accept (과잉 보류 확인용 정상 대조) |

## 2. 파일

| 파일 | 내용 |
|---|---|
| `value_cases.json` | 값 채택 사례 38건. 형식: field·value·unit·text (held-out 6 labs와 같음) |
| `narrative_conflict_cases.json` | 서술 상충 사례 10건. 형식: criterion·quote·text (held-out 6 conflict와 같음) |
| `results_4f81460.json` | 현재 코드 처리 기록 (새 사례 + held-out 6 실패 집합 25건) |
| `../../../scripts/p0_fact_cases_build.py` | 사례 작성 스크립트 (기대값과 근거가 코드에 고정) |
| `../../../scripts/eval_p0_fact_cases.py` | 실행: `PYTHONPATH=src python scripts/eval_p0_fact_cases.py --holdout --out data/eval/p0_fact_cases/results_4f81460.json` |

| 범주 | 건수 | 기대 |
|---|---|---|
| 상충: 같은 항목 다른 값 | 5 | hold |
| 상충: 시점이 다른 값 (과거 값) | 5 | hold |
| 상충: 시점이 다른 값 (최근 값, 정상 대조) | 2 | accept |
| 상충: 부정 후 번복 | 5 | conflict |
| 전해 들은 값: 의사가 ~라고 했던 것 같다 | 5 | hold |
| 전해 들은 값: 가족이 전한 값 | 5 | hold |
| 전해 들은 값: 불확실 표현 | 5 | hold |
| 정상 대조: 확실한 본인 값 | 5 | accept |
| 정상 대조: 문서 근거 값 | 6 | accept |
| 정상 대조: 부정이 다른 근거와 일치 | 5 | accept |

## 3. 기존 실패 집합 기대값 검토 (held-out 6, `docs/dev/m_holdout_6_*.json`)

상충 15건 (`m_holdout_6_cases.json` conflict, expected=hold)

| # | 사례 요약 | 검토 | 근거 |
|---|---|---|---|
| 1 | "No history of ILD" + 스테로이드 치료 폐렴 2024 | 타당 | 기준이 "ILD 또는 스테로이드가 필요한 폐렴" - 외부 기록이 기준 충족 |
| 2 | "denies seizures" + 뇌전증 약 복용, 2025 발작 | 타당 | 같은 질환 부정·긍정 |
| 3 | "No history of hepatitis" + 만성 B형 간염 entecavir | 타당 | 상충은 분명. 'active' 해당 여부는 임상 판단이지만 보류가 맞음 |
| 4 | "결핵 없음" + 2023 폐결핵 치료 완료 | 타당 | 기준이 '과거력' |
| 5 | "Denies autoimmune" + RA로 methotrexate | 타당 | 전신 치료 중 자가면역질환 |
| 6 | "뇌전이 없음" + MRI 8mm 병변 '전이 의심' | 타당 (메모) | 상충보다 '의심' 불확실 - 탐지가 아니라 보류로 세야 함 |
| 7 | "No prior malignancy" + 2018 대장암 수술 | 타당 | 다른 원발암 과거력 |
| 8 | "심부전 없음(환자 진술)" + 문제목록 CHF EF 30% | 타당 | 진술과 기록 상충 |
| 9 | "HIV negative per patient" + HIV RNA 검출, 치료 중 | 타당 | 진술과 검사 상충 |
| 10 | "간질성 폐질환 부인" + CT UIP, IPF 추정 | 타당 (메모) | '추정' 영상 소견 - 보류가 맞고, 상충 탐지로만 세면 엄격함 |
| 11 | "No organ transplant" + 2016 신장이식 면역억제제 | 타당 | 사망자 공여 = 동종 이식 |
| 12 | "Denies MI or stroke" + 2022 뇌경색 | 타당 | 같은 질환 상충 |
| 13 | "자가면역질환 없음" + 건선 adalimumab | 타당 | 건선은 면역 매개 질환, 전신 치료 중 |
| 14 | "negative for bleeding disorders" + 혈우병 A | 타당 | 템플릿 ROS와 병력 상충 |
| 15 | "방사선 폐렴 없음" + 2025 방사선 폐렴 스테로이드 치료 | 타당 | 같은 질환 상충 |

전해 들은 값 10건 (`m_holdout_6_labs.json` negative, hearsay_or_uncertain, expected=보류)

| # | 사례 | 검토 | 근거 |
|---|---|---|---|
| 1 | "Pt thinks her hemoglobin was 'around 10' ..." | 타당 | 기억 + 'around' + 시점 모름 |
| 2 | "maybe his creatinine was 1.3 or so per his nephrologist" | 타당 | 전언 + 불확실 |
| 3 | "환자 말로는 ... 혈소판 120이라고 들었다고 함, 결과지 없음" | 타당 | 전언, 결과지 없음 |
| 4 | "Someone at the pharmacy told him his sugar was 180" | 타당 | 제3자 전언 |
| 5 | "보호자 진술: ALT '80 정도', 확인 불가" | 타당 | 가족 전언 + '정도' |
| 6 | "Self-reported age 67 or 68; no ID, DOB unknown" | **모호** | 나이는 자기 보고 허용 항목. 67·68이 기준 문턱을 가로지를 때만 보류가 맞고, 그 밖에는 판정 가능 |
| 7 | "'my bilirubin is always about 2', no records" | 타당 | 'about' + 기록 없음 |
| 8 | "본인 기억으로 INR 1.2쯤" | 타당 | 기억 + '쯤' |
| 9 | "Wife believes his albumin was maybe 3.1 ..." | 타당 | 가족 전언 + 불확실 |
| 10 | "환자 스스로 '수행도 1 정도', 의료진 평가 없음" | 타당 | ECOG는 의료진 평가 항목 |

합계: 타당 24 (메모 2) / 모호 1 / 기대값 오류 0

## 4. 현재 결과 (4f81460, 엔진 코드 변경 없음)

| 집합 | 안전 처리 (`ok`) |
|---|---|
| held-out 6 상충 15건 | 9/15: 탐지 7, 그 밖 보류 2, **수용 6** (#3, #7, #10, #11, #13, #14) |
| held-out 6 전해 들은 값 10건 | 7/10: **오채택 3** (#1, #3, #6) |
| 새 사례: 같은 항목 다른 값 | **0/5** (모두 채택) |
| 새 사례: 과거 값 | 3/5 (V06 "Hb 8.7 on 08-02 / 10.9 on 11-05"에서 8.7 채택, V09 한국어 CrCl 38 채택) |
| 새 사례: 최근 값 (정상) | 2/2 |
| 새 사례: 부정 후 번복 | 4/5 (탐지 3, 보류 1, **N03 수용**: "No prior cancer" + 갑상선암 추가 기록) |
| 새 사례: 의사 전언 | 3/5 (V15 "I believe", V17 "recalls the doctor saying ... 'around 2'" 채택) |
| 새 사례: 가족 전언 | 5/5 |
| 새 사례: 불확실 표현 | 4/5 (V26 "something like 1.5, not sure" 채택) |
| 새 사례: 정상 대조 (본인·문서·일치 부정) | 14/16: **과잉 보류 2** (V29 "Patient states: I'm 58.", V35 "크레아티닌 0.9 mg/dL" - 모두 unlocated) |

- 기존 보고(`docs/dev/q_residual_p0_risks_2026_11_07.md`)의 상충 8/15 미탐지, 전해 들은 값 3/10과 같습니다.
- 새로 드러난 유형
  - **같은 항목에 서로 다른 값이 함께 있을 때 한 값을 채택**합니다(5/5).
  - 날짜가 붙은 과거 값을 채택합니다.
  - 'I believe'·'recalls'·'something like ... not sure' 같은 영어 전언·불확실 표현을 채택합니다.
- 정상 대조의 과잉 보류는 2/16입니다. 영어 축약 "I'm 58"과 한국어 항목명 "크레아티닌"의 원문 위치를 찾지 못한(unlocated) 경우입니다.
