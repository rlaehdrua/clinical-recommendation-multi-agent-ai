# WP2a 파싱 파일럿: NCT06928389 1개 (실측 사용량)

> 2026-11-03 · 작성: 서연 · 브랜치 `feat/wp2a-nsclc-trials`
> 승인: 공정현, LLM 파싱 비용 상한 $15. 이번 실행은 시험 1개만 했고 배치·전체 실행은 하지 않았습니다.
> **파싱 결과의 의학적 해석은 모두 "검수 필요"**입니다. expert 기준은 정답을 만들지 않고 `EXPERT_REVIEW`로 둡니다.

## 1. 실행 조건

| 항목 | 값 |
|---|---|
| 경로 | `scripts/compile_trials.py` (기존 코드, 수정 없음) |
| 입력 | `data/nsclc/parsed_trial_run/trials/NCT06928389.json` (`data/cache/ctgov/` 원본 복사) |
| 출력 | `--store data/nsclc/parsed_trial_run/store`, `--runs-dir data/nsclc/parsed_trial_run/runs` |
| 보호 경로 | 보호 경로는 `data/parsed/`(`FROZEN.json` 포함)와 `data/trials/`이며 둘 다 쓰지 않음. 출력 위치 `data/nsclc/**`는 서연 수정 가능 경로(`team_parallel_start.md` §2 "`data/nsclc/**`(별도 store_dir)"). 부수 효과로 `criteria_parser`가 `data/cache/parsed/`(git 무시 경로)에 캐시 1건을 씀 |
| 옵션 | `--only NCT06928389 --runs 3 --workers 1 --force` |
| 모델 | `claude-sonnet-5` (`.env`의 `CTREC_MODEL`). 서버측 fallback 켜짐(`CTREC_FALLBACKS=1`), **실제 fallback 발생 0** (4회 모두 응답 model = `claude-sonnet-5`, stop_reason = `end_turn`) |
| 호출 설정 | `max_tokens` 16000, effort high (파서·검증기), 구조화 출력 |
| 재시도 | 저장소 밖 래퍼에서 SDK `max_retries=1`로 실행. 실제 재시도 0 |
| 비용 감시 | 호출마다 응답 `usage`를 기록하고, API 호출 직전마다 누적 비용을 확인. 중단선 $0.47 (아래 §3) |
| 기타 | 첫 시도는 `.env` API 키 만료로 401 (토큰 사용 0, 비용 0). 키 교체 후 재실행 |

## 2. 실측 사용량

| 호출 | 용도 | input | output | cache write | cache read | 비용 (공시) | 비용 (보수 $3/$15) |
|---|---|---|---|---|---|---|---|
| 1 | 파싱 1회차 | 959 | 4,123 | 4,073 | 0 | $0.0533 | $0.0800 |
| 2 | 파싱 2회차 | 959 | 3,677 | 0 | 4,073 | $0.0395 | $0.0593 |
| 3 | 파싱 3회차 | 959 | 3,211 | 0 | 4,073 | $0.0348 | $0.0523 |
| 4 | 파싱 검증 (위험 규칙 5개) | 1,812 | 2,565 | 2,337 | 0 | $0.0351 | $0.0527 |
| **합계** | **4회** | **4,689** | **13,576** | **6,410** | **8,146** | **$0.163** | **$0.244** |

- 공시 단가 (`claude-sonnet-5`): 입력 $2, 출력 $10, 캐시 쓰기 $2.50, 캐시 읽기 $0.20 (1M 토큰당). 보수 단가: 입력 $3, 출력 $15, 캐시 쓰기 $3.75, 캐시 읽기 $0.30.
- 비용의 약 83%가 출력 토큰입니다(13,576 × $10/1M = $0.136).
- 시스템 프롬프트(약 4.1k 토큰)는 2·3회차에서 캐시로 읽혔습니다.
- 호출별 원자료: `data/nsclc/parsed_trial_run/usage_calls.jsonl`, 실행 로그: `run_log.txt` (커밋하지 않음).

