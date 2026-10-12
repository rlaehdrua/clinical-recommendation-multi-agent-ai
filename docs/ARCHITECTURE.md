# Healthcare Agentic AI Challenge 2026
## Final Merged Architecture Context — Implementation Master

> 변경 이력: 2026-10-12 팀 결정으로 위치(location) 항목 삭제 — §8 관리 필드, §25 Core Facts, §31 Retrieval-relevant fact, §50 같은 group 내 표시 순서, §56 Freshness, Stage 5.

이 문서는 본 프로젝트의 **최종 병합 Architecture 기준**이다.

초기 설계, Master Architecture V3, 팀원 1차 검토안, 팀원 2차 코드/실행 기반 검토안을 통합한 결과다.

앞으로 코드 분석, migration, 구현, 테스트, 평가를 수행하는 AI는 **이 문서를 상위 설계 기준으로 사용한다.**

단, 별도 제공되는 **Team Decision Pending 문서의 항목은 아직 확정되지 않았다.**
해당 항목을 임의로 결정하여 구현하지 않는다.


# 1. 프로젝트 목표

본 프로젝트는 단순 Clinical Trial 검색, eligibility predictor 또는 단일 QA chatbot이 아니다.

목표는:

> 환자 상태와 임상시험 기준을 근거 기반으로 비교하고, 현재 판단에 필요한 정보가 부족하면 Agent가 다음 행동을 선택하여 필요한 정보를 능동적으로 확보하고, 검증된 상태를 갱신하여 필요한 부분만 재평가한 뒤 공개 임상시험 protocol 기준의 추천 결과에 도달하는 Interactive Multi-Agent Clinical Trial Recommendation System을 구현하는 것이다.

중앙 흐름:

```text
MATCH
→ DETECT UNCERTAINTY
→ CHOOSE ACTION
→ ACQUIRE INFORMATION
→ UPDATE VERIFIED STATE
→ RE-EVALUATE
→ RECOMMEND
```

Agentic Loop:

```text
OBSERVE STATE
→ DETERMINE ALLOWED ACTIONS
→ SELECT ACTION
→ EXECUTE
→ VALIDATE
→ TRANSITION
→ REPLAN
```

이 두 흐름이 전체 Architecture의 중심이다.


# 2. 최초 과제 요구사항

시스템은 최소 다음 기능을 실제로 수행해야 한다.

1. Patient Profile과 Clinical Trial inclusion/exclusion criteria + description 비교
2. Criterion-level matching 판단
3. 판단 근거 제시
4. 부족한 환자 정보 탐지
5. 필요한 clarification/question/action 결정
6. 사용자 답변 또는 추가 자료 획득
7. 새 정보를 반영한 재판단
8. 적절한 Trial 추천 및 이유 설명
9. Multi-Agent architecture와 orchestration
10. iterative Agentic behavior
11. 공개/허용 데이터 사용
12. source/license 공개
13. reproducible code + README + dependencies
14. medical disclaimer

평가 축:

```text
Matching Accuracy       30%
Lab Qualitative         30%
Presentation            40%
```

따라서 infrastructure 자체보다:

```text
Correct Matching
+
Useful Agentic Interaction
+
Clear Evidence/Explanation
+
Strong Demonstration
```

이 우선이다.


# 3. Architecture의 최상위 4 Layer

```text
① KNOWLEDGE LAYER
   Trial / Criteria / Medical Terminology

              ↓

② VERIFIED STATE LAYER
   Patient Facts
   Evidence
   Trial Judgments

              ↓

③ AGENTIC DECISION LOOP
   Retrieve
   Evaluate
   Detect Missing Information
   Select Action
   Acquire Information
   Update State
   Re-evaluate
   Replan

              ↓

④ RECOMMENDATION & EXPLANATION
   Trial Match State
   Remaining Uncertainty
   Evidence
   Recommendation Group
```

DB, Cache, Queue, Worker, Security 등은 Supporting Infrastructure다.

Core Agentic Loop보다 앞에 두지 않는다.


# 4. 핵심 Invariant

가장 중요한 시스템 원칙:

> **검증되지 않은 LLM output은 공식 system state를 변경할 수 없다.**

LLM은 worker다.

System authority는:

