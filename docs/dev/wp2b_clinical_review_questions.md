# NSCLC 코호트 임상 검수 질문 (WP2b)

> 2026-11-03 · 작성: 서연 · 브랜치 `feat/wp2a-nsclc-trials`
> 대상: 임상 검수자 (호흡기내과·종양내과). 검수 전 모든 해석은 **"검수 필요"** 상태이며, 생성기와 판정에 기본값으로 쓰지 않습니다.
> 출처: `docs/dev/wp2b_slot_decisions.md`(D1–D6), `data/nsclc/cohort/cross_trial_conflicts.md`, `slot_combination_candidates.md`(#1–#22), `lab_panels.md`,
> WP3 설계 `docs/dev/wp3_verdict_transition_design.md` §3.3 (브랜치 `docs/wp3-verdict-transition`).
> 인용은 CT.gov 원문 그대로입니다(마크다운 이스케이프 역슬래시만 뺌).

시험 약칭: T1 NCT07098338 · T2 NCT07027514 · T3 NCT07416058 · T4 NCT06216301 · T5 NCT05255302 · T6 NCT04892953 · T7 NCT06928389 · T8 NCT07680790

## 답하는 방법

- 질문마다 선택지 중 하나를 고르고, 필요하면 한 줄 의견을 적어 주세요. 선택지에 없으면 "기타"에 적습니다.
- **"판단 불가(프로토콜 본문 필요)"는 언제나 고를 수 있습니다.** 공개 기준만으로 정할 수 없다는 답도 유효한 결과입니다.
- 비율·확률은 묻지 않습니다(그건 연구팀이 정함). 묻는 것은 **정의와 방향**입니다.
- 우선순위: **A**(판정이 직접 바뀜) → **B**(조건부·파생 구조) → **C**(생성기 조합 제약) → **D**(정의 해석).

## 요약

| 구분 | 질문 수 | 영향 |
|---|---|---|
| A. 구동 유전자 판정 | 6 | T1–T8 전체 EGFR·ALK 방향 |
| B. 조건부·파생 기준 | 5 | 간기능·신기능·응고 (T4·T5·T6) |
| C. 생성기 조합 제약 | 7 | 불가능 조합 금지 목록 |
| D. 기준 정의 해석 | 8 | 치료력·뇌전이·HIV·병기 등 |
| 합계 | 26 | |

---

## A. 구동 유전자 판정 (우선)

슬롯 값은 `positive` / `negative` / `not_tested` 3개입니다. "모름"은 환자의 응답 상태이며 슬롯 값이 아닙니다(D1).

**A1. Known 제외 기준에서 "검사 안 함"(`not_tested`)**
- 원문: T2 "Have known EGFR or ALK mutations" · T5 "Known EGFR activating tumor mutation …" · "Known ALK, ROS1, Ret, NTRK, NRG1 gene rearrangement"
- 질문: EGFR·ALK를 검사하지 않은 환자는 이 제외 기준에 걸리지 않는다(통과)고 봐도 됩니까?
- 선택지: ① 통과 (알려진 변이 없음) ② 통과가 아님, 등록 전 검사가 사실상 필요 ③ 시험마다 다름 (어느 시험인지 적기) ④ 판단 불가

**A2. Absence 포함 기준에서 "검사 안 함"**
- 원문: T1 하위 연구 1·2 "Absence of sensitizing EGFR mutations or ALK rearrangements. No known other Actionable Genomic Alterations(AGAs)" · T6 (선암) "patients must not harbor any EGFR sensitizing mutations, ALK fusion, ROS1 rearrangements, RET fusions, or MET exon 14 skipping mutations where there are standard of care therapy options available."
- 질문: 검사하지 않은 환자는 이 포함 기준을 충족합니까?
- 선택지: ① 불충족 (음성 결과가 있어야 함) ② 충족 (알려진 변이 없음으로 충분) ③ 정보 부족: 검사 후 판정 ④ 판단 불가
- 참고: T1 문장 후반("No known other AGAs")은 Known 표현입니다. 앞뒤 절의 근거 수준이 다를 수 있는지도 의견 부탁드립니다.

**A3. 유전자별 3값 구조**
- 질문: 검사 상태를 유전자별로 따로 두어야 합니까? (예: EGFR은 음성, ALK는 미검사)
- 선택지: ① 유전자별 (EGFR·ALK·ROS1·… 각각 3값) ② EGFR·ALK만 따로, 나머지는 "기타 AGA" 하나로 묶음 ③ 구동 유전자 전체를 하나로 ④ 기타
- 참고: 지금 생성기 후보는 "변이 값 하나, 동시 양성 없음"(조합 후보 #4)입니다. 동시 양성을 허용해야 하는지도 같이 답해 주세요.

**A4. "Actionable genomic alterations"(AGA)의 범위**
- 원문: T7 "Known actionable genomic alterations." (범위 정의 없음) · T3 목록 "EGFR (19del, L858R); ALK, ROS1, RET fusions; KRAS G12C; BRAF V600; and HER2 exon 20 insertions"
- 질문: T7의 AGA에 포함되는 것을 모두 고르세요: EGFR 감수성 · EGFR 희귀 변이(L861X, G719X) · EGFR ex20ins · ALK · ROS1 · RET · KRAS G12C · BRAF V600 · HER2 ex20ins · MET ex14 · NTRK · NRG1
- 추가: T3 목록을 T7에 그대로 적용해도 됩니까? (① 예 ② 아니요 ③ 판단 불가)

**A5. T4의 "directed therapy is indicated or planned"**
- 원문: T4 "EGFR sensitizing mutation and/or ALK translocation, … and/or BRAF V600 mutations directed therapy is indicated or planned"
- 질문: 변이가 양성이면 이 조건도 충족된 것으로 봐도 됩니까, 아니면 치료 계획을 따로 확인해야 합니까?
- 선택지: ① 양성이면 제외 (표적치료 적응이 있다고 봄) ② 치료 계획 확인 필요 (임상의 판단) ③ 판단 불가

**A6. 편평상피암에서의 검사 상태**
- 원문: T6 "For patients with histologies other than adenocarcinoma, EGFR and ALK status is not required."
- 질문: 생성기에서 편평상피암 환자는 `not_tested` 비율이 높다고 두어도 됩니까? (조합 후보 #8)
- 선택지: ① 예 ② 아니요 (최근 진료에서는 편평상피암도 대부분 검사함) ③ 판단 불가

## B. 조건부·파생 기준

**B1. 크레아티닌 청소율 계산식 (D2)**
- 원문: T5 "Creatinine Clearance ≥ 45 mL/min (Cockcroft or MDRD or CKD-epi)" · T6 "Measured creatinine clearance (CL) > 15 mL/min or calculated creatinine CL > 15 mL/min by the Cockcroft-Gault formula"
- 질문 1: 생성기의 기본 계산식으로 Cockcroft-Gault 하나를 써도 됩니까? (① 예 ② CKD-EPI ③ 시험별 식 ④ 판단 불가)
- 질문 2: T5에서 MDRD·CKD-EPI(체표면적 보정 eGFR, mL/min/1.73m²)를 Cockcroft-Gault(mL/min)와 같은 단위로 비교해도 됩니까? (① 예 ② 아니요, 체표면적 보정을 풀어야 함 ③ 판단 불가)
- 질문 3: 식마다 판정이 갈리는 경계 환자(예: 한 식으로는 45 이상, 다른 식으로는 미만)는 어떻게 판정합니까? (① 하나라도 충족하면 통과 ② 임상의 판단 ③ 판단 불가)

**B2. 간 전이에 따른 AST·ALT 상한 (D3)**
- 원문: T5 "patients with hepatic metastases or Gilbert's syndrome must have AST and ALT ≤ 5 x ULN and a baseline total bilirubin ≤ 2xULN" · T6 "unless liver metastases are present, in which case it must be =< 5 x ULN"
- 질문: 간 전이 여부는 영상 소견만으로 충분합니까, 조직 확인이 필요합니까? (① 영상으로 충분 ② 조직 확인 필요 ③ 판단 불가)

**B3. Gilbert 증후군 확인 (D3)**
- 원문: T6 "This will not apply to patients with confirmed Gilbert's syndrome (persistent or recurrent hyperbilirubinemia that is predominantly unconjugated in the absence of hemolysis …)"
- 질문: 환자의 자기 보고("Gilbert 진단을 받았다")만으로 "confirmed"를 인정할 수 있습니까?
- 선택지: ① 자기 보고로 충분 ② 진료 기록(간접 빌리루빈 수치 등) 필요 ③ 임상의 판단 ④ 판단 불가

**B4. 치료 용량 항응고 (D3)**
- 원문: T4 "For subjects not receiving therapeutic anticoagulation: INR or aPTT ≤ 1.5 x ULN (unless participant is receiving anticoagulant therapy as long as INR or aPTT is within therapeutic range of intended use of anticoagulants)" · T2 "Participants on coumadin (warfarin)"
- 질문 1: 예방 용량 저분자량 헤파린은 "therapeutic anticoagulation"에 들어갑니까? (① 아니요 ② 예 ③ 판단 불가)
- 질문 2: DOAC 복용자는 INR·aPTT가 치료 범위 지표가 아닌데, T4에서 어떻게 판정합니까? (① 항응고 중이면 INR 기준 면제 ② 임상의 판단 ③ 판단 불가)

**B5. "Adequate … function"만 있는 시험 (D4)**
- 원문: T1 "Adequate bone marrow and organ function" · T4 "Adequate hematologic and end-organ function" · T7 "Adequate organ function."
- 질문: 공개 기준에 숫자가 없을 때, 다른 시험의 기준값을 빌리지 않고 "판정 불가"로 두는 원칙에 동의합니까?
- 선택지: ① 동의 ② 통상 기준(예: 표준 프로토콜 값)을 써도 됨 — 어떤 값인지 적기 ③ 판단 불가
- 참고: 연구팀 기본안은 ①입니다. ②를 고르면 그 값의 출처를 적어 주세요.

## C. 생성기 조합 제약 (`slot_combination_candidates.md`)

각 항목에 ① 금지 조합으로 둠 ② 드물게 허용 ③ 제약 없음 ④ 판단 불가 중 하나로 답해 주세요.

| 질문 | 조합 후보 | 내용 |
|---|---|---|
| C1 | #1 | 구동 유전자 변이 `negative`인데 표적치료(TKI) 치료력이 있음 |
| C2 | #5 | 진행성 병기 전신 치료력이 없는데 PD-(L)1·TKI 치료력이 있음 (3기 근치 후 더발루맙 공고 요법은 예외로 둠) |
| C3 | #6 | 더발루맙 공고 요법인데 3기 병력·백금 화학방사선 치료력이 없음 |
| C4 | #10 | 남성인데 임신·수유 = 예 (정의상 금지 예정, 확인만) |
| C5 | #11 | 하루 10 mg 넘는 스테로이드를 쓰는데 사용 사유(증상 있는 뇌전이, 자가면역질환, 중증 면역 관련 이상반응)가 없음 |
| C6 | #18 | PD-(L)1 치료력이 없는데 면역 관련 이상반응(irAE) 이력이 있음 |
| C7 | #22 | T2는 "조절된 HIV"를 허용하지만 "acquired immunodeficient states"는 제외함. 조절된 HIV 환자를 후천 면역결핍으로 표시해야 합니까? (① 예 ② 아니요 ③ 판단 불가) |

## D. 기준 정의 해석

**D1. T8 "newly diagnosed"와 치료력 (조합 후보 #7)**
- 원문: T8 "newly diagnosed locally advanced (Stage IIIB/IIIC), metastatic (Stage IVA/IVB), or recurrent disease"
- 질문: "newly diagnosed"는 진행성 병기 전신 치료력이 없다는 뜻으로 봐도 됩니까? 재발(recurrent) 환자는 어떻게 봅니까? (① 치료력 없음 ② 치료력과 무관 ③ 판단 불가)

**D2. T6의 3기 근치 치료 후 진행**
- 원문: T6 "patients who received standard chemoradiation followed by durvalumab therapy with either progressive disease or persistent disease"
- 질문: 이 환자의 화학방사선·더발루맙을 "진행성 병기 전신 치료력"으로 셉니까? (T4·T5의 "치료력 없어야 함" 판정이 갈림) (① 셈 ② 세지 않음 ③ 판단 불가)

**D3. PD-(L)1 치료 시점 (T4 12개월)**
- 원문: T4 "… in the 12 months prior to randomization."
- 질문: 치료 종료 시점 기준입니까, 마지막 투여일 기준입니까? (① 마지막 투여일 ② 치료 시작일 ③ 판단 불가)

**D4. 뇌전이 "unstable"**
- 원문: T1 "Unstable brain metastases" · T6 "Brain metastases must be treated prior to enrolment and demonstrate radiographic stability"
- 질문: "unstable"을 판정하려면 최소 무엇이 필요합니까? (① 직전 영상 대비 크기 변화 ② 증상·스테로이드 필요 여부 ③ 둘 다 ④ 임상의 판단)
- 추가: 미치료·무증상 뇌전이는 T4·T5는 허용하고 T3·T6은 제외합니다. 이 구분(미치료/치료 후 안정/증상)으로 충분합니까?

**D5. HIV 조절 기준**
- 원문: T2 "must be on anti-retroviral therapy (ART) and have well-controlled HIV infection/disease" · T3 "unless well controlled according to protocol-defined criteria" · T1 "Active infection, including … HIV" · T5 "Known HIV infection"
- 질문 1: T1의 "Active infection … HIV"는 조절된 HIV도 제외합니까? (① 제외 ② 조절되면 허용 ③ 판단 불가)
- 질문 2: "well-controlled"를 자기 보고로 판정할 수 있습니까? (① 예 ② 진료 기록 필요 ③ 판단 불가)

**D6. EGFR 희귀 변이 분리**
- 원문: T5 "… L858R ou L861X mutations in exon 21, G719A/S mutation in exon 18, exon 20 insertion" · T8 "exon 19 deletion or exon 21 L858R substitution"
- 질문: 생성기 값을 `egfr_ex19del_L858R` / `egfr_uncommon` / `egfr_ex20ins` 셋으로 나누는 것이 충분합니까? (① 충분 ② 더 나눠야 함 — 어떻게 ③ 판단 불가)

**D7. PD-L1 TC와 TPS**
- 원문: T1 "PD-L1 TC ≥ 1% (TC≥ 50% for sub-study 1, 1-49% for sub-study 2)" · T3 "PD-L1 tumor proportion score (TPS) ≥ 50%, as confirmed by central laboratory testing using the 22C3 or SP263 clone …"
- 질문: TC와 TPS를 같은 값으로 봐도 됩니까? (① 같음 ② 다름 — 변환 불가 ③ 판단 불가)

**D8. 병기 판 차이**
- 원문: T3 "AJCC 9th Edition, Stage IIIB-IVB" · T4 "stage 4 (according to Version 8 of the American Joint Committee on Cancer [AJCC] criteria)" · T6 "Stage III NSCLC (AJCC 7th and 8th edition)"
- 질문: 생성기에서 병기를 8판 하나로 생성하고, 9판 기준(T3)은 8판 값으로 판정해도 됩니까? (① 예 ② 아니요, 9판 슬롯 별도 ③ 판단 불가)

---

## 답변 반영 규칙 (연구팀용)

- 답이 오면 `slot_candidates.csv`의 해당 행 `review_status`를 "검수 완료"로 바꾸고, `note`에 질문 ID와 답을 적습니다(예: `검수 A1: ①`).
- "판단 불가"는 그 기준을 정답 라벨에서 "판정 불가"로 둡니다(D4와 같은 처리).
- A1·A2 답은 WP3 설계 §3.3 표(Known/Absence 근거 수준)에 반영합니다.
- 검수자가 비율·분포를 제안하면 근거 후보로만 기록합니다. 값은 연구팀이 정합니다(D5).
