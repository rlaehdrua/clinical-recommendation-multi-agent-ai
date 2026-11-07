# 문서 검증·임상의 판단 완료 API 계약 (TRIALOG O 후속)

대상: 프런트 연결(동겸). 기준: PR #35 O Follow-up API(`/api/followup/*`, [followup_api_contract.md](followup_api_contract.md))와
현재 D+E 안전성 계약. 코드: `src/ctrec/web/followup_api.py`(HTTP), `src/ctrec/followup/service.py`(흐름),
`src/ctrec/followup/verification.py`(권한·문서·감사), `src/ctrec/clinician_authority.py`, `src/ctrec/expert_boundary.py`,
`src/ctrec/evidence_basis.py`(판정 계층).

> **운영 검증은 완성되지 않았습니다.** 실제 의료기관의 사용자 인증·권한 시스템과 연결된 권한이 없습니다.
> 지금 동작하는 검증자·임상의 권한은 **합성 시험 전용**(`CTREC_VERIFICATION_MODE=synthetic`)이며 합성 사례에서만 동작합니다.
> 운영 설정(모드 없음)이나 `production` 모드에서는 이 문서의 완료 API가 모두 **503**이고, 문서 검토·임상 검토 요청은 열린 채로
> 남으며 어떤 쌍도 문서·임상의 근거로 확정되지 않습니다.

## 1. 역할과 권한

| 역할 | 헤더 | 서버 설정 | 할 수 있는 일 |
|---|---|---|---|
| 환자 | 쿠키 `ctrec_case` | (사례 생성 때 발급) | 질문 확인·답변 (O 그대로) |
| 연구 담당자 | `X-CTREC-Staff-Key`, `X-CTREC-Staff-Id` | `CTREC_STAFF_KEY` | 요청 생성, 상태 표시(연락 불가·문서 없음·취소), **문서 등록**, 검증 요약·감사 기록 조회 |
| 문서 검증자 | `X-CTREC-Verifier-Key`, `X-CTREC-Verifier-Id` | `CTREC_SYNTHETIC_DOC_VERIFIER_KEY` | 문서 내용 조회, **문서 검토 요청 완료(증명 발급)** |
| 임상의 | `X-CTREC-Clinician-Key`, `X-CTREC-Clinician-Id` | `CTREC_SYNTHETIC_CLINICIAN_KEY` | **임상 검토 요청 완료(판단 기록)** |

- 서명 비밀 `CTREC_SYNTHETIC_SIGNING_SECRET`은 요청으로 받지 않습니다. 증명·판단의 HMAC 서명에만 씁니다.
- 세 키와 서명 비밀은 16자 이상이어야 하고, 서로 달라야 하며, `CTREC_STAFF_KEY`와도 달라야 합니다. 어기면 `503 verification_misconfigured`가 납니다.
- 역할은 겹치지 않습니다. 연구 담당자 키로 검증하거나, 검증자 키로 임상 판단을 하거나, 그 반대로 하면 403입니다.
  한 요청에 두 역할의 헤더를 함께 보내면 `400 multiple_roles`입니다.
- 환자 답변 대리 입력은 금지됩니다(O 계약 유지). 응답 API에 연구 담당자·검증자·임상의 헤더 중 하나라도 있으면
  `403 staff_cannot_answer`입니다. 검증자·임상의에게는 환자 답을 바꾸는 경로가 없습니다.
- 키는 "이 키를 가진 쪽"만 확인합니다. 사람 계정 인증이 아닙니다. `X-...-Id`는 기록용 이름이고 신원 증명이 아닙니다(§8).
- 상태를 바꾸는 POST는 O와 같이 `X-CTREC-Request: 1`이 필요하고 같은 출처여야 합니다(CSRF).

## 2. 상태 모델 (O에 추가된 것)

