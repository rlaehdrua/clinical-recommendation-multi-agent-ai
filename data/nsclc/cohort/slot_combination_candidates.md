# 슬롯 조합 제약 후보 (검수 전)

슬롯을 서로 독립으로 샘플링하면 임상적으로 불가능하거나 드문 조합이 생깁니다(예: EGFR 변이가 없는데 EGFR-TKI 치료력이 있음). 아래는 생성기에 넣을 **후보**일 뿐이며, 확정하지 않았습니다. 각 항목은 **검수 필요**이고, 확률값은 일부러 적지 않았습니다(검수 후 사람이 정함).

유형:
- **논리**: 시험 기준이나 정의상 함께 올 수 없음. 검수 후 생성기에서 금지 조합으로 둘 후보
- **조건부**: 가능하지만 한 슬롯 값이 다른 슬롯의 분포를 바꿈. 조건부 사전 분포 후보
- **파생**: 다른 슬롯에서 계산되는 값이라 독립 샘플링하면 안 됨

| # | 유형 | 조합 | 왜 후보인지 | 관련 시험 | 검수 |
|---|---|---|---|---|---|
| 1 | 논리 | `tx.prior_targeted_tki` ≠ none → `bm.driver_alteration` ≠ none·not_tested | 표적치료(TKI)는 해당 변이가 있어야 받는 치료로 보임. 변이 없음이면서 TKI 치료력은 모순 후보 | T1 하위 연구 3, T3, T5 | 검수 필요 |
| 2 | 조건부 | `bm.driver_alteration` = egfr_* → `tx.prior_targeted_tki` = egfr_tki 확률이 높음 (진행성 병기 치료력이 있다면) | EGFR 변이 환자의 1차 치료는 대개 EGFR-TKI라는 가정. T3·T8의 대상 환자가 여기서 갈림 | T3, T8 | 검수 필요 |
| 3 | 조건부 | `bm.driver_alteration` = alk → `tx.prior_targeted_tki` = alk_tki | 2와 같은 이유 | T3 | 검수 필요 |
| 4 | 논리 | `bm.driver_alteration`은 값 하나 (EGFR와 ALK 등 동시 양성 없음) | 동시 변이는 드물다는 가정으로 단일 범주 슬롯을 씀. 동시 변이를 허용할지 결정 필요 | 전체 | 검수 필요 |
| 5 | 논리 | `tx.prior_systemic_advanced` = none → `tx.prior_pd1_pdl1` = none 또는 durvalumab_consolidation, `tx.prior_targeted_tki` = none | 진행성 병기 치료력이 없으면 면역치료·TKI 치료력도 없어야 함. 단, 3기 근치 후 더발루맙 공고 요법은 예외 | T4, T5, T6 | 검수 필요 |
| 6 | 논리 | `tx.prior_pd1_pdl1` = durvalumab_consolidation → `dx.stage` 이력 = III, `tx.prior_platinum` = yes (화학방사선) | 더발루맙 공고 요법은 3기 화학방사선 치료 뒤에 쓰인다는 가정 | T6 | 검수 필요 |
| 7 | 조건부 | `dx.disease_setting` = newly_diagnosed_advanced → `tx.prior_systemic_advanced` = none | "newly diagnosed"는 치료력이 없다는 뜻으로 볼지 결정 필요 (T8 공개 기준에는 치료력 조항 없음) | T8 | 검수 필요 |
| 8 | 조건부 | `dx.histology` = squamous → `bm.driver_alteration` = not_tested 또는 none 확률이 높음 | T6은 선암이 아니면 EGFR·ALK 검사를 요구하지 않음. 편평상피암은 구동 유전자 변이 빈도가 낮다는 가정 | T6, T8 (비편평만), T1 하위 연구 3 | 검수 필요 |
| 9 | 조건부 | `bm.driver_alteration` ≠ none → `bm.pdl1_tps` ≥50 확률이 낮음 | T3은 AGA 양성과 TPS ≥50%를 함께 요구함. 두 조건이 함께 나오는 빈도를 정해야 T3 적격 환자 비율이 정해짐 | T3 | 검수 필요 |
| 10 | 논리 | `sex` = male → `repro.pregnant_or_breastfeeding` = no | 정의상 | T2–T6 | 검수 필요 |
| 11 | 조건부 | `med.systemic_steroid_dose` > 10 → 사용 사유 슬롯(증상 있는 `cns.brain_mets`, `cm.autoimmune`, `tx.severe_irae`) 중 하나가 함께 있음 | 고용량 스테로이드를 쓸 이유가 없는 환자에게 스테로이드만 샘플링되는 것을 막기 위함 | T3, T4, T5, T6 | 검수 필요 |
| 12 | 조건부 | `cns.brain_mets` = symptomatic_or_unstable → `med.systemic_steroid_dose` 높을 확률이 높음, `ecog` 높을 확률이 높음 | 증상 있는 뇌전이와 스테로이드·수행 상태의 상관 가정 | T3, T4, T5, T8 | 검수 필요 |
| 13 | 조건부 | `dx.liver_mets` = yes → `lab.ast_alt_uln` 분포가 높음 | T5·T6은 간 전이가 있으면 AST·ALT 기준이 ≤5×ULN으로 바뀜. 간 전이 슬롯 없이는 판정 불가 | T5, T6 | 검수 필요 |
| 14 | 조건부 | Gilbert 증후군 → `lab.bilirubin_uln` 높음 | T5·T6에 Gilbert 예외 조항. 슬롯을 따로 둘지 결정 필요 | T5, T6 | 검수 필요 |
| 15 | 조건부 | `med.anticoagulant` ≠ none → `lab.inr_aptt_uln` 높음 (치료 범위) | T4는 항응고제 사용자의 INR·aPTT 기준이 다름 | T4, T2 (와파린 제외) | 검수 필요 |
| 16 | 파생 | `lab.creatinine_clearance` ← 나이·체중·혈청 크레아티닌·성별 (Cockcroft-Gault) | 독립 샘플링하면 나이·체중과 맞지 않는 값이 나옴. 계산식도 시험마다 다름 (T5: Cockcroft·MDRD·CKD-EPI, T6: Cockcroft-Gault 또는 측정값) | T5, T6 | 검수 필요 |
| 17 | 파생 | `tx.days_since_last_anticancer` ← `tx.prior_systemic_advanced` = none이면 값 없음 | 치료력이 없으면 경과일 슬롯은 해당 없음 | T3, T6 | 검수 필요 |
| 18 | 조건부 | `tx.prior_pd1_pdl1` ≠ none → `tx.severe_irae`·`tx.unresolved_toxicity` 값이 의미 있음, none이면 `tx.severe_irae` = none | 면역치료를 받지 않았으면 면역 관련 이상반응도 없음 | T2, T5, T6 | 검수 필요 |
| 19 | 조건부 | `tx.prior_docetaxel` = yes → `tx.prior_platinum` = yes 확률이 높음 | 도세탁셀은 대개 백금 치료 이후에 쓰인다는 가정 | T7 | 검수 필요 |
| 20 | 조건부 | `tx.recent_radiotherapy` = thoracic_gt30gy_6m → `cm.ild_pneumonitis` = radiation_pneumonitis 확률이 있음 | 흉부 방사선과 방사선 폐렴의 관계 가정 | T4 | 검수 필요 |
| 21 | 조건부 | `ecog`는 8개 시험이 모두 0–1만 받음 | 사전 분포에 2 이상을 넣지 않으면 ECOG로 탈락하는 환자가 생기지 않음. 분포 범위 결정 필요 | 전체 | 검수 필요 |
| 22 | 조건부 | `cm.hiv` = controlled_on_art → `cm.immunodeficiency` = acquired 와의 관계 | T2는 조절된 HIV를 허용하지만 "acquired immunodeficient states"는 제외함. 같은 환자를 두 슬롯이 어떻게 표현할지 결정 필요 | T2 | 검수 필요 |

## 전문가 판단 슬롯 (생성하지 않음)

`judge.life_expectancy`(T1·T2·T5·T6·T7)와 `judge.investigator_unsuitable`(T1·T2·T3·T5·T6)은 연구자 판단 기준입니다. 사전 분포와 정답을 만들지 않으며, 생성기에서는 이 기준이 걸린 쌍을 `EXPERT_REVIEW`로 두는 것을 전제로 합니다(M12). 원문은 `slot_candidates.csv`의 `judge.*` 행에 있습니다.