```text
Verified State
+
Validation Gate
+
Dependency Graph
+
Controlled State Transition
```

에 있다.


# 5. LLM과 Deterministic System의 역할

## Deterministic/System

가능하면 다음은 코드 또는 tool이 처리한다.

```text
numeric comparison
threshold
date
time window
unit
logic
dedup
sorting
state transition
dependency propagation
allowed actions
retry limit
fallback
stable tie-break
```

## LLM

주로 다음에 사용한다.

```text
complex natural-language parsing
unstructured patient fact extraction
residual semantic clinical reasoning
question wording
human-readable explanation
```

원칙:

> LLM이 필요 없는 작업에는 LLM을 사용하지 않는다.


# 6. OFFLINE / ONLINE 분리

## OFFLINE / PERIODIC

```text
Official Trial Source
        ↓
Raw Trial + Version
        ↓
Criterion Split
        ↓
Atomic Criterion Parse
        ↓
Parser Validation
        ↓
Logic / Cohort Compilation
        ↓
Required Facts
        ↓
Evidence Policy
        ↓
Dependency Generation
        ↓
Terminology / Embedding
        ↓
VERIFIED TRIAL STORE
```

환자마다 Trial criterion을 다시 parsing하지 않는다.


# 7. New / Changed Trial 처리

사전 compile되지 않은 Trial이 online에서 발견될 수 있다.

```text
New / Changed Trial
        ↓
COMPILATION_PENDING
        ↓
Exploration
        ↓
Parse / Validate / Compile
        ↓
VERIFIED TRIAL STORE
        ↓
Active Candidate 승격 가능
```

검증되지 않은 새 Trial을 즉시 full eligibility 판단에 사용하지 않는다.


# 8. Trial Source of Truth

기본 Trial Source of Truth:

```text
ClinicalTrials.gov
```

관리:

```text
Trial ID
title
description
condition
eligibility criteria
recruitment status
phase
intervention
age/sex
cohort
source update
```

LLM parametric knowledge를 공식 Trial evidence로 사용하지 않는다.


# 9. Description의 역할

Trial description은:

```text
Retrieval
Relevance
Context
Explanation
```

에 사용한다.

Criterion 생성 및 eligibility 판단의 Source of Truth는 explicit eligibility text다.

Description이 eligibility criterion을 임의로 override하지 못한다.


# 10. Medical Terminology Layer

의료 terminology/ontology는:

```text
synonym normalization
concept mapping
retrieval expansion
terminology interpretation
```

용도다.

반드시:

```text
synonym
broader
narrower
related
```

관계를 구분한다.

**Concept normalization은 eligibility equivalence가 아니다.**


# 11. RAW / PARSED / VERIFIED

중요한 데이터는:

```text
RAW
→ 원본

PARSED
→ AI/tool이 구조화

VERIFIED
→ Validation을 통과하여 Decision에 사용 가능
```

으로 분리한다.

Raw source를 AI 결과로 overwrite하지 않는다.


# 12. Criterion Representation

각 criterion은 atomic 구조를 갖는다.

예:

```text
criterion_id
trial_id
cohort_id

type
raw_text

structured_logic

required_facts
evaluation_mode
evidence_requirement

parser_status
```

parser_status:

```text
VERIFIED
AMBIGUOUS
UNSUPPORTED
FAILED
```


# 13. Parser Validation Gate

```text
LLM Parse
↓
Schema Validation
↓
Structural Validation
↓
Raw-text / Semantic Consistency
↓
Applicability Validation
↓
Rule Compile Validation
↓
VERIFIED
```

VERIFIED criterion만 deterministic evaluation에 사용한다.

Subjective/ambiguous criterion을 억지로 deterministic rule로 만들지 않는다.


# 14. Compound Logic

예:

```text
A AND (B OR C)
```

는 매번 LLM이 재해석하지 않는다.

```text
AND
├ A
└ OR
  ├ B
  └ C
```

형태의 Logic Graph로 관리한다.


# 15. Cohort

Trial 내부 cohort를 명시적으로 분리한다.

```text
Trial
├ Cohort A
│  └ Criteria
└ Cohort B
   └ Criteria
```

각 cohort를 독립 평가한다.

LLM이 임의로 가장 유리한 cohort를 선택하지 않는다.


# 16. Patient Fact Store

