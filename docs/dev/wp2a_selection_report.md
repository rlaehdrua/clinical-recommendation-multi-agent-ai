# WP2a NSCLC 시험 후보 선별 보고 (2026-11-03, 서연)

브랜치 `feat/wp2a-nsclc-trials` (기준 `origin/integration/dcvc` ddd9016). push·PR 안 함. 생성기 브랜치(`research/cohort-generator`)는 병합하지 않았습니다.

## 결과

ClinicalTrials.gov 검색 100개 중 53개가 필터(모집 중 · 중재 연구 · 최소 나이 18세 이상)를 통과했습니다. 키워드 점수로 후보를 추린 뒤, 서연이 원문 길이·대상 환자 겹침·EGFR 방향으로 보정해 추천 8개와 예비 2개를 확정했습니다(2026-11-03). 각 시험의 기준 해석은 모두 "검수 필요"입니다.

| 파일 | 내용 |
|---|---|
| `data/nsclc/selection/candidates.csv` | 100행 전체: 단계·나이·등록 질환, 기준 수(포함/제외)와 형식, 원문 길이, 공통 정보 8개 언급 여부, 필터 결과와 탈락 사유, 겹침 점수, 키워드 순위, 선별 결과(`recommendation`·`selection_note`), 검수·선별자·선별일 |
| `data/nsclc/selection/recommended.md` | 추천 8 + 예비 2와 근거, 제외 2개와 사유, 선별 메모, 방법과 한계 |

- 추천: NCT07098338, NCT07027514, NCT07416058, NCT06216301, NCT05255302, NCT04892953, NCT06928389, NCT07680790
- 예비: NCT05242965, NCT07590531
- 제외: NCT07217301(원문 19,291자로 추천 8개 합계의 36%, 같은 역할의 짧은 시험인 NCT06928389로 대체), NCT06440954(연수막 전이 한정으로 다른 시험과 대상 환자가 거의 겹치지 않음, NCT07680790으로 대체)

## 실행한 것

1. `env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN python scripts/fetch_trials.py --condition "non-small cell lung cancer" --max 100`. 100개 레코드가 `data/cache/ctgov/`(git 제외)에 저장됐습니다. 조회 시각 2026-11-02T16:15:35Z. `--nct`는 쓰지 않았습니다.
2. 분석은 저장소 밖 임시 스크립트로 했습니다(파이썬 표준 라이브러리, 정규식). LLM·유료 API 호출 0건, `compile_trials.py` 실행 안 함.
3. 필터 탈락: 모집 상태 31개(모두 `NOT_YET_RECRUITING`), 관찰 연구 17개, 최소 나이 18세 미만 1개.
4. 통과 53개 중 공통 정보 언급 빈도: 병기 52, 이전 치료 49, ECOG 47, 뇌전이 34, EGFR 28, ALK 19, 검사 수치 18, PD-L1 14. 등록 질환이 모두 폐암인 시험은 31개입니다.
5. 교체 후보 4개(NCT06928389, NCT07680790, NCT07738172, NCT04892953)의 병기·치료 차수·EGFR/ALK·PD-L1·뇌전이 조건을 원문 인용으로 비교한 뒤 서연이 교체를 정했습니다. 근거는 `recommended.md`의 선별 메모에 있습니다.

## 계산 오류 정정

첫 커밋(756ca19)의 "검사 수치" 정규식은 대소문자를 무시한 채 `eGFR`(사구체 여과율)을 찾아 `EGFR`(유전자)까지 잡았습니다. 100개 중 26개 행이 영향을 받았습니다.

| 값 | 정정 전 | 정정 후 |
|---|---|---|
| 통과 53개 중 "검사 수치" 언급 | 34개 | 18개 |
| 공통 정보 8/8인 시험 | 7개 | 3개 (NCT05255302, NCT04892953, NCT07217301) |
| 첫 추천 1–4위(NCT07098338, NCT07027514, NCT07416058, NCT06216301) | 8/8 | 7/8 |

b56836c에서 `eGFR`만 대소문자를 구분하도록 고쳐 공통 정보 수·겹침 점수·키워드 순위를 다시 계산했습니다. 최종 선별은 키워드 순위가 아니라 위 보정 기준으로 정했으므로 정정 후에도 바뀌지 않았습니다.

## 보호 파일 확인 (b56836c 커밋 후)

| 확인 | 결과 |
|---|---|
| `data/parsed/*`·`data/trials/*` 53개 파일 sha256 (검색 전 / 후) | 모두 같음 |
| `data/parsed/FROZEN.json`의 26개 시험 해시와 실제 파일 | 불일치 0 |
| `git status` | 바뀐 파일은 `data/nsclc/selection/`뿐, `data/cache/`는 무시됨 |
| `tests/test_decisions.py`, `tests/test_store_patch.py` | 46 passed |
| `test_llm_pilot_exec.py::test_frozen_store_and_policy_files_unchanged` | passed |

## 한계와 다음 단계

- 공통 정보는 키워드 언급 여부만 봅니다. 기준의 방향·값·의학적 적합성은 판단하지 않았습니다. 병기(`advanced`·`metastatic` 포함)와 이전 치료(`prior`)는 거의 모든 시험에 걸립니다.
- 기준 수는 글머리표 줄 수입니다. 줄글 형식 4개는 문단 수로 셌고, 하위 번호도 함께 셉니다.
- 추천 8개의 원문 합계는 33,611자(평균 4,201자)로, 교체 전 52,998자보다 37% 줄었습니다. 가장 긴 시험은 NCT04892953(10,968자)입니다.
- NCT07680790은 공개 기준 마지막 줄이 "Other protocol-defined inclusion/exclusion criteria apply."라 공개 원문만으로는 기준이 불완전합니다.
- 다음 단계: 고른 8개를 `data/nsclc/trials/`에 복사 → 비용 승인 후 1개 시험으로 파싱 시험 실행(`compile_trials.py --trials-dir data/nsclc/trials --store data/nsclc/parsed`) → 검수 → `data/nsclc/parsed/FROZEN.json`으로 동결. `freeze_parsed.py`에는 `--store` 옵션이 없어 동결 방법은 따로 정해야 합니다(`trial_store.freeze(store_dir=...)`는 이미 지원).
