"""검사 조회 (WP7): 화면·API가 쓰는 DecisionRecord·진단 조회. scanner 없이는 값을 내보내지 않습니다.

audit_diagnostics.inspect_run은 scanner가 진단 파일에만 적용되고 정상 기록의 값은 그대로 보입니다. 이 모듈은 같은 공개
함수를 쓰되 정상 기록과 진단에 같은 CheckedScanner를 적용합니다. 엔진 격리 규칙 때문에 eval_hygiene는 import하지 않고
scanner를 주입받습니다.

보장 범위: 검사는 scanner가 아는 값만 찾습니다. 통과는 "이 scanner로 찾지 못했다"는 뜻이지 안전하다는 뜻이 아닙니다.
scanner에 없는 식별자와, 형식 제한이 없는 필드의 자유 서술은 통과할 수 있습니다 (docs/dev/wp7_audit_read_security.md).
"""

from __future__ import annotations

import html
from pathlib import Path

from . import audit
from . import audit_diagnostics as ad

SCAN_KEY = "value_scan"
SCAN_LIMIT = "scanner_known_values_only"  # 보장 범위 표시. 화면은 이 값을 '안전함'으로 바꿔 표시하면 안 됨
STATUS_WITHHELD = "values_withheld"


def inspect_run_checked(run_dir: Path, scanner) -> dict:
    """build_diagnostics_view 결과 + value_scan. scanner가 없거나 검사가 실패하면 값 없는 AuditError (보기를 만들지 않음).

    걸린 기록·진단은 보기에서 빠지고 건수와 값 없는 코드만 남습니다. 빠진 것이 있으면 audit_status가 정상처럼 보이지 않습니다.
    """
    checked = audit.CheckedScanner(scanner)
    loaded = audit.load_run_v2(run_dir, scanner=checked)
    diag = ad.load_diagnostics(run_dir, {r.run_id for r in loaded.records}, checked)
    view = ad.build_diagnostics_view(loaded, diag)
    records = audit.count_withheld(loaded.issues)
    diagnostics = sum(": sensitive_text:" in p for p in diag.problems)
    view[SCAN_KEY] = {"status": "withheld" if records or diagnostics else "no_findings", "limit": SCAN_LIMIT,
                      "withheld_records": records, "withheld_diagnostics": diagnostics}
    if records and view["audit_status"] == "no_blocking_diagnostics":
        view["audit_status"] = STATUS_WITHHELD
    return view


def render_inspection_html(view: dict) -> str:
    """inspect_run_checked 보기만 그림. 검사를 거치지 않은 보기를 이 경로로 내보내지 않음."""
    scan = view.get(SCAN_KEY)
    if scan is None:
        raise audit.AuditError("검사 조회 결과가 아님")
    base = ad.render_diagnostics_html(view)
    section = (f"<h2>값 검사</h2><p>결과: <b>{html.escape(scan['status'])}</b> · 제외한 기록 "
               f"{int(scan['withheld_records'])} · 제외한 진단 {int(scan['withheld_diagnostics'])}</p>"
               "<p>이 검사는 scanner가 아는 값만 찾습니다. 결과가 안전을 보장하지 않습니다.</p>")
    tail = "</body></html>"
    if not base.endswith(tail):
        raise audit.AuditError("보기 형식이 다름")
    return base[: -len(tail)] + section + tail


__all__ = ["inspect_run_checked", "render_inspection_html", "SCAN_KEY", "SCAN_LIMIT", "STATUS_WITHHELD"]
