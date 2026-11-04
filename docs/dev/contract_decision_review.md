# 공통 계약(WP0) 결정 검토 — 열린 질문 5개 + 추가 미결정 사항

> 작성일 2026-11-02 · 기준 브랜치 `feat/dcvc-backend-foundation` (HEAD `8bfc0fe`) · 계약 `dcvc.engine.contracts/0.1` (`PROVISIONAL`)
> 읽은 자료: `docs/engine_contracts.md`, `src/ctrec/engine/contracts.py`·`cohort_state.py`·`bundles.py`·`cost.py`·`policy.py`·`fairness.py`, `docs/dev/night_run_report.md`, `docs/dev/integration_handoff.md`, `docs/audit/02_M1_M14_gap_analysis.md`·`03_architecture_and_work_packages.md`, `tests/test_engine_*.py`, `tests/test_eval_hygiene.py`, `src/ctrec/decision_state.py`, `src/ctrec/schemas.py`(ClarifyingQuestion), 원격 `origin/integration/midterm-1026`의 `question_contract.py`·`evaluator.py`·`propositions.py`·`contract_session.py`(앞부분)
>
> 표기: **[확인]** 코드·실행으로 확인한 사실 / **[추정]** 설계 판단 / **[미확인]** 확인하지 못함
> 이 문서는 코드·기존 문서를 바꾸지 않았습니다. 권장안은 팀 승인 전 제안입니다.

## 0. 요약

| # | 질문 | 권장안 | 지금 확정? | 주 영향 |
|---|---|---|---|---|
| Q1 | PairStatus 7값·UNCERTAIN 우선순위 | 7값과 순서는 **유지**. 단 ELIGIBLE/INELIGIBLE 경계 2곳을 안전 쪽으로 고치고, 남은 작업 종류를 잃지 않게 `pending_kinds`를 **추가** | **지금** (의미 정의) | 서연(M3·M5 지표), 동겸(화면 라벨) |
| Q2 | EffortCost 축·단위 | 5개 축·단위(분, USD) **유지**. `patient_wait_minutes` 합산 의미(병렬 최댓값)를 문서로 명시, 순차 경과 시간은 실험 지표로 따로 | 축·단위는 **지금**, 대기 합산 규칙은 추후 가능 | 서연(WP6 지표), 동겸(W-5) |
| Q3 | DEFAULT_SOURCES 비용 값 | 값 자체는 **실험 파라미터**로 다룸(민감도 분석). 대신 "카탈로그 해시를 ExperimentSpec에 반드시 기록"을 지금 정함 | 값은 **추후**, 기록 규칙은 **지금** | 서연(WP2·WP6) |
| Q4 | 웹 Action→질문 변환 추가 필드 | `purpose`, `info_key`, `question_contract_id`를 기본값 있는 필드로 **추가**. 질문 문장은 계약에 넣지 않음 | `purpose`는 **지금**(안전), 나머지 추후 | 동겸(W-1·W-3) |
| Q5 | 확정→반대 확정 직접 전이 금지 | **유지**. 대신 "반대 근거로 확정이 뒤집힌 경우" 기록용 사유 코드를 정함 | **지금** | 서연(WP3·WP4), 공정현(WP7) |
| 추가 | 문서에 없던 미결정 9건 (§6) | 가장 중요: midterm 브랜치의 **명제·질문 계약·결정적 평가기**와 엔진 계약의 정렬 (A-4) | A-1·A-2·A-4는 **지금** | 전원 |

원칙 유지 확인: 아래 어떤 권장안도 (1) 임상 판정 권한을 정책 계층에 주지 않고, (2) 근거 검증 요구를 낮추지 않으며, (3) 원래 환자 ID를 계약에 넣지 않고, (4) 공유 예산(`EffortLedger`)을 웹 LLM 비용 원장과 섞지 않으며, (5) 안전 필수 행동(`safety_required`, `safety_unfunded`)을 최적화로 버리지 않습니다.

---

## Q1. `PairStatus` 7값과 UNCERTAIN 우선순위가 연구 정의(M5)와 맞는가

