"""NSCLC 6개 시험 프로토콜·파싱 검수표 작성 (2단계). 서연(데이터) 검토 결과를 표로 고정합니다.

- 입력(읽기만): data/nsclc/trials/*.json(ClinicalTrials.gov 원문), data/nsclc/parsed/*.json(파싱본),
  data/nsclc/cohort/slot_candidates.csv, 선우 검수 큐 CSV(인자 1, 기본 docs/dev/expert_rules_review_queue_2026_11_07.csv,
  없으면 빈 값. 원격 feat/p-common-engine 브랜치에 있음: git show origin/feat/p-common-engine:docs/dev/...)
- 출력: data/nsclc/review/criteria_verification.csv, protocol_status.csv, overrides.json, expert_criteria.csv
- 원본 파일(파싱본·원문)은 바꾸지 않습니다. 파싱 수정 제안은 overrides.json에만 둡니다.

아래 REVIEW 표가 사람의 검토 결과입니다(데이터 검증). 임상 검수는 받지 않았으며 모든 행이 clinical_review=미완료입니다.
scope 값
- in_rule_engine : 나이·성별·ECOG (팀 결정 3)
- in_categorical : CSV에 슬롯이 있는 범주형 기준 (팀 결정 3)
- in_lab         : 검사 수치 4개(ANC·총 빌리루빈·크레아티닌 청소율·Hb). 6개 시험에는 해당 기준이 없음
- expert         : 임상의 판단 기준 (spec.is_expert_slot 슬롯). GT에서 NEEDS_EXPERT
- excluded       : 동의서·절차 기준 (팀 결정 2). GT 집계 제외, 목록만 남김
- out_of_scope   : 축소 범위 밖. 평가셋 시험 정의에서 빠짐 (전체 적격성을 주장하지 않음)
result 값: 일치 / 불일치 / 모호 / 미검증(범위 밖은 원문 대조를 하지 않음)
gt_use 값: bindable(GT·생성기 정답에 씀) / unbound(범위 안이지만 슬롯이 원문을 다 표현하지 못함 -> NOT_EVALUATED)
           / region_conditional(지역 조건부) / needs_expert / excluded / out_of_scope
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ctrec.cohort.spec import is_expert_slot  # noqa: E402

NSCLC = ROOT / "data" / "nsclc"
OUT = NSCLC / "review"
TRIALS_ALL = ["NCT04892953", "NCT05255302", "NCT06216301", "NCT06928389", "NCT07027514", "NCT07098338",
              "NCT07416058", "NCT07680790"]
VERIFIER, CLINICAL = "서연(데이터)", "미완료"

# 공통 값 집합
EGFR_ALK = ["egfr_ex19del_L858R", "egfr_uncommon", "egfr_ex20ins", "alk"]
ICI_ANY = ["durvalumab_consolidation", "anti_pd1_pdl1", "ici_other"]
PD1_L1 = ["durvalumab_consolidation", "anti_pd1_pdl1"]
FEMALE = {"slot": "sex", "values": ["female"]}

# (시험, 기준) -> 검토 결과. 적지 않은 기준은 out_of_scope·미검증(범위 밖)으로 채움
R: dict[tuple[str, str], dict] = {}


def rv(t, r, scope, result, *, slot=None, op=None, values=None, applies_if=None, gt_use=None, issue="", suggestion="",
       expert_slot=None, timing="", specimen="", region="", cohort_check="", quote=""):
    R[(t, r)] = dict(scope=scope, result=result, slot=slot, op=op, values=values or [], applies_if=applies_if,
                     gt_use=gt_use, issue=issue, suggestion=suggestion, expert_slot=expert_slot, timing=timing,
                     specimen=specimen, region=region, cohort_check=cohort_check, quote=quote)


# ---------------------------------------------------------------- NCT06216301 (코호트 없음)
T = "NCT06216301"
rv(T, "I1", "in_rule_engine", "불일치", slot="age", op=">=", values=["22"], gt_use="region_conditional",
   region="USA 한정", quote="≥22 years of age in the USA",
   issue="지역 조건(in the USA) 누락: 파싱본은 모든 환자에 22세 이상을 요구. USA 밖 18-21세 환자가 거짓 부적격",
   suggestion="조건부 기준으로 표시(국가=USA일 때만 적용). 국가 슬롯이 없으므로 22세 미만이면 판정하지 않음(NOT_EVALUATED)")
rv(T, "I2", "in_rule_engine", "일치", slot="age", op=">=", values=["18"], region="USA 밖 (판정 영향 없음)",
   quote="≥18 years of age outside of the USA.",
   issue="지역 조건(outside of the USA) 누락. USA는 22세 이상을 요구하므로 18세 이상은 모든 지역의 필요조건 -> 판정 동치")
rv(T, "I3", "in_rule_engine", "일치", slot="sex", op="==", values=["all"], quote="(구조화 항목 sex=ALL)")
rv(T, "I7", "in_rule_engine", "일치", slot="ecog", op="<=", values=["1"], quote="ECOG Performance Status (PS) of 0-1.")
rv(T, "I8", "in_categorical", "모호", gt_use="unbound", slot="lab.inr_aptt_uln",
   quote="Adequate hematologic and end-organ function / For subjects not receiving therapeutic anticoagulation: INR or aPTT ≤ 1.5 x ULN",
   issue="'적절한 혈액·장기 기능'에 기준값 없음(원문). 응고 수치만 조건부(항응고 비사용자) 기준값 있음",
   suggestion="임계값이 원문에 없으므로 연결하지 않음. 전체 프로토콜의 검사 기준값 확인 필요", specimen="혈액")
rv(T, "I9", "in_categorical", "일치", slot="repro.pregnant_or_breastfeeding", op="in", values=["no"], applies_if=FEMALE,
   quote="A female participant is eligible to participate if she is not pregnant, not breastfeeding",
   issue="CSV 인용은 E16에만 매칭됨. 같은 슬롯으로 연결 가능", suggestion="여성에게만 적용(남성은 해당 없음)")
rv(T, "I10", "excluded", "미검증", gt_use="excluded", quote="must agree to use an effective contraception",
   issue="동의·절차(피임 동의) - 팀 결정 2")
rv(T, "I11", "excluded", "미검증", gt_use="excluded", quote="All subjects must sign written informed consent.",
   issue="동의서 - 팀 결정 2")
rv(T, "E1", "in_categorical", "일치", slot="dx.histology", op="in", values=["mixed_sclc"],
   quote="Mixed small cell and NSCLC histology.", specimen="조직·세포")
rv(T, "E4", "in_categorical", "일치", slot="proc.days_since_major_surgery", op="in", values=["lt_21d"],
   timing="무작위배정 전 3주", quote="Had major surgery <3 weeks prior to randomization",
   issue="슬롯을 일수 구간(lt_21d / 21_27d / ge_28d_or_none)으로 표현")
rv(T, "E5", "expert", "일치", expert_slot="tx.recent_radiotherapy", gt_use="needs_expert", timing="6개월",
   quote="Received radiation therapy to the lung that is > 30 Gy within 6 months of randomization.",
   issue="CSV adjudication=expert 슬롯(선량·부위 확인)")
rv(T, "E6", "expert", "일치", expert_slot="tx.recent_radiotherapy", gt_use="needs_expert", timing="2주 (완화 목적 1주)",
   quote="Has received prior radiotherapy within 2 weeks of randomization. Subjects must have recovered ...",
   issue="방사선 독성 회복·폐렴 여부 포함 - 판단 기준")
rv(T, "E11", "in_categorical", "모호", gt_use="unbound", slot="cm.immunodeficiency", timing="7일",
   quote="Has a diagnosis of immunodeficiency or is receiving chronic systemic steroid therapy (in dosing exceeding 10 mg daily ...) or any other form of immunosuppressive therapy within 7 days",
   issue="면역결핍(범주형) + 스테로이드 용량(수치) + 기타 면역억제제(fact_llm)의 OR. 범주형 슬롯만으로 표현 불가, 천식 예외 포함",
   suggestion="스테로이드 용량 구간·면역억제제 슬롯을 추가해 any 결합으로 연결 (검수 필요)")
rv(T, "E12", "in_categorical", "모호", gt_use="unbound", slot="tx.prior_pd1_pdl1", timing="12개월",
   quote="Had prior treatment with any other anti-PD-1, or PD-L1 or PD-L2 agent ... in the 12 months prior to randomization.",
   issue="12개월 기간 조건이 슬롯 값에 없음(언제 받았는지 모름). 기타 면역조절 수용체 표적 약제도 포함",
   suggestion="치료 시점 슬롯 추가 후 연결")
rv(T, "E13", "in_categorical", "일치", slot="tx.other_investigational_4w", op="in", values=["yes"], timing="4주",
   quote="Participation in another clinical study with an investigational agent or device during the 4 weeks prior to randomization.")
rv(T, "E16", "in_categorical", "일치", slot="repro.pregnant_or_breastfeeding", op="in", values=["yes"],
   applies_if=FEMALE, quote="Pregnant or breastfeeding")

# ---------------------------------------------------------------- NCT06928389 (코호트 없음)
T = "NCT06928389"
rv(T, "I1", "excluded", "미검증", gt_use="excluded",
   quote="Be able and willing to provide written informed consent and to comply with all requirements",
   issue="동의서·순응 - 팀 결정 2")
rv(T, "I2", "in_rule_engine", "일치", slot="age", op=">=", values=["18"], timing="무작위배정 시점",
   quote="Age ≥ 18 years old and ≤ 75 years old at the time of randomization.", issue="원문 한 문장을 두 기준으로 분리 (정상)")
rv(T, "I3", "in_rule_engine", "일치", slot="age", op="<=", values=["75"], timing="무작위배정 시점",
   quote="Age ≥ 18 years old and ≤ 75 years old at the time of randomization.")
rv(T, "I4", "in_rule_engine", "일치", slot="ecog", op="<=", values=["1"],
   quote="Eastern Cooperative Oncology Group (ECOG) performance status of 0 or 1.")
rv(T, "I5", "expert", "일치", expert_slot="judge.life_expectancy", gt_use="needs_expert",
   quote="Expected life expectancy of at least 3 months.")
rv(T, "I8", "in_categorical", "일치", slot="tx.prior_platinum", op="in", values=["yes"],
   quote="Previously received systemic platinum-based chemotherapy and PD-1/L1 inhibitors.",
   issue="원문 한 문장을 I8(백금)·I9(PD-1/L1)로 분리 (정상). CSV의 PD-1/L1 행이 I8에 매칭돼 있었음")
rv(T, "I9", "in_categorical", "일치", slot="tx.prior_pd1_pdl1", op="in", values=PD1_L1,
   quote="Previously received systemic platinum-based chemotherapy and PD-1/L1 inhibitors.",
   issue="durvalumab 공고요법(항PD-L1)을 '받은 적 있음'으로 셈 - 임상 검수 필요")
rv(T, "E1", "in_categorical", "일치", slot="dx.histology", op="in", values=["mixed_sclc"], specimen="조직·세포",
   quote="Histologic or cytopathologic evidence of the presence of small cell lung carcinoma.",
   issue="순수 소세포암은 NSCLC 대상 집단 밖이라 값 목록에 없음")
rv(T, "E5", "in_categorical", "일치", slot="tx.prior_docetaxel", op="in", values=["yes"],
   quote="Previous treatment with docetaxel.")
for r_ in ("E8", "E9", "E10"):
    rv(T, r_, "expert", "일치", expert_slot="cm.cardiac", gt_use="needs_expert",
       quote="History of myocarditis, cardiomyopathy, and malignant arrhythmia.",
       issue="원문 한 문장을 세 기준으로 분리. CSV 인용은 E8에만 매칭 -> 같은 판단 슬롯으로 셋 모두 판단 기준")

# ---------------------------------------------------------------- NCT07027514 (Cohort A / B)
T = "NCT07027514"
rv(T, "I1", "in_rule_engine", "일치", slot="age", op=">=", values=["18"], quote="(구조화 항목 minimum_age=18 Years)")
rv(T, "I2", "in_rule_engine", "일치", slot="sex", op="==", values=["all"], quote="(구조화 항목 sex=ALL)")
rv(T, "I4", "in_categorical", "일치", slot="lab.tsh_normal", op="in", values=["yes"], specimen="혈청(TSH)",
   quote="Thyroid-stimulating hormone (TSH) within normal limits", issue="검사 시점 조건 없음(원문)")
rv(T, "I6", "in_rule_engine", "일치", slot="ecog", op="<=", values=["1"],
   quote="Eastern Cooperative Oncology Group performance status (ECOG PS) 0 to 1")
rv(T, "I7", "expert", "일치", expert_slot="judge.life_expectancy", gt_use="needs_expert",
   quote="Have a life expectancy > 12 weeks")
for r_, q in (("I9", "Heterosexually active women of childbearing potential (WOCBP) must agree to use highly effective methods of contraception"),
              ("I10", "Male participants who are not sterile must commit to the use of a reliable method of birth control or abstinence"),
              ("I14", "Capable of understanding and complying with protocol requirements"),
              ("I15", "Provides written informed consent for the study")):
    rv(T, r_, "excluded", "미검증", gt_use="excluded", quote=q, issue="동의·순응·피임 - 팀 결정 2")
rv(T, "I13", "excluded", "미검증", gt_use="excluded", quote="Willingness to provide fresh tumor biopsy specimens",
   issue="CSV는 other.tumor_tissue(판단 기준)로 분류했지만 원문은 생검 '의향' - 절차 동의로 보고 팀 결정 2 적용",
   suggestion="판단 기준(조직 적합성)과 절차 동의(생검 의향) 구분 - 임상 검수 필요", specimen="신선 종양 조직")
rv(T, "E2", "in_categorical", "일치", slot="tx.prior_pd1_pdl1", op="in", values=ICI_ANY, cohort_check="Cohort B 전용 (일치)",
   quote="Cohort B only: Prior therapy with an immune checkpoint inhibitor.")
rv(T, "E3", "in_categorical", "일치", slot="bm.driver_alteration", op="in", values=EGFR_ALK,
   quote="Have known EGFR or ALK mutations", issue="'known': 미검사(not_tested)는 제외 대상 아님", specimen="종양 조직·혈장")
rv(T, "E4", "in_categorical", "일치", slot="tx.prior_il12", op="in", values=["yes"],
   quote="Have had prior treatment with recombinant interleukin-12 (IL-12)")
rv(T, "E7", "in_categorical", "모호", gt_use="unbound", slot="med.live_vaccine_30d", timing="C1D1 전 (기간 없음)",
   quote="Have received live vaccines prior to C1D1",
   issue="원문에 기간이 없음(문자 그대로는 '언제든'). 슬롯은 30일 이내만 표현 -> '아니오'여도 통과를 확정할 수 없음",
   suggestion="전체 프로토콜에서 기간 확인 후 연결")
rv(T, "E9", "in_categorical", "일치", slot="repro.pregnant_or_breastfeeding", op="in", values=["yes"], applies_if=FEMALE,
   quote="Female participant who is pregnant or breastfeeding")
rv(T, "E10", "in_categorical", "일치", slot="cm.organ_transplant", op="in", values=["yes"],
   quote="Have a history of allogeneic tissue/solid organ transplant")
rv(T, "E14", "expert", "일치", expert_slot="cm.cardiac", gt_use="needs_expert",
   quote="Have congestive heart failure, active coronary artery disease, ... or clinically significant cardiac arrhythmias.")
rv(T, "E16", "in_categorical", "일치", slot="med.anticoagulant", op="in", values=["warfarin"],
   quote="Participants on coumadin (warfarin), due to potential for increased bleeding risk associated with surgery")
rv(T, "E20", "expert", "일치", expert_slot="judge.investigator_unsuitable", gt_use="needs_expert",
   quote="Have other systemic conditions or organ abnormalities that may interfere with the conduct of the study")
rv(T, "E21", "expert", "일치", expert_slot="judge.investigator_unsuitable", gt_use="needs_expert",
   quote="Have any acute or chronic psychiatric problems or substance abuse disorder that make the participant unsuitable")

# ---------------------------------------------------------------- NCT07098338 (Sub-study 1 / 2 / 3)
T = "NCT07098338"
rv(T, "I1", "in_rule_engine", "일치", slot="age", op=">=", values=["18"], timing="동의서 서명 시점",
   quote="Participant must be ≥ 18 years of age at the time of signing the ICF")
rv(T, "I2", "in_rule_engine", "일치", slot="ecog", op="<=", values=["1"], quote="WHO/ECOG performance status of 0 or 1")
rv(T, "I5", "expert", "일치", expert_slot="judge.life_expectancy", gt_use="needs_expert", quote="Life expectancy ≥ 12 weeks",
   issue="파싱본 unit=weeks만 있고 값 없음 - 판단 기준이라 판정 영향 없음")
rv(T, "I6", "expert", "일치", expert_slot="other.tumor_tissue", gt_use="needs_expert", specimen="종양 조직 (적합성)",
   quote="Provision of acceptable tumour tissue")
for r_, c in (("I7", "Sub-study 1"), ("I8", "Sub-study 2")):
    rv(T, r_, "in_categorical", "모호", gt_use="unbound", slot="dx.histology", specimen="조직·세포",
       cohort_check=f"{c} (원문은 Sub-study 1·2 공통 -> 코호트별 복제, 일치)",
       quote="Histologically or cytologically documented advanced or metastatic NSCLC",
       issue="'advanced'가 병기 몇 기부터인지 원문에 없음 (IIIB 이상?). 병기 슬롯 값과 대응 불확실",
       suggestion="병기 대응(IIIB/IIIC 포함 여부) 임상 검수 후 연결")
rv(T, "E1", "expert", "일치", expert_slot="judge.investigator_unsuitable", gt_use="needs_expert",
   quote="As judged by the investigator, any severe or uncontrolled systemic diseases ...")
rv(T, "E9", "in_categorical", "모호", gt_use="unbound", slot="cm.hiv",
   quote="Active infection, including TB and infections with HIV, HBV (verified by known positive HBsAg result), HCV.",
   issue="활동성 감염 전체(TB 포함)를 슬롯 두 개(HIV·간염)로 다 표현 못함", suggestion="활동성 감염 슬롯 추가", specimen="혈청 (HBsAg)")
rv(T, "E10", "expert", "일치", expert_slot="cm.cardiac", gt_use="needs_expert",
   quote="Uncontrolled or significant cardiac disease")
rv(T, "E19", "in_categorical", "일치", slot="med.live_vaccine_30d", op="in", values=["yes"], timing="첫 투여 전 30일",
   quote="Receipt of live, attenuated vaccine within 30 days prior to the first dose of study intervention.")

# ---------------------------------------------------------------- NCT07416058 (코호트 없음)
T = "NCT07416058"
rv(T, "I1", "in_rule_engine", "일치", slot="age", op=">=", values=["18"], quote="adult patients (aged 18-75 years)")
rv(T, "I2", "in_rule_engine", "일치", slot="age", op="<=", values=["75"], quote="adult patients (aged 18-75 years)")
rv(T, "I3", "in_categorical", "일치", slot="dx.stage", op="in", values=["IIIB", "IIIC", "IVA", "IVB"],
   quote="histologically confirmed locally advanced or metastatic NSCLC (AJCC 9th Edition, Stage IIIB-IVB)",
   issue="AJCC 9판 기준. 슬롯 값은 판 구분 없음 (표현 검토 참조)", specimen="조직")
rv(T, "I4", "in_categorical", "일치", slot="bm.driver_alteration", op="in",
   values=["egfr_ex19del_L858R", "alk", "ros1", "ret", "kras_g12c", "braf_v600", "her2_ex20ins"], specimen="종양 조직·혈장",
   quote="harbor confirmed actionable driver mutations ... EGFR (19del, L858R); ALK, ROS1, RET fusions; KRAS G12C; BRAF V600; and HER2 exon 20 insertions")
rv(T, "I6", "in_categorical", "일치", slot="tx.days_since_last_anticancer", op="in", values=["ge_14d"], timing="2주 휴약",
   quote="a 2-week washout period is required for prior TKI therapy or chemotherapy",
   issue="슬롯을 일수 구간(lt_14d / ge_14d)으로 표현")
rv(T, "I7", "in_categorical", "모호", gt_use="unbound", slot="tx.prior_pd1_pdl1", quote="Prior immunotherapy is not permitted.",
   issue="'면역치료' 전체(사이토카인·백신·세포치료 포함 가능) vs 슬롯은 면역관문억제제만", suggestion="면역치료 범위 임상 검수 후 연결")
rv(T, "I8", "in_categorical", "모호", gt_use="unbound", slot="bm.pdl1_tps", timing="검체 2년 이내",
   specimen="신선·보관 종양 조직, 중앙검사실, 22C3·SP263 클론",
   quote="PD-L1 tumor proportion score (TPS) ≥ 50%, as confirmed by central laboratory testing using the 22C3 or SP263 clone on fresh or archival tumor tissue (collected within 2 years)",
   issue="검체 종류·검사 기관·클론·검체 시점 조건이 슬롯에 없음", suggestion="검체 조건을 판단 기준 후보로 검수")
rv(T, "I9", "in_rule_engine", "일치", slot="ecog", op="<=", values=["1"],
   quote="Patients must have an Eastern Cooperative Oncology Group (ECOG) Performance Status of 0 or 1")
rv(T, "I11", "in_rule_engine", "일치", slot="sex", op="==", values=["all"], quote="(구조화 항목 sex=ALL)")
rv(T, "E5", "expert", "일치", expert_slot="cm.cardiac", gt_use="needs_expert",
   quote="New York Heart Association (NYHA) class III or IV heart failure",
   issue="CSV는 이 시험에서 categorical로 표시했지만 슬롯 cm.cardiac은 판단 슬롯(EXPERT_SLOTS) -> 판단 기준")
rv(T, "E12", "in_categorical", "일치", slot="proc.days_since_major_surgery", op="in", values=["lt_21d", "21_27d"],
   timing="첫 투여 전 4주", quote="Major surgery within 4 weeks before first dose of study treatment")
rv(T, "E14", "in_categorical", "모호", gt_use="unbound", slot="repro.pregnant_or_breastfeeding",
   quote="Pregnant or breastfeeding women.",
   issue="원문 한 문장을 임신(E14)·수유(E15)로 분리했지만 슬롯은 둘을 합침 -> 기준별로 판정 불가", suggestion="임신·수유 슬롯 분리")
rv(T, "E15", "in_categorical", "모호", gt_use="unbound", slot="repro.pregnant_or_breastfeeding",
   quote="Pregnant or breastfeeding women.", issue="E14와 같음")
rv(T, "E16", "expert", "일치", expert_slot="judge.investigator_unsuitable", gt_use="needs_expert",
   quote="Any other serious medical condition ... that, in the opinion of the investigator, would compromise ...")
rv(T, "E17", "expert", "일치", expert_slot="cm.cardiac", gt_use="needs_expert",
   quote="Clinically significant cardiovascular disease, including but not limited to:",
   issue="하위 항목(E4~E7)을 묶는 머리 문장. 'not limited to' -> 판단 기준 (선우 검수 큐 note와 같음)")

# ---------------------------------------------------------------- NCT07680790 (코호트 없음)
T = "NCT07680790"
rv(T, "I1", "in_categorical", "일치", slot="dx.histology", op="in", values=["adenocarcinoma", "nonsquamous_other"],
   specimen="조직·세포", quote="Participants must have histologically or cytologically confirmed non-squamous NSCLC",
   issue="NSCLC-NOS를 비편평으로 볼지 불확실 -> 슬롯 값에서 NOS를 뺌 (표현 검토 참조)")
rv(T, "I2", "expert", "일치", expert_slot="dx.disease_setting", gt_use="needs_expert",
   quote="newly diagnosed locally advanced (Stage IIIB/IIIC), metastatic (Stage IVA/IVB), or recurrent disease not amenable to curative surgery or definitive radiotherapy and requiring systemic treatment",
   issue="'근치 수술·방사선 불가' 판단 포함 -> 판단 기준 (CSV dx.disease_setting=expert)")
rv(T, "I3", "in_categorical", "일치", slot="bm.driver_alteration", op="in", values=["egfr_ex19del_L858R"], specimen="종양 조직·혈장",
   quote="Participants must have documented EGFR-TKI-sensitizing mutation (exon 19 deletion or exon 21 L858R substitution)")
rv(T, "I5", "in_rule_engine", "일치", slot="ecog", op="<=", values=["1"], quote="Participants must have ECOG Performance Status 0-1")
rv(T, "I6", "in_rule_engine", "일치", slot="age", op=">=", values=["18"], quote="(구조화 항목 minimum_age=18 Years)")
rv(T, "I7", "in_rule_engine", "일치", slot="sex", op="==", values=["all"], quote="(구조화 항목 sex=ALL)")
rv(T, "E3", "expert", "일치", expert_slot="cm.cardiac", gt_use="needs_expert",
   quote="Participants must not have clinically significant cardiac disease")
rv(T, "E4", "excluded", "미검증", gt_use="excluded", quote="Other protocol-defined inclusion/exclusion criteria apply.",
   issue="평가 가능한 내용 없음. ClinicalTrials.gov 기준이 전체 프로토콜의 요약임을 뜻함 -> 문서에 한계로 명시")

SCOPE_IN = ("in_rule_engine", "in_categorical", "in_lab", "expert")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def parsed_value(r: dict) -> str:
    if r["field"]:
        v = f'{r["field"]} {r["operator"]} {r["value"]}' + (f' {r["unit"]}' if r["unit"] else "")
    else:
        v = "(구조화 없음 - LLM 판정 대상)"
    if r["time_window_days"] is not None:
        v += f' | 기간 {r["time_window_days"]}일'
    return v


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    csv_rows = list(csv.DictReader(open(NSCLC / "cohort" / "slot_candidates.csv", encoding="utf-8-sig")))
    queue = {}
    qpath = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "dev" / "expert_rules_review_queue_2026_11_07.csv"
    if qpath.exists():  # 선우 검수 큐 (원격 feat/p-common-engine 등에만 있음. 없으면 빈 값)
        queue = {r["key"]: r for r in csv.DictReader(open(qpath, encoding="utf-8-sig"))}
    rows, status, expert_rows, overrides = [], [], [], []
    for t in TRIALS_ALL:
        trial = json.loads((NSCLC / "trials" / f"{t}.json").read_text(encoding="utf-8"))
        ppath = NSCLC / "parsed" / f"{t}.json"
        if not ppath.exists():
            status.append({"trial_id": t, "parsed": "파싱 미완료 - API 크레딧, 분할 파서 반영됨", "criteria": "",
                           "in_scope": "", "data_verified": "", "mismatch": "", "ambiguous": "", "expert": "",
                           "region_condition": "미검증", "cohorts": "", "clinical_review": CLINICAL,
                           "note": "평가셋 제외. 원문에 검사 수치 기준(ANC·빌리루빈·CrCl·Hb)이 있는 두 시험"})
            continue
        rec = json.loads(ppath.read_text(encoding="utf-8"))
        meta = rec.get("rule_meta") or {}
        n = dict(in_scope=0, verified=0, mismatch=0, ambiguous=0, expert=0, region=0)
        for r in rec["parsed"]["rules"]:
            key = (t, r["rule_id"])
            rvw = R.get(key) or dict(scope="out_of_scope", result="미검증", slot=None, op=None, values=[],
                                     applies_if=None, gt_use="out_of_scope", issue="축소 범위 밖 (원문 대조 안 함)",
                                     suggestion="", expert_slot=None, timing="", specimen="", region="",
                                     cohort_check="", quote="")
            csv_slots = [x["slot_id"] for x in csv_rows if x["trial_id"] == t
                         and (norm(x["quote"])[:60] in norm(r["text"]) or norm(r["text"])[:60] in norm(x["quote"]))]
            gt_use = rvw["gt_use"] or "bindable"
            expert = rvw["scope"] == "expert" or bool(rvw["slot"] and is_expert_slot(rvw["slot"]))
            q = queue.get(f"{t}:{r['rule_id']}")
            rows.append({
                "trial_id": t, "criterion_id": r["rule_id"], "kind": r["kind"], "cohort": r["cohort"] or "",
                "original_text": rvw["quote"], "parsed_text": r["text"], "parsed_value": parsed_value(r),
                "parser_underspecified": str(r["underspecified"]).lower(),
                "required_evidence": (meta.get(r["rule_id"]) or {}).get("required_evidence", ""),
                "result": rvw["result"], "issue": rvw["issue"], "suggestion": rvw["suggestion"],
                "timing": rvw["timing"], "specimen": rvw["specimen"], "region": rvw["region"],
                "cohort_check": rvw["cohort_check"], "scope": rvw["scope"],
                "in_scope": "yes" if rvw["scope"] in SCOPE_IN else "no",
                "expert": "yes" if expert else "no", "expert_slot": rvw["expert_slot"] or "",
                "gt_use": gt_use, "slot": rvw["slot"] or "", "op": rvw["op"] or "", "values": "|".join(rvw["values"]),
                "applies_if": "" if not rvw["applies_if"] else f'{rvw["applies_if"]["slot"]} in {"|".join(rvw["applies_if"]["values"])}',
                "csv_slots": "|".join(dict.fromkeys(csv_slots)),
                "sunwoo_queue": f'{q["signal"]}/{q["review_status"]}' if q else "",
                "verifier": VERIFIER if rvw["scope"] in SCOPE_IN or rvw["scope"] == "excluded" else "",
                "clinical_review": CLINICAL,
            })
            if rvw["scope"] in SCOPE_IN:
                n["in_scope"] += 1
                n["verified"] += rvw["result"] in ("일치", "불일치", "모호")
                n["mismatch"] += rvw["result"] == "불일치"
                n["ambiguous"] += rvw["result"] == "모호"
                n["expert"] += expert
            n["region"] += bool(rvw["region"])
            if rvw["result"] == "불일치":
                overrides.append({"trial_id": t, "criterion_id": r["rule_id"], "kind": "region_conditional",
                                  "parsed": parsed_value(r), "proposed": {"applies_if": {"country": ["USA"]},
                                                                           "otherwise": "not_applicable"},
                                  "gt_reading": "age>=22면 통과, 아니면 국가를 몰라 판정하지 않음(NOT_EVALUATED)",
                                  "reason": rvw["issue"], "applied_to": ["gt"],
                                  "not_applied_to": ["data/nsclc/parsed (원본 보존)", "평가용 시험 저장소 (엔진 입력은 파싱본 그대로)"],
                                  "verifier": VERIFIER, "clinical_review": CLINICAL})
            if expert:
                expert_rows.append({"trial_id": t, "criterion_id": r["rule_id"], "expert_slot": rvw["expert_slot"] or rvw["slot"],
                                    "original_text": rvw["quote"], "parser_underspecified": str(r["underspecified"]).lower(),
                                    "engine_route_without_flag": "physician_review" if r["underspecified"] else "missing -> 확보 시도 (BLOCKED 가능, #20 리뷰 2)",
                                    "eval_store_underspecified": "true", "sunwoo_queue": f'{q["signal"]}/{q["review_status"]}' if q else "",
                                    "specimen_or_timing_candidate": "", "clinical_review": CLINICAL})
        status.append({"trial_id": t, "parsed": "파싱 완료", "criteria": len(rec["parsed"]["rules"]),
                       "in_scope": n["in_scope"], "data_verified": n["verified"], "mismatch": n["mismatch"],
                       "ambiguous": n["ambiguous"], "expert": n["expert"],
                       "region_condition": "있음" if n["region"] else "없음",
                       "cohorts": "|".join(rec["parsed"]["cohorts"]), "clinical_review": CLINICAL, "note": ""})
    # 검체·시점 요구조건은 판단 기준 후보로 따로 표시 (범위 안 기준 중)
    for r in rows:
        if r["in_scope"] == "yes" and r["expert"] == "no" and (r["specimen"] or r["timing"]):
            expert_rows.append({"trial_id": r["trial_id"], "criterion_id": r["criterion_id"], "expert_slot": r["slot"],
                                "original_text": r["original_text"], "parser_underspecified": r["parser_underspecified"],
                                "engine_route_without_flag": "", "eval_store_underspecified": "false",
                                "sunwoo_queue": r["sunwoo_queue"],
                                "specimen_or_timing_candidate": "; ".join(x for x in (r["specimen"] and f"검체: {r['specimen']}",
                                                                                     r["timing"] and f"시점: {r['timing']}") if x),
                                "clinical_review": CLINICAL})
    _write(OUT / "criteria_verification.csv", rows)
    _write(OUT / "protocol_status.csv", status)
    _write(OUT / "expert_criteria.csv", expert_rows)
    (OUT / "overrides.json").write_text(json.dumps({
        "version": 1, "note": "파싱 수정 제안. 원본(data/nsclc/parsed)은 바꾸지 않음. applied_to에 적힌 곳에서만 씀",
        "verifier": VERIFIER, "clinical_review": CLINICAL, "overrides": overrides}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    print(f"criteria={len(rows)} in_scope={sum(r['in_scope'] == 'yes' for r in rows)} "
          f"overrides={len(overrides)} expert_rows={len(expert_rows)}")
    return 0


def _write(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
