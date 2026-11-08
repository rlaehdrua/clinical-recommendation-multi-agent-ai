# 독립 GT 도구 (I-D1)

> **독립 GT는 아직 없습니다.** 이 폴더에는 스키마, 빈 템플릿, 라벨러 안내 초안만 있습니다. 라벨이 든 세트, 라벨 시트, 판정 기록, `FROZEN_GT.json`은 없습니다(`tests/test_gt_guard.py`가 확인).
> **도구가 있다고 해서 엔진 정확도에 대해 증명되는 것은 없습니다.** 테스트는 `tests/data/gt_fixture/`의 FIXTURE_ONLY 가짜 데이터로 도구의 계산과 검사만 확인합니다. 그 수치는 엔진 성능과 무관합니다.

설계 근거: `trialog-p0-analysis/D_independent_gt_design.md` 2절(층위·주석·블라인딩·버전·크기)과 3절(지표 A1-A7, B1-B6, C1-C5, E1-E6).

## 1. 구성

| 위치 | 내용 |
|---|---|
| `gt/schema/*.schema.json` | pydantic 모델에서 내보낸 JSON Schema (`scripts/gt_validate.py --export-schema gt/schema`). 모델과 다르면 테스트 실패 |
| `gt/templates/` | 사례 파일·라벨 시트·판정 기록·세트 메타 템플릿, 라벨러 안내 초안. 값은 모두 `<...>` 자리표시 |
| `scripts/gt_lib/` | 라이브러리 (아래). **엔진(ctrec)을 import하지 않음** |
| `scripts/gt_*.py` | 명령줄 도구 |
| `tests/test_gt_*.py`, `tests/data/gt_fixture/` | 도구 테스트와 FIXTURE_ONLY 가짜 데이터 |

라이브러리 위치를 `gt/`가 아니라 `scripts/gt_lib/`로 정한 이유: 스크립트가 `sys.path`에 자기 폴더를 넣고 `from gt_lib import ...`로 쓰고, 테스트는 `scripts/`를 경로에 넣어 같은 모듈을 씁니다. `gt/`는 데이터·문서 폴더로 남겨 동결 대상과 코드를 섞지 않습니다.

| 모듈 | 역할 |
|---|---|
| `gt_lib/models.py` | 스키마: G-U(문장) / G-C(기준) / G-P(쌍). 모든 항목에 `input`(엔진에 줄 수 있는 것)과 라벨(`annotations`, `final`)이 분리됨. 라벨러 ID·시각, `needs_expert`(+사유), `adjudication`, `source`(출처·작성자 독립성·기존 세트 비파생 진술). 세트에 `blinding` 증명 |
| `gt_lib/leak.py` | 누출 검사 |
| `gt_lib/freeze.py` | 동결·확인·상태 전이 |
| `gt_lib/metrics.py` | 지표 계산, 엔진 출력 형식 정의 |
| `gt_lib/stats.py` | Wilson·정확(Clopper-Pearson) 구간, McNemar 정확 검정, Cohen's kappa |
| `gt_lib/labelio.py`, `agreement.py`, `assemble.py` | 라벨 시트 입출력, 일치도·판정 대상, 세트 조립 |

### 1.1 세 층의 라벨

| 층 | `input` (엔진 가능) | 라벨 (엔진 금지) |
|---|---|---|
| G-U | `kind`(conflict/value), `text`, `criterion`, `quote`, `holds`, `field`, `value`, `unit`, `reference_date` | `decision`(accept/hold), `category`, `hearsay`, `approx`, `conflict`, `threshold_crossing`, `concept_cluster`, `source_priority_note` |
| G-C | `trial_id`, `criterion_id`, `criterion_text`, `trial_source`, `parsed_store_ref` | `interpretation`, `country_scope`(scoped, scope_unit, countries, applies_when), `applicability_condition`, `needs_clinical_judgement`, `structured` |
| G-P | `patient_id`, `record_ref`(경로+sha256, 원문은 저장소 밖 가능), `trial_id`, `parsed_store_ref`, `institution_country`, `grid`(국가 격자 셀) | 기준별 `MET/NOT_MET/UNKNOWN_FROM_RECORD/NEEDS_CLINICIAN` + 근거 위치, 쌍 `ELIGIBLE/INELIGIBLE/NOT_DETERMINABLE/NEEDS_CLINICIAN`, `aggregation_rule_version` |

