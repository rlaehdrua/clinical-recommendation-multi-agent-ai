"""감사 F-17: 손상된 사례 파일 하나가 담당자 사례 목록·Cohort C 대기열·Insight·검증자/임상의 큐 전체를 500으로 만들던 것.

읽지 못한 사례는 목록에서 빼고 응답의 unreadable에 사례 id만 남김 (조용히 사라지지 않게). 원문·예외 메시지는 넣지 않음.
읽을 수 있는 사례는 그대로 보임. 합성 데이터, LLM 대역.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
sys.path.insert(0, str(Path(__file__).parent))

import test_document_clinician_completion as D  # noqa: E402

env = D.env
BAD = "FC-" + "0" * 32


def _corrupt(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{BAD}.json").write_text("{ not json", encoding="utf-8")


@pytest.mark.parametrize("path,headers,key", [
    ("/api/followup/staff/cases", D.STAFF, "cases"),
    ("/api/followup/staff/queue", D.STAFF, "items"),
    ("/api/followup/staff/insight", D.STAFF, "questions"),
    ("/api/followup/verifier/queue", D.VERIFIER, "items"),
    ("/api/followup/clinician/queue", D.CLINICIAN, "items"),
])
def test_one_corrupt_case_does_not_break_lists(env, path, headers, key):
    patient, staff = D.h.browser(), D.staff_client()
    cid = D.create(patient, ["T-A"], text=D.LAB_TEXT)
    _corrupt(env)
    r = staff.get(path, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert key in body
    assert body["unreadable"] == [BAD]
    assert "not json" not in r.text
    if path.endswith("/staff/cases"):
        assert [c["case_id"] for c in body["cases"]] == [cid]


def test_no_unreadable_when_all_cases_load(env):
    patient, staff = D.h.browser(), D.staff_client()
    D.create(patient, ["T-A"], text=D.LAB_TEXT)
    r = staff.get("/api/followup/staff/cases", headers=D.STAFF)
    assert r.status_code == 200 and r.json()["unreadable"] == []
