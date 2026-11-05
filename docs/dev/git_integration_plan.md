# Git 통합 계획 — `feat/dcvc-backend-foundation` 신규 5커밋

> 작성일 2026-11-02. 이 문서는 계획만 담습니다. 작성 중 **merge·rebase·cherry-pick·commit·push·PR·브랜치 전환을 하지 않았습니다.**
> 표기: **[확인]** Git·실행으로 확인 / **[추정]** 판단 / **[미확인]** 확인 못 함

## 0. 결론 (요약)

- 권장안: **C′ — 별도 작업 폴더(worktree)에 `integration/dcvc` 브랜치를 `origin/integration/midterm-1026`(팀이 main 기준을 바꿨다면 그 최신 main)에서 만들고, 신규 5커밋만 cherry-pick.**
- 이유
  1. 신규 5커밋은 **새 파일 17개만 추가**합니다 [확인]. 어느 기준에 올려도 텍스트 충돌이 없습니다 [확인: 병합 시뮬레이션].
  2. `feat/decision-inspector`의 Inspector 작업(4커밋)은 이미 원격 `integration/midterm-1026`에 통합되어 있습니다(`866d45a` "From feat/decision-inspector (62f744e) …") [확인]. 현재 브랜치를 그대로 합치면 `src/ctrec/inspector.py`에서 **add/add 충돌**이 납니다 [확인: 시뮬레이션].
  3. `origin/main`에는 `inspector.py`가 없어서, main에 5커밋만 올리면 `tests/test_engine_audit.py`가 import 오류로 실패합니다 [확인: 시뮬레이션 트리 실행].
  4. midterm 기준 시뮬레이션 트리에서 전체 테스트: **1960 passed, 1 failed, 7 skipped**. 실패 1건은 `.git` 없는 사본에서만 나는 기존 환경 실패(`test_frozen_store_and_policy_files_unchanged`)입니다 [확인].
- 팀 결정이 필요한 것: **midterm이 main으로 들어가는지(그리고 언제)** [미확인]. 이 결정에 따라 기준 브랜치만 바뀌고 절차는 같습니다.

---

## 1. 현재 Git 상태 [확인]

| 항목 | 값 |
|---|---|
| 현재 브랜치 | `feat/dcvc-backend-foundation` (upstream 없음) |
| HEAD | `8bfc0fe6a5ac17ce00fcbfc95804124e65c2517c` |
| `git status` | 추적 파일 변경 없음. 미추적: `docs/audit/`(감사 보고서 4개), `TRIALOG_개발결과_검토용.md`(이번 세션에서 만든 검토용 합본) |
| 로컬 `main` | `origin/main`보다 6커밋 뒤, 앞선 커밋 0 |
| `origin/main` | `9fdffc4` (2026-10-25 17:35 +0900, "feat: 프로젝트 이름 Trialog와 로고, 입력 화면 정리") |
| 원격 추적 정보의 최신성 | **[미확인]** 이 작업에서 `git fetch`(실제 갱신)를 하지 않았습니다. `git fetch --dry-run`을 1회 실행했고(원격 조회만, ref 변경 없음), 원격에 로컬에 없는 브랜치 `report/llm-pilot-a-1026`이 있다는 것이 보였습니다. 즉 **원격이 로컬 추적 정보보다 앞서 있을 수 있습니다**. |
| worktree | 이 폴더 외 15개 worktree가 다른 브랜치를 사용 중(`integration/midterm-1026`, `integration/review-midterm-1026` 등). 해당 브랜치들은 이 폴더에서 checkout할 수 없음 |

### 1.1 커밋 관계 [확인]

```
64d471b (origin/main과의 공통 조상, 2026-10-20)
 ├── origin/main: +6  (3bfa58a … b11ec29, 9fdffc4)   ← 웹 화면 개편·matcher·pipeline 수정
 ├── feat/decision-inspector: +4 (40c94fb, 173314f, 3f5026f, 62f744e)  ← inspector.py, render_inspector.py, test_inspector.py
 │     └── feat/dcvc-backend-foundation: +5 (a3ab322, 9f85acb, 0c98238, 7247330, 8bfc0fe)  ← 신규 파일만
 └── origin/integration/midterm-1026 (= 로컬 integration/review-midterm-1026 = 448a8c5): +34, main보다 2 뒤
       └── 866d45a: feat/decision-inspector(62f744e) 내용 통합 + 확장, 계약 재설계(명제·질문 계약·평가기) 포함
```

