# R1 WP7 보안 적용 경로 보고서 (2026-11-07)

- 브랜치: `night2/wp7-security-paths`, 폴더 `workseoyeon-r1-wp7`. 원격에 올리지 않음
- 기준: `origin/integration/dcvc` `b2100d8` + N1 `3acc817` (fast-forward, N1 브랜치는 수정하지 않음)
- 코드 최종: `f9d91f2`. 이 보고서 커밋은 문서만 더함
- 전체 pytest: `f9d91f2`에서 **2617 passed, 9 skipped, exit 0** (N1 2556 passed, 61개 증가)

## 결론

N1의 보호 기능은 대부분 테스트에서만 호출되고 있었습니다. 이번 작업으로 오프라인 실행기는 실제로 검사를 거치게 됐습니다. CLI와 웹은 `DecisionRecord`를 만들지 않으므로 기록 검사 기능이 적용될 경로 자체가 없습니다. 이 빈틈은 호출 인자 연결로 해결할 수 없고, WP8에서 기록 생산을 연결해야 합니다.

## 1. 호출 지점 전수조사

`src`, `scripts`, `eval` 기준입니다.

| 기능 | R1 전 운영 호출 | R1 후 |
|---|---|---|
| `compute_for_runs`, `load_run_inputs`, `write_metrics` | 없음 | 없음. 직접 호출이 생기면 `test_no_operational_code_reads_metrics_without_a_scanner` 실패 |
| `export_metrics` (신규) | 없음 | `scripts/run_offline_records.py` |
| `RecordWriter`, `IssueWriter` | `record_case`, scanner만 전달 | `record_case`, scanner + 환자 문장 전달 |
| `load_run_v2` | `verify_run`, scanner 없음 | `verify_run`, scanner 전달 |
| `inspect_run_checked`, `render_inspection_html` | 없음 | 없음 |
| `to_trace`, `to_trace_checked` | 없음 | 없음 |
| `audit_diagnostics.inspect_run` | 없음 | 없음 |

scanner를 만드는 실제 경로는 `load_cases_hygienic`뿐이고, 오프라인 평가에서만 씁니다. 웹과 CLI에는 실행별 scanner가 없습니다.

## 2. 실제 적용된 보안 경로

| 경로 | 적용 내용 | 확인 |
|---|---|---|
| `record_case` 쓰기 | scanner, 환자 문장 금지 목록, 자유 서술 검사 | 사례 201건 정상, 호출 인자 테스트 |
| `verify_run` | 읽기 scanner, 자유 서술 검사 | 변조 파일에서 `sensitive_record:free_text` 보고 |
| `run_offline_records.py` | `reversal_metrics.json`, `no_findings`가 아니면 exit 1 | 실행 201건 `no_findings` |
| `load_run_v2`를 쓰는 모든 경로 | 64MiB, 문제 줄 10,000 상한 | 80MB 입력 즉시 실패, 최대 메모리 67MB |
| CLI 실패 출력 | 예외 종류만, exit 1 유지 | 일반 예외, pydantic `input_value` 합성 재현 |
| orchestrator `tool_error` | 예외 종류만 기록 | 코드 고정 테스트 |
| `--redact-console` (선택) | ID, 근거 문장, 보고서 경로, Tracer 콘솔 제외 | 합성 재현 |

실제 데이터 비교: masked 101건과 validation 100건의 정규화 파일과 `summary.json`이 N1과 같습니다 (201건 일치, 0건 차이). 새로 생긴 산출물은 `reversal_metrics.json`뿐입니다.

## 3. 아직 호출되지 않는 보안 기능

- `inspect_run_checked`, `render_inspection_html`: 호출자 없음
- `to_trace_checked`: 호출자 없음. CLI·웹 trace에 v2 이벤트가 없음
- `load_diagnostics`의 scanner 경로: `inspect_run_checked`를 통해서만 쓰임
- `--redact-console`: 기본 꺼짐
- `CTREC_CLI_TRACEBACK`: 개발용 해제 스위치

## 4. scanner가 없을 때의 동작