### 1) 현재 구현과 기존 파이프라인 동작 [확인]
- 기존 판정은 `TrialMatch.eligibility` 4값(ELIGIBLE / INELIGIBLE / UNCERTAIN / NOT_EVALUATED)이고 바뀌지 않습니다. 기준별 `gap`은 `decision_state.Gap` = none/missing/unverified/physician_review/conflict/stale (`decision_state.py:49`).
- 엔진은 `derive_pair_status(eligibility, gaps, mismatch, exhausted)`로 7값을 **파생**만 합니다 (`contracts.py`). UNCERTAIN일 때 순서: missing/stale → INFO_MISSING > unverified/conflict → VERIFICATION_PENDING > physician_review → EXPERT_REVIEW. `exhausted`는 INFO_MISSING·VERIFICATION_PENDING만 BLOCKED_FAILURE로 바꿉니다.
- `cohort_state._pair_states`는 `requirement == "required"`인 기준의 gap을 모아 넘깁니다(코호트 경로 구분 없이). `mismatch`는 `decision_state._classify_mismatch`의 none/provisional/confirmed입니다(`decision_state.py:344-365`).
- 경계값을 직접 실행해 확인한 결과(2026-11-02, 로컬 venv):

| 입력 | 결과 | 평가 |
|---|---|---|
| ELIGIBLE + {physician_review} | **CONFIRMED_ELIGIBLE** | 임상의 판단이 남았는데 확정으로 표시됨 → 안전 원칙과 어긋날 수 있음 |
| ELIGIBLE + {missing} | **CONFIRMED_ELIGIBLE** | 정보가 없는데 확정 |
| INELIGIBLE + mismatch `"none"` 또는 `None` | **CONFIRMED_INELIGIBLE** | 문서는 "자기 보고만이면 확정 아님"이라고 했지만 mismatch 정보가 없을 때 확정으로 떨어짐 |
| UNCERTAIN + {conflict, physician_review} | VERIFICATION_PENDING | 의도대로. 단 임상의 검토가 함께 남았다는 사실은 상태에서 사라짐 |

- 기존 matcher가 실제로 "ELIGIBLE + 필수 기준 gap" 조합을 만드는지는 **[미확인]** (코호트 경로가 여러 개인 시험에서 닫힌 경로의 gap이 섞이면 생길 수 있음 [추정]). 테스트 표(`tests/test_engine_contracts.py:183-196`)에는 이 조합이 없습니다.

### 2) 설계안
- **1-A 현행 유지**: 7값 + 현재 순서 + 현재 경계.
- **1-B 7값·순서 유지 + 경계 보강 + 추가 필드**: ELIGIBLE에 missing/stale → INFO_MISSING, physician_review → EXPERT_REVIEW. INELIGIBLE에 mismatch가 confirmed가 아니면 VERIFICATION_PENDING. `PairState`(또는 계약에 새 모델)에 `pending_kinds: frozenset[GapKind]`를 **추가**해 우선순위에서 밀린 작업도 남김.
- **1-C 다중 라벨**: 단일 상태 대신 (확정 여부, 남은 작업 집합)으로 표현.
- **1-D 비용 기준 순서**: "다음 행동이 가장 싼 것" 순으로 상태를 정함.

### 3) 장단점
| 안 | 장점 | 단점 |
|---|---|---|
| 1-A | 변경 없음, 46개 테스트 그대로 | 위 경계 3건이 확정 쌍 수(M3)를 과대 계산할 수 있음. 안전 원칙 위반 소지 |
| 1-B | 확정은 더 보수적(내리기만 함), 기존 테스트 표의 값은 그대로 유지됨 [추정], 정보 손실 없음 | `derive_pair_status` 의미 변경 → 승인 시점에 해야 비용이 작음 |
| 1-C | 표현력 최대 | 전이 표·지표·화면을 모두 다시 설계. M5 정의("상태 머신")와 거리가 멂 |
| 1-D | 정책 친화적 | 상태가 정보원 비용(자리표시값, Q3)에 따라 바뀜 → 판정과 정책 분리 원칙 위반 |

