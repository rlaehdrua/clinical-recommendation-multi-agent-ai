# 최종 검수 — 실험 결과·발표 자료 (2026-11-08 21시, 선우 요청)

작성: 서연 · 기준: 선우 통합본 `urgent/integration` `8afe5ea` (2026-11-08 20:50) · 검수 범위: 읽기 + 허용된 확인 실행(P0 채점, 데모 A~D 재확인)
> **데이터 검증 완료 / 임상 검수 미완료.** GT는 축소 범위 규칙 GT (임상 전문가 미검수). 정확도·성능 향상 표현을 쓰지 않습니다. 실험(정책 비교·ablation)은 다시 돌리지 않았고, 코드·GT 라벨·데이터는 바꾸지 않았습니다.

## 발표 문서 최종 정리 요약 (11절)

| 항목 | 결과 |
|---|---|
| 표기 | 07cb2e0·60b41da 충돌을 규칙 하나로 통일: 1~4 = 평가셋 생성 `996efd6` / 5 = 최종 통합본 `b1d2a7f` · `dba06d9`·`8afe5ea` 동일 결과 / 5-1 = 최종 통합본 `b1d2a7f` · `8afe5ea` 동일 결과 / 6 = 11/08 dba06d9 기준 · 최종 통합본 실험 결과 아님 |
| 번들링 결론 | "예산에 따라 방향이 달라짐: 90분은 dcvc 우세, 400·800분은 번들링 제거 우세, 6000분은 차이 없음" |
| 행동 수 | 발표 문서에서 제거, 검수 기록에는 노트로만 |
| 수치 | 바뀐 값 없음 (실행·채점 없음) |

## 발표 수치 고정 요약 (24시, 원자료 대조 → 10절)

| 항목 | 결과 |
|---|---|
| 원자료 불일치 | **0건 / 대조 21항목.** 슬라이드 6 ablation 값은 result.json 243개(실행 환경 commit 전부 `dba06d9`)·`wp6_eval_*.json` 재집계와 모두 같음 |
| 최종 표기 규칙 (24시판, **11절 규칙으로 대체**) | 슬라이드 5 = "`dba06d9` 기준 · `8afe5ea` 동일 결과 확인", 5-1 = "`8afe5ea` 기준 · `b1d2a7f` 동일"이었음 → 07cb2e0과 충돌해 11절에서 하나로 통일 |
| E2E | 추정 → 확정: `d20cc0d`, SYN-04 × T-C, 3/11 → 4/10, 안전 위반 0 (선우 확인) |
| 변경 위치 | `presentation_data_gt.md` 머리말·슬라이드 5·5-1·6·바뀐 점 표 / `demo_final_flow.md` 4·5절 / 이 문서 8-0·8-4·10절, 원자료 문구 4곳 |

## 최종 통합 브랜치 대조 요약 (23시, `integration/phase1-final` `b1d2a7f` → 9절)

| 항목 | 결과 |
|---|---|
| 누락 | **4건** (모두 내 문서 브랜치의 21시 이후 커밋: `eba0fc6`·`4fbe82b`의 presentation 최종본·demo_final_flow·final_review·근거 파일). 코드·데이터·테스트·결정 5 패치·P0 사례는 모두 포함 |
| 수치 변경 | **없음.** 240쌍 400분 47, 6000분 20/23·124/130, 거짓 확정 0, 라벨 0/23/130/87, 설정 해시 `a549da3b1171fd52`, P0 22/127 모두 `8afe5ea`와 같음 (400분 result_hash도 같음) |
| 전체 테스트 | **6,254 passed / 0 failed / 9 skipped / 4 xfailed** (32분) |
| 문서 정정 | C(P058) "처방 목록으로 확보"는 오류 (새 사실 0) → 고침. B는 통합본과 같이 P052, P045는 대체안 |

## 최신 갱신 요약 (22시, 선우 `dba06d9` 재실험 보고서 대조 → 8절)

| 항목 | 결과 |
|---|---|
| 불일치 | 30개 항목 중 **불일치 5 / 코호트 다름 12** / 일치 13. 모두 내 문서의 `3e4b2fd`·`4f81460` 값이고, `8afe5ea`(240쌍) 또는 `dba06d9`(ablation) 값으로 고침 |
| 400분 47 vs 48 | **엔진 버전 차이.** `4f81460` 엔진 48 → `dba06d9`·`8afe5ea` 47 (result_hash 같음, 재현 안정). 갈린 쌍 P034 × NCT07416058, 원인 후보 `39ec3d2`. 발표는 `8afe5ea` 47, 48은 노트 |
| 데모 영향 | **없음** (메인 W1, 보조 A·B). E2E "확정 부적격 4 / 미확정 10"은 보고서·통합본에서 못 찾음 → 선우 확인 필요 (정황상 `d20cc0d`). 백업 E2E 표 숫자만 리허설 때 다시 확인 |
| 표기 | ablation 수치는 모두 "11/08 dba06d9 기준, 최종 통합본 실험 결과 아님". 원자료 대조 완료 (24시, 10절) |

아래 요약·0~7절은 21시 검수 내용이며, 바뀐 수치는 그 자리에서 고쳤습니다.

## 요약 (21시 검수)

| 항목 | 결과 |
|---|---|
| 수치 불일치 | **9건** (발표 초안 5, 데모 문서 4). 슬라이드 1~5의 데이터·라벨·실행 수치 19개 항목은 모두 일치. 불일치는 모두 통합본의 엔진 수정으로 바뀐 P0·지역 조건·데모 실행 기록. 내 문서는 직접 고침 |
| 성능 주장 | 내 문서 **삭제 1 / 수정 3**, 남의 문서 **수정 필요 4**. DCV·번들링 효과 주장은 내 문서에 없음 → 한계로 명시 |
| DCV·번들링 | (11/08 dba06d9 기준 · 최종 통합본 실험 결과 아님) no_dcv = dcvc (측정한 7개 조건 전부). 번들링 제거와의 차이는 예산에 따라 방향이 달라짐: 90분은 dcvc 우세, 400·800분은 번들링 제거 우세, 6000분은 차이 없음 (11절에서 보정) → "이번 실험 조건에서 차이 없음 또는 방향이 바뀜, 추가 검증 필요" |
| GT 명칭 | 내 문서 3개 통일. 남의 문서는 대상이 다른 GT라 구분 표기 권장 |
| known_defect | **충돌 있음.** 선우 `5fa4e24`로 P0-3이 고쳐져 strict xfail 2건이 XPASS로 실패. 해제 패치(2줄) 제안 → 적용 시 13 passed |
| P0 127건 (합성 사례·오프라인 스텁 기준) | 오채택 36 → 22, 상충 탐지 10/27 → 15/27, 과잉 보류 4 → 4. 나빠진 사례 0 |
| 확정 데모 흐름 | 웹 W1(DEMO-NSCLC-1, 묶음 요청 → 서류 확인 → 의사 판단) 메인 + 오프라인 A(P052 EXPERT_REVIEW)·B(P045 기록 확인 후 부적격) 보조. D는 한계 설명용, C는 질문 대비용 |
| 위치 미확인 | ① (22시 해소) DCV+C·ablation 재실험은 선우 `dba06d9` 03·04 보고서, 원자료는 24시 `share/seoyeon-experiment-raw-dba06d9` `ce4a8e1` `docs/experiments/seoyeon-handoff-dba06d9/raw/sy_b/` (10절) ② W1~W3 번호가 붙은 문서 |

## 0. 위치 파악

| 대상 | 위치 | 생성 커밋·시각 | 비고 |
|---|---|---|---|
| 선우 최종 통합 브랜치 | `origin/urgent/integration` `8afe5ea` | 2026-11-08 20:50 | `integration/dcvc` `471a9ee`(16:29) + 선우 P0-B·지역 조건·E12·성별·GT 도구 + 동겸 웹 수정 |
| DCV+C·ablation 결과 (최신 저장본) | `docs/dev/p_results/{demo,nsclc_pilot,generator_neph_k3}.{tsv,json}`, 보고서 `docs/dev/p_common_engine_2026_11_07.md` | `3e4b2fd` 2026-11-07 20:44 (선우) | 통합본 엔진 수정 **이전** 결과. 그 뒤 재실행 결과 파일은 모든 원격 브랜치에서 **위치 미확인** |
| DCV+C·ablation 재실험 (22시 추가) | `share/seoyeon-experiment-reports-dba06d9` `9807a22` 03·04 보고서 | 측정 `dba06d9` 2026-11-08 16:35, 공유 21:46 (선우) | 원자료 없음(선우 로컬). 8절 |
| NSCLC 평가셋 실행 결과 | 내 `data/nsclc/eval/checks/runner_*_4f81460.json` (`9e79820`, 11-08 02:33) | 4f81460 | 이번에 데모 재확인과 같은 실행으로 `8afe5ea` 값도 확인 (5절) |
| 자동 실행(e2e) 결과 | `README.md` 11-08 "끝까지 자동 실행" 표 (`7c282b2`, 14:23, 동겸) | | 가상 시험 T-A~D, 결과 파일 `outputs/e2e/`는 git 밖 |
| 발표 자료 | 내 `docs/dev/presentation_data_gt.md`, `demo_nsclc_scenarios.md`, `nsclc_eval_dataset.md` (`docs/presentation-data-gt`) / 통합본 `docs/dev/web_demo_flow.md`, `docs/demo_capture.md`(중간발표용), `docs/team_handoff.md`, `README.md` 변경 이력 | | 별도 슬라이드 파일(.pptx 등)은 저장소에 없음 |
| 웹 데모 시나리오 W1~W3 | W 번호 문서는 **위치 미확인**. `web_demo_flow.md` §2·§4와 `README.md` 11-08 브라우저 확인 기록 3건을 W1~W3로 대응 (5절, 동겸 확인 필요) | `f1c56ef` 15:30, `894747a` 13:42, `d20cc0d` 20:43 | |