스키마가 강제하는 것: `input`에 다른 필드 금지(엔진 출력·정답 필드를 넣을 수 없음), 상충이면 hold, 문턱을 가로지르는 전해 들은 값이면 hold, 판정자 ≠ 라벨러, `agreed/adjudicated`면 `final` 필수, `ambiguous/pending`이면 `final` 없음, 블라인딩 증명은 "보지 않았다"만 표현 가능, FIXTURE_ONLY 세트와 실제 세트의 이름·출처 분리, GT 쌍 라벨에 엔진 상태(`CONFIRMED_*`) 금지.

## 2. 작업 흐름

명령은 저장소 루트에서 실행합니다. 공통 환경:

```bash
export PYTHONPATH=$PWD/src PYTHONIOENCODING=utf-8
python -c "import ctrec; print(ctrec.__file__)"   # 이 작업 트리의 src/ctrec인지 확인
```

| 단계 | 누가 | 명령·산출물 |
|---|---|---|
| 0. 사전 등록 | GT 책임자 + 엔지니어 | 지표 정의(`scripts/gt_lib/metrics.py`, `stats.py`)와 합격 기준(D 3절)을 세트 공개 **전에** 커밋. 동결 시 평가 결과를 바꿀 수 있는 코드(`metrics.py`, `stats.py`, `models.py`, `labelio.py`, `freeze.py`, `scripts/gt_eval.py`, `scripts/gt_engine_adapter.py`)의 해시가 `metric_definitions`에 기록되고, 동결 뒤 바뀌면 `gt_verify.py`가 문제로 보고하며 그 뒤 결과는 공식이 아님 |
| 1. 규약 승인 | 임상의 (CL-1, CL-2, CL-9) | `gt/protocol/annotation_protocol_v1.md` (승인본), 상충 질환 묶음표, 쌍 집계 규칙. `templates/labeler_instructions_v1_DRAFT.md`는 초안 |
| 2. 사례 작성 | 엔진 A·B·C 작업에 참여하지 않은 작성자 | `templates/*_cases.template.json` 형식의 사례 파일. 세트 1-6의 작성 프롬프트·범주 목록을 쓰지 않음. 원문은 **저장소 밖** 보관 |
| 3. 누출 검사 | 엔지니어 | 아래 3절. 사례 파일을 세트 형식으로 조립한 뒤에도 다시 실행 |
| 4. 라벨 시트 | 엔지니어 | `python scripts/gt_assemble.py sheet --cases cases.json --out sheet_L-A.csv` (라벨러마다 따로). 엔진 출력 열 없음 |
| 5. 이중 라벨 | 라벨러 A, B (CL-3, CL-8) | 서로 독립, 엔진 출력 비공개. 라벨러 안내 따름 |
| 6. 일치도 | 엔지니어 | `python scripts/gt_agreement.py --layer G-U --a sheet_L-A.csv --b sheet_L-B.csv --out agreement.json --queue adjudication_queue.csv`. 목표 kappa ≥ 0.7. 0.6 미만 필드는 규약을 고친 뒤 재라벨. 같은 라벨러 ID가 두 시트에 있으면 거부 |
| 7. 판정 | 판정자 (라벨러와 다른 상급 임상의, CL-4) | `adjudication_queue.csv`(불일치·확신도 3 미만·needs_expert)를 `adjudication_log.csv`로 채움. `status`=adjudicated(최종 라벨 JSON) 또는 ambiguous(판정 불가, 주 분모 제외) |
| 8. 조립·검증 | 엔지니어 | `python scripts/gt_assemble.py build --cases cases.json --meta set_meta.json --labels sheet_L-A.csv sheet_L-B.csv --adjudication adjudication_log.csv --out <보관소>/g_u/set7.json` 다음 `python scripts/gt_validate.py <보관소>/g_u/set7.json --freezable --adjudication-log adjudication_log.csv`. 동결 조건: 빈 세트 금지, agreed 항목의 `final`은 라벨러들이 합의한 라벨과 같아야 함, adjudicated 항목은 `log_ref`가 판정 기록의 `log_id`로 있고 그 행의 `item_id`·`status`·판정자·`final_label_json`이 항목과 같아야 함 (기록 없이는 거부). `gt_freeze.py`는 gt 루트·보관소의 `adjudication_log.csv`를 읽어 같은 대조를 함 |
| 9. 동결 | GT 책임자 | `python scripts/gt_freeze.py freeze --gt-root gt --external <보관소> --version gt-v1.0 --reference-date 2026-11-07 --trial-store data/parsed/FROZEN.json --labelers L-A L-B --adjudicator ADJ-1`. 스키마·동결 조건·누출 검사 중 하나라도 실패하면 쓰지 않음. 저장소에는 `gt/FROZEN_GT.json`(해시)만 커밋 |
| 10. 확인 | 누구나 | `python scripts/gt_verify.py --gt-root gt --external <보관소>` (보관소가 없으면 외부 파일은 "확인 불가"로 표시) |
| 11. 1회 측정 | 엔지니어 | A·B·C 코드를 고정한 커밋에서: `python scripts/gt_engine_adapter.py <보관소>/g_u/set7.json --out gt/results/gt-v1.0/<sha>/engine.json --engine-commit <sha>` → `python scripts/gt_eval.py --gt <보관소>/g_u/set7.json --engine gt/results/gt-v1.0/<sha>/engine.json [--baseline <전 커밋 출력>] --frozen gt --official --out gt/results/gt-v1.0/<sha>/report.json` → `python scripts/gt_freeze.py advance --gt-root gt --to measured_once --reason "1회 측정" --measurement gt/results/gt-v1.0/<sha>/report.json --engine-commit <sha>` |
| 12. 강등 | GT 책임자 | 결과를 보고 코드를 고치면 즉시 `python scripts/gt_freeze.py advance --gt-root gt --to regression --reason "..."`. 그 뒤 세트는 회귀 세트(EN-12, A·B 병합 뒤 `tests/data/` 쪽으로)이며 독립 평가로 인용하지 않음. 새 독립 평가는 새 세트·새 버전 |