| kind | 처음 | 완료 | 누가 완료 |
|---|---|---|---|
| `document_review` | `requires_document` | `document_verified` | 문서 검증자 (`POST .../verification`) |
| `clinician_review` | `requires_clinician` | `clinician_decided` | 임상의 (`POST .../judgment`) |
| `conflict_resolution` | `requires_document` | (완료 없음. 충돌이 사라지면 `superseded`) | 문서 검증으로 닫지 않음 |

- 완료 상태는 **요청의 운영 상태일 뿐 판정 근거가 아닙니다.** 판정은 사실 이력의 서명된 증명(`ServerAttestation`)과
  판단 기록(`ClinicianJudgment`)을 **현재 서버 권한이 인정할 때만** 바뀝니다. 상태 문자열은 남아 있는데 권한이 바뀌면
  (서명 비밀이 다르거나, 모드가 꺼졌거나, 운영 모드이면) 기준은 다시 보류됩니다. 이때 화면은 `authority_changed: true`를 받습니다(§5).
- 연구 담당자 상태 변경 API(`POST /staff/.../status`)는 `unreachable|document_unavailable|cancelled`만 받습니다.
  `document_verified`·`clinician_decided`를 보내면 422입니다.
- 임상의 판단 기준(`expert_boundary`: 원문에 구체 기준이 없는 `underspecified` 기준, 또는 등록 목록 `EXPERT_RULES`)이
  `EXPERT_REVIEW`로 남으면, 서버가 기준마다 `clinician_review` 요청을 하나씩 엽니다. 이때 `created_by: "system"`,
  `reason_code: "clinician_judgment_required"`입니다. 정책 엔진은 이 기준에 판정용 제안을 내지 않기 때문입니다.
- 출처가 모두 소진되어 생긴 `clinician_review`(`item_sources_exhausted`, `conflict_unresolved`)는 대상이 검사 수치처럼
  임상의 판단 기준이 아닌 기준입니다. 임상의 판단으로 닫을 수 없고(`judgeable: false`, 409 `not_clinician_criterion`),
  담당자가 취소하거나 열린 채로 둡니다.

## 3. 흐름

```
환자 답변(자기 보고) ─► 기준 UNVERIFIED (VERIFICATION_PENDING) ─► 정책 제안 REQUEST_DOCUMENT
담당자: POST /staff/cases/{id}/requests  (그 제안으로 document_review 생성)
담당자: POST /staff/cases/{id}/documents (문서 등록, 서버가 SHA-256 보관)
검증자: GET  /verifier/queue · /verifier/cases/{id} · .../documents/{doc}/content
검증자: POST /verifier/cases/{id}/requests/{rid}/verification
        ─► 증명 발급(FactStore.attest) ─► 영향받은 기준 재평가(프로필 재추출 없음, 전체 재평가 대조)
        ─► 쌍 상태·추천·Inspector·Cohort C 대기열 갱신, 요청 document_verified
EXPERT_REVIEW 기준 ─► 서버가 clinician_review 생성
임상의: GET  /clinician/queue · /clinician/cases/{id}
임상의: POST /clinician/cases/{id}/requests/{rid}/judgment  ─► 서명된 판단 ─► 재평가 ─► clinician_decided
```

확정 적격(`CONFIRMED_ELIGIBLE`)이 되는 조건은 기존 판정 계층과 같습니다. 판정에 적용된 필수 기준이 모두 충족이어야 하고,
남은 gap이 없어야 합니다. 진료 기록이 필요한 기준(`required_evidence: clinical_record`)은 근거 사실이 모두 문서로 확인돼야 합니다.
예: 합성 시험 T-A는 혈소판·ANC·ECOG 세 기준이 모두 문서로 확인돼야 확정되고, 하나만 확인되면 `VERIFICATION_PENDING`에 머뭅니다.

## 4. 엔드포인트

모든 경로의 접두사는 `/api/followup`입니다. 응답 오류 형식은 O와 같은 `{"detail": {"code": "...", "message": "..."}}`입니다.

### 4.1 문서 등록 (연구 담당자)

`POST /staff/cases/{case_id}/documents` → 201