## 1. 수치 대조

대조 방법: 통합본 `8afe5ea` worktree의 파일을 스크립트로 다시 셈 (`docs/dev/final_review_scripts/verify_numbers.py`). 평가셋 파일(`data/nsclc/eval`, `data/nsclc/review`)은 `46e011e` 이후 바뀌지 않았습니다.

### 1-1. 발표 초안 `presentation_data_gt.md`

| 위치 | 발표 자료 값 | 최신 값 | 출처 파일 | 판정 |
|---|---|---|---|---|
| 슬1 프로토콜 | 8건, 조회 2026-11-02 | 8, 2026-11-02 | `data/nsclc/trials/*.json` | 일치 |
| 슬1 파싱 | 완료 6 / 미완료 2 | 6 / 2 | `protocol_status.csv` | 일치 |
| 슬1 기준 | 160 | 160 | `criteria_verification.csv` | 일치 |
| 슬1 환자 | 합성 80 | 80 | `patients.json` | 일치 |
| 슬1 라이선스 | 확인 필요 | – | – | 출처 없음 (그대로 "확인 필요") |
| 슬2 범위 안 | 72 / 160 | 72 | `criteria_verification.csv` `in_scope` | 일치 |
| 슬2 나이·성별·ECOG | 19 (지역 조건부 1) | 19 (`in_rule_engine`, region_conditional 1) | 같음 | 일치 |
| 슬2 범주형 | 34 (연결 23, 미연결 11) | 34, 23, 11 | 같음 | 일치 |
| 슬2 판단 기준 | 19 | 19 | 같음 | 일치 |
| 슬2 excluded / 범위 밖 | 9 / 79 | 9 / 79 | 같음 | 일치 |
| 슬2 원문 대조 | 60 / 11 / 1 | 60 / 11 / 1 | 같음 `result` | 일치 |
| 슬2 검사 수치 기준 | 0건 | 0 | `lab_reference.json` | 일치 |
| 슬3 엔진 import | 0 | 테스트 2건 통과 | `tests/test_nsclc_eval_gt.py` (20 passed) | 일치 |
| 슬3 순환 라벨 일치 | 237 / 240 | 237 / 240 | `checks/circular_vs_independent_4f81460.json` | 일치 |
| 슬4 쌍·시드·해시 | 240, 20261107, `a549da3b1171fd52` | 240, base_seed 20261107, 같음 | `manifest.json` | 일치 |
| 슬4 라벨 | 0 / 23 / 130 / 87 | 0 / 23 / 130 / 87 | `label_summary.json`, `pair_gt.csv` | 일치 |
| 슬4 runner 400분 | no_action 0·0·0·0, full_dcvc 0·48·0·0 | 4f81460 같음 / **8afe5ea 47** (22시 확인) | `checks/runner_400m_h10_4f81460.json`, `final_review_evidence/r400_three_commits.json` | 22시 **불일치** → 47로 교체 (8-2) |
| 슬4 runner 6000분 | 20 / 23, 124 / 130, 0, 0 | 4f81460 같음, **8afe5ea도 같음** | `checks/runner_6000m_h80_4f81460.json`, `final_review_evidence/runner_6000m_h80_8afe5ea_from_demo.json` | 일치 |
| 슬4 결함 사례 | 거짓 부적격 6 → 0 | 같음 | `nsclc_eval_dataset.md` §7 D7 | 일치 |
| 슬5 판단 대기 비율 | 9.6% | 23/240 = 9.58% | `label_summary.json` | 일치 |
| 슬5 P0 사례 수 | "48건" | **127건** (102 + held-out 25) | `data/p0-eval-cases` README | **불일치** → 127 |
| 슬5 지역 조건 | "20세 거짓 부적격 2케이스" | **0** (8afe5ea INFO_MISSING) | `final_review_evidence/p0_3_region_cases_8afe5ea.txt` | **불일치** → 문장 삭제, 수정 사례로 이동 |
| 슬5 held-out 상충 | 오채택 6 / 15 | **1 / 15** | `final_review_evidence/p0_fact_cases_8afe5ea_score.md` | **불일치** → 최신 값 |
| 슬5 held-out 전해 들은 값 | 오채택 3 / 10 | **0 / 10** | 같음 | **불일치** → 최신 값 |
| 슬5 P0 전체 | 오채택 36, 과잉 보류 4, 같은 항목 7/8 | **22**, 4, 7/8 | 같음 | **불일치 1** (36 → 22, 합성 사례·오프라인 스텁 기준), 나머지 일치 |

### 1-2. 데모 문서 `demo_nsclc_scenarios.md`

| 위치 | 문서 값 | 최신 값 (8afe5ea) | 출처 | 판정 |
|---|---|---|---|---|
| A 실행 step | ECOG 25, EGFR 431, 조직형 433 | 22, 402, (408 시점 EXPERT_REVIEW) | `final_review_evidence/demo_scenarios_8afe5ea.json` | **불일치** (step 번호만, 상태 같음) |
| B 실행 step | ECOG 근거 확인 1713 | 1726 | `demo_pairs_8afe5ea.json` | **불일치** (step 번호만) |
| C 흐름 | P061 PD-L1 "모름" → 병리 결과지 | 이 흐름 없음 (E12 항목 수정). P058로 교체 | 같음 | **불일치** → 시나리오 교체 |
| D 상태 | CONFIRMED_INELIGIBLE ✗ | INFO_MISSING | `demo_scenarios_8afe5ea.json` `D_region` | **불일치** → 갱신 |
| A·B·C 최종 상태 | EXPERT_REVIEW / CONFIRMED_INELIGIBLE / INFO_MISSING | 같음 | 같음 | 일치 |

### 1-3. 남의 발표 관련 문서 (보고만)

| 위치 | 값 | 최신 결과 | 판정 |
|---|---|---|---|
| 선우 `p_common_engine_2026_11_07.md` ablation 표 | `p_results/*.tsv`와 같음 | 같은 파일 (`3e4b2fd`), 22시 `dba06d9` 보고서로 대체 | 일치. 단 통합본 엔진 수정 이전 결과 (6절 S4) |
| 동겸 `README.md` 자동 실행 표 | offline 확정 1+6, 참여 불가 3 / llm 5, 안전 위반 0 | 결과 파일이 git 밖(`outputs/e2e/`) | **출처 없음** (저장소에서 대조 불가) |
| 동겸 `README.md` 서류 기록 수정 | 확정 1 → 4 | 같음 | 출처 없음 (같은 이유) |
| 동겸 `web_demo_flow.md` §3 | 81.5분 대 81.5분, 4명일 때 13분 | 화면 계산값 기록 | 출처 없음 (브라우저 기록) |

## 2. 성능 주장 정리

### 2-1. no_dcv = dcvc, 번들링 제거와의 차이 방향 (결론: 예산에 따라 방향이 달라짐: 90분은 dcvc 우세, 400·800분은 번들링 제거 우세, 6000분은 차이 없음)

출처: 선우 `03_LATEST_EXPERIMENT_RESULTS.md`·`04_DCV_ABLATION_ANALYSIS.md` (`share/seoyeon-experiment-reports-dba06d9` `9807a22`, 측정 `dba06d9`). **11/08 dba06d9 기준, 최종 통합본 실험 결과 아님.** 원자료 없음(선우 로컬). 이전 `3e4b2fd` 값은 삭제했습니다(8-3). 코호트는 표를 나눕니다.

NSCLC 파일럿 (`sim-nsclc-pilot-8x6`, 48쌍, 작성자 판독 부적격 라벨, seed 1–5)

| 예산 | dcvc 확정 부적격 | no_dcv | no_bundles | 판정 |
|---|---|---|---|---|
| 30분 | 6 | 6 | 2 | no_dcv = dcvc |
| 90분 | 8 | 8 | 4 | no_dcv = dcvc |
| 400분 | 8 | 8 | **14** | no_dcv = dcvc, **번들링 제거 우세 (5/5 seed)** |

생성기 (`neph-k3-catalog-source` replicate 0·1·2 × seed 1·2, 생성기 합성 라벨, 평균±표준편차)

| 예산 | dcvc | no_dcv | no_bundles | 판정 |
|---|---|---|---|---|
| 200분 | 4±4.34 | 4±4.34 | 3.33±3.33 | no_dcv = dcvc |
| 800분 | 5±5.87 | 5±5.87 | **8±3.69** | no_dcv = dcvc, **번들링 제거 우세 (우세 4·동률 2·열세 0)** |

240쌍 평가셋 (seed 1, 1회)

| 예산 | dcvc | no_dcv | no_bundles | 판정 |
|---|---|---|---|---|
| 400분 | 47 | 47 | **51** | no_dcv = dcvc, **번들링 제거 우세** |
| 6000분 | 124 (판단 대기 도달 20) | 124 (20) | 124 (20) | **차이 없음** (노트: 행동 수는 팔·버전마다 달라 발표에 쓰지 않음) |

- no_dcv: 측정한 7개 조건 모두 확정 수가 dcvc와 같습니다. 선우 04 §1: 라운드 0 계획에서 예산을 98–100% 쓰고 그때는 필터가 뺄 것이 없어, 현재 실행 모델에서 no_dcv는 측정 도구로 무효입니다.
- 번들링 제거: 예산에 따라 방향이 달라짐: 90분은 dcvc 우세, 400·800분은 번들링 제거 우세, 6000분은 차이 없음. 30·200분도 dcvc가 같거나 많습니다. 세 코호트(파일럿·생성기·평가셋)가 섞인 요약이라 발표에서는 코호트를 괄호로 함께 말합니다.
- 구간 추정이 없습니다. 파일럿은 seed 간 표준편차가 거의 0이라 실질 표본은 결정적 실행 1개에 가깝고, 평가셋은 seed 1 한 번입니다.

### 2-2. 문장 검사