매 답변마다 전체 Patient Profile을 재추출하지 않는다.

Canonical Patient State:

```text
Patient Fact Store
```

Fact 예:

```text
fact_id
key
value
unit

observed_time
valid_time
recorded_time

source_type
source_doc_id
source_span

verification_status
version
```

새 정보는 관련 Fact만 추가/갱신한다.

"모름"은 임상 fact를 바꾸지 않는다.


# 17. Temporal Facts

의료 정보는 시간에 따라 달라질 수 있다.

예:

```text
2026-01 ECOG = 1
2026-09 ECOG = 2
```

따라서 최신값 하나만 저장하지 않는다.

Temporal Engine이 criterion의 요구 시점/기간에 적용 가능한 fact를 선택한다.

Run마다 reference time을 고정한다.


# 18. Provenance / Evidence

Fact source 예:

```text
SELF_REPORTED
DOCUMENT_EXTRACTED
DOCUMENT_VERIFIED
STRUCTURED_SOURCE
```

Evidence 판단은 단순 단일 숫자 등급보다:

```text
source_type
verification_status
recency
directness
criterion_fit
```

을 사용한다.

Criterion별 evidence requirement를 적용한다.


# 19. User Answer ≠ Verified Clinical Truth

사용자의 답변은 기본적으로:

```text
SELF_REPORTED
```

다.

문서 evidence가 필요한 criterion이라면 self-report만으로 verified clinical fact로 승격하지 않는다.

필요 시:

```text
UNKNOWN_UNVERIFIED
→ REQUEST_DOCUMENT
```

로 진행한다.


# 20. Internal Decision States

```text
MET
NOT_MET

UNKNOWN_MISSING
UNKNOWN_UNVERIFIED
UNKNOWN_AMBIGUOUS
UNKNOWN_CONFLICTING
UNKNOWN_UNINTERPRETABLE

N_A_VERIFIED
NEEDS_REVIEW
```

UI에서는 단순화할 수 있지만 내부 reason code는 유지한다.


# 21. N/A Validation

`applicable=false`만으로 N/A 처리하지 않는다.

Applicability 자체가 verified evidence로 판단되어야:

```text
N_A_VERIFIED
```

가 된다.


# 22. Criterion Evaluation Router

```text
Criterion
↓
Evaluation Router
├ Rule Engine
├ Numeric Engine
├ Temporal Engine
├ Unit Tool
├ Terminology Tool
└ Clinical Matching Agent
```

Clinical LLM은 deterministic evaluation이 불가능한 residual criterion만 처리한다.


# 23. Judgment Validation Gate

```text
Candidate Judgment
↓
Schema Check
↓
Evidence Existence
↓
Evidence Relevance
↓
Applicability Check
↓
Logic Check
↓
Contradiction Check
↓
PASS
↓
VERIFIED JUDGMENT
```

Evidence가 존재한다는 것과 criterion을 실제로 지지한다는 것을 구분한다.


# 24. Safe Failure

```text
Deterministic Evaluation
↓ impossible

LLM Reasoning
↓ validation fail

Limited Retry
↓ fail

Reason-coded UNKNOWN
or
NEEDS_REVIEW
```

금지:

```text
failure → MET
failure → NOT_MET
```

Trial evaluation 자체가 실패하면:

```text
NOT_EVALUATED
```

로 기록한다.

조용히 제거하지 않는다.


# 25. Initial Patient Information

세 층으로 본다.

## Core Facts

Retrieval seed.

예:

```text
age
sex
primary diagnosis / main symptom
```

## Known Clinical Facts

이미 알려진 임상정보.

## Adaptive Facts

현재 Candidate Trial criteria 때문에 추가로 필요한 정보.


# 26. Candidate Retrieval

```text
Patient Core Facts
↓
Cheap Hard Filter
↓
Lexical + Semantic Retrieval
↓
Retrieval Validation
↓
Candidate Pools
```

전체 Trial에 LLM matching을 하지 않는다.


# 27. Hard Filter

명확한 incompatibility만 사용한다.

예:

```text
recruitment status
clear age mismatch
clear sex mismatch
healthy-volunteer mismatch
```

복잡한 subtype/biomarker/treatment history는 너무 일찍 hard reject하지 않는다.


