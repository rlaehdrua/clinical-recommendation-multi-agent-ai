# P 검증 기록과 PR 준비 (2026-11-07)

## 1. P 고유 변경 (610bd49..3e4b2fd, 커밋 2개, 파일 18개)

| 구분 | 파일 |
|---|---|
| 코드 (6) | `src/ctrec/engine/loop.py` (신규), `src/ctrec/engine/dcvc.py`, `src/ctrec/sim/runner.py`, `src/ctrec/sim/suite.py` (신규), `src/ctrec/sim/nsclc_pilot.py` (신규), `src/ctrec/web/engine_runner.py` |
| 테스트 (4) | `tests/test_p_common_engine.py` (신규), `tests/test_engine_baselines.py`, `tests/test_engine_runner.py`, `tests/test_recovery_r1_r2.py` |
| 보고서 (2) | `docs/dev/p_common_engine_2026_11_07.md` (신규), `README.md` (변경 항목 1절) |
| 실험 결과 (6) | `docs/dev/p_results/{demo,nsclc_pilot,generator_neph_k3}.{tsv,json}` |

이 브랜치(`pr/p-common-engine`)의 증거 커밋은 이 문서, `wp6_hashes/`, `engine_mode_forced_failures.txt`를 더합니다.

## 2. 보존한 검증 결과 (코드 c62f591)

| 검사 | 결과 |
|---|---|
| 전체 pytest (기준 610bd49) | 5330 passed (P 전 a42d50d 기준선), 5388 passed (610bd49 기준선, Q 보고와 같음) |
| 전체 pytest (P 최종, 테스트 3건 갱신 후) | **5420 passed, 9 skipped, 2 xfailed** (522s) |
| 엔진 모드 강제 (`CTREC_POLICY_MODE=engine`) | 16 failed, 5404 passed — `engine_mode_forced_failures.txt`. 웹 기본값은 바꾸지 않음 |
| WP6 해시: 루프 리팩터만 (DCV 변경 전) | 56/56 동일 — `wp6_hashes/loop_refactor_only.json` vs `base_610bd49.json` |
| WP6 해시: P 최종 | 56개 중 8개 변경, 모두 `full_dcvc` (기준선·oracle 48개 동일) — `wp6_hashes/comparison.json` |

해시 범위: 합성 데모 + 생성기 neph-k3-catalog-source r0, 정책 7종 + oracle_upper, 예산 20·400분, rephrase 끔·켬, seed 1, horizon 10.
재현: `PYTHONPATH=src python docs/dev/p_results/wp6_hashes/snapshot_hashes.py out.json` (기준 커밋을 체크아웃한 worktree에서).

## 3. Base 판단