상태는 `sealed → measured_once → regression` 순서로만 바뀝니다. `advance`는 먼저 `verify()`를 실행하고, 문제가 있으면 상태를 바꾸지 않습니다(외부 보관이면 `--external` 필요). 같은 버전 재동결은 거부되고, 새 버전을 동결하면 이전 목록은 `gt/frozen_history/`로 옮겨집니다. `gt_eval.py`의 공식 결과 조건은 4.1절에 있습니다. 조건 하나라도 못 맞추면 결과에 "탐색적"을 붙이고, `--official`을 주면 거부합니다.

## 3. 누출 검사

```bash
python scripts/gt_leak_check.py <보관소>/g_u/set7.json [--extra-corpus 추가자료] [--json refusals.json]
```

거부 사유:

1. `text_overlap`: 사례 원문(`text`, G-C는 `criterion_text`)이 다음과 정규화(NFKC·소문자·구두점·공백) 후 같거나, 한쪽이 다른 쪽에 포함(25자 이상)되거나, 토큰 Jaccard ≥ 0.8, 또는 짧은 쪽 토큰 8개 이상에서 포함률 ≥ 0.9. 한글이 든 조각은 공백을 모두 지운 형태로도 비교합니다(완전 일치 8자 이상, 포함 관계 15자 이상). 그래서 "간질성 폐질환" → "간질성폐질환" 같은 띄어쓰기 변형도 거부됩니다.
   비교 대상: `docs/dev/*.json`(held-out 4·5·6 포함), `tests/data/**/*.json`(회귀 세트 1-3·dev), `eval/**/*.json`, `tests/**/*.py`의 문자열 상수. GT 도구 자신의 가짜 데이터는 비교 대상에서 빼고 FIXTURE 표시로 따로 거부합니다.