검색 대상: 내 문서 3개(`presentation_data_gt.md`, `demo_nsclc_scenarios.md`, `nsclc_eval_dataset.md`), 통합본의 `docs/dev/web_demo_flow.md`, `docs/demo_capture.md`, `docs/team_handoff.md`, `README.md` 변경 이력 11-07~08, `docs/dev/p_common_engine_2026_11_07.md`, 웹 화면 문구(`staff.js`).
검색어: 성능, 효율, 향상, 개선, 우수, 가장 좋, 기준선보다, 정확도, False Eligible·오분류, 절감, 줄여/줄어, 절약.

| # | 위치 | 문장 | 처리 | 근거·대체 문장 |
|---|---|---|---|---|
| 1 | 내 `presentation_data_gt.md` 슬라이드 5 | "이 평가셋·이 조건에서 판단 기준 외 확인 완료 20 / 23쌍, 거짓 확정 0" | 유지 (조건 명시 강화) | `runner_6000m_h80_4f81460.json`. "full_dcvc 6000분·horizon 80·seed 1 한 번 실행" 조건을 붙임 |
| 2 | 내 `presentation_data_gt.md` 슬라이드 4 표 | 기준선(no_action) 0 vs full_dcvc 48 (400분) | 수정 | no_action은 행동을 하지 않는 기준점이라 "기준선보다 우수"로 읽히면 안 됨. "행동 없음 기준점 대비 예산 사용 시 확정 부적격 증가"로 표기, DCV·번들링 효과의 근거로 쓰지 않음 |
| 3 | 내 `presentation_data_gt.md` 슬라이드 5 주장 가능 | "엔진과 분리된 규칙 GT로 정책 결과 대조 가능" | 유지 (명칭 통일) | 도구가 있다는 사실. 성능 주장 아님 |
| 4 | 내 문서 전체 | DCV·번들링·DCV+C 효과 문장 | 없음 → 한계 슬라이드에 "DCV·번들링 효과 미입증" 추가 | 2-1 표 |
| 5 | 내 `nsclc_eval_dataset.md` §8 | "실행 사례에서 확정 적격 0, GT가 부적격이 아닌데 확정 부적격 0" | 유지 | 이 평가셋·실행 조건 한정 문구가 이미 있음 |
| 6 | 선우 `p_common_engine_2026_11_07.md` 표 열 | "False Eligible", "False Ineligible", "확정 Eligible 정확도" | 수정 필요 (선우) | 임상 오분류로 읽힘. 발표에 옮길 때는 "축소 범위 규칙 GT와 다른 확정 적격 / 부적격"으로. 문서 본문 §7은 측정 불가를 이미 밝힘 |
| 7 | 선우 `p_common_engine_2026_11_07.md` §6 (11-07) / 03 E1-b (`dba06d9`) | "200분에서는 dcvc가 기준선보다 확인된 부적격이 많습니다" (11-07 7.5 vs 0~1 → `dba06d9` 4±4.34 vs ≤0.83) | 수정 필요 (선우, 발표에 쓸 때) | 수치는 맞음(`generator_neph_k3.tsv`). 같은 표 800분 역전과 seed 2·구간 없음을 함께 적어야 함. DCV 효과 근거로는 쓸 수 없음(no_dcv와 같음) |
| 8 | 동겸 웹 화면 `staff.js:225-235` | "…연락을 줄여요", "줄어드는 시간 N분 · N% 절약" | 수정 필요 (동겸) | 숫자는 비용 원장의 가정 비용(같은 환자·같은 서류 연락비 1회) 계산값. 화면에 "가정 비용 기준" 표기가 없음(코드 주석에만 있음, `staff.js:187`). "가정 비용으로 계산한 예상 절약" 표기 권장. 시뮬레이션의 번들링 효과(2-1, 800분 역전)와 구분 |
| 9 | 통합본 `README.md` 2026-11-08 담당자 화면 항목 | "묶어 코디네이터 시간을 줄이는 것" | 유지 (설계 의도 문장) | 같은 항목의 "표현 규칙"이 가정 비용임을 밝힘. 발표에서 결과로 인용하지 않음 |
| 10 | 통합본 `README.md` 자동 실행 표 | "확정 (GT와 일치)", "서류 읽기 정확 32 / 틀림 0" | 수정 필요 (동겸, 발표에 쓸 때) | 여기 GT는 가상 시험 T-A~D의 규칙 GT로, 축소 범위 규칙 GT와 다름. "정확"은 합성 정답값과 읽은 값 일치 건수라 "일치 32 / 불일치 0"으로 |
| 11 | 통합본 `README.md` §5 평가 절 "정확도·Rescue…" (10-12 이전 파이프라인) | 정확도 지표 설명 | 해당 없음 | 이번 발표(DCV+C·NSCLC) 범위 밖. 인용하지 않음 |

