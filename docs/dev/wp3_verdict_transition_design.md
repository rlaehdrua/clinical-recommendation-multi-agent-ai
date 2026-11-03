# WP3 확정 판정 변경 기록·질문별 출처·"모름" 처리 설계 (초안)

> 2026-11-03 · 작성: 서연 · 상태: **설계 초안, 코드 변경 없음**
> 기준: `origin/integration/dcvc` `b0a429a` (PR #1·#2 병합 뒤). 계약 0.2는 **PROVISIONAL**.
> PR #3(WP7 확정 판정 역전 지표)은 미병합이며, 이 문서는 PR #3 브랜치 `origin/feat/wp7-reversal-metrics` `80aa90b`의
> `src/ctrec/engine/reversal_metrics.py`를 읽고 썼습니다(로컬에 `gh`가 없어 `gh pr view/diff 3` 대신 브랜치를 직접 읽음.
> PR #3의 최신 커밋이 `80aa90b`가 아니면 §1.4 대응표를 다시 확인해야 함).
>
> 이 문서에서 "제안"은 아직 아무 코드에도 없는 것, "현재"는 위 커밋에 있는 것입니다.
> `engine/contracts.py`는 서연 변경 금지 파일이므로(`team_parallel_start.md` §2) 계약 변경은 모두 **추가 요청**으로만 적습니다.

---

## 0. 요약

| 항목 | 결론 |
|---|---|
| (a) 확정 판정 변경 | 계약은 이미 "확정 → 반대 확정 직행 금지"를 강제함. 빠진 것은 **사유 코드의 의미 규칙**(언제 어떤 코드가 필수인지)과 **올라갈 때의 근거 요구**. `REVERSAL_REASON_CODES`는 문서용 집합일 뿐 어디서도 검사하지 않음 |
| (b) 질문별 `source_ref` | 계약 질문 경로(`proposition_store`)는 이미 `q:r<라운드>:<질문ID>`를 남김. **기존 프로필 경로(`profile_facts`)만** 모든 답을 `raw_text:answers` 하나로 뭉침 → 여기를 질문 단위로 나눔 |
| (c) "모름" | 응답 상태와 의료 사실을 분리. "모름"은 사실 저장소에 값으로 들어가지 않음. 구동 유전자 슬롯은 `positive`/`negative`/`not_tested`(모두 사실, `not_tested`≠음성), "모름"은 응답 상태 |
| (d) 계약 0.2 의존 | 안건 1(`RuleMeta.adjudication`) 결정 전까지 정할 수 없는 부분은 §4로 분리 |
| (e) 확인 질문 | §5 (동겸 6개, PR #3 작성자 6개) |

---

## 1. (a) 확정 판정 변경 시 기록 항목

### 1.1 현재 계약 (사실 확인)

- 쌍 상태 `PairStatus` 7개 (`engine/contracts.py`):
  확정 `CONFIRMED_ELIGIBLE`(CE) · `CONFIRMED_INELIGIBLE`(CI) /
  중간 `VERIFICATION_PENDING`(VP) · `EXPERT_REVIEW`(ER) · `INFO_MISSING`(IM) · `BLOCKED_FAILURE`(BF) · `NOT_EVALUATED`(NE).
- `ALLOWED_TRANSITIONS`: CE·CI에서는 자기 자신, VP, ER, NE로만 갈 수 있음. 중간 상태 5개에서는 어디로든 갈 수 있음.
- `POLICY_TRANSITIONS`: 정책 계층은 IM→BF, VP→BF만 할 수 있음. 나머지는 모두 판정 계층(`authority="adjudication"`).
- `DecisionRecord._links`: 한 기록 안에 같은 쌍의 전이는 하나만 둘 수 있음. 한 단계 안에서 CE→VP→CI로 돌아가는 우회를 막음.
- `PairTransition.reason_code`: 형식(`^[a-z0-9_]{1,64}$`)만 검사함. 값 목록 `REVERSAL_REASON_CODES`
  (`contradicting_evidence`, `evidence_reverified`, `evidence_withdrawn`, `evaluation_failed`, `expert_review_requested`)는
  **어떤 검증기에서도 참조하지 않음**(`audit.py:59`의 주석이 유일한 참조).
- `record_producer` 진단 코드: `direct_confirmed_reversal`(차단), `transition_rejected`(차단), `unexplained_confirmed_downgrade`
  (경고: 확정에서 내려가는데 사유 없음), `withheld_pair_transition`·`withheld_criterion_change`·`withheld_outcome`(차단 단계에서 보류된 정상 변화).
  실행기가 사유를 주지 않으면 →NE일 때만 `evaluation_failed`를 자동으로 붙임.

### 1.2 `reason_code` 열거형 후보

**확정에서 내려갈 때 (CE/CI → 중간)**

| 코드 | 상태 | 뜻 | 허용 도착 상태 | 함께 남길 것 |
|---|---|---|---|---|
| `contradicting_evidence` | 현재 | 확정 뒤 반대 방향 근거가 들어옴 | VP | `facts_added`(새 사실 1개 이상), 바뀐 기준(`criteria`) |
| `evidence_withdrawn` | 현재(이름만) | 확정 근거였던 사실이 정정·대체됨 (`proposition_store.correct`의 `supersedes`) | VP | 대체된 사실 ID, 정정 사유 코드 |
| `expert_review_requested` | 현재(이름만) | 자동 판정이 임상의 판단으로 넘어감 | ER | 대상 기준 |
| `evaluation_failed` | 현재 | 재평가 자체가 실패 | NE | `outcome.error_code` |
| `answer_corrected` | **제안** | 환자가 이전 답을 스스로 고침. `evidence_withdrawn`의 하위 사례지만, 환자가 정정한 것과 문서로 반박된 것을 지표에서 나누려면 따로 둠 | VP | 정정 전후 사실 ID |
| `criterion_version_changed` | **제안** | 시험 기준 본문·IR 버전이 바뀌어 재평가함 (`criterion_version` 변경) | VP, NE | 이전·새 `criterion_version` |

**다시 확정할 때 (중간 → CE/CI)**

| 코드 | 상태 | 뜻 | 조건 |
|---|---|---|---|
| `evidence_reverified` | 현재 | 별도 근거를 확인한 뒤 확정 (반대 확정 포함) | **제안 규칙**: 직전 확정(anchor)과 반대 쪽으로 확정하면 이 코드가 필수이고, 같은 기록에 `facts_added`가 1개 이상 있거나 `VerificationEvent`(문서 검증)가 있어야 함 |
| `clinician_adjudicated` | **제안** | ER에서 임상의가 판단해 확정 | `decided_by="clinician"`인 `CriterionResult`가 있어야 함 (이미 계약이 강제하는 권한 규칙과 맞음) |
| `contradiction_resolved` | **제안** | 같은 쪽 확정으로 돌아옴 (CE→VP→CE). 반대 근거가 틀렸거나 철회됨 | 역전이 아님. PR #3에서도 사건으로 세지 않음 |

**확정과 무관 (참고)**: 정책 계층의 IM/VP→BF에는 `sources_exhausted`(이미 `PendingKind` 값)를 사유 코드로 그대로 쓰는 것을 제안합니다.
`reason_code=None`은 "확정이 걸리지 않은 일반 전이"에만 허용합니다.

> 계약 변경 요청 (공정현): ① `REVERSAL_REASON_CODES`에 제안 코드 추가, ② `PairTransition` 검증기에 "before ∈ CONFIRMED이고
> after ≠ before면 reason_code ∈ 내려갈 때 코드" 규칙 추가 여부. ②는 지금 경고(`unexplained_confirmed_downgrade`)인 것을
> 계약 위반으로 올리는 것이라 기존 기록이 깨질 수 있음 → 0.2 승인 회의에서 결정. **이 문서는 ①·② 모두 적용하지 않음.**

### 1.3 상태 전이표 (확정 상태 기준)

"현재"는 `check_transition`의 결과, "사유 규칙"은 이 문서의 제안입니다.

| from → to | 현재 | 사유 규칙 (제안) | 비고 |
|---|---|---|---|
| CE → CE, CI → CI | 허용 | 없음 | 상태 유지는 전이 기록을 만들지 않아도 됨 |
| CE → VP, CI → VP | 허용 | `contradicting_evidence` \| `evidence_withdrawn` \| `answer_corrected` \| `criterion_version_changed` 필수 | 사유가 없으면 지금은 경고만 남음 |
| CE → ER, CI → ER | 허용 | `expert_review_requested` 필수 | |
| CE → NE, CI → NE | 허용 | `evaluation_failed` \| `criterion_version_changed` | 실행기가 사유를 안 주면 `evaluation_failed`가 자동으로 붙음 |
| CE → IM, CI → IM | **금지** (`transition_rejected`) | — | 근거가 빠져도 VP를 먼저 거쳐야 함. 의도된 설계인지 §5 Q-P1 |
| CE → BF, CI → BF | **금지** (`transition_rejected`) | — | BF는 IM·VP에서만 감 |
| CE ↔ CI 직접 | **금지** (`direct_confirmed_reversal`, 차단) | — | 정상 기록이 만들어지지 않고 진단 파일에만 남음 |
| 중간 → 반대 확정 | 허용 | `evidence_reverified` 필수 + 새 근거 (§1.2) | **완료된 역전**. 지금은 사유·근거 둘 다 검사하지 않음 |
| 중간 → 같은 확정 | 허용 | `contradiction_resolved` \| `evidence_reverified` | 역전 아님 |
| ER → 확정 | 허용 | `clinician_adjudicated` (expert 기준일 때) | 안건 1 결정 후 확정 (§4) |
| IM/VP → BF | 허용 (정책만) | `sources_exhausted` | `authority="policy"` |
| NE → 아무 상태 | 허용 | 없음 (첫 평가) | anchor가 없으면 역전 판단 대상 아님 |

### 1.4 PR #3 역전 분류와 1:1 대응표

PR #3(`reversal_metrics.py`)은 **상태 이력만** 보고 `reason_code`는 읽지 않습니다. 아래 "기대 사유 연쇄"는 §1.2 규칙을 적용했을 때 각 분류에 나와야 하는 사유이고,
맞지 않으면 기록 생산 쪽(WP4 실행기)의 사유 누락으로 봅니다.

| 분류 (요청 항목) | PR #3 필드 | 상태 경로 | 기대 사유 연쇄 (제안) | 비고 |
|---|---|---|---|---|
| 적격확정 → 부적격확정 | `confirmed_eligible_to_ineligible`, `ReversalEvent.direction == "eligible_to_ineligible"` | CE → (중간 1개 이상) → CI | 내려갈 때 1.2 하강 코드 → 올라갈 때 `evidence_reverified` | 경유 유형과 관계없는 합계 |
| 부적격확정 → 적격확정 | `confirmed_ineligible_to_eligible` | CI → (중간) → CE | 같음 | |
| 검증 대기 경유 | `reversals_by_path[*]["verification_only"]` | via ⊆ {VP, ER} | `contradicting_evidence`/`evidence_withdrawn`/`answer_corrected` → VP → `evidence_reverified`, 또는 `expert_review_requested` → ER → `clinician_adjudicated` | 정상 검증 경로 |
| 정보 재확보 경유 | `reversals_by_path[*]["via_info_missing"]` | via에 IM 포함, BF·NE 없음 | 하강 코드 → VP(또는 ER) → (IM) → `evidence_reverified` | CE→IM 직행이 금지라 IM 앞에는 반드시 VP/ER/NE가 있음. 정보를 새로 받아 확정했으므로 `facts_added`가 비어 있으면 안 됨 |
| 실패·미평가 경유 | `reversals_by_path[*]["failure_or_not_evaluated"]` | via에 BF 또는 NE 포함 (우선순위 최상) | `evaluation_failed` → NE → … 또는 … → `sources_exhausted` → BF → … → `evidence_reverified` | **정상 검증 경유로 보고하지 않음**. NE를 거친 확정은 사실상 처음부터 다시 평가한 것이라 같은 근거로 반대 확정이 나왔는지 따로 봐야 함 |
| 직접 역전 계약 위반 | `direct_reversal_violations`, `direct_violations_by_direction` | CE ↔ CI 한 단계 | 해당 없음 (정상 기록 없음) | 진단 `direct_confirmed_reversal`(차단)만 셈. 정상 지표와 합치지 않음 |
| (참고) 역전 대기 | `downgraded_pending_from_*` | 이력 끝이 중간 상태 | 하강 코드만 있음 | 역전 확정 아님 |
| (참고) 미확인 역전 | `unverified_reversals`, `unverified_by_reason` (`missing_record`/`conflicting_records`) | 이력 끊김을 건너 반대 확정 | 판단 불가 | 사유 연쇄를 검사할 수 없음 |
| (참고) 새 근거 없는 역전 | `reversals_without_new_evidence` | 확인된 역전 중 도착 기록에 `facts_added` 없음 | `evidence_reverified`의 근거 요구(§1.2) **위반 후보** | PR #3에서는 정보용. 이 문서의 제안이 채택되면 위반 신호가 됨 |

대응이 비는 곳 (PR #3에 추가를 제안할지 §5에서 질문):
1. `unexplained_confirmed_downgrade`(사유 없는 하강) 경고 수를 PR #3이 세지 않음.
2. 사건(`ReversalEvent`)에 내려갈 때·올라갈 때 `reason_code`가 없음 → "사유 연쇄가 규칙과 맞는 역전 수"를 낼 수 없음.
3. `clinician_adjudicated` 같은 ER 경유 확정을 `verification_only`와 나눌지.

---

## 2. (b) `profile_facts`에 질문별 `source_ref` 추가

### 2.1 현재 정의 위치

| 대상 | 위치 | 보호 파일? |
|---|---|---|
| 프로필 → 사실 어댑터 `ingest_profile`, `_locate` | `src/ctrec/profile_facts.py` | 아님 (`PROTECTED` 목록 `tests/test_llm_pilot_exec.py:446`에 없음) |
| 호출 지점 `Session._shadow_record` | `src/ctrec/pipeline.py` (`profile_facts.ingest_profile(..., raw_text=self.case.raw_text, ...)`) | **보호** |
| 답변을 원문에 붙이는 곳 `Session.clarify` | `src/ctrec/pipeline.py` (`[추가 확인 정보 - 라운드 N]\nQ: …\nA: …`) | **보호** |
| 출처 참조 모델 `SourceReference(kind, ref, quote)` | `src/ctrec/fact_store.py` | 아님 (단, fact_store 원칙 변경은 팀 합의) |
| 원문 나누기 `split_source`, `ANSWER_MARKER` | `src/ctrec/evidence.py` | 아님 |
| 계약 답변 사실 (참고: 이미 질문 단위) | `src/ctrec/proposition_store.py` `ref = "contract:QC-…@v<n>#<digest12>;q:r<라운드>:<질문ID>"` | 아님 |

현재 `_locate`는 인용이 답변 부분에서 찾아지면 질문과 관계없이 `SourceReference(kind="answer", ref="raw_text:answers")`를 줍니다.
그래서 같은 환자에게 여러 질문을 했을 때 어느 질문의 답이 어느 사실이 되었는지, 그 질문의 `purpose`(적격성/의사 참고)가 무엇이었는지를
사실 쪽에서 알 수 없습니다. D4 수정(의사 참고용 답을 근거로 쓰지 않음)은 지금 답변 쪽(`evidence_directness`)에서 막고 있는데, 사실 쪽에서는 같은 검사를 할 수 없습니다.

### 2.2 필드 후보

| 안 | 내용 | 장점 | 단점 |
|---|---|---|---|
| **A (권장)** | 스키마는 그대로 두고 `SourceReference.ref` 문자열 형식만 정함: `qa:r<라운드>:<질문ID>`. 계약 경로의 `q:r<라운드>:<질문ID>` 뒷부분과 같은 모양 | fact_store·EvidenceRef·DecisionRecord 계약 변경 없음. 기존 `raw_text:answers`와 함께 쓸 수 있음 | 질문 목적 등은 qa_log와 조인해야 알 수 있음 |
| B | `SourceReference`에 `round: int \| None`, `question_id: str \| None` 필드 추가 (기본값 None) | 조인 없이 바로 읽힘 | fact_store 모델 변경 → 저장 파일·중복 판정 키(`evidence_key`) 영향 |
| C | `EvidenceRef`(I-2 계약)에 `source_ref` 추가 | DecisionRecord만 보고 출처 확인 가능 | `contracts.py` 변경 → 공정현 승인 필요. 값·원문을 계약에 넣지 않는 원칙과 충돌하지 않게 ID만 넣어야 함 |

`purpose`는 사실에 복사하지 않습니다. 질문 ID로 `qa_log`(`QAPair.purpose`)에서 찾습니다. 복사본이 갈라지는 것을 막기 위해서입니다.

### 2.3 질문 ID를 얻는 방법

`raw_text`의 답변 블록에는 질문 문장과 답만 있고 질문 ID가 없습니다. 방법은 두 가지입니다.

1. **qa_log를 넘김 (권장)**: `ingest_profile(..., qa_pairs: Sequence[QAPair] | None = None)` 인자를 추가하고, 인용이 들어 있는 답이
   **정확히 하나**일 때만 `qa:r<라운드>:<질문ID>`를 붙입니다. 둘 이상이거나 못 찾으면 지금처럼 `raw_text:answers`(추정하지 않음).
   - `profile_facts.py` 변경은 서연 범위. **호출하는 쪽 `pipeline.py`의 `_shadow_record`에 `qa_pairs=self.qa_log` 한 줄을 넣는 것은
     보호 파일 수정 → "공정현 승인 필요"**. 수정안:
     ```text
     # pipeline.py Session._shadow_record (보호 파일 · 공정현 승인 필요 · 적용하지 않음)
     r = profile_facts.ingest_profile(self.fact_store, self.profile.model_copy(deep=True),
                                      raw_text=self.case.raw_text, input_source=self.case.input_source,
                                      session_id=self.session_id, qa_pairs=self.qa_log)   # ← 추가
     ```
   - 기본값이 None이므로 승인 전에도 `profile_facts`만 먼저 바꿀 수 있고 동작은 그대로입니다.
2. 원문만으로 나눔: 블록의 라운드 번호와 Q/A 순서(`qa:r2:#3`)만 남김. 질문 ID가 아니라 순번이라 qa_log와 조인할 때 순서 가정이 필요함 → 권장하지 않음.

중복 판정: `evidence_key`가 `ref.ref`를 포함하므로, 같은 값을 같은 답에서 다시 추출하면 지금처럼 중복이 됩니다. 다른 질문의 답에서 같은 값이 나오면 **별개의 사실**로 남습니다(출처가 다르므로 의도한 동작).
기존에 `raw_text:answers`로 저장된 사실과 새 형식 사실은 키가 달라 한 번은 중복으로 보이지 않습니다. 이전 실행 기록과 비교할 때 주의가 필요합니다(§5 Q-D5).

### 2.4 I-5 `InformationSource`와의 관계

두 개념은 층위가 다르고 합치지 않습니다.

| | `InformationSource` (I-5) | 질문별 `source_ref` |
|---|---|---|
| 무엇 | 정보원의 **종류**(카탈로그): `source_id`, `kind`(document/patient/clinician/system), 비용, 대체 정보원 | 사실 하나가 **실제로 어느 접촉에서** 나왔는지 (인스턴스) |
| 쓰는 곳 | 정책(행동 비용·청구 키·소진) | 판정 근거 추적, 감사, D4 목적 검사 |
| 연결 | `Action.source_id` → 실행 → `ActionOutcome.new_fact_ids` | 사실의 `source_reference.ref` |

연결 규칙 (제안): `kind="answer"`인 사실은 `InformationSource.kind="patient"`인 행동의 결과에서만 나옵니다.
`ActionOutcome.new_fact_ids`에 들어간 사실의 `ref`가 `qa:r<n>:<qid>`이면, 그 질문이 그 행동에서 나왔는지(`Action.action_id` ↔ 질문 ID 매핑)를
감사에서 맞춰 볼 수 있습니다. 매핑 표를 어디에 둘지는 WP8 실행기(동겸)와 정해야 합니다(§5 Q-D4).

---

## 3. (c) "모름" 처리 원칙

### 3.1 원칙

1. **응답 상태와 의료 사실은 다른 층입니다.**
   - 응답 상태: `answered` / `unknown`("모름") / `declined`(거부) / `no_response`(무응답). 질문·행동 기록(qa_log, `ActionOutcome.status`)에 남김.
   - 의료 사실: 사실 저장소의 값. "모름"은 **사실값으로 저장하지 않습니다**.
2. "모름"은 새 근거가 아니므로 **판정 상태를 바꾸는 사유가 될 수 없습니다.** 확정 상태에서 "모름" 답 때문에 내려가는 전이는 만들지 않고,
   역전의 "새 근거"(§1.2 `evidence_reverified` 조건)로도 세지 않습니다.
3. "모름"은 그 항목에 대한 **정보원(환자) 소진 신호**일 수 있습니다. 판정이 아니라 정책 쪽 정보입니다(§3.4).

현재 상태:
- 계약 경로: `question_contract.map_answer`가 "모름·거부·미응답"을 `non_answer`로 분류하고, `PropositionLedger.record`는 `option`만 사실로 만듦 → 원칙과 맞음.
  계약 검사기는 "'모름' 같은 미응답은 선택지로 넣지 않음"을 이미 강제함(`question_contract.py:113`).
- 기존 경로: 답이 없으면 `"모름"`으로 채워(`pipeline.py` `answers.get(q.question_id, "모름")`) `A: 모름`이 원문에 붙고, 프로필을 다시 추출합니다.
  `decision_state.DECLINED_ANSWERS`, `record_producer._declined`가 같은 집합으로 무응답을 판별합니다.
  **위험**: 프로필 추출 LLM이 `A: 모름`을 값으로 해석할 수 있음(예: 바이오마커 "unknown"을 description으로 넣음). `profile_facts`는 `sex="unknown"`만 걸러 냄.
  → 제안: `profile_facts`에서 인용이 답변 블록의 "모름" 답(`DECLINED_ANSWERS`에 속함)과만 겹치는 항목은 사실로 만들지 않고 `skipped`에 사유와 함께 남김. 보호 파일 변경 없이 가능.

### 3.2 WP2b 구동 유전자 3값 (`positive` / `negative` / `not_tested`)

> 정의 출처: WP2b 슬롯 결정 `docs/dev/wp2b_slot_decisions.md` D1 (브랜치 `feat/wp2a-nsclc-trials`, 커밋 `3f03094`).
> 구동 유전자 슬롯 값은 `positive` / `negative` / `not_tested` 3개이고, "모름"은 슬롯 값이 아니라 응답 상태입니다.

| 값 | 층 | 저장 | 뜻 |
|---|---|---|---|
| `positive` | 의료 사실 | 저장 | 검사했고 해당 변이 있음 (변이 종류는 하위 값) |
| `negative` | 의료 사실 | 저장 | 검사했고 해당 변이 없음 (기존 CSV의 `none`) |
| `not_tested` | 의료 사실 | 저장 | 검사를 하지 않았다는 **사실**. 환자·기록이 그렇게 말했을 때만. **음성으로 처리하지 않음** |
| (모름) | 응답 상태 | **저장 안 함** | 슬롯 값이 아님. 환자가 검사 여부·결과를 모름 → 사실 없음 → 판정 입력에서 "정보 없음"(IM) |

핵심: `not_tested`를 `negative`로 바꾸지 않고, `not_tested`와 "모름"을 합치지도 않습니다. "검사 안 함"은 Known 계열 제외 기준을 통과시키는 근거가 될 수 있지만(검수 필요), "모름"은 아무것도 통과시키지 않습니다.
유전자별로 3값을 둘지(EGFR·ALK 각각), 구동 유전자 전체에 하나만 둘지는 WP2b D1의 남은 결정입니다.

### 3.3 "Known" vs "Absence" 근거 수준

WP2a 교차 충돌 표(`cross_trial_conflicts.md`)에 나온 두 기준 표현의 차이입니다. 근거 수준(어떤 사실이 있어야 기준 상태가 정해지는가)으로 정리하면:

| 기준 표현 (예) | `negative` | `not_tested` | "모름" (사실 없음) |
|---|---|---|---|
| 제외 "Have **known** EGFR or ALK mutations" (NCT07027514) | 제외 안 됨 | 제외 안 됨 (알려진 변이 없음) — **임상 검수 필요** | UNKNOWN → IM. "환자가 모름"은 "의료진이 알고 있는 변이 없음"과 다름 |
| 포함 "**Absence** of sensitizing EGFR mutations or ALK rearrangements" (NCT07098338 sub-study 1·2) | 충족 | **미정**: 검사 필수로 보면 NOT_MET 후보, 아니면 UNKNOWN — **임상 검수 필요** | UNKNOWN → IM |
| 포함 "**Documented** EGFR-TKI-sensitizing mutation" (NCT07680790) | 불충족 | 불충족 | UNKNOWN → IM |

- `not_tested`로 기준이 정해지는 칸은 모두 임상 검수 질문(오늘 할 일 4번)으로 넘깁니다.
- 자기 보고만으로 불충족이 되면 0.2 경계상 CI가 아니라 VP입니다(`derive_pair_status`: `mismatch != "confirmed"` → VP).

### 3.4 동겸 WP5 `answer_unknown` 항목 단위 소진과의 연결점

`answer_unknown`은 저장소 어디에도 아직 없습니다(전 브랜치 검색). 연동 지점만 남깁니다.

| 연동 지점 | 현재 | 연결 (확정 전 TODO) |
|---|---|---|
| 응답 상태 → 행동 결과 | `ActionOutcome.status` ∈ succeeded/partial/failed/no_response/not_executed. "모름"을 나타내는 값 없음 | TODO(동겸·answer_unknown): "모름"을 `no_response`로 볼지, `succeeded` + 새 사실 0으로 볼지, 새 `error_code`(예: `answer_unknown`)로 둘지 |
| 항목 단위 소진 | `derive_pair_status(..., exhausted=)`는 쌍 단위 bool. `pending_kinds`에 `sources_exhausted` | TODO(동겸·answer_unknown): 항목(info_key/기준) 단위 소진을 쌍 단위 `exhausted`로 어떻게 합칠지. 일부 항목만 소진이면 BF가 아님 |
| 대체 정보원 | `InformationSource.alternatives` | TODO: 환자 "모름" 뒤 문서 요청(`REQUEST_DOCUMENT`)으로 넘어가는 규칙. 이 경우 IM 유지, BF 아님 |
| 판정 쪽 | "모름"은 사실 0 → 재평가해도 판정 불변 | 판정 계층은 바꿀 것 없음. 단, §3.1의 `profile_facts` 걸러내기는 WP3 쪽에서 함 |
| 역전 지표 | "모름" 단계는 `facts_added` 없음 | "모름" 단계에서 확정 상태가 바뀌면 기록 생산 오류로 봄 (PR #3에 진단 추가할지 §5 Q-P5) |

---

## 4. 계약 0.2 확정 후 (안건 1 결정에 의존)

안건 1: `RuleMeta.adjudication`(rule / fact_llm / expert) 정의. 결정 전까지는 `contract_bridge.provisional_adjudication`
(underspecified → expert, structured_evaluable → rule, 그 밖 → fact_llm)을 씁니다. 아래는 그 결정이 나야 확정할 수 있습니다.

1. **ER 경유 확정의 사유 코드**: `clinician_adjudicated`가 필요한지는 expert 기준이 어떻게 정해지느냐에 달림. expert 기준이 적으면 `evidence_reverified` 하나로 충분할 수 있음.
2. **`expert_review_requested` 하강**: 판정 방식이 fact_llm → expert로 재분류되는 경우(기준 메타데이터 변경)를 `criterion_version_changed`로 볼지 `expert_review_requested`로 볼지.
3. **Known/Absence 기준의 판정 방식**: WP2a 후보에서 "Absence" 기준은 `fact_llm`, "Known" 제외는 `rule`로 적혀 있음. 안건 1 정의에 따라 `not_tested`를 규칙으로 처리할 수 있는지가 바뀜.
4. **재평가 사유 코드(`CriterionChange.reason_code`)**: 지금은 `reevaluated_by_executor` / `not_reevaluated` 두 개뿐. 판정 방식별(rule 재계산 / LLM 재질의 / 임상의) 코드를 나눌지는 안건 1 결정 뒤에.
5. (관련) 안건 4 "확정 부적격 = 문서 검증 불충족만"이 바뀌면, CI→중간 하강이 일어나는 경우의 수가 달라져 §1.3 표의 CI 행을 다시 봐야 함.

---

## 5. 확인 질문

### 동겸 (WP5 · WP8)

- **Q-D1** `answer_unknown`은 `ActionOutcome`의 어느 필드로 표현할 계획인가요? (`status="no_response"` / `succeeded` + 새 사실 0 / `error_code="answer_unknown"`)
- **Q-D2** 항목 단위 소진은 `info_key` 단위인가요, 기준(`<trial>:<rule>`) 단위인가요? 쌍 단위 `exhausted`로는 어떻게 합치나요?
- **Q-D3** "모름" 뒤 같은 항목을 다른 문장으로 다시 묻는 것을 허용하나요? 허용하면 소진 판정은 몇 번째 "모름"에서 하나요?
- **Q-D4** WP8 실행기에서 `Action.action_id` ↔ 질문 ID(`QAPair.question_id`) 매핑을 기록하나요? 질문별 `source_ref`(§2.4)와 감사에서 맞춰 볼 때 필요합니다.
- **Q-D5** 사실의 `ref` 형식이 `raw_text:answers` → `qa:r<n>:<qid>`로 바뀌면 웹 쪽(Inspector·결과 화면)에 영향이 있나요?
- **Q-D6** engine 모드에서 확정 하강 시 `pair_reasons`를 실행기가 넘기나요, 판정 계층(WP4)이 넘기나요?

### PR #3 작성자 (WP7 역전 지표)

- **Q-P1** CE/CI → IM 직행이 금지라 `via_info_missing` 사건에는 IM 앞에 항상 VP/ER/NE가 있습니다. 이 경로를 별도 유형으로 나눌 필요가 있나요, 아니면 지금처럼 IM 포함 여부만 보면 되나요?
- **Q-P2** `ReversalEvent`에 내려갈 때·올라갈 때 `reason_code`를 넣어 "사유 연쇄가 규칙과 맞는 역전 수"를 낼 수 있을까요? (§1.4 빈칸 2)
- **Q-P3** `unexplained_confirmed_downgrade` 경고 수도 요약에 넣을 수 있을까요? (§1.4 빈칸 1)
- **Q-P4** `reversals_without_new_evidence`를 정보용에서 위반 후보로 올리는 데(§1.2 `evidence_reverified` 근거 요구) 동의하나요? 새 근거를 `facts_added`만으로 볼지, 문서 검증 이벤트도 포함할지요?
- **Q-P5** "모름" 단계(`facts_added` 없음, 응답 상태 unknown)에서 확정 상태가 바뀌는 경우를 진단으로 따로 셀까요?
- **Q-P6** PR #3의 최신 커밋이 `80aa90b`인가요? (로컬에 `gh`가 없어 브랜치 `origin/feat/wp7-reversal-metrics`를 기준으로 작성함)

### 공정현 (참고 · 승인 필요 항목)

- `pipeline.py` `_shadow_record`에 `qa_pairs=self.qa_log` 추가 (§2.3) — **보호 파일, 공정현 승인 필요**.
- `REVERSAL_REASON_CODES` 확장과 하강 사유 필수화 (§1.2) — `contracts.py` 추가 요청.
