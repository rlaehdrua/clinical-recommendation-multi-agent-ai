# WP2a NSCLC 시험 후보 선별 보고 (2026-11-03, 서연)

브랜치 `feat/wp2a-nsclc-trials` (기준 `origin/integration/dcvc` ddd9016). push·PR 안 함. 생성기 브랜치(`research/cohort-generator`)는 병합하지 않았습니다.

## 결과

ClinicalTrials.gov 검색 100개 중 53개가 필터(모집 중 · 중재 연구 · 최소 나이 18세 이상)를 통과했습니다. 공통 정보 언급이 많은 순으로 8개를 추천하고 2개를 예비로 두었습니다. 모두 "검수 필요" 상태이며 선별자·선별일 칸은 비워 두었습니다.

| 파일 | 내용 |
|---|---|
| `data/nsclc/selection/candidates.csv` | 100행 전체: 단계·나이·등록 질환, 기준 수(포함/제외)와 형식, 원문 길이, 공통 정보 8개 언급 여부, 필터 결과와 탈락 사유, 겹침 점수, 순위, 검수·선별 칸 |
| `data/nsclc/selection/recommended.md` | 추천 8 + 예비 2, 한 줄 근거, 방법과 한계 |

추천: NCT07098338, NCT07027514, NCT07416058, NCT06216301, NCT05255302, NCT04892953, NCT07217301, NCT06440954. 예비: NCT05242965, NCT07590531.

## 실행한 것

1. `env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN python scripts/fetch_trials.py --condition "non-small cell lung cancer" --max 100`. 100개 레코드가 `data/cache/ctgov/`(git 제외)에 저장됐습니다. 조회 시각 2026-11-02T16:15:35Z. `--nct`는 쓰지 않았습니다.
2. 분석은 저장소 밖 임시 스크립트로 했습니다(파이썬 표준 라이브러리, 정규식). LLM·유료 API 호출 0건, `compile_trials.py` 실행 안 함.
3. 필터 탈락: 모집 상태 31개(모두 `NOT_YET_RECRUITING`), 관찰 연구 17개, 최소 나이 18세 미만 1개.
4. 통과 53개 중 공통 정보 언급 빈도: 병기 52, 이전 치료 49, ECOG 47, 뇌전이 34, 검사 수치 34, EGFR 28, ALK 19, PD-L1 14. 등록 질환이 모두 폐암인 시험은 31개입니다.

## 보호 파일 확인

| 확인 | 결과 |
|---|---|
| `data/parsed/*`·`data/trials/*` 53개 파일 sha256 (검색 전 / 후) | 모두 같음 |
| `data/parsed/FROZEN.json`의 26개 시험 해시와 실제 파일 | 불일치 0 |
| `git status` | 새 파일은 `data/nsclc/`뿐, `data/cache/`는 무시됨 |
| `tests/test_decisions.py`, `tests/test_store_patch.py` | 46 passed |
| `test_llm_pilot_exec.py::test_frozen_store_and_policy_files_unchanged` (커밋 후) | passed |

## 한계와 다음 단계

- 공통 정보는 키워드 언급 여부만 봅니다. 기준의 방향·값·의학적 적합성은 판단하지 않았습니다. 병기(`advanced`·`metastatic` 포함)와 이전 치료(`prior`)는 거의 모든 시험에 걸립니다.
- 기준 수는 글머리표 줄 수입니다. 줄글 형식 4개는 문단 수로 셌습니다.
- 추천 8개의 원문 합계는 52,998자입니다. NCT07217301 하나가 19,291자(36%)라 파싱 비용을 가장 크게 좌우합니다.
- 다음 단계: 사람이 선별하고 선별자·날짜를 기록 → 고른 시험을 `data/nsclc/trials/`에 복사 → 비용 승인 후 1개 시험으로 파싱 시험 실행(`compile_trials.py --trials-dir data/nsclc/trials --store data/nsclc/parsed`) → 검수 → `data/nsclc/parsed/FROZEN.json`으로 동결. `freeze_parsed.py`에는 `--store` 옵션이 없어 동결 방법은 따로 정해야 합니다(`trial_store.freeze(store_dir=...)`는 이미 지원).