```json
{"kind": "lab_report", "media_type": "text/plain", "content_base64": "U1lOVEhFVElD...", "expected_revision": 6}
```
- `kind`: `lab_report | pathology_report | genomic_report | imaging_report | clinical_note | other`
- `media_type`: `application/pdf | image/png | image/jpeg | text/plain`. 최대 2 MB입니다.
- 합성 사례에서만 됩니다. 실제 시험 저장소 사례는 `403 synthetic_authority_real_case`이고, 검증 모드가 없으면 `503`입니다.
- 등록만으로는 판정이 바뀌지 않습니다.

응답: `{"document": {"document_id": "DOC-5c99a45c96ba23f8", "sha256": "57fc02…", "size": 87, "media_type": "text/plain",
"kind": "lab_report", "registered_by": "staff:coord-demo", "registered_at": "…"}, "case": <staff case view>}`

### 4.2 검증자 화면

- `GET /verifier/queue`: `{"items": [{"case_id", "synthetic", "request_id", "info_key", "target_criteria", "reason_code", "created_at"}]}`
  (열린 `document_review` 요청만 나옵니다)
- `GET /verifier/cases/{case_id}`:
```json
{"case_id": "FC-…", "revision": 6, "synthetic": true,
 "documents": [{"document_id": "DOC-…", "sha256": "…", "kind": "lab_report", "media_type": "text/plain", "size": 87, …}],
 "requests": [{"request_id": "FR-…", "kind": "document_review", "status": "requires_document", "verifiable": true,
               "info_key": "lab:platelets", "source_id": "lab_report", "target_criteria": ["T-A:I3"],
               "criteria": [{"criterion": "T-A:I3", "kind": "inclusion", "text": "Platelets >= 100 x10^9/L",
                             "clinician_reason": null, "required_evidence": "clinical_record"}],
               "allowed_fields": ["lab:platelets"],
               "facts": [{"fact_id": "F-…", "field": "lab:platelets", "value": 180.0, "unit": "x10^9/l",
                          "observed_at": null, "source_type": "self_report",
                          "source_reference": {"kind": "answer", "ref": "q:r1:FR-…", "quote": "Platelets 180 x10^9/L"},
                          "verification_status": "unknown", "document_verified": false, "conflict_status": "none"}]}],
 "verifications": []}
```
  - `allowed_fields`: 이 요청으로 검증할 수 있는 항목입니다. 범위 밖 항목은 `409 field_out_of_scope`입니다.
  - `document_verified`: **현재 권한이 인정하는** 문서 확인입니다. `verification_status` 문자열과 다를 수 있습니다.
  - `conflict_resolution` 요청도 보이지만 `verifiable: false`입니다.
- `GET /verifier/cases/{case_id}/documents/{document_id}/content`: 원본 바이트를 돌려줍니다. 헤더는 `Content-Disposition: attachment`,
  `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, `X-Document-SHA256`입니다. 저장된 내용의 해시가 다르면 `409 document_integrity`입니다.

### 4.3 문서 검증 완료 (검증자)

`POST /verifier/cases/{case_id}/requests/{request_id}/verification` → 200

```json
{"document_id": "DOC-5c99a45c96ba23f8",
 "document_sha256": "57fc0211e6ae50484a61ad1da171e7a629167723cf39fd40909f9f4d312a324e",
 "items": [{"fact_id": "F-6a8231007b2c"}],
 "expected_revision": 6}
