# 추가 정보 확인 API 계약 (TRIALOG O, 2026-11-07)

환자 입력 → 시험별 평가 → 부족 정보와 확인 경로 → 연구 담당자 요청 → 환자 응답(또는 '모름') → 재평가 →
쌍 상태·DCV 제안·Cohort C 순서·추천·Inspector 갱신. 백엔드만 다룹니다. 화면은 동겸 담당입니다.

- 코드: `src/ctrec/followup/` (흐름), `src/ctrec/web/followup_api.py` (HTTP), `web/app.py`는 라우터 등록 2줄만
- 테스트: `tests/test_followup_flow.py` (TestClient, 합성 NSCLC 기준표 `ctrec.sim.demo`, LLM 오프라인 대역)
- 실제 HTTP 재현: `python scripts/followup_demo_flow.py` (데모 서버를 띄우고, 중간에 두 번 강제 종료 후 재시작)
- 프런트 개발용 서버: `python scripts/followup_demo_server.py --port 8010`
  (오프라인 대역과 합성 시험만 씁니다. 실제 LLM은 호출하지 않습니다. 담당자 키는 시작할 때 출력됩니다.)

기존 `/api/runs`·`/api/cohorts`는 바꾸지 않았습니다. 새 경로는 모두 `/api/followup/` 아래에 있습니다.

## 1. 역할과 권한

| 역할 | 인증 | 할 수 있는 것 |
|---|---|---|
| 환자 | 사례를 만들 때 받는 쿠키 `ctrec_case` (HttpOnly, SameSite=Strict, Path=`/api/followup/cases/<case_id>`, 30일) | 자기 사례 보기, 자기 질문 보기·답하기 |
| 연구 담당자 | 헤더 `X-CTREC-Staff-Key` = 서버 `CTREC_STAFF_KEY`, 선택 헤더 `X-CTREC-Staff-Id` (`[A-Za-z0-9._-]{1,64}`, 기록용) | 사례 검토, 요청 만들기, 연락 불가·문서 없음·취소 표시, Inspector, 대기열 |

- 상태를 바꾸는 모든 POST에는 `X-CTREC-Request: 1`이 필요합니다. Origin 헤더가 있으면 같은 호스트여야 합니다 (CSRF 방어).
- 담당자는 환자 답변을 입력할 수 없습니다. 응답 API에 `X-CTREC-Staff-Key`가 붙어 오면 403 `staff_cannot_answer`입니다.
  환자 쿠키가 같이 와도 마찬가지입니다. 대리 기입 경로는 만들지 않았습니다.
- 없는 사례와 다른 환자의 사례는 똑같이 404로 응답합니다. 사례가 있는지 드러내지 않습니다.
- `CTREC_STAFF_KEY`가 없는 서버에서는 담당자 API가 모두 503 `staff_access_not_configured`입니다.
- 이 방식은 브라우저와 공유 키를 확인할 뿐입니다. 계정 인증을 대신하지 않습니다 (기존 `access.py`와 같은 한계).

## 2. 상태 모델

### 요청 종류와 상태

| kind | 처음 상태 | 이후 상태 | 누가 바꿈 |
|---|---|---|---|
| `ask_patient` (환자 질문) | `pending` | `answered`, `unknown`, `unreachable`, `superseded`, `cancelled` | 환자(답변·모름), 담당자(연락 불가·취소), 시스템(대상 해소) |
| `document_review` (기록 확인) | `requires_document` | `document_unavailable`, `superseded`, `cancelled` | 담당자, 시스템 |
| `conflict_resolution` (사실 충돌) | `requires_document` | `document_unavailable`, `superseded`(충돌이 사라짐), `cancelled` | 담당자, 시스템 |
| `clinician_review` (임상 검토) | `requires_clinician` | `superseded`, `cancelled` | 담당자, 시스템 |

- **"문서 확인 완료"와 "임상의 판단 완료" 상태는 없습니다.** 서버에는 문서를 확인하는 권한(`document_authority`)이
  없고, 임상의 판단을 입력받는 계약도 없습니다. 그래서 시스템은 완료 상태를 만들지 않습니다. 이 요청들은 열린 채로 남고,
  쌍은 확정되지 않습니다.
