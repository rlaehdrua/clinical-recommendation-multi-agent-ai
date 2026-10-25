"""P6 우선순위 기준의 Condition IR·질문 계약 초안 생성 (검수 전 초안, 승인 기록을 만들지 않음).

    python scripts/build_contract_drafts.py           # contracts/ 아래 초안 파일을 다시 씀
    python scripts/build_contract_drafts.py --check   # 파일이 이 스크립트의 결과와 같은지만 확인 (쓰지 않음)

입력: data/parsed/ (잠긴 기준표, 읽기만). 출력: contracts/ir/<trial_id>.json, contracts/question_contracts.json,
contracts/ir_cases.json (기준별 긍정·부정·미확인 검증 사례와 질문 계약 후보).
모든 초안의 review_status는 "draft"이며, 활성화는 사람 검수 기록(contracts/reviews.json, ir_review.Registry.record_review)
으로만 됩니다. 이 스크립트는 reviews.json을 만들거나 바꾸지 않습니다.

작성 원칙
- 원문의 기간·기준 시점·한정어를 생략하지 않음. 표현하지 못한 부분은 unsupported 노드나 unsupported_parts로 남기고
  completeness=partial (평가기는 partial IR로 확정하지 않음)
- 기준 시점(first_dose, randomization, enrollment, screening)을 평가 시점으로 근사할지(anchor_policy)는 넣지 않음:
  팀 결정(P0 K-2) 후 검수자가 기준별로 추가해야 함. 그 전에는 기간 기준은 승인되어도 결정되지 않음
- 개월 -> 일 환산은 잠긴 기준표의 time_window_days 값을 그대로 씀 (검수 확인 사항으로 notes에 기록)
- 근거 수준: 잠긴 기준표의 required_evidence보다 엄격하게만 바꿈 (예: 'documented', 활동성 감염 -> clinical_record)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ctrec.condition_ir import CriterionIR  # noqa: E402
from ctrec.propositions import Proposition  # noqa: E402
from ctrec.question_contract import QuestionContract  # noqa: E402

PARSED = ROOT / "data" / "parsed"
OUT = ROOT / "contracts"
AUTHOR = "draft-generator (P6, 사람 검수 전)"


def A(key):
    return {"op": "atom", "prop": key}


def OR(*a):
    return {"op": "or", "args": list(a)}


def AND(*a):
    return {"op": "and", "args": list(a)}


def NOT(a):
    return {"op": "not", "arg": a}


def U(text, reason):
    return {"op": "unsupported", "text": text, "reason": reason}


PREG_NOW, LACT_NOW = "pregnancy/condition/current", "lactation/condition/current"
PREG_OR_LACT = OR(A(PREG_NOW), A(LACT_NOW))
PREG_CASES = [({PREG_NOW: True}, "yes"), ({LACT_NOW: True}, "yes"), ({PREG_NOW: False, LACT_NOW: False}, "no"),
              ({PREG_NOW: False}, "unknown"), ({}, "unknown")]


def ever_cases(key, broader_false=None):
    c = [({key: True}, "yes"), ({key: False}, "no"), ({}, "unknown")]
    if broader_false:
        c.append(({broader_false: False}, "no"))
    return c


def drug_none(drug, days, anchor="enrollment"):
    return NOT(A(f"{drug}/event/w{days}d-{anchor}"))


def drug_none_cases(drug, days):
    k = f"{drug}/event/w{days}d-assessment"
    return [({k: False}, "yes"), ({k: True}, "no"), ({f"{drug}/event/ever": False}, "yes"),
            ({f"{drug}/event/ever": True}, "unknown"), ({}, "unknown")]


MONTHS = "개월 -> 일 환산은 잠긴 기준표 time_window_days 값 사용 (검수 확인)"
ANCHOR = "기준 시점({a})을 평가 시점으로 근사할지 검수 결정 필요 (anchor_policy 미설정 -> 승인돼도 미확정)"

# (trial, rule, condition, completeness, source_requirement, span, unsupported_parts, notes, contract_candidates, cases)
SPEC = [
    ("DEMO-LUNG-001", "E2", A("egfr_adc/event/ever"), "complete", None, None, [], "",
     ["QC-EGFR-ADC-EVER"], ever_cases("egfr_adc/event/ever")),
    ("DEMO-LUNG-001", "E3", A("interstitial_lung_disease_steroid/condition/ever"), "complete", None, None, [],
     "'스테로이드 치료가 필요했던 ILD'를 한 개념으로 둠. 스테로이드가 필요 없던 ILD 이력은 이 명제가 아님",
     ["QC-ILD-STEROID-EVER"], ever_cases("interstitial_lung_disease_steroid/condition/ever")),
    ("DEMO-LUNG-001", "E4", PREG_OR_LACT, "complete", None, None, [], "", ["QC-PREGNANT-NOW", "QC-LACTATING-NOW"],
     PREG_CASES),
    ("DEMO-LUNG-001", "E5", OR(A("active_hepatitis_b/condition/current"), A("active_hepatitis_c/condition/current")),
     "complete", "clinical_record", None, [], "활동성 감염 여부는 혈액검사 확인이 필요해 근거 수준을 clinical_record로 올림",
     [], [({"active_hepatitis_b/condition/current": False, "active_hepatitis_c/condition/current": False}, "no"),
          ({"active_hepatitis_c/condition/current": True}, "yes"), ({}, "unknown")]),
    ("NCT07087314", "E15", A("surgery(major.non_diagnostic)/event/w28d-first_dose"), "complete", None, None, [],
     "'major' 정의가 원문에 없음: 환자 답 '수술 없음'만 결정, '수술 있음'은 큰 수술 여부를 몰라 미확인. "
     + ANCHOR.format(a="first_dose"), ["QC-SURGERY-LAST"],
     [({"surgery/event/ever": False}, "no"), ({"surgery/event/w28d-assessment": False}, "no"),
      ({"surgery/event/w28d-assessment": True}, "unknown"), ({"surgery/event/ever": True}, "unknown"),
      ({"surgery(major.non_diagnostic)/event/w28d-assessment": True}, "yes"), ({}, "unknown")]),
    ("NCT07087314", "E13", A("anti_cd38_therapy/event/ever"), "complete", None, None, [], "",
     ["QC-ANTICD38-EVER"], ever_cases("anti_cd38_therapy/event/ever")),
    ("NCT07087314", "E14", A("live_vaccine/event/w28d-first_dose"), "complete", None, None, [],
     ANCHOR.format(a="first_dose"), ["QC-LIVE-VACCINE-LAST"],
     [({"live_vaccine/event/w28d-assessment": True}, "yes"), ({"live_vaccine/event/w28d-assessment": False}, "no"),
      ({"vaccine/event/w28d-assessment": False}, "no"), ({"vaccine/event/w28d-assessment": True}, "unknown"),
      ({}, "unknown")]),
    ("NCT07087314", "I11", NOT(OR(*[A(f"{d}/event/w60d-enrollment") for d in
                                   ("tacrolimus", "cyclosporine", "mycophenolate_mofetil", "belimumab", "levamisole",
                                    "azathioprine", "cyclophosphamide")])), "complete", None, None, [],
     "'in prior 2 months'의 기준 시점이 원문에 없어 enrollment로 둠(검수 확인). " + MONTHS,
     [f"QC-{d}-LAST" for d in ("TACROLIMUS", "CYCLOSPORINE", "MYCOPHENOLATE-MOFETIL", "BELIMUMAB", "LEVAMISOLE",
                               "AZATHIOPRINE", "CYCLOPHOSPHAMIDE")],
     [({f"{d}/event/w60d-assessment": False for d in ("tacrolimus", "cyclosporine", "mycophenolate_mofetil",
                                                      "belimumab", "levamisole", "azathioprine", "cyclophosphamide")},
       "yes"),
      ({"azathioprine/event/w60d-assessment": True}, "no"),
      ({f"{d}/event/w60d-assessment": False for d in ("tacrolimus", "cyclosporine", "mycophenolate_mofetil",
                                                      "belimumab", "levamisole", "azathioprine")}, "unknown")]),
    ("NCT07087314", "I12", NOT(OR(A("rituximab/event/w180d-enrollment"), A("obinutuzumab/event/w180d-enrollment"))),
     "complete", None, None, [], "기준 시점 enrollment로 둠(원문 'prior 6 months'). " + MONTHS,
     ["QC-ANTICD20-LAST", "QC-RITUXIMAB-LAST", "QC-OBINUTUZUMAB-LAST"],
     [({"anti_cd20_antibody/event/w180d-assessment": False}, "yes"), ({"rituximab/event/w180d-assessment": True}, "no"),
      ({"rituximab/event/w180d-assessment": False}, "unknown"),
      ({"rituximab/event/w180d-assessment": False, "obinutuzumab/event/w180d-assessment": False}, "yes")]),
    ("NCT07087314", "E11", A("primary_immunodeficiency/condition/ever"), "complete", None, None, [], "",
     ["QC-PRIMARY-IMMUNODEFICIENCY-EVER"], ever_cases("primary_immunodeficiency/condition/ever")),
    ("NCT07087314", "E1", A("nephrotic_family_history/condition/ever"), "complete", None, None, [], "",
     ["QC-NEPHROTIC-FAMILY-HISTORY"], ever_cases("nephrotic_family_history/condition/ever")),
    ("NCT06391359", "E5", A("cholecystectomy/event/ever"), "complete", None, None, [], "",
     ["QC-CHOLECYSTECTOMY-EVER", "QC-SURGERY-LAST"], ever_cases("cholecystectomy/event/ever", "surgery/event/ever")),
    ("NCT06727604", "E1", OR(A("radioactive_iodine_therapy(successful)/event/ever"), A("total_thyroidectomy/event/ever")),
     "partial", None, None, ["successfully treated: 방사성 요오드 치료의 '성공' 여부는 의료진 판단"], "",
     ["QC-THYROIDECTOMY-EVER"],
     [({"total_thyroidectomy/event/ever": True}, "yes"),
      ({"radioactive_iodine_therapy/event/ever": False, "total_thyroidectomy/event/ever": False}, "no")]),
    ("NCT06294847", "E1", A("vitrectomy(for_detached_retina)/event/ever"), "complete", None, None, [], "",
     ["QC-VITRECTOMY-RD-EVER"],
     [({"vitrectomy(for_detached_retina)/event/ever": True}, "yes"), ({"vitrectomy/event/ever": False}, "no"),
      ({"vitrectomy/event/ever": True}, "unknown"), ({}, "unknown")]),
    ("NCT06294847", "E5", PREG_OR_LACT, "complete", None, None, [], "", ["QC-PREGNANT-NOW", "QC-LACTATING-NOW"],
     PREG_CASES),
    ("NCT06080789", "E4", OR(A("pancreatic_necrosis/condition/ever"), A("pancreatic_necrosectomy/event/ever")), "complete", None,
     None, [], "원문 'including necrosectomy'는 췌장 괴사의 제거술만 뜻함 (다른 부위 괴사 조직 제거는 해당 없음)",
     ["QC-PANCREATIC-NECROSIS-EVER", "QC-PANCREATIC-NECROSECTOMY-EVER"],
     [({"pancreatic_necrosis/condition/ever": True}, "yes"), ({"pancreatic_necrosectomy/event/ever": True}, "yes"),
      ({"pancreatic_necrosis/condition/ever": False, "pancreatic_necrosectomy/event/ever": False}, "no"),
      ({"pancreatic_necrosis/condition/ever": False}, "unknown")]),
    ("NCT06833073", "E4", A("myocardial_infarction/condition/w180d-randomization"), "complete", None, None, [],
     ANCHOR.format(a="randomization") + " " + MONTHS, ["QC-MI-LAST"],
     [({"myocardial_infarction/condition/w180d-assessment": True}, "yes"),
      ({"myocardial_infarction/condition/ever": False}, "no"),
      ({"myocardial_infarction/condition/ever": True}, "unknown"), ({}, "unknown")]),
    ("NCT06833073", "E5", OR(A("systemic_anticancer_therapy/event/w28d-randomization"),
                             U("including investigational agents", "항암 목적 임상시험약인지 환자 답으로 구분하지 않음")),
     "partial", None, None, ["investigational agents의 범위"], ANCHOR.format(a="randomization"),
     ["QC-ANTICANCER-LAST"],
     [({"systemic_anticancer_therapy/event/w28d-assessment": True}, "yes"),
      ({"systemic_anticancer_therapy/event/w28d-assessment": False}, "unknown")]),
    ("NCT06833073", "E6", A("cancer_vaccine/event/ever"), "complete", None, None, [], "", ["QC-CANCER-VACCINE-EVER"],
     ever_cases("cancer_vaccine/event/ever")),
    ("NCT06833073", "E10", A("active_tuberculosis/condition/current"), "complete", "clinical_record", None, [],
     "활동성 결핵은 검사·진료 기록 확인이 필요해 clinical_record. 코호트 A 적용은 Rule.cohort로 처리", [],
     ever_cases("active_tuberculosis/condition/current")),
    ("NCT06833073", "E11", A("hiv_infection/condition/ever"), "complete", None, None, [],
     "'known history of HIV infection' = 진단받은 적 있음. 코호트 A 적용은 Rule.cohort로 처리", ["QC-HIV-EVER"],
     ever_cases("hiv_infection/condition/ever")),
    ("NCT06833073", "E12", AND(A("hiv_infection/condition/ever"), A("kaposi_or_castleman/condition/ever")),
     "complete", None, None, [], "코호트 B 전용", ["QC-HIV-EVER", "QC-KAPOSI-CASTLEMAN-EVER"],
     [({"hiv_infection/condition/ever": True, "kaposi_or_castleman/condition/ever": True}, "yes"),
      ({"hiv_infection/condition/ever": False}, "no"), ({"kaposi_or_castleman/condition/ever": False}, "no"),
      ({"hiv_infection/condition/ever": True}, "unknown")]),
    ("NCT06916494", "I3", NOT(OR(A("cancer_immunotherapy/event/ever"), A("targeted_anticancer_therapy/event/ever"))),
     "partial", None, None, ["systemic immunotherapy가 항암 면역요법만인지(다른 면역요법 포함 여부)",
                             "target therapy의 범위"], "", ["QC-CANCER-IMMUNOTHERAPY-EVER", "QC-TARGETED-THERAPY-EVER"],
     [({"cancer_immunotherapy/event/ever": False, "targeted_anticancer_therapy/event/ever": False}, "yes"),
      ({"cancer_immunotherapy/event/ever": True}, "no")]),
    ("NCT04063540", "E9", PREG_OR_LACT, "complete", None, None, [], "", ["QC-PREGNANT-NOW", "QC-LACTATING-NOW"],
     PREG_CASES),
    ("NCT04761523", "E6", PREG_OR_LACT, "complete", None, None, [], "'nursing' = 수유", ["QC-PREGNANT-NOW",
                                                                                        "QC-LACTATING-NOW"],
     PREG_CASES),
    ("NCT06756191", "E13", PREG_OR_LACT, "complete", None, None, [], "", ["QC-PREGNANT-NOW", "QC-LACTATING-NOW"],
     PREG_CASES),
    ("NCT07369063", "E9", A(PREG_NOW), "complete", None, None, [], "", ["QC-PREGNANT-NOW"],
     [({PREG_NOW: True}, "yes"), ({PREG_NOW: False}, "no"), ({"pregnancy/condition/ever": True}, "unknown"),
      ({"pregnancy/condition/ever": False}, "no"), ({}, "unknown")]),
    ("NCT00575185", "E6", OR(A(LACT_NOW), U("during the study", "연구 기간 중 수유 계획은 현재 사실이 아님")), "partial", None,
     None, ["연구 기간 중(미래) 수유"], "", ["QC-LACTATING-NOW"],
     [({LACT_NOW: True}, "yes"), ({LACT_NOW: False}, "unknown")]),
    ("NCT06468397", "E5", PREG_OR_LACT, "partial", None, None, ["to be confirmed on treatment day: 치료일 확인 절차"],
     "", ["QC-PREGNANT-NOW", "QC-LACTATING-NOW"], [({PREG_NOW: True}, "yes"), ({PREG_NOW: False, LACT_NOW: False}, "no")]),
    ("NCT06468397", "E21", A("retinal_detachment_family_history/condition/ever"), "partial", None, None,
     ["'Fellow eye:' 표지와 가족력의 관계가 원문상 불분명"], "", ["QC-RETINAL-DETACHMENT-FAMILY-HISTORY"],
     ever_cases("retinal_detachment_family_history/condition/ever")),
    ("NCT06756191", "E1", AND(A("pulmonary_mucormycosis/condition/ever"), A("amphotericin_b(over_5d)/event/ever"),
                              U("who have previously been treated with amphotericin B",
                                "암포테리신 B 투여가 폐 털곰팡이증 치료(같은 에피소드)였어야 하는지 원문에 없음")),
     "partial", None, None, ["암포테리신 B 투여와 폐 털곰팡이증의 관계(치료 목적·에피소드)"],
     "P8: 'for a duration exceeding 5 days'를 한정어 over_5d로 표현(원문 그대로 질문, 누적·1회 구분은 임상 검토). "
     "남은 unsupported 때문에 부분 IR(결정적 판정에 쓰지 않음, 계산 값은 provisional 기록). 임상 검토에서 투여 목적 "
     "조건이 필요 없다고 정하면 unsupported 노드를 빼고 complete로 바꿀 수 있음",
     ["QC-MUCORMYCOSIS-EVER", "QC-AMPHOTERICIN-COURSE"],
     [({"pulmonary_mucormycosis/condition/ever": False}, "no"),
      ({"pulmonary_mucormycosis/condition/ever": True, "amphotericin_b/event/ever": False}, "no"),
      ({"pulmonary_mucormycosis/condition/ever": True, "amphotericin_b(over_5d)/event/ever": False}, "no"),
      ({"pulmonary_mucormycosis/condition/ever": True, "amphotericin_b(over_5d)/event/ever": True}, "unknown")]),
    ("NCT06756191", "E10", A("myocardial_infarction/condition/ever"), "complete", None, None, [], "", ["QC-MI-LAST"],
     ever_cases("myocardial_infarction/condition/ever")),
    ("NCT06756191", "E11", A("cerebral_infarction/condition/ever"), "complete", None, None, [], "",
     ["QC-CEREBRAL-INFARCTION-EVER"], ever_cases("cerebral_infarction/condition/ever")),
    ("NCT06756191", "E12", A("ecmo/event/current"), "complete", None, None, [], "", ["QC-ECMO-NOW"],
     [({"ecmo/event/current": True}, "yes"), ({"ecmo/event/current": False}, "no"), ({"ecmo/event/ever": False}, "no"),
      ({"ecmo/event/ever": True}, "unknown")]),
    ("NCT06908096", "E1", OR(A(PREG_NOW), A(LACT_NOW),
                             U("planning to become pregnant while participating through 60 days after the last dose",
                               "향후 임신 계획은 사실이 아닌 의향")), "partial", None, None, ["임신 계획(미래)"], "",
     ["QC-PREGNANT-NOW", "QC-LACTATING-NOW"], [({PREG_NOW: True}, "yes"), ({PREG_NOW: False, LACT_NOW: False}, "unknown")]),
    ("NCT06908096", "E3", U("Immunomodulating therapy within 180 days", "면역조절 치료의 범위(약물 목록)가 원문에 없음"),
     "partial", None, None, ["면역조절 치료의 정의"], "", [], [({}, "unknown")]),
    ("NCT06908096", "E4", A("blood_product/event/w120d-first_dose"), "complete", None, None, [],
     "면역글로불린 제제를 blood_product 개념에 포함. " + ANCHOR.format(a="first_dose"), ["QC-BLOOD-PRODUCT-LAST"],
     [({"blood_product/event/w120d-assessment": True}, "yes"), ({"blood_product/event/w120d-assessment": False}, "no"),
      ({"blood_product/event/ever": True}, "unknown")]),
    ("NCT06908096", "E5", OR(A("investigational_agent/event/w30d-first_dose"),
                             U("planning to receive investigational products while on study", "향후 계획")),
     "partial", None, None, ["연구 중 임상시험약 투여 계획"], ANCHOR.format(a="first_dose"), ["QC-INVESTIGATIONAL-LAST"],
     [({"investigational_agent/event/w30d-assessment": True}, "yes"),
      ({"investigational_agent/event/w30d-assessment": False}, "unknown")]),
    ("NCT06908096", "E7", A("ebv_vaccine_trial_participation/event/ever"), "complete", None, None, [], "",
     ["QC-EBV-VACCINE-TRIAL-EVER"], ever_cases("ebv_vaccine_trial_participation/event/ever",
                                               "clinical_trial_participation/event/ever")),
    ("NCT06908096", "E8", A("febrile_illness/condition/w14d-first_dose"), "complete", None, None, [],
     ANCHOR.format(a="first_dose"), ["QC-FEBRILE-ILLNESS-LAST"],
     [({"febrile_illness/condition/w14d-assessment": True}, "yes"),
      ({"febrile_illness/condition/w14d-assessment": False}, "no"), ({}, "unknown")]),
    ("NCT06908096", "E11", A("angioedema/condition/ever"), "complete", None, None, [],
     "유전성·후천성·특발성 = 모든 형태의 혈관부종으로 해석 (검수 확인)", ["QC-ANGIOEDEMA-EVER"],
     ever_cases("angioedema/condition/ever")),
    ("NCT06908096", "E12", A("idiopathic_urticaria/condition/w365d-screening"), "complete", None, None, [],
     "'within the past year'의 기준 시점을 screening으로 둠. " + ANCHOR.format(a="screening"), ["QC-URTICARIA-LAST"],
     [({"idiopathic_urticaria/condition/w365d-assessment": True}, "yes"),
      ({"idiopathic_urticaria/condition/ever": False}, "no"), ({"idiopathic_urticaria/condition/ever": True}, "unknown")]),
    ("NCT06908096", "E14", OR(A("diabetes_type1/condition/ever"), A("diabetes_type2/condition/ever")), "complete", None,
     None, [], "임신성 당뇨병은 별개 개념이라 제외 조건이 개념 구분으로 표현됨", ["QC-DIABETES-TYPE"],
     [({"diabetes_type2/condition/ever": True}, "yes"),
      ({"diabetes_type1/condition/ever": False, "diabetes_type2/condition/ever": False}, "no"),
      ({"diabetes_gestational/condition/ever": True}, "unknown")]),
    ("NCT06908096", "E22", A("asplenia/condition/current"), "complete", None, None, [],
     "해부학적·기능적 무비증과 비장 제거를 한 개념(asplenia)으로 둠", ["QC-ASPLENIA-NOW"],
     ever_cases("asplenia/condition/current")),
    ("NCT06908096", "E23", A("guillain_barre_syndrome/condition/ever"), "complete", None, None, [], "", ["QC-GBS-EVER"],
     ever_cases("guillain_barre_syndrome/condition/ever")),
    ("NCT06908096", "E25", A("hiv_infection/condition/ever"), "complete", None, None, [],
     "HIV 감염은 완치되지 않으므로 진단 이력 = 감염으로 해석 (검수 확인)", ["QC-HIV-EVER"],
     ever_cases("hiv_infection/condition/ever")),
    ("NCT07516951", "E1", A("coeliac_disease/condition/ever"), "complete", "clinical_record", None, [],
     "'documented diagnosis' -> clinical_record", ["QC-COELIAC-EVER"], ever_cases("coeliac_disease/condition/ever")),
    ("NCT07516951", "E2", A("lower_respiratory_tract_infection/condition/w28d-screening"), "complete",
     "clinical_record", None, [], "'Documented' -> clinical_record. " + ANCHOR.format(a="screening"), [],
     [({"lower_respiratory_tract_infection/condition/w28d-assessment": False}, "no")]),
    *[("NCT06125405", rid, drug_none(drug, days), "complete", None, None, [],
       MONTHS + ". " + ANCHOR.format(a="enrollment"),
       [f"QC-{drug.upper().replace('_', '-')}-LAST"], drug_none_cases(drug, days))
      for rid, drug, days in (("I7", "rituximab", 180), ("I8", "tacrolimus", 90), ("I9", "mycophenolate_mofetil", 90),
                              ("I10", "cyclosporine", 90), ("I11", "cyclophosphamide", 90), ("I12", "acth", 90))],
    ("NCT06125405", "E1", A("nephrotic_family_history/condition/ever"), "complete", None, None, [], "",
     ["QC-NEPHROTIC-FAMILY-HISTORY"], ever_cases("nephrotic_family_history/condition/ever")),
    ("NCT06125405", "E13", A("clinical_trial_participation/event/w90d-enrollment"), "complete", None, None, [],
     MONTHS + ". " + ANCHOR.format(a="enrollment"),
     ["QC-TRIAL-PARTICIPATION-LAST"],
     [({"clinical_trial_participation/event/w90d-assessment": True}, "yes"),
      ({"clinical_trial_participation/event/ever": False}, "no"),
      ({"ebv_vaccine_trial_participation/event/w90d-assessment": True}, "yes")]),
    ("NCT07679893", "E25", OR(A("smoking/event/current"), A("smoking/event/w365d-screening")), "partial", None, None,
     ["'recently smoked'의 기간 정의 없음"], ANCHOR.format(a="screening"), ["QC-SMOKING-LAST"],
     [({"smoking/event/current": True}, "yes"), ({"smoking/event/ever": False}, "no")]),
]


# ------------------------------------------------------------------ 질문 계약 초안
def _days_ko(d: int) -> str:
    return {14: "2주(14일)", 28: "4주(28일)", 30: "30일", 60: "2개월(60일)", 90: "3개월(90일)", 120: "4개월(120일)",
            180: "6개월(180일)", 365: "1년(365일)"}[d]


def last_contract(cid, concept, question, bins, never_label):
    aspect = "event" if Proposition.parse(f"{concept}/event/ever" if _is_event(concept) else
                                          f"{concept}/condition/ever").aspect == "event" else "condition"
    k = lambda d: f"{concept}/{aspect}/w{d}d-assessment"  # noqa: E731
    ever = f"{concept}/{aspect}/ever"
    opts = [{"label": f"최근 {_days_ko(bins[0])} 이내", "assertions": [[k(bins[0]), True]]}]
    for a, b in zip(bins, bins[1:]):
        opts.append({"label": f"{_days_ko(a)}보다 전, {_days_ko(b)} 이내", "assertions": [[k(a), False], [k(b), True]]})
    opts.append({"label": f"{_days_ko(bins[-1])}보다 전", "assertions": [[k(bins[-1]), False], [ever, True]]})
    opts.append({"label": never_label, "assertions": [[ever, False]]})
    return {"contract_id": cid, "version": 1, "question": question, "answer_type": "single_choice", "options": opts}


def _obj(ko: str) -> str:
    """목적격 조사: 마지막 한글 음절에 받침이 있으면 '을', 없으면 '를' ("리툭시맙(rituximab)을", "ACTH) 주사를")."""
    last = next(ch for ch in reversed(ko) if "가" <= ch <= "힣")
    return ko + ("을" if (ord(last) - 0xAC00) % 28 else "를")


def _is_event(concept):
    from ctrec.propositions import CONCEPTS
    return CONCEPTS[concept].aspect == "event"


def yes_no(cid, key, question):
    return {"contract_id": cid, "version": 1, "question": question, "answer_type": "yes_no",
            "options": [{"label": "예", "assertions": [[key, True]]}, {"label": "아니요", "assertions": [[key, False]]}]}


DRUGS = [("rituximab", "리툭시맙(rituximab)"), ("obinutuzumab", "오비누투주맙(obinutuzumab)"),
         ("tacrolimus", "타크로리무스(tacrolimus)"), ("cyclosporine", "사이클로스포린(cyclosporine A)"),
         ("mycophenolate_mofetil", "마이코페놀레이트 모페틸(mycophenolate mofetil)"),
         ("cyclophosphamide", "사이클로포스파마이드(cyclophosphamide)"), ("acth", "부신피질자극호르몬(ACTH) 주사"),
         ("azathioprine", "아자티오프린(azathioprine)"), ("belimumab", "벨리무맙(belimumab)"),
         ("levamisole", "레바미솔(levamisole)")]

CONTRACTS = [
    last_contract("QC-SURGERY-LAST", "surgery",
                  "가장 최근에 수술(내시경·조직검사 같은 시술 제외)을 받은 시기는 언제인가요?", [28, 90], "수술을 받은 적 없음"),
    last_contract("QC-LIVE-VACCINE-LAST", "live_vaccine",
                  "가장 최근에 생백신(살아 있는 약독화 바이러스·세균 백신)을 맞은 시기는 언제인가요?", [28], "생백신을 맞은 적 없음"),
    last_contract("QC-BLOOD-PRODUCT-LAST", "blood_product",
                  "가장 최근에 수혈이나 면역글로불린 같은 혈액 제제를 맞은 시기는 언제인가요?", [120], "맞은 적 없음"),
    last_contract("QC-INVESTIGATIONAL-LAST", "investigational_agent",
                  "가장 최근에 임상시험에서 시험약을 투여받은 시기는 언제인가요?", [30], "시험약을 투여받은 적 없음"),
    last_contract("QC-TRIAL-PARTICIPATION-LAST", "clinical_trial_participation",
                  "다른 임상시험에 마지막으로 참여한 때는 언제인가요? (지금도 참여 중이면 '최근 3개월(90일) 이내'를 "
                  "고르세요)", [90], "임상시험에 참여한 적 없음"),
    last_contract("QC-FEBRILE-ILLNESS-LAST", "febrile_illness",
                  "가장 최근에 열이 나는 병을 앓은 시기는 언제인가요?", [14], "열이 나는 병을 앓은 적 없음"),
    last_contract("QC-MI-LAST", "myocardial_infarction",
                  "심근경색(심장마비) 진단을 받은 가장 최근 시기는 언제인가요?", [180], "심근경색 진단을 받은 적 없음"),
    last_contract("QC-URTICARIA-LAST", "idiopathic_urticaria",
                  "원인을 모르는 두드러기(특발성 두드러기) 증상이 마지막으로 있었던 시기는 언제인가요?", [365],
                  "특발성 두드러기 증상이 있었던 적 없음"),
    last_contract("QC-ANTICD20-LAST", "anti_cd20_antibody",
                  "항CD20 항체 주사(리툭시맙·오비누투주맙 등)를 가장 최근에 맞은 시기는 언제인가요?", [180], "맞은 적 없음"),
    last_contract("QC-ANTICANCER-LAST", "systemic_anticancer_therapy",
                  "가장 최근에 전신 항암 치료(항암 화학요법·면역항암제·표적항암제)를 받은 시기는 언제인가요?", [28],
                  "받은 적 없음"),
    last_contract("QC-SMOKING-LAST", "smoking", "가장 최근에 담배를 피운 시기는 언제인가요?", [365],
                  "담배를 피운 적 없음"),
    yes_no("QC-CANCER-IMMUNOTHERAPY-EVER", "cancer_immunotherapy/event/ever",
           "암 치료로 면역항암제(면역관문억제제 등)를 쓴 적이 있나요?"),
    yes_no("QC-TARGETED-THERAPY-EVER", "targeted_anticancer_therapy/event/ever", "암 치료로 표적항암제를 쓴 적이 있나요?"),
    yes_no("QC-MUCORMYCOSIS-EVER", "pulmonary_mucormycosis/condition/ever", "폐 털곰팡이증(뮤코르진균증) 진단을 받은 적이 있나요?"),
    {"contract_id": "QC-AMPHOTERICIN-COURSE", "version": 1, "answer_type": "single_choice",
     "question": "암포테리신 B(주사 항진균제) 치료를 받은 적이 있나요? 받았다면 투여 기간이 5일을 넘었나요?",
     "options": [{"label": "받았고, 5일을 넘게 투여함", "assertions": [["amphotericin_b(over_5d)/event/ever", True]]},
                 {"label": "받았지만 5일 이하로 투여함", "assertions": [["amphotericin_b(over_5d)/event/ever", False],
                                                          ["amphotericin_b/event/ever", True]]},
                 {"label": "받은 적 없음", "assertions": [["amphotericin_b/event/ever", False]]}]},
    *[last_contract(f"QC-{c.upper().replace('_', '-')}-LAST", c, f"{_obj(ko)} 가장 최근에 사용한 시기는 언제인가요?",
                    [60, 90, 180], "사용한 적 없음") for c, ko in DRUGS],
    yes_no("QC-PREGNANT-NOW", PREG_NOW, "현재 임신 중인가요?"),
    yes_no("QC-LACTATING-NOW", LACT_NOW, "현재 모유 수유 중인가요?"),
    yes_no("QC-ECMO-NOW", "ecmo/event/current", "현재 체외막산소공급(ECMO) 치료를 받고 있나요?"),
    yes_no("QC-ASPLENIA-NOW", "asplenia/condition/current",
           "비장을 제거했거나, 비장이 없거나 기능하지 않는다는 진단을 받았나요?"),
    yes_no("QC-EGFR-ADC-EVER", "egfr_adc/event/ever", "EGFR를 표적으로 하는 항체-약물 결합체(ADC) 치료를 받은 적이 있나요?"),
    yes_no("QC-ILD-STEROID-EVER", "interstitial_lung_disease_steroid/condition/ever",
           "간질성 폐질환으로 스테로이드 치료가 필요했던 적이 있나요?"),
    yes_no("QC-ANTICD38-EVER", "anti_cd38_therapy/event/ever", "항CD38 치료(다라투무맙 등)를 받은 적이 있나요?"),
    yes_no("QC-PRIMARY-IMMUNODEFICIENCY-EVER", "primary_immunodeficiency/condition/ever",
           "선천성(원발성) 면역결핍 진단을 받은 적이 있나요?"),
    yes_no("QC-NEPHROTIC-FAMILY-HISTORY", "nephrotic_family_history/condition/ever",
           "가족 중에 신증후군, 만성 사구체신염, 요독증 진단을 받은 사람이 있나요?"),
    yes_no("QC-CHOLECYSTECTOMY-EVER", "cholecystectomy/event/ever", "담낭(쓸개) 절제 수술을 받은 적이 있나요?"),
    yes_no("QC-VITRECTOMY-RD-EVER", "vitrectomy(for_detached_retina)/event/ever",
           "망막 박리 때문에 유리체 절제술을 받은 적이 있나요?"),
    yes_no("QC-THYROIDECTOMY-EVER", "total_thyroidectomy/event/ever", "갑상샘 전체를 떼어내는 수술을 받은 적이 있나요?"),
    yes_no("QC-PANCREATIC-NECROSIS-EVER", "pancreatic_necrosis/condition/ever", "췌장 괴사 진단을 받은 적이 있나요?"),
    yes_no("QC-PANCREATIC-NECROSECTOMY-EVER", "pancreatic_necrosectomy/event/ever",
           "췌장의 괴사된 조직을 제거하는 시술·수술(췌장 괴사 조직 제거술)을 받은 적이 있나요?"),
    yes_no("QC-CANCER-VACCINE-EVER", "cancer_vaccine/event/ever",
           "암을 치료하기 위한 암 백신(치료용 암 백신)을 맞은 적이 있나요? (자궁경부암(HPV)·B형간염 같은 예방 백신은 "
           "해당하지 않습니다)"),
    yes_no("QC-HIV-EVER", "hiv_infection/condition/ever", "HIV 감염 진단을 받은 적이 있나요?"),
    yes_no("QC-KAPOSI-CASTLEMAN-EVER", "kaposi_or_castleman/condition/ever",
           "카포시 육종이나 다발성 캐슬만병 진단을 받은 적이 있나요?"),
    yes_no("QC-CEREBRAL-INFARCTION-EVER", "cerebral_infarction/condition/ever", "뇌경색 진단을 받은 적이 있나요?"),
    yes_no("QC-GBS-EVER", "guillain_barre_syndrome/condition/ever", "길랭-바레 증후군 진단을 받은 적이 있나요?"),
    yes_no("QC-ANGIOEDEMA-EVER", "angioedema/condition/ever", "혈관부종 진단을 받은 적이 있나요?"),
    yes_no("QC-EBV-VACCINE-TRIAL-EVER", "ebv_vaccine_trial_participation/event/ever",
           "EBV(엡스타인-바 바이러스) 백신 임상시험에 참여한 적이 있나요?"),
    yes_no("QC-COELIAC-EVER", "coeliac_disease/condition/ever", "셀리악병 진단을 받은 적이 있나요?"),
    yes_no("QC-RETINAL-DETACHMENT-FAMILY-HISTORY", "retinal_detachment_family_history/condition/ever",
           "가족 중에 망막 박리를 겪은 사람이 있나요?"),
    {"contract_id": "QC-DIABETES-TYPE", "version": 1, "answer_type": "single_choice",
     "question": "당뇨병 진단을 받은 적이 있다면 어떤 당뇨병인가요?",
     "options": [{"label": "제1형 당뇨병", "assertions": [["diabetes_type1/condition/ever", True]]},
                 {"label": "제2형 당뇨병", "assertions": [["diabetes_type2/condition/ever", True]]},
                 {"label": "임신 중에만 생긴 임신성 당뇨병", "assertions": [["diabetes_gestational/condition/ever", True],
                                                                ["diabetes_type1/condition/ever", False],
                                                                ["diabetes_type2/condition/ever", False]]},
                 {"label": "당뇨병 진단을 받은 적 없음", "assertions": [["diabetes_type1/condition/ever", False],
                                                          ["diabetes_type2/condition/ever", False],
                                                          ["diabetes_gestational/condition/ever", False]]}]},
]


def build() -> dict[Path, str]:
    manifest = json.loads((PARSED / "FROZEN.json").read_text(encoding="utf-8"))["files"]
    by_trial: dict[str, list] = {}
    cases = {}
    for trial, rid, cond, completeness, source_req, span, unsupported, notes, contracts, case_list in SPEC:
        path = PARSED / f"{trial}.json"
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest[trial]:
            raise SystemExit(f"{trial}: 잠긴 기준표 해시 불일치 - 초안을 만들지 않음")
        frozen = json.loads(path.read_text(encoding="utf-8"))
        r = next(x for x in frozen["parsed"]["rules"] if x["rule_id"] == rid)
        meta_req = frozen["rule_meta"][rid]["required_evidence"]
        req = source_req or meta_req
        if meta_req == "clinical_record" and req != "clinical_record":
            raise SystemExit(f"{trial}:{rid} 근거 수준을 낮출 수 없음")
        ir = CriterionIR(trial_id=trial, rule_id=rid, ir_version=1, criterion_text=r["text"],
                         source_span=span or r["text"], frozen_file_hash=manifest[trial], condition=cond,
                         completeness=completeness, source_requirement=req, unsupported_parts=list(unsupported),
                         notes=notes, author=AUTHOR, review_status="draft")
        errs = ir.errors()
        if errs:
            raise SystemExit(f"{trial}:{rid} {errs}")
        by_trial.setdefault(trial, []).append(ir.to_dict())
        cases[f"{trial}:{rid}"] = {"contract_candidates": contracts, "kind": r["kind"],
                                   "cases": [{"facts": [[k, v] for k, v in f.items()], "expect": e}
                                             for f, e in case_list]}
    for c in CONTRACTS:
        errs = QuestionContract.from_dict(c).errors()
        if errs:
            raise SystemExit(f"{c['contract_id']} {errs}")
    known = {c["contract_id"] for c in CONTRACTS}
    for key, v in cases.items():
        missing = [c for c in v["contract_candidates"] if c not in known]
        if missing:
            raise SystemExit(f"{key}: 정의되지 않은 계약 후보 {missing}")
    dump = lambda o: json.dumps(o, ensure_ascii=False, indent=1) + "\n"  # noqa: E731
    out = {OUT / "ir" / f"{t}.json": dump({"format_version": 1, "trial_id": t, "criteria": crit})
           for t, crit in sorted(by_trial.items())}
    out[OUT / "question_contracts.json"] = dump({"format_version": 1, "contracts": [
        {**c, "notes": "", "review_status": "draft"} for c in CONTRACTS]})
    out[OUT / "ir_cases.json"] = dump({"format_version": 1, "note": "사실 키는 평가 시점(assessment) 기준. "
                                       "테스트는 기준 시점 근사를 '가정'으로 적용해 IR 의도만 확인함(승인 아님)",
                                       "criteria": cases})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    files = build()
    if args.check:
        stale = [str(p.relative_to(ROOT)) for p, s in files.items()
                 if not p.exists() or p.read_text(encoding="utf-8") != s]
        print("최신" if not stale else f"다름: {stale}")
        return 1 if stale else 0
    for p, s in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(s, encoding="utf-8")
    n = sum(len(json.loads(s)["criteria"]) for p, s in files.items() if p.parent.name == "ir")
    print(f"IR 초안 {n}개 ({len(files) - 2}개 시험), 질문 계약 초안 {len(CONTRACTS)}개 - 모두 draft (승인 기록 없음)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