- P의 올바른 Base는 **610bd49** (`local/l-k-check`)입니다. 모든 테스트·해시·실험 결과를 이 위에서 측정했습니다.
- 610bd49는 **원격에 없습니다.** 원격에서 610bd49를 담은 브랜치는 P 자신(`feat/p-common-engine`)뿐입니다.
- 610bd49는 원격 PR 코드(#22, #24, #26–#30, #32, #33, #31의 4fc465e, #34의 f002bde)에 **로컬 전용 커밋 4개**를 더한 것입니다.
  - `9690a89` merge "PR 20 recovery onto the integrated stack (local check, not for PR)": `engine/cohort_state.py` 충돌 해소 (I x J, items_exhausted). 이 내용은 원격의 어느 브랜치·PR에도 없습니다.
  - `76dbbb2` "pin the blocked count under the document gate (local check, not for PR)": `tests/test_recovery_stress.py` 기대값 BLOCKED 17.
  - `36ccdef`, `610bd49`: 충돌 없는 merge.
- `origin/integration/dcvc`(290420d = 64a5011 + #24)에는 #22, #26–#34가 아직 없습니다. 그래서 그곳을 Base로 두면 P와 무관한 선행 변경 수십 개가 diff에 섞입니다.
- 지시에 따라 **Base를 임의로 고르지 않았습니다.** 다른 원격 PR 브랜치(예: #33 `fix/pr20-recovery-r1-r2`)를 Base로 쓰면 9690a89의 충돌 해소가 빠져 측정한 코드와 달라집니다.
- PR을 열 수 있는 조건: 610bd49를 조상으로 갖는 브랜치가 원격에 생기면 그 브랜치를 Base로 지정합니다. 예를 들어 Q의 `integration/q-final-2`는 merge-base(P)가 정확히 610bd49입니다. 그러면 PR diff는 P 고유 변경만 보여 줍니다(merge-base..head = 이 문서 §1 + 증거 커밋). 이 브랜치를 원격에 올리는 일은 Q 작업자의 결정입니다.

## 4. Q `integration/q-final-2` (f3c8d71) 병합 가능성 — 읽기 전용

- Q는 610bd49를 포함하고 P는 포함하지 않습니다. merge-base = 610bd49이고, 두 쪽이 함께 바꾼 파일은 0개입니다.
- `git merge-tree --write-tree`: **충돌 없음** (tree ad937bb).
- 병합 트리 전체 pytest (참조 없는 임시 커밋 3902e77, scratchpad 분리 worktree, 검사 뒤 제거, Q 브랜치·worktree 변경 없음): **6 failed, 5461 passed, 9 skipped, 7 xfailed.**
  - 실패 6건은 모두 `tests/test_followup_flow.py`(O)입니다.
  - 원인: P의 근거 경로(`full_dcvc` 0.2, `evidence_routing`)가 진료 기록 근거가 필요한 기준(lab:platelets·lab:hemoglobin·biomarker:EGFR 등)의 환자 질문을 문서 요청으로 바꿉니다. O의 테스트는 이 제안이 `ASK_PATIENT`라고 기대합니다.
  - 확인: 같은 병합 트리에서 `evidence_routing` 기본값만 끄면 `test_followup_flow.py` 10/10이 통과합니다.
- 계약 충돌(팀 결정 필요): O의 후속 확인 서비스(`followup/service.py`)는 `FullDCVCPolicy`를 직접 만들어 `select`·`apply_outcome`을 부릅니다. 즉 `engine.loop.EngineLoop`를 거치지 않는 **세 번째 선택 경로**이며, P의 반복 억제·실행 배분 규칙을 공유하지 않습니다. 선택지:
  1. O가 새 DCV 동작(기록 필요 기준은 문서 먼저)을 받아들이고 테스트를 갱신
  2. O가 `FullDCVCPolicy(evidence_routing=False)`로 0.1 동작을 고정
  3. O의 계획·정산을 `EngineLoop`로 옮김 (공통 엔진 원칙에 맞음)
- `followup/service.py`가 쓰는 `EngineRunner.render`는 P에서도 같은 문구를 냅니다 (`EngineRunner`는 `CohortRunner`의 이전 이름).

## 5. 미해결 (변경하지 않음)

1. **웹 기본 모드는 legacy 그대로입니다.** 엔진 모드를 웹 전체 기본값으로 바꾸지 않았습니다. 강제하면 16건이 실패합니다(엔진 질문이 자유 입력뿐이고 선택형 answer_type이 없음).
2. **NSCLC 국가 조건 오류**: NCT06216301 I1 "미국에서 22세 이상"이 국가 조건 없이 나이 기준으로 파싱됩니다. 그래서 한국 20세 합성 환자(SYN-NSCLC-03)가 확정 부적격이 됩니다(거짓 부적격 1, 90분 예산). 파서·국가 조건 문제이며 P는 고치지 않았습니다. `test_nsclc_us_only_age_rule_does_not_confirm_ineligible`는 strict xfail입니다.
3. **번들링 성능 역전**: 생성기 코호트 800분 예산에서 `dcvc-no_bundles`가 확인된 부적격 11.17로, `dcvc` 8.17보다 많습니다(200분에서는 dcvc 7.5 > 6.83). 묶음 할인 점수식(`FullDCVCPolicy.cost_minutes`)을 재검토해야 합니다. 원인은 분석하지 않았습니다.
4. §4의 O–P 계약 충돌과 세 번째 선택 경로.
5. 610bd49의 로컬 전용 충돌 해소(9690a89)와 테스트 고정(76dbbb2)을 원격에 올리는 방법은 팀 결정입니다.
