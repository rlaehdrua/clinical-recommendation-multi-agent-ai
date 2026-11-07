"""P0-1·P0-2 독립 평가 사례 작성 (서연 데이터, 정답 쪽만). 출력: data/eval/p0_fact_cases/{value_cases,narrative_conflict_cases}.json

작성 원칙: 판정 구현(ctrec.value_grounding, ctrec.evidence_directness 등)과 엔진 출력을 보지 않고, 원문 처리 원칙만으로 기대값을 정함.
- 같은 항목에 같은 시점의 다른 값 -> 어느 값도 채택하지 않음 (hold)
- 시점이 다른 값 -> 이후 값으로 대체된 과거 값은 hold, 가장 최근 날짜 값은 accept
- 부정 뒤 번복, 문서와 자가보고가 충돌 -> 상충 (conflict, 보류도 안전)
- 의료진 말을 기억으로 전함·가족 전언·기억 불확실·다른 사람 값 -> hold
- 확실한 본인 값(자기 보고 허용 항목)·날짜 있는 검사값·문서 근거 -> accept (과잉 보류 측정용 정상 대조)
슬롯: NSCLC 축소 범위 위주 (나이, ECOG, 병기, 치료 이력, 검사 수치). 값 사례는 field·value·text 형식(held-out 6 labs와 같음),
병기·치료 이력처럼 값 비교 함수가 없는 항목은 서술 사례(criterion·quote·text, held-out 6 conflict와 같음)로 씀.
실행: python scripts/p0_fact_cases_build.py (저장소 루트)
"""

import json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "eval" / "p0_fact_cases"
H, A, C = "hold", "accept", "conflict"
V: list[dict] = []
N: list[dict] = []


def v(cat, field, value, unit, text, exp, basis):
    V.append({"case_id": f"V{len(V) + 1:03d}", "category": cat, "field": field, "value": value, "unit": unit,
              "text": text, "expected": exp, "basis": basis})


def n(cat, criterion, quote, text, exp, basis):
    N.append({"case_id": f"N{len(N) + 1:03d}", "category": cat, "criterion": criterion, "quote": quote, "text": text,
              "expected": exp, "basis": basis})