| 비교 | 결과 |
|---|---|
| `HEAD...origin/main` | 9 / 6 (HEAD에만 9, main에만 6) |
| `HEAD...feat/decision-inspector` | 5 / 0 (신규 5커밋만 더 있음) |
| `feat/decision-inspector`를 포함한 브랜치 | 이 두 브랜치뿐. 원격 어디에도 그 커밋 자체는 없음 |
| `git cherry midterm 62f744e` | 4개 모두 `+`(패치 동일 커밋 없음) — midterm은 같은 내용을 **다른 커밋으로 재통합**함 |
| `62f744e` vs midterm의 Inspector 파일 | `scripts/render_inspector.py`, `tests/test_inspector.py` **동일**. `src/ctrec/inspector.py`는 midterm 쪽이 +203/−11줄(삭제 11줄은 같은 줄의 확장 수정으로 보임 [추정]) |
| midterm `...origin/main` | 34 / 2. main에만 있는 2커밋(`b11ec29`, `9fdffc4`)은 midterm에 패치 동일 커밋이 없음 |

### 1.2 신규 5커밋의 변경 파일 [확인] — 모두 **추가(A)**, 기존 파일 수정 0

```
docs/dev/integration_handoff.md, docs/dev/night_run_report.md, docs/engine_contracts.md
src/ctrec/cohort_metrics.py, src/ctrec/eval_hygiene.py
src/ctrec/engine/{__init__,audit,bundles,cohort_state,contracts,cost,fairness,policy}.py
tests/test_engine_audit.py, tests/test_engine_contracts.py, tests/test_engine_policy.py, tests/test_eval_hygiene.py
(17 files, +3098)
```
같은 경로가 `origin/main`·midterm에 이미 있는지 확인: **없음** [확인].

### 1.3 병합 시뮬레이션 (`git merge-tree --write-tree`, 작업 트리·ref 변경 없음) [확인]

| 시뮬레이션 | 충돌 |
|---|---|
| 현재 HEAD(9커밋) + `origin/main` | 없음 |
| 현재 HEAD(9커밋) + `origin/integration/midterm-1026` | **`src/ctrec/inspector.py` add/add 충돌** |
| 신규 5커밋만(base `62f744e`) → `origin/main` | 없음 (단, 테스트에서 `ctrec.inspector` import 실패) |
| 신규 5커밋만(base `62f744e`) → `origin/integration/midterm-1026` | 없음 |
| 참고: midterm + `origin/main` | `README.md`, `src/ctrec/agents/matcher.py`, `src/ctrec/pipeline.py` 충돌 (이 작업과 무관하지만 midterm의 main 통합은 별도 해결 필요) |

### 1.4 시뮬레이션 트리 테스트 (스크래치 폴더에 `git archive`로 풀어서 실행, API 키 제거) [확인]

| 트리 | 신규 4개 테스트 파일 | 전체 |
|---|---|---|
| 현재 HEAD | 113 passed, 1 skipped | (야간 보고서: 1175 passed, 5 failed(환경), 6 skipped) |
| 5커밋 → midterm | 113 passed, 1 skipped | **1960 passed, 1 failed, 7 skipped** (실패 = `.git` 없는 사본의 기존 환경 실패) |
| 5커밋 → origin/main | **수집 오류 1** (`tests/test_engine_audit.py`가 `from ctrec import inspector`) | 실행 안 함 |