| 호출 | 동작 |
|---|---|
| `export_metrics(dirs, None)` | `AuditError`, 값 없음 |
| `inspect_run_checked(dir, None)` | `AuditError`, 값 없음 |
| `compute_for_runs(dirs)` | 검사 없이 집계. 걸릴 기록도 수와 공개 `pair_ref`에 포함 |
| `to_dict(identifiers=True)` | 원시 ID 출력 |
| `write_metrics` | 파일에 `value_scan.status: not_scanned` 표시. 식별자와 폴더 이름은 없음 |
| `load_run_v2(dir)` | 원시 값을 그대로 돌려줌 |

**안전한 기본값 후보:** `export_metrics`를 외부 경로의 유일한 진입점으로 두고 AST 테스트로 고정했습니다. `compute_for_runs`의 기본값을 scanner 필수로 바꾸는 안은 적용하지 않았습니다. 테스트의 scanner 없는 호출 약 50곳을 고쳐야 하고, 오프라인 분석 호환이 깨지기 때문입니다.

## 5. 재현 결과 (합성 값, 출력은 참/거짓만)

| 항목 | N1 | R1 기본 | R1 `--redact-console` |
|---|---|---|---|
| 병렬 실패 예외 문구 | **노출** | 없음 | 없음 |
| pydantic `input_value` | **노출** | 없음 | 없음 |
| 단일 실행 traceback 문구 | **노출** | 없음 | 없음 |
| `rationale`, 의사 확인, 선정 사유 | **노출** | **노출** (호환 유지) | 없음 |
| 환자 ID | **노출** | **노출** (호환 유지) | `#순번` |
| Tracer의 요약·예외 문구 | **노출** | **노출** (호환 유지) | 콘솔 없음 |

| 잔여 위험 검증 | N1 | R1 |
|---|---|---|
| 자유 서술 저장 | 저장됨 | 공백·비ASCII는 거부 |
| 검사 보기의 자유 서술 | 표시됨 | 제외, `values_withheld` |
| 지표 파일의 자유 서술 | 없음 | 없음 |
| 공백 없는 ASCII 서술 | 통과 | **통과** (테스트로 고정) |
| 공개 `pair_ref` 대입 | 후보 1000개, 1ms 미만 복원 | `to_dict`·`write_metrics`는 **여전히 복원 가능** |
| `export_metrics`의 `pair_ref` | 없음 | 호출마다 달라 공개 해시와 맞지 않음 |
| 20MB 손상 파일 | 75초(계측 포함), 757MB, 문제 400만 줄 | 80MB에서 즉시 실패, 67MB, 문제 1줄 |
| 무한대 비용 | `OverflowError` | 값 없는 `AuditError` |

`pair_ref`는 어떤 형태든 익명화가 아닙니다. 사건 수, 단계, 경유 상태가 남고 `export_metrics`는 scanner가 아는 값만 찾습니다.

## 6. 수정 커밋

| SHA | 내용 | 분류 |
|---|---|---|
| `65b3bd9` | 읽기 상한, 무한대 비용 처리 | 코드 |
| `cb2a468` | `export_metrics`, 키 있는 `pair_ref`, `value_scan`, `free_text` | 코드 |
| `6e701fe` | 이전 한계를 고정한 테스트 기대값 갱신 | **기대값 변경** |
| `ff0dc1e` | 오프라인 실행기 연결 | 코드 |
| `1d9e472` | CLI 예외 종류 출력, `--redact-console`, orchestrator | 코드 |
| `f9d91f2` | WP7 문서 | 문서 |

`6e701fe`의 기대값은 다음과 같습니다. 한글 문장이 scanner를 통과한다고 고정한 테스트 2개는 ASCII 값으로 바꿔 같은 한계를 계속 고정합니다. 정확한 문제 코드를 비교하는 테스트에는 `free_text`가 추가됩니다.

