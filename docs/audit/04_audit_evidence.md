# 04. 감사 근거 기록

> 기준 커밋 `62f744ec887cd937889fd96622bd70b19703bfd2`, 2026-11-02. 모든 명령은 읽기 전용이었습니다(테스트 실행 제외, 아래 부작용 항목 참고).
> 민감정보: API 키·비밀번호 값은 출력하지 않았고(길이·형식만 확인), 환자 원문은 인용하지 않았습니다.

## 1. 저장소 상태 명령

| 명령 | 결과 요약 |
|---|---|
| `git branch -vv` | 현재 `feat/decision-inspector` 62f744e, 원격 추적 없음. 로컬 브랜치 약 50개, 외부 worktree 다수 |
| `git rev-parse HEAD` | `62f744ec887cd937889fd96622bd70b19703bfd2` |
| `git status --short` | 출력 없음 (감사 시작·종료 시. 종료 시 `docs/audit/` 4개 파일만 새로 생김) |
| `git status --short --ignored` | `!! .pytest_cache/ .venv/ outputs/ src/ctrec.egg-info/` (모두 감사 전부터 있음, `outputs/` 10-24 생성) |
| `git rev-list --left-right --count origin/main...HEAD` | `6	4` |
| `git fetch --dry-run` | 원격에 새 브랜치 `report/llm-pilot-a-1026` 있음(가져오지 않음) |
| `git ls-files \| wc -l` | 246 |
| `git ls-files src \| xargs wc -l` | 16,244줄 (정적 라이선스·minified JS 포함). Python 약 13,000줄 |
| `git log --format= --name-only \| sort \| uniq -c` | 변경 빈도 상위: README.md 17, web/app.py 15, pipeline.py 14, static/index.html 12, schemas.py 9, matcher.py 8 |
| `git log --format='%an' \| sort -u \| wc -l` | 작성자 2명 (이름은 기록하지 않음), 총 48커밋 |

## 2. 테스트 실행

### 2.1 전체 실행
```
env -u ANTHROPIC_API_KEY .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -rs --durations=15
```
- 결과: **1062 passed, 5 failed, 5 skipped in 180.83s**
- 실패:
  - `tests/test_llm_pilot.py::test_dry_run_output_redacted_and_no_patient_saving` (:472)
  - `tests/test_llm_pilot_exec.py::test_cli_with_every_flag_and_key_still_refused` (:117)
  - `tests/test_llm_pilot_exec.py::test_result_format_fields_and_no_saving_by_default` (:438)
  - `tests/test_pilot_process.py::test_dedicated_process_fake_e2e[A_pipeline]` (:82)
  - `tests/test_pilot_process.py::test_dedicated_process_fake_e2e[B_question_plan]` (:82)
  - 공통 단언: `assert not (ROOT / "outputs").exists()`. `outputs/inspector/M-SYN-DEMO-A-DEMO-LUNG-001-rescue2-3`(10-24 생성)이 원래 있었음
- skip: `test_web_browser.py:26` (`CTREC_BROWSER_TESTS=1` 필요) 1건, `test_inspector.py:331` (이 환경에서는 심볼릭 링크를 만들 수 없음) 4건
- 가장 느린 테스트: web_budget/web_quality 각 약 10초, pilot_process 3–8초 (enforce 원장·별도 프로세스)

### 2.2 원인 분리 재실행 (깨끗한 사본)
```
git archive HEAD | tar -x -C <scratchpad>/clean
cd <scratchpad>/clean
PYTHONPATH=<clean>/src env -u ANTHROPIC_API_KEY <repo>/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/test_llm_pilot.py tests/test_llm_pilot_exec.py tests/test_pilot_process.py
```
- import 경로 확인: `ctrec.__file__`이 사본의 `src/ctrec/__init__.py`를 가리킴
- 결과: **100 passed, 1 failed**. 위 5건은 통과 → **환경(기존 `outputs/` 폴더) 원인 확정**
- 새 실패 1건: `test_frozen_store_and_policy_files_unchanged` (test_llm_pilot_exec.py:484). `git diff`를 부르는데 사본에 `.git`이 없음 → 사본 환경 원인. 원 저장소에서는 통과
- 판정: **코드 결함 0건, 환경 의존 테스트 6건**(01 문서 P2-5)