### 1.5 충돌 가능성이 높은 파일
| 파일 | 위험 | 이유 |
|---|---|---|
| `src/ctrec/inspector.py` | **높음** | 세 계열(feat/decision-inspector, midterm, 이후 Inspector 브랜치들)에서 서로 다른 커밋으로 추가·수정 [확인] |
| `src/ctrec/agents/matcher.py`, `src/ctrec/pipeline.py`, `README.md` | 높음 (midterm↔main) | 시뮬레이션 충돌 [확인]. 보호 파일 목록에 pipeline.py·matcher.py 포함 (감사 03 §7) |
| `src/ctrec/schemas.py`, `web/app.py`, `web/static/*` | 중간 | main·midterm 양쪽 변경, WP8에서 동겸이 수정 예정 [추정] |
| 신규 `engine/*`, `eval_hygiene.py`, `cohort_metrics.py` | 낮음(텍스트) / **중간(의미)** | 텍스트 충돌은 없지만, midterm의 `ContractSession`·결정적 평가기와 의미 정렬 필요(계약 검토 A-4) |

### 1.6 `docs/audit/` 미추적 파일 보존
- 현재 상태: 미추적 4개 파일(01~04). 이 계획의 권장 절차는 **새 worktree에서만** 작업하므로 이 폴더의 미추적 파일은 건드리지 않습니다.
- 추가 안전장치: 저장소 밖으로 복사(아래 1단계). 커밋 여부는 팀이 결정(야간 보고서 §7-3). 커밋한다면 `integration/dcvc`에 **별도 문서 커밋**으로.
- `git clean`, `git stash -u`, `git checkout -f`는 쓰지 않습니다(미추적 파일 손실 위험).

---

## 2. 대안 비교

| 기준 | A. 현재 브랜치로 main에 PR | B. `integration/dcvc`로 단계 통합 (현재 브랜치 전체 병합) | C. 최신 main에서 새 브랜치 + 5커밋 cherry-pick | **C′. midterm(또는 midterm이 들어간 최신 main)에서 새 브랜치 + 5커밋 cherry-pick** |
|---|---|---|---|---|
| Inspector 작업 보존 | 4커밋이 함께 들어감. 그러나 midterm이 나중에 main에 들어오면 inspector.py add/add 충돌 [확인] | A와 같음 | **Inspector 없음** → 테스트 import 실패 [확인] | midterm에 통합된 Inspector(확장판) 위에 올라감. 원 브랜치·태그는 그대로 보존 |
| origin/main 변경 보존 | 보존 | 보존 | 보존 | midterm이 main보다 2커밋 뒤 → 그 2커밋은 midterm의 main 통합 때 들어옴 (별도 해결 필요) |
| 텍스트 충돌 | main과는 없음 / midterm과 inspector.py | 같음 | 없음 | 없음 [확인] |
| 테스트 | 미실행 | 미실행 | 수집 오류 [확인] | 1960 passed / 환경 실패 1 [확인] |
| 이력 | Inspector가 두 계열(원 커밋 + 866d45a)로 중복 | 같음 | 깔끔하지만 의존성 누락 | 깔끔. `-x`로 원 커밋 해시 기록 |
| 되돌리기 | PR 닫기 | 브랜치 삭제 | 브랜치 삭제 | worktree·브랜치 삭제 |
| 판정 | ✗ | ✗ | △ (main에 Inspector가 먼저 들어간 뒤라면 가능) | **권장** |

---

## 3. 권장 절차 (Windows PowerShell 5.1)

> 공통 전제: 각 단계는 앞 단계의 "예상 결과"가 나왔을 때만 진행. **4단계 결정 전에는 브랜치를 만들지 않음.** push·PR은 7단계에서 팀 승인 뒤 사용자가 직접 실행.
> PowerShell 5.1에는 `&&`가 없으므로 한 줄씩 실행합니다.

