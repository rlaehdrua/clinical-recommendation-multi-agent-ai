# WP2b 슬롯 결정 (NSCLC 8개 시험 코호트 생성기용)

> 2026-11-03 · 작성: 서연 · 브랜치 `feat/wp2a-nsclc-trials`
> 입력: `data/nsclc/cohort/slot_candidates.csv`(188행, 2026-11-03 기준), `lab_panels.md`, `cross_trial_conflicts.md`, `slot_combination_candidates.md`.
> 이 문서는 **슬롯 구조 결정**입니다. 분포·확률값은 정하지 않습니다.
> **의학적 해석은 모두 "검수 필요"**이며, 임상 검수 전에는 생성기 기본값으로 쓰지 않습니다.
> CT.gov 인용은 원문 그대로이며, 마크다운 이스케이프 역슬래시만 뺐습니다.

시험 약칭: T1 NCT07098338 · T2 NCT07027514 · T3 NCT07416058 · T4 NCT06216301 · T5 NCT05255302 · T6 NCT04892953 · T7 NCT06928389 · T8 NCT07680790

## 0. 결정 요약

| # | 결정 | 영향 받는 슬롯 | 검수 |
|---|---|---|---|
| D1 | 구동 유전자 슬롯 값은 `positive` / `negative` / `not_tested` 3개. `not_tested`를 음성으로 처리하지 않음. "모름"은 슬롯 값이 아님 | `bm.driver_alteration` | 검수 필요 |
| D2 | 크레아티닌 청소율은 파생 슬롯. 나이·체중·크레아티닌으로 계산하고 독립 샘플링하지 않음 | `lab.creatinine_clearance` | 검수 필요 |
| D3 | 조건부 기준용 슬롯 3개 추가: `dx.liver_mets`, `cm.gilbert`, `med.on_anticoagulant` | `lab.ast_alt_uln`, `lab.bilirubin_uln`, `lab.inr_aptt_uln` | 검수 필요 |
| D4 | 숫자 기준 없이 "Adequate … function"만 있는 시험은 `UNRESOLVED`. 다른 시험의 기준값을 빌려오지 않음 | T1, T4, T7의 검사 기준 | 검수 필요 |
| D5 | ECOG 사전 분포에 ≥2를 포함함. 비율은 서연이 결정하며, 이 문서에는 근거 후보만 정리 | `ecog` | 검수 필요 |
| D6 | `fact_llm` 기준(약 44%)은 WP4 부분 재평가 대상이 아니고, 전체 재평가로 폴백함 | `adjudication_candidate = fact_llm` 행 전체 | — |

---

## D1. 구동 유전자 슬롯: `positive` / `negative` / `not_tested`

### 값 정의

| 값 | 뜻 | 사실 저장 | 근거 수준 |
|---|---|---|---|
| `positive` | 검사했고 해당 변이가 있음 | 저장 | 검사 결과 |
| `negative` | 검사했고 해당 변이가 없음 | 저장 | 검사 결과 |
| `not_tested` | 검사를 하지 않았다는 **사실** | 저장 | 검사 이력 |
| (모름) | 환자가 검사 여부나 결과를 모름 | **저장 안 함** | 해당 없음: 응답 상태 |

- `not_tested`는 `negative`가 아닙니다. 생성기와 판정 모두 `not_tested`를 `negative`로 바꾸지 않습니다.
- "모름"은 슬롯 값이 아니라 **응답 상태**입니다. 생성기는 환자 슬롯에 "모름"을 샘플링하지 않습니다. 환자 시뮬레이터(WP6)가 답할 때만 응답 상태로 나타납니다.
  사실 저장소에 값으로 저장하지 않습니다(WP3 설계 `docs/dev/wp3_verdict_transition_design.md` §3, 브랜치 `docs/wp3-verdict-transition`).
