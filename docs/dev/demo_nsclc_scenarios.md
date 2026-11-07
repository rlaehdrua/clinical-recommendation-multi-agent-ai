# NSCLC 데모 시나리오 (동겸 웹 데모용)

작성: 서연 (데이터) · 2026-11-08 · 데이터: `data/nsclc/eval` (240쌍) · 확인 실행: 통합본 `4f81460` + #43, #20 runner, full_dcvc, 6000분, horizon 80, seed 1
실행 기록: `data/nsclc/eval/checks/demo_scenarios_4f81460.json` (`scripts/demo_nsclc_scenarios.py`)

> 합성 환자입니다(실제 환자 아님). 데이터 검증 완료 / 임상 검수 미완료. GT는 규칙 GT이며 축소 범위 안의 뜻입니다. 웹·엔진 코드는 바꾸지 않았습니다.

| # | 환자 × 시험 | 보여 줄 것 | GT 라벨 | 오프라인 실행 최종 상태 | 일치 |
|---|---|---|---|---|---|
| A | P052 × NCT07680790 | 판단 기준 외 확인을 모두 마치고 담당의 판단으로 넘어감 | 전문가 판단 대기 적격 | EXPERT_REVIEW | ✓ |
| B | P045 × NCT06928389 | 공개된 값을 기록으로 확인해 부적격 확정 | 부적격 | CONFIRMED_INELIGIBLE | ✓ |
| C | P061 × NCT06216301 | 환자가 "모름" → 결과지·기록으로 확보 | 미평가 (범위 안 미연결 기준) | INFO_MISSING | ✓ (확정하지 않음) |
| D | DEMO-REGION × NCT06216301 | 지역 조건: 국가를 모르면 확정 금지 | 미평가 (지역 조건) | **CONFIRMED_INELIGIBLE** | ✗ 알려진 결함 (P0-3) |

데모 환자는 P052·P045·P061·DEMO-REGION 4명입니다.

---

## A. 판단 대기 적격 — P052 × NCT07680790 (EGFR 변이 비편평 NSCLC 시험)

**초기 입력 (공개 값)**
- 69세 남성
- 이전 치료: 백금 항암제 없음, docetaxel 없음, anti-PD-1/PD-L1 치료 있음
- 4주 내 다른 임상시험 약 없음, 큰 수술 없음

**연구진이 요청할 항목 (숨긴 값)과 확보 경로**

| 항목 | 정보원 | 실제 값 | 실행 기록 |
|---|---|---|---|
| ECOG | 진료기록 | 1 | step 25 REQUEST_DOCUMENT medical_record → 성공 |
| EGFR 변이 | 병리 결과지 (환자가 모르는 항목) | exon 19 결실/L858R | step 431 REQUEST_DOCUMENT pathology_report → 성공 |
| 조직형 | 진료기록 | 선암 | step 433 진료기록 요청으로 확보, 이 시점에 EXPERT_REVIEW 전이 |

**기대 상태 변화:** INFO_MISSING → (ECOG·EGFR·조직형 확인) → **EXPERT_REVIEW**

**GT 근거 (`rule_gt.csv`)**

| 기준 | 원문 | GT |
|---|---|---|
| I1 | "Participants must have histologically or cytologically confirmed non-squamous NSCLC" | PASS (선암) |
| I3 | "Participants must have documented EGFR-TKI-sensitizing mutation (exon 19 deletion or exon 21 L858R substitution)" | PASS |
| I5 | "Participants must have ECOG Performance Status 0-1" | PASS (1) |
| I6·I7 | 구조화 항목 18세 이상·성별 제한 없음 | PASS |
| I2 | "... or recurrent disease not amenable to curative surgery or definitive radiotherapy ..." | NEEDS_EXPERT |
| E3 | "Participants must not have clinically significant cardiac disease" | NEEDS_EXPERT |

→ 판단 기준 2개만 남음 = 전문가 판단 대기 적격

데모 문구 예시
- "판단 기준 외 확인 완료. 담당의 판단 2건(병기·치료 적합성, 심장 질환) 대기"
- 확정 적격으로 표시하지 않음

## B. 부적격 확정 — P045 × NCT06928389 (백금·PD-1/L1 치료 후 2차 시험)

**초기 입력 (공개 값)**
- 57세 남성
- ECOG 2 (자기 보고·미검증)
- 백금 항암제 있음, docetaxel 없음, 큰 수술 없음

**연구진이 요청할 항목**

| 항목 | 정보원 | 실행 기록 |
|---|---|---|
| ECOG 기록 확인 | 진료기록 | step 1713 REQUEST_DOCUMENT medical_record (basis:ecog) → 성공 → **VERIFICATION_PENDING → CONFIRMED_INELIGIBLE** |
| (그 전) E1 소세포암 여부 | 진료기록 | step 1446 연락 불가 (실패) |
| (그 전) I9 PD-1/L1 치료력 | 환자 질문 | step 1447 응답 |