### 2.3 실행하지 않은 것
- 실제 Anthropic API 호출: 키를 제거하고 실행함. 비용·외부 전송 발생 0. 실제 모델 판정 품질은 미확인
- 브라우저 테스트(`CTREC_BROWSER_TESTS=1`): 브라우저 드라이버 준비 여부 미확인으로 실행 안 함
- CT.gov 네트워크 호출(`fetch_trials`), 웹 서버 기동: 하지 않음

### 2.4 부작용
- `-p no:cacheprovider`로 `.pytest_cache` 갱신을 막음. 실행 뒤 `git status --short --ignored` 결과가 실행 전과 같음
- 깨끗한 사본은 세션 scratchpad에만 만들었고 저장소 밖에 있음

## 3. 데이터 점검 명령

| 확인 | 명령(요약) | 결과 |
|---|---|---|
| 시험 풀 질환 | `data/trials/*.json`의 `overall_status`, `conditions`, `source` 출력 | 26개. 실제 NSCLC 0개. `DEMO-LUNG-001`만 NSCLC/EGFR(source=synthetic). 나머지는 췌장염, 그레이브스병, 망막박리, 소아 신증후군, 편두통, EBV, IPF, 방광암, 털곰팡이증 등. COMPLETED 3개 |
| Frozen 로드 | `trial_store.load(t)`를 26개에 실행 | 실패 0 |
| Frozen 해시 | `trial_store.frozen_manifest()` | `files` 26개(키는 trial_id stem). 해시 일치는 test_decisions.py:253-256과 `store_patch.verify_frozen`(test_llm_pilot_exec.py:485)이 확인(둘 다 통과) |
| 케이스 ID 정답 누출 | masked_cases.json의 `patient_id` 접미사·`case_type` 포함 수, validation_cases.json의 `-[IE]\d+-` 포함 수 | masked 101건 중 72건이 ID에 case_type(rescue/cleanup/rescue2) 포함. validation 100건 중 96건이 ID에 rule_id 토큰 포함 |
| env 예시 파일 | 값 길이·`sk-ant-` 접두 여부·주석 앞 값 길이만 awk로 확인 | `deploy/web-record.env.example`의 `ANTHROPIC_API_KEY`, `CTREC_PASSWORD`: 주석 앞 값 길이 0, 같은 줄 주석 있음 → 실제 비밀 아님. `.env.example`, `web-enforce.env.example`: 빈 값 |

## 4. 중요 코드 근거 (발췌 위치)

