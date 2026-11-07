"""요청 본문 크기 제한 (감사 V5 F-09, F-10). FastAPI TestClient로 실제 HTTP 경로를 거침, 실제 LLM 없음.

- F-09: Content-Length가 없는 chunked 요청도 같은 한도를 넘으면 413 (본문을 끝까지 파싱하지 않음).
- F-10: 문서 등록 경로만 기존 문서 계약(최대 2 MB, verification.MAX_DOCUMENT_BYTES)에 맞는 한도. 다른 경로는 그대로.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402
from test_document_clinician_completion import LAB_REPORT, create, env, register  # noqa: E402,F401
from test_followup_flow import STAFF, staff_client  # noqa: E402

from ctrec.followup import verification  # noqa: E402
from ctrec.web import app as web  # noqa: E402

HDR = {"x-ctrec-request": "1", "content-type": "application/json"}


def _chunks(data: bytes, size: int = 65536):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def _big_json(n: int) -> bytes:
    return b'{"patient_text": "' + b"a" * n + b'", "trial_ids": ["x"]}'


# ------------------------------------------------------------------ F-09
def test_chunked_body_over_the_limit_is_rejected():
    c = h.browser()
    big = _big_json(web.MAX_BODY_BYTES * 3)
    assert c.post("/api/followup/cases", content=big, headers=HDR).status_code == 413  # Content-Length 경로 (기존)
    r = c.post("/api/followup/cases", content=_chunks(big), headers=HDR)
    assert r.status_code == 413, r.text
    assert r.json() == {"detail": "요청이 너무 큽니다."}


def test_chunked_body_under_the_limit_still_reaches_the_route(env):  # noqa: F811
    import json

    from test_followup_flow import TEXT

    c = h.browser()
    body = json.dumps({"patient_text": TEXT, "trial_ids": ["T-A"]}).encode()
    r = c.post("/api/followup/cases", content=_chunks(body, 16), headers=HDR)
    assert r.status_code == 201, r.text  # 한도 안의 chunked 본문은 기존대로 처리됨


# ------------------------------------------------------------------ F-10
def _doc_body(size: int) -> dict:
    content = LAB_REPORT + b"x" * (size - len(LAB_REPORT))
    return {"kind": "lab_report", "media_type": "text/plain", "content_base64": base64.b64encode(content).decode()}


@pytest.mark.parametrize("size", [300_000, verification.MAX_DOCUMENT_BYTES])
def test_document_up_to_the_contract_size_is_accepted(env, size):  # noqa: F811
    patient, staff = h.browser(), staff_client()
    cid = create(patient, ["T-A"])
    r = staff.post(f"/api/followup/staff/cases/{cid}/documents", headers=STAFF, json=_doc_body(size))
    assert r.status_code == 201, r.text[:200]


def test_document_over_the_contract_size_is_still_rejected(env):  # noqa: F811
    patient, staff = h.browser(), staff_client()
    cid = create(patient, ["T-A"])
    r = staff.post(f"/api/followup/staff/cases/{cid}/documents", headers=STAFF,
                   json=_doc_body(verification.MAX_DOCUMENT_BYTES * 2))
    assert r.status_code == 413


def test_other_routes_keep_the_256kb_limit(env):  # noqa: F811
    patient, staff = h.browser(), staff_client()
    cid = create(patient, ["T-A"])
    big = b'{"proposal_ids": ["' + b"p" * 300_000 + b'"]}'
    r = staff.post(f"/api/followup/staff/cases/{cid}/requests", headers={**STAFF, **HDR}, content=big)
    assert r.status_code == 413