```
- `document_sha256`: 검증자가 본 문서의 해시입니다. 등록 기록과 다르면 `409 document_changed`입니다.
- `items` (1~20개)는 두 형태 중 하나입니다.
  - `{"fact_id": "F-…"}`: 기존 사실의 값이 문서와 같다는 확인(`confirm`)입니다. 이미 문서로 확인된 사실이면 `409 already_verified`입니다.
  - `{"field": "lab:absolute neutrophil count", "value": 2.1, "unit": "x10^9/L", "observed_at": "2026-10-01"}`: 문서의 값을
    새 사실로 기록합니다(`record`). 출처는 `clinical_record`, 참조는 이 문서입니다. 값이 환자 답과 **다르면** 같은 항목의
    충돌이 되어 보류되고 `conflict_resolution` 요청이 생깁니다. 어느 값도 채택하지 않습니다.
- 모든 항목을 먼저 검사합니다. 하나라도 틀리면 아무것도 기록하지 않습니다.
- LLM 판정 기준이 있는 시험도 다시 매칭하므로 O와 같은 실제 LLM 허가가 필요합니다(`_llm_ready`, 아니면 `503 analysis_disabled`).

응답:
```json
{"verification": {"verification_id": "DV-c95404924a929ae3", "request_id": "FR-…", "document_id": "DOC-…",
                  "document_sha256": "…", "verifier": "synthetic-document-verifier:verifier-demo",
                  "by": "verifier:verifier-demo", "at": "…",
                  "items": [{"fact_id": "F-6a8231007b2c", "field": "lab:platelets", "mode": "confirm"}],
                  "revision_after": 7, "authority": "synthetic"},
 "request": {"request_id": "FR-…", "kind": "document_review", "status": "document_verified", "history": [ … ], …},
 "transitions": [{"trial_id": "T-A", "before": "VERIFICATION_PENDING", "after": "CONFIRMED_ELIGIBLE"}],
 "revision": 9,
 "pairs": {"T-A": {"status": "CONFIRMED_ELIGIBLE", "eligibility": "ELIGIBLE", "gaps": [], …}}}
```
`transitions`는 쌍 상태가 바뀐 시험만 담습니다(첫 검증 한 건만으로는 빈 목록일 수 있음).

### 4.4 임상의 화면

- `GET /clinician/queue`: 열린 `clinician_review`만 나옵니다(형식은 검증자 대기열과 같음).
- `GET /clinician/cases/{case_id}`:
```json
{"case_id": "FC-…", "revision": 9, "synthetic": true, "pairs": { … },
 "requests": [{"request_id": "FR-9ce7…", "status": "requires_clinician", "reason_code": "clinician_judgment_required",
               "follows": null, "judgeable": true,
               "criteria": [{"criterion": "T-D:I2", "kind": "inclusion",
                             "text": "Life expectancy of at least 12 weeks in the opinion of the investigator",
                             "clinician_reason": "underspecified", "required_evidence": "clinical_record"}]}],
 "facts": [{"fact_id": "F-c3ab962c169e", "field": "ecog", "value": 1, "source_type": "unknown",
            "document_verified": true, …}],
 "judgments": [{"judgment_id": "CJ-…", "decision": "MET", …, "recognition": "recognized"}]}
```
  - `judgeable`: 모든 대상 기준이 임상의 판단 기준이고 요청이 열려 있으면 true입니다.
  - `judgments[].recognition`은 세 값 중 하나입니다.
    - `recognized`: 지금 판정에 쓰이는 판단입니다.
    - `stale`: 판단 뒤 환자 사실이나 기준이 바뀌어 인정되지 않습니다.
    - `invalid`/`not_recognized`: 서명이 맞지 않거나 권한이 없습니다.

### 4.5 임상의 판단 완료 (임상의)

`POST /clinician/cases/{case_id}/requests/{request_id}/judgment` → 200

```json
{"decisions": [{"criterion": "T-D:I2", "decision": "MET", "rationale_code": "life_expectancy_adequate",
                "basis_fact_ids": ["F-c3ab962c169e"]}],
 "expected_revision": 9}