- 환자 화면에는 `ask_patient`만 보입니다. 문서 검토, 충돌 확인, 임상 검토는 담당자 내부 작업입니다.
- `unknown`('모름', '잘 모르겠어요' 등. 판별은 `engine.answers.answer_outcome` 하나): 같은 질문을 다시 만들지 않습니다.
  정책 엔진의 대체 경로가 새 요청으로 생깁니다 (`created_by: "system"`, `follows: <원래 요청>`).
  문서 정보원이 있으면 `document_review`가 생깁니다. 남은 정보원이 없으면 `clinician_review`(`reason_code: item_sources_exhausted`)가 생깁니다.
  담당자가 같은 항목의 환자 질문을 다시 만들려고 하면 409 `already_unknown`입니다.
- `unreachable`은 환자 연락 불가입니다. 정보원 전체의 실패이므로 같은 환자의 다른 `pending` 질문도 함께 `unreachable`이 되고,
  각 질문마다 대체 경로가 생깁니다.
- `document_unavailable`은 그 문서 정보원의 실패입니다. 다음 문서 정보원이 있으면 그 요청이, 없으면 임상 검토 요청이 생깁니다.
- 평가가 끝날 때마다 대상 기준이 더 이상 열려 있지 않은 요청은 `superseded`로 닫힙니다. 지난 질문은 환자에게 답할 수 있는 상태로 보이지 않습니다.

### 쌍 상태 (`pairs[*].status`, 엔진 계약 0.2 PairStatus)

`CONFIRMED_ELIGIBLE`, `CONFIRMED_INELIGIBLE`, `VERIFICATION_PENDING`, `INFO_MISSING`, `EXPERT_REVIEW`, `BLOCKED_FAILURE`, `NOT_EVALUATED`.
상태는 우선순위가 가장 높은 하나만 보여 줍니다. 남은 작업 전체는 담당자 응답의 `pending_kinds`
(`missing`, `unverified`, `conflict`, `physician_review`, `exclusion_unverified`, `sources_exhausted` …)를 보세요.
환자 답변만으로는 확정 상태가 되지 않습니다. 예를 들어 80세라는 답은 T-C를 `VERIFICATION_PENDING`(부적격·`provisional`)으로 만들 뿐이고,
확정 부적격은 아닙니다.

## 3. 엔드포인트

공통 오류 형식은 `{"detail": {"code": "<고정 코드>", "message": "<한국어 안내>"}}`입니다.
요청 형식 오류(422)만 기존 앱처럼 `{"detail": "요청 형식이 올바르지 않습니다: <필드>"}`입니다.

### 환자

| 메서드·경로 | 본문 | 성공 | 설명 |
|---|---|---|---|
| `POST /api/followup/cases` | `{"patient_text": str(1~20000), "trial_ids": [str](1~200), "input_source": "self_report"\|"clinical_record"\|"unknown"}` | 201 + 쿠키 | 초기 평가까지 끝낸 뒤 응답 (실제 LLM이면 수 분 걸릴 수 있음) |
| `GET /api/followup/cases/{case_id}` | - | 200 | 쌍 상태, 대기 질문 수, 결과 화면(`view`) |
| `GET /api/followup/cases/{case_id}/requests` | - | 200 | 내 질문 목록 (답할 수 있는 것은 `answerable: true`) |
| `POST /api/followup/cases/{case_id}/requests/{request_id}/response` | `{"answer": str(≤2000)}` 또는 `{"unknown": true}`, 선택 `client_response_id` (`[A-Za-z0-9._-]{1,64}`) | 200 | 기록 → 재평가 → 갱신된 사례 |

`view`는 `/api/runs`의 `result` SSE 이벤트와 같은 형태 `{result, report, match_note, display, trials}`입니다.
보류(conservative_view)가 적용된 결과라서 기존 결과 화면 코드를 그대로 쓸 수 있습니다.

### 연구 담당자

| 메서드·경로 | 본문 | 설명 |
|---|---|---|
| `GET /api/followup/staff/cases` | - | 사례 목록 (쌍 상태 개수, 열린 요청 수) |
| `GET /api/followup/staff/cases/{case_id}` | - | 검토 화면: `pairs`(gap 포함), `proposals`(DCV+C 순서), `held`, `requests`, `responses`, `evaluations`(변경 기록), `events`, `view` |
| `POST /api/followup/staff/cases/{case_id}/requests` | `{"proposal_ids": [str](1~50), "expected_revision": int?}` | 지금 제안에서 골라 요청 생성. 모두 검사한 뒤 만들고, 일부만 만들지 않음 |
| `POST /api/followup/staff/cases/{case_id}/requests/{request_id}/status` | `{"status": "unreachable"\|"document_unavailable"\|"cancelled"}` | 허용된 전이만 (§2) |
| `GET /api/followup/staff/cases/{case_id}/inspector` | - | Inspector 보기 (`inspector.build_view`) + `followup`(평가·요청·응답 기록) |
| `GET /api/followup/staff/queue` | - | 모든 사례를 한 코호트로 본 DCV+C 순서 (Cohort C 대기열) |