### 4) M1~M14 일치
- M5(검증 필요 vs 미해결 구분): 1-A·1-B 모두 7값으로 충족. 1-B가 "검증만 남음/정보 부족/전문가 대기/실패 보류" 구분을 경계까지 지킴.
- M3(정확히 확정된 적격 쌍): 확정 정의가 느슨하면 목적 함수가 부풀려짐 → 1-B가 맞음.
- M6·M12(임상의 판단 분리): ELIGIBLE+physician_review를 확정으로 두면 M12 테스트("expert 기준은 어떤 경로로도 자동 확정되지 않음")의 쌍 수준 버전이 깨짐.

### 5) 권장안: **1-B**
근거: 확정 상태는 연구 목적 함수(M3)의 분자이자 화면에서 "확정"으로 보이는 값이므로, 애매하면 확정이 아닌 쪽으로 떨어져야 합니다. 이 변경은 확정을 내리기만 하고 올리지 않습니다.

### 6) 확정 시점
**지금 확정**. `PairStatus` 의미가 바뀌면 `PairTransition` 기록·M3 지표·화면 라벨이 모두 달라지므로 서연이 지표를 만들기 전에 정해야 합니다. `pending_kinds`는 추가 필드라 나중에 넣어도 되지만 같이 넣는 편이 쌉니다.

### 7) 영향
- 서연: M3 `confirmed_correct_pairs`·M5 지표가 이 정의를 따릅니다. WP3에서 경로별 gap(열린 경로만)을 넘기는 방법을 정해야 함.
- 동겸: 화면 상태 라벨 7개 + "추가로 남은 확인" 표시(pending_kinds).
- 공정현: 경계 테스트 추가(ELIGIBLE×각 gap, INELIGIBLE×mismatch 3값).

---

## Q2. `EffortCost` 축과 단위가 실험 설계와 맞는가

### 1) 현재 구현 [확인]
- 축: `coordinator_minutes`, `clinician_minutes`, `llm_calls`, `llm_cost_usd`, `patient_wait_minutes` (모두 음수 금지).
- 합산(`__add__`): 앞의 4개는 더하고 대기는 **최댓값**. 실행 확인: 1440분 + 2880분 → 2880분.
- 예산(`EffortBudget`)·원장(`EffortLedger.fits`): 코디네이터 분, 임상의 분, (선택) LLM USD를 검사. `per_patient_floor/cap`은 **코디네이터 분만** 적용. 대기는 예산에서 빼지 않음.
- 기존 체계: `decision_state.Budget`(반복·질문·호출 수 등 무차원), `PolicyConfig.cost_*`(무차원), 웹 `web/budget.py`(LLM 토큰·달러). 엔진 축은 이들과 별개 (`docs/engine_contracts.md` §1).

### 2) 설계안
- **2-A 현행 유지**(5축, 분·USD, 대기=최댓값).
- **2-B 현행 + 의미 명시**: 대기 최댓값은 "한 단계 안에서 병렬로 보낸 요청"에만 해당한다고 문서화. 여러 단계에 걸친 경과 시간(달력 시간)은 실험 시뮬레이터(WP6)가 `elapsed_minutes` 지표로 따로 계산.
- **2-C 환자 부담 축 추가**: `patient_contacts`(질문 수·연락 횟수) 같은 축을 추가(기본 0).
- **2-D 단일 통합 비용**(가중합 1개 숫자).

### 3) 장단점
| 안 | 장점 | 단점 |
|---|---|---|
| 2-A | 변경 없음 | 대기 최댓값을 순차 단계에 잘못 쓰면 지연을 과소 평가 |
| 2-B | 계약 변경 없음, 해석 오류 방지 | 시뮬레이터가 경과 시간을 별도로 관리해야 함 |
| 2-C | 환자 부담을 공정성(M13)·비교(M10)에 쓸 수 있음 | 축이 늘어나 예산 검사 범위 결정 필요. 추가 필드라 언제든 넣을 수 있음 |
| 2-D | 최적화가 단순 | 축 분리 원칙(M2, 감사 02) 위반. 단위 혼합 |

