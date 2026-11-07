# WP7 DecisionRecord 저장·조회 경로 보안

> 처음 작성: 브랜치 `fix/wp7-audit-read-security` (base `0b402c6`). 현재 PR 기준은 `integration/dcvc` `b2100d8`입니다.
> 측정과 테스트는 합성 표지값으로만 했습니다. LLM·외부 API 호출 없음.

## 1. 어떤 경로를 쓸지

| 용도 | 쓸 함수 | 값 검사 |
|---|---|---|
| 화면·API (WP8) | `audit_inspection.inspect_run_checked(run_dir, scanner)` + `render_inspection_html(view)` | 정상 기록·진단 모두. scanner 필수 |
| 오프라인 평가·검증 | `audit.load_run_v2(run_dir)` | 없음 (기존 동작) |
| 기록 저장 | `audit.RecordWriter` | 저장 시 |
| trace 저장 (새 코드) | `audit.to_trace_checked(...)` | 저장 시 |
| 사용 금지 (새 코드) | `audit.to_trace` | 없음. 호환을 위해 남김 |
| 화면에 쓰지 않음 | `audit_diagnostics.inspect_run` | 진단만. 정상 기록 값은 그대로 보임 |

명시적 한계
- `load_run_v2(scanner=None)`은 오프라인용이며 값을 검사하지 않습니다.
- `to_trace()`는 검사하지 않습니다.
- scanner가 모르는 민감정보는 탐지되지 않을 수 있습니다.
- WP8은 `inspect_run_checked`와 `render_inspection_html`을 써야 합니다.
- 이 문서의 검증은 파일 기반 합성 입력으로 한 것입니다. 실제 운영 연결은 별도 검증이 필요합니다.
- `load_run_v2(scanner=None)`, `to_trace()`, `audit_diagnostics.inspect_run`은 운영용 조회·저장 경로로 쓰지 않습니다.
- 역전 지표를 밖으로 내보내는 경로는 `reversal_metrics.export_metrics(run_dirs, scanner)`뿐입니다. 조건은 §3과 §5를 봅니다.
- 이 작업은 위 경로의 보호만 다룹니다. WP7 전체의 개인정보 보호가 완성되었다는 뜻이 아닙니다.

검사는 **scanner가 아는 값만** 찾습니다. 통과는 "이 scanner로 찾지 못했다"는 뜻이며 안전하다는 뜻이 아닙니다. 그래서 보기의 `value_scan.limit`은 `scanner_known_values_only`이고, `value_scan.status`는 `no_findings` 또는 `withheld`입니다. 화면은 이를 "안전함"으로 바꿔 표시하면 안 됩니다.

## 2. 경로별 동작

| 경로 | 입력 | 동작 |
|---|---|---|
| 오류 보고 | 비정상 `schema_version` | `unsupported_schema:?`. `ctrec.decision_record/v<숫자 1~4자리>`만 표시 |
| 오류 보고 | 알 수 없는 JSON 키 | `action.<extra>`처럼 위치만 표시. 계약 필드 이름과 번호 외에는 `<unknown>` |
| 오류 보고 | 손상된 줄 (깊은 중첩, 초장 정수 포함) | 그 줄만 `corrupt_json`, 나머지 줄은 로드 |
| 오류 보고 | 비 UTF-8 파일 | 값 없는 `AuditError`. 원래 예외를 `__context__`에 남기지 않음 |
| 저장 | `RecordWriter` | 걸리면 쓰지 않고 `AuditError` (종류만) |
| 저장 | `to_trace_checked` | 같은 검사. 쓰기 전에 검사하고, 통과하면 `to_trace`와 같은 형식 |
| 저장 | `to_trace` | **검사 없음** |
| 조회 | `load_run_v2(run_dir, scanner=s)` | 걸린 기록을 빼고 `line N: sensitive_record:<종류>`. trace 폴백도 동일 |
| 조회 | `inspect_run_checked` | 정상 기록과 진단에 같은 `CheckedScanner` 적용. 표시할 수 없는 값(무한대 비용)은 값 없는 `AuditError` |
| 조회 | `export_metrics(run_dirs, scanner)` | scanner 없으면 `AuditError`. 식별자·폴더 이름 없음, `pair_ref`는 호출마다 새 키 |
| 저장·조회 | 자유 서술 (`free_text`) | 공백·비ASCII가 있거나 256자를 넘는 문자열. 저장은 거부, scanner 조회는 제외. `required_fields`는 기준 항목 이름이라 공백 3개까지 허용. `info_key`는 공백이 있으면 `lab:`·`basis:lab:` 뒤 소문자 단어 2~4개만 허용 |
| 읽기 상한 | 파일 64MiB, 문제 줄 10,000 초과 | 끝까지 읽지 않고 `AuditError`. 지표는 그 폴더만 `records_unreadable` 또는 `issues_file_too_large` / `issues_file_too_damaged` |

### scanner 실패 처리 (`audit.CheckedScanner`)