정리: 내 문서 **삭제 1** (지역 조건 결함 문장 — 통합본에서 고쳐져 근거가 없어짐), **수정 3** (#1 조건 명시, #2 기준점 표기, 순환 라벨 표기). 남의 문서 **수정 필요 4** (#6, #7, #8, #10 → 6절).

## 3. GT 명칭 통일

공식 명칭: **축소 범위 규칙 GT (임상 전문가 미검수)**. 코드 식별자·파일명·라벨 값은 바꾸지 않았습니다.

| 문서 | 이전 표기 | 처리 |
|---|---|---|
| 내 `nsclc_eval_dataset.md` | 독립 규칙 GT 3, 독립 GT 4, 규칙 GT 2, "정답" 3 | 직접 통일. 머리에 공식 명칭 + "이 문서의 GT는 모두 이 GT" 줄임 규칙 |
| 내 `presentation_data_gt.md` | 독립 규칙 GT 1, 규칙 GT 1, 순환 GT 1, 정답 3 | 직접 통일. 순환 GT는 다른 개념이라 "엔진 순환 라벨(비교용)"으로 바꿈 |
| 내 `demo_nsclc_scenarios.md` | 규칙 GT 1, GT 9 | 직접 통일 |
| 선우 `p_common_engine_2026_11_07.md` | 독립 GT 1, 규칙 GT 2, "독립 합성 GT" | 수정 필요 목록. 단 이 문서의 GT는 NSCLC 파일럿 작성자 판독·생성기 GT라 공식 명칭 대상이 아님 → "파일럿 작성자 판독 라벨", "생성기 라벨"로 구분 권장 |
| 선우 `gt/README.md` | 독립 GT 3 | 수정 불필요. 사람 라벨링 도구(G_C·G_P·G_U)의 GT로 다른 대상. 발표에서 같은 이름을 쓰지 않도록 주의만 |
| 동겸 `README.md` 자동 실행·웹 | 규칙 GT, 정답 | 수정 필요 목록. 가상 시험 T-A~D 규칙 GT라 "가상 시험 규칙 라벨"로 구분 권장 |
| 동겸 `web_demo_flow.md` | "합성 정답에서만" | 수정 불필요(합성 값의 출처 설명) |

## 4. known_defect 2건 충돌 확인

실행: 통합본 `urgent/integration` `8afe5ea`에 `data/p0-eval-cases`의 테스트·데이터 파일만 복사(커밋 안 함), 오프라인.

| 케이스 | 4f81460 | 8afe5ea | 판정 |
|---|---|---|---|
| age20-unknown | CONFIRMED_INELIGIBLE (I1 NOT_MET) | INFO_MISSING (I1 UNKNOWN, I2 MET) | 고쳐짐 → strict xfail이 **XPASS로 실패** |
| age20-non_USA | CONFIRMED_INELIGIBLE (I1 NOT_MET) | INFO_MISSING (I1 UNKNOWN, I2 MET) | 같음 |
| age17·age20-USA 4건 | CONFIRMED_INELIGIBLE | INFO_MISSING (I1·I2 UNKNOWN 또는 I1 UNKNOWN) | 허용 상태 안 (보수적 보류) |
| age22·25 6건 | INFO_MISSING | INFO_MISSING | 변화 없음 |

- `tests/test_p0_3_region_cases.py` 결과: 4f81460 11 passed + 2 xfailed → 8afe5ea **11 passed + 2 failed (XPASS strict)**.
- 원인: 선우 `5fa4e24` fix(rules) "hold failing country-scoped structured criteria when site country is unknown" (11-08 02:28). 12케이스 결과가 내 제안 패치의 기대값(`measured_with_proposal`)과 모두 같습니다.
- **결론: 충돌 있음.** `data/p0-eval-cases`를 통합본과 합치면 실패 2건.
- 조치 제안: `known_defect` 2건을 false로 바꾸는 패치(아래, 2줄). 적용하면 8afe5ea에서 13 passed. 내 제안 패치 `docs/proposals/p0_3_region_condition.patch`는 선우 수정과 겹치므로 적용하지 않음(폐기 제안).
  - 패치 파일: 이 브랜치 `docs/proposals/p0_3_xfail_release.patch` (`data/p0-eval-cases` 브랜치 자체는 이번 규칙상 수정하지 않음)
- 참고: 17세(모든 지역에서 불충족)도 이제 확정 부적격이 아니라 INFO_MISSING입니다. 안전한 쪽이지만, 국가와 무관하게 부적격인 경우까지 보류됩니다(선우 확인 필요, 결함 아님).

### P0 사례 127건 채점 (합성 사례·오프라인 스텁 기준, 채점 스크립트만 실행)

| 유형 | 사례 | 4f81460 안전 / 오채택 / 과잉 보류 | 8afe5ea 안전 / 오채택 / 과잉 보류 | 상충 탐지 |
|---|---|---|---|---|
| held-out 6 상충 | 15 | 9 / 6 / 0 | **14 / 1 / 0** | 7/15 → **12/15** |
| held-out 6 전해 들은 값 | 10 | 7 / 3 / 0 | **10 / 0 / 0** | - |
| 상충: 같은 항목 다른 값 | 8 | 1 / 7 / 0 | 1 / 7 / 0 | - |
| 상충: 시점 차이 (과거 값) | 8 | 4 / 4 / 0 | 4 / 4 / 0 | - |
| 상충: 부정 후 번복 (서술) | 8 | 4 / 4 / 0 | 4 / 4 / 0 | 3/8 → 3/8 |
| 상충: 문서 vs 자가보고 (값) | 4 | 4 / 0 / 0 | 4 / 0 / 0 | - |
| 상충: 문서 vs 자가보고 (서술) | 4 | 0 / 4 / 0 | 0 / 4 / 0 | 0/4 → 0/4 |
| 전해 들은 값: 의료진 | 8 | 6 / 2 / 0 | **8 / 0 / 0** | - |
| 전해 들은 값: 가족 | 8 | 6 / 2 / 0 | **8 / 0 / 0** | - |
| 전해 들은 값: 기억 불확실 | 8 | 6 / 2 / 0 | **8 / 0 / 0** | - |
| 전해 들은 값: 다른 사람 값 | 8 | 6 / 2 / 0 | 6 / 2 / 0 | - |
| 정상 대조 (본인·검사·문서·최근 값) | 38 | 34 / 0 / 4 | 34 / 0 / 4 | - |
| **합계** | 127 | 87 / 36 / 4 | **101 / 22 / 4** | 10/27 → **15/27** |

- 바뀐 사례 14건은 모두 채택 → 보류·상충 방향입니다. 보류 → 채택으로 바뀐 사례는 0건, 과잉 보류는 4건 그대로입니다.
- 남은 오채택 22건: 같은 항목 다른 값 7, 시점 차이 4, 부정 후 번복 4, 문서 vs 자가보고(서술) 4, 다른 사람 값 2, held-out 상충 1.
- 건수이며 정확도가 아닙니다. 근거 확인 함수 단위 측정이고 웹·세션 전체 경로가 아닙니다.
- 결과 파일: `docs/dev/final_review_evidence/p0_fact_cases_8afe5ea.json`, `p0_fact_cases_8afe5ea_score.md`, 케이스별 상태 `p0_3_region_cases_8afe5ea.txt`

## 5. 데모 시나리오 확정

확정 흐름 문서: `docs/dev/demo_final_flow.md`. 오프라인 상세: `docs/dev/demo_nsclc_scenarios.md`.

| 이름 | 환자 · 시험 | 흐름 | 기대 상태 | 실제 결과 | 실행 환경 |
|---|---|---|---|---|---|
| W1 | DEMO-NSCLC-1 · 가상 T-A~D | 질문 고르기 → 같은 서류 묶음 요청 → 서류 확인 5 → 의사 판단 1 | T-A·T-C·T-D 참여 가능(확정), T-B 미확정 | 같음 | 웹 데모 서버(오프라인 대역), Chrome, 동겸 기록 `f1c56ef` |
| W2 | DEMO-NSCLC-4 · 가상 T-A~D | 나이 미기재 → 환자에게 나이 질문 → 재계산 | 나이 반영 재판정 | 같음 | 같음, 동겸 기록 `894747a` |
| W3 | S003 (7세) · 가상 T-A~D | 자기 보고 나이 불충족 → 나이 근거 서류 요청 유지 → 확인 | T-A~D 참여 불가(확정) | 같음 | 같음, 동겸 기록 `d20cc0d` |
| A | P052 × NCT07680790 | ECOG 진료기록 → EGFR 병리 결과지 | EXPERT_REVIEW | EXPERT_REVIEW | 오프라인 runner, `8afe5ea` 재확인 |
| B | P045 × NCT06928389 | 자기 보고 ECOG 2 → 진료기록 확인 | CONFIRMED_INELIGIBLE | CONFIRMED_INELIGIBLE | 같음 |
| C | P058 × NCT07416058 (P061에서 교체) | I6 "모름" → 투약 목록 요청 처리됨, **새 사실 0** (23시 정정) | INFO_MISSING | INFO_MISSING | 같음 |
| D | DEMO-REGION × NCT06216301 | 20세·국가 미상 | 확정하지 않음 | **INFO_MISSING** (4f81460에서는 CONFIRMED_INELIGIBLE) | 오프라인 판정기 |

- W1~W3 실제 결과는 동겸 기록이며 이번에 브라우저로 다시 확인하지 않았습니다. W1 기록은 담당자 화면 개편(`721fcf7`·`1e47a71`)·보류 표시 변경(`4dcf8ae`) 이전이라 통합본에서 리허설 확인이 필요합니다.
- 같은 6000분 실행 집계: 판단 대기 적격 → EXPERT_REVIEW 20/23, 확정 부적격 ∧ 규칙 GT 부적격 124/130, 거짓 확정 0, 확정 적격 0. 최종 상태 INFO_MISSING 92 → 91, VERIFICATION_PENDING 4 → 5 (나머지 같음).

### 엔진 관찰 2건

| 관찰 | 4f81460 | 8afe5ea | 결론 |
|---|---|---|---|
| E12 PD-1/PD-L1 치료력 → `biomarker:PD-L1`로 잡힘 | 발생 | `criterion:NCT06216301:E12` 64건, `biomarker:PD-L1` 0건 | 고쳐짐 (`173cd3f`) |
| 남성 환자에게 임신 여부 질문 (E16) | 발생 (P061) | 남성 47명 중 0건 | 고쳐짐 (`d03c39e`) |

### 발표 흐름 (요약, 상세는 `demo_final_flow.md`)

1. 웹 W1 메인: 환자 신청 → 담당자 질문 고르기(묶음, **가정 비용**) → 서류 확인 → 의사 판단 → T-A·C·D 확정, T-B 미확정 설명
2. 오프라인 보조 A: 실제 프로토콜 평가셋에서 판단 기준 외 확인 후 담당의 검토 (20/23, 한 번 실행)
3. 오프라인 보조 B: 자기 보고만으로 확정하지 않고 기록 확인 후 확정 (거짓 확정 0)
4. 시연에서 뺌: D(한계·수정 사례 슬라이드만), C(질문 대비), W1의 T-B 확정 장면
5. 백업: 녹화본(리허설 때 녹화 필요) → `run_e2e_synthetic.py --mode offline` 표 → 오프라인 A·B 슬라이드

## 6. 수정 필요 목록 (남의 문서·코드, 보고만)

### 선우

| # | 위치 | 내용 | 제안 |
|---|---|---|---|
| S1 | `data/p0-eval-cases` 병합 시 | strict xfail 2건이 통합본 `8afe5ea`에서 XPASS로 실패 | `docs/proposals/p0_3_xfail_release.patch`(2줄) 적용 후 병합. 내 `p0_3_region_condition.patch`는 `5fa4e24`와 겹쳐 폐기 |
| S2 | `docs/dev/p_common_engine_2026_11_07.md` 표 열 | False Eligible·False Ineligible·확정 Eligible 정확도 | 발표용으로는 "GT와 다른 확정 적격/부적격"으로. 임상 오분류로 읽힘 |
| S3 | 같은 문서 §6 | "200분에서는 dcvc가 기준선보다 확인된 부적격이 많습니다" | 800분 역전, seed 수, 구간 없음, no_dcv = dcvc를 같은 문단에 |
| S4 | 같은 문서·`p_results/` | ablation 결과가 `3e4b2fd`(11-07) 기준. 22시에 `dba06d9` 03·04 보고서로 대체됨. 원자료(result.json)를 공유 브랜치에 올려 주면 대조 가능 | 발표에 ablation 수치를 쓸 거면 통합본 기준 재실행 결과 위치를 알려 주기 (이번 검수에서는 위치 미확인) |
| S5 | 같은 문서 GT 표기 | "독립 GT", "독립 합성 GT", "규칙 GT" | 파일럿은 "작성자 판독 라벨", 생성기는 "생성기 라벨"로 구분. 공식 명칭(축소 범위 규칙 GT)은 NSCLC 평가셋 GT 전용 |
| S6 | 17세 국가 미상 케이스 | `5fa4e24` 뒤 모든 지역에서 불충족인 17세도 INFO_MISSING | 안전한 쪽이라 결함 아님. 의도인지 확인만 |

### 동겸

| # | 위치 | 내용 | 제안 |
|---|---|---|---|
| D1 | 웹 담당자 화면 `static/staff.js:225-235` | "연락을 줄여요", "줄어드는 시간 · N% 절약"에 가정 비용 표기 없음 | 숫자 옆에 "가정 비용 기준 예상값" 표기. 시뮬레이션의 번들링 효과와 구분 (800분 역전) |
| D2 | `README.md` 자동 실행 결과 표 | "확정 (GT와 일치)", "서류 읽기 정확 32 / 틀림 0" | 가상 시험 T-A~D 규칙 라벨임을 밝히고 "일치 32 / 불일치 0"으로. 축소 범위 규칙 GT와 다른 GT |
| D3 | 웹 데모 시나리오 이름 | W1~W3 번호가 저장소에 없음 | 이 문서 5절의 W1~W3 대응(README 브라우저 확인 기록 기준)이 맞는지 확인 |
| D4 | 데모 흐름 | DEMO-NSCLC-1의 T-B(EGFR)가 오프라인 대역에서 미확정 | 시연에서 T-B를 "LLM 판정 기준, 오프라인 데모에서는 미확정"으로 설명하거나 T-A·C·D만 보여 주기 |


## 8. 추가 대조 — 선우 dba06d9 재실험 보고서 (11/08 21:46 공유)

입력: `share/seoyeon-experiment-reports-dba06d9` `9807a22` (2026-11-08 21:46) — `docs/experiments/seoyeon-handoff-dba06d9/03_LATEST_EXPERIMENT_RESULTS.md`, `04_DCV_ABLATION_ANALYSIS.md`. 측정 커밋 `dba06d9` (2026-11-08 16:35, PR #53 head).
**원자료:** 22시에는 원자료가 없어 보고서 표만 대조했습니다. 24시에 원자료 `share/seoyeon-experiment-raw-dba06d9` `ce4a8e1` `docs/experiments/seoyeon-handoff-dba06d9/raw/sy_b/`가 공유되어 재집계로 대조를 끝냈습니다(10절, 불일치 0).
**표기:** 이 절과 발표 자료의 ablation 수치는 모두 **11/08 dba06d9 기준, 최종 통합본 실험 결과 아님**입니다.

### 8-0. 요약

| 항목 | 결과 |
|---|---|
| 수치 대조 | 30개 항목 중 일치 13 / 불일치 5 / 코호트 다름 12. 불일치·코호트 다름은 모두 내 문서에 있던 `3e4b2fd`·`4f81460` 값이며 `dba06d9`·`8afe5ea` 값으로 교체 |
| 400분 47 vs 48 | **엔진 버전 차이.** `4f81460` 엔진 48, `dba06d9`·`8afe5ea` 47 (result_hash 같음 → 재현 안정). 갈린 쌍 P034 × NCT07416058. 집계 방식 차이 아님 (내 채점과 선우 채점 모두 47) |
| 발표 사용 값 | 최종 통합본 `8afe5ea` 47. 48은 노트 |
| DCV·번들링 | `3e4b2fd` 값 삭제, `dba06d9` 값으로 교체. 코호트별 표 분리 |
| E2E 변경 | (24시 확정) `d20cc0d`·SYN-04 × T-C, 3 / 11 → 4 / 10, 안전 위반 0 (선우 확인). 데모 메인 흐름 영향 없음 |
| 발표 사용 금지 문장 | 선우 보고서 9건 (8-5) |

### 8-1. 수치 대조

| # | 위치 | 내 문서 값 | 선우 보고서 값 | 기준 커밋 (내 / 선우) | 코호트 | 판정 → 처리 |
|---|---|---|---|---|---|---|
| 1 | presentation 슬5 400분 no_action | 0·0·0·0 | CI 0, ready 0 (E1-c) | 4f81460 / dba06d9 | 240쌍 평가셋 | 일치 |
| 2 | presentation 슬5 400분 full_dcvc 확정 부적격 | 48 / 130 | 47 / 130 | 4f81460 / dba06d9 | 240쌍 | **불일치** → 8afe5ea 47로 교체, 48은 노트 (8-2) |
| 3 | presentation 슬5 400분 판단 대기 도달 | 0 / 23 | ready 0 | 4f81460 / dba06d9 | 240쌍 | 일치 |
| 4 | presentation 슬5 6000분 판단 대기 도달 | 20 / 23 | 20 / 23 | 4f81460·8afe5ea / dba06d9 | 240쌍 | 일치 |
| 5 | presentation 슬5 6000분 확정 부적격 | 124 / 130 | 124 / 130 | 같음 | 240쌍 | 일치 |
| 6 | presentation 슬5 거짓 확정 (부적격 아님 + 확정 부적격) | 0 | FI 0, CI∧unlabeled 0 | 같음 | 240쌍 | 일치 |
| 7 | presentation 슬5 확정 적격 | 0 | CE 0 | 같음 | 240쌍 | 일치 |
| 8 | presentation 슬5 "48에서 124로" | 48 → 124 | 47 → 124 | 4f81460 / dba06d9 | 240쌍 | **불일치** → "47에서 124로" |
| 9 | presentation 한계 "no_dcv 7개 조건 모두 같음" | 데모 3 + 파일럿 2 + 생성기 2 | 파일럿 3 + 생성기 2 + 평가셋 2 (모두 확정 수 같음) | 3e4b2fd / dba06d9 | 섞임 | **코호트 다름** → 코호트별 표 (슬6) |
| 10 | presentation 한계 no_bundles 생성기 800분 | 11.17 vs dcvc 8.17 | 8±3.69 vs 5±5.87 | 3e4b2fd / dba06d9 | 생성기 (라벨 29/39/38 → 29/29/32) | **코호트 다름** → dba06d9 값 |
| 11 | presentation 한계 "역전 1개 조건" | 생성기 800분 1개 | 파일럿 400, 생성기 800, 평가셋 400 (3개) | 3e4b2fd / dba06d9 | 섞임 | **불일치** → 3개 조건, 코호트별 |
| 12 | final_review 2-1 파일럿 30분 dcvc / no_dcv / no_bundles | 6 / 6 / 2 | 6 / 6 / 2 | 3e4b2fd / dba06d9 (seed 1–3 → 1–5) | 파일럿 | 일치 (seed 수 다름) |
| 13 | final_review 2-1 파일럿 90분 확정 부적격 | 8 / 8 / 4 | 8 / 8 / 4 | 같음 | 파일럿 | 일치 |
| 14 | final_review 2-1 파일럿 90분 coverage | 0.188 | 0.167 | 3e4b2fd / dba06d9 | 파일럿 | **코호트 다름** (같은 이름, 결과 다름: 이전 거짓 부적격 1 포함) → 삭제 |
| 15 | final_review 2-1 파일럿 90분 거짓 부적격 | 1 (지역 조건 결함) | FI 0 | 3e4b2fd / dba06d9 | 파일럿 | **코호트 다름** (엔진 `5fa4e24` 뒤) → dba06d9 값 |
| 16 | final_review 2-1 파일럿 400분 | (없음) | 8 / 8 / 14 | – / dba06d9 | 파일럿 | 추가 |
| 17 | final_review 2-1 생성기 200분 | 7.5 / 7.5 / 6.83 | 4±4.34 / 4±4.34 / 3.33±3.33 | 3e4b2fd / dba06d9 | 생성기 | **코호트 다름** → dba06d9 값 |
| 18 | final_review 2-1 생성기 800분 | 8.17 / 8.17 / 11.17 | 5±5.87 / 5±5.87 / 8±3.69 | 3e4b2fd / dba06d9 | 생성기 | **코호트 다름** → dba06d9 값 |
| 19 | final_review 2-1 데모 6×3 20·45·90분 | coverage 값 9개 | 재측정 안 함 ("이전 데모 수치는 현재 결과로 쓰면 안 됨") | 3e4b2fd / – | 데모 | **코호트 다름** (측정 없음) → 삭제 |
| 20 | final_review 2-1 평가셋 ablation | (없음) | 400분 47/47/51, 6000분 124/124/124 | – / dba06d9 | 240쌍 | 추가 |
| 21 | final_review 2-2 #7 "200분 dcvc 7.5 vs 0~1" | 7.5 vs 0~1 | 4±4.34 vs random 0.33, patient_ind 0.83 | 3e4b2fd / dba06d9 | 생성기 | **코호트 다름** → 갱신, 발표 사용 금지 유지 |
| 22 | final_review 요약 "구간 추정 없음" | 구간 없음 | 평균±표준편차만, 구간 없음 | – | 전체 | 일치 |
| 23 | final_review 0 "재실행 결과 위치 미확인" | 위치 미확인 | 03·04 보고서 + 원자료 (24시) | – | – | **불일치** → 위치 확인, 원자료 없음 |
| 24 | demo_final_flow 보조 1 "20/23, 한 번 실행" | 20/23 | 20/23 (seed 1) | 8afe5ea / dba06d9 | 240쌍 | 일치 |
| 25 | demo_final_flow 보조 2 "거짓 확정 0" | 0 | 0 | 같음 | 240쌍 | 일치 |
| 26 | demo_final_flow "생성기 800분 역전" | 역전 (값 없음) | 8 vs 5 | 3e4b2fd / dba06d9 | 생성기 | **코호트 다름** → 기준 커밋 표기 추가 |
| 27 | final_review 4·presentation 5-1 P0 36 → 22 | 36 → 22 | (보고서에 없음) | 4f81460 → 8afe5ea | P0 사례 | 일치 (대조 대상 없음), "합성 사례·오프라인 스텁 기준" 표기 확인 |
| 28 | presentation 슬5 판단 대기 적격 23·부적격 130 | 23 / 130 / 87 | 23 / 130 / 87 | 996efd6 | 240쌍 | 일치 |
| 29 | presentation 한계 "DCV 필터 기여" | 미입증 | "측정 도구로 무효" (04 §1) | – / dba06d9 | 전체 | **불일치 (표현)** → "측정할 수 없음"으로 강화 |
| 30 | final_review 1-3 선우 P 보고서 ablation | `3e4b2fd` 값 | dba06d9 값으로 대체됨 | 3e4b2fd / dba06d9 | 섞임 | **코호트 다름** → S4 갱신 |

판정 집계: 일치 13 (1, 3, 4, 5, 6, 7, 12, 13, 22, 24, 25, 27, 28) / 불일치 5 (2, 8, 11, 23, 29의 표현) / 코호트 다름 12 (9, 10, 14, 15, 16, 17, 18, 19, 20, 21, 26, 30).

### 8-2. 400분 확정 부적격 47 vs 48

실행: 240쌍 평가셋, full_dcvc, 400분, horizon 10, seed 1, 커밋마다 한 번 (오프라인, `docs/dev/final_review_scripts/r400_one_commit.py`). `4f81460`에는 평가셋이 없어 엔진이 같은 `c3cc3d5`(#43, 4f81460 + 평가셋·cohort 코드만)에서 실행했습니다.

| 커밋 | 확정 부적격 | ∧ 규칙 GT 부적격 | 검증 대기 | 정보 부족 | 행동 | result_hash |
|---|---|---|---|---|---|---|
| `4f81460` 엔진 (`c3cc3d5`) | 48 | 48 | 26 | 166 | 88 | `07840bd8b59f7c5c` |
| `dba06d9` | 47 | 47 | 28 | 165 | 88 | `0a798d99c1df5cb5` |
| `8afe5ea` | 47 | 47 | 28 | 165 | 88 | `0a798d99c1df5cb5` |

갈린 쌍 (전체 목록 `docs/dev/final_review_evidence/r400_three_commits.json`)

| 쌍 키 | 환자 × 시험 | 4f81460 | dba06d9 | 8afe5ea | 규칙 GT |
|---|---|---|---|---|---|
| `PA-5b410d13fb6eb7bc\|NCT07416058` | P034 × NCT07416058 | CONFIRMED_INELIGIBLE | VERIFICATION_PENDING | VERIFICATION_PENDING | INELIGIBLE (fail:I4) |
| `PA-0dd0dfa09527da66\|NCT06216301` | P058 × NCT06216301 | INFO_MISSING | VERIFICATION_PENDING | VERIFICATION_PENDING | INELIGIBLE (fail:E16·I9) — 확정 수에는 영향 없음 |

원인으로 보이는 엔진 변경
- 행동 목록이 step 1부터 다릅니다. 4f81460: P034에 `pathology_report criterion:NCT07098338:I16` (범위 밖 biomarker 기준) → 성공하면서 같은 결과지로 NCT07416058 I4 근거가 확인되어 VERIFICATION_PENDING → CONFIRMED_INELIGIBLE. dba06d9: 첫 행동이 다른 환자의 `biomarker:PD-L1`이고 P034는 행동을 받지 못했습니다.
- 확인 항목 분포가 바뀌었습니다 (criterion 35 → 32, biomarker 6 → 8). biomarker 기준의 확인 항목 묶음을 바꾼 **`39ec3d2` fix(engine): group by biomarker target only for biomarker criteria**가 원인 후보입니다.
- P058 × NCT06216301: dba06d9에서 E16(임신·수유) 근거 확인 요청(`basis:criterion:NCT06216301:E16`)이 새로 나왔습니다. 성별 판정 `d03c39e` 또는 P0-B 값 보류 `02f9d2c`가 후보입니다.
- 커밋 하나씩 좁혀 가는 추가 실행은 이번 허용 범위(커밋당 한 번) 밖이라 하지 않았습니다. 원인 커밋은 "후보"입니다.

결론: **엔진 버전 차이.** 근거 — ① dba06d9와 8afe5ea의 result_hash가 같음(재현 안정) ② 갈린 쌍의 행동 목록이 step 1부터 다름 ③ 내 채점(`pair_gt.csv` 대조)과 선우 채점(`runner.run(labels=)`)이 같은 커밋에서 같은 47 (집계 방식 차이 아님).
발표: 최종 통합본 `8afe5ea` 값 47을 쓰고, "4f81460 엔진 48" 노트를 붙입니다.

### 8-3. DCV·번들링 수치 교체

- 삭제: `3e4b2fd` 기반 값 전부 (데모 6×3 coverage, 파일럿 90분 coverage·거짓 부적격 1, 생성기 7.5/6.83/8.17/11.17)
- 교체 (모두 `dba06d9`, 11/08 dba06d9 기준, 최종 통합본 실험 결과 아님)
  - NSCLC 파일럿 (seed 1–5): 30분 6/6/2, 90분 dcvc 8 · no_dcv 8 · no_bundles 4, 400분 dcvc 8 · no_dcv 8 · no_bundles **14**
  - 생성기 (replicate 3 × seed 2): 200분 4±4.34 / 4±4.34 / 3.33±3.33, 800분 dcvc **5±5.87** vs no_bundles **8±3.69** (no_dcv 5±5.87)
  - 240쌍 평가셋 (seed 1): 400분 47 / 47 / 51, 6000분 124 / 124 / 124 (판단 대기 도달 모두 20)
- 반영 위치: `presentation_data_gt.md` 슬라이드 6·7, 이 문서 2-1·2-2 #7·6절 S4, `demo_final_flow.md` "말하지 않을 것"

### 8-4. E2E 변경 확인 (24시 확정)

- **확정 (선우 확인, 2026-11-08):** 오프라인 E2E가 참여 불가 확정 3 / 미확정 11 → **4 / 10**으로 바뀐 원인은 `d20cc0d` (fix/exclusion-basis-kept, PR #56)입니다. 대상은 **SYN-04 × T-C** 1쌍(80세, '75세 이하' 불충족)입니다. 나이 근거 확인 요청이 다른 서류 확인 뒤에도 닫히지 않아, 의무기록으로 나이를 확인한 뒤 확정 부적격(정답과 같음)이 됩니다. 안전 위반 0입니다.
- 최종 통합본 `b1d2a7f` 기대값: 참여 가능 확정 정답과 일치 4 + 의사 판단 뒤 6 / **참여 불가 확정 4 / 미확정 10** / 안전 위반 0 (README §3.0, phase1-final 3회 실행 동일). 이전 22시 판의 "추정·선우 확인 필요"는 해소했습니다. 이번에도 E2E를 직접 돌리지는 않았습니다(선우 실행 기록 기준).
- 데모 영향
  - 메인 W1·보조 A·B: 영향 없음 (W1은 문서 확인·의사 판단 경로, A·B는 오프라인 runner)
  - W3 (S003 7세, 나이 근거 요청 유지): `d20cc0d`가 고친 흐름과 같은 종류이고, 동겸 기록이 수정 뒤 기록이라 기대 상태 그대로
  - 백업 E2E 표: **최종 통합본 `b1d2a7f` 오프라인 E2E · 확정 부적격 4 / 미확정 10** 값으로 고정

### 8-5. 선우 보고서 — 발표 사용 금지 문장

| # | 위치 | 문장 | 사유 |
|---|---|---|---|
| X1 | 04 §4 | "NSCLC 파일럿 30·90·400분에서 dcvc는 random·patient_independent·cost_first보다 확정 부적격이 많습니다" | 기준선보다 우수로 읽힘. 결정적 실행 1개에 가까움(03 E1-a), 400분에서 no_bundles가 더 높음 |
| X2 | 03 E1-c, 04 §4 | "6000분에서 dcvc는 기준선 3종보다 expert-ready(20 vs 16/16/8)와 CI(124 vs 110/112/117)가 높습니다" | seed 1, 1회. 성능 우위로 읽힘. 같은 조건 no_bundles·no_dcv와 같음 |
| X3 | 04 §4 | "400분에서는 CI 47 vs 0–8" | 같음. 같은 조건 no_bundles 51 |
| X4 | 03 E1-b | "200분에서 dcvc의 기준선 대비 우위 … 는 유지되지만" | 우위 표현. 분산이 크고 replicate 2에서 0–1 |
| X5 | 03 E1-a | "400분에서도 기준선 3종은 CI 0입니다" | 기준선 대비 비교 문장으로 쓰지 않음 |
| X6 | 04 §0·§1.4 | "C 점수의 impact 기여 NSCLC에서 있음", "결정 단락 우선이 NSCLC 결과의 대부분을 만든다" | 공식 팔이 아닌 진단 변형(diag_noimpact), seed 1 |
| X7 | 04 §3 | "큰 예산에서는 라우팅 기여가 보입니다" | seed 1, 같은 표에서 400분은 반대 방향(49 > 47) |
| X8 | 03 열 약어 | VE·FE·FI (verified_eligible, false_eligible, false_ineligible) | 임상 오분류로 읽힘. 쓸 때는 "규칙 GT와 다른 확정"으로. 적격 GT 0이라 생길 수 없는 0 |
| X9 | 04 §2.2 | "작은 예산에서는 집중이 유리합니다" | 번들링 이득 주장으로 읽힘. 인과 미확인(생성기 800분 inconclusive) |

발표에서 쓸 수 있는 선우 보고서 문장 (조건 포함 그대로): "no_dcv는 현재 실행 모델에서 측정 도구로 무효", "큰 예산에서는 번들 할인을 끈 쪽이 더 높다", "어떤 수치도 임상 정확도가 아니다", "적격 GT가 없어 FE 0은 안전성 증거가 아니다".

## 9. 최종 통합 브랜치 대조 — `integration/phase1-final` `b1d2a7f` (2026-11-08 21:36, 선우)

통합 브랜치는 수정·push하지 않았습니다. 별도 worktree에서 읽기·허용된 실행만 했습니다(240쌍 runner 단일 실행 3개, P0 채점, 평가셋 재생성·테스트, 전체 테스트).

### 9-1. 포함 여부

| 항목 | 상태 | 근거 커밋 (phase1-final) |
|---|---|---|
| #43 `gt.py`, `data/nsclc/eval`, `data/nsclc/review`, `scripts/build_nsclc_eval.py`, `tests/test_nsclc_eval_gt.py` | 포함 | `ade22d8`~`c3cc3d5` (#43, 8afe5ea 경유) |
| 결정 5 board 테스트 (`moved == {"NCT07087314": 1}`) | 포함 | `c076add` (feat/nsclc-eval-gt-decision5-patches, #46 경유) |
| 결정 5 sim_expert_metric (`from ..cohort.spec import EXPERT_SLOTS`) | 포함 | 같음 + 동겸 #46 테스트 "같은 객체" |
| 결정 5 recovery_stress (16·24) | 포함 | 같음 |
| `cohort.spec.EXPERT_SLOTS` 8개 (CSV 6 + EXTRA 2) | 포함 | `spec.py:28-31` |
| P0 사례 127건 (값 82·서술 20·held-out 25), `score_p0_fact_cases.py`, 오프라인 어댑터 | 포함 | `3ee3e7d`·`0bec65b`·`53ab2de` (data/p0-eval-cases 재작성 커밋) |
| P0-3 회귀 케이스 | 포함 | `1de0f6d` |
| xfail 해제 (known_defect 2건 false) | 포함 (내 패치 파일 대신 같은 내용을 선우가 커밋) | `60d7921` |
| P0 phase1-final 채점 결과 | 포함 | `4606c58` |
| 내 `nsclc_eval_dataset.md` | **부분** — K판(`2172892`)까지. 21시 GT 명칭 통일(`eba0fc6`) 누락 | |
| 내 `presentation_data_gt.md` | **부분** — `70fe9a5`판(`179b339`, 48·지역 결함 문장·3e4b2fd ablation 포함) + 선우 15줄 추가. 21시 최종본(`eba0fc6`)·22시 dba06d9 대조판(`4fbe82b`) 누락 | |
| 내 `demo_final_flow.md` | **누락** | |
| 내 `final_review_2026-11-08.md`, `final_review_evidence/`, `final_review_scripts/` | **누락** | |
| `demo_nsclc_scenarios.md` | 선우 갱신본으로 대체 (`fa07b65`·`3eecd50`, B=P052, C=P058). 내 21시판(`eba0fc6`)은 들어가지 않음 — 통합본 판이 더 정확해 그대로 두는 것 권장 | |
| `docs/dev/final_review_evidence/phase1_final_runs.json`, `final_review_scripts/run_one_policy.py` | 23시: phase1-final runner 3개·테스트·재생성 결과 |
| `docs/dev/final_review_evidence/reagg_dba06d9.json`, `final_review_scripts/reagg_dba06d9_raw.py` | 24시: 선우 원자료 재집계 |
| `docs/proposals/p0_3_xfail_release.patch` | 누락 (불필요: `60d7921`로 반영됨) | |

### 9-2. 수치 재확인

| 항목 | 슬라이드 값 (8afe5ea) | phase1-final 값 | 일치 |
|---|---|---|---|
| 전체 테스트 | – | 6,254 passed / 0 failed / 9 skipped / 4 xfailed | – (8afe5ea 전체 테스트는 돌리지 않음) |
| `tests/test_nsclc_eval_gt.py`·`tests/test_p0_3_region_cases.py` | 20 passed · 13 passed(해제 패치 적용 시) | 전체 테스트 안에서 통과 (실패 0) | 일치 |
| 평가셋 설정 해시 | `a549da3b1171fd52` | 재생성 `a549da3b1171fd52` | 일치 |
| content / sim cohort 해시 | `ec277a27…` / `4bd9143334bb0871` | 같음 | 일치 |
| 라벨 분포 | 0 / 23 / 130 / 87 | 0 / 23 / 130 / 87 | 일치 |
| 재생성 diff | – | manifest `generated_at`·`build_commit`만, CSV는 줄바꿈만 | 정상 |
| no_action 400분 | 판단 대기 0, 확정 부적격 0, 거짓 0, 확정 적격 0 | 같음 (검증 대기 75 / 정보 부족 165) | 일치 (초기 상태 1쌍은 `4f81460`의 74/166과 다름, 지표 영향 없음, 쌍 미특정) |
| full_dcvc 400분·h10 | 47 / 130, 거짓 0 | 47 / 130, 거짓 0, result_hash `0a798d99c1df5cb5` (8afe5ea와 같음, 쌍 단위 차이 0) | 일치 |
| full_dcvc 6000분·h80 | 20 / 23, 124 / 130, 거짓 0, 확정 적격 0 | 같음 | 일치 (노트: 행동 수는 버전마다 달라 발표에 쓰지 않음 — b1d2a7f 2,236 / 선우 dba06d9 2,440, 최종 상태 같음. 후보 `b382176` 문서 소진 항목 보류) |
| P0 127건 | 안전 101 / 오채택 22 / 과잉 보류 4 / 상충 15/27 | 같음 (사례별 차이 0) | 일치 |

근거: `docs/dev/final_review_evidence/phase1_final_runs.json` (쌍별 최종 상태 포함), 통합본 자체 기록 `data/nsclc/eval/checks/*_phase1-final.json`과도 같음.

### 9-3. 문서 갱신

- `presentation_data_gt.md`: 슬라이드 1~5·5-1 기준 커밋을 phase1-final `b1d2a7f`로, `8afe5ea`는 노트로. 슬라이드 6의 "11/08 dba06d9 기준, 최종 통합본 실험 결과 아님"은 유지.
- `demo_final_flow.md`: README §3.0과 맞춤 (포트 8011 → 8791, `--store`, `CTREC_SYNTHETIC_SIGNING_SECRET`, `verify_frozen` 확인), 시나리오 이름을 통합본 문서와 맞춤 (B = P052, P045 대체), C 문구 정정, E2E 4 / 10 출처 확인(PR #56).
- `PHASE1_FINAL_INTEGRATION.md`: 모든 원격 브랜치에서 **위치 미확인**. README 9.1이 말하는 "통합 보고서"도 저장소에 없음. 대신 README §3.0 명령과 대조했습니다.

### 9-4. 선우용 누락 목록

1. `docs/presentation-data-gt`의 `eba0fc6`·`4fbe82b`(+ 이번 23시 커밋)를 phase1-final에 넣어 줘: `docs/dev/presentation_data_gt.md`(최종본), `docs/dev/demo_final_flow.md`, `docs/dev/final_review_2026-11-08.md`, `docs/dev/final_review_evidence/`, `docs/dev/final_review_scripts/`, `docs/dev/nsclc_eval_dataset.md`(GT 명칭 통일).
   - 지금 phase1-final의 `presentation_data_gt.md`에는 400분 48, "20세 거짓 부적격 2케이스", `3e4b2fd` ablation 값이 남아 있음 → 발표 자료로 쓰면 틀린 값.
   - `demo_nsclc_scenarios.md`는 통합본 판(`3eecd50`)을 유지하고 내 판은 넣지 않아도 됨.
2. `PHASE1_FINAL_INTEGRATION.md`(통합 보고서)가 저장소에 없음. 실행 방법 8번 대조 요청이 있었는데 파일을 못 찾았어 — 올려 주거나 경로 알려 줘.
3. `docs/proposals/p0_3_xfail_release.patch`는 `60d7921`과 같은 내용이라 넣지 않아도 됨.

### 9-5. 동겸용 리허설 참고

- 서버 명령은 README §3.0 그대로: 포트 **8791**, `--demo-staff --synthetic-verification --store <빈-폴더>`, `CTREC_SYNTHETIC_SIGNING_SECRET` 고정 (재시작 뒤 서류 확인·의사 판단이 인정되려면 필요).
- phase1-final에는 PR #54(질문 고르기 화면), #55(보류 표시), `73ca6e5`(진행 중 요청 수), F-03(`e8ac1e5`, 의사 판단에 `expected_revision` 필수 — 화면이 이를 보내는지 확인)가 들어갔는데 README 9.1 기준 브라우저 화면은 아무도 열지 않음 → W1을 phase1-final에서 한 번 돌리고 녹화.
- 백업 E2E 기대값: 참여 가능 확정 4 + 의사 판단 뒤 6 / 참여 불가 확정 4 / 미확정 10 / 안전 위반 0.
- 오프라인 보조는 통합본 문서 이름 그대로 A(P052 × NCT07680790), B(P052 × NCT06928389). C는 "확보했다"고 말하지 않음.

## 10. 원자료 대조 — 슬라이드 6 ablation (24시)

입력: `share/seoyeon-experiment-raw-dba06d9` `ce4a8e1` `docs/experiments/seoyeon-handoff-dba06d9/raw/sy_b/` (선우, 2026-11-08 22:06). 실험은 다시 돌리지 않고 `row` 값만 다시 셌습니다 (`docs/dev/final_review_scripts/reagg_dba06d9_raw.py`, 결과 `docs/dev/final_review_evidence/reagg_dba06d9.json`). 모두 **11/08 dba06d9 기준 · 최종 통합본 실험 결과 아님**.

| 수치 | 문서 값 | 원자료 재집계 | 일치 | result.json 경로 (`raw/sy_b/` 아래) |
|---|---|---|---|---|
| 파일럿 30분 dcvc 확정 부적격 | 6 | 6 (n=5) | 일치 | `e1_nsclc_out/c*-dcvc-b30-s*/result.json` |
| 파일럿 30분 dcvc-no_dcv 확정 부적격 | 6 | 6 (n=5) | 일치 | `e1_nsclc_out/c*-dcvc-no_dcv-b30-s*/result.json` |
| 파일럿 30분 dcvc-no_bundles 확정 부적격 | 2 | 2 (n=5) | 일치 | `e1_nsclc_out/c*-dcvc-no_bundles-b30-s*/result.json` |
| 파일럿 90분 dcvc 확정 부적격 | 8 | 8 (n=5) | 일치 | `e1_nsclc_out/c*-dcvc-b90-s*/result.json` |
| 파일럿 90분 dcvc-no_dcv 확정 부적격 | 8 | 8 (n=5) | 일치 | `e1_nsclc_out/c*-dcvc-no_dcv-b90-s*/result.json` |
| 파일럿 90분 dcvc-no_bundles 확정 부적격 | 4 | 4 (n=5) | 일치 | `e1_nsclc_out/c*-dcvc-no_bundles-b90-s*/result.json` |
| 파일럿 400분 dcvc 확정 부적격 | 8 | 8 (n=5) | 일치 | `e1_nsclc_out/c*-dcvc-b400-s*/result.json` |
| 파일럿 400분 dcvc-no_dcv 확정 부적격 | 8 | 8 (n=5) | 일치 | `e1_nsclc_out/c*-dcvc-no_dcv-b400-s*/result.json` |
| 파일럿 400분 dcvc-no_bundles 확정 부적격 | 14 | 14 (n=5) | 일치 | `e1_nsclc_out/c*-dcvc-no_bundles-b400-s*/result.json` |
| 생성기 200분 dcvc 확정 부적격 | 4±4.34 | 4±4.34 (n=6) | 일치 | `e1_gen_out/c*-dcvc-b200-s*/result.json` |
| 생성기 200분 dcvc-no_dcv 확정 부적격 | 4±4.34 | 4±4.34 (n=6) | 일치 | `e1_gen_out/c*-dcvc-no_dcv-b200-s*/result.json` |
| 생성기 200분 dcvc-no_bundles 확정 부적격 | 3.33±3.33 | 3.33±3.33 (n=6) | 일치 | `e1_gen_out/c*-dcvc-no_bundles-b200-s*/result.json` |
| 생성기 800분 dcvc 확정 부적격 | 5±5.87 | 5±5.87 (n=6) | 일치 | `e1_gen_out/c*-dcvc-b800-s*/result.json` |
| 생성기 800분 dcvc-no_dcv 확정 부적격 | 5±5.87 | 5±5.87 (n=6) | 일치 | `e1_gen_out/c*-dcvc-no_dcv-b800-s*/result.json` |
| 생성기 800분 dcvc-no_bundles 확정 부적격 | 8±3.69 | 8±3.69 (n=6) | 일치 | `e1_gen_out/c*-dcvc-no_bundles-b800-s*/result.json` |
| 평가셋 400분 dcvc 확정 부적격(∧규칙 GT 부적격) | 47 | 47 | 일치 | `wp6_eval_400.json` rows[arm=dcvc] `CI_gt_ineligible` |
| 평가셋 400분 dcvc-no_bundles 확정 부적격(∧규칙 GT 부적격) | 51 | 51 | 일치 | `wp6_eval_400.json` rows[arm=dcvc-no_bundles] `CI_gt_ineligible` |
| 평가셋 400분 dcvc-no_dcv 확정 부적격(∧규칙 GT 부적격) | 47 | 47 | 일치 | `wp6_eval_400.json` rows[arm=dcvc-no_dcv] `CI_gt_ineligible` |
| 평가셋 6000분 dcvc·no_bundles·no_dcv 확정 부적격 / 판단 대기 도달 | 124 / 20 (세 팔 같음) | 124 / 20 (세 팔 같음) | 일치 | `wp6_eval_6000.json` `CI_gt_ineligible`, `expert_ready_correct` |
| 평가셋 400·6000분 거짓 확정 (FE·FI) | 0 | 0 (9팔 전부) | 일치 | `wp6_eval_400.json`·`wp6_eval_6000.json` |
| 실행 commit | dba06d9 | result.json 243개 `manifest.environment.commit` 전부 `dba06d9d26b1…`, `wp6_eval_400.json` `head` 같음 | 일치 | 전체 |

- 집계: 21항목 전부 일치, **불일치 0**. 수정할 수치가 없어 이전 값 노트는 만들지 않았습니다.
- 생성기 표준편차는 표본 표준편차(n−1)입니다. 값 6개 — dcvc 800분 [2, 2, 12, 13, 0, 1], no_bundles 800분 [7, 7, 12, 13, 4, 5] (replicate 0·1·2 × seed 1·2 순).
- 파일럿은 seed 1–5에서 값이 모두 같습니다(표준편차 0).
- 평가셋 result_hash(`e487f6ca93a9a302` 등)가 내 실행(`0a798d99c1df5cb5`)과 다른 것은 실험 ID(arm 이름 등)가 spec에 들어가기 때문입니다(선우 03 머리말). 쌍 단위 결과는 내 dba06d9 실행과 같은 47입니다(8-2).
- 슬라이드 6 노트의 "원자료 없음"은 "원자료 대조 완료 + 공유 브랜치 경로"로 바꿨습니다.

## 11. 발표 문서 최종 정리 — 표기 단일화 + 번들링 결론 보정 (실행 없음)

### 11-1. 07cb2e0 ↔ 60b41da 표기 충돌

| 위치 | 07cb2e0 (23시) | 60b41da (24시) | 최종 (이번) |
|---|---|---|---|
| presentation 머리말 | 실행 수치 = b1d2a7f, 8afe5ea 노트 | 5 = dba06d9 기준·8afe5ea 동일(+b1d2a7f 노트), 5-1 = 8afe5ea 기준·b1d2a7f 동일, 1~4 규칙 밖 | 아래 규칙 하나 |
| 슬라이드 1~4 | "기준 커밋: 최종 통합본 b1d2a7f (평가셋 생성 996efd6 …)" | 그대로 (머리말과 충돌) | "평가셋 생성 `996efd6`" |
| 슬라이드 5 | "최종 통합본 b1d2a7f" | "dba06d9 기준 · 8afe5ea 동일 결과 확인" | "최종 통합본 `b1d2a7f` · `dba06d9`·`8afe5ea` 동일 결과" |
| 슬라이드 5-1 | "4f81460 → b1d2a7f (8afe5ea와 같음)" | "8afe5ea 기준 · b1d2a7f 동일" | "최종 통합본 `b1d2a7f` · `8afe5ea` 동일 결과" |
| 슬라이드 6 | "11/08 dba06d9 기준, 최종 통합본 실험 결과 아님" | "11/08 dba06d9 기준 · 최종 통합본 실험 결과 아님" | 같음 (가운뎃점 표기로 통일) |
| demo_final_flow A·보조 1 | phase1-final | "dba06d9 기준 · 8afe5ea 동일 · b1d2a7f에서도 동일" | "최종 통합본 `b1d2a7f` · `dba06d9`·`8afe5ea` 동일 결과" |
| final_review 요약 | 23시·24시 요약 각각 다른 규칙 | – | 24시 요약 행에 "11절 규칙으로 대체" 표시, 이전 절은 기록으로 둠 |

최종 규칙: 슬라이드 1~4 = "평가셋 생성 `996efd6`" / 5 = "최종 통합본 `b1d2a7f` · `dba06d9`·`8afe5ea` 동일 결과" / 5-1 = "최종 통합본 `b1d2a7f` · `8afe5ea` 동일 결과" / 6 = "11/08 dba06d9 기준 · 최종 통합본 실험 결과 아님".
전수 확인: `presentation_data_gt.md`(머리말·슬라이드 1~7·바뀐 점 표), `demo_final_flow.md`(머리말·A·보조 1·말하지 않을 것), 이 문서(요약·2-1·9-2). 8·9·10절 본문의 이전 표기는 그 시점 기록이라 그대로 둡니다.

### 11-2. 번들링 결론 보정

- 이전: "작은 예산에서 적고 큰 예산에서 많음(역전)", "역전 3개 조건(파일럿 400·생성기 800·평가셋 400)" — 400·800분만 강조되어 "번들링 제거가 낫다"로 읽힘
- 최종: **"예산에 따라 방향이 달라짐: 90분은 dcvc 우세, 400·800분은 번들링 제거 우세, 6000분은 차이 없음"** (90분 = 파일럿 8 vs 4, 400분 = 파일럿 14 vs 8·평가셋 51 vs 47, 800분 = 생성기 8±3.69 vs 5±5.87, 6000분 = 평가셋 124 vs 124)
- 슬라이드 6 표에 방향 열을 넣고 파일럿 90분(8 / 4)·평가셋 6000분(124 / 124) 행을 강조. 같은 결론 문장을 presentation 슬라이드 6·7, demo_final_flow "말하지 않을 것", 이 문서 21시 요약·2-1에서 고침

### 11-3. 행동 수

- 발표 문서(`presentation_data_gt.md`, `demo_final_flow.md`)에서 행동 수·step 번호를 뺌 (demo_final_flow B의 step 1767·1726 삭제). 엔진 버전마다 다름(예: 6000분 b1d2a7f 2,236 / dba06d9 2,440, C의 step 1838 → 1835)
- 이 문서(검수 기록)의 행동 수는 노트로만 남김. `demo_nsclc_scenarios.md`는 실행 기록 문서라 step을 그대로 두되, 발표에는 쓰지 않음

## 7. 이 검수에서 바꾼 것 (docs/presentation-data-gt만)

| 파일 | 변경 |
|---|---|
| `docs/dev/presentation_data_gt.md` | 최종본: 슬라이드 6장, 명칭 통일, 수치 최신화, 지역 조건 결함 문장 삭제, 한계 슬라이드 확장 |
| `docs/dev/demo_nsclc_scenarios.md` | 8afe5ea 재확인 반영 (C 교체, D 갱신, 엔진 관찰 고쳐짐), 명칭 통일 |
| `docs/dev/nsclc_eval_dataset.md` | 명칭 통일만 (수치 변경 없음) |
| `docs/dev/demo_final_flow.md` | 신규: 확정 데모 흐름 |
| `docs/dev/final_review_2026-11-08.md` | 신규: 이 문서 |
| `docs/dev/final_review_evidence/` | 확인 실행 결과 (데모 재확인·쌍별 기록·P0 채점·지역 조건 12케이스) |
| `docs/dev/final_review_scripts/` | 대조·집계 스크립트 (엔진 코드 변경 없음, 읽기·실행만) |
| `docs/dev/final_review_evidence/r400_three_commits.json`, `final_review_scripts/r400_one_commit.py` | 22시: 240쌍 400분 full_dcvc 커밋 3개 실행 결과 |
| `docs/proposals/p0_3_xfail_release.patch` | `data/p0-eval-cases`용 xfail 해제 패치 (적용하지 않음) |

재현
```bash
# 통합본 worktree에 data/p0-eval-cases의 tests/data/p0_3_region_cases.json, tests/test_p0_3_region_cases.py, scripts/*p0_fact*, data/eval/p0_fact_cases 복사 (커밋 안 함)
export PYTHONPATH=$PWD/src PYTHONIOENCODING=utf-8 ANTHROPIC_API_KEY=
python -m pytest -q tests/test_p0_3_region_cases.py                       # 8afe5ea: 11 passed, 2 failed (XPASS strict)
python scripts/p0_fact_adapter_offline.py --tag 8afe5ea --out p0.json && python scripts/score_p0_fact_cases.py --results p0.json --md p0.md
python docs/dev/final_review_scripts/demo_with_final.py $PWD <out_dir> 8afe5ea   # scripts/demo_nsclc_scenarios.py 필요, 약 10분
```
