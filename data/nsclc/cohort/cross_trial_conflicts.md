# 시험 간 같은 슬롯을 반대 방향으로 쓰는 쌍 (검수 전)

추천 8개 시험에서 같은 정보(슬롯)를 한 시험은 요구하고 다른 시험은 제외하는 경우를 모았습니다. 이런 쌍이 있으면 정보 하나를 확인할 때 여러 시험의 판정이 동시에 반대로 갈립니다. 코호트 실험에서 정보를 공유하는 구조가 여기서 생깁니다. 아래 해석은 모두 **검수 필요**이며, 인용은 CT.gov 원문 그대로입니다(마크다운 이스케이프 역슬래시만 뺌).

시험 약칭: T1 NCT07098338 · T2 NCT07027514 · T3 NCT07416058 (PHOENIX) · T4 NCT06216301 (LUNAR-2) · T5 NCT05255302 · T6 NCT04892953 · T7 NCT06928389 · T8 NCT07680790

## 요약

| 슬롯 | 요구 (포함 조건) | 제외 | 관련 없음 | 검수 포인트 |
|---|---|---|---|---|
| `bm.driver_alteration` EGFR 감수성 변이 | T8, T3 (AGA 목록 중 하나), T1 하위 연구 3 (AGA) | T1 하위 연구 1·2, T2, T4, T5, T6 (선암만), T7 ("actionable" 해석) | — | "Known"(검사 안 했으면 통과)과 "Absence"(검사 필요?)의 차이 |
| `bm.driver_alteration` ALK | T3 (AGA 목록 중 하나), T1 하위 연구 3 | T1 하위 연구 1·2, T2, T4, T5, T6 (선암만), T7 | T8 | T8은 ALK 언급 없음 |
| `bm.driver_alteration` KRAS G12C | T3 (AGA 목록에 포함) | T7 ("Known actionable genomic alterations"에 들어가는지 미정) | T4·T5 제외 목록에 없음 | KRAS G12C를 AGA로 볼지 시험마다 다를 수 있음 |
| `tx.prior_pd1_pdl1` | T7 (PD-1/L1 억제제 치료력), T6 (더발루맙 치료력) | T1, T2 코호트 B, T3, T4 (12개월 이내), T5, T6 (더발루맙 외) | T8 | T6은 더발루맙은 요구, 다른 PD-(L)1은 제외 |
| `tx.prior_systemic_advanced` | T7 (백금 + PD-(L)1), T3 (TKI 후 진행), T1 하위 연구 3 (표적치료 후 진행) | T1 하위 연구 1·2, T4, T5 | T8 (공개 기준에 조항 없음, "newly diagnosed"만 있음) | T6은 3기 근치 치료 후 진행이라 진행성 병기 치료력으로 볼지 검수 |
| `tx.prior_targeted_tki` | T3, T1 하위 연구 3 | T5 (EGFR·ALK 억제제 없음) | 나머지 | 구동 유전자 변이와 함께 움직임 (조합 후보 참고) |
| `tx.prior_platinum` | T7, T6 (화학방사선) | T5 (폐암 화학요법) | 나머지 | T6의 화학방사선 약제는 명시 없음 |
| `cm.hiv` | — (T2는 조절된 HIV 허용 조항이 포함 기준에 있음) | T1 (HIV 감염), T5 ("Known HIV infection") | — | T2·T3은 조절되면 허용, T1·T5는 제외: 같은 환자가 반대로 갈림 |
| `cns.brain_mets` 미치료 무증상 | T4·T5 (무증상이면 허용) | T3 ("Untreated or symptomatic"), T6 (치료 후 영상 안정 요구) | T7 (언급 없음) | 방향이 반대는 아니지만 같은 값의 판정이 갈림 |

## 원문 인용

### EGFR 감수성 변이

| 방향 | 시험 | 원문 |
|---|---|---|
| 요구 | T8 | "Participants must have documented EGFR-TKI-sensitizing mutation (exon 19 deletion or exon 21 L858R substitution)" |
| 요구 (목록 중 하나) | T3 | "harbor confirmed actionable driver mutations for which targeted therapies are available; these mutations are stratified as follows: EGFR (19del, L858R); ALK, ROS1, RET fusions; KRAS G12C; BRAF V600; and HER2 exon 20 insertions" |
| 요구 (AGA) | T1 하위 연구 3 | "Documented positive AGA and had progressed on prior targeted therapy" |
| 없어야 함 | T1 하위 연구 1·2 | "Absence of sensitizing EGFR mutations or ALK rearrangements. No known other Actionable Genomic Alterations(AGAs)" |
| 제외 | T2 | "Have known EGFR or ALK mutations" |
| 제외 | T4 | "EGFR sensitizing mutation and/or ALK translocation, and/or ROS1 and/or RET targetable gene rearrangement, and/or METex14 skipping mutation, and/or NTRK1/2 gene fusion and/or BRAF V600 mutations directed therapy is indicated or planned" |
| 제외 | T5 | "Known EGFR activating tumor mutation (deletion LREA in exon 19, L858R ou L861X mutations in exon 21, G719A/S mutation in exon 18, exon 20 insertion) or HER2 exon 20 insertion" |
| 없어야 함 (선암) | T6 | "For lung adenocarcinoma patients, patients must not harbor any EGFR sensitizing mutations, ALK fusion, ROS1 rearrangements, RET fusions, or MET exon 14 skipping mutations where there are standard of care therapy options available." |
| 제외 (해석 필요) | T7 | "Known actionable genomic alterations." |

