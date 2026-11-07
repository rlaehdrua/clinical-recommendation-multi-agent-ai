"""P0-1·P0-2 독립 평가 사례 작성 (서연 데이터, 정답 쪽만). 출력: data/eval/p0_fact_cases/*.json

작성 원칙: 판정 구현(ctrec.value_grounding, ctrec.evidence_directness 등)을 열어 보지 않고, 원문 처리 원칙만으로 기대값을 정함.
- 같은 항목에 같은 시점의 다른 값 -> 어느 값도 채택하지 않음 (hold)
- 시점이 다른 값 -> 이후 값으로 대체된 과거 값은 채택하지 않음 (hold), 가장 최근 날짜 값은 채택 (accept)
- 부정 뒤 번복 -> 상충 (conflict)
- 의사 말을 기억으로 전함·가족 전언·불확실 표현 -> 보류 (hold)
- 확실한 본인 값(자기 보고 허용 항목)·날짜 있는 문서 값 -> 채택 (accept): 과잉 보류 확인용 정상 대조
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
    V.append({"case_id": f"V{len(V) + 1:02d}", "category": cat, "field": field, "value": value, "unit": unit,
              "text": text, "expected": exp, "basis": basis})


def n(cat, criterion, quote, text, exp, basis):
    N.append({"case_id": f"N{len(N) + 1:02d}", "category": cat, "criterion": criterion, "quote": quote, "text": text,
              "expected": exp, "basis": basis})


# 상충: 같은 항목, 같은 시점, 다른 값
v("conflict_same_item", "hemoglobin", 11.4, "g/dL", "CBC 2026-11-05: Hb 9.6 g/dL. Same-day progress note: Hb 11.4 g/dL.", H,
  "같은 날 같은 항목에 9.6과 11.4 - 어느 쪽이 맞는지 원문으로 정할 수 없음")
v("conflict_same_item", "platelets", 152, "10^9/L", "외래 검사지: 혈소판 152 x10^9/L. 같은 날 간호 기록: 혈소판 98 x10^9/L.", H,
  "같은 날 두 출처 값이 다름")
v("conflict_same_item", "creatinine", 1.1, "mg/dL",
  "Lab report: creatinine 1.1 mg/dL. Discharge summary lists creatinine 1.9 mg/dL for the same draw.", H, "같은 채혈의 값이 문서마다 다름")
v("conflict_same_item", "absolute neutrophil count", 1.6, "10^9/L",
  "ANC 1.6 x10^9/L reported; a repeat run on the same sample gave ANC 0.9 x10^9/L.", H, "같은 검체 재측정 값이 다름")
v("conflict_same_item", "total bilirubin", 1.0, "mg/dL",
  "Total bilirubin 1.0 mg/dL (lab A) and 2.3 mg/dL (lab B), both dated 2026-11-04.", H, "같은 날짜 두 검사실 값이 다름")
# 상충: 시점이 다른 값
v("conflict_timepoint_old", "hemoglobin", 8.7, "g/dL", "Hb 8.7 g/dL on 2026-08-02. Hb 10.9 g/dL on 2026-11-05.", H,
  "8.7은 이후 값(10.9)으로 대체된 과거 값")
v("conflict_timepoint_old", "ecog", 2, None, "ECOG 2 in July after surgery; today (2026-11-06) the oncologist recorded ECOG 1.", H,
  "ECOG 2는 과거, 현재 기록은 1")
v("conflict_timepoint_old", "platelets", 85, "10^9/L", "Platelets 85 x10^9/L last month; CBC 2026-11-07 platelets 160 x10^9/L.", H,
  "85는 지난달 값, 오늘 값 160")
v("conflict_timepoint_old", "creatinine clearance", 38, "mL/min", "크레아티닌 청소율 38 mL/min (2026-06), 2026-11-03 재검 62 mL/min.", H,
  "38은 이전 검사, 재검 62")
v("conflict_timepoint_old", "absolute neutrophil count", 0.8, "10^9/L",
  "ANC 2.4 x10^9/L on 2026-11-06. During the September chemo cycle ANC was 0.8 x10^9/L.", H, "0.8은 9월 항암 중 값")
v("conflict_timepoint_latest", "hemoglobin", 10.9, "g/dL", "Hb 8.7 g/dL on 2026-08-02. Hb 10.9 g/dL on 2026-11-05.", A,
  "가장 최근 날짜의 검사값 - 정상 대조 (과잉 보류 확인)")
v("conflict_timepoint_latest", "platelets", 160, "10^9/L", "Platelets 85 x10^9/L last month; CBC 2026-11-07 platelets 160 x10^9/L.", A,
  "오늘 날짜 검사값 - 정상 대조")
# 전해 들은 값: 의사가 ~라고 했던 것 같다
v("hearsay_doctor", "hemoglobin", 12, None, "I think my doctor said my hemoglobin was about 12.", H, "의사 말을 기억으로 전함 + 'about'")
v("hearsay_doctor", "creatinine", 1.2, None, "의사 선생님이 신장 수치가 1.2라고 했던 것 같아요.", H, "의사 말을 기억으로 전함")
v("hearsay_doctor", "platelets", 140, None, "My oncologist mentioned, I believe, that my platelets were 140.", H, "'I believe' - 기억에 의존한 전언")
v("hearsay_doctor", "total bilirubin", 1.0, None, "주치의가 빌리루빈은 정상이라고 했던 것 같은데, 숫자는 1.0이었나 싶어요.", H, "전언 + 숫자 불확실")
v("hearsay_doctor", "absolute neutrophil count", 2, None, "Pt recalls the doctor saying her ANC was 'around 2'.", H, "전언 + 'around'")
# 전해 들은 값: 가족이 전한 값
v("hearsay_family", "hemoglobin", 10.5, None, "Daughter says her mother's hemoglobin was 10.5 at the last visit; no report brought.", H,
  "가족 전언, 결과지 없음")
v("hearsay_family", "platelets", 110, None, "아들 말로는 아버지 혈소판이 110이었다고 함. 결과지 없음.", H, "가족 전언")
v("hearsay_family", "creatinine", 0.9, None, "Husband reports her creatinine was 0.9 last week (no report available).", H, "배우자 전언")
v("hearsay_family", "ecog", 0, None, "Wife says his ECOG is 0 because he walks 5 km a day. No clinician assessment yet.", H,
  "가족이 수행도를 평가 - 의료진 평가 아님")
v("hearsay_family", "inr", 1.1, None, "보호자(딸)가 INR이 1.1이었다고 전함, 확인 불가.", H, "가족 전언, 확인 불가")
# 전해 들은 값: 불확실 표현
v("uncertain", "hemoglobin", 9, None, "Hb maybe 9 or 10, I don't remember exactly.", H, "택일 + 기억 불확실")
v("uncertain", "platelets", 100, None, "혈소판이 100에서 150 사이였던 것 같아요.", H, "범위 + '것 같다'")
v("uncertain", "creatinine", 1.5, None, "Creatinine probably around 1.5?", H, "'probably around' + 물음표")
v("uncertain", "absolute neutrophil count", 1.5, None, "ANC was something like 1.5, not sure.", H, "'something like' + 'not sure'")
v("uncertain", "inr", 1.3, None, "INR 1.3쯤? 정확하지 않음.", H, "'쯤' + '정확하지 않음'")
# 정상 대조: 확실한 본인 값 (자기 보고 허용 항목)
v("control_self_certain", "age", 64, None, "I am 64 years old; date of birth 1962-03-14 on my ID.", A, "본인 진술 + 생년월일 일치")
v("control_self_certain", "age", 58, None, "Patient states: I'm 58.", A, "확실한 본인 나이 진술")
v("control_self_certain", "age", 47, None, "환자 본인: 나이 47세.", A, "확실한 본인 나이 진술")
v("control_self_certain", "age", 67, None, "본인 진술: 1959년생, 만 67세.", A, "생년과 만 나이가 맞음 (기준일 2026-11-07)")
v("control_self_certain", "sex", "female", None, "Sex: female (self-identified, matches ID).", A, "본인 진술 + 신분증 일치")
# 정상 대조: 문서 근거 값
v("control_document", "hemoglobin", 11.8, "g/dL", "CBC 2026-11-05: Hb 11.8 g/dL.", A, "날짜 있는 검사 결과")
v("control_document", "platelets", 210, "10^9/L", "Lab report 2026-11-04: platelets 210 x10^9/L.", A, "날짜 있는 검사 결과")
v("control_document", "creatinine", 0.9, "mg/dL", "검사 결과지(2026-11-06): 크레아티닌 0.9 mg/dL.", A, "날짜 있는 검사 결과지")
v("control_document", "total bilirubin", 0.7, "mg/dL", "Chemistry 2026-11-03: total bilirubin 0.7 mg/dL.", A, "날짜 있는 검사 결과")
v("control_document", "absolute neutrophil count", 3.2, "10^9/L", "CBC with differential 2026-11-05: ANC 3.2 x10^9/L.", A, "날짜 있는 검사 결과")
v("control_document", "ecog", 1, None, "Oncology clinic note 2026-11-06: ECOG performance status 1.", A, "의료진 평가 기록")

# 서술 상충: 부정 뒤 번복
n("negation_reversed", "History of pneumonitis requiring steroids", "Denies ILD or pneumonitis.",
  "Denies ILD or pneumonitis. Later in the visit the patient recalls prednisone treatment for radiation pneumonitis in 2025.", C,
  "부정 뒤 같은 방문에서 번복 (스테로이드 치료 폐렴)")
n("negation_reversed", "Active autoimmune disease requiring systemic treatment", "자가면역질환 없다고 함",
  "처음 문진에서 자가면역질환 없다고 함. 재문진에서 루푸스로 hydroxychloroquine 복용 중이라고 정정함.", C,
  "부정 뒤 정정 (전신 치료 중 루푸스)")
n("negation_reversed", "History of another primary malignancy", "No prior cancer.",
  "No prior cancer. Same-day addendum: treated for papillary thyroid carcinoma in 2021 with thyroidectomy and radioiodine.", C,
  "부정 뒤 추가 기록으로 번복 (다른 원발암)")
n("negation_reversed", "Known HIV infection", "HIV 음성",
  "초진 기록: HIV 음성. 추가 문진: 2019년 HIV 진단 후 항레트로바이러스제 복용 중이라고 밝힘.", C, "부정 뒤 번복 (HIV 치료 중)")
n("negation_reversed", "Congestive heart failure", "Denies heart failure.",
  "Denies heart failure. Addendum: echo 2026-10 shows EF 28%; started sacubitril/valsartan for HFrEF.", C,
  "부정 뒤 추가 기록 (심초음파 EF 28%)")
# 정상 대조: 부정이 다른 근거와 일치
n("control_consistent", "History of interstitial lung disease", "Denies ILD.",
  "Denies ILD. HRCT 2026-10: no interstitial abnormality.", A, "부정과 영상이 일치")
n("control_consistent", "Active autoimmune disease requiring systemic treatment", "자가면역질환 없음.",
  "자가면역질환 없음. 재문진에서도 없음을 확인함. 복용 약: 암로디핀.", A, "부정이 재확인됨")
n("control_consistent", "Known HIV infection", "No known HIV.",
  "No known HIV. HIV Ag/Ab screen negative on 2026-10-20.", A, "부정과 검사가 일치")
n("control_consistent", "Congestive heart failure", "Denies heart failure.",
  "Denies heart failure. Echo 2026-10: EF 60%, normal chamber size.", A, "부정과 심초음파가 일치")
n("control_consistent", "History of another primary malignancy", "No prior malignancy.",
  "No prior malignancy. Problem list reviewed with the patient: none.", A, "부정이 문제 목록과 일치")

META = {"author": "서연(데이터)", "written": "2026-11-08", "clinical_review": "미완료",
        "independence": "판정 구현 코드(ctrec.value_grounding, ctrec.evidence_directness 등)를 열어 보지 않고 원문 처리 원칙만으로 "
                        "기대값을 정함. 평가 스크립트의 입력 형식(scripts/eval_m_holdout.py)과 held-out 6 사례 문장은 읽었으므로 "
                        "held-out 6과 완전히 독립은 아님",
        "expected_values": "accept = 그 값을 사실로 채택 / hold = 채택하지 않고 보류 / conflict = 상충으로 탐지(보류 이상)",
        "note": "합성 문장. 실제 기록의 빈도·분포를 대표하지 않음"}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, cases in (("value_cases.json", V), ("narrative_conflict_cases.json", N)):
        (OUT / name).write_text(json.dumps({**META, "cases": cases}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(dict(Counter(x["category"] for x in V)), dict(Counter(x["category"] for x in N)))


if __name__ == "__main__":
    main()