### 4) M1~M14 일치
M2(코디네이터 능동 시간 예산)와 정확히 맞음. M13(공정성)은 지금 코디네이터 분만 환자별로 봄 → 임상의 분 배분은 관측되지 않음(§6 A-7). M9(실패·무응답) 지연은 `latency_minutes`(ActionOutcome)로 따로 남음.

### 5) 권장안: **2-B** (+ 2-C는 WP6 설계 때 필요하면 추가)
근거: 축과 단위는 이미 M2·감사 02 권고와 맞고, 바꾸는 것보다 **의미를 고정**하는 것이 중요합니다. 단위는 분(정수 아님, float)과 USD로 확정합니다.

### 6) 확정 시점
축 이름·단위는 **지금 확정**(이름 변경은 `contract_version` 상승 사유). 대기 합산 규칙과 환자 부담 축은 **추후 변경 가능**(추가·문서 변경).

### 7) 영향
- 서연: WP6 실험 결과표의 비용 열 = 이 5축 + 시뮬레이터 경과 시간.
- 동겸: W-5 그대로 — 웹 LLM 비용은 `web/budget.py`가 계속 관리하고 `EffortBudget.llm_cost_usd=None`이 기본.

---

## Q3. `InformationSource` 기본 카탈로그의 분 단위 값(자리표시값)

### 1) 현재 구현 [확인]
- `engine/bundles.py`의 `DEFAULT_SOURCES` 9개: patient_interview(5분+항목 1분, 대기 1440분), lab_report(8분, 2880분), pathology_report(10분, 4320분), medication_list(6분), medical_record(15분), clinician_review(코디네이터 2분 + 임상의 10분), system_check(0), system_reparse(LLM 1회), trial_registry(5분). 모듈 docstring에 "자리표시값"이라고 적혀 있음.
- `SourceCatalog.digest()`로 카탈로그 해시를 만들 수 있고, `ExperimentSpec.data_version.source_catalog_hash`는 **선택 필드(None 허용)** 입니다.
- 근거 문헌·실측값 **[미확인]**(저장소에 없음).

### 2) 설계안
- **3-A 문헌값으로 교체**: 코디네이터 업무 시간 연구 등 문헌에서 값 인용.
- **3-B 현장 추정(전문가 설문)**: 코디네이터·임상의에게 범위를 받음.
- **3-C 실험 파라미터화**: 값은 시나리오(낮음/기본/높음)로 두고 결과를 민감도 분석으로 보고. 카탈로그 해시를 실험마다 기록.
- **3-D 그대로 사용**.

### 3) 장단점
| 안 | 장점 | 단점 |
|---|---|---|
| 3-A | 근거 명확 | 정보원별 세분 값이 문헌에 없을 가능성 큼 [추정], 시간 소요 |
| 3-B | 현실적 | 접근성·표본 문제, IRB 필요 여부 [미확인] |
| 3-C | 지금 바로 가능, 결론이 값에 얼마나 민감한지 보여 줌 | 절대값 주장은 못 함 |
| 3-D | 없음 | 분 단위 결과에 의미 없음(야간 보고서 §6-6) |

### 4) M1~M14 일치
M7(정보원 단위 묶음)·M2(분 예산)의 수치 결과가 이 값에 직접 의존. M10(같은 조건 비교)은 **카탈로그가 같다는 것**을 증명해야 하므로 해시 기록이 필수.

### 5) 권장안: **3-C 먼저, 3-A/3-B로 기본 시나리오 보강**
근거: 정책 간 상대 비교(M10)는 값이 같기만 하면 성립하고, 절대 시간 주장은 하지 않는 것이 안전합니다. 연구 보고에는 민감도 범위를 함께 냅니다.

### 6) 확정 시점
값 자체는 **추후 변경 가능**(카탈로그는 데이터). 다만 다음 두 규칙은 **지금 정함**: (1) WP6 실험은 `source_catalog_hash`를 반드시 채움(실행기에서 강제, 계약은 그대로 둬도 됨), (2) 카탈로그에 출처 표시(예: `cost_basis: placeholder|literature|elicited`)를 추가 필드로 둘지 결정.