검수 포인트:
- T5는 희귀 변이(L861X, G719X)와 exon 20 삽입까지 제외합니다. T8은 exon 19 결손·L858R만 요구합니다. 그래서 L861X 환자는 T5에서 제외되지만 T8 기준도 충족하지 못합니다. 슬롯 값을 `egfr_ex19del_L858R` / `egfr_uncommon` / `egfr_ex20ins`로 나눈 이유입니다.
- T6은 선암에만 적용됩니다(`dx.histology`와 조건부). 편평상피암은 "EGFR and ALK status is not required".

### PD-1/PD-L1 치료력

| 방향 | 시험 | 원문 |
|---|---|---|
| 요구 | T7 | "Previously received systemic platinum-based chemotherapy and PD-1/L1 inhibitors." |
| 요구 (더발루맙) / 제외 (그 외) | T6 | "patients who received standard chemoradiation followed by durvalumab therapy" / "Patients who were treated with anti-PD-(L)1 therapy other than durvalumab" |
| 제외 | T1 | "Prior exposure to immune-mediated therapy" |
| 제외 (코호트 B) | T2 | "Cohort B only: Prior therapy with an immune checkpoint inhibitor." |
| 제외 | T3 | "Prior immunotherapy is not permitted." |
| 제외 (12개월) | T4 | "Had prior treatment with any other anti-PD-1, or PD-L1 or PD-L2 agent or an antibody or a small molecule targeting other immuno-regulatory receptors or mechanisms in the 12 months prior to randomization." |
| 제외 | T5 | "Previous treatment with anti-PD-1, anti-PD-L1, Anti-CTLA4 or any ICI antibody" |

검수 포인트: T4는 12개월 이내만 제외하므로, 13개월 전 PD-(L)1 치료를 받은 환자는 T4는 통과하고 T5는 제외됩니다. 치료 시점 슬롯(경과 개월)이 필요할 수 있습니다.

### 진행성 병기 전신 치료력

| 방향 | 시험 | 원문 |
|---|---|---|
| 요구 | T3 | "Patients must have disease progression following at least one line of TKI therapy" |
| 요구 | T7 | "Previously received systemic platinum-based chemotherapy and PD-1/L1 inhibitors." |
| 제외 | T1 하위 연구 1·2 | "Receipt of prior systemic chemotherapy/chemoradiation/immunotherapy for advanced NSCLC for sub-study 1 and sub-study 2." |
| 없어야 함 | T4 | "Have not received prior systemic treatment for their metastatic NSCLC." |
| 없어야 함 | T5 | "No prior systemic anticancer therapy (including EGFR or ALK inhibitors) given as primary therapy for advanced or metastatic disease." |
| 해석 필요 | T8 | "newly diagnosed locally advanced (Stage IIIB/IIIC), metastatic (Stage IVA/IVB), or recurrent disease" |

### HIV

| 방향 | 시험 | 원문 |
|---|---|---|
| 조절되면 허용 | T2 | "Human immunodeficiency virus (HIV)-infected participants must be on anti-retroviral therapy (ART) and have well-controlled HIV infection/disease" |
| 조절되면 허용 | T3 | "human immunodeficiency virus (HIV) infection, unless well controlled according to protocol-defined criteria" |
| 제외 | T1 | "Active infection, including TB and infections with HIV, HBV (verified by known positive HBsAg result), HCV." |
| 제외 | T5 | "Known HIV infection" |

### 뇌전이 (미치료·무증상)

| 판정 | 시험 | 원문 |
|---|---|---|
| 허용 | T4 | "Subjects with asymptomatic CNS metastases or with previously treated brain metastases may participate" |
| 허용 | T5 | "Asymptomatic brain metastasis, not needing corticosteroids greater than 10 mg prednisone equivalent daily or mannitol infusions, are allowed." |
| 제외 | T3 | "Untreated or symptomatic central nervous system (CNS) metastases." |
| 치료 후 안정 요구 | T6 | "Brain metastases must be treated prior to enrolment and demonstrate radiographic stability" |

## 방향이 반대는 아니지만 범위가 겹치지 않는 슬롯

| 슬롯 | 내용 | 원문 근거 |
|---|---|---|
| `dx.stage` | T6은 3기만, T4·T5는 4기만 받습니다. 3기 환자는 T4·T5에, 4기 환자는 T6에 해당하지 않습니다. 병기 판도 다릅니다(T3 AJCC 9판, T4·T7·T8 8판, T6 7·8판). | T6 "Stage III NSCLC (AJCC 7th and 8th edition)", T4 "stage 4 (according to Version 8 of the American Joint Committee on Cancer [AJCC] criteria)", T5 "Stage IV accordingly to 8th classification TNM, UICC 2015" |
| `bm.pdl1_tps` | T1은 하위 연구로 ≥50%와 1–49%를 나눕니다. T3은 ≥50%(중앙 검사, 2년 이내 검체)입니다. T5는 값 조건 없이 결과만 요구합니다. | T1 "PD-L1 TC ≥ 1% (TC≥ 50% for sub-study 1, 1-49% for sub-study 2)" |
| `age` | 상한이 있는 시험(T3·T7 ≤75, T5 <75)과 없는 시험이 섞여 있습니다. T4는 미국에서만 하한이 22세입니다. | T5 "Age≥ 18 years, <75 years" |