**기대 상태 변화:** VERIFICATION_PENDING → (ECOG 2를 진료기록으로 확인) → **CONFIRMED_INELIGIBLE**

**GT 근거:** I4 "Eastern Cooperative Oncology Group (ECOG) performance status of 0 or 1." → ecog=2 FAIL. 이 환자의 다른 두 시험(NCT07416058, NCT07680790)도 같은 ECOG 기준으로 확정 부적격입니다.

데모 문구 예시
- "ECOG 2 (진료기록 확인) - 기준 0-1 불충족으로 부적격 확정"
- 자기 보고만으로는 확정하지 않다가, 기록 확인 뒤 확정하는 흐름을 보여 줍니다.

## C. 모름 → 결과지·기록으로 확보 — P061 × NCT06216301

**초기 입력 (공개 값)**
- 59세 남성
- 백금 항암제 있음, 다른 면역관문억제제(CTLA-4 등) 치료 있음, 마지막 항암 14일 이상 전

**요청 항목과 환자 응답 (실행 기록)**

| step | 행동 | 결과 |
|---|---|---|
| 43 | ASK_PATIENT: PD-L1 관련 (`biomarker:PD-L1`) | **환자 "모름"** (no_answer) |
| 1822 | REQUEST_DOCUMENT: 병리 결과지 (pathology_report) | 성공 — 환자가 모르는 값을 결과지로 확보 |
| 917 | ASK_PATIENT: E16 임신·수유 | 환자 "모름" |
| 2112 → 2401 | 처방 목록 요청 연락 불가 → 진료기록 요청 성공 | 기록으로 확보 |

**기대 상태 변화:** INFO_MISSING 유지
- 이 시험에는 범위 안 미연결 기준(I8 "적절한 장기 기능" 기준값 없음, E11, E12)이 남아 있습니다.
- 그래서 확보 행동이 성공해도 확정하지 않는 것이 맞습니다. GT도 NOT_EVALUATED입니다.

**GT 근거:** I8 "Adequate hematologic and end-organ function ..." → NOT_EVALUATED(원문에 기준값 없음). E12 "... in the 12 months prior to randomization" → NOT_EVALUATED(시점 슬롯 없음).

데모 문구 예시
- "환자가 모르는 항목은 결과지로 확인했습니다"
- "남은 기준 중 원문만으로 판정할 수 없는 항목이 있어 확정하지 않습니다"

관찰 (엔진 쪽, 보고만)
- step 43의 확인 항목이 `biomarker:PD-L1`입니다. 하지만 E12의 원문은 PD-1/PD-L1 **치료력**(12개월)입니다. 미연결 기준이라 슬롯 지정이 없어 엔진이 문장에서 항목을 추정한 것으로 보이며, 원인은 확인하지 않았습니다.
- P061은 남성인데 E16(임신·수유)을 환자에게 묻고 문서를 요청했습니다. GT는 해당 없음(NOT_APPLICABLE)입니다. 성별이 공개 값인데도 확인 행동이 생긴 원인은 엔진·판정기 쪽이라 확인 필요입니다.
- 데모에서는 PD-L1 → 병리 결과지 흐름만 보여 주는 것을 권장합니다.

## D. 지역 조건 — DEMO-REGION × NCT06216301 (평가셋 밖 데모 환자)

- 평가셋 240쌍에는 NCT06216301 후보 중 22세 미만 환자가 없어 이 경우가 생기지 않습니다.
- 그래서 P0-3 회귀 케이스(`tests/data/p0_3_region_cases.json`, branch `data/p0-eval-cases`)의 환자를 씁니다.

**초기 입력:** 20세 여성, 국가 미상 (나이·성별 문서 확인됨)

**원문:** "≥22 years of age in the USA / ≥18 years of age outside of the USA."

| | 상태 |
|---|---|
| GT 기대 | NOT_EVALUATED (I1 지역 조건부, 국가를 모름) |
| 통합본 4f81460 실행 | **CONFIRMED_INELIGIBLE** (I1 NOT_MET, mismatch_basis NCT06216301:I1) |
| P0-3 제안 패치 적용 시 | INFO_MISSING (확정하지 않음) |

데모 권장
- 현재 엔진으로는 이 시나리오를 '정상 동작'으로 보여 줄 수 없습니다.
- 한계 슬라이드에 "알려진 결함·제안 패치 있음"으로 넣거나, 패치 병합 후 시연을 권장합니다.

## 재현

```bash
export PYTHONPATH=$PWD/src PYTHONIOENCODING=utf-8 ANTHROPIC_API_KEY=
python scripts/demo_nsclc_scenarios.py --out data/nsclc/eval/checks/demo_scenarios_4f81460.json   # 약 10분
```