- 변이 종류(EGFR ex19del/L858R, ALK, ROS1 …)는 `positive`일 때만 붙는 하위 값입니다. 기존 CSV의 `value_range`(`none / egfr_ex19del_L858R / … / not_tested`)에서 `none`은 `negative`에 해당합니다.
- **구조 결정 필요 (검수 필요)**: 3값을 유전자별로 둘지(`bm.egfr`, `bm.alk`, … 각각 3값), 구동 유전자 전체에 하나만 둘지.
  T1("Absence of sensitizing EGFR mutations or ALK rearrangements")과 T2("Have known EGFR or ALK mutations")는 EGFR과 ALK를 따로 묻습니다.
  그래서 "EGFR은 검사함, ALK는 미검사"를 표현하려면 유전자별 구조가 필요합니다. 이 경우 조합 후보 #4(단일 범주, 동시 양성 없음)도 다시 정해야 합니다.

### "Known"과 "Absence"의 차이: WP3 근거 수준으로 연결

| 기준 유형 | 예 | `negative` | `not_tested` | (모름) |
|---|---|---|---|---|
| Known 제외 | T2 "Have known EGFR or ALK mutations", T5 "Known EGFR activating tumor mutation …", T7 "Known actionable genomic alterations." | 제외 안 됨 | 제외 안 됨 (알려진 변이 없음) — **검수 필요** | 판정 안 됨 (정보 없음). "환자가 모름"은 "알려진 변이 없음"이 아님 |
| Absence 포함 | T1 "Absence of sensitizing EGFR mutations or ALK rearrangements", T6 "patients must not harbor any EGFR sensitizing mutations …" (선암만) | 충족 | **미정**: 검사가 필수면 불충족 후보, 아니면 정보 없음 — **검수 필요** | 판정 안 됨 |
| Documented 포함 | T8 "documented EGFR-TKI-sensitizing mutation", T3 "harbor confirmed actionable driver mutations" | 불충족 | 불충족 | 판정 안 됨 |