`proposals[*]`의 필드: `proposal_id`, `rank`(1부터, C 순서), `action_type`, `route`(`ask_patient`/`document_review`/
`conflict_resolution`/`clinician_review`, 기록용 행동이면 null), `source_id`, `info_key`, `target_criteria`, `target_trials`,
`reason_code`, `purpose`, `safety_required`, `requestable`, `patient_question_preview`, `request_id`·`request_status`
(이미 만든 요청이 있으면 채워짐).
`safety_required: true`(예: `VERIFY_RECORD` 사실 충돌)는 정책상 먼저 처리해야 하는 안전 확인입니다.

### 오류 코드

| HTTP | code | 언제 |
|---|---|---|
| 400 | `empty_answer`, `empty_patient_text`, `no_trials`, `bad_input_source`, `invalid_answer`(선택지 밖), `not_requestable`, `bad_staff_id` | 입력 문제 |
| 403 | (문자열 detail) | `X-CTREC-Request` 없음, 다른 출처 |
| 403 | `staff_forbidden` / `staff_cannot_answer` | 담당자 키가 틀림 / 담당자가 응답 API를 부름 |
| 404 | `case_not_found`, `request_not_found`, `trial_not_found` | 없는 ID, 다른 환자의 사례 |
| 409 | `request_closed` | 이미 처리된 질문에 다른 답 (같은 답이나 같은 `client_response_id`면 200 `duplicate: true`) |
| 409 | `proposal_not_current`, `stale_revision`, `already_answered`, `already_unknown`, `transition_not_allowed` | 화면이 오래된 상태이거나 허용되지 않는 동작 |
| 422 | `not_renderable` | 환자 질문으로 바꿀 수 없는 제안 |
| 503 | `analysis_disabled`, `staff_access_not_configured`, 예산 코드(`budget_exceeded` 등) | 실제 분석이 꺼져 있음, 담당자 설정 없음, 비용 한도 |
| 500 | `evaluation_failed` | 평가 중 오류. 오류 종류만 알리고, 변경은 저장하지 않음 (다시 보내도 안전) |

## 4. 예시 (실제 HTTP 기록, 합성 데이터, `scripts/followup_demo_flow.py`. 긴 필드는 줄임)

### 4.1 환자 입력

```http
POST /api/followup/cases
X-CTREC-Request: 1
{"patient_text": "Synthetic NSCLC patient, male, stage IV non-small cell lung cancer. ECOG 1.", "trial_ids": ["T-A", "T-B", "T-C"]}

201  Set-Cookie: ctrec_case=…; Path=/api/followup/cases/FC-8691bdcc57df0f77b27607b4073a7a41; HttpOnly; SameSite=strict
{"case_id": "FC-8691bdcc57df0f77b27607b4073a7a41", "revision": 1, "created_at": "2026-11-07T10:21:36+00:00",
 "pairs": {"T-A": {"status": "INFO_MISSING"}, "T-B": {"status": "INFO_MISSING"}, "T-C": {"status": "INFO_MISSING"}},
 "pending_requests": 0, "view": {"result": {…}, "report": null, "match_note": "…", "display": {…}, "trials": {…}}}
```

### 4.2 담당자 검토 (제안 = DCV 후보, rank = C 순서)