# ============================================================ 상충 1: 같은 항목, 같은 시점, 다른 값 (8)
cat = "conflict_same_item"
v(cat, "hemoglobin", 11.4, "g/dL", "CBC 2026-11-05: Hb 9.6 g/dL. Same-day progress note: Hb 11.4 g/dL.", H, "같은 날 같은 항목 9.6·11.4")
v(cat, "platelets", 152, "10^9/L", "외래 검사지: 혈소판 152 x10^9/L. 같은 날 간호 기록: 혈소판 98 x10^9/L.", H, "같은 날 두 출처 값이 다름")
v(cat, "creatinine", 1.1, "mg/dL", "Lab report: creatinine 1.1 mg/dL. Discharge summary lists creatinine 1.9 mg/dL for the same draw.", H, "같은 채혈 값이 문서마다 다름")
v(cat, "absolute neutrophil count", 1.6, "10^9/L", "ANC 1.6 x10^9/L reported; a repeat run on the same sample gave ANC 0.9 x10^9/L.", H, "같은 검체 재측정 값이 다름")
v(cat, "total bilirubin", 1.0, "mg/dL", "Total bilirubin 1.0 mg/dL (lab A) and 2.3 mg/dL (lab B), both dated 2026-11-04.", H, "같은 날짜 두 검사실 값")
v(cat, "ecog", 1, None, "2026-11-06 oncology note: ECOG 1. 2026-11-06 nursing assessment: ECOG 2.", H, "같은 날 두 평가가 다름")
v(cat, "age", 64, None, "Registration form: age 64. ID card on file: date of birth 1955-02-10 (age 71).", H, "같은 시점 두 나이가 다름")
v(cat, "creatinine clearance", 55, "mL/min", "CrCl 55 mL/min (Cockcroft-Gault) and 38 mL/min (24-h urine), both from 2026-11-03.", H, "같은 날 두 계산값 - 어느 쪽인지 원문으로 정할 수 없음")
# ============================================================ 상충 2: 시점이 다른 값 (과거 값 8 + 최근 값 대조 4)
cat = "conflict_timepoint_old"
v(cat, "hemoglobin", 8.7, "g/dL", "Hb 8.7 g/dL on 2026-08-02. Hb 10.9 g/dL on 2026-11-05.", H, "8.7은 이후 값으로 대체된 과거 값")
v(cat, "ecog", 2, None, "ECOG 2 in July after surgery; today (2026-11-06) the oncologist recorded ECOG 1.", H, "ECOG 2는 과거")
v(cat, "platelets", 85, "10^9/L", "Platelets 85 x10^9/L last month; CBC 2026-11-07 platelets 160 x10^9/L.", H, "85는 지난달 값")
v(cat, "creatinine clearance", 38, "mL/min", "크레아티닌 청소율 38 mL/min (2026-06), 2026-11-03 재검 62 mL/min.", H, "38은 이전 검사")
v(cat, "absolute neutrophil count", 0.8, "10^9/L", "ANC 2.4 x10^9/L on 2026-11-06. During the September chemo cycle ANC was 0.8 x10^9/L.", H, "0.8은 9월 값")
v(cat, "total bilirubin", 2.4, "mg/dL", "총 빌리루빈 2.4 mg/dL (2026-07-15, 담도 폐쇄 시). 스텐트 후 2026-11-02 0.9 mg/dL.", H, "2.4는 시술 전 과거 값")
v(cat, "ecog", 0, None, "ECOG 0 at diagnosis (2025-03). Current visit 2026-11-05: ECOG 2 due to bone pain.", H, "ECOG 0은 진단 당시 값")
v(cat, "hemoglobin", 12.5, "g/dL", "Baseline Hb 12.5 g/dL (2026-01). Most recent CBC 2026-11-04: Hb 8.9 g/dL.", H, "12.5는 연초 값")
cat = "control_timepoint_latest"
v(cat, "hemoglobin", 10.9, "g/dL", "Hb 8.7 g/dL on 2026-08-02. Hb 10.9 g/dL on 2026-11-05.", A, "가장 최근 날짜 값")
v(cat, "platelets", 160, "10^9/L", "Platelets 85 x10^9/L last month; CBC 2026-11-07 platelets 160 x10^9/L.", A, "오늘 날짜 값")
v(cat, "ecog", 2, None, "ECOG 0 at diagnosis (2025-03). Current visit 2026-11-05: ECOG 2 due to bone pain.", A, "현재 방문 평가")
v(cat, "total bilirubin", 0.9, "mg/dL", "총 빌리루빈 2.4 mg/dL (2026-07-15, 담도 폐쇄 시). 스텐트 후 2026-11-02 0.9 mg/dL.", A, "가장 최근 값")
# ============================================================ 상충 3: 부정 후 번복 (8, 서술)
cat = "conflict_negation_reversed"
n(cat, "History of pneumonitis requiring steroids", "Denies ILD or pneumonitis.",
  "Denies ILD or pneumonitis. Later in the visit the patient recalls prednisone treatment for radiation pneumonitis in 2025.", C, "같은 방문에서 번복")
n(cat, "Active autoimmune disease requiring systemic treatment", "자가면역질환 없다고 함",
  "처음 문진에서 자가면역질환 없다고 함. 재문진에서 루푸스로 hydroxychloroquine 복용 중이라고 정정함.", C, "재문진에서 정정")
n(cat, "History of another primary malignancy", "No prior cancer.",
  "No prior cancer. Same-day addendum: treated for papillary thyroid carcinoma in 2021 with thyroidectomy and radioiodine.", C, "추가 기록으로 번복")
n(cat, "Known HIV infection", "HIV 음성",
  "초진 기록: HIV 음성. 추가 문진: 2019년 HIV 진단 후 항레트로바이러스제 복용 중이라고 밝힘.", C, "추가 문진에서 번복")
n(cat, "Congestive heart failure", "Denies heart failure.",
  "Denies heart failure. Addendum: echo 2026-10 shows EF 28%; started sacubitril/valsartan for HFrEF.", C, "추가 기록 (EF 28%)")