### 1단계 — 현재 상태 확인과 미추적 파일 백업
```powershell
Set-Location C:\Users\jsw58\Desktop\workseoyeon
git status --short
git rev-parse HEAD
$bk = "C:\Users\jsw58\Desktop\trialog_backup_20261102"
New-Item -ItemType Directory -Force $bk | Out-Null
Copy-Item -Recurse -Force .\docs\audit "$bk\audit"
Copy-Item -Force .\TRIALOG_*.md $bk
Get-ChildItem -Recurse $bk | Measure-Object
```
- 전제조건: 이 폴더가 `feat/dcvc-backend-foundation`에 있음
- 목적: 미추적 파일을 저장소 밖에 복사
- 예상 결과: status는 `?? docs/audit/`, `?? TRIALOG_…md`만, HEAD `8bfc0fe…`, 백업 폴더에 파일 5개 이상
- 중단 조건: 추적 파일 변경(` M`)이 보이면 중단하고 원인 확인
- 되돌리기: 백업 폴더 삭제(원본 영향 없음)

### 2단계 — 보존용 로컬 태그
```powershell
git tag backup/dcvc-foundation-20261102 8bfc0fe
git tag backup/decision-inspector-20261102 62f744e
git tag --list "backup/*"
```
- 전제조건: 같은 이름 태그 없음
- 목적: 어떤 작업 뒤에도 원래 커밋으로 돌아갈 수 있게 표시 (원격에는 올리지 않음)
- 예상 결과: 태그 2개 표시
- 중단 조건: `already exists` 오류 → 다른 이름 사용
- 되돌리기: `git tag -d backup/dcvc-foundation-20261102`, `git tag -d backup/decision-inspector-20261102`

### 3단계 — 원격 최신화 (네트워크 사용, 사용자 승인 후)
```powershell
git fetch origin
git log --oneline -3 origin/main
git log --oneline -3 origin/integration/midterm-1026
git rev-list --left-right --count origin/integration/midterm-1026...origin/main
```
- 전제조건: 네트워크·GitHub 인증
- 목적: 이 계획의 숫자(34/2 등)가 최신인지 확인. fetch는 원격 추적 ref만 갱신하고 로컬 브랜치·작업 트리는 바꾸지 않음
- 예상 결과: 이 문서와 같거나, midterm이 main에 들어갔다면 왼쪽 숫자가 0
- 중단 조건: 인증 실패, 또는 `origin/main`/midterm이 크게 바뀜 → 1.3 시뮬레이션을 다시 실행하고 이 계획을 갱신
- 되돌리기: 필요 없음(원격 추적 ref만 바뀜)

### 4단계 — 기준 브랜치 결정과 사전 시뮬레이션 (읽기 전용)
```powershell
# (팀 결정) midterm이 아직 main에 안 들어갔으면 midterm, 들어갔으면 origin/main
$BASE = "origin/integration/midterm-1026"
git merge-tree --write-tree --name-only --merge-base=62f744e $BASE 8bfc0fe
git cat-file -e "${BASE}:src/ctrec/inspector.py"; if ($?) { "inspector.py 있음" }
```
- 전제조건: 3단계 완료, 팀이 기준 브랜치에 동의
- 목적: 실제 cherry-pick 전에 충돌·의존성 확인
- 예상 결과: 첫 명령은 트리 해시 한 줄만(충돌 파일 목록 없음), 두 번째는 "inspector.py 있음"
- 중단 조건: 파일 이름이나 `CONFLICT`가 출력됨, 또는 inspector.py 없음 → C′ 진행 중단(기준을 Inspector가 있는 브랜치로 바꾸거나 팀과 재논의)
- 되돌리기: 필요 없음(ref 변경 없음, 객체만 생성)

### 5단계 — 별도 worktree에 통합 브랜치 생성
```powershell
$WT = "C:\Users\jsw58\Desktop\workseoyeon-dcvc"
Test-Path $WT
git worktree add -b integration/dcvc $WT $BASE
git -C $WT status --short
git -C $WT log --oneline -1
```
- 전제조건: `Test-Path $WT`가 `False`, `integration/dcvc` 브랜치 없음(`git branch --list integration/dcvc`가 비어 있음)
- 목적: 현재 폴더(미추적 파일 포함)를 건드리지 않고 새 폴더에서 통합
- 예상 결과: 새 폴더 생성, status 비어 있음, HEAD = `$BASE`의 커밋
- 중단 조건: 경로·브랜치가 이미 있음 → 이름을 바꾸거나 기존 것을 먼저 확인
- 되돌리기: `git worktree remove $WT` 후 `git branch -D integration/dcvc`

