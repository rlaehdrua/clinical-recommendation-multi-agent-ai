# IR·질문 계약 초안 검수표 (자동 생성: scripts/report_contract_drafts.py)

**모든 초안은 미승인(unreviewed)입니다. '검수 준비 완료'는 승인이 아니며, 사람(임상 검수자)의 검수 기록 없이는 결정적 판정에 쓰이지 않습니다.** 의미 분류는 AI가 만든 사전 분류이므로 검수자가 다시 판단해야 합니다.

검수 우선순위: 임상 전문가 검토 필요 -> 수정 필요 -> 검수 준비 완료 -> 현재 IR로 표현 불가(부분 IR, 확정에 쓰이지 않음).
기간 기준의 '기준 시점 결정 필요'는 팀 정책(P0 K-2) 결정 후 검수자가 anchor_policy를 추가해야 판정에 쓰입니다.

## IR 57개

임상 전문가 검토 필요 13 / 수정 필요 0 / 검수 준비 완료 37 / 현재 IR로 표현 불가 7

| 기준 | 사전 분류 | 완전성 | 근거 수준 | 명제 | 상위 개념 사용 | 계약 후보 | 정적 검사 | 위험·확인 사항 |
|---|---|---|---|---|---|---|---|---|
| NCT06125405:I12 | 임상 전문가 검토 필요 | complete | self_report_ok | acth/event/w90d-assessment | - | QC-ACTH-LAST | 기준 시점 결정 필요: enrollment | 진단 목적 ACTH(자극 검사) 투여가 'used'에 포함되는지 |
| NCT06468397:E21 | 임상 전문가 검토 필요 | partial | self_report_ok | retinal_detachment_family_history/condition/ever | - | QC-RETINAL-DETACHMENT-FAMILY-HISTORY | - | 'Fellow eye:' 표지와 가족력의 관계 불분명 |
| NCT06756191:E1 | 임상 전문가 검토 필요 | partial | self_report_ok | amphotericin_b(over_5d)/event/ever, pulmonary_mucormycosis/condition/ever | - | QC-MUCORMYCOSIS-EVER, QC-AMPHOTERICIN-COURSE | - | P8 수정: '5일 초과'를 한정어(over_5d)로 표현. 남은 쟁점: 투여가 폐 털곰팡이증 치료(같은 에피소드)여야 하는지 (부분 IR: 결정적 판정에 쓰이지 않음), 5일이 누적인지 1회 투여 기간인지 |
| NCT06908096:E11 | 임상 전문가 검토 필요 | complete | self_report_ok | angioedema/condition/ever | - | QC-ANGIOEDEMA-EVER | - | '유전성·후천성·특발성 = 모든 혈관부종' 해석 확인 |
| NCT06908096:E12 | 임상 전문가 검토 필요 | complete | self_report_ok | idiopathic_urticaria/condition/w365d-assessment | - | QC-URTICARIA-LAST | 기준 시점 결정 필요: screening | 'within the past year' 기준 시점을 screening으로 가정 |
| NCT06908096:E25 | 임상 전문가 검토 필요 | complete | self_report_ok | hiv_infection/condition/ever | - | QC-HIV-EVER | - | 'HIV 진단 이력 = 감염' 해석 확인 |
| NCT06908096:E8 | 임상 전문가 검토 필요 | complete | self_report_ok | febrile_illness/condition/w14d-assessment | - | QC-FEBRILE-ILLNESS-LAST | 기준 시점 결정 필요: first_dose | 'febrile illness' 체온 기준 정의, 기준 시점 first_dose |
| NCT06916494:I3 | 임상 전문가 검토 필요 | partial | self_report_ok | cancer_immunotherapy/event/ever, targeted_anticancer_therapy/event/ever | cancer_immunotherapy, targeted_anticancer_therapy | QC-CANCER-IMMUNOTHERAPY-EVER, QC-TARGETED-THERAPY-EVER | - | systemic immunotherapy·target therapy의 범위 (부분 IR) |
| NCT07087314:E14 | 임상 전문가 검토 필요 | complete | self_report_ok | live_vaccine/event/w28d-assessment | live_vaccine | QC-LIVE-VACCINE-LAST | 기준 시점 결정 필요: first_dose | 환자가 생백신 여부를 구분할 수 있는지 (답 신뢰도), 기준 시점 first_dose |
| NCT07087314:E15 | 임상 전문가 검토 필요 | complete | self_report_ok | surgery(major.non_diagnostic)/event/w28d-assessment | - | QC-SURGERY-LAST | 기준 시점 결정 필요: first_dose | 'major' 정의 없음, 기준 시점 first_dose. '수술 없음'만 결정 가능 |
| NCT07087314:I11 | 임상 전문가 검토 필요 | complete | self_report_ok | azathioprine/event/w60d-assessment, belimumab/event/w60d-assessment, cyclophosphamide/event/w60d-assessment, cyclosporine/event/w60d-assessment, levamisole/event/w60d-assessment, mycophenolate_mofetil/event/w60d-assessment, tacrolimus/event/w60d-assessment | - | QC-TACROLIMUS-LAST, QC-CYCLOSPORINE-LAST, QC-MYCOPHENOLATE-MOFETIL-LAST, QC-BELIMUMAB-LAST, QC-LEVAMISOLE-LAST, QC-AZATHIOPRINE-LAST, QC-CYCLOPHOSPHAMIDE-LAST | 기준 시점 결정 필요: enrollment | 원문에 기준 시점이 없어 enrollment로 가정, 2개월=60일 환산 |
| NCT07087314:I12 | 임상 전문가 검토 필요 | complete | self_report_ok | obinutuzumab/event/w180d-assessment, rituximab/event/w180d-assessment | obinutuzumab, rituximab | QC-ANTICD20-LAST, QC-RITUXIMAB-LAST, QC-OBINUTUZUMAB-LAST | 기준 시점 결정 필요: enrollment | 기준 시점 가정(enrollment), 6개월=180일 환산 |
| NCT07679893:E25 | 임상 전문가 검토 필요 | partial | self_report_ok | smoking/event/current, smoking/event/w365d-assessment | - | QC-SMOKING-LAST | 기준 시점 결정 필요: screening | 'recently smoked' 기간 정의, 전자담배 포함 여부 (부분 IR) |
| DEMO-LUNG-001:E2 | 검수 준비 완료 | complete | self_report_ok | egfr_adc/event/ever | - | QC-EGFR-ADC-EVER | - | 환자가 'ADC' 용어를 모를 수 있음 (모름 -> 미확인으로 남음) |
| DEMO-LUNG-001:E3 | 검수 준비 완료 | complete | self_report_ok | interstitial_lung_disease_steroid/condition/ever | - | QC-ILD-STEROID-EVER | - | '스테로이드가 필요했던 ILD'를 한 개념으로 둠 |
| DEMO-LUNG-001:E4 | 검수 준비 완료 | complete | self_report_ok | lactation/condition/current, pregnancy/condition/current | - | QC-PREGNANT-NOW, QC-LACTATING-NOW | - | 현재 임신·수유 자기 보고의 신뢰도는 QC-PREGNANT-NOW 검토 사항 |
| DEMO-LUNG-001:E5 | 검수 준비 완료 | complete | clinical_record | active_hepatitis_b/condition/current, active_hepatitis_c/condition/current | - | - | 계약 후보 없음 (기록 확인 경로) | clinical_record로 상향: 자기 보고만으로는 UNVERIFIED (계약 없음) |
| NCT04063540:E9 | 검수 준비 완료 | complete | self_report_ok | lactation/condition/current, pregnancy/condition/current | - | QC-PREGNANT-NOW, QC-LACTATING-NOW | - | - |
| NCT04761523:E6 | 검수 준비 완료 | complete | self_report_ok | lactation/condition/current, pregnancy/condition/current | - | QC-PREGNANT-NOW, QC-LACTATING-NOW | - | - |
| NCT06080789:E4 | 검수 준비 완료 | complete | self_report_ok | pancreatic_necrosectomy/event/ever, pancreatic_necrosis/condition/ever | pancreatic_necrosectomy | QC-PANCREATIC-NECROSIS-EVER, QC-PANCREATIC-NECROSECTOMY-EVER | - | P7에서 수정: necrosectomy를 췌장 괴사 제거술로 한정 (다른 부위 제거술로 잘못 제외하던 결함) |
| NCT06125405:E1 | 검수 준비 완료 | complete | self_report_ok | nephrotic_family_history/condition/ever | - | QC-NEPHROTIC-FAMILY-HISTORY | - | - |
| NCT06125405:E13 | 검수 준비 완료 | complete | self_report_ok | clinical_trial_participation/event/w90d-assessment | - | QC-TRIAL-PARTICIPATION-LAST | 기준 시점 결정 필요: enrollment | '참여' 정의(동의 시점·투약 시점) 확인 |
| NCT06125405:I10 | 검수 준비 완료 | complete | self_report_ok | cyclosporine/event/w90d-assessment | - | QC-CYCLOSPORINE-LAST | 기준 시점 결정 필요: enrollment | 3개월=90일 환산 확인 |
| NCT06125405:I11 | 검수 준비 완료 | complete | self_report_ok | cyclophosphamide/event/w90d-assessment | - | QC-CYCLOPHOSPHAMIDE-LAST | 기준 시점 결정 필요: enrollment | 3개월=90일 환산 확인 |
| NCT06125405:I7 | 검수 준비 완료 | complete | self_report_ok | rituximab/event/w180d-assessment | rituximab | QC-RITUXIMAB-LAST | 기준 시점 결정 필요: enrollment | 6개월=180일 환산 확인 |
| NCT06125405:I8 | 검수 준비 완료 | complete | self_report_ok | tacrolimus/event/w90d-assessment | - | QC-TACROLIMUS-LAST | 기준 시점 결정 필요: enrollment | 3개월=90일 환산 확인 |
| NCT06125405:I9 | 검수 준비 완료 | complete | self_report_ok | mycophenolate_mofetil/event/w90d-assessment | - | QC-MYCOPHENOLATE-MOFETIL-LAST | 기준 시점 결정 필요: enrollment | 3개월=90일 환산 확인 |
| NCT06294847:E1 | 검수 준비 완료 | complete | self_report_ok | vitrectomy(for_detached_retina)/event/ever | vitrectomy | QC-VITRECTOMY-RD-EVER | - | - |
| NCT06294847:E5 | 검수 준비 완료 | complete | self_report_ok | lactation/condition/current, pregnancy/condition/current | - | QC-PREGNANT-NOW, QC-LACTATING-NOW | - | - |
| NCT06391359:E5 | 검수 준비 완료 | complete | self_report_ok | cholecystectomy/event/ever | cholecystectomy | QC-CHOLECYSTECTOMY-EVER, QC-SURGERY-LAST | - | - |
| NCT06756191:E10 | 검수 준비 완료 | complete | self_report_ok | myocardial_infarction/condition/ever | - | QC-MI-LAST | - | - |
| NCT06756191:E11 | 검수 준비 완료 | complete | self_report_ok | cerebral_infarction/condition/ever | - | QC-CEREBRAL-INFARCTION-EVER | - | - |
| NCT06756191:E12 | 검수 준비 완료 | complete | self_report_ok | ecmo/event/current | - | QC-ECMO-NOW | - | 현재 ECMO 중인 환자는 직접 답하기 어려움 (보호자 답) |
| NCT06756191:E13 | 검수 준비 완료 | complete | self_report_ok | lactation/condition/current, pregnancy/condition/current | - | QC-PREGNANT-NOW, QC-LACTATING-NOW | - | - |
| NCT06833073:E10 | 검수 준비 완료 | complete | clinical_record | active_tuberculosis/condition/current | - | - | 계약 후보 없음 (기록 확인 경로) | clinical_record (활동성 결핵) |
| NCT06833073:E11 | 검수 준비 완료 | complete | self_report_ok | hiv_infection/condition/ever | - | QC-HIV-EVER | - | - |
| NCT06833073:E12 | 검수 준비 완료 | complete | self_report_ok | hiv_infection/condition/ever, kaposi_or_castleman/condition/ever | - | QC-HIV-EVER, QC-KAPOSI-CASTLEMAN-EVER | - | - |
| NCT06833073:E4 | 검수 준비 완료 | complete | self_report_ok | myocardial_infarction/condition/w180d-assessment | - | QC-MI-LAST | 기준 시점 결정 필요: randomization | 기준 시점 randomization 근사는 팀 결정 |
| NCT06833073:E6 | 검수 준비 완료 | complete | self_report_ok | cancer_vaccine/event/ever | - | QC-CANCER-VACCINE-EVER | - | - |
| NCT06908096:E14 | 검수 준비 완료 | complete | self_report_ok | diabetes_type1/condition/ever, diabetes_type2/condition/ever | - | QC-DIABETES-TYPE | - | 임신성 당뇨 제외는 개념 구분으로 표현 |
| NCT06908096:E22 | 검수 준비 완료 | complete | self_report_ok | asplenia/condition/current | - | QC-ASPLENIA-NOW | - | - |
| NCT06908096:E23 | 검수 준비 완료 | complete | self_report_ok | guillain_barre_syndrome/condition/ever | - | QC-GBS-EVER | - | - |
| NCT06908096:E4 | 검수 준비 완료 | complete | self_report_ok | blood_product/event/w120d-assessment | - | QC-BLOOD-PRODUCT-LAST | 기준 시점 결정 필요: first_dose | 기준 시점 first_dose 근사는 팀 결정 |
| NCT06908096:E7 | 검수 준비 완료 | complete | self_report_ok | ebv_vaccine_trial_participation/event/ever | ebv_vaccine_trial_participation | QC-EBV-VACCINE-TRIAL-EVER | - | - |
| NCT07087314:E1 | 검수 준비 완료 | complete | self_report_ok | nephrotic_family_history/condition/ever | - | QC-NEPHROTIC-FAMILY-HISTORY | - | 가족 범위(직계 등)가 원문에 없음 - 질문은 '가족 중' |
| NCT07087314:E11 | 검수 준비 완료 | complete | self_report_ok | primary_immunodeficiency/condition/ever | - | QC-PRIMARY-IMMUNODEFICIENCY-EVER | - | - |
| NCT07087314:E13 | 검수 준비 완료 | complete | self_report_ok | anti_cd38_therapy/event/ever | - | QC-ANTICD38-EVER | - | 환자가 'CD38' 치료명을 모를 수 있음 |
| NCT07369063:E9 | 검수 준비 완료 | complete | self_report_ok | pregnancy/condition/current | - | QC-PREGNANT-NOW | - | - |
| NCT07516951:E1 | 검수 준비 완료 | complete | clinical_record | coeliac_disease/condition/ever | - | QC-COELIAC-EVER | - | clinical_record ('documented') |
| NCT07516951:E2 | 검수 준비 완료 | complete | clinical_record | lower_respiratory_tract_infection/condition/w28d-assessment | - | - | 기준 시점 결정 필요: screening; 계약 후보 없음 (기록 확인 경로) | clinical_record ('Documented'), 계약 없음 |
| NCT00575185:E6 | 현재 IR로 표현 불가 | partial | self_report_ok | lactation/condition/current | - | QC-LACTATING-NOW | - | 'during the study' 미래 수유 (부분 IR) |
| NCT06468397:E5 | 현재 IR로 표현 불가 | partial | self_report_ok | lactation/condition/current, pregnancy/condition/current | - | QC-PREGNANT-NOW, QC-LACTATING-NOW | - | 'to be confirmed on treatment day' 절차 (부분 IR) |
| NCT06727604:E1 | 현재 IR로 표현 불가 | partial | self_report_ok | radioactive_iodine_therapy(successful)/event/ever, total_thyroidectomy/event/ever | total_thyroidectomy | QC-THYROIDECTOMY-EVER | - | 'successfully treated'는 의료진 판단 (부분 IR) |
| NCT06833073:E5 | 현재 IR로 표현 불가 | partial | self_report_ok | systemic_anticancer_therapy/event/w28d-assessment | - | QC-ANTICANCER-LAST | 기준 시점 결정 필요: randomization | 'including investigational agents' 범위 (부분 IR) |
| NCT06908096:E1 | 현재 IR로 표현 불가 | partial | self_report_ok | lactation/condition/current, pregnancy/condition/current | - | QC-PREGNANT-NOW, QC-LACTATING-NOW | - | 임신 계획(미래 의향) |
| NCT06908096:E3 | 현재 IR로 표현 불가 | partial | self_report_ok | - | - | - | 계약 후보 없음 (기록 확인 경로) | 면역조절 치료 범위가 원문에 없음 |
| NCT06908096:E5 | 현재 IR로 표현 불가 | partial | self_report_ok | investigational_agent/event/w30d-assessment | - | QC-INVESTIGATIONAL-LAST | 기준 시점 결정 필요: first_dose | 연구 중 시험약 계획(미래 의향) |