n(cat, "Prior treatment with an anti-PD-1 or anti-PD-L1 agent", "No prior immunotherapy.",
  "No prior immunotherapy. Correction after pharmacy review: received pembrolizumab for 6 cycles in 2025.", C, "약국 확인 후 정정 (치료 이력)")
n(cat, "Prior systemic therapy for metastatic NSCLC", "전이성 폐암에 대한 항암치료 받은 적 없음",
  "초진: 전이성 폐암에 대한 항암치료 받은 적 없음. 타원 의뢰서 확인 후: 2026년 3-6월 carboplatin/pemetrexed 4주기 투여.", C, "의뢰서 확인 후 번복 (치료 이력)")
n(cat, "Stage IV disease", "Stage IIIA per initial staging.",
  "Stage IIIA per initial staging. Restaging PET 2026-10 shows new adrenal metastasis; now stage IVB.", C, "재병기에서 번복 (병기)")
# ============================================================ 상충 4: 문서 vs 자가보고 (8: 값 4 + 서술 4)
cat = "conflict_doc_vs_self"
v(cat, "ecog", 0, None, "Patient says she is fully active (ECOG 0). Clinic note 2026-11-06: ECOG 2, needs help with daily activities.", H, "자가보고 0 vs 문서 2")
v(cat, "age", 59, None, "Pt states he is 59. Hospital registration (DOB 1961-04-02): age 65.", H, "자가보고 59 vs 등록 65")
v(cat, "hemoglobin", 12, None, "환자는 빈혈 수치가 12라고 말함. 검사 결과지 2026-11-05: Hb 8.4 g/dL.", H, "자가보고 12 vs 결과지 8.4")
v(cat, "creatinine", 0.8, None, "Patient reports his kidney number was 0.8. Lab report 2026-11-04: creatinine 1.9 mg/dL.", H, "자가보고 0.8 vs 결과지 1.9")
n(cat, "Prior treatment with an anti-PD-1 or anti-PD-L1 agent", "Patient denies prior immunotherapy.",
  "Patient denies prior immunotherapy. Medication administration record: nivolumab 2025-03 to 2025-09.", C, "자가보고 부정 vs 투약 기록")
n(cat, "Prior platinum-based chemotherapy", "환자 본인은 백금 항암제를 맞은 적 없다고 함",
  "환자 본인은 백금 항암제를 맞은 적 없다고 함. 투약 기록: 2025년 cisplatin/pemetrexed 4주기.", C, "자가보고 부정 vs 투약 기록")
n(cat, "Known brain metastases", "Patient says he has no brain metastases.",
  "Patient says he has no brain metastases. Brain MRI report 2026-10-28: two enhancing lesions consistent with metastases.", C, "자가보고 부정 vs 영상 판독")
n(cat, "Stage IV disease", "환자는 3기라고 알고 있음",
  "환자는 3기라고 알고 있음. 병리·영상 종합 보고 2026-10: 간 전이, cT2N2M1b, IVA기.", C, "자가보고 병기 vs 문서 병기")