### 6단계 — 신규 5커밋 cherry-pick과 검증
```powershell
git -C $WT cherry-pick -x a3ab322^..8bfc0fe
git -C $WT log --oneline "${BASE}..HEAD"
git -C $WT diff --name-status "${BASE}..HEAD"
```
- 전제조건: 5단계 완료, worktree status 깨끗
- 목적: 신규 파일 17개만 기준 위에 올림 (`-x`로 원 커밋 해시를 메시지에 남김)
- 예상 결과: 커밋 5개, `diff --name-status`는 `A` 17줄만
- 중단 조건: `CONFLICT` 또는 `M`/`D` 줄이 보임 → `git -C $WT cherry-pick --abort` 후 중단, 원인 보고
- 되돌리기: `git -C $WT reset --hard $BASE` (이 worktree 안에서만, 다른 폴더 영향 없음) 또는 5단계 되돌리기

테스트(같은 단계, 커밋 없음):
```powershell
Set-Location $WT
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
$env:PYTHONPATH = "$WT\src"
& C:\Users\jsw58\Desktop\workseoyeon\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_engine_contracts.py tests/test_engine_policy.py tests/test_engine_audit.py tests/test_eval_hygiene.py
& C:\Users\jsw58\Desktop\workseoyeon\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rfs
Remove-Item Env:PYTHONPATH
Set-Location C:\Users\jsw58\Desktop\workseoyeon
```
- 예상 결과: 신규 113 passed / 1 skipped. 전체는 시뮬레이션 기준 1960 passed 내외 [추정: worktree에는 `.git`이 있으므로 환경 실패 1건이 통과할 수 있음 — 미확인]
- 중단 조건: 시뮬레이션보다 실패가 늘어남 → push하지 않고 실패 목록 보고
- 되돌리기: 테스트는 상태를 바꾸지 않음. `outputs/`가 생기면 그 worktree 안에서만 생김

### 7단계 — (팀 승인 뒤, 사용자가 직접) push와 PR
```powershell
git -C $WT push -u origin integration/dcvc
# PR: base = integration/midterm-1026 (midterm이 main에 들어간 뒤라면 main), compare = integration/dcvc
```
- 전제조건: 6단계 통과, 계약 검토 문서의 "지금 확정" 항목을 팀이 확인
- 목적: 리뷰 가능한 PR 생성
- 중단 조건: 원격에 같은 이름 브랜치 존재
- 되돌리기: PR 닫기, `git push origin --delete integration/dcvc`(원격 삭제는 팀 확인 후)

### 보존 원칙 요약
- `feat/decision-inspector`, `feat/dcvc-backend-foundation`은 **삭제·수정하지 않음**(태그로도 보존).
- `feat/decision-inspector`를 main에 따로 병합하지 않음: 같은 내용이 midterm에 있고, 따로 넣으면 inspector.py 충돌.
- midterm ↔ main 충돌(README, matcher.py, pipeline.py) 해결은 이 계획 범위 밖. 해당 브랜치 담당자가 처리 [미확인: 담당자].

---

## 4. 팀원 병렬 개발 준비