## 3. 예상 대비

| 항목 | 값 |
|---|---|
| 원문 길이 | 1,320자 (8개 합계 33,611자의 3.93%) |
| 예상 (인수인계 추정 8개 $3–4 × 3.93%) | $0.118–0.157 |
| 예상 보수 (8개 $4.5–6 × 3.93%) | $0.177–0.236 |
| 중단선 (보수 상한 × 2) | $0.47 |
| 실측 (공시) | **$0.163** → 기본 예상 대비 **1.04–1.38배** |
| 실측 (보수 단가) | **$0.244** → 보수 예상 대비 **1.03–1.38배** |
| 중단 여부 | 중단선 미도달 (공시 누적 $0.163 / $0.47) |

## 4. 8개 전체 예상 (실측 × 원문 길이 비율)

| 단가 | 이 시험 실측 | × 25.46 (33,611 / 1,320) | 비고 |
|---|---|---|---|
| 공시 | $0.163 | **약 $4.1** | 인수인계 추정 $3–4보다 약간 높음 |
| 보수 | $0.244 | **약 $6.2** | 보수 추정 $4.5–6보다 약간 높음 |

- 단순 비례에는 한계가 있습니다.
  - 시스템 프롬프트는 길이와 관계없이 고정이고 2회차부터 캐시로 읽히므로, 긴 시험에서는 입력 비용이 비례보다 작습니다.
  - 반대로 출력은 규칙 수에 비례하고, 긴 시험(NCT04892953 10,968자, NCT05255302 7,939자)은 위험 규칙이 많아 검증 호출 출력도 커질 수 있습니다.
  - 그래서 **$4–6 범위를 권장 예산**으로 보고, 실행 중단선은 시험별 누적으로 두는 것을 제안합니다.
- 승인 상한 $15 대비 여유가 있습니다.

## 5. 파싱 결과 요약 (검수 필요)

- 규칙 20개 (선정 12, 제외 8). 대표 실행은 2회차(medoid). 1회차 22개, 2·3회차 20개.
- 판정 유형 분포: 계약 0.2 안건 1 결정 전이라 `contract_bridge.provisional_adjudication`과 같은 규칙을 적용했습니다(underspecified → expert, structured_evaluable → rule, 그 밖 → fact_llm).

| 유형 | 수 | 규칙 |
|---|---|---|
| rule | 3 | I2 나이 ≥18, I3 나이 ≤75, I4 성별 ALL |
| fact_llm | 16 | I1 동의, I5 ECOG, I6 기대여명, I7 조직 진단, I8 병기, I9 백금 치료력, I10 PD-(L)1 치료력, I11 측정 병변, E1 소세포암, E2 3년 내 다른 암, E3 AGA, E4 다른 면역치료, E5 도세탁셀, E6 출혈, E7 자가면역, E8 심근염·심근병증·부정맥 |
| expert | 1 | I12 "Adequate organ function." → `EXPERT_REVIEW`, 정답 만들지 않음 |

- 검증기: 위험 규칙 5개(I1, I6, I11, I12, E2)를 LLM이 원문과 대조했습니다.
  - 변경 2건: I12 category other → lab, E2 other → comorbidity.
  - 근거 충돌 1건: E2는 유형 규칙(자기 보고 허용)과 검증 제안(진료 기록)이 달라 진료 기록 요구로 적용됐습니다.
  - 누락 후보 0, 추가 규칙 0, 의사 확인 0.

### 이상·확인 사례

