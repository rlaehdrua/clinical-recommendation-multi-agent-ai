# NSCLC 추천 8개 시험의 검사 수치 (패널별, 검수 전)

기준값이 숫자로 적힌 시험은 NCT05255302와 NCT04892953 두 개뿐입니다. 나머지는 `Adequate … function` 형태로 기준값 없이 적혀 있습니다. 모든 해석은 **검수 필요**입니다. 인용은 CT.gov 원문 그대로이며, 마크다운 이스케이프 역슬래시(`\>`, `\^`)만 뺐습니다. 행별 자료는 `slot_candidates.csv`의 `lab.*` 행에 있습니다.

## 검사 유효 기간 (`lab_age_days`)

8개 시험 모두 혈액검사 결과의 유효 기간을 공개 기준에 적지 않았습니다. 그래서 `lab_age_days`는 이 시험들에서 판정에 쓰이지 않습니다(검수 필요: 프로토콜 본문에는 있을 수 있음). 기간이 있는 검사 관련 항목은 아래 두 개뿐입니다.

| 시험 | 항목 | 원문 |
|---|---|---|
| NCT04892953 | 영상 (CT·PET-CT·뇌 MRI) | "magnetic resonance imaging (MRI) brain must be performed within 28 days prior to study entry" |
| NCT07416058 | PD-L1 검체 | "on fresh or archival tumor tissue (collected within 2 years)" |

## 기준값 없이 적힌 시험

| 시험 | 원문 | 판정 유형 후보 |
|---|---|---|
| NCT07098338 | "Adequate bone marrow and organ function" | fact_llm (프로토콜 정의 필요) |
| NCT06216301 | "Adequate hematologic and end-organ function" (응고만 기준값 있음) | fact_llm |
| NCT06928389 | "Adequate organ function." | fact_llm |
| NCT07027514 | 혈액검사 기준값 없음 (TSH·ECG만) | — |
| NCT07416058 | 혈액검사 기준값 없음 | — |
| NCT07680790 | 혈액검사 기준값 없음 ("Other protocol-defined inclusion/exclusion criteria apply.") | — |

## CBC

| 항목 | 슬롯 (단위) | NCT05255302 | NCT04892953 |
|---|---|---|---|
| 호중구 | `lab.anc` (10^9/L) | "neutrophils≥ 1500/mm3" | — |
| 혈소판 | `lab.platelets` (10^9/L) | "platelets ≥100 000/mm3" | "Platelet count >= 75 × 10^9/L" |
| 혈색소 | `lab.hemoglobin` (g/dL) | "Hemoglobin≥ 9g/dL" | "Hemoglobin >= 9.0 g/dL" |

- 단위 환산(검수 필요): 1500/mm3 = 1.5 × 10^9/L, 100 000/mm3 = 100 × 10^9/L.

## 간기능

| 항목 | 슬롯 (단위) | NCT05255302 | NCT04892953 |
|---|---|---|---|
| AST·ALT | `lab.ast_alt_uln` (×ULN) | "AST and ALT< 3x ULN" / 간 전이·Gilbert: "must have AST and ALT ≤ 5 x ULN" | "=< 2.5 x institutional upper limit of normal unless liver metastases are present, in which case it must be =< 5 x ULN" |
| 총 빌리루빈 | `lab.bilirubin_uln` (×ULN) | "total bilirubin < 2xULN" | "Serum bilirubin =< 1.5 x institutional upper limit of normal (ULN)." (Gilbert 증후군 예외) |

- 기준값이 간 전이(`dx.liver_mets`)와 Gilbert 증후군 여부에 따라 달라집니다. 생성기에서 조건부 기준이 필요합니다(검수 필요).

## 신기능

| 항목 | 슬롯 (단위) | NCT05255302 | NCT04892953 |
|---|---|---|---|
| 크레아티닌 청소율 | `lab.creatinine_clearance` (mL/min) | "Creatinine Clearance ≥ 45 mL/min (Cockcroft or MDRD or CKD-epi)" | "Measured creatinine clearance (CL) > 15 mL/min or calculated creatinine CL > 15 mL/min by the Cockcroft-Gault formula" |

- 계산식이 다릅니다(Cockcroft-Gault / MDRD / CKD-EPI). 같은 환자도 식에 따라 값이 다를 수 있습니다(검수 필요).

## 응고

| 항목 | 슬롯 (단위) | NCT06216301 |
|---|---|---|
| INR·aPTT | `lab.inr_aptt_uln` (×ULN) | "For subjects not receiving therapeutic anticoagulation: INR or aPTT ≤ 1.5 x ULN" / 항응고제 사용자: "as long as INR or aPTT is within therapeutic range of intended use of anticoagulants" |

- 항응고제 사용 여부(`med.anticoagulant`)에 따라 기준이 바뀝니다.

## 기타

| 항목 | 슬롯 | 시험 | 원문 |
|---|---|---|---|
| TSH | `lab.tsh_normal` | NCT07027514 | "Thyroid-stimulating hormone (TSH) within normal limits" |
| QTcF | `lab.qtcf` (ms) | NCT04892953 | "Mean QT interval corrected for heart rate using Fridericia's formula (QTcF) >= 470 ms" (제외) |
| QT | `lab.qtcf` | NCT07027514 | "Have baseline electrocardiogram (ECG) without evidence of acute ischemia or prolonged QT interval" (기준값 없음) |
| B·C형 간염 | `lab.hbv_hcv_status` | NCT04892953 | "hepatitis B (known positive hepatitis B virus [HBV] surface antigen (HBsAg) result)" 제외, anti-HBc 양성·HBsAg 음성 허용, HCV 항체 양성은 PCR 음성일 때만 허용 |
| B·C형 간염 | `lab.hbv_hcv_status` | NCT07098338, NCT07027514, NCT07416058, NCT05255302 | 활동성·급성 간염 제외 (기준값 없음, `slot_candidates.csv` 참고) |
| LVEF | (슬롯 후보 아님) | NCT05255302 | "Patients with a significant cardiac history, even if controlled, should have a LVEF > 50%." |
| 임신 검사 | (`repro.*`) | NCT05255302 | "Women must have a negative serum or urine pregnancy test" |