### 4.0 공통 규칙
- **정책 엔진을 다시 만들지 않습니다.** 행동 후보·선택은 `ctrec.engine.policy.LegacyAdapterPolicy`(`propose/select/apply_outcome`)와 `PolicyEngine` 프로토콜만 씁니다. 새 알고리즘은 같은 프로토콜의 **다른 구현 클래스**로 추가하고 `tests/test_engine_policy.py` 조건(예산 초과 0, 중복 청구 0, 결정성, 1명 동등성, 안전 행동 보존)을 통과해야 합니다.
- 계약(`engine/contracts.py`)은 PROVISIONAL: 필드 **추가만**, 의미·이름 변경은 선우 승인 + `contract_version` 상승.
- 모든 작업 브랜치는 `integration/dcvc`에서 분기하고 PR도 `integration/dcvc`로.
- 테스트는 `ANTHROPIC_API_KEY` 없이 실행. 실제 LLM·유료 API 호출 금지(별도 승인 없이는).
- 보호 파일 테스트(`test_frozen_store_and_policy_files_unchanged`): 보호 목록(pipeline.py, matcher.py, patient_profiler.py, recommender.py, decision_*.py, question_priority.py, web/access.py, web/retention.py, status.py, data/)을 **커밋 전 로컬에서 수정하는 동안 실패**합니다(감사 03 §7, `62f744e` 기준 [확인] / midterm 기준 목록 [미확인]).
- 임상 판정은 판정 계층만 바꿉니다. 정책·웹·렌더러는 `CriterionResult`·`PairStatus`를 만들거나 바꾸지 않습니다.

### 4.1 서연 — WP2·WP3·WP4·WP6 + WP1 평가 진입점
| 항목 | 내용 |
|---|---|
| 필요한 공통 계약 | I-1(`derive_pair_status`, 전이 표), I-2(`CriterionResult` 권한 규칙), I-5(정보원·자리표시 비용), I-7(`make_record` 입력: criteria_before/after·reevaluated), I-9(`ExperimentSpec`·`DataVersion`), `eval_hygiene`(AliasMap, LeakScanner, PromptLeakGuard, run_method_hygienic), `cohort_metrics` |
| 선행 조건 | 계약 검토 Q1·Q5·A-1·A-2·A-3·A-4 결정. `integration/dcvc` 생성. **실제 LLM으로 masked/validation을 돌리기 전 B-1·B-4 반영 필수** |
| 변경 가능 파일 | `question_eval.py`(B-1 `answerer_factory` 인자 추가), `metrics.py`(B-2, 함수 추가만), `eval/evaluate.py`·sweep 스크립트(B-4 기본값을 별칭 경로로), `scripts/build_masked_benchmark.py`(B-3), `data/nsclc/**`(WP2, 별도 store_dir), 신규 `src/ctrec/engine/adjudication.py`(WP3), 신규 `src/ctrec/agents/criterion_assessor.py`(WP4), `dependency_graph.py`(추가만), 신규 `src/ctrec/sim/**`·`scripts/run_cohort_experiment.py`(WP6), `schemas.RuleMeta`(기본값 있는 필드 추가만), `eval_hygiene.py`·`cohort_metrics.py`(B-1/B-2 뒤 소유 이관) |
| 변경 금지 파일 | `engine/contracts.py`(추가 요청은 선우에게), `engine/policy.py`·`cost.py`·`bundles.py`·`cohort_state.py`(정책 재구현 금지), `inspector.py` v1 경로, `web/**`, `pipeline.py`(WP8 주입 지점은 동겸), 기존 `data/parsed/*`·`FROZEN.json`(store_patch 절차 외) |
| 테스트 조건 | 기존 전체 테스트 회귀 없음. 신규: 별칭 경로 누출 0(`test_eval_hygiene.py`), WP3 expert 기준 자동 확정 불가·gate 단방향, WP4 `compare_states` 동등 100%·영향 밖 기준 LLM 호출 0, WP6 같은 spec 2회 결과 해시 동일·`source_catalog_hash` 필수, WP2 생성기 결정성·FROZEN 해시 테스트 불변 |