# 28. Retrieval Validation

```text
Retrieved Trial
↓
Retrieval Validator
↓
ACTIVE
EXPLORATION
REJECT_HARD
```

REJECT_HARD는 명확한 incompatibility에만 사용한다.

애매하면 Exploration으로 보존한다.

**Semantic Similarity ≠ Clinical Relevance**


# 29. Retrieval Provenance

Candidate마다 왜 검색되었는지 저장한다.

예:

```text
keyword match
semantic match
diagnosis match
stage match
biomarker match
retrieval score
pool assignment
```


# 30. Active / Exploration Pool

## ACTIVE

현재 높은 relevance.

Full eligibility evaluation 대상.

## EXPLORATION

가능성을 완전히 배제할 수 없으나 불확실한 Trial.

```text
Exploration
↓
Cheap Evaluation
↓
Promotion Condition
↓
Active
```

Operational budget 때문에 평가하지 못한 Trial은:

```text
NOT_EVALUATED_WITHIN_BUDGET
```

으로 구분한다.


# 31. Dynamic Retrieval

Candidate Pool은 고정되지 않는다.

```text
New Patient Fact
↓
Retrieval Trigger
↓
Retrieval-relevant?
├ NO → Partial Re-evaluation
└ YES → Re-retrieval
```

Retrieval-relevant fact 예:

```text
diagnosis
subtype
stage
biomarker
```


# 32. Dependency Graph

```text
Patient Fact
↓
Atomic Criterion
↓
Compound Logic
↓
Cohort
↓
Trial Match State
↓
Recommendation
```

Reverse traversal:

```text
Recommendation Uncertainty
↓
Trial
↓
Criterion
↓
Missing Fact
```

용도:

```text
Partial Re-evaluation
Missing Fact Analysis
Counterfactual Impact
Cache Invalidation
```


# 33. Missing Fact Analyzer

모든 UNKNOWN을 질문하지 않는다.

```text
Unknown Criteria
↓
Remove Dead Paths
↓
Extract Required Facts
↓
Merge Duplicate Facts
↓
Impact Analysis
```

이미 확실히 mismatch인 Trial의 나머지 irrelevant unknown은 질문하지 않는다.


# 34. Missing Information의 세 출처

```text
RETRIEVAL_MISSING
ELIGIBILITY_MISSING
VERIFICATION_MISSING
```

각각:

- 새로운 Trial 검색에 필요한 정보
- 현재 criterion 판단에 필요한 정보
- 알려져 있으나 evidence가 부족한 정보

를 의미한다.


# 35. Missing Fact Object

최종 개념 구조:

```text
fact_key
reason

affected_trials
affected_criteria

current_states
decision_impact

preferred_acquisition
alternative_acquisitions
required_evidence

user_answerable

attempted_actions
last_result
user_declined
unavailable

consequence_if_unresolved
```


# 36. Acquisition Alternatives

Missing Fact가 있다고 곧바로 같은 질문을 반복하지 않는다.

```text
Missing Fact
↓
Preferred Acquisition
↓
Alternative Acquisition
↓
Evidence Validation
↓
Unavailable?
├ Alternative exists → next method
├ Conditional evaluation possible → preserve uncertainty
└ No useful method → UNRESOLVABLE_CURRENTLY
```

예:

```text
ASK_USER
→ REQUEST_DOCUMENT
→ OTHER AVAILABLE EVIDENCE
→ DEFER
```

구체적인 evidence policy는 criterion에 따라 달라진다.


# 37. Acquisition Attempt State

각 Missing Fact에:

```text
attempted_actions
last_result
user_declined
unavailable
retry_allowed
```

를 저장한다.

예:

```text
EGFR

ASK_USER → 모름
REQUEST_DOCUMENT → 문서 없음

→ UNRESOLVABLE_CURRENTLY
```

같은 행동을 반복하지 않는다.


# 38. Counterfactual Decision Impact

질문의 중요도를 근거 없는 확률로 만들지 않는다.

질문:

> 이 Missing Fact의 가능한 결과 중 하나라도 현재 Trial Match 또는 중요한 Recommendation Set을 바꿀 수 있는가?

YES이면 decision-relevant다.


# 39. Decision Boundary

연속값 전체를 brute-force하지 않는다.