## 질문 계약 49개

임상 전문가 검토 필요 9 / 검수 준비 완료 40

공유 범위: 같은 계약을 여러 IR이 쓰는 것은 명제 키가 같거나 코드로 승인된 함의(넓은 기간 '없음' -> 좁은 기간 '없음', 하위 개념 '있음' -> 상위 개념 '있음')로만 이어질 때입니다. 아래 '사용 IR'이 그 목록입니다.

| 계약 | 사전 분류 | 형식 | 선택지 수 | 단언 명제 | 사용 IR | 정적 검사 | 위험·확인 사항 |
|---|---|---|---|---|---|---|---|
| QC-ACTH-LAST | 임상 전문가 검토 필요 | single_choice | 5 | acth/event/ever, acth/event/w180d-assessment, acth/event/w60d-assessment, acth/event/w90d-assessment | 1 (NCT06125405:I12) | - | 진단 목적 ACTH 투여 포함 여부 |
| QC-AMPHOTERICIN-COURSE | 임상 전문가 검토 필요 | single_choice | 3 | amphotericin_b(over_5d)/event/ever, amphotericin_b/event/ever | 1 (NCT06756191:E1) | - | P8 신규(QC-AMPHOTERICIN-EVER 대체): '5일'이 누적 기간인지 1회 투여 기간인지 환자가 다르게 해석할 수 있음 |
| QC-ANTICANCER-LAST | 임상 전문가 검토 필요 | single_choice | 3 | systemic_anticancer_therapy/event/ever, systemic_anticancer_therapy/event/w28d-assessment | 1 (NCT06833073:E5) | - | 호르몬 치료 포함 여부가 선택지에 드러나지 않음 |
| QC-CANCER-VACCINE-EVER | 임상 전문가 검토 필요 | yes_no | 2 | cancer_vaccine/event/ever | 1 (NCT06833073:E6) | - | P8 수정: '암 백신'이 예방 백신(HPV 등)으로 읽힐 수 있었음(잘못된 제외) -> 치료용 명시. 예방 백신 제외가 기준 의도와 맞는지 임상 확인 |
| QC-FEBRILE-ILLNESS-LAST | 임상 전문가 검토 필요 | single_choice | 3 | febrile_illness/condition/ever, febrile_illness/condition/w14d-assessment | 1 (NCT06908096:E8) | - | '열이 나는 병'의 체온 기준 |
| QC-LIVE-VACCINE-LAST | 임상 전문가 검토 필요 | single_choice | 3 | live_vaccine/event/ever, live_vaccine/event/w28d-assessment | 1 (NCT07087314:E14) | - | 환자가 생백신 여부를 구분하기 어려움 |
| QC-PREGNANT-NOW | 임상 전문가 검토 필요 | yes_no | 2 | pregnancy/condition/current | 8 (DEMO-LUNG-001:E4, NCT04063540:E9, NCT04761523:E6, NCT06294847:E5, NCT06756191:E13, NCT07369063:E9, NCT06468397:E5, NCT06908096:E1) | - | 초기 임신은 본인이 모를 수 있음. 자기 보고 '아니요'로 '임신 아님'을 확정해도 되는지 (검사 요구 여부) |
| QC-SMOKING-LAST | 임상 전문가 검토 필요 | single_choice | 3 | smoking/event/ever, smoking/event/w365d-assessment | 1 (NCT07679893:E25) | - | 전자담배·가열담배 포함 여부 |
| QC-TRIAL-PARTICIPATION-LAST | 임상 전문가 검토 필요 | single_choice | 3 | clinical_trial_participation/event/ever, clinical_trial_participation/event/w90d-assessment | 1 (NCT06125405:E13) | - | P8 수정: '참여한 시기'가 시작 시점으로 읽혀 현재 참여 중인 환자가 '3개월보다 전'을 고를 수 있었음(잘못된 통과) -> 마지막 참여 시점·현재 참여 안내. '참여' 종료 기준(마지막 투약·방문·추적관찰 포함 여부)은 임상 결정 |
| QC-ANGIOEDEMA-EVER | 검수 준비 완료 | yes_no | 2 | angioedema/condition/ever | 1 (NCT06908096:E11) | - | - |
| QC-ANTICD20-LAST | 검수 준비 완료 | single_choice | 3 | anti_cd20_antibody/event/ever, anti_cd20_antibody/event/w180d-assessment | 1 (NCT07087314:I12) | - | - |
| QC-ANTICD38-EVER | 검수 준비 완료 | yes_no | 2 | anti_cd38_therapy/event/ever | 1 (NCT07087314:E13) | - | - |
| QC-ASPLENIA-NOW | 검수 준비 완료 | yes_no | 2 | asplenia/condition/current | 1 (NCT06908096:E22) | - | - |
| QC-AZATHIOPRINE-LAST | 검수 준비 완료 | single_choice | 5 | azathioprine/event/ever, azathioprine/event/w180d-assessment, azathioprine/event/w60d-assessment, azathioprine/event/w90d-assessment | 1 (NCT07087314:I11) | - | - |
| QC-BELIMUMAB-LAST | 검수 준비 완료 | single_choice | 5 | belimumab/event/ever, belimumab/event/w180d-assessment, belimumab/event/w60d-assessment, belimumab/event/w90d-assessment | 1 (NCT07087314:I11) | - | - |
| QC-BLOOD-PRODUCT-LAST | 검수 준비 완료 | single_choice | 3 | blood_product/event/ever, blood_product/event/w120d-assessment | 1 (NCT06908096:E4) | - | - |
| QC-CANCER-IMMUNOTHERAPY-EVER | 검수 준비 완료 | yes_no | 2 | cancer_immunotherapy/event/ever | 1 (NCT06916494:I3) | - | - |
| QC-CEREBRAL-INFARCTION-EVER | 검수 준비 완료 | yes_no | 2 | cerebral_infarction/condition/ever | 1 (NCT06756191:E11) | - | - |
| QC-CHOLECYSTECTOMY-EVER | 검수 준비 완료 | yes_no | 2 | cholecystectomy/event/ever | 1 (NCT06391359:E5) | - | - |
| QC-COELIAC-EVER | 검수 준비 완료 | yes_no | 2 | coeliac_disease/condition/ever | 1 (NCT07516951:E1) | - | - |
| QC-CYCLOPHOSPHAMIDE-LAST | 검수 준비 완료 | single_choice | 5 | cyclophosphamide/event/ever, cyclophosphamide/event/w180d-assessment, cyclophosphamide/event/w60d-assessment, cyclophosphamide/event/w90d-assessment | 2 (NCT07087314:I11, NCT06125405:I11) | - | - |
| QC-CYCLOSPORINE-LAST | 검수 준비 완료 | single_choice | 5 | cyclosporine/event/ever, cyclosporine/event/w180d-assessment, cyclosporine/event/w60d-assessment, cyclosporine/event/w90d-assessment | 2 (NCT07087314:I11, NCT06125405:I10) | - | - |
| QC-DIABETES-TYPE | 검수 준비 완료 | single_choice | 4 | diabetes_gestational/condition/ever, diabetes_type1/condition/ever, diabetes_type2/condition/ever | 1 (NCT06908096:E14) | - | - |
| QC-EBV-VACCINE-TRIAL-EVER | 검수 준비 완료 | yes_no | 2 | ebv_vaccine_trial_participation/event/ever | 1 (NCT06908096:E7) | - | - |
| QC-ECMO-NOW | 검수 준비 완료 | yes_no | 2 | ecmo/event/current | 1 (NCT06756191:E12) | - | - |
| QC-EGFR-ADC-EVER | 검수 준비 완료 | yes_no | 2 | egfr_adc/event/ever | 1 (DEMO-LUNG-001:E2) | - | - |
| QC-GBS-EVER | 검수 준비 완료 | yes_no | 2 | guillain_barre_syndrome/condition/ever | 1 (NCT06908096:E23) | - | - |
| QC-HIV-EVER | 검수 준비 완료 | yes_no | 2 | hiv_infection/condition/ever | 3 (NCT06908096:E25, NCT06833073:E11, NCT06833073:E12) | - | - |
| QC-ILD-STEROID-EVER | 검수 준비 완료 | yes_no | 2 | interstitial_lung_disease_steroid/condition/ever | 1 (DEMO-LUNG-001:E3) | - | - |
| QC-INVESTIGATIONAL-LAST | 검수 준비 완료 | single_choice | 3 | investigational_agent/event/ever, investigational_agent/event/w30d-assessment | 1 (NCT06908096:E5) | - | - |
| QC-KAPOSI-CASTLEMAN-EVER | 검수 준비 완료 | yes_no | 2 | kaposi_or_castleman/condition/ever | 1 (NCT06833073:E12) | - | - |
| QC-LACTATING-NOW | 검수 준비 완료 | yes_no | 2 | lactation/condition/current | 8 (DEMO-LUNG-001:E4, NCT04063540:E9, NCT04761523:E6, NCT06294847:E5, NCT06756191:E13, NCT00575185:E6, NCT06468397:E5, NCT06908096:E1) | - | - |
| QC-LEVAMISOLE-LAST | 검수 준비 완료 | single_choice | 5 | levamisole/event/ever, levamisole/event/w180d-assessment, levamisole/event/w60d-assessment, levamisole/event/w90d-assessment | 1 (NCT07087314:I11) | - | - |
| QC-MI-LAST | 검수 준비 완료 | single_choice | 3 | myocardial_infarction/condition/ever, myocardial_infarction/condition/w180d-assessment | 2 (NCT06756191:E10, NCT06833073:E4) | - | - |
| QC-MUCORMYCOSIS-EVER | 검수 준비 완료 | yes_no | 2 | pulmonary_mucormycosis/condition/ever | 1 (NCT06756191:E1) | - | - |
| QC-MYCOPHENOLATE-MOFETIL-LAST | 검수 준비 완료 | single_choice | 5 | mycophenolate_mofetil/event/ever, mycophenolate_mofetil/event/w180d-assessment, mycophenolate_mofetil/event/w60d-assessment, mycophenolate_mofetil/event/w90d-assessment | 2 (NCT07087314:I11, NCT06125405:I9) | - | - |
| QC-NEPHROTIC-FAMILY-HISTORY | 검수 준비 완료 | yes_no | 2 | nephrotic_family_history/condition/ever | 2 (NCT06125405:E1, NCT07087314:E1) | - | - |
| QC-OBINUTUZUMAB-LAST | 검수 준비 완료 | single_choice | 5 | obinutuzumab/event/ever, obinutuzumab/event/w180d-assessment, obinutuzumab/event/w60d-assessment, obinutuzumab/event/w90d-assessment | 1 (NCT07087314:I12) | - | - |
| QC-PANCREATIC-NECROSECTOMY-EVER | 검수 준비 완료 | yes_no | 2 | pancreatic_necrosectomy/event/ever | 1 (NCT06080789:E4) | - | P7에서 수정: 췌장 괴사 제거술로 한정 |
| QC-PANCREATIC-NECROSIS-EVER | 검수 준비 완료 | yes_no | 2 | pancreatic_necrosis/condition/ever | 1 (NCT06080789:E4) | - | - |
| QC-PRIMARY-IMMUNODEFICIENCY-EVER | 검수 준비 완료 | yes_no | 2 | primary_immunodeficiency/condition/ever | 1 (NCT07087314:E11) | - | - |
| QC-RETINAL-DETACHMENT-FAMILY-HISTORY | 검수 준비 완료 | yes_no | 2 | retinal_detachment_family_history/condition/ever | 1 (NCT06468397:E21) | - | - |
| QC-RITUXIMAB-LAST | 검수 준비 완료 | single_choice | 5 | rituximab/event/ever, rituximab/event/w180d-assessment, rituximab/event/w60d-assessment, rituximab/event/w90d-assessment | 2 (NCT07087314:I12, NCT06125405:I7) | - | - |
| QC-SURGERY-LAST | 검수 준비 완료 | single_choice | 4 | surgery/event/ever, surgery/event/w28d-assessment, surgery/event/w90d-assessment | 2 (NCT07087314:E15, NCT06391359:E5) | - | '시술 제외' 안내가 환자마다 다르게 해석될 수 있음 (복강경 수술은 수술) |
| QC-TACROLIMUS-LAST | 검수 준비 완료 | single_choice | 5 | tacrolimus/event/ever, tacrolimus/event/w180d-assessment, tacrolimus/event/w60d-assessment, tacrolimus/event/w90d-assessment | 2 (NCT07087314:I11, NCT06125405:I8) | - | - |
| QC-TARGETED-THERAPY-EVER | 검수 준비 완료 | yes_no | 2 | targeted_anticancer_therapy/event/ever | 1 (NCT06916494:I3) | - | - |
| QC-THYROIDECTOMY-EVER | 검수 준비 완료 | yes_no | 2 | total_thyroidectomy/event/ever | 1 (NCT06727604:E1) | - | - |
| QC-URTICARIA-LAST | 검수 준비 완료 | single_choice | 3 | idiopathic_urticaria/condition/ever, idiopathic_urticaria/condition/w365d-assessment | 1 (NCT06908096:E12) | - | P7에서 수정: '진단 시기' -> '증상이 마지막으로 있었던 시기' (기준은 1년 내 증상) |
| QC-VITRECTOMY-RD-EVER | 검수 준비 완료 | yes_no | 2 | vitrectomy(for_detached_retina)/event/ever | 1 (NCT06294847:E1) | - | - |