2. `engine_field`: 엔진 출력 키(`directness`, `match_status`, `validation_flags`, `engine_*` 등)나 엔진 쌍 상태 값(`CONFIRMED_ELIGIBLE` 등)이 항목 어디에든 있음. 메모·사유·출처 설명 같은 자유 텍스트 **안에** 든 엔진 상태 문자열도 잡습니다(`confirmed_eligible`, `EngineStatus=...`, `PairStatus.X`, `MatchStatus.ELIGIBLE`, `VERIFICATION_PENDING`, `ExpertReview`, `likely_eligible`, `match_status` 등, `leak.ENGINE_TEXT_PATTERNS`). GT 어휘 `ELIGIBLE`/`INELIGIBLE` 단독과 띄어 쓴 일반 영어("expert review")는 잡지 않습니다.
3. `no_blinding`: 세트에 블라인딩 증명이 없음.
4. `fixture`: FIXTURE_ONLY 세트·문장 (`--allow-fixture`는 도구 테스트 전용).

거부를 무시하는 옵션은 일부러 두지 않았습니다. 오탐이면 사례를 고쳐 쓰고 다시 검사합니다. G-U의 `criterion`(기준 문장)은 시험 사이에 원래 반복되므로 검사하지 않습니다. G-C에서 NCT06216301 같은 이미 알려진 실패 기준이 걸리면, 그 기준은 독립 평가 대상에서 빼고 따로 보고하는 것을 권합니다.

## 4. 측정과 엔진 출력 형식

`scripts/gt_engine_adapter.py`는 G-U 항목을 `scripts/eval_m_holdout.py`와 같은 결정적 함수로 오프라인 실행합니다(실제 LLM 호출 없음).

- conflict: `evidence_directness.check(criterion, quote, text, holds=...)`
- value: `value_grounding.ground`(age/sex/ecog) 또는 `ground_lab(field, value, text, text, unit=...)`. 반환 `None` = 채택

라벨 차단: 어댑터는 세트 파일에서 `input`만 꺼내 `UtteranceInput`(라벨 필드 없음, 다른 필드 금지)으로 만든 뒤 엔진 함수에 넘깁니다. `tests/test_gt_adapter.py`가 엔진 함수 인자를 가로채 라벨 문자열이 없는지 확인합니다.

출력 형식 `gt-engine-output/1` (G-P·G-C 출력은 다른 어댑터가 같은 형식으로 만들어야 함, 정의는 `scripts/gt_lib/metrics.py` 머리말):

```json
{"format": "gt-engine-output/1", "gt_set_id": "...", "gt_set_sha256": "<GT 파일 sha256>", "level": "U|S|R",
 "engine": {"commit": "...", "ctrec_file": "...", "reference_date": "..."}, "generated_at": "...",
 "items": [{"item_id": "...", "layer": "G-U", "kind": "conflict", "result": {"directness": "...", "flags": []}},
           {"item_id": "...", "layer": "G-U", "kind": "value", "result": {"accepted": true, "grounding": null}},
           {"item_id": "...", "layer": "G-P", "result": {"pair_status": "CONFIRMED_ELIGIBLE", "hold_reason": "...", "deciding_criteria": ["I1"]}}]}
```

`gt_eval.py`는 `gt_set_sha256`이 GT 파일과 다르면 거부합니다.

### 4.1 공식 결과와 탐색적 결과

`report["official"]`이 `true`인 것은 다음을 **모두** 만족할 때뿐입니다. 하나라도 어기면 `official_blockers`에 사유가 남고 `exploratory`가 붙습니다.

1. `--frozen gt`를 주었고 `FROZEN_GT.json`이 `verify()`를 통과함 (외부 보관이면 `--external`도 필요)
2. 상태가 `sealed` (측정 뒤·강등 뒤에는 공식 결과 없음)
3. `--gt` 파일의 sha256이 매니페스트의 세트 파일(`g_u/`, `g_c/`, `g_p/`) 해시로 등록되어 있음. 동결 루트가 깨끗해도 다른 파일·변조본을 넣으면 공식이 아님
4. 엔진 출력(그리고 `--baseline`이 있으면 기준선)이 GT 항목을 **100%** 덮고, GT에 없는 항목이 없음