# ============================================================ 전해 들은 값 1: 의료진 전언 (8)
cat = "hearsay_clinician"
v(cat, "hemoglobin", 12, None, "I think my doctor said my hemoglobin was about 12.", H, "의사 말을 기억 + 'about'")
v(cat, "creatinine", 1.2, None, "의사 선생님이 신장 수치가 1.2라고 했던 것 같아요.", H, "의사 말을 기억")
v(cat, "platelets", 140, None, "My oncologist mentioned, I believe, that my platelets were 140.", H, "'I believe'")
v(cat, "total bilirubin", 1.0, None, "주치의가 빌리루빈은 정상이라고 했던 것 같은데, 숫자는 1.0이었나 싶어요.", H, "전언 + 숫자 불확실")
v(cat, "absolute neutrophil count", 2, None, "Pt recalls the doctor saying her ANC was 'around 2'.", H, "전언 + 'around'")
v(cat, "ecog", 1, None, "The nurse at the other hospital told me my performance score was probably 1.", H, "타원 간호사 전언 + 'probably'")
v(cat, "creatinine clearance", 50, None, "신장내과 선생님이 청소율이 50 정도 된다고 하셨던 것 같아요.", H, "전언 + '정도'")
v(cat, "hemoglobin", 9.5, None, "He says the ER doctor told him his blood count was 'nine point something'.", H, "전언 + 불명확한 숫자")
# ============================================================ 전해 들은 값 2: 가족 전언 (8)
cat = "hearsay_family"
v(cat, "hemoglobin", 10.5, None, "Daughter says her mother's hemoglobin was 10.5 at the last visit; no report brought.", H, "가족 전언, 결과지 없음")
v(cat, "platelets", 110, None, "아들 말로는 아버지 혈소판이 110이었다고 함. 결과지 없음.", H, "가족 전언")
v(cat, "creatinine", 0.9, None, "Husband reports her creatinine was 0.9 last week (no report available).", H, "배우자 전언")
v(cat, "ecog", 0, None, "Wife says his ECOG is 0 because he walks 5 km a day. No clinician assessment yet.", H, "가족이 수행도를 평가")
v(cat, "inr", 1.1, None, "보호자(딸)가 INR이 1.1이었다고 전함, 확인 불가.", H, "가족 전언, 확인 불가")
v(cat, "age", 72, None, "보호자(아들)가 환자 나이를 72세라고 전함. 환자는 의사소통 어려움, 신분증 미지참.", H, "가족이 전한 나이, 확인 불가")
v(cat, "absolute neutrophil count", 3, None, "Son relays that the oncology clinic said white cells were fine, neutrophils 'about 3'.", H, "가족 전언 + 'about'")
v(cat, "total bilirubin", 1.2, None, "딸이 간 수치(빌리루빈) 1.2였다고 메모를 보여줌. 출처·날짜 없음.", H, "가족 메모, 출처 없음")
# ============================================================ 전해 들은 값 3: 기억 불확실 (8)
cat = "hearsay_memory"
v(cat, "hemoglobin", 9, None, "Hb maybe 9 or 10, I don't remember exactly.", H, "택일 + 기억 불확실")
v(cat, "platelets", 100, None, "혈소판이 100에서 150 사이였던 것 같아요.", H, "범위 + '것 같다'")
v(cat, "creatinine", 1.5, None, "Creatinine probably around 1.5?", H, "'probably around' + 물음표")
v(cat, "absolute neutrophil count", 1.5, None, "ANC was something like 1.5, not sure.", H, "'something like' + 'not sure'")
v(cat, "inr", 1.3, None, "INR 1.3쯤? 정확하지 않음.", H, "'쯤' + '정확하지 않음'")
v(cat, "age", 68, None, "나이는 68인가 69인가, 정확히 기억이 안 남. 생년월일 모름.", H, "택일 + 생년월일 모름")
v(cat, "ecog", 1, None, "I guess I'm mostly OK, maybe a 1? I can't remember what they scored me.", H, "'I guess' + 'maybe'")
v(cat, "creatinine clearance", 60, None, "Clearance was in the 60s, I think, but I'm not certain.", H, "범위 + 'not certain'")
# ============================================================ 전해 들은 값 4: 다른 사람 값 혼동 (8)
cat = "hearsay_other_person"
v(cat, "hemoglobin", 9.1, "g/dL", "Wife's Hb was 9.1 g/dL at her own visit; patient's CBC is pending.", H, "배우자의 값")
v(cat, "ecog", 1, None, "Patient's sister (also in treatment) has ECOG 1; patient not yet assessed.", H, "자매의 수행도")
v(cat, "platelets", 95, "10^9/L", "환자 남편의 혈소판 95라고 함. 환자 본인 검사는 아직.", H, "남편의 값")
v(cat, "creatinine", 2.1, "mg/dL", "Roommate's creatinine 2.1 mg/dL was read aloud by mistake; patient's lab not drawn.", H, "다른 환자의 값")
v(cat, "age", 45, None, "Daughter (45 years old) accompanies the patient. Patient's age not recorded.", H, "보호자의 나이")
v(cat, "absolute neutrophil count", 0.7, "10^9/L", "같은 병실 환자의 ANC 0.7 결과지가 잘못 첨부됨. 본 환자 결과 아님.", H, "다른 환자 결과지")
v(cat, "total bilirubin", 3.0, "mg/dL", "Patient mentions her brother's bilirubin was 3.0 when he had liver cancer.", H, "형제의 값")
v(cat, "hemoglobin", 13.2, "g/dL", "Lab printout belongs to the husband (Hb 13.2 g/dL); patient's own CBC not available.", H, "남편 결과지")
# ============================================================ 정상 대조 1: 확실한 본인 값 (10)
cat = "control_self_certain"
v(cat, "age", 64, None, "I am 64 years old; date of birth 1962-03-14 on my ID.", A, "본인 진술 + 생년월일 일치")
v(cat, "age", 58, None, "Patient states: I'm 58.", A, "확실한 본인 나이 진술")
v(cat, "age", 47, None, "환자 본인: 나이 47세.", A, "확실한 본인 나이 진술")
v(cat, "age", 67, None, "본인 진술: 1959년생, 만 67세.", A, "생년과 만 나이가 맞음 (기준일 2026-11-07)")
v(cat, "sex", "female", None, "Sex: female (self-identified, matches ID).", A, "본인 진술 + 신분증 일치")
v(cat, "age", 71, None, "Patient reports age 71 (born 1955-06-20), confirmed with insurance card.", A, "본인 진술 + 보험증 확인")
v(cat, "age", 52, None, "환자 본인 진술: 52세, 1974년 3월생.", A, "생년과 나이 일치")
v(cat, "sex", "male", None, "Patient is a 60-year-old man.", A, "명확한 성별 기술")
v(cat, "age", 60, None, "Patient is a 60-year-old man.", A, "명확한 나이 기술")
v(cat, "age", 75, None, "75세 남자 환자. 신분증으로 확인.", A, "나이 + 신분증 확인")
# ============================================================ 정상 대조 2: 날짜 있는 검사값 (12)
cat = "control_dated_lab"
v(cat, "hemoglobin", 11.8, "g/dL", "CBC 2026-11-05: Hb 11.8 g/dL.", A, "날짜 있는 검사 결과")
v(cat, "platelets", 210, "10^9/L", "Lab report 2026-11-04: platelets 210 x10^9/L.", A, "날짜 있는 검사 결과")
v(cat, "creatinine", 0.9, "mg/dL", "검사 결과지(2026-11-06): 크레아티닌 0.9 mg/dL.", A, "날짜 있는 검사 결과지")
v(cat, "total bilirubin", 0.7, "mg/dL", "Chemistry 2026-11-03: total bilirubin 0.7 mg/dL.", A, "날짜 있는 검사 결과")
v(cat, "absolute neutrophil count", 3.2, "10^9/L", "CBC with differential 2026-11-05: ANC 3.2 x10^9/L.", A, "날짜 있는 검사 결과")
v(cat, "creatinine clearance", 72, "mL/min", "2026-11-04 Cockcroft-Gault creatinine clearance 72 mL/min.", A, "날짜 있는 계산값")
v(cat, "hemoglobin", 13.1, "g/dL", "2026-11-06 혈액검사: 혈색소 13.1 g/dL.", A, "날짜 있는 검사 결과 (한국어)")
v(cat, "platelets", 185, "10^9/L", "2026-11-02 CBC: PLT 185 x10^9/L, WBC 6.2.", A, "날짜 있는 검사 결과")
v(cat, "absolute neutrophil count", 2.1, "10^9/L", "2026-11-05 호중구 절대수 2.1 x10^9/L.", A, "날짜 있는 검사 결과 (한국어)")
v(cat, "total bilirubin", 1.1, "mg/dL", "LFT 2026-11-04: T.bil 1.1 mg/dL, AST 30, ALT 28.", A, "날짜 있는 검사 결과 (약어)")
v(cat, "creatinine", 1.0, "mg/dL", "BMP 2026-11-03: Cr 1.0 mg/dL.", A, "날짜 있는 검사 결과 (약어)")
v(cat, "inr", 1.0, None, "Coagulation 2026-11-05: INR 1.0.", A, "날짜 있는 검사 결과")
# ============================================================ 정상 대조 3: 문서 근거 (값 4 + 서술 8)
cat = "control_document"
v(cat, "ecog", 1, None, "Oncology clinic note 2026-11-06: ECOG performance status 1.", A, "의료진 평가 기록")
v(cat, "ecog", 0, None, "2026-11-05 종양내과 외래 기록: ECOG 0.", A, "의료진 평가 기록 (한국어)")
v(cat, "age", 63, None, "Hospital registration: DOB 1963-01-15, age 63.", A, "등록 문서")
v(cat, "ecog", 2, None, "Physician assessment 2026-11-04: ECOG PS 2 (ambulatory, unable to work).", A, "의료진 평가 기록")
n(cat, "History of interstitial lung disease", "Denies ILD.", "Denies ILD. HRCT 2026-10: no interstitial abnormality.", A, "부정과 영상 일치")
n(cat, "Active autoimmune disease requiring systemic treatment", "자가면역질환 없음.",
  "자가면역질환 없음. 재문진에서도 없음을 확인함. 복용 약: 암로디핀.", A, "부정이 재확인됨")
