"""NSCLC 범위 안 기준의 표현 타당성 검토표 (4.5단계, 서연 데이터 검토) -> data/nsclc/review/expression_review.csv.

질환·조직형·병기 표현, 검체 요구, 과거값·시점 조건. 수정은 GT·슬롯 사전 범위만, 엔진 쪽은 보고만 (engine_side_report_only).
임상 검수 미완료. 저장소 루트에서 실행: python scripts/nsclc_expression_review.py
"""
import csv
rows = []
def add(ptype, t, c, orig, cur, sug, clin, applied, engine=""):
    rows.append({"problem_type": ptype, "trial_id": t, "criterion_id": c, "original_text": orig,
                 "current_representation": cur, "suggestion": sug, "clinical_review_needed": clin,
                 "applied_in": applied, "engine_side_report_only": engine,
                 "verifier": "서연(데이터)", "clinical_review": "미완료"})
# 1. 질환 동의어·조직형·병기
add("질환 표현(조직형)", "NCT07680790", "I1", "histologically or cytologically confirmed non-squamous NSCLC",
    "dx.histology in {adenocarcinoma, nonsquamous_other}. NSCLC-NOS 값은 슬롯에서 뺌",
    "NSCLC-NOS(조직형 미분류)를 비편평으로 볼지 임상 검수. 포함한다면 슬롯 값 nos를 되살리고 이 기준 값에 추가", "예",
    "슬롯 사전(값 목록), GT(값 목록)")
add("질환 표현(조직형)", "NCT06928389", "E1", "Histologic or cytopathologic evidence of the presence of small cell lung carcinoma.",
    "dx.histology in {mixed_sclc}. 순수 소세포암 값 없음", "대상 집단이 NSCLC라 순수 SCLC는 생성하지 않음. 실제 환자 적용 시 sclc 값 추가 필요", "아니오",
    "슬롯 사전")
add("질환 표현(조직형)", "NCT06216301", "E1", "Mixed small cell and NSCLC histology.", "dx.histology in {mixed_sclc}",
    "일치. 다른 시험(NCT06928389 E1)과 같은 값으로 묶임", "아니오", "-")
add("병기 표현(AJCC 판)", "NCT07416058", "I3", "locally advanced or metastatic NSCLC (AJCC 9th Edition, Stage IIIB-IVB)",
    "dx.stage in {IIIB, IIIC, IVA, IVB}. 슬롯 값에 AJCC 판 구분 없음",
    "9판은 N2·M1c를 세분해 8판과 병기 경계가 일부 다름. 8판 시험(NCT06216301 I4, NCT06928389 I7: 범위 밖)과 섞을 때 판별 슬롯 또는 판 표시 필요", "예",
    "-", "파서는 판 정보를 기준 문장에만 남김 (필드 없음)")
add("병기 표현(재발)", "NCT07680790", "I2", "newly diagnosed locally advanced (Stage IIIB/IIIC), metastatic (Stage IVA/IVB), or recurrent disease ...",
    "판단 기준 (dx.disease_setting). dx.stage 값에서 recurrent를 뺌 (병기와 축이 다름)",
    "재발은 병기가 아니라 질병 경과 - dx.disease_setting으로만 표현. dx.stage와 분리 유지", "예", "슬롯 사전")
add("병기 표현('advanced')", "NCT07098338", "I7|I8", "Histologically or cytologically documented advanced or metastatic NSCLC",
    "미연결 (gt_use=unbound)", "'advanced'에 IIIB/IIIC 포함 여부를 임상 검수 후 dx.stage로 연결", "예", "검수표(모호)")
add("유전자 변이 표현", "NCT07680790", "I3", "documented EGFR-TKI-sensitizing mutation (exon 19 deletion or exon 21 L858R substitution)",
    "bm.driver_alteration in {egfr_ex19del_L858R}", "괄호 안 두 변이로 한정되어 일치. 비정형 감수성 변이(G719X·L861Q·S768I)는 egfr_uncommon으로 제외됨 - 의도 확인", "예", "-")
add("유전자 변이 표현", "NCT07027514", "E3", "Have known EGFR or ALK mutations",
    "bm.driver_alteration in {egfr_ex19del_L858R, egfr_uncommon, egfr_ex20ins, alk}. not_tested는 제외 대상 아님",
    "'known' 해석(미검사 = 모름 = 제외 안 함) 확인", "예", "-")
add("면역치료 범위", "NCT07416058", "I7", "Prior immunotherapy is not permitted.", "미연결 (슬롯은 면역관문억제제만)",
    "면역치료 범위(사이토카인·백신·세포치료 포함 여부) 검수 후 슬롯 추가", "예", "검수표(모호)")
add("면역치료 범위", "NCT06928389", "I9", "Previously received ... PD-1/L1 inhibitors.",
    "tx.prior_pd1_pdl1 in {durvalumab_consolidation, anti_pd1_pdl1}", "durvalumab 공고요법을 'PD-L1 억제제 치료력'으로 셈 - 확인", "예", "-")