| # | 내용 | 영향 | 제안 |
|---|---|---|---|
| A1 | **ECOG(I5)가 3회 모두 구조화되지 않음** (`field=None`, `structured_evaluable=False`). 규칙 엔진은 `ecog` 항목을 지원함 | rule로 판정할 수 있는 기준이 fact_llm으로 감. WP2b CSV 후보는 rule | 파서 프롬프트·스키마 확인 필요. 8개 실행 전에 원인 파악 권장 |
| A2 | 실행 간 규칙 분할 차이: 1회차는 E8(심근염·심근병증·부정맥)을 3개로 나눔. 성별 규칙 문구가 매번 다름 | medoid 선택으로 흡수됨. 분할 차이는 판정 단위 차이 | 검수 시 E8 하나로 둘지 확인 |
| A3 | I4 "Sex eligibility: All"은 원문 기준이 아니라 구조화 필드(sex=ALL)에서 만든 규칙 | 항상 충족이라 판정 영향 없음 | 그대로 둬도 됨 |
| A4 | 원문 한 줄 "platinum-based chemotherapy and PD-1/L1 inhibitors"를 I9·I10 두 규칙으로 나눔 | WP2b 슬롯(`tx.prior_platinum`, `tx.prior_pd1_pdl1`)과 맞음 | — |
| A5 | WP2b CSV 후보와 판정 유형이 다른 기준: I6 기대여명(CSV expert → 파싱 fact_llm), I12(CSV unresolved → 파싱 expert), I5 ECOG·E5 도세탁셀·I9 백금(CSV rule → 파싱 fact_llm) | 안건 1 결정과 함께 맞춰야 함 | I6은 연구자 판단 기준이라 expert 유지 제안(검수 필요). I12는 WP2b D4대로 정답 없음 |
| A6 | E2·E7의 기간(3년 = 1,095일, 2년 = 730일)은 `time_window_days`로 구조화됨 | 정상 | — |

## 6. 산출물

| 파일 | 크기 | 커밋 |
|---|---|---|
| `data/nsclc/parsed_trial_run/store/NCT06928389.json` | 14.6 KB | 커밋 (`639d429`) — 대표 실행 + 검증 결과 + rule_meta |
| `data/nsclc/parsed_trial_run/runs/NCT06928389_run{1,2,3}.json` | 8–9 KB | 커밋 — 실행별 파싱 |
| `data/nsclc/parsed_trial_run/usage_calls.jsonl` | 1.3 KB | 커밋 — 호출별 usage |
| `data/nsclc/parsed_trial_run/trials/NCT06928389.json` | 3.3 KB | **제외** — WP2a 선정 기록(`data/nsclc/selection/candidates.csv`: 1,320자, 선정 9·제외 8 항목, `retrieved_at 2026-11-02T16:15:35+00:00`)과 일치하고, `data/cache/ctgov/` 원본과 바이트 단위로 같음. 원문 JSON 자체는 WP2a에서도 커밋하지 않았음(캐시는 git 무시) |
| `run_log.txt`, `run_log_attempt1_401.txt` | — | 제외 (실행 로그) |

- 커밋 전 키 검사: `sk-ant`, `api_key`, `x-api-key`, `bearer`, `auth_token`, `.env` 패턴 grep 결과 매치 0.
- 저장 레코드의 `trial_hash`(`699a518660451ca4`)로 입력 원문 동일성을 다시 확인할 수 있습니다.

## 7. 테스트 실행 방식 (API 차단)

`src/ctrec/config.py:12`가 `load_dotenv()`를 인자 없이 부릅니다. python-dotenv 1.13.0의 기본값은 `override=False`이고(`dotenv/main.py:392`), 이미 환경에 있는 변수는 건너뜁니다(`main.py:105` `if k in os.environ and not self.override: continue`).

| 실행 방식 | import 뒤 `ANTHROPIC_API_KEY` | 결과 |
|---|---|---|
| `env -u ANTHROPIC_API_KEY pytest` | `.env` 값이 다시 들어옴 | **차단 안 됨** (이전 팀 표준) |
| `ANTHROPIC_API_KEY= pytest` | 빈 값 유지 (길이 0) | 차단됨. `ant` 로그인 프로필도 디스크에 없어 SDK가 쓸 다른 자격 증명이 없음 |