### 4.2 동겸 — WP5 인수·WP8 웹 통합·프론트엔드
| 항목 | 내용 |
|---|---|
| 필요한 공통 계약 | I-3 `Action`, I-4 `ActionOutcome`, I-6 `EffortBudget`, I-8 `PolicyEngine`/`Plan`(특히 `safety_unfunded`, `held`), `CohortState.from_decision_states`, `EffortLedger`, `fairness.allocation_report`, `audit.to_trace`(W-4) |
| 선행 조건 | 계약 검토 Q4(`purpose` 등 추가 필드)·A-4(`ContractSession` 기준 연결)·A-9(별칭 salt) 결정. `integration/dcvc` 생성(midterm 기준 — 웹은 `ContractSession` 경로 [확인: 모듈 존재, 웹 사용 여부 미확인]) |
| 변경 가능 파일 | 신규 `src/ctrec/web/engine_runner.py`(W-1), 신규 ActionRenderer 모듈(W-3), `web/app.py`(`policy_mode` 분기 몇 줄), `pipeline.py` 또는 `ContractSession`의 **주입 지점 1곳**(보호 파일 — 커밋 전 보호 테스트 실패 인지), `web/static/*`(엔진 모드 표시, 안전 확인 미실행 표시 W-7), `engine/fairness.py`(관측 지표 추가) |
| 변경 금지 파일 | `engine/contracts.py`(추가 요청만), `engine/policy.py`의 선택 규칙(바꾸려면 새 `PolicyEngine` 구현 클래스로), `engine/cost.py`의 청구 규칙, 판정 계층(`agents/matcher.py`, `tools/rule_engine.py`, `evidence.py`, `evaluator.py`), `web/budget.py`와 `EffortLedger` 통합 금지(W-5), `eval_hygiene.py` |
| 테스트 조건 | `policy_mode="legacy"`(기본)에서 기존 웹 테스트 전부 통과. engine 모드: 같은 `DecisionState` → 오프라인과 같은 행동 ID 순서(골든), orchestrator LLM 호출 0, `safety_unfunded`가 있으면 그 환자의 다른 행동 미진행, 실패·무응답 결과로 사실 0개, `apply_outcome` 뒤 판정 불변, SSE/화면에 원래 환자 ID·별칭 매핑 미노출 |

### 4.3 선우 — WP0 계약 승인·WP7 Inspector 검토·최종 품질 확인
| 항목 | 내용 |
|---|---|
| 필요한 공통 계약 | 전체 I-1~I-9, `docs/engine_contracts.md`, 이 두 문서 |
| 선행 조건 | 위 1~6단계(`integration/dcvc` 생성·테스트) |
| 변경 가능 파일 | `engine/contracts.py`·`docs/engine_contracts.md`(승인 결과 반영, 0.2), `engine/audit.py`, `inspector.py`의 v2 섹션(v1 렌더 결과 불변), 계약 경계 테스트(A-1·A-2) |
| 변경 금지 파일 | 서연·동겸 소유 파일(위 표). 판정 계층 로직 |
| 테스트 조건 | 계약 테스트(경계 조합 전수 추가), v1 Inspector 회귀 없음(`test_inspector.py`), v2 기록 로드·검증, 각 PR마다 전체 테스트·누출 검사·보호 파일 테스트 확인 |

### 4.4 병렬 순서
1. 즉시: 선우(계약 0.2 PR), 서연(B-1·B-2·B-4, WP2 데이터 수집) — 파일이 겹치지 않음
2. 계약 0.2 승인 뒤: 서연 WP3, 동겸 W-1·W-3(engine 모드 골격, legacy 기본)
3. WP3 뒤: 서연 WP4 → DecisionRecord 실제 생산(B-8) → 선우 WP7 연결 검토
4. WP5·WP2 뒤: 서연 WP6. 마지막: 동겸 WP8 마무리

---

## 5. 이 작업에서 실행한 명령 (투명성)
- 읽기 전용 Git 명령(`status`, `log`, `rev-list`, `merge-base`, `cherry`, `diff`, `ls-tree`, `cat-file`, `worktree list`, `for-each-ref`).
- `git merge-tree --write-tree`(시뮬레이션: 객체만 생성, ref·작업 트리 변경 없음) 5회.
- `git fetch --dry-run` 1회(원격 조회, ref 변경 없음) — 외부 네트워크 접속에 해당하므로 기록합니다.
- `git archive`로 시뮬레이션 트리를 스크래치 폴더에 풀어 pytest 실행(저장소 밖).
- 로컬 venv로 `derive_pair_status` 경계값 확인, 신규 테스트 4개 파일 실행.
- 커밋·병합·리베이스·cherry-pick·push·PR·브랜치 전환·파일 삭제: **0건**.