예:

```text
Hb >= 9
```

이면:

```text
Hb < 9
Hb >= 9
```

처럼 criterion의 decision region을 이용한다.

복잡한 semantic criterion을 억지로 boundary화하지 않는다.


# 40. Adaptive Questioning

시스템이 수학적으로 최소 질문 집합을 보장한다고 주장하지 않는다.

정확한 원칙:

> 현재 상태에서 decision-relevant한 정보를 우선적으로 소수 획득하고, 각 응답 후 필요성을 다시 계산하여 불필요한 후속 질문을 제거한다.

초기 policy는 explainable deterministic policy를 사용한다.


# 41. Action Planner

Action space:

```text
ASK_USER
REQUEST_DOCUMENT
VERIFY
RUN_TOOL
REASON
REPARSE
RETRIEVE
RESOLVE_CONFLICT
DEFER
NEEDS_REVIEW
FINALIZE
```


# 42. WHAT / HOW Separation

System이 결정:

```text
WHAT information/action is needed?
WHY?
Which Trial/Criterion is affected?
What evidence is required?
```

LLM Interaction Agent:

```text
HOW should this be expressed to the user?
```

를 담당한다.

LLM이 질문 우선순위를 자유롭게 결정하지 않는다.


# 43. Question Usability

Action 선택 시:

```text
patient answerability
medical jargon
document availability
alternative source
acquisition burden
```

을 고려한다.

환자가 알기 어려운 검사 결과는 무리하게 직접 질문하지 않고 document request가 더 적절할 수 있다.


# 44. Conflict Resolution

서로 다른 evidence를 overwrite하지 않는다.

```text
Conflicting Facts
↓
UNKNOWN_CONFLICTING
↓
Resolve if safely possible
↓
otherwise NEEDS_REVIEW
```


# 45. Partial Re-evaluation

새 Fact 하나 때문에 전체 pipeline을 재실행하지 않는다.

```text
New Fact
↓
Affected Criterion
↓
Affected Logic
↓
Affected Cohort
↓
Affected Trial
↓
Affected Recommendation
```

만 재계산한다.


# 46. Dual Stability

두 종류의 stability를 본다.

## Eligibility Stability

현재 Candidate의 중요한 match state가 추가 정보로 바뀔 수 있는가?

## Retrieval Stability

남은 retrieval-relevant 정보가 중요한 새로운 Candidate 발견에 영향을 줄 수 있는가?


# 47. STABLE_WITHIN_SEARCH_SCOPE

절대적 안정성을 주장하지 않는다.

```text
STABLE_WITHIN_SEARCH_SCOPE
```

의 의미:

> 현재 Trial DB version, search scope 및 확보된 Patient Facts 기준으로 추가 확보 가능한 정보가 현재 중요한 추천 결과를 실질적으로 변경하지 않는 상태.


# 48. Stop Conditions

다음에서 loop 종료 가능:

```text
important eligibility uncertainty resolved
important retrieval uncertainty resolved
remaining information cannot change recommendation
information unavailable
user declines/skips
NEEDS_REVIEW appropriate
operational safety cap reached
```

Max round는 semantic sufficiency가 아니다.


# 49. Trial Match 외부 표현

최종 enrollment 확정처럼 표현하지 않는다.

외부 상태:

```text
CRITERIA_MATCH
POTENTIAL_MATCH
CRITERIA_MISMATCH
INSUFFICIENT_INFORMATION
NOT_EVALUATED
NEEDS_REVIEW
```

공개 protocol 기준 사전 matching이며 최종 참여 여부는 연구기관 screening/investigator 판단임을 명시한다.


# 50. Recommendation / Ranking

Eligibility와 Ranking을 분리한다.

```text
Trial Match
↓
Recommendation Group
↓
Display Ordering
```

예:

```text
Criteria Matched
Potential Match
Additional Information Needed
```

초기 시스템에서 Trial 간 clinical superiority를 주장하지 않는다.

같은 group 내:

```text
recruitment
retrieval relevance
unresolved burden
```

등으로 설명 가능한 표시 순서만 만든다.


# 51. Policy / Impact / Orchestrator

## Policy Engine

불가능하거나 invariant를 위반하는 action을 제거한다.

## Impact Engine