- 근거 수준은 WP3 설계 §3.3에서 쌍 상태로 연결합니다. 자기 보고만으로 불충족이면 확정 부적격이 아니라 확인 대기(`VERIFICATION_PENDING`)입니다(계약 0.2 `derive_pair_status`).
- T6는 조직형 조건이 붙습니다("For patients with histologies other than adenocarcinoma, EGFR and ALK status is not required."). 편평상피암에서 `not_tested`는 이 기준과 무관합니다. 다만 `dx.histology`와 조건부로 연결해야 합니다(조합 후보 #8).
- T4("… directed therapy is indicated or planned")는 변이 상태만으로 정해지지 않습니다. 치료 계획 조건이 붙기 때문입니다.

## D2. 크레아티닌 청소율: 파생 슬롯

- `lab.creatinine_clearance`는 샘플링하지 않고 `age`, `other.body_weight`, `lab.serum_creatinine`(신규 입력 슬롯, 이를 직접 요구하는 기준이 없어 CSV 행은 없음), `sex`로 계산합니다. 독립 샘플링하면 나이·체중과 맞지 않는 값이 나옵니다(조합 후보 #16).
- 계산식은 시험마다 다릅니다.
  - T5: "Creatinine Clearance ≥ 45 mL/min (Cockcroft or MDRD or CKD-epi)"
  - T6: "Measured creatinine clearance (CL) > 15 mL/min or calculated creatinine CL > 15 mL/min by the Cockcroft-Gault formula"
  - 생성기는 Cockcroft-Gault 값 하나를 기본으로 계산합니다. T5에서 식에 따라 판정이 갈리는 경계 환자는 따로 표시합니다. 어느 식을 기본으로 둘지와 MDRD·CKD-EPI 병행 여부는 **검수 필요**입니다.
  - MDRD·CKD-EPI는 체표면적 보정 eGFR(mL/min/1.73m²)이라 Cockcroft-Gault(mL/min)와 단위 정의가 다릅니다 — **검수 필요**.
- 판정 쪽에서는 계산값을 사실로 저장할 때 계산식과 입력 사실 ID를 함께 남겨야 합니다(입력이 바뀌면 다시 계산). 구현은 WP3·WP4 범위입니다.

## D3. 조건부 기준용 슬롯 추가

| 새 슬롯 | 값 | 바뀌는 기준 | 시험 · 원문 |
|---|---|---|---|
| `dx.liver_mets` | yes / no | `lab.ast_alt_uln` 상한 | T5 "patients with hepatic metastases or Gilbert's syndrome must have AST and ALT ≤ 5 x ULN and a baseline total bilirubin ≤ 2xULN" · T6 "unless liver metastases are present, in which case it must be =< 5 x ULN" |
| `cm.gilbert` | yes / no | `lab.bilirubin_uln` 상한 (T6), AST·ALT 상한 (T5) | T5 (위와 같은 문장) · T6 "This will not apply to patients with confirmed Gilbert's syndrome" |
| `med.on_anticoagulant` | yes / no (치료 용량 항응고) | `lab.inr_aptt_uln` 적용 여부 | T4 "For subjects not receiving therapeutic anticoagulation: INR or aPTT ≤ 1.5 x ULN" |

- `med.on_anticoagulant`와 기존 `med.anticoagulant`(none / warfarin / other_therapeutic, T2의 와파린 제외용)의 관계는 논리 제약 후보입니다. `med.anticoagulant ≠ none`이면 `med.on_anticoagulant = yes`입니다. 예방 용량 항응고를 "therapeutic"에서 뺄지, 두 슬롯을 하나로 합칠지는 **검수 필요**입니다.
- 조건 슬롯은 검사 수치보다 먼저 샘플링합니다. 수치 분포는 조건 슬롯 값에 따라 정합니다(조합 후보 #13·#14·#15).
- T6의 Gilbert 정의("persistent or recurrent hyperbilirubinemia that is predominantly unconjugated …")를 충족하는지는 임상 판단입니다. 판정 방식 후보는 `fact_llm`이고 **검수 필요**입니다.

## D4. "Adequate … function"만 있는 시험은 `UNRESOLVED`

| 시험 | 원문 | 결정 |
|---|---|---|
| T1 NCT07098338 | "Adequate bone marrow and organ function" | `UNRESOLVED` |
| T4 NCT06216301 | "Adequate hematologic and end-organ function" (응고만 기준값 있음 → D3) | 응고 외 `UNRESOLVED` |
| T7 NCT06928389 | "Adequate organ function." | `UNRESOLVED` |

- 다른 시험(T5·T6)의 기준값을 빌려오지 않습니다. 빌려오면 공개 기준에 없는 판정을 만드는 것입니다.
- 생성기는 이 기준의 정답 라벨을 만들지 않습니다. 이 기준만으로 적격 여부가 갈리는 쌍은 정답에서 "판정 불가"로 표시합니다. 쌍 상태는 확정되지 않으며, 프로토콜 본문을 확보하기 전까지 확인 대기 또는 임상의 판단으로 둡니다.
  둘 중 어디로 둘지는 계약 0.2 안건 1(`RuleMeta.adjudication`)에 따라 정합니다.
- T2·T3·T8은 공개 기준에 혈액검사 기준 자체가 없습니다. 기준이 없으므로 `UNRESOLVED`가 아니라 "해당 없음"입니다(`lab_panels.md`).
- CSV에는 위 3개 시험에 `lab.adequate_function` 행을 추가하고, `adjudication_candidate = unresolved`로 표시합니다(D4 전용 값).

## D5. ECOG ≥2를 사전 분포에 포함

- 8개 시험 모두 ECOG 0–1만 받습니다(CSV `ecog` 8행). 사전 분포에 ≥2가 없으면 ECOG로 탈락하는 환자가 생기지 않습니다. 그러면 ECOG 질문의 정보 가치가 0이 되어 정책 비교가 왜곡됩니다(조합 후보 #21).
- **비율은 서연이 결정**합니다. 아래는 근거 후보만 정리한 것이고, 값은 적지 않았습니다.

| 근거 후보 | 내용 | 장단점 |
|---|---|---|
| 실제 진료(real-world) NSCLC 코호트 문헌 | 진행성 NSCLC 진료 환자의 ECOG 분포 | 현실 비율. 문헌 선정과 값 확인 필요 — **검수 필요** |
| 임상시험 스크리닝 탈락 사유 문헌 | 스크리닝 단계 ECOG 탈락 비율 | 이 프로젝트(시험 사전 선별)에 가장 가까움. 자료가 적을 수 있음 |
| 설계값 + 민감도 분석 | 비율 몇 개(예: 낮음/중간/높음)로 실험을 반복 | 근거 없는 단일 값을 피함. 결과 해석은 상대 비교로 한정 |
| 현재 저장소 환자 세트 | `data/patients/synthetic`·`masked`에서 ECOG 언급은 0 또는 1뿐 (정규식 대략 집계, ≥2는 0건) | 현재 데이터가 ≥2를 포함하지 않는다는 근거일 뿐, 비율 근거는 아님 |

- ECOG 2 이상과 다른 슬롯의 상관(증상 있는 뇌전이, 고용량 스테로이드 등, 조합 후보 #12)은 비율과 함께 정해야 합니다.
- 비율이 정해지면 이 표의 출처를 `InformationSource.cost_basis`와 같은 방식(`assumed` / `literature` …)으로 표시하는 것을 제안합니다.

## D6. `fact_llm` 약 44%와 WP4 부분 재평가

| `adjudication_candidate` | 행 | 비율 |
|---|---|---|
| rule | 90 | 47.9% |
| fact_llm | 82 | 43.6% |
| expert | 16 | 8.5% |
| 합계 | 188 | |

(D4로 추가한 `unresolved` 3행은 위 집계에 넣지 않음)

- WP4 기준 단위 부분 재평가는 **규칙(`rule`) 기준만** 대상입니다. 새 사실이 들어오면 의존하는 `rule` 기준만 다시 계산합니다.
- `fact_llm` 기준은 부분 재평가 대상이 아닙니다. 의존 사실이 바뀌면 그 시험을 **전체 재평가로 폴백**합니다. LLM 판정의 입력 항목을 정확히 알 수 없기 때문입니다(`CriterionResult.inputs_used = None`).
- `expert` 기준은 재평가하지 않고 임상의 판단 대기(`EXPERT_REVIEW`)로 남깁니다.
- 영향: 기준의 약 44%가 폴백 대상이므로, 부분 재평가의 비용 절감 효과는 `rule` 비중이 높은 시험에서만 기대할 수 있습니다. 정확성 조건은 "부분 재평가 = 전체 재평가"(`compare_states`)입니다. 폴백은 이 조건을 지키기 위한 장치입니다.
- 이 비율은 판정 방식 **후보** 기준입니다. 계약 0.2 안건 1이 정해지면 다시 집계합니다.

## 7. CSV 반영 내용 (`slot_candidates.csv`, 별도 커밋)

- `note` 열: 위 D1(`bm.driver_alteration` 10행), D2(`lab.creatinine_clearance` 2행), D3(`lab.ast_alt_uln`·`lab.bilirubin_uln`·`lab.inr_aptt_uln`·`med.anticoagulant` T4 행), D5(`ecog` 8행)의 결정을 적었습니다. 기존 메모는 앞에 그대로 두고 ` · WP2b: …`로 덧붙였습니다.
- 새 행: `dx.liver_mets`(T5·T6), `cm.gilbert`(T5·T6), `med.on_anticoagulant`(T4), `lab.adequate_function`(T1·T4·T7, `unresolved`).
- `value_range` 등 다른 열은 바꾸지 않았습니다(D1의 3값 구조는 유전자별 구조 결정 뒤 반영). `review_status`는 모두 "검수 필요"입니다.

## 8. 남은 결정 (검수 회의)

1. D1 유전자별 3값 구조 채택 여부, 조합 후보 #4 수정.
2. Known 제외에서 `not_tested` 통과, Absence 포함에서 `not_tested` 처리.
3. D2 기본 계산식과 eGFR 단위 문제.
4. D3 `med.on_anticoagulant`와 `med.anticoagulant` 통합 여부.
5. D4 `UNRESOLVED` 쌍 상태(확인 대기 vs 임상의 판단) — 계약 0.2 안건 1 결정 후.
6. D5 ECOG ≥2 비율(서연).