| 상황 | 동작 |
|---|---|
| 없음, `scan` 메서드 없음 | `AuditError`. 보기·기록을 만들지 않음 |
| 예외, 잘못된 반환 (`None`, 문자열, 비문자열 항목) | 통과가 아니라 `AuditError`. 원래 예외를 남기지 않음 |
| 모르는 종류 코드 | 값일 수 있어 `other`로만 보고 |
| 오탐 | 기록을 빼고 건수·코드를 표시. 자동 복구 없음, scanner 설정을 고쳐야 함 |

걸린 기록도 `multiple_run_ids`와 역전 검사에는 포함합니다. 코드와 step만 보고하므로 값이 나가지 않고, 기록을 뺐다고 역전 경고가 사라지지 않습니다.

걸린 기록이 있으면 `audit_status`는 `no_blocking_diagnostics`로 남지 않고 `values_withheld`가 됩니다. `contract_violation` 같은 더 심각한 상태는 덮어쓰지 않습니다.

## 3. scanner로도 남는 위험

- **scanner가 모르는 식별자**: `literals`에 없는 환자 ID는 ID 모양이 `F-`/`M-`/`V-` 규칙에 맞지 않으면 통과합니다.
- **자유 서술**: 기본 `LeakScanner`는 문장을 찾지 못합니다. 한글 문장은 `literals`나 `forbidden_texts`에 정확히 있을 때만 걸립니다.
- **항상 빈 목록을 돌려주는 scanner**: 형식이 맞으므로 실패로 알아낼 수 없습니다.
- **`forbidden_texts`의 4자 미만 항목**: `sensitive_findings`가 무시합니다. 따옴표·줄바꿈이 있는 문장은 JSON 이스케이프 모양까지 찾지만, 공백·대소문자가 다른 표기는 찾지 못합니다.
- **`IssueWriter`**: `scanner=None`이면 저장 시 검사가 없습니다. 조회 시 검사가 이를 보완합니다.
- **`load_run_v2(scanner=None)`·`to_trace`**: 호환을 위해 남겼으므로 직접 호출하면 값이 검사 없이 지나갑니다.
- **역전 지표**: `compute_for_runs`·`load_run_inputs`의 기본값은 검사 없음입니다. 이때는 걸린 기록이 집계 수와 `pair_ref`에 들어가고 `to_dict(identifiers=True)`로 원시 값이 나옵니다. `write_metrics` 파일은 `value_scan.status`가 `not_scanned`입니다. `src`, `scripts`, `eval`에서 이 함수를 직접 부르면 `tests/test_security_paths.py`가 실패합니다.
- **`pair_ref`**: 기본 `to_dict()`와 `write_metrics`는 소금 없는 SHA-256 앞 12자리라 후보를 아는 사람이 대입으로 되찾을 수 있습니다. `export_metrics`는 호출마다 키를 새로 만들어 대입과 출력 간 연결을 막습니다. **어느 쪽도 익명화가 아닙니다.** 사건 수, 단계, 경유 상태는 그대로 남습니다.
- **자유 서술 탐지**: 공백 없는 ASCII 서술(`chest-pain-yesterday`)과 이름, 숫자만 있는 값은 찾지 못합니다.
- **`Tracer`**: 생성할 때 trace 파일을 비웁니다. 기존 동작이며 검사 실패 여부와 무관합니다.

## 4. 후속 계약 검토 항목 (`contracts.py` 보류)

다음 문자열 필드는 형식 제한이 없어 공백·한글 자유 서술이 계약 검증을 통과합니다.

- `facts_added`, `outcome.new_fact_ids`, `outcome.charge_keys`, `outcome.resolved_criteria`
- `criteria[].evidence_fact_ids`, `criteria[].evaluator_version`
- `action.target_trial_ids`, `action.required_fields`, `action.evidence_fact_ids`, `action.fingerprint`, `action.legacy_action_id`, `action.policy_version`
- `policy_version`, `evaluator_version`, `fact_version_before`, `fact_version_after`

`RecordWriter`의 기본 검사는 ID 모양과 정답 토큰만 보므로, `forbidden_texts`가 없으면 위 필드의 자유 서술을 저장합니다. 근본 해결은 필드 패턴 추가이며, 팀 합의 뒤 별도 작업으로 진행합니다. 패턴을 정하기 전에 실제 값의 형식(`rule_engine/1`, `PA-...|lab_report` 등)을 확인해야 합니다.

`audit.has_free_text`가 공백·비ASCII·초장 값을 저장과 조회에서 막지만, 필드 패턴을 대신하지는 않습니다. 기준은 오프라인 사례 201개의 기록 402건 실측값이며, 다른 데이터 세트에서는 오탐 여부를 다시 확인해야 합니다.

별개로 `EffortCost`는 무한대를 허용합니다. `build_v2_view`를 직접 부르면 `OverflowError`가 나고, `inspect_run_checked`는 이를 값 없는 `AuditError`로 바꿉니다. NaN은 계약 검증에서 `invalid_record`가 됩니다.

## 5. WP8 연결 조건