### 7) 영향
- 서연: WP2(정보원별 사실 배치)·WP6(시나리오·민감도). B-7.
- 동겸: 화면에 분 단위 값을 보일 경우 "추정치" 표시 필요.

---

## Q4. 웹 `QuestionChannel` 어댑터가 `Action` → 질문으로 바꿀 때 필요한 추가 필드

### 1) 현재 구현 [확인]
- 엔진 `Action`에는 질문 문장 필드가 **일부러 없음**. 있는 것: action_type, source_id, target_trial_ids, target_criteria, required_fields, evidence_fact_ids, reason_code, tier, priority, sequence, safety_required, fingerprint.
- 웹 질문 형식 `schemas.ClarifyingQuestion`: question_id, question, answer_type(yes_no/single_choice/multi_choice/number/date/free_text), options, unit, **target**, **purpose(eligibility | physician_reference)**, why.
- 기존 `decision_state.Action`에는 `info_key`, `human_readable_reason`, `tool`, `prerequisites`가 있는데, 엔진 변환(`policy._convert`)에서 **`info_key`는 옮기지 않음**(정보원 선택에만 사용).
- 원격 `origin/integration/midterm-1026`에는 **질문 계약**(`question_contract.py`)이 있음: 검수 승인된 계약의 문장·선택지를 LLM 없이 그대로 렌더하고, 답은 선택지 일치로만 명제 사실이 됨. 또한 D4 수정(`a17d868`, `61c7130`)으로 `physician_reference` 답은 근거로 쓰지 않음 [확인: 커밋 제목·모듈 docstring 기준, 동작은 실행 미확인].

### 2) 설계안
- **4-A 필드 추가 없음**: 렌더러가 target_criteria로 기준표를 찾아 모든 것을 결정.
- **4-B 최소 추가(기본값 있는 필드)**: `purpose: Literal["eligibility","physician_reference"]="eligibility"`, `info_key: str|None=None`, `question_contract_id: str|None=None`.
- **4-C 렌더링 정보까지 계약에**: answer_type·options·unit·문장까지 Action에 포함.

### 3) 장단점
| 안 | 장점 | 단점 |
|---|---|---|
| 4-A | 계약 단순 | `purpose`를 렌더러(LLM 가능)가 정하게 됨 → 답을 근거로 쓸지가 표현 계층에서 결정되는 안전 문제. 같은 info_key 질문을 다시 묶을 수 없음 |
| 4-B | 안전 관련 결정(purpose)·결정적 질문 계약 연결이 정책 쪽에 남음. 하위 호환(추가만) | 정책이 purpose를 정할 근거(기준 메타데이터) 필요 |
| 4-C | 웹 구현 쉬움 | WHAT/HOW 분리(감사 02 M14) 위반, 정책이 표현에 묶임, 문장이 감사 기록에 들어갈 위험(원칙 7) |

### 4) M1~M14 일치
M14(공통 정책 엔진, 행동 선택과 문구 분리)와 4-B가 맞음. M6·M12(임상의 판단 분리): physician_reference 질문의 답이 판정 근거로 새지 않게 하는 경계가 정책 출력에 있어야 함.

### 5) 권장안: **4-B**
- `purpose`는 정책(결정적)이 기준 메타데이터(`underspecified`/RuleMeta.review 등)로 정함 — 렌더러·LLM은 바꾸지 못함.
- `question_contract_id`가 있으면 렌더러는 `question_contract.render`만 쓰고 LLM을 부르지 않음. 없을 때만 LLM 문구 생성 허용(행동 선택에는 관여 금지, W-3).
- answer_type·unit은 렌더러가 기준표·필드 사전에서 결정적으로 고름.

### 6) 확정 시점
`purpose`는 **지금 확정**(안전 경계). `info_key`·`question_contract_id`는 midterm 통합 뒤 **추후 추가 가능**(추가 필드).

### 7) 영향
- 동겸: W-1·W-3 렌더러 설계의 입력. midterm의 `ContractSession`을 기준으로 붙여야 함(§6 A-4).
- 서연: 기준 메타데이터에서 purpose를 정하는 규칙(WP3)과 질문 계약 검수 상태.