`--official`을 주면 조건을 못 맞출 때 보고서를 쓰지 않고 종료 코드 1로 끝납니다. 1회 측정(2절 11단계)에는 `--official`을 씁니다. `--official` 없이 실행하면 예전처럼 탐색적 결과를 계산하고 종료 코드 0입니다.

**빠진 항목 처리 (D-2):** 빠진 항목을 "최악의 경우"로 채우지 않고 **공식 결과 자체를 거부**하는 쪽을 택했습니다. 최악의 경우는 지표마다 다르고(A1에서는 확정 경로, A5에서는 보류가 최악), 대체값을 만들어 넣으면 엔진이 내지 않은 결과가 보고서에 섞입니다. 따라서 커버리지가 100%가 아니면 공식 수치가 없고, 탐색적 계산에서도 빠진 항목은 `coverage.missing_engine_output`에 남고 분모에서는 빠집니다(탐색적 수치를 공식 수치처럼 인용하면 안 됨). 거짓 확정 항목을 출력에서 빼서 E1=0을 만드는 방식은 이렇게 막힙니다.

**형식이 틀린 엔진 기록은 오류입니다:** value 항목의 `accepted`가 없거나 true/false가 아님, conflict 항목에 문자열 `directness`가 없음, `pair_status`가 알려진 값이 아님, `deciding_criteria`가 문자열 목록이 아님, `scope_represented`가 true/false가 아님, `result`가 객체가 아님, `item_id` 없음·중복. 예전처럼 `accepted`가 없으면 보류로 세는 기본값은 없습니다. 기준선 출력도 같은 검사를 받습니다.

### 4.2 계산하는 지표

| 지표 | 계산 | 비고 |
|---|---|---|
| A1-A5 | G-U conflict | 확정 경로 = `direct/unchecked/structured` (`Check.ok`). A5는 D 원문 "≠ direct"를 A1과 같은 확정 경로 정의로 읽음. A2는 질환 묶음별 재현율과 0/≥5 묶음 목록 포함 |
| B1(+문턱 가로지름), B2, B3, B4 | G-U value | B2는 보고만 (정책 CL-10) |
| 짝지은 비교 | `--baseline` | McNemar 정확 검정, 새로 보류된 정상 값, 새로 채택된 음성 (B3·B4의 전후 기준) |
| E1, E2, E4, E6 | G-P | 거짓 확정 적격·거짓 확정 부적격·미해결(GT 부적격인데 미확정)·과잉 보류(GT 적격인데 미확정)를 따로 셈. CI∧GT NEEDS_CLINICIAN도 따로 |
| E3 | G-P + `--baseline` | 상태 전이표, 확정 진입·이탈 수 |
| C2-C4 | G-P의 `grid` 쌍 | 범위 밖 CI, 범위 안 잘못된 CE, 국가 모름 확정 |
| C1 | G-C | 엔진 출력에 `scope_represented`가 있을 때만 |

모든 비율은 분자·분모·Wilson 95%·정확 95%를 냅니다. 분모 0은 **"추정 불가"**이고 합격으로 치지 않습니다. 분모가 D 2.8의 최소 n보다 작으면 합격 판정을 하지 않습니다(`d_criterion_met = null`). AMBIGUOUS 항목은 주 분모에서 빼고, 그 항목에서 엔진이 확정한 수를 따로 냅니다.

### 4.3 측정하지 않는 지표 (보고서에 "미측정"으로 표시)

| 지표 | 이유 |
|---|---|
| A6 | 대역 Session(S) 측정. 오프라인 가능하나 이 어댑터는 함수 수준(U)만 |
| A7, B6 | 실제 LLM(R) 필요. 승인 전 0건 |
| B5 | 회귀 테스트 통과 여부는 pytest로 따로 |
| C5 | 두 커밋 상태 전이표 도구(EN-7) 필요 |
| E5 | 순환 GT 지표. 독립 GT로 계산하지 않음, 정확도로 인용 금지 |
| G-P·G-C 엔진 실행 | 대역 Session 어댑터 미구현. 형식만 정의됨 |

## 5. 최소 크기 (D 2.8)