```
- 요청의 **모든** 대상 기준을 한 번씩 판단해야 합니다. 빠지거나 넘치면 `409 judgment_incomplete`입니다.
- `decision`은 기준 문장이 성립하는가입니다(`MET`/`NOT_MET`). 제외 기준이면 `MET`이 불충족(부적격 쪽)이 됩니다.
- `rationale_code`: 영문 소문자·숫자·`_` 1~64자입니다. 자유 서술 칸은 두지 않았습니다(§8).
- `basis_fact_ids`: 판단 근거 사실입니다. 기준이 `required_evidence: clinical_record`이면 하나 이상 필요하고,
  **모두 지금 문서로 확인된 사실**이어야 합니다. 아니면 `409 basis_not_document_verified`입니다. 임상의 판단은 문서 확인
  요건을 대신하지 않습니다. 합성 T-D(범주 other → clinical_record)는 먼저 ECOG를 문서로 확인한 뒤 그 사실을 근거로 판단했습니다.
- 임상의 판단 기준이 아니면 `409 not_clinician_criterion`입니다.
- 판단은 그때의 사실 버전(`fact_version`)과 기준 버전(`criterion_version`)에 묶입니다. **이후 환자 사실이 하나라도 바뀌면**
  (답변, 문서 확인 포함) 판단은 `stale`이 됩니다. 기준은 다시 `EXPERT_REVIEW`로 돌아가고 새 `clinician_review` 요청이 생깁니다.
  보수적인 선택입니다. 순서는 **문서 확인 먼저, 임상의 판단 나중**을 권장합니다.

응답: `{"judgments": [{"judgment_id": "CJ-…", "trial_id": "T-D", "rule_id": "I2", "decision": "MET",
"criterion_version": "…", "fact_version": "V-…", "clinician": "synthetic-clinician:dr-demo", "rationale_code": "…",
"basis_fact_ids": ["F-…"], "at": "…", "signature": "…"}], "request": {… "status": "clinician_decided"},
"transitions": [{"trial_id": "T-D", "before": "EXPERT_REVIEW", "after": "CONFIRMED_ELIGIBLE"}], "revision": 10, "pairs": {…}}`

### 4.6 연구 담당자 추가 보기

- `GET /staff/cases/{case_id}`(O)에 `documents`, `verifications`, `judgments`, `authority_changed` 필드가 추가됐습니다.
- `GET /staff/cases/{case_id}/verification`:
  `{"authority": {"mode": "synthetic"|"none"|"production", "active": bool, "synthetic_only": true, "production_authority": false,
  "problems": [...]}, "documents", "verifications", "judgments"(+recognition), "document_verified_facts": [fact_id],
  "verification_status_only_facts": [fact_id]}`. 마지막 필드는 상태 문자열만 남고 지금 권한이 인정하지 않는 사실입니다.
- `GET /staff/cases/{case_id}/inspector`(O)에 `verification` 절(위와 같은 내용)이 추가됐습니다.
- `GET /staff/cases/{case_id}/audit`: `{"intact": bool, "entries": [...], "head" | "broken_at"}`(§6)

환자 화면(`GET /cases/{id}`)은 쌍 상태만 보여 줍니다. 문서·검증자·판단 정보는 넣지 않습니다.

## 5. 재시작·권한 변경

- 사례 JSON에는 문서 메타데이터, 증명(사실 이력 안), 판단 기록, 이벤트가 들어 있습니다. 문서 파일은 `<CTREC_FOLLOWUP_DIR>/documents/<case>/<doc>`,
  감사 기록은 `<CTREC_FOLLOWUP_DIR>/audit/<case>.jsonl`에 있습니다. 재시작 뒤에도 그대로 이어집니다(테스트·실제 프로세스 재시작으로 확인).
- 같은 서명 비밀로 재시작하면 증명·판단이 그대로 인정되고 쌍 상태도 같습니다.
- 서명 비밀이 다르거나 모드가 꺼졌거나 운영 모드이면 저장된 증명·판단이 인정되지 않습니다. 사례 보기 API는 쌍 상태를 지금 권한으로
  다시 계산합니다(`refresh_pairs`). 그 결과 `authority_changed: true`와 `stored_pairs`(저장 당시 값)를 함께 줍니다.
  **`current.view`(추천 결과)는 저장 당시 것입니다.** `authority_changed`가 true이면 추천 표시를 믿지 말고 쌍 상태를 보세요.

## 6. 감사 기록

- 사례마다 추가 전용 JSONL이 있습니다. 각 항목은 `seq`, `prev`(이전 항목 해시), `hash`를 가진 연결 해시입니다. `GET .../audit`이 다시 계산해
  `intact`를 줍니다. 한 줄이라도 바뀌면 `intact: false, broken_at: <seq>`입니다.
- 기록하는 것은 세 가지입니다.
  - 사례 이벤트 전부(`case_created`, `requests_created`, `response_recorded`, `request_status`, `document_registered`,
    `document_verified`, `clinician_judged`)
  - 평가마다 `evaluation_recorded`(쌍 상태 전이, 바뀐 기준 ID, 증분·전체 대조 결과, 사실 버전)
  - **거부된 완료 시도**(`kind: "denied"`, `code`, `action`, `by`). 권한 헤더가 틀린 시도는 `by: "unauthenticated"`로 남습니다.
    사례가 있을 때만 기록하며, 사례 ID는 128비트 난수입니다.
- 사례 이벤트는 사례 저장이 끝난 뒤 옮깁니다. 저장과 옮기기 사이에 프로세스가 죽으면 다음 저장 때 이어서 옮깁니다(`sync`).
- 값·환자 원문은 감사 기록에 넣지 않습니다(사실 ID, 문서 ID·해시, 기준 ID만).

## 7. 오류 코드

| code | HTTP | 뜻 |
|---|---|---|
| `verification_not_configured` | 503 | 검증 모드 없음 (운영 기본값) |
| `production_authority_unavailable` | 503 | 운영 모드: 의료기관 인증 연동 없음 |
| `verification_misconfigured` | 503 | 키가 짧거나 서로 같음 |
| `synthetic_authority_real_case` | 403 | 합성 권한으로 실제 시험 저장소 사례를 다루려 함 |
| `verifier_forbidden` / `clinician_forbidden` / `staff_forbidden` | 403 | 그 역할의 키가 아님 |
| `multiple_roles` | 400 | 한 요청에 여러 역할 헤더 |
| `bad_actor_id` | 400 | 식별자 헤더 형식 |
| `staff_cannot_answer` | 403 | 환자 답변 대리 입력 |
| `stale_revision` | 409 | `expected_revision`이 현재 개정 번호와 다름 |
| `request_not_found` / `document_not_found` / `fact_not_found` | 404 | |
| `not_document_review` / `not_clinician_review` | 409 | 요청 종류가 다름 |
| `conflict_not_closed_by_document` | 409 | 충돌 확인을 문서 검증으로 닫으려 함 |
| `request_closed` | 409 | 이미 처리된 요청 |
| `document_changed` / `document_integrity` | 409 | 해시 불일치 (요청 값 / 저장 파일) |
| `field_out_of_scope` | 409 | 이 요청이 확인하는 항목이 아님 |
| `already_verified` | 409 | 이미 문서로 확인된 사실 |
| `bad_value` / `bad_field` / `no_items` / `duplicate_items` | 400 | 검증 항목 형식 |
| `judgment_incomplete` | 409 | 대상 기준을 모두 한 번씩 판단하지 않음 |
| `not_clinician_criterion` | 409 | 임상의 판단 기준이 아님 |
| `basis_not_document_verified` | 409 | 진료 기록 필요 기준에 문서 확인 근거 없음 |
| `bad_document` / `bad_document_kind` / `bad_media_type` | 400 | 문서 형식 |
| `analysis_disabled` | 503 | 실제 LLM 허가 없음 (재평가 불가, O와 같음) |

## 8. 실제 운영에 남은 제약

1. **운영 인증·권한 없음.** 검증자·임상의는 공유 키로만 확인합니다. 의료기관의 계정(SSO), 면허·직무 확인, 사례별 접근 권한,
   2인 확인, 키 회전·폐기가 없습니다. `production` 모드는 일부러 비어 있고(503), 운영 `DocumentAuthority`·`ClinicianAuthority`
   어댑터는 계약 결정이 필요합니다. 운영 어댑터는 `synthetic-` 접두어 증명을 받아들이지 않아야 합니다.
2. **문서 내용은 서버가 해석하지 않습니다.** 값이 문서와 같다는 것은 검증자가 확인하는 일입니다. 서버는 문서의 존재,
   해시, 요청 범위, 사실 값의 묶임(`fact_digest`)만 보장합니다. OCR·구조화 추출은 없습니다.
3. **문서 저장은 합성 전용 파일 저장소입니다.** 암호화, 보관 기간, 삭제, 접근 기록, 악성 파일 검사가 없습니다. 실제 의무기록을
   이 저장소에 올리면 안 됩니다. 그래서 실제 사례의 문서 등록도 막혀 있습니다.
4. **서명 비밀 하나가 모든 증명을 묶습니다.** 비밀이 유출되면 증명을 위조할 수 있습니다. 비밀이 바뀌면 모든 증명이 무효가 되어
   실패 시 닫힙니다. KMS·HSM, 키 버전 관리는 없습니다.
5. **임상의 판단의 범위는 좁습니다.** 판단 기준 분류(`underspecified`, `EXPERT_RULES`)는 완전하지 않습니다. 플래그 없이 들어온
   새 시험의 임상 판단 기준은 막지 못합니다. 판단 사유는 코드만 받고 자유 서술은 없습니다. 판단은 사실이 바뀌면 모두 다시 해야 합니다.
6. **충돌은 문서로 해결하지 않습니다.** 문서 값이 환자 답과 다르면 보류됩니다. 어느 값을 채택할지 정하는 계약은 없습니다.
7. **자기 보고 보류 해제는 한 경우만, follow-up 서비스에서만 합니다**(`evidence_basis.release_match`). matcher 게이트와
   보호 파일(`decision_state.py`, `matcher.py`, `pipeline.py`)은 바꾸지 않았습니다. 판정기가 "근거가 환자 답변뿐"이라 보류하고
   답변 값을 남긴 기준만, 기존 문서 확인 게이트(`fact_view.gate`)를 통과할 때 해제합니다. 출처 불명(`unknown`) 보류와 구조화 값 근거 문제로
   생긴 보류는 해제하지 않습니다. 그래서 웹 `/api/runs`·WP6 시뮬레이터의 판정은 이 변경으로 달라지지 않습니다.
   또 `tests/test_clinical_record_gate.py::test_no_production_code_installs_an_authority`를 바꿨습니다. 이 테스트는 문서 권한을
   설치하는 코드로 `followup/service.py`를 허용하고, 설정 없음·운영 모드·실제 사례에서 그 권한이 `NoDocumentAuthority`인지 확인합니다.
   이 계약 변경은 리뷰에서 확인이 필요합니다.
8. **O의 남은 제약은 그대로입니다**: 단일 프로세스 저장소, 동기 LLM 평가, 공유 담당자 키, 환자 재접속 경로 없음.
   검증·판단 API도 같은 저장소 잠금을 씁니다.
9. 감사 기록은 같은 디스크의 파일입니다. 연결 해시는 변조를 **감지**할 뿐 막지 못합니다(전체를 다시 쓰면 감지되지 않음).
   외부 앵커링(WORM 저장소 등)은 없습니다.

## 9. 확인 방법

- `pytest tests/test_document_clinician_completion.py`(합성, 오프라인)
- `python scripts/followup_verification_flow.py --out <json>`: 실제 서버 프로세스를 띄워 HTTP로 흐름 전체를 돕니다.
  두 번 강제 종료 후 다시 띄우고, 서명 비밀을 바꾸고, 모드를 끕니다(26개 확인).
- 데모 서버: `python scripts/followup_demo_server.py --synthetic-verification`. 키를 출력하고 T-D 시험을 포함합니다.
  서명 비밀을 고정하지 않으면 재시작할 때마다 새로 만들어지므로, 이전 증명·판단은 인정되지 않습니다.