```http
GET /api/followup/staff/cases/FC-8691…
X-CTREC-Staff-Key: <키>
X-CTREC-Staff-Id: coord-demo

200
{"case_id": "FC-8691…", "revision": 1, "synthetic": true,
 "pairs": {"T-A": {"status": "INFO_MISSING", "eligibility": "UNCERTAIN", "open": true,
                   "gaps": ["missing", "unverified"], "pending_kinds": ["missing", "unverified"], "mismatch": "none"}, …},
 "pair_counts": {"INFO_MISSING": 3},
 "proposals": [
  {"proposal_id": "PA-4712cd13496a8079:2a1a2e2a2e8b427f", "rank": 1, "action_type": "ASK_PATIENT", "route": "ask_patient",
   "source_id": "patient_interview", "info_key": "age", "target_criteria": ["T-A:I1", "T-B:I1", "T-C:I1", "T-C:I2"],
   "target_trials": ["T-A", "T-B", "T-C"], "reason_code": "missing_required_info", "purpose": "eligibility",
   "safety_required": false, "requestable": true,
   "patient_question_preview": "다음 시험 기준에 해당하는지 확인이 필요합니다. 해당되시나요? \"Age >= 18 years; Age <= 75 years\"",
   "request_id": null, "request_status": null},
  {"proposal_id": "PA-4712cd13496a8079:9713c0d7e26727c1", "rank": 2, "info_key": "lab:hemoglobin", …},
  {"…": "biomarker:EGFR, ANC, platelets (ASK_PATIENT), ecog (REQUEST_DOCUMENT)"}],
 "held": [{"action_type": "FINALIZE", "reason_code": "evaluation_step_complete", "info_key": "finalize", "target_criteria": []}],
 "requests": [], "responses": [], "evaluations": [{"revision": 1, "trigger": {"type": "intake", "by": "patient"}, …}]}
```

### 4.3 요청 만들기 (같은 제안을 다시 보내면 `existing`)

```http
POST /api/followup/staff/cases/FC-8691…/requests
X-CTREC-Request: 1
X-CTREC-Staff-Key: <키>
{"proposal_ids": ["PA-4712cd13496a8079:2a1a2e2a2e8b427f", "PA-4712cd13496a8079:05c98c2e30cfc518",
                  "PA-4712cd13496a8079:9713c0d7e26727c1", "PA-4712cd13496a8079:f0678086892579ec"]}

200 {"created": ["FR-c54aa85d22881269", "FR-f1fe3a6cf8125d3c", "FR-9a64cd3755498bb3", "FR-c12dca248ad29a04"], "existing": [],
     "case": {…담당자 검토 형태, revision 2…}}

POST …/requests  {"proposal_ids": ["PA-4712cd13496a8079:05c98c2e30cfc518"]}
200 {"created": [], "existing": ["FR-f1fe3a6cf8125d3c"], "case": {…}}
```

### 4.4 환자 질문 목록 (서버 재시작 뒤에도 같음)

```http
GET /api/followup/cases/FC-8691…/requests

200
{"case_id": "FC-8691…", "revision": 2, "pending": 4,
 "requests": [{"request_id": "FR-c54aa85d22881269", "status": "pending", "created_at": "2026-11-07T10:21:37+00:00",
               "question": {"question": "다음 시험 기준에 해당하는지 확인이 필요합니다. 해당되시나요? \"Age >= 18 years; Age <= 75 years\"",
                            "answer_type": "free_text", "options": [], "unit": null},
               "answerable": true}, …]}
```

답한 질문에는 `"response": {"outcome": "answered", "answer": "I am 80 years old", "at": "…"}`가 붙습니다.
`answer_type`이 `yes_no`·`single_choice`·`multi_choice`이면 선택지 밖의 답은 400 `invalid_answer`입니다 (`/api/runs`와 같은 규칙).
지금 엔진 질문은 모두 `free_text`입니다.

### 4.5 환자 응답 → 재평가

```http
POST /api/followup/cases/FC-8691…/requests/FR-c54aa85d22881269/response
X-CTREC-Request: 1
{"answer": "I am 80 years old"}

200
{"duplicate": false, "request_id": "FR-c54aa85d22881269", "status": "answered", "outcome": "answered", "revision": 3,
 "conflict": false,
 "case": {"pairs": {"T-A": {"status": "INFO_MISSING"}, "T-B": {"status": "INFO_MISSING"}, "T-C": {"status": "VERIFICATION_PENDING"}},
          "pending_requests": 3, "view": {…T-C는 추천(ranked)에서 빠지고 excluded로…}}}
```

담당자 검토의 `evaluations[-1]`:

```json
{"revision": 3, "trigger": {"type": "response", "request_id": "FR-c54aa85d22881269", "outcome": "answered"},
 "transitions": [{"trial_id": "T-C", "before": "INFO_MISSING", "after": "VERIFICATION_PENDING"}],
 "criteria_changes": [{"criterion": "T-C:I2", "before": {"passes": null, …}, "after": {"passes": false, …}}, …],
 "reevaluated_trials": ["T-A", "T-B", "T-C"], "reused_trials": [],
 "impact": {"changed_fields": ["age", "other_fact"], …},
 "equivalence": {"status": "match", "compared_trials": 3},
 "proposals": […], "conflicts": [], "fact_version": "V-…"}
```