각 action이 어떤 state에 영향을 줄 수 있는지 계산한다.

## Orchestrator

허용된 여러 action 중 다음 행동을 전략적으로 선택한다.

Orchestrator가 단순 fixed pipeline executor가 되어서는 안 된다.


# 52. Specialist Agents

논리적 역할:

```text
Trial Understanding Agent
Patient Understanding Agent
Clinical Matching Agent
Interaction Agent
Recommendation / Explanation Agent
Orchestrator
```

Deterministic Tools:

```text
Rule Engine
Numeric Engine
Temporal Engine
Unit Tool
Terminology Tool
Retrieval
Evidence Checker
Dependency Engine
```


# 53. Failure-aware Retry

Failure type을 분리한다.

```text
FORMAT_ERROR
VALIDATION_ERROR
EVIDENCE_ERROR
SEMANTIC_AMBIGUITY
MISSING_INFORMATION

RATE_LIMIT
TIMEOUT
PROVIDER_ERROR
```

처리:

```text
FORMAT → constrained retry
RATE_LIMIT → backoff
TIMEOUT → limited retry
MISSING_INFORMATION → no retry, acquire information
EVIDENCE_ERROR → verification
AMBIGUITY → limited alternate reasoning / NEEDS_REVIEW
```

원하는 답이 나올 때까지 반복 호출하지 않는다.


# 54. Infrastructure Failure와 Clinical Uncertainty

분리한다.

Infrastructure:

```text
429
timeout
provider
DB
queue
```

Clinical:

```text
missing
unverified
ambiguous
conflicting
uninterpretable
```

Infrastructure failure를 clinical judgment로 변환하지 않는다.


# 55. Reproducibility / Versioning

최소 추적:

```text
trial version
criterion version
knowledge version
patient fact version
model version
prompt version
reference time
run version
```

Run마다 기준 시점을 고정한다.


# 56. Freshness

```text
source_updated_at
local_synced_at
freshness_status
```

를 관리한다.

Recruitment status 등 freshness-sensitive field는 criteria보다 자주 갱신할 수 있다.


# 57. State-aware Cache

Cache key 예:

```text
patient_fact_version
trial_version
criterion_version
model_version
prompt_version
```

Dependency Graph를 cache invalidation에도 사용한다.


# 58. Multi-user / Async 확장

Core 이후 필요 시:

```text
User
↓
API
↓
Queue
↓
Worker
↓
Tools / LLM
↓
Result Store
↓
SSE / WebSocket
```

Async job은:

```text
input_state_version
```

을 가진다.

Commit 시 최신 state와 다르면:

```text
STALE_RESULT
```

로 처리한다.


# 59. Security / Privacy

Patient-specific data는:

```text
user scope
patient scope
run scope
```

로 격리한다.

Trial/public cache와 private patient cache를 분리한다.

Patient Document와 Trial text는:

```text
UNTRUSTED DATA
```

로 취급한다.

외부 문서의 instruction-like text가 Agent policy를 변경하지 못하게 한다.


# 60. Trace / Observability

일반 운영 trace와 clinical audit을 분리한다.

Operational:

```text
state ID
action
latency
status
error
```

Clinical Audit:

```text
judgment
evidence reference
state transition
```

Raw patient information을 일반 log에 과도하게 남기지 않는다.


# 61. Pipeline Error Attribution

```text
EXTRACTION_ERROR
RETRIEVAL_ERROR
RETRIEVAL_VALIDATION_ERROR
PARSING_ERROR
MATCHING_ERROR
EVIDENCE_ERROR
ORDERING_ERROR
ORCHESTRATION_ERROR
INFRASTRUCTURE_ERROR
```

로 구분한다.


# 62. Progressive UI / Decision History

사용자에게:

```text
환자 정보 확인
관련 Trial 탐색
기본 조건 분석
추가 정보 확인
추천 준비
```

등 실제 진행 상태를 보여준다.

새 evidence 때문에 Trial 상태가 바뀌었다면:

```text
Before
→ New Evidence
→ Affected Criterion
→ After
```

를 보여준다.


# 63. Evaluation

단일 최종 accuracy만 평가하지 않는다.

