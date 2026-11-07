# P — DCV + Cohort C 공통 엔진 (2026-11-07)

브랜치 `feat/p-common-engine` (기준: `local/l-k-check` 610bd49 = integration 64a5011 + #20·#22·#24·G·J·H·I·L, Q 후보의 기준과 같음).
결과 파일: `docs/dev/p_results/*.tsv|json`. 모든 수치는 합성 환자·가정 비용·오프라인 평가기 기준이며 임상 성능이 아닙니다.

## 1. 공통 엔진 경로

```
            ┌──────────── engine.baselines.make_policy (같은 등록표, 기본 full_dcvc) ────────────┐
            │                                                                                  │
 웹·CLI  Session ─ SessionView ─ build_state ┐                ┌─ build_state ─ SessionView ─ OfflineEvaluator  WP6
 (web.engine_runner.CohortRunner)             ├─ CohortState ─┤                              (sim.runner.run)
                                              │               │
                              engine.loop.EngineLoop  ← 두 경로가 같은 클래스·같은 메서드
                                plan      = propose → 반복 억제(repeat_key) → select(남은 예산 원장)
                                dispatch  = 실행 가능 / 이 경로에서 실행 불가 / 라운드 한도
                                begin     = 시도 기록 + 실행했을 때만 청구
                                settle    = policy.apply_outcome (운영 상태만, 판정 불변)
                                              │
              실행 수단만 다름:  화면 질문 + answers.answer_outcome   |   CohortEnvironment (GT를 쥔 유일한 곳)
```

- `sim/runner.py`, `web/engine_runner.py` 모두 `policy.select`·`policy.apply_outcome`을 직접 부르지 않습니다 (AST 검사 테스트).
- 웹의 코호트 실행: `engine_runner.run_cohort(sessions)` / `activate_cohort` — 여러 환자 Session을 한 정책·한 원장·라운드당 한 계획으로.
  Session이 하나면 기존 `activate`와 같습니다 (`EngineRunner`는 `CohortRunner`의 이전 이름).

## 2. 조사한 차이와 조치

| # | 차이 (조치 전) | 조치 |
|---|---|---|
| D1 | 반복 억제·청구·정산을 시뮬레이터와 웹이 각자 구현 (`sim.runner._repeat_key`, 웹 `_repeat_key` 사본) | `engine/loop.py` 하나로. WP6 해시 56/56 동일 (리팩터만, DCV 변경 전) |
| D2 | 웹에서 실행할 수 없는 선택 행동(VERIFY_RECORD·임상의 판단 등)은 기록 없이 매 라운드 다시 선택됨. 시뮬레이터는 not_executed로 정산 | 웹도 not_executed로 정산·시도로 셈 (청구 0). 시뮬레이터는 system 정보원을 실행하므로 이 차이는 **실행 수단 차이**로 남음 |
| D3 | 웹 `cohort()`가 모든 시험을 최신 판정으로 표시 → Session이 다시 매칭하지 않은 INELIGIBLE 시험의 stale을 숨김 (엔진 감사 J4) | 실제 판정 시점 사실 버전을 기록하고, 엔진 모드에서 사실이 바뀐 뒤 오래된 시험을 재매칭에 더함 = 시뮬레이터의 환자 단위 풀 재평가와 같은 상태. 대가: 엔진 모드에서 답이 사실을 바꾸면 INELIGIBLE 시험도 다시 매칭(LLM 비용) |
| D4 | 웹 기본 정책 `legacy_greedy`, 실험 제안 정책 `full_dcvc` | 엔진 모드 기본 정책을 `full_dcvc`로 (같은 클래스) |
| D5 | 웹은 환자 하나만. Cohort C(공유 예산·번들·환자별 최소 배정)는 시뮬레이터에만 | `CohortRunner`/`run_cohort` |
| D6 | **기본 웹 모드는 여전히 legacy(LLM 질문 생성기)** | 바꾸지 않음 — §8 결정 필요 |

## 3. DCV 변경 (full_dcvc 0.2-draft)

- **근거 경로** (`evidence_routing`, 기본 켬): 환자 질문의 대상 기준이 모두 `required_evidence = clinical_record`이면, 같은 항목을 담은 문서 정보원이 남아 있을 때 문서 요청(`record_evidence_required`)으로 바꿉니다. 실패한 정보원·이미 요청한 (문서, 항목)은 쓰지 않고, 남은 문서가 없으면 환자 질문 그대로. 기준의 근거 요건만 보며 확률을 쓰지 않습니다.
- **결정 단락**: 불충족(NOT_MET)으로 판정된 필수 기준의 근거를 확인하는 행동은 그것만으로 쌍을 닫으므로 점수에서 그 쌍의 남은 수를 1로 셉니다.
- 원인: 생성기·NSCLC 코호트에서 0.1은 예산을 "환자가 알 수 없는 / 환자 답으로 확정되지 않는" 질문에 먼저 다 썼습니다 (예산 400분, 341행동 중 ASK 339, 무응답 218, 확정 0).
- WP6 해시: 56개 중 `full_dcvc` 8개만 바뀜, 기준선·oracle은 동일.

## 4. 일치 검증 (tests/test_p_common_engine.py, 31 통과 + strict xfail 1)

| 검증 | 방법 |
|---|---|
| 같은 객체 | 시뮬레이터 run과 웹 activate 모두 `EngineLoop.plan`·`settle`을 부르고 정책 클래스가 같음 (spy) |
| 웹 = 공통 루프 | 웹 실행의 라운드 기록(같은 초기 상태·같은 결과)을 새 `EngineLoop`로 재생 → 라운드마다 선택 ID·배분·최종 운영 상태·시도·원장이 같음. 시나리오: 기본("네"), 무응답("모름"), 무응답 뒤 문서 확인 성공. 코호트(환자 2명)도 같은 재생 |
| 실패(연락 불가) | 시뮬레이터 응답 모델로 (웹에는 연락 불가 결과가 없음): 실패 → 문서 요청, 회복 제거 시 문서 요청 감소 |
| 증분 = 풀 | 시뮬레이터: 환자 단위 재평가 최종 상태 = 종료 사실로 전원 다시 평가한 상태 (full_dcvc·patient_order). 웹: 엔진 모드 종료 시 판정이 오래된 시험 0 |
| 반복 금지 | 웹 같은 repeat_key·같은 문구+대상 질문 0, 시뮬레이터 같은 행동 재실행 0 |
| 성공 확률 비사용 | 응답 모델을 바꿔도 첫 계획 동일 |
| GT 격리 | 등록표 정책은 env·labels를 받지 않음, 실험 팔에 oracle 없음, 라벨은 지표 계산에만 |
| 임상의 판단 | expert 기준은 끝까지 UNKNOWN, CLINICIAN_REVIEW는 사실을 만들지 않음 |
| 미해결 유지 | 종료 시 쌍 상태를 바꾸지 않음 (unresolved = waiting + unresolved_no_action) |

검증하지 못한 것: Session의 Matcher(LLM 대역)와 OfflineEvaluator가 **같은 사실에서 같은 DecisionState**를 만드는지는 이번 범위 밖입니다. 재생 검증은 판정 이후(정책·루프·상태 전이)의 일치입니다. 시뮬레이터는 행동마다 재평가하고 웹은 라운드 끝에 한 번 재매칭하므로 라운드 안의 정산 순서가 다를 수 있습니다.

## 5. 실행 명령

```bash
export PYTHONPATH=$PWD/src PYTHONIOENCODING=utf-8
python -m ctrec.sim.suite --cohort demo  --budgets 20,45,90 --seeds 1,2,3 --json docs/dev/p_results/demo.json
python -m ctrec.sim.suite --cohort nsclc --budgets 30,90    --seeds 1,2,3 --json docs/dev/p_results/nsclc_pilot.json
python -m ctrec.sim.suite --cohort generator --scenario neph-k3-catalog-source --replicate 0,1,2 \
       --budgets 200,800 --seeds 1,2 --json docs/dev/p_results/generator_neph_k3.json
python -m pytest -q tests/test_p_common_engine.py      # 일치 검증
python -m pytest -q                                     # 전체
```

팔: `no_action`(기준점), `random`, `patient_independent`(patient_order), `cost_first`(cheapest_first), `dcvc`(full_dcvc), ablation `dcvc-no_bundles`·`dcvc-no_dcv`·`dcvc-no_recovery`, 추가 `dcvc-no_evidence_routing`(0.1 동작). 모든 팔이 같은 후보·같은 안전 규칙(안전 필수 먼저·예산 초과 거부·중복 제거)·같은 루프.

## 6. 결과 (seed·replicate 평균)

### NSCLC 파일럿 — 실제 프로토콜 6개 x 합성 환자 8명 (48쌍), GT = 작성자 원문 판독 부적격 라벨 15쌍

| 예산 | 팔 | 확인된 Ineligible | False Ineligible | 확정 Eligible | Coverage | GT 범위 | Unresolved | 행동 | 가정 비용(분) | 배정 0 환자 |
|---|---|---|---|---|---|---|---|---|---|---|
| 30 | random | 0 | 0 | 0 | 0 | 0 | 48 | 10 | 30 | 4 |
| 30 | patient_independent | 0 | 0 | 0 | 0 | 0 | 48 | 17 | 30 | 7 |
| 30 | cost_first | 0 | 0 | 0 | 0 | 0 | 48 | 19.3 | 30 | 2 |
| 30 | **dcvc** | **6** | 0 | 0 | 0.125 | 0.40 | 42 | 9 | 30 | 7 |
| 30 | dcvc-no_bundles | 2 | 0 | 0 | 0.042 | 0.13 | 46 | 8 | 30 | 1 |
| 30 | dcvc-no_dcv | 6 | 0 | 0 | 0.125 | 0.40 | 42 | 9 | 30 | 7 |
| 30 | dcvc-no_recovery | 6 | 0 | 0 | 0.125 | 0.40 | 42 | 9 | 30 | 7 |
| 30 | dcvc-no_evidence_routing | 2 | 0 | 0 | 0.042 | 0.13 | 46 | 4 | 30 | 6 |
| 90 | random | 0 | 0 | 0 | 0 | 0 | 48 | 48.7 | 90 | 0 |
| 90 | patient_independent | 0 | 0 | 0 | 0 | 0 | 48 | 77 | 90 | 7 |
| 90 | cost_first | 0 | 0 | 0 | 0 | 0 | 48 | 80 | 90 | 2 |
| 90 | **dcvc** | **8** | **1** | 0 | 0.188 | 0.60 | 39 | 28 | 90 | 5 |
| 90 | dcvc-no_bundles | 4 | 1 | 0 | 0.104 | 0.33 | 43 | 18 | 90 | 0 |
| 90 | dcvc-no_dcv | 8 | 1 | 0 | 0.188 | 0.60 | 39 | 28 | 90 | 5 |
| 90 | dcvc-no_recovery | 8 | 1 | 0 | 0.188 | 0.60 | 39 | 28 | 90 | 5 |
| 90 | dcvc-no_evidence_routing | 4 | 1 | 0 | 0.104 | 0.33 | 43 | 33 | 90 | 4 |

- False Ineligible 1 = SYN-NSCLC-03(20세) x NCT06216301: I1 "미국에서 22세 이상"이 무조건 나이 기준으로 파싱된 기존 결함(N4·R3). strict xfail로 고정.
- 확정 Eligible 0은 설계상 당연 (판정기 없음 → LLM 기준 판정 불가). False Eligible 0은 "만들 수 없는 상태에서 0"이라 안전성 증거가 아님.
- 기준선 셋은 이 예산에서 확정 0: 환자 질문(값을 알아도 자기 보고는 진료 기록 필요 기준을 확정하지 못함)에 예산을 씀.
- dcvc는 적은 환자에게 몰림 (배정 0 환자 5~7/8). 실험 예산의 `per_patient_floor_minutes`가 0이라 최소 배정이 꺼져 있음 — 공정성 비교에는 floor를 둔 실행이 더 필요.
- 이 코호트에서 no_dcv·no_recovery는 dcvc와 같음 (닫힌 쌍 행동·실패 회복이 결과를 바꿀 상황이 없음).

### 합성 데모 (6x3, 독립 GT 없음 → 정확도 칸 미검증)

| 예산 | random | patient_ind. | cost_first | dcvc | no_bundles | no_dcv | no_recovery | no_evidence_routing |
|---|---|---|---|---|---|---|---|---|
| Coverage 20분 | 0.019 | 0 | 0 | 0.111 | 0.111 | 0.111 | 0.111 | 0 |
| Coverage 45분 | 0.074 | 0.037 | 0 | 0.167 | 0.074 | 0.167 | 0.167 | 0 |
| Coverage 90분 | 0.056 | 0.130 | 0.056 | 0.259 | 0.167 | 0.259 | 0.241 | 0 |
| 행동 수 90분 | 17.7 | 22.7 | 28.7 | 22.3 | 10.0 | 22.3 | 22.0 | 27.0 |
| 남은 행동 없는 미해결 90분 | 0 | 0.33 | 0 | 2 | 1 | 2 | 4 | 0 |

- 회복 제거는 BLOCKED_FAILURE를 만들고(seed 1: 3쌍) 확정 적격 1건을 잃음. 순환 규칙 GT 불일치는 모든 팔에서 0 (구현 일관성 검사일 뿐).

### 생성기 코호트 (neph-k3-catalog-source, 환자 20 x 시험 3, replicate 0·1·2)

GT = 생성기 쌍 라벨 (합성, Matcher·근거 게이트·정책과 별개 경로, 수치 비교는 같은 규칙 엔진). 60쌍 중 라벨 있는 쌍은 부적격 약 29개, 적격 0.
6회 = replicate 3 x seed 2 평균.

| 예산 | 팔 | 확인된 Ineligible | False Ineligible | False Eligible | Coverage | GT 범위 | Unresolved | 행동 | 가정 비용(분) | 배정 0 환자 | 지니 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 200 | random | 0.83 | 0 | 0 | 0.014 | 0.025 | 59.2 | 68 | 200 | 0.5 | 0.23 |
| 200 | patient_independent | 1.0 | 0 | 0 | 0.017 | 0.032 | 59.0 | 153 | 200 | 16 | 0.86 |
| 200 | cost_first | 0 | 0 | 0 | 0 | 0 | 60 | 166 | 200 | 11 | 0.75 |
| 200 | **dcvc** | **7.5** | 0 | 0 | 0.125 | 0.22 | 52.5 | 109 | 200 | 9 | 0.60 |
| 200 | dcvc-no_bundles | 6.83 | 0 | 0 | 0.114 | 0.20 | 53.2 | 28 | 200 | 6 | 0.47 |
| 200 | dcvc-no_dcv | 7.5 | 0 | 0 | 0.125 | 0.22 | 52.5 | 109 | 200 | 9 | 0.60 |
| 200 | dcvc-no_recovery | 7.5 | 0 | 0 | 0.125 | 0.22 | 52.5 | 109 | 200 | 9 | 0.60 |
| 200 | dcvc-no_evidence_routing | 6.83 | 0 | 0 | 0.114 | 0.20 | 53.2 | 58 | 199.5 | 7.7 | 0.57 |
| 800 | random | 5.17 | 0 | 0 | 0.086 | 0.15 | 54.8 | 499 | 800 | 0 | 0.11 |
| 800 | patient_independent | 4.33 | 0 | 0 | 0.072 | 0.13 | 55.7 | 600 | 800 | 8 | 0.44 |
| 800 | cost_first | 0 | 0 | 0 | 0 | 0 | 60 | 699 | 800 | 0 | 0.18 |
| 800 | dcvc | 8.17 | 0 | 0 | 0.136 | 0.24 | 51.8 | 578 | 800 | 0 | 0.21 |
| 800 | **dcvc-no_bundles** | **11.17** | 0 | 0 | 0.186 | 0.32 | 48.8 | 433 | 800 | 0 | 0.10 |
| 800 | dcvc-no_dcv | 8.17 | 0 | 0 | 0.136 | 0.24 | 51.8 | 578 | 800 | 0 | 0.21 |
| 800 | dcvc-no_recovery | 8.17 | 0 | 0 | 0.136 | 0.24 | 51.8 | 577 | 800 | 0 | 0.21 |
| 800 | dcvc-no_evidence_routing | 6.83 | 0 | 0 | 0.114 | 0.20 | 53.2 | 576 | 800 | 2.7 | 0.35 |

- 200분에서는 dcvc가 기준선보다 확인된 부적격이 많습니다 (7.5 vs 0~1). **800분에서는 번들 제거 쪽이 더 많습니다 (11.2 vs 8.2).** 묶음 할인 점수가 이미 요청한 정보원의 싼 추가 항목을 계속 고르고, 결정적인 다른 문서를 미루기 때문으로 보입니다 (원인 분석은 하지 않음). 번들 점수식은 재검토가 필요합니다.
- 회복 제거: 확정 수는 같지만 남은 행동 없는 미해결이 생깁니다 (800분 6.7쌍, BLOCKED). 결정 영향 필터 제거는 이 코호트에서 차이 없음.
- 적격 라벨이 0이라 확인된 Eligible·False Eligible은 측정 대상이 없습니다 (확정 적격 0).
- 20~90분에서는 모든 정책이 확정 0이었습니다 (설정 예산 `budgets_min`과 실험 규모가 맞지 않음).

## 7. 정확히 검증 가능한 지표 범위

| 지표 | 데모 | NSCLC 파일럿 | 생성기 |
|---|---|---|---|
| 확정 Eligible 정확도 | 미검증 (GT 없음) | 측정 불가 (적격 라벨 없음, 확정 적격도 구조상 불가) | 측정 불가 (생성기 적격 라벨 0) |
| False Eligible | 미검증 | 무의미 (만들 수 없음) | 측정 가능하나 확정 적격이 나오지 않음 |
| 확정 Ineligible 정확도 | 미검증 | 작성자 판독 15쌍만 (임상 검수 전) | 생성기 라벨 (합성, 같은 규칙 엔진의 수치 비교 공유) |
| Coverage·Unresolved·행동·가정 비용 | 측정 (상대 비교) | 측정 | 측정 |
| 공정성 | 배정 0 환자·지니 (floor 꺼짐) | 같음 | 같음 |
| 순환 규칙 GT 불일치 | 일관성 검사만 | 같음 | 같음 |

실제 임상 GT는 없습니다. 가정 비용은 `relative_policy_comparison_only`이고, 실제 업무시간 절감은 주장하지 않습니다.

## 8. 미해결 — 결정·연구·임상 가정

1. **웹 기본 모드 (팀 결정)**: 기본은 `CTREC_POLICY_MODE=legacy`(LLM 질문 생성기)로, README의 WP8 결정입니다. 엔진 모드를 강제하면 전체 테스트 중 16건이 실패합니다. 엔진 질문은 자유 입력뿐이라 선택형 답·형식 검증·브라우저 흐름이 깨집니다 (`test_team_integration`·`test_web_integration`·`test_web_security`·`test_p14_backend_followups`). 기본을 엔진으로 바꾸려면 엔진 질문 렌더러가 선택형 answer_type을 내야 합니다. **그 전까지 웹 기본 경로는 공통 정책을 쓰지 않으므로, 이 항목은 완료가 아닙니다.**
2. 운영에는 문서 권한이 없습니다(NoDocumentAuthority). 따라서 근거 경로가 웹에서 낸 문서 요청의 답도 자기 보고 수준입니다. 실험의 확정 수는 미래의 서버 문서 계약을 가정한 값입니다.
3. 평가기 일치(Session Matcher vs OfflineEvaluator)는 검증하지 않았습니다.
4. CLI에는 코호트 실행 옵션이 없습니다 (`run_cohort` API만). 웹에는 여러 환자를 묶는 화면이 없습니다.
5. full_dcvc의 식(점수·결정 단락·근거 경로)은 정의 합의 전 초안입니다 (#13 안건 ①).
6. 생성기 nephrotic 코호트는 적격 라벨이 0이고, 쌍마다 미확인 기준이 약 25개입니다. 20~90분 예산에서는 어떤 정책도 확정하지 못하므로 예산 범위를 재설정해야 합니다.
7. NCT06216301 국가 조건 나이 기준 결함 (파서).
8. 공정성: 실험 예산에 환자별 최소 배정(`per_patient_floor_minutes`)이 없어 fairness_floor ablation은 효과가 없습니다.
9. 응답 모델 확률·정보원 비용·환자가 아는 항목은 모두 가정값입니다.