중복 처리:

```http
POST …/FR-f1fe3a6cf8125d3c/response {"answer": "Platelets 180 x10^9/L", "client_response_id": "ui-1"}   -> 200 duplicate false, revision 4
POST …/FR-f1fe3a6cf8125d3c/response {"answer": "Platelets 180 x10^9/L", "client_response_id": "ui-1"}   -> 200 duplicate true, revision 4 (다시 평가하지 않음)
POST …/FR-f1fe3a6cf8125d3c/response {"answer": "Platelets 95 x10^9/L"}                                 -> 409 request_closed
```

같은 답인지는 공백과 대소문자를 정규화한 SHA-256으로 판별합니다. 같은 `client_response_id`도 중복으로 봅니다.
네트워크 재시도를 위해 화면은 버튼을 누를 때마다 같은 `client_response_id`를 보내 주세요.

### 4.6 '모름' → 대체 경로

```http
POST …/FR-9a64cd3755498bb3/response {"unknown": true}
200 {"status": "unknown", "outcome": "unknown", "revision": 5, …}
```

이때 담당자 `requests`에 아래 요청이 추가됩니다. 환자 화면에는 보이지 않습니다.

```json
{"request_id": "FR-…", "kind": "document_review", "status": "requires_document", "created_by": "system",
 "follows": "FR-9a64cd3755498bb3", "action_type": "REQUEST_DOCUMENT", "source_id": "lab_report", "info_key": "lab:hemoglobin",
 "reason_code": "alternative_source_after_no_answer", …}
```

### 4.7 기존 사실과 충돌 → 보류

```http
POST …/FR-c12dca248ad29a04/response {"answer": "EGFR exon 19 deletion.\nMy ECOG performance status is 3 now."}
200 {"status": "answered", "conflict": true, "revision": 6, …}
```

처음 입력의 ECOG 1과 답변의 ECOG 3이 함께 남습니다. 사실 저장소는 둘 다 지우지 않고 `contradictory`로 표시합니다.
그리고 아래 요청이 생깁니다 (어느 값도 채택하지 않음).

```json
{"kind": "conflict_resolution", "status": "requires_document", "created_by": "system", "reason_code": "fact_conflict",
 "info_key": "ecog", "conflict": {"field": "ecog", "relation": "contradictory", "fact_ids": ["F-…", "F-…"]},
 "target_criteria": ["T-A:I2", "T-B:I2", "T-C:I3"]}
```

ECOG 기준이 있는 쌍은 `pending_kinds`에 `conflict`가 들어갑니다. 제안 1순위는 `VERIFY_RECORD`(`safety_required: true`)입니다.

### 4.8 대기열·Inspector

```http
GET /api/followup/staff/queue
200 {"cases": 1, "items": [{"rank": 1, "case_id": "FC-8691…", "action_type": "VERIFY_RECORD", "route": "conflict_resolution",
                            "info_key": "ecog", "safety_required": true, "request_id": null, …},
                           {"rank": 2, "action_type": "ASK_PATIENT", "info_key": "lab:absolute neutrophil count", …}, …]}

GET /api/followup/staff/cases/FC-8691…/inspector
200 {"schema": "ctrec.inspector/v1", "run": {…}, "overview": {…}, "timeline": [{"round": 0, …}, {"round": 1, …}, …],
     "changes": {"trial": [{"trial_id": "T-C", …}], …}, "facts": [{"field": "ecog", "conflict_status": "contradictory", …}],
     "followup": {"evaluations": […], "requests": […], "responses": […]}}
```

## 5. 재평가 규칙 (백엔드가 보장하는 것)

1. 답은 `Session.clarify`와 같은 경로로 들어갑니다. 질문이 이 요청인 QAPair가 만들어지고, 원문 뒤에
   `[추가 확인 정보 - 라운드 N]` 블록이 붙은 뒤 사실을 다시 추출합니다. 확인 답변에서만 확인된 사실은 사실 저장소에
   `source_type: self_report`로 기록되고, 답변 인용이 출처가 됩니다. 검증 상태는 `unknown`으로 남습니다.
2. 증분 재평가: 바뀐 사실 → 의존 그래프 영향 분석(`dependency_graph.analyze_impact`)으로 영향받는 시험만 다시 매칭합니다.
   LLM 경로로 판정한 기준은 입력 항목을 검증할 수 없어서 새 문답이 생기면 다시 평가합니다.
   그래서 합성 시험 3개에서는 대부분 3개 모두 다시 평가됩니다.