```text
Retrieval Recall
↓
Parser Accuracy
↓
Criterion Judgment Accuracy
↓
Evidence Grounding Accuracy
↓
Trial Match Accuracy
↓
Adaptive Interaction Efficiency
↓
Recommendation Quality
```

추가 지표:

```text
UNKNOWN Rate
NEEDS_REVIEW Rate
Question Count
Unnecessary Question Rate
Decision-changing Question Rate
Criteria Resolved / Question
Time to Stable State
LLM Calls
Tokens
Latency
Cost
Consistency
```


# 64. Gold Set

LLM-generated label만으로 최종 성능을 주장하지 않는다.

가능하면 human-reviewed Gold Set을 구축한다.

Synthetic data는:

```text
coverage
stress testing
interaction simulation
```

용도로 사용한다.


# 65. Baseline

현재 LLM-heavy sequential pipeline을 삭제하지 않는다.

Baseline으로 보존한다.

비교:

```text
Current/Baseline
vs
Verified-State Adaptive Pipeline
```

지표:

```text
accuracy
consistency
questions
LLM calls
latency
tokens
cost
failure rate
```


# 66. 현재 Repository에서 이미 적용된 안전 패치

기존 0/0.5단계에서 해결된 다음 문제는 regression 없이 보존한다.

```text
N/A evidence bypass
evaluation failure silent drop
cohort mixing
fixed reference date
lab date comparison
infant age units
negative evidence from empty fields
underspecified criteria incorrectly allowing ELIGIBLE
lab unit mismatch
all-sex criterion handling
```

기존 offline tests를 regression baseline으로 유지한다.

현재 baseline path도 비교용으로 유지한다.


# 67. 즉시 해결할 작은 안전 문제

Core migration 전에 낮은 회귀 위험으로 처리할 항목:

```text
synthetic/demo Trial 실제 추천 차단
외부 ELIGIBLE 표현 변경
clinical superiority ranking 제거
cheap hard filtering
web RUNS lifecycle 제한
```

Demo Trial은 명시적인 demo mode에서만 허용한다.


# 68. Implementation Priority

## STAGE 0 — Existing Safety Baseline

기존 안전 패치 보존.

작은 즉시 수정.

Baseline 보존.


## STAGE 1 — Verified State Foundation

```text
Decision State
Patient Fact Store
Provenance
Temporal Facts
Self-report Verification
Conflict State
Acquisition Attempts
```


## STAGE 2 — Adaptive Interaction

```text
Missing Fact Analyzer
Dead-path Removal
Duplicate Merge
Impact / Counterfactual
Policy Engine
Action Planner
WHAT/HOW Separation
Acquisition Alternatives
Guard
```


## STAGE 3 — Dependency / Re-evaluation

```text
Dependency Graph
Compound Logic
Cohort Propagation
Partial Re-evaluation
Retrieval Trigger
Stability
Core Interactive Demo
```


## STAGE 4 — Trial Reliability

```text
Parser Validation
Applicability
Evidence Relevance
Numeric/Temporal/Unit/Terminology Tools
Failure-aware Retry
Targeted Missing-rule Retry
```


## STAGE 5 — Retrieval

```text
Candidate Funnel
Hybrid Retrieval
Retrieval Validator
Active / Exploration
Retrieval Provenance
Dynamic Retrieval
Freshness
```


## STAGE 6 — Evaluation

각 Stage와 병행하되 최종적으로:

```text
Gold Set
Stage-level Metrics
Baseline Comparison
Consistency
Latency / Cost
```


## STAGE 7 — Production / Scale

```text
Privacy Hardening
Prompt Injection Boundary
Cache
Async Version Guard
Queue / Worker
Storage Lifecycle
Load Test
```


# 69. Core Demo Target

대표 Demo:

```text
Patient
54세
NSCLC
Stage IV
EGFR unknown

↓

Trial A → POTENTIAL
Trial B → POTENTIAL
Trial C → MISMATCH

↓

Missing Fact:
EGFR

↓

Action:
REQUEST_DOCUMENT

↓

Pathology Upload

↓

EGFR exon 19 deletion

↓

Validation:
VERIFIED

↓

Dependency-based Partial Reevaluation

↓

Trial A → CRITERIA_MATCH
Trial B → CRITERIA_MISMATCH

↓

Recommendation Update
```

Trace에서 반드시 보여준다.