호환성 변경:
- 단일 실행 실패가 traceback 대신 한 줄로 나옵니다. exit 1은 같습니다.
- 공백·비ASCII 값이 든 기록은 새로 저장되지 않습니다. 기존 파일은 지우지 않고 `scanner=None` 읽기도 그대로입니다.
- 64MiB 또는 문제 줄 10,000을 넘는 감사 파일은 읽기 실패가 됩니다.
- `write_metrics` 파일에 `value_scan` 키가 추가됩니다. `to_dict()`는 그대로입니다.

## 7. 잔여 위험

실제 환자 데이터를 쓰기 전에 닫아야 하는 항목을 P0으로 분류했습니다. 이 등급은 재현 결과에 근거한 판단이며, 실제 데이터 사용 일정은 확인하지 않았습니다.

### P0

1. **CLI 기본 콘솔에 환자 ID, 근거 문장, Tracer 문구가 나옵니다.** 해결책은 `--redact-console`의 기본화입니다. 출력 계약이 바뀌므로 팀 결정이 필요합니다.
2. **`pipeline.py:554`의 `repr(e)`가 콘솔과 `trace.jsonl`에 남습니다.** 보호 파일이라 고치지 않았습니다. 웹 화면은 `_client_record`가 줄이지만, 파일에는 남습니다.
3. **CLI·웹 운영 경로에 감사 기록과 scanner가 없습니다.** `RecordWriter`, `inspect_run_checked`, `export_metrics` 모두 이 경로에서 실행되지 않습니다.

### P1

1. `trace.jsonl`, `report.md`와 실행 결과 파일에 환자 문구가 그대로 남습니다. `--redact-console`은 콘솔만 줄입니다. 보관 기간과 접근 정책 결정이 필요합니다.
2. 공백 없는 ASCII 서술과 ID 모양의 이름은 `free_text`로 찾지 못합니다. `contracts.py` 필드 패턴이 근본 해결입니다.
3. `free_text` 기준은 402건 실측값입니다. 기준 항목 이름이 한글인 데이터는 쓰기 거부로 실행이 멈출 수 있습니다.
4. `write_metrics`의 공개 `pair_ref`는 대입 가능합니다. 운영 경로는 `export_metrics`만 쓰지만, 파일을 직접 만들면 남습니다.
5. 오프라인 실행기의 별칭은 고정 기본 salt `wp7-offline`의 HMAC입니다. 이 값은 스크립트에 공개돼 있어, 원래 ID 후보를 아는 사람은 코드 기준으로 대입할 수 있습니다 (실행 재현은 하지 않음). `AliasMap` 자체의 기본값은 무작위 salt입니다.
6. `audit_diagnostics.inspect_run`은 scanner 결과를 검증하지 않고, `rp.load_issues`에는 읽기 상한이 없습니다. 둘 다 외부 입력을 받는 운영 호출은 없습니다.
7. scanner 비교는 정확한 문자열 일치입니다. 공백이나 대소문자가 다르면 찾지 못합니다.
8. 상한 안의 유효한 64MiB 파일을 끝까지 파싱하는 최악 시간은 재지 않았습니다.

## 8. N1과 비교한 추가 효과

- 운영 호출이 0이던 지표 검사가 오프라인 실행기에서 실제로 실행되고, 실패하면 exit 1이 됩니다.
- N1의 금지 문자열 수정이 실제 쓰기 경로에 처음 연결됐습니다.
- 자유 서술 저장을 `contracts.py` 변경 없이 막았습니다.
- CLI 예외 문구 노출 3개 경로를 기본값에서 제거했습니다.
- 대입 가능하던 `pair_ref`에 내보내기용 키 경로가 생겼습니다.
- 메모리와 문제 목록 증폭을 상한으로 막았습니다.
- 회귀 테스트 61개가 늘었습니다.

## 9. 다른 브랜치와의 관계

N1은 fast-forward로 적용했으므로 충돌과 중복 수정이 없습니다. 이 브랜치는 N1과 같은 파일을 더 수정하므로 N1과 함께 또는 N1 뒤에 병합해야 합니다. 다른 브랜치와의 `git merge-tree` 결과는 `reports/R1.md`에 기록합니다.
