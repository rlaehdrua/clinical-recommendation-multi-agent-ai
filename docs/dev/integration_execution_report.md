# 통합 실행 보고서 — `integration/dcvc` (계약 0.2 후보)

> 실행일 2026-11-02. 실제 LLM·유료 API 호출 0회. 원격 push·PR·main 병합 0건. 기존 브랜치·사용자 파일 삭제 0건.
> 표기: **[확인]** Git·실행으로 확인 / **[추정]** 판단 / **[미확인]** 확인 못 함

## 1. 결과 요약

| 항목 | 결과 |
|---|---|
| 통합 | **성공** — 충돌 0, 신규 5커밋을 `origin/integration/midterm-1026` 위에 cherry-pick |
| 통합 브랜치 / 폴더 | `integration/dcvc` / `C:\Users\jsw58\Desktop\workseoyeon-dcvc` (별도 worktree, upstream 없음) |
| 기준 커밋 | `448a8c5` (`origin/integration/midterm-1026`, fetch 후에도 같은 값) |
| 계약 | `dcvc.engine.contracts/0.2` 후보 구현, `CONTRACT_STATUS = "PROVISIONAL"` 유지 |
| midterm 연결 | `engine/contract_bridge.py`, `engine/question_bridge.py` 구현 + ContractSession 오프라인 E2E 테스트 |
| 테스트 | 통합 직후 **1961 passed / 0 failed / 7 skipped** → 0.2 후 **1996 passed / 0 failed / 7 skipped** |
| 신규 회귀 | **없음** |
| 로컬 커밋 | cherry-pick 5 + 신규 3 (코드 2, 문서 1) |

## 2. 사전 확인과 백업 [확인]

| 확인 | 값 |
|---|---|
| 시작 위치 | `C:\Users\jsw58\Desktop\workseoyeon`, 브랜치 `feat/dcvc-backend-foundation`, HEAD `8bfc0fe` |
| 추적 파일 변경 | 없음 |
| 미추적 파일 | `docs/audit/`(4개), `docs/dev/contract_decision_review.md`, `docs/dev/git_integration_plan.md`, 검토용 합본 2개 = 8개 |
| 신규 5커밋 | `a3ab322`, `9f85acb`, `0c98238`, `7247330`, `8bfc0fe` 모두 존재 |
| Inspector의 midterm 통합 | `866d45a` "From feat/decision-inspector (62f744e) …", `src/ctrec/inspector.py` 존재 |
| worktree | 기존 16개 (이번 작업으로 1개 추가) |