```text
왜 정보가 필요했는가
왜 해당 action을 골랐는가
어떤 evidence가 들어왔는가
어떤 criterion이 바뀌었는가
어떤 Trial 상태가 바뀌었는가
왜 추천이 변경됐는가
```


# 70. Architecture Freeze Rule

새 아이디어를 자동으로 Core에 추가하지 않는다.

반드시 묻는다.

```text
1. 최초 요강의 어떤 요구사항을 개선하는가?
2. Core Agentic Loop 어디에 속하는가?
3. 기존 component로 해결할 수 없는가?
4. 추가 복잡도보다 measurable benefit이 큰가?
```

명확하지 않으면:

```text
ADVANCED
or
PRODUCTION BACKLOG
```

로 이동한다.


# 71. Migration Rule

현재 Repository를 한 번에 갈아엎지 않는다.

각 module을:

```text
KEEP
MODIFY
REPLACE
NEW
```

로 분류한다.

진행:

```text
Current Code Audit
↓
KEEP / MODIFY / REPLACE / NEW
↓
Dependency / Regression Analysis
↓
Stage-by-stage Migration
↓
Offline Tests
↓
Real LLM Regression
↓
Evaluation
```

명시적 구현 요청 전에는 파일 수정, dependency 설치, commit/push, 대규모 refactor를 하지 않는다.


# 72. Final Architecture

```text
══════════════ OFFLINE / PERIODIC ══════════════

Official Trial Source
        ↓
RAW + VERSION
        ↓
Criterion Split
        ↓
Structured Parse
        ↓
PARSER VALIDATION
        ↓
Logic / Cohort / Required Facts
        ↓
Evidence Policy
        ↓
Dependency / Terminology / Embedding
        ↓
VERIFIED TRIAL STORE

New/Changed Trial
        ↓
COMPILATION_PENDING
        ↓
Exploration
        ↓
Compile + Validate


════════════════ ONLINE ═════════════════

Patient Input / Document
        ↓
Patient Understanding Agent
        ↓
Fact Validation
        ↓
VERIFIED PATIENT FACT STORE
        ↓
Hard Filter
        ↓
Hybrid Retrieval
        ↓
Retrieval Validation
        ↓
ACTIVE / EXPLORATION
        ↓
Criterion Evaluation Router
   ┌────────┼──────────┐
 Rules     Tools      LLM
   └────────┼──────────┘
            ↓
    JUDGMENT VALIDATION
            ↓
       VERIFIED STATE
            ↓
       DEPENDENCY GRAPH
            ↓
      TRIAL MATCH STATES
            ↓
     MISSING FACT ANALYZER
            ↓
 Decision Boundary / Counterfactual
            ↓
         IMPACT ENGINE
            ↓
         POLICY ENGINE
            ↓
      ALLOWED ACTIONS
            ↓
        ORCHESTRATOR
            ↓
      ACTION / ACQUISITION
            ↓
   Agent / Tool / User / Document
            ↓
         VALIDATION
            ↓
      STATE TRANSITION
            ↓
    Dependency Propagation
            ↓
     PARTIAL RE-EVALUATION
            ↓
     RETRIEVAL TRIGGER
            ↓
Eligibility Stability
        +
Retrieval Stability
            ↓
STABLE_WITHIN_SEARCH_SCOPE?
       ↙             ↘
      NO              YES
      ↓                ↓
   REPLAN         MATCH GROUPS
                       ↓
                DISPLAY ORDER
                       ↓
             EVIDENCE / EXPLANATION
```


# 73. 최종 정의

본 시스템은:

> **Verified State를 중심으로 환자와 임상시험을 근거 기반으로 비교하고, 현재 판단을 바꿀 수 있는 부족정보를 탐지한 뒤 Orchestrator가 허용된 행동 중 다음 행동을 선택하여 필요한 정보를 획득하고, 검증된 상태를 갱신하여 영향받는 판단만 재평가하면서 공개 Trial protocol 기준의 추천 결과에 도달하는 Interactive Multi-Agent Clinical Trial Recommendation System**

이다.

앞으로 구현은 이 Architecture를 기준으로 수행한다.

단, 별도의 Team Decision Pending 문서에 기록된 사항은 팀 결정 전까지 임의로 확정하지 않는다.