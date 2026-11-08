# NSCLC 데모 시나리오 (동겸 웹 데모용) — dba06d9 갱신본, integration/phase1-final 재확인

작성: 서연 (데이터) 2026-11-08 원본(70fe9a5, 4f81460 실행) → 갱신: 핸드오프 Agent C 2026-11-08 (dba06d9 = PR #53 head 실행)
데이터: `data/nsclc/eval` (240쌍) · 실행: #20 runner, full_dcvc, 6000분, horizon 80, seed 1 · `scripts/demo_nsclc_scenarios.py` (기록 필드와 C2를 더한 사본)
상세 비교·근거: `C:/Users/jsw58/Desktop/trialog-seoyeon-handoff/05_DEMO_SCENARIOS_UPDATED.md`

> 합성 환자입니다(실제 환자 아님). 데이터 검증 완료 / 임상 검수 미완료. GT는 규칙 GT이며 축소 범위 안의 뜻입니다.
> 분은 시뮬레이터 비용표의 **가정 시간**입니다. 아래 A~D는 **오프라인 runner 기록**으로 보여 주는 시나리오이고, 웹 화면에서는 만들 수 없습니다
> (데모 서버 카탈로그는 합성 시험 T-A~T-D뿐이고, NCT 시험은 `trial_not_found` 404). 웹 클릭 시연은 `docs/dev/web_demo_flow.md`(DEMO-NSCLC-1~4)를 따릅니다.

| # | 환자 × 시험 | 보여 줄 것 | GT 라벨 | dba06d9 최종 상태 | 일치 |
|---|---|---|---|---|---|
| A | P052 × NCT07680790 | 판단 기준 외 확인을 마치고 담당의 판단으로 넘김 | 전문가 판단 대기 적격 | EXPERT_REVIEW | ✓ |
| B | **P052** × NCT06928389 | 자기 보고 "백금 치료 없음"을 투약 목록으로 확인한 뒤 부적격 확정 | 부적격 (I8) | CONFIRMED_INELIGIBLE | ✓ |
| C | **P058** × NCT07416058 | '모름'·연락 불가로 값을 얻지 못하면 확정하지 않음 | 미평가 (범위 안 미연결 기준) | INFO_MISSING | ✓ (확정하지 않음) |
| D | DEMO-REGION × NCT06216301 | 국가를 모르면 부적격으로 확정하지 않음 | 미평가 (지역 조건) | **INFO_MISSING** (I1 UNKNOWN) | ✓ (5fa4e24에서 고침) |

데모 환자: P052·P058·DEMO-REGION 3명 (P045는 B 대체안으로 계속 재현됨)

> **integration/phase1-final 재확인 (2026-11-08):** 같은 명령으로 다시 실행했습니다(`data/nsclc/eval/checks/demo_scenarios_phase1-final.json`). A~D의 환자·시험·최종 상태는 위 표와 같습니다. 바뀐 것은 두 가지입니다. P052의 가정 코디네이터 분이 56.5 → 55.5가 되었고, C의 투약 목록 요청 step 번호가 3씩 당겨졌습니다(1838–1841 → 1835–1838). 원인은 확인하지 않았습니다. 같은 브랜치에 들어간 문서 소진 뒤 행동 보류 수정(87f5f07)과 관련이 있을 수 있습니다. 판정에는 영향이 없습니다.

**바뀐 점 (4f81460 → dba06d9)**
- B: 자동 선택이 P045 → P052로 바뀌었습니다(같은 조건에서 행동 기록이 더 짧음, 12 대 13).
- C: P061이 빠졌습니다. 예전 선택 근거는 남성에게 임신 질문(E16, d03c39e에서 고침)이었고, "PD-L1 모름 → 병리 결과지" 장면은 E12 치료력을 바이오마커로 잘못 묶은 결과였습니다(39ec3d2에서 고침).
- C: 240쌍 전체에서 **"모름 → 같은 항목 서류 요청 → 새 값 확보"인 쌍은 0개**입니다. 그래서 C는 "확보하지 못하면 확정하지 않음" 장면으로 바꿨습니다. "서류로 확보" 장면은 웹 데모의 '모르겠어요' → 서류 확인 대체 요청으로 보여 줍니다.
- D: 결함이 고쳐져 정상 동작으로 시연할 수 있습니다.

---

## A. 판단 대기 적격 — P052 × NCT07680790

초기 입력 (공개 값): 69세 남성, 백금 없음, docetaxel 없음, anti-PD-1/PD-L1 있음, 4주 내 다른 시험약 없음, 큰 수술 없음

| 순서 | 행동 (step) | 결과 | 상태 |
|---|---|---|---|
| 1 | 진료기록 ECOG (22, 가정 18분) | ECOG 1 | I5 MET |
| 2 | 병리 결과지 EGFR (402, 11분) | exon19del/L858R | I3 MET |
| 3 | 진료기록 — NCT06216301 E1용 (403, 1분) | 조직형 선암, 이 시험 I1에도 반영 | **INFO_MISSING → EXPERT_REVIEW** |
| 4 | 진료기록 I1 (408, 1분) | 연락 불가 (영향 없음) | EXPERT_REVIEW |

남은 기준: I2 (근치 치료 불가 판단), E3 (임상적으로 의미 있는 심장 질환) — 담당의 판단. 문구: "판단 기준 외 확인 완료. 담당의 판단 2건 대기" (확정 적격 아님)

## B. 부적격 확정 — P052 × NCT06928389

원문 I8: "Previously received systemic platinum-based chemotherapy and PD-1/L1 inhibitors." (백금 = I8, PD-1/L1 = I9로 나눔) → GT I8 FAIL (`tx.prior_platinum=no`)

| 순서 | 행동 (step) | 결과 | 상태 |
|---|---|---|---|
| 0 | 자기 보고 "백금 치료 없음" | 미검증 | VERIFICATION_PENDING |
| 1 | 진료기록 ECOG (22, 18분, A와 같은 요청) | ECOG 1 | I4 MET |
| 2 | 진료기록 E1 (1582, 1분) | 새 사실 0 (조직형 이미 있음) | VERIFICATION_PENDING |
| 3 | 투약 목록 basis:criterion:NCT06928389:I8 (1767, 6.5분) | 새 사실 0 = 기존 자기 보고를 기록으로 확인 | **→ CONFIRMED_INELIGIBLE** |

문구: "백금 치료 이력 없음 (투약 목록으로 확인) — 기준 I8 불충족으로 부적격 확정". 판정 칸의 `llm`은 오프라인 범주 판정기이고 실제 LLM이 아닙니다.
대체안: P045 (ECOG 2 → 진료기록 basis:ecog, step 1726, 3분 → CONFIRMED_INELIGIBLE)

## C. 값을 얻지 못하면 확정하지 않음 — P058 × NCT07416058

공개 값: 63세 여성, 큰 수술 없음, PD-1/L1 치료 있음, 임신·수유 '예'(자기 보고)
GT NOT_EVALUATED: unbound I7 (면역치료 전체 vs 슬롯은 면역관문억제제만), I8 (PD-L1 TPS ≥ 50%, 중앙검사·클론 조건), E14·E15 (임신·수유를 한 슬롯으로 합침)

| 순서 | 행동 (step) | 결과 | 상태 |
|---|---|---|---|
| 1 | 진료기록 ECOG (17, 18분) | 연락 불가 | I9 UNKNOWN |
| 2 | 병리 결과지 드라이버 변이 (61, 11분) | EGFR 변이, 새 사실 1 | I4 MET |
| 3 | 환자 질문 I6 "마지막 항암 후 2주 경과" (69, 1분) | **'모름'** | I6 UNKNOWN |
| 4 | 진료기록 I3 병기 (68, 1분) | 연락 불가 | I3 UNKNOWN |
| 5 | 투약 목록 I6 (1837, 0.5분; dba06d9에서는 1840) | 처리됨, **새 사실 0** | I6 여전히 UNKNOWN |
| 끝 | - | 값 없는 기준 I3·I6·I9, 미연결 I7·I8·E14·E15, 판단 E5·E16·E17 | **INFO_MISSING** |

문구: "확인하지 못한 항목과 원문만으로 판정할 수 없는 항목이 남아 확정하지 않습니다". **"모르는 값을 서류로 확보했다"고 말하지 않습니다.**
예상 질문: "임신 중이면 제외 아닌가?" → "이 시험의 임신·수유 기준은 슬롯 표현 문제로 범위 안에서 판정하지 않는 기준(미평가)이고, 시스템도 확정하지 않습니다."
관찰 (보고만): E14는 공개 값이 있는데도 다시 물었습니다. 투약 목록 요청 4건이 성공으로 기록됐지만 새 사실은 0건입니다(원인 미확인).

## D. 지역 조건 — DEMO-REGION × NCT06216301 (평가셋 밖 데모 환자)

초기 입력: 20세 여성, 국가 미상 (나이·성별 문서 확인됨). 원문: "≥22 years of age in the USA / ≥18 years of age outside of the USA."

| | 상태 |
|---|---|
| GT 기대 | NOT_EVALUATED (I1 지역 조건부, 국가 미상) |
| 4f81460 | CONFIRMED_INELIGIBLE (I1 NOT_MET) — 결함 |
| **dba06d9** | **INFO_MISSING, I1 UNKNOWN, mismatch_basis 없음** (5fa4e24) |

문구: "국가를 모르면 부적격으로 확정하지 않고, 국가를 추측하지도 않습니다." 한계: 환자 정보에 국가 항목이 없습니다. 두 조건 모두에 못 미치는 나이(예: 17세)도 이 시험에서는 보류됩니다(팀 결정 CL-C1).

## 재현

```bash
export PYTHONPATH=$PWD/src PYTHONIOENCODING=utf-8 ANTHROPIC_API_KEY=
python scripts/demo_nsclc_scenarios.py --out <출력 경로>.json   # 약 7분. 결과에 assumed_*_min, new_facts, C2(null) 포함
```