n(cat, "Known HIV infection", "No known HIV.", "No known HIV. HIV Ag/Ab screen negative on 2026-10-20.", A, "부정과 검사 일치")
n(cat, "Congestive heart failure", "Denies heart failure.", "Denies heart failure. Echo 2026-10: EF 60%, normal chamber size.", A, "부정과 심초음파 일치")
n(cat, "History of another primary malignancy", "No prior malignancy.",
  "No prior malignancy. Problem list reviewed with the patient: none.", A, "부정이 문제 목록과 일치")
n(cat, "Prior treatment with an anti-PD-1 or anti-PD-L1 agent", "No prior immunotherapy.",
  "No prior immunotherapy. Pharmacy records reviewed 2026-11-05: no checkpoint inhibitor dispensed.", A, "부정이 약국 기록과 일치 (치료 이력)")
n(cat, "Prior systemic therapy for metastatic NSCLC", "전이성 폐암 진단 후 항암치료 없음",
  "2026-10 전이성 폐암 진단. 전이성 폐암 진단 후 항암치료 없음 (의뢰서·투약 기록 확인).", A, "치료 이력 문서 일치")
n(cat, "Stage IV disease", "Stage IVA (cT3N2M1a) per PET-CT 2026-10-20.",
  "Stage IVA (cT3N2M1a) per PET-CT 2026-10-20; pleural effusion cytology positive.", A, "병기 문서 일치")