| 대상 | 최소 n |
|---|---|
| A 상충 양성 | 60 (권장 100), 질환 묶음 ≥ 12 × 묶음당 ≥ 5 |
| A 상충 음성 | 60 |
| B 전해 들은 값·불확실 음성 | 60 (그중 문턱을 가로지르는 범위 ≥ 30) |
| B 정상 값 양성 | 80 이상 (범위·추정 표현이지만 문턱 한쪽인 정상 20 포함) |
| C 국가 범위 기준 (G-C) | 서로 다른 시험의 기준 ≥ 15 |
| C 쌍 격자 | 기준당 9셀 (국가 3 × 나이 3), 15기준이면 135쌍 |
| E1 / E2 분모 | 엔진 CONFIRMED_ELIGIBLE / CONFIRMED_INELIGIBLE ≥ 60 |
| G-P 전체 | 환자 ≥ 40 × 후보 시험 3-5 ≈ 150-200쌍 (실효 n은 환자 수에 가까움) |

0건 관측일 때 정확 95% 상한은 n=60에서 약 6%, "3의 규칙"으로 약 5%입니다. 이 도구는 정확 상한으로 판정합니다(A1·A4·B1·E2 기준 "0건 또는 상한 ≤ 5%").

## 6. 사람이 해야 하는 일과 엔지니어링

| 구분 | 일 | `needs_expert` 연결 |
|---|---|---|
| 임상 전문가 | CL-1 규약 승인, CL-2 상충 질환 묶음(엔진 사전과 독립), CL-3 G-U 이중 라벨, CL-4 판정, CL-5 국가 범위 해석(기관 국가 vs 거주지), CL-6 임상 판단 필요 기준 분류, CL-8 G-P 기준별·쌍 라벨, CL-9 집계 규칙 승인, CL-10 B2 정책 | 라벨러가 `needs_expert=true`와 사유를 쓰면 판정 대상에 자동으로 들어가고, 판정자가 결정하거나 ambiguous로 둠 |
| 사례 작성자 | 세트 7 문장, G-P 합성 기록. 엔진 A·B·C 작업과 분리된 사람 | `source.author_independent_of_engine_work`, `not_derived_from_existing_sets` |
| GT 책임자 | 블라인딩 증명, 보관소 관리, 동결·상태 전이 결정 | `blinding` |
| 엔지니어 | 스키마 검증, 누출 검사, 시트 생성, kappa 계산, 조립, 동결·확인, 1회 측정 실행. **라벨 내용에는 관여하지 않음** | — |

## 7. 테스트

```bash
python -m pytest -q tests/test_gt_*.py
```

FIXTURE_ONLY 가짜 데이터만 씁니다. 확인 내용: 스키마 규칙, 동결·확인 왕복과 변조·누락·추가 파일 탐지, 외부 보관 해시, 상태 전이, 세트 6 문장 복사(정확·대소문자·공백·삽입·한 단어 변경) 거부, 엔진 필드·블라인딩 누락 거부, 구간 공식(알려진 값), 분모·분모 0 처리, 오류 분해, 짝지은 비교, 어댑터 라벨 차단, `gt/`에 라벨 데이터 없음.

## 8. 한계

- 누출 검사는 문자열·토큰 수준입니다. 의역, 번역, 숫자만 바꾼 짧은 정형 문장(예: "ALT 40 U/L on Oct 24, 2026.")은 놓칩니다. 세트 1-6의 작성 프롬프트나 범주 구성을 따라 쓴 경우도 잡지 못합니다. 이는 블라인딩 증명과 작성자 분리로 막아야 합니다.
- 블라인딩 증명은 자기 신고입니다. 도구는 증명이 있는지와 내용 형식만 확인합니다.
- 측정 위치는 함수 수준(U)뿐입니다. 실제 경로에서는 LLM이 고른 인용·값이 이 함수에 들어가므로, U 결과는 "그 인용이 골라졌을 때"의 조건부 값입니다.
- G-P·G-C를 엔진에 넣는 어댑터(대역 Session)는 없습니다. 형식과 지표 계산만 있습니다.
- 쌍 지표의 구간은 쌍을 독립으로 본 이항 구간입니다. 환자 단위 군집을 반영한 부트스트랩(`sim/stats.py` 방식)은 아직 없습니다.