---

## Q5. 확정 → 반대 확정 직접 전이 금지 규칙을 유지할지

### 1) 현재 구현 [확인]
- `ALLOWED_TRANSITIONS`: CONFIRMED_ELIGIBLE ↔ CONFIRMED_INELIGIBLE 직접 전이 금지. 확정에서는 VERIFICATION_PENDING / EXPERT_REVIEW / NOT_EVALUATED로만 감. 판정 권한(`authority="adjudication"`)이어도 금지(`tests/test_engine_contracts.py:166`).
- 정책 권한은 INFO_MISSING|VERIFICATION_PENDING → BLOCKED_FAILURE만.
- `DecisionRecord`는 한 기록에 쌍당 before/after 한 번만 담음. 따라서 실제로 "한 행동 결과로 확정이 뒤집힌" 경우 기록하려면 중간 상태를 거친 **두 개의 PairTransition**(또는 두 기록)이 필요함 — 어떻게 남길지 규칙이 **없음**.

### 2) 설계안
- **5-A 유지**: 직접 전이 금지. 뒤집힘은 반드시 VERIFICATION_PENDING 또는 EXPERT_REVIEW를 거침.
- **5-B 허용 + 사유 필수**: 판정 권한일 때 직접 전이를 허용하되 `reason_code`(예: `contradicting_verified_evidence`) 필수.
- **5-C 판정 계층만 허용**: 문서 검증 근거가 있을 때만 직접 전이.

### 3) 장단점
| 안 | 장점 | 단점 |
|---|---|---|
| 5-A | 상충 근거가 생기면 반드시 확인 단계를 거쳐 사람이 볼 기회가 생김(안전) | 같은 단계 안에서 뒤집힐 때 중간 상태 기록 방법을 정해야 함 |
| 5-B | 기록 단순 | 확정 뒤집힘이 조용히 일어날 수 있음 — 임상 판정 권한·근거 검증 원칙과 충돌 |
| 5-C | 근거 수준 연동 | 전이 검사에 근거 수준이 들어가 계약이 복잡해짐 |

### 4) M1~M14 일치
M4(충돌 관리)·M5(검증 필요 상태)와 5-A가 맞음. midterm의 결정적 평가기도 "처음 입력으로 내린 반대 확정과 부딪히면 상충(UNVERIFIED)으로 둔다"는 같은 방향 [확인: `contract_session.py` docstring].

### 5) 권장안: **5-A 유지 + 기록 규칙 추가**
한 행동 결과로 반대 근거가 들어오면 판정 계층은 확정 → VERIFICATION_PENDING(사유 `contradicting_evidence`)으로만 내리고, 다시 확정하는 것은 별도 확인 행동의 결과로 남깁니다. 이렇게 하면 `DecisionRecord` 구조 변경이 필요 없습니다.

### 6) 확정 시점
**지금 확정**(전이 표는 모든 기록의 검증 기준).

### 7) 영향
- 서연: WP3/WP4 재평가가 반대 확정을 만들면 VERIFICATION_PENDING으로 내려야 함.
- 공정현: WP7 Inspector에서 "확정 → 확인 대기" 전이를 강조 표시.
- 동겸: 화면에서 확정이 "확인 대기"로 바뀌는 경우 안내 문구.

---

## 6. 문서에 없던 추가 미결정 사항