META = {"author": "서연(데이터)", "written": "2026-11-08", "clinical_review": "미완료",
        "independence": "판정 구현 코드(ctrec.value_grounding, ctrec.evidence_directness 등)를 열어 보지 않고 원문 처리 원칙만으로 기대값을 정함. "
                        "평가 스크립트 입력 형식(scripts/eval_m_holdout.py)과 held-out 6 사례 문장은 읽었고, 1차 사례(C 단계 48건)의 현재 결과는 "
                        "확장 전에 보았음 -> 완전 비공개 작성은 아님. 확장 사례(I 단계)의 기대값은 결과를 보고 고치지 않음",
        "expected_values": "accept = 그 값을 사실로 채택 / hold = 채택하지 않고 보류 / conflict = 상충으로 탐지(보류도 안전)",
        "note": "합성 문장. 실제 기록의 빈도·분포를 대표하지 않음"}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, cases in (("value_cases.json", V), ("narrative_conflict_cases.json", N)):
        (OUT / name).write_text(json.dumps({**META, "cases": cases}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    allc = Counter(x["category"] for x in V + N)
    print(dict(sorted(allc.items())), "total", len(V) + len(N),
          "controls", sum(v_ for k, v_ in allc.items() if k.startswith("control_")))


if __name__ == "__main__":
    main()
