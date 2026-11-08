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


# ----------------------------------------------------------------------------- 구조 손상 (JSON은 정상)
import json  # noqa: E402

BROKEN = {
    "no_session": lambda d: d.pop("session"),
    "requests_null": lambda d: d.__setitem__("requests", None),
    "no_current": lambda d: d.pop("current"),
}
ENDPOINTS = [
    ("/api/followup/staff/cases", D.STAFF, "cases"),
    ("/api/followup/staff/queue", D.STAFF, "items"),
    ("/api/followup/staff/insight", D.STAFF, "questions"),
    ("/api/followup/verifier/queue", D.VERIFIER, "items"),
    ("/api/followup/clinician/queue", D.CLINICIAN, "items"),
]


def _broken_copy(root: Path, good_id: str, how: str) -> None:
    """읽을 수 있는 사례를 복사해 id만 바꾸고 구조 하나를 깨뜨림 (JSON·형식·id 검사는 통과)."""
    d = json.loads((root / f"{good_id}.json").read_text(encoding="utf-8"))
    d["case_id"] = BAD
    BROKEN[how](d)
    (root / f"{BAD}.json").write_text(json.dumps(d), encoding="utf-8")


def _case_ids(path: str, body: dict) -> set[str]:
    if path.endswith("/staff/insight"):
        return {p["case_id"] for q in body["questions"] for p in q["patients"]}
    key = "cases" if path.endswith("/staff/cases") else "items"
    return {x["case_id"] for x in body[key]}


@pytest.mark.parametrize("how", list(BROKEN))
@pytest.mark.parametrize("path,headers,key", ENDPOINTS)
def test_structurally_broken_case_is_isolated(env, caplog, path, headers, key, how):
    patient, staff = D.h.browser(), D.staff_client()
    cid, _ = D.answered_lab_case(patient, staff)  # 문서 확인 요청 + 임상의 판단 요청(T-D)이 열린 사례
    before = staff.get(path, headers=headers)
    assert before.status_code == 200, before.text
    expected = _case_ids(path, before.json())
    assert cid in expected  # 대기열마다 이 사례의 항목이 있음
    _broken_copy(env, cid, how)
    with caplog.at_level("WARNING"):
        r = staff.get(path, headers=headers)
    assert r.status_code == 200, (how, r.text[:300])
    body = r.json()
    assert key in body and body["unreadable"] == [BAD]
    assert _case_ids(path, body) == expected  # 읽을 수 있는 사례는 그대로 보임
    for rec in caplog.records:  # 로그에는 id와 예외 종류만
        if BAD in rec.getMessage():
            assert "Platelets" not in rec.getMessage() and "Synthetic" not in rec.getMessage()


@pytest.mark.parametrize("path", ["/api/followup/staff/queue", "/api/followup/staff/insight"])
def test_case_failing_during_restore_is_isolated(env, path):
    """구조 검사는 통과하지만 복원(_restore) 중에 실패하는 사례: 사례별 본문 전체가 격리됨."""
    patient, staff = D.h.browser(), D.staff_client()
    cid, _ = D.answered_lab_case(patient, staff)
    expected = _case_ids(path, staff.get(path, headers=D.STAFF).json())
    assert cid in expected
    d = json.loads((env / f"{cid}.json").read_text(encoding="utf-8"))
    d["case_id"], d["session"] = BAD, {"not": "a session"}
    (env / f"{BAD}.json").write_text(json.dumps(d), encoding="utf-8")
    r = staff.get(path, headers=D.STAFF)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["unreadable"] == [BAD] and _case_ids(path, r.json()) == expected


def test_staff_screen_shows_unreadable_notice():
    """담당자 화면이 목록·대기열·Insight의 unreadable을 모아 '읽지 못한 사례 N건'을 띄움 (모두 손상되어도 '환자 없음'만 보이지 않게)."""
    src = (Path(__file__).parents[1] / "src" / "ctrec" / "web" / "static" / "staff.js").read_text(encoding="utf-8")
    assert "x.unreadable" in src and "읽지 못한 사례" in src
    assert "renderCases(list, unreadable.size)" in src