| 주장 | 근거 |
|---|---|
| 판정·질문·추천이 FactStore를 쓰지 않음 | `pipeline.py:148-150` 주석, `_shadow_record` (:231-260)가 기록만 하고, `match`(:285)가 `self.profile`을 사용 |
| 답변 반영 = 텍스트 추가 + LLM 재추출 + 재매칭 | `pipeline.py:436-441` |
| 시험 적격성은 코드가 결정 | `agents/matcher.py:86-91` `decide`, `:97-124` `_select_cohort` |
| LLM 판정은 시험 단위 일괄 호출 | `agents/matcher.py:316-343` `_llm_assess`, `:361-392` 루프 |
| 근거 출처를 문자열 표식으로 판정 | `evidence.py:56, 66-69`, `agents/matcher.py:257-283` |
| 기간 조건 검사는 규칙 엔진이 판정하지 않음 | `tools/rule_engine.py:273` |
| 관찰 기간 게이트는 진단 경로에만 있음 | `fact_view.py:381-386`, 모듈 docstring `:3` ("비교용 경로에서만") |
| LLM 기준은 모든 항목에 의존 | `dependency_graph.py:12-18` |
| Decision Loop은 진단 전용·NoDispatch | `decision_loop.py:1-20, 63-69, 369-383`, `pipeline.py:317-336` |
| 정책 value_score는 임상 판단에 쓰지 않음 | `decision_policy.py:10` |
| 비용 단위는 무차원 상수 | `decision_policy.py:35-44`, `decision_state.py:138-144` |
| info_key 묶음 규칙 | `decision_state.py:217-227` |
| 웹 실행 경로 | `web/app.py:434-444` (`Orchestrator(session).run()`), `orchestrator.py:158-166, 205-214` |
| 웹 인증 생략 조건 | `web/app.py:85-88` (`if not PASSWORD ...`) |
| `/api/patients` 원문 반환 | `web/app.py:536-546` |
| patient_id가 프롬프트로 들어감 | `agents/patient_profiler.py:26`, `orchestrator.py:161`, `PatientProfile.patient_id`(schemas.py:124)가 `model_dump_json`으로 matcher 프롬프트에 들어감(matcher.py:321) |
| 숨긴 필드 제거 | `pipeline.py:63-67` `_HIDDEN_KEYS`, `:78` |
| 런타임 Frozen 해시 미확인 | `trial_store.py:107-120` (`trial_hash`만 비교) |
| LAZY_COMPILE 대체 경로 | `trial_store.py:123-130`, `config.py:68` |
| Tracer는 스키마 없음 | `trace.py:25-38` |
| 보호 파일 테스트 동작 | `tests/test_llm_pilot_exec.py:443-447, 484-497` |
| 유료 파일럿 하드 비활성 | `llm_pilot.py:40` `PAID_EXECUTION_ENABLED = False` (하위 감사 보고, 줄 번호는 직접 확인하지 않음) |

## 5. 하위 조사 사용 내역
- 웹 계층(`web/*`, static, budget, call_guard, deploy)과 평가·데이터·파일럿 영역은 읽기 전용 하위 조사 2건으로 먼저 훑었습니다.
- 그 결과 중 **보고서 결론에 직접 쓰인 항목은 직접 다시 확인**했습니다: 웹 실행 경로, 인증 생략, `/api/patients`, patient_id 프롬프트 유입, ID 정답 인코딩 비율, env 예시 값.
- 다시 확인하지 않은 하위 조사 결과(display.py·app.js의 중복 로직 줄 번호, `OracleAnswerer`의 일괄 공개(question_eval.py:352-358), metrics 조인 줄 번호, 웹 테스트 개수, `limit_body`의 chunked 우회, SSE 연결 제한 없음)는 보고서 본문에 "하위 감사"로 출처를 표시했습니다. 이 항목들은 **[추정]에 준해** 다뤄야 합니다.

## 6. 확인하지 못한 항목과 이유

| 항목 | 이유 |
|---|---|
| 실제 LLM 판정 정확도·안정성 | API 키를 쓰지 않기로 함(비용·외부 전송). 라벨도 LLM 생성이라 임상 정답이 아님 |
| 임상적 정확성 | 임상 전문가 검토 범위. 테스트 통과는 임상 정확성을 뜻하지 않음 |
| `data/patients/provided`(사업단 제공) 데이터의 비식별·합성 여부 | 출처 문서(README) 외 검증 수단 없음 |
| 브라우저 E2E | 실행 환경 미준비 |
| 심볼릭 링크 관련 Inspector 안전성 4건 | Windows 권한상 skip |
| 다른 브랜치·worktree(예: `integration/midterm-1026`, `feat/proposition-contract-redesign`)의 구현 | 감사 범위를 HEAD로 한정. 이 브랜치들에 M 항목 관련 구현이 있을 수 있음 |
| 팀의 main 병합 계획, 역할 배정 | 저장소에 근거 없음 |
| 배포 환경(Render) 실제 설정값 | 저장소 밖 |
| `build_report.json`의 글자 깨짐이 파일 문제인지 콘솔 인코딩 문제인지 | 하위 조사에서 콘솔 출력으로만 봄 |