| ID | 내용 | 근거 | 권장 | 시점 | 담당 |
|---|---|---|---|---|---|
| A-1 | ELIGIBLE + missing/physician_review → CONFIRMED_ELIGIBLE | [확인] 직접 실행 | Q1 1-B에 포함 | 지금 | 공정현 |
| A-2 | INELIGIBLE + mismatch none/None → CONFIRMED_INELIGIBLE | [확인] 직접 실행. `derive_pair_status`의 `mismatch` 기본값이 None | mismatch가 `confirmed`일 때만 확정 | 지금 | 공정현 |
| A-3 | 기준의 판정 방식(adjudication) 분류가 **결과에서 역추정**됨: UNKNOWN+physician_review면 expert, `record.engine`이 rule_engine이 아니면 모두 fact_llm/llm (engine 값 `review`·`conflict`·`unknown`도 llm으로 기록됨) | [확인] `cohort_state._adjudication`. `EvaluationRecord.engine` 값은 rule_engine/llm/review/conflict/unknown | 기준 성격은 기준 메타데이터(M12 `RuleMeta.adjudication`, WP3)에서 받고, engine이 review/conflict이면 decided_by를 llm으로 쓰지 않음. 그전까지 Inspector v2는 이 값을 "추정 분류"로 표시 | WP3 시작 전 | 서연(WP3), 공정현 |
| A-4 | **midterm 브랜치의 계약 재설계와 엔진 계약의 정렬**: `origin/integration/midterm-1026`에 `propositions.py`(명제 키), `question_contract.py`, `evaluator.py`(결정적 평가기, LLM 없음), `contract_session.py`(`ContractSession`), 루트 `contracts/ir/*.json`이 있음. 엔진 계약은 이를 모르고 `62f744e` 기준으로 설계됨 | [확인] 파일 존재·docstring. 동작 세부는 [미확인] | (1) `CriterionResult.decided_by`에 결정적 평가기 값(예: `contract_evaluator`) 추가 또는 rule_engine으로 매핑 규칙 결정 (2) 명제 사실 ID(`prop:<키>`)를 `evidence_fact_ids`에 그대로 쓰는지 결정 (3) 웹 연결(WP8)은 `Session`이 아니라 `ContractSession` 기준 (4) 이름 혼동 방지: "engine contracts" vs "question contract"/`contracts/` 디렉터리 — 문서에서 용어 구분 | **지금**(WP3·WP8 착수 전) | 공정현(계약), 서연(평가기 연결), 동겸(WP8) |
| A-5 | 0.1에서 `priority`·`sequence`를 추가했지만 `CONTRACT_VERSION`은 그대로이고 변경 기록이 없음 | [확인] | 승인 시 `0.2`로 올리고 `docs/engine_contracts.md`에 변경 기록 절 추가 | 승인 시 | 공정현 |
| A-6 | 엔진 `Action`에 `info_key`가 없음 → 같은 정보(예: `biomarker:EGFR`)를 묻는 행동을 시험 사이에 묶는 기존 구조가 엔진에서 사라짐(묶음은 정보원+기준 단위만) | [확인] `policy._convert` | Q4 4-B에서 `info_key` 추가 | 추후 | 동겸 |
| A-7 | 환자별 floor/cap과 공정성 지표가 **코디네이터 분만** 봄. 임상의 분 배분은 감시되지 않음 | [확인] `cost.fits`, `fairness.allocation_report` | M13 확장 때 임상의 분 열 추가(관측만) | 추후 | 동겸 |
| A-8 | `ExperimentSpec`의 `source_catalog_hash`가 선택 필드, `seed`가 정책에서 쓰이지 않음(결정적 정책) | [확인] | Q3 규칙: WP6 실행기에서 필수로 검사. seed는 ResponseModel(M9) 전용임을 문서화 | WP6 착수 시 | 서연 |
| A-9 | 웹 실행에서 `AliasMap` salt의 수명(실행별/세션별)과 저장 위치, 재접속 시 같은 환자의 별칭 유지 여부 | [미확인] 웹 코드 미검토 | 실행별 salt, 서버 메모리에만, trace에는 별칭만 — W-2/W-4와 함께 결정 | WP8 착수 시 | 동겸, 공정현 |

## 7. 승인 절차 제안
1. 이 문서의 "지금 확정" 항목(Q1, Q2 축·단위, Q3 기록 규칙, Q4 purpose, Q5, A-1, A-2, A-4)을 팀 회의에서 결정.
2. 결정 결과를 `docs/engine_contracts.md` §5에 반영하고 `CONTRACT_VERSION`을 `0.2`로, 승인되면 `CONTRACT_STATUS="APPROVED"`(별도 PR, 공정현).
3. A-1·A-2 경계 수정과 테스트는 계약 PR에 함께 포함(코드 변경은 이 검토 범위 밖이라 아직 하지 않음).