**앞으로의 실행 명령** (worktree에서, 메인 `.venv` 사용 시 `PYTHONPATH=src` 필요):

```bash
PYTHONUTF8=1 ANTHROPIC_API_KEY= PYTHONPATH=src ../../../.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

- 결과 (`b1ea2b5` 기준): **1996 passed / 0 failed / 7 skipped** (심볼릭 링크 6, 브라우저 테스트 1). 이 브랜치 기준 커밋(`ddd9016`)의 팀 기대값과 같음.
- 네트워크 확인: 저장소 밖 pytest 플러그인으로 `anthropic._base_client.SyncAPIClient.request`·`AsyncAPIClient.request` 호출을 기록함. 7건이 기록됐는데 모두 `base_url="http://mock.invalid"` + `httpx2.MockTransport`로 만든 SDK 객체였음(`test_call_guard.py` 2, `test_web_budget.py` 2, `test_web_metering.py` 2, `test_web_paid_gate.py` 1). **실제 네트워크 호출 0**.
- 코드(`config.py` 등)는 수정하지 않았습니다. 팀 문서의 `env -u ANTHROPIC_API_KEY` 표기는 이 방식으로 바꾸는 것을 제안합니다.

## 8. 8개 전체 실행 전 원인 조사 (LLM 호출 없음, 코드 수정 없음)

### 8.1 ECOG(I5)가 rule로 남지 않은 원인

파서는 3회 모두 ECOG를 **구조화했습니다**. 그런데 코드의 무결성 검사가 되돌렸습니다. 끊기는 지점은 `operator="in"`입니다.

| 단계 | 위치 | 동작 |
|---|---|---|
| ① 스키마 | `src/ctrec/schemas.py:27` `Operator = Literal[">=", "<=", ">", "<", "==", "!=", "in", "not_in", "exists", "not_exists"]` | `in` 허용 |
| ② 프롬프트 | `src/ctrec/agents/criteria_parser.py:27` `field="ecog": ECOG 수행 상태 숫자.` | 연산자 지시 없음 → 모델이 "0 or 1"을 `operator="in"`, `value="0,1"`로 출력 (3회 모두, `parsing_notes`에 "ECOG 0/1은 범주형이므로 operator='in'") |
| ③ 무결성 검사 | `criteria_parser.py:137-142` `_integrity_problem`: `float(rule.value)` 실패 → "비교값 '0,1'이 숫자가 아님" | `criteria_parser.py:157-160` `demote_engine_fields`로 field·operator·value를 비우고 `structured_evaluable=False` (3회 모두 `[코드 검증] … -> LLM 판정으로 전환` 기록) |
| ④ 규칙 엔진 | `src/ctrec/tools/rule_engine.py:17` `_OPS`는 `>=, <=, >, <, ==, !=`만, `:263-267` ecog는 `_to_float(value)` + `_OPS`만 | ③을 통과했어도 `in`은 판정하지 못함(None) |

- 참고: 기존 동결 저장소 `data/parsed/`의 ECOG 규칙 2개는 `<= 1`로 구조화돼 있어 정상입니다. 같은 문장이라도 모델이 연산자를 고르는 방식에 따라 갈립니다.
- 8개 시험 모두 ECOG 0–1 기준이 있어서(WP2b CSV `ecog` 8행), 고치지 않으면 대부분 같은 방식으로 fact_llm이 될 가능성이 큽니다.

### 8.2 rule 비율 (파싱 3/20 = 15% vs WP2b CSV 47.9%)

이 시험의 CSV 행 15개 중 rule은 6개(40%)이고, 파싱 결과와 대응하면 다음과 같습니다.

| CSV 슬롯 (rule 후보) | 파싱 규칙 | 파싱 유형 | 원인 |
|---|---|---|---|
| `age` | I2, I3 | rule | 일치 |
| `ecog` | I5 | fact_llm | §8.1 버그 |
| `dx.histology` (소세포암 제외) | E1 | fact_llm | 규칙 엔진 필드가 아님 |
| `tx.prior_platinum` | I9 | fact_llm | 규칙 엔진 필드가 아님 |
| `tx.prior_pd1_pdl1` | I10 | fact_llm | 규칙 엔진 필드가 아님 |
| `tx.prior_docetaxel` | E5 | fact_llm | 규칙 엔진 필드가 아님 |
| (CSV에 없음) | I4 성별 ALL | rule | 구조화 필드에서 만든 규칙 |

- **공통 원인은 정의 차이입니다.** 파서의 `structured_evaluable`은 규칙 엔진이 읽는 4개 항목(`age`, `sex`, `ecog`, `lab:*`)에만 쓰입니다(`criteria_parser.py:24-29`). 반면 WP2b CSV의 `rule`은 "범주 슬롯 값으로 결정적으로 판정 가능"이라는 생성기 기준입니다(조직형·치료력 범주). 조직형·치료력은 규칙 엔진 필드가 아니라서, 파서가 아무리 잘 읽어도 rule이 될 수 없습니다.
- ECOG를 고치면 이 시험은 4/20 = 20%가 됩니다. 나머지 범주 기준을 결정적으로 판정하려면 규칙 엔진이 아니라 명제·질문 계약 경로(`propositions.py`, `question_contract.py`)나 생성기 범주 연결(`categorical`)이 맡아야 합니다.
- 제안: WP2b CSV의 `adjudication_candidate`에서 `rule`을 `rule_engine`(age/sex/ecog/lab)과 `categorical`(범주 슬롯)로 나눠 적기. 그러면 파싱 결과와 바로 비교할 수 있습니다(검수 필요).

### 8.3 실행 간 변동 (1회차 22개 vs 2·3회차 20개)

- 결정성 설정: `llm.create`(`src/ctrec/llm.py:174-200`)는 temperature·top_p를 보내지 않습니다. 현재 API는 이 값들을 받지 않고, 모델 기본(적응형 사고, effort high)으로 실행되므로 **실행마다 출력이 다를 수 있습니다**. 결정성은 3회 파싱 + 대표 실행 선택으로 보완하는 설계입니다(팀 결정 D8 Option C).
- 대표 실행 선택: `scripts/compile_trials.py:36-44` `medoid` — 규칙 문장을 정규화(소문자·영숫자)해 실행 쌍마다 자카드 겹침을 구하고, 합이 가장 큰 실행을 고릅니다. 동점이면 앞선 실행입니다. 이번에는 2회차가 선택됐습니다(2·3회차가 E8 문장과 대부분 규칙이 같음).
- E8 분할 원인: 프롬프트 `criteria_parser.py:17` "A and B처럼 여러 조건이 묶인 기준은 나눕니다. A or B는 하나의 규칙"입니다. "History of myocarditis, cardiomyopathy, and malignant arrhythmia"는 제외 기준 목록이라 의미상 "하나라도 있으면"(OR)인데, 표기는 "and"입니다. 그래서 1회차는 표기대로 나눴습니다. 제외 기준에서는 나눠도 판정 결과는 같지만 규칙 수와 medoid 점수가 흔들립니다.
- 성별 규칙(I4) 문구가 매번 다른 것은 `criteria_parser.py:21`("구조화 필드가 주어지면 … 규칙으로 포함")에 문구 형식이 없기 때문입니다. 판정 영향은 없습니다.

### 8.4 고칠 위치와 파일 구분

| 수정안 | 파일 | 보호 파일? | 비고 |
|---|---|---|---|
| A. 프롬프트: ECOG "0 or 1"·"0-1"은 `operator="<="`, `value="1"`로 적으라고 명시 | `src/ctrec/agents/criteria_parser.py:27` | 아님 (`PROTECTED` 목록 밖) | `team_parallel_start.md` §2 서연 변경 가능 목록에도 없음 → **공정현 확인 권장**. `parser_prompt_hash`가 바뀜(동결 기록은 `trial_hash`로만 최신 판정이라 재컴파일은 일어나지 않음) |
| B. 무결성 검사에서 정규화: ecog `in` + 0부터 이어진 정수 목록(`0,1`)이면 `<= max`로 바꿈 | `criteria_parser.py:137-160` (`check_integrity`) | 아님 | 프롬프트 해시 불변. 저장된 3회 파싱을 그대로 다시 쓸 수 있음(§8.5). 같은 소유 확인 필요 |
| C. 규칙 엔진에 `in`/`not_in` 지원 추가 | `src/ctrec/tools/rule_engine.py:17, 263-267` | 아님 (단, 판정 계층 파일. 동겸 금지 목록에 있음) | 판정 의미가 넓어지므로 가장 큰 변경. 이번 문제에는 A·B로 충분 |
| D. E8 같은 "목록형 and" 분할 지시 보완 | `criteria_parser.py:17` | 아님 | 선택 사항. medoid로 이미 흡수됨 |
| E. CSV `rule`을 `rule_engine` / `categorical`로 나누기 | `data/nsclc/cohort/slot_candidates.csv` | 아님 (`data/nsclc/**` 수정 가능) | 정의 정리. 코드 영향 없음 |

보호 파일(`pipeline.py`, `matcher.py`, `decision_*.py`, `data/parsed/`, `data/trials/` 등)을 수정할 필요는 없습니다.

### 8.5 결론

- **지금 8개를 돌리는 것은 권장하지 않습니다.** 8개 모두 ECOG 0–1 기준이 있고, 이번과 같이 `in`으로 출력되면 결정적으로 판정할 수 있는 기준이 LLM 판정으로 바뀝니다. 비용은 $4–6로 작지만, 고친 뒤 다시 돌리면 그만큼이 중복됩니다.
- **권장 순서**: B(무결성 정규화)를 먼저 적용합니다(+ 원하면 A). 그다음 이 시험으로 재확인한 뒤 8개를 실행합니다.
- **재확인 비용 추정**
  - B만: 저장된 3회 파싱(`runs/`)을 `--runs-dir`로 다시 쓰면 파싱 호출이 0이고 검증 호출 1회만 남습니다 → 이번 4번째 호출 기준 **약 $0.035 (보수 $0.053)**. 정규화 자체는 `--no-llm`으로 비용 0에서 먼저 볼 수 있습니다(저장하지 않음).
  - A 포함(프롬프트 변경): 새로 파싱해야 하므로 이번 파일럿과 같은 **약 $0.16 (보수 $0.24)**.
- 위 수정은 모두 코드 변경이라 이번 작업에서는 하지 않았습니다.

## 9. 정규화 적용 후 재파싱 (v2)

수정안 B만 적용했습니다(`d00e4fe`, `criteria_parser.normalize_engine_fields`). ECOG `in` 목록이 0부터 빈틈없이 이어지면 `<= 최댓값`으로 바꾸고, 그 밖의 목록은 지금처럼 LLM 판정으로 넘깁니다. 테스트 2개를 추가했고, 전체 테스트는 빈 키 방식으로 **1998 passed / 0 failed / 7 skipped**입니다.

### 9.1 `--no-llm` 사전 확인 (API 호출 0)

| 대상 | ECOG | 분포 (rule / fact_llm / expert) |
|---|---|---|
| 저장된 v1 `runs/` 재사용 (`compile_trials.py --no-llm`) | 변화 없음 | 3 / 16 / 1 |
| LLM 원본 캐시 (`data/cache/parsed/`, v1 마지막 실행) | `ecog <= 1`로 구조화 | 4 / 15 / 1 |

- v1 `runs/`에는 이미 무결성 검사를 거친 결과가 저장돼 있습니다. `parse_trial`(`criteria_parser.py:87-88`)이 검사를 마친 결과를 돌려주고, `compile_trials.py`가 그것을 저장하기 때문입니다. 그래서 저장된 실행으로는 새 정규화를 확인할 수 없고, §8.5의 "B만이면 검증 호출 1회(약 $0.035)" 추정은 틀렸습니다. 재파싱이 필요했습니다.

### 9.2 재파싱 결과 (출력 `data/nsclc/parsed_trial_run/v2/`)

| 항목 | v1 | v2 |
|---|---|---|
| 호출 | 4 (파싱 3 + 검증 1) | 4 (파싱 3 + 검증 1), fallback·재시도 0 |
| 토큰 (input / output / cache write / cache read) | 4,689 / 13,576 / 6,410 / 8,146 | 4,684 / 14,823 / 6,410 / 8,146 |
| 비용 (공시 / 보수) | $0.163 / $0.244 | **$0.175 / $0.263** |
| ECOG 구조화 | 0/3 실행 | **3/3 실행** (`[코드 정규화] … ecog in 0,1 -> ecog <= 1`) |
| 실행별 규칙 수 | 22 / 20 / 20 | 21 / 21 / 22 |
| 대표 실행 | 2회차 | 1회차 |
| 대표 실행 분포 (rule / fact_llm / expert) | 3 / 16 / 1 (규칙 20) | **3 / 17 / 1 (규칙 21)** |
| 검증 | 위험 5, 변경 2, 근거 충돌 1 (E2) | 위험 5 (ECOG 포함 "규칙 엔진 확정 판정"), 변경 1 (I10 → procedure), 근거 충돌 1 (I10 측정 병변 → 진료 기록 요구) |

- ECOG는 3회 모두 rule로 남았고, 검증기도 구조화를 유지했습니다.
- 그런데도 rule 수가 3인 이유가 있습니다. 대표 실행(1회차)과 2회차가 **구조화 필드 성별(sex=ALL) 규칙을 만들지 않았고**, 3회차만 만들었습니다(3회차 rule 4). 성별 ALL은 항상 충족이라 판정 영향은 없지만, rule 개수 비교에는 흔들림이 생깁니다.
- E8(심근염·심근병증·부정맥)은 v2에서 **3회 모두 3개로 분할**됐습니다(v1은 1회만). 제외 기준이라 판정 결과는 같습니다(§8.3).
- 원문 기준(성별 규칙 제외)으로 보면 ECOG가 rule로 바뀌어 age 2 + ECOG 1 = rule 3입니다. v1의 원문 기준 rule은 age 2개뿐이었습니다.

### 9.3 누적 비용

| | 공시 | 보수 |
|---|---|---|
| v1 | $0.163 | $0.244 |
| v2 | $0.175 | $0.263 |
| 합계 (승인 상한 $15) | **$0.338** | **$0.507** |

### 9.4 산출물

- v2 `store/`, `runs/`, `usage_calls.jsonl`: 키 패턴 grep 매치 0. **커밋 여부는 결정 필요**(이 보고서 커밋에는 넣지 않음).
- v2 `run_log.txt`: 제외(실행 로그).

### 9.5 8개 실행 전 남은 점

1. ECOG 문제는 해결됐습니다. 다만 다른 시험의 ECOG 표기("0-1", "0 to 1")가 `in`이 아닌 다른 형태로 나오면 이번 정규화가 다루지 않습니다. 무결성 검사의 기존 규칙은 범위 표기를 통과시키므로(`test_integrity_accepts_ranges_commas_and_structured_age`) 위험은 낮습니다.
2. 구조화 필드 성별 규칙이 실행마다 있었다 없었다 합니다. 판정 영향은 없지만 규칙 수 비교 지표가 흔들립니다. 프롬프트(`criteria_parser.py:21`)를 고칠지는 선택 사항입니다.
3. 8개 전체 예상은 이번 v2 실측 기준 공시 $0.175 × 25.46 ≈ **$4.5**, 보수 ≈ **$6.7**로, 승인 상한 $15 안입니다.