# 2. 검체
for t, c, orig, cur, sug in [
    ("NCT07416058", "I8", "PD-L1 TPS ≥ 50%, as confirmed by central laboratory testing using the 22C3 or SP263 clone on fresh or archival tumor tissue (collected within 2 years)",
     "미연결. 슬롯 bm.pdl1_tps는 값만 있고 검체·검사기관·클론·채취 시점이 없음", "검체 조건을 판단 기준 후보로 검수하거나, 검사 메타(기관·클론·채취일) 슬롯 추가"),
    ("NCT07027514", "I4", "Thyroid-stimulating hormone (TSH) within normal limits", "lab.tsh_normal (혈청, 정상 여부만)", "검사 시점 조건이 원문에 없음 - 유효 기간 가정 필요"),
    ("NCT07098338", "E9", "HBV (verified by known positive HBsAg result), HCV", "미연결 (활동성 감염 전체를 표현 못함)", "HBsAg(혈청)·HCV RNA 검사 슬롯과 활동성 감염 슬롯 분리"),
    ("NCT07027514", "E3", "Have known EGFR or ALK mutations", "bm.driver_alteration (검체 구분 없음)", "조직·혈장(ctDNA) 결과를 같은 값으로 봄 - 시험 의도 확인"),
    ("NCT07416058", "I4", "harbor confirmed actionable driver mutations ...", "bm.driver_alteration (검체 구분 없음)", "'confirmed' 검사 방법(조직·혈장) 확인"),
    ("NCT07680790", "I3", "documented EGFR-TKI-sensitizing mutation", "bm.driver_alteration (검체 구분 없음)", "'documented' 검사 방법 확인"),
    ("NCT06216301|NCT06928389|NCT07680790", "E1|E1|I1", "Histologic or cytologic ...", "dx.histology (조직·세포 구분 없음)", "조직·세포 검체 모두 인정하는 문구 - 현재 표현과 일치"),
    ("NCT07098338", "I6", "Provision of acceptable tumour tissue", "판단 기준 (other.tumor_tissue)", "검체 적합성 판단 - 판단 기준 유지"),
]:
    add("검체", t, c, orig, cur, sug, "예" if "일치" not in sug else "아니오", "-",
        "엔진 사실 형식에 검체 속성 없음 (보고만)" if "검체 구분 없음" in cur or "검체·검사기관" in cur else "")
# 3. 과거값·시점
for t, c, orig, tw, cur, res in [
    ("NCT06216301", "E4", "Had major surgery <3 weeks prior to randomization", "21", "proc.days_since_major_surgery in {lt_21d}", "일치"),
    ("NCT07416058", "E12", "Major surgery within 4 weeks before first dose of study treatment", "28", "proc.days_since_major_surgery in {lt_21d, 21_27d}", "일치"),
    ("NCT06216301", "E13", "during the 4 weeks prior to randomization", "28", "tx.other_investigational_4w (슬롯에 4주 포함)", "일치"),
    ("NCT07098338", "E19", "within 30 days prior to the first dose of study intervention", "30", "med.live_vaccine_30d (슬롯에 30일 포함)", "일치"),
    ("NCT07416058", "I6", "a 2-week washout period is required for prior TKI therapy or chemotherapy", "14", "tx.days_since_last_anticancer in {ge_14d}", "일치"),
    ("NCT06216301", "E12", "in the 12 months prior to randomization", "365", "미연결 (치료 시점 없음)", "모호"),
    ("NCT07027514", "E7", "Have received live vaccines prior to C1D1", "(없음)", "미연결 (원문에 기간 없음, 슬롯은 30일)", "모호"),
    ("NCT07416058", "I8", "tumor tissue (collected within 2 years)", "730", "미연결 (검체 채취 시점 없음)", "모호"),
    ("NCT06928389", "I2|I3", "at the time of randomization", "-", "age (기준일 2026-11-07 나이)", "일치 (시점 가정)"),
    ("NCT07098338", "I1", "at the time of signing the ICF", "-", "age (기준일 나이)", "일치 (시점 가정)"),
]:
    add("과거값·시점", t, c, orig, f"파싱 기간 {tw}일 / {cur}",
        "파싱된 기간 일수는 원문과 일치" if res == "일치" else
        ("기준 시점(무작위배정·동의·첫 투여)을 평가셋 기준일로 대체 - 경계 나이 환자는 시점에 따라 달라질 수 있음" if "시점 가정" in res
         else "시점 슬롯 추가 후 연결"), "아니오" if res == "일치" else "예",
        "GT·슬롯 사전(구간 경계)" if res == "일치" else "검수표(모호)" if res == "모호" else "-",
        "엔진 기간 조건은 검사값 유효 기간에만 쓰임 (범주형 기준의 기간은 슬롯 값으로만 표현) - 보고만" if res == "일치" else "")
with open("data/nsclc/review/expression_review.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
print(len(rows))