1. 화면·API는 §1의 `inspect_run_checked`와 `render_inspection_html`만 씁니다.
2. 실행마다 원래 환자 ID를 채운 scanner를 만듭니다. `LeakScanner()`를 그대로 쓰면 ID 모양과 정답 토큰만 봅니다. 웹 경로에는 이 scanner를 만드는 곳이 아직 없습니다.
3. 같은 scanner를 `RecordWriter`, `IssueWriter`, `to_trace_checked`에도 넘깁니다. 환자 원문은 `forbidden_texts`로 넘깁니다.
4. `AuditError`는 일반 조회 실패로 처리합니다. 검사 없는 경로로 되돌아가면 안 됩니다.
5. `value_scan`과 `audit_status`를 표시하고, `values_withheld`를 정상 완료로 보이지 않게 합니다.
6. §4의 자유 서술 위험을 받아들일지, 계약 변경을 먼저 할지 결정합니다.
7. 역전 지표는 §1과 같은 scanner로 `export_metrics`를 호출한 결과만 내보냅니다. `value_scan.status`가 `no_findings`가 아니면 정상 완료로 보이지 않게 합니다 (§3).
8. 실제 웹 경로에서 위 조건을 별도로 검증합니다. 이 문서의 결과는 운영 연결을 검증한 것이 아닙니다.

## 6. 호환성

- 기존 호출은 바뀌지 않습니다. `scanner`는 기본값 `None`인 추가 인자이고, `load_run_v2`는 두 번째 위치 인자, 나머지(`load_records`, `parse_records`, `records_from_trace`)는 키워드 전용입니다.
- `LoadResult`, `build_v2_view`, `render_v2_html`, trace 이벤트 형식은 그대로입니다. 깨끗한 실행에서 `inspect_run_checked`의 결과는 `inspect_run` 결과에 `value_scan`만 더한 것입니다.
- 바뀐 메시지: 추가 키는 `<extra>`, 유니온 표시는 `<unknown>`, 다른 계열의 `schema_version`은 `?`입니다.
- 바뀐 예외: 비 UTF-8 파일은 `UnicodeDecodeError` 대신 `AuditError`입니다. 둘 다 `ValueError`입니다.
- 추가된 출력: `write_metrics` 파일의 `value_scan`, 오프라인 실행기의 `reversal_metrics.json`. `to_dict()`의 키와 공개 `pair_ref`는 그대로입니다.
- 바뀐 저장 동작: `sensitive_findings`가 `free_text`를 돌려주므로 공백·비ASCII 값이 든 기록은 저장되지 않습니다. 기존 파일은 지우지 않으며 검사 없는 읽기(`scanner=None`)는 그대로입니다.
- trace 문제의 `line N`은 파일 줄 번호가 아니라 파싱된 이벤트 순번입니다 (기존 동작).

## 7. 병합 의존성

PR #3과 PR #5는 이 문서를 처음 쓴 뒤 `integration/dcvc`에 병합되었으므로 아래 조건은 이미 충족됐습니다. 이 변경은 `b2100d8` 위에 있습니다.

- `audit_inspection.py`는 `audit_diagnostics.py`를 import합니다.
- `tests/test_reversal_metrics.py`의 기존 노출 고정 단언을 별도 커밋에서 아래와 같이 바꿨습니다.

  ```python
  # 변경 전
  assert SENSITIVE in str(audit.load_run_v2(d).issues)  # 기존 로더 메시지에는 남음: 여기서 제거해야 함
  # 변경 후
  assert SENSITIVE not in str(audit.load_run_v2(d).issues)  # 로더 문제 목록에도 파일 값이 없음
  ```

  같은 합성 입력의 검증은 `test_loader_issues_for_extra_key_and_schema_keep_only_safe_details`에도 있습니다.

### PR #5 문서와의 차이

PR #5의 `docs/dev/wp7_offline_records.md`는 이 브랜치에서 고치지 않습니다. 아래 항목은 **이 문서가 우선**합니다.

| 위치 | PR 문서 내용 | 이 문서 기준 |
|---|---|---|
| `wp7_offline_records.md` §3 WP8 계약 1번 | 화면·API는 `inspect_run(run_dir, scanner=...)` | `inspect_run_checked` + `render_inspection_html` |
| 같은 절 4번 | 같은 scanner를 `RecordWriter`·`IssueWriter`에 전달 | 일치. `to_trace_checked`에도 전달 |
| 같은 절 5번 | scanner는 진단에만, `to_trace`는 검사 없음 | `inspect_run`·`to_trace`는 그대로. 새 경로는 정상 기록도 검사 |
| 같은 절 2번 | scanner 반환값은 종류 코드만, 예외는 조회 실패 | 일치. `CheckedScanner`가 강제 |
| 같은 절 3번 | `LeakScanner()`만으로는 원래 ID를 못 막음, 웹 scanner 없음 | 일치 |
| §8-2 | `audit.py`의 값 노출 수정 요청 | 해결됨 |
| `wp7_reversal_metrics.md` 개인정보 항목 | `audit` 문제 메시지에 파일 내용이 섞일 수 있음 | `audit`은 수정됨. `reversal_metrics`의 코드 축약은 그대로 동작 |