3. 전체 재평가 대조: 재추출 직후 상태를 복원한 사본에서 모든 시험을 다시 매칭해 판정과 쌍 상태를 비교합니다.
   다르면 전체 결과를 쓰고 `equivalence.status = "mismatch_full_adopted"`로 남깁니다. 증분 결과를 조용히 쓰지 않습니다.
   실제 LLM에서는 매칭 비용이 두 배입니다 (끄는 옵션은 `FollowupService(full_check=False)`, 기본은 켜짐).
4. 확정 적격은 판정 계층(conservative_view·engine)만 정합니다. 이 흐름은 판정 값을 만들거나 고치지 않습니다.
   검증된 문서 없이 `CONFIRMED_ELIGIBLE`이 되는 경로가 없고, 테스트는 모든 평가에서 이것을 확인합니다.
5. 실패하면 저장하지 않습니다. 평가는 복원한 사본에서 하고 마지막에 한 번만 저장합니다 (원자적 쓰기, 개정 번호 확인).

## 6. 저장·실행

- 위치: `CTREC_FOLLOWUP_DIR` (기본 `outputs/followup/`, git 제외), 사례마다 `FC-<32hex>.json` 파일 하나입니다.
  환자 입력 원문, 답변, 사실 이력이 들어 있습니다. 운영 배포에서는 접근이 제한된 위치를 지정해야 합니다.
  보관 기간과 삭제 정책은 아직 없습니다 (후속).
- 서버를 재시작해도 이어집니다. `scripts/followup_demo_flow.py`는 요청 생성 뒤와 응답 뒤에 서버 프로세스를 강제 종료(kill)하고 다시 띄워
  `revision`, `requests`, `responses`, `evaluations`, `pairs`, `proposals`가 그대로인지 확인합니다.
- 한 프로세스만 지원합니다. 같은 디렉터리를 여러 프로세스(워커)가 쓰는 배포는 아직 안전하지 않습니다.
  저장할 때 개정 번호가 다르면 거절(`StaleWrite` → 500)하지만, 프로세스 사이의 잠금은 없습니다.
- 실제 LLM: `/api/runs`와 같은 허가(`paid_gate`)를 거칩니다. `CTREC_WEB_METERING=enforce`이면 평가 한 번이 실행 한 번처럼
  예산(`web/budget.py`)을 예약·정산합니다. 초기 평가와 응답 한 번의 재평가는 HTTP 요청 안에서 동기로 돕니다.
  실제 LLM에서는 수 분이 걸릴 수 있으므로 화면은 긴 대기 표시가 필요합니다. 비동기 작업과 진행 이벤트는 아직 없습니다.

## 7. 동겸 담당 API와의 경계

- 새 경로는 `/api/followup/*`뿐입니다. 기존 `/api/runs*`, `/api/cohorts*`, `/api/patients`, `/api/trials`, `/api/config`의
  동작과 응답은 그대로입니다. `app.py`에 바뀐 것은 import 한 줄과 `include_router` 한 줄입니다.
- 원격 브랜치 기준(2026-11-07 19시)으로 integration 대비 `src/ctrec/web/`을 바꾼 다른 브랜치는 없었습니다. 동겸이
  로컬에서 만드는 API와 겹치는지는 확인할 수 없었으므로, 같은 접두사(`/api/followup`)를 쓰는지만 확인해 주세요.
- 쿠키 이름 `ctrec_case`는 새 이름입니다. 기존 `ctrec_run`·`ctrec_client`와 경로가 겹치지 않습니다.

## 8. 하지 않은 것 (후속)

- 임상의 판단 입력 계약, 문서 업로드·확인(`DocumentAuthority` 운영 어댑터): 없음. 그래서 `requires_document`·`requires_clinician`은
  닫히지 않습니다.
- 환자가 다른 기기에서 다시 들어오는 경로 (쿠키를 잃으면 사례에 접근할 수 없음. 재발급은 담당자 기능으로 따로 설계 필요)
- 담당자 계정·권한 분리 (공유 키 하나), 보관 기간·삭제, 여러 프로세스 잠금, 비동기 평가·진행 이벤트
- 질문 문구: 엔진 질문은 기준 원문(영문)을 그대로 보여 주는 `free_text`입니다 (`engine_runner._wording`). 환자용 문구 개선은 질문 계약(P3) 쪽 작업입니다.