- 백업: `C:\Users\jsw58\Desktop\trialog_backup_20261102\` (미추적 8개, 경로 유지 복사). 원본·사본 SHA-256 8/8 일치. 해시 목록 `C:\Users\jsw58\Desktop\trialog_backup_20261102.sha256`.
- 로컬 태그(새로 만듦, 덮어쓰기 없음): `backup/dcvc-foundation-20261102 → 8bfc0fe`, `backup/decision-inspector-20261102 → 62f744e`. 기존 태그 `backup/midterm-1024-v1`은 그대로.
- `git fetch origin`: `origin/main`(`9fdffc4`)·`origin/integration/midterm-1026`(`448a8c5`) **변경 없음**. 새 원격 브랜치 2개:
  - `origin/research/cohort-generator` (2026-11-02, 작성 계정 `ehdroal`, midterm `448a8c5` 위 3커밋): `src/ctrec/cohort/**`, `scripts/build_cohort_benchmark.py`, `src/ctrec/web/app.py`·`static/*`, `tests/test_cohort_generator.py` 등 25파일. **이번 통합과 경로 충돌 없음** (엔진은 `src/ctrec/engine/`). 다만 WP2(코호트 생성)·WP8(웹)과 내용이 겹칠 수 있음 [추정] → §6
  - `origin/report/llm-pilot-a-1026` (문서 2파일)
- 중단 조건(예상 밖 추적 파일 변경, 기준 브랜치 삭제·대폭 변경, Inspector 없음, 5커밋 식별 불가, 백업 실패): **해당 없음**.

## 3. 기준 브랜치 결정 [확인]

`origin/main`은 midterm보다 34커밋 뒤이고 `inspector.py`·`contract_session.py`·`propositions.py`·`question_contract.py`·`evaluator.py`가 없음 → **midterm을 기준으로 사용**. 새 원격 브랜치 두 개도 `448a8c5` 위에 있어 팀의 작업 기준이 midterm임을 뒷받침함.

기준 브랜치 검증: `contract_session.py`(`ContractSession`), `propositions.py`, `question_contract.py`, `evaluator.py`, `inspector.py`, `contracts/ir/`·`contracts/question_contracts.json`(계약 49개) 존재. 신규 5커밋과 경로 충돌 없음(`git merge-tree` 시뮬레이션).

## 4. 통합 절차와 cherry-pick 결과 [확인]

```
git worktree add -b integration/dcvc C:\Users\jsw58\Desktop\workseoyeon-dcvc origin/integration/midterm-1026
git branch --unset-upstream            # 자동 설정된 upstream(midterm) 제거: 실수로 midterm에 push하지 않게
git cherry-pick -x a3ab322 9f85acb 0c98238 7247330 8bfc0fe
```

| 원본 | 통합 커밋 | 내용 |
|---|---|---|
| `a3ab322` | `b5f3de9` | WP0 계약 I-1~I-9 |
| `9f85acb` | `b01043e` | WP1 평가 위생 |
| `0c98238` | `acf0a75` | WP7 DecisionRecord v2 |
| `7247330` | `7163283` | WP5 정책 엔진 기반 |
| `8bfc0fe` | `396ed95` | 야간 보고서·인수인계 |

충돌 0, 변경 파일 17개 모두 추가(A). `feat/decision-inspector` 4커밋은 병합하지 않음(같은 내용이 midterm에 있음).

## 5. 통합 뒤 추가 커밋 (이번 작업)

| 커밋 | 내용 |
|---|---|
| `4fd479d` | 계약 0.2 안전 경계 (`contracts.py`, `cohort_state.py`, `policy.py`, `audit.py`, `docs/engine_contracts.md`) |
| `8986bad` | midterm 계약 연결 (`contract_bridge.py`, `question_bridge.py`, `experiment.py`, `tests/test_engine_contract_v02.py`) |
| (문서 커밋) | 이 보고서, `contract_v02_changelog.md`, `team_parallel_start.md`, 지난 검토 문서 2개(`contract_decision_review.md`, `git_integration_plan.md`) |

### 신규·수정 파일
| 파일 | 구분 | 비고 |
|---|---|---|
| `src/ctrec/engine/contracts.py` | 수정 | 0.2 후보 (상세: changelog) |
| `src/ctrec/engine/cohort_state.py` | 수정 | `required_gaps`, `pending_kinds`, 출처 보충(overrides) |
| `src/ctrec/engine/policy.py` | 수정 | `purpose_of`, `info_key` 전달, 정책 전이 사유, `POLICY_VERSION = 0.2-provisional` |
| `src/ctrec/engine/audit.py` | 수정 | `pair_reasons`·`pair_pending`, `reversal_issues`, v2 뷰에 사유·남은 작업 |
| `src/ctrec/engine/contract_bridge.py` | 신규 | 평가기·ContractSession → CriterionResult |
| `src/ctrec/engine/question_bridge.py` | 신규 | Action → ClarifyingQuestion 경계 |
| `src/ctrec/engine/experiment.py` | 신규 | 카탈로그 해시 검사·주장 범위 |
| `tests/test_engine_contract_v02.py` | 신규 | 35개 |
| `docs/engine_contracts.md` | 수정 | 머리말 + §7 변경 기록 |
| `docs/dev/*.md` | 신규 | 문서 5개 |

수정하지 않은 것 [확인]: `data/`, `contracts/`, `pipeline.py`, `agents/**`, `decision_state.py`, `decision_policy.py`, `web/**`, `inspector.py`, `evaluator.py`, `contract_session.py`, `question_contract.py`, `propositions.py`, `proposition_store.py` (`git diff --name-only` 확인, 해당 경로 0개).

## 6. 테스트 [확인]

명령 (worktree에서, 원 저장소 venv 사용, API 키 제거):
```bash
cd /c/Users/jsw58/Desktop/workseoyeon-dcvc
PYTHONPATH="$(pwd)/src" env -u ANTHROPIC_API_KEY /c/Users/jsw58/Desktop/workseoyeon/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -rfEs
```
PowerShell:
```powershell
Set-Location C:\Users\jsw58\Desktop\workseoyeon-dcvc
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
$env:PYTHONPATH = "C:\Users\jsw58\Desktop\workseoyeon-dcvc\src"
& C:\Users\jsw58\Desktop\workseoyeon\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rfEs
```
`import ctrec`가 worktree의 `src`를 가리키는지 확인함.

| 실행 | 결과 |
|---|---|
| 통합 직후, 신규 4개 파일 | 113 passed, 1 skipped |
| 통합 직후, 전체 | **1961 passed, 0 failed, 7 skipped** (205초). 이전 시뮬레이션(`.git` 없는 사본)의 실패 1건(`test_frozen_store_and_policy_files_unchanged`)은 실제 worktree에서 통과 |
| 0.2 수정 뒤 엔진·Inspector 관련 (`test_engine_*`, `test_eval_hygiene`, `test_inspector`, `test_p9_inspector_contract`) | 239 passed, 5 skipped |
| `tests/test_engine_contract_v02.py` | 35 passed |
| 0.2 뒤 전체 | **1996 passed, 0 failed, 7 skipped** (205초) |

skip 7건: 브라우저 테스트(`CTREC_BROWSER_TESTS=1` 필요) 1, Windows 심볼릭 링크 불가 6 (`test_cli_records` 1, `test_engine_audit` 1, `test_inspector` 4). 모두 기존과 같은 환경 사유.
정책 엔진 기존 테스트(`test_engine_policy.py`, 36건)·Inspector v1(`test_inspector.py`)·v2(`test_engine_audit.py`) 회귀 없음. 0.1 계약 테스트 표의 기대값은 **하나도 바꾸지 않았음**(새 경계는 표에 없던 조합만 바꿈).

## 7. 구현하지 못한 사항 (의도적으로 남김)

| 항목 | 이유 | 다음 담당 |
|---|---|---|
| `decision_state._record`가 `method="contract"`를 `engine="llm"`으로 기록 | 보호 파일(`decision_*.py`). 엔진 쪽은 `contract_bridge`의 출처 보충(overrides)으로 우회. 기존 파일 수정은 정책 담당 승인 필요 | 동겸(정책) + 서연(판정) 합의 |
| 기준 성격 분류 `RuleMeta.adjudication`(M12) | 임상 판정 규칙의 의미 결정이 필요. 잠정 규칙 `provisional_adjudication`만 둠(underspecified → expert) | 서연 WP3 |
| 정책이 `question_contract_id`를 자동으로 붙이기 | 활성 IR 출처(ContractSession 실행 상태)가 있어야 함. `contract_bridge.contract_candidates` + `question_bridge.attach_question_contract`만 제공 | 동겸 WP8 |
| 웹 실행 경로 결정 (Session vs ContractSession) | [확인] `web/app.py:437`과 `cli.py:176`은 `session_class()` 사용 → `CTREC_SESSION=contract`일 때만 ContractSession, **기본은 기존 Session**. 운영 기본값을 바꿀지는 팀 결정 | 팀 |
| 정보원 비용 실측값 | 근거 자료 없음. 가정값 표시 + 해시 기록 + 주장 제한으로 대체 | 서연 WP6 |
| B-1(answerer 주입)·B-4(평가 기본 경로 별칭화) | 서연 담당 파일 (`question_eval.py` 등) | 서연 |
| midterm ↔ `origin/main` 충돌 (README, matcher.py, pipeline.py) | 이번 범위 밖 | midterm 담당 |
| `research/cohort-generator`와의 정렬 | 이번 fetch에서 처음 보임. 통합하지 않음 | 해당 작성자 + 서연 |

## 8. 남은 위험

1. **쌍 상태 의미 변경**: INELIGIBLE이라도 불충족 근거가 문서 검증되지 않으면 VERIFICATION_PENDING. 자기 보고 허용 기준(self_report_ok)에서 평가기가 NOT_MET을 낸 경우도 쌍 수준에서는 "확정 부적격"이 아님. 판정 값(eligibility)은 그대로이므로 화면·지표에서 어느 값을 보일지 명확히 해야 함.
2. 0.1에서 ELIGIBLE + stale은 VERIFICATION_PENDING이었고 0.2에서는 INFO_MISSING(정보 없음 우선). 확정이 아닌 상태끼리의 이동이지만 이 값을 쓰는 코드가 생기기 전에 공지 필요.
3. `SourceCatalog.digest()` 값이 바뀜(`cost_basis` 필드 추가). 이전 해시를 저장한 실험 기록은 없음 [추정: 저장소에 실험 실행 기록 없음].
4. `reversal_issues`는 기록이 생긴 뒤의 사후 검사. 실제 생산자(WP3/WP4)가 확정 하향 시 사유 코드를 넘기지 않으면 Inspector에 issue로 보임(의도된 동작).
5. `eval_hygiene`의 모듈 속성 교체(동시 실행 불가)는 그대로(야간 보고서 §6-3).
6. 이 worktree는 원 저장소 `.venv`를 `PYTHONPATH`로 씀. 원 저장소 venv가 editable 설치라면 PYTHONPATH 없이 실행하면 **원 저장소 src**를 import함 → 반드시 위 명령 사용.

## 9. 보존 상태와 현재 Git 상태 [확인]

| 대상 | 상태 |
|---|---|
| `feat/dcvc-backend-foundation` (`8bfc0fe`) | 그대로, 원 폴더에서 checkout 상태 유지 |
| `feat/decision-inspector` (`62f744e`) | 그대로 |
| 태그 `backup/*` 3개 | 존재 |
| 원 폴더 미추적 파일 8개 | 그대로 + 저장소 밖 백업 |
| 기존 worktree 16개 | 변경 없음 |
| `integration/dcvc` | 로컬 전용, upstream 없음, push 안 함 |

`integration/dcvc`의 최종 HEAD와 `git status`는 문서 커밋 뒤 최종 보고에 적습니다(이 문서는 그 커밋 안에 있어 자기 해시를 담을 수 없음).
