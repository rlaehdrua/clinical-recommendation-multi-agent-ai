"""담당자 화면 '고른 질문 합계' (POST /api/followup/staff/insight/estimate): insight와 같은 원장·같은 추천 순서의 가정 비용.

같은 환자·같은 정보원(검사 결과지 한 장)은 연락 비용을 한 번만 청구하므로 묶으면 하나씩보다 싸고, 이미 보낸 확인은 다시 세지 않음.
요청을 만들지 않고 판정도 바꾸지 않음. 합성 시험·오프라인 대역만."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sys.path.insert(0, str(Path(__file__).parent))

import test_document_clinician_completion as D  # noqa: E402
import web_harness as h  # noqa: E402

env = D.env
URL = "/api/followup/staff/insight/estimate"


def _insight(c):
    return c.get("/api/followup/staff/insight", headers=D.STAFF).json()


def _unsent(ins, pred=lambda g: True):
    return [{"case_id": p["case_id"], "proposal_id": p["proposal_id"], "minutes": p["minutes"]}
            for g in ins["questions"] if g["route"] != "clinician_review" and pred(g)
            for p in g["patients"] if not p["request_id"] and p["requestable"]]


def _estimate(c, picks):
    r = c.post(URL, headers=D.STAFF, json={"picks": [{"case_id": p["case_id"], "proposal_id": p["proposal_id"]} for p in picks]})
    assert r.status_code == 200, r.text
    return r.json()


def test_bundled_lab_items_cost_less_than_one_by_one(env):
    cid = D.create(h.browser(), ["T-A", "T-B", "T-C"])
    c = D.staff_client()
    labs = [p for p in _unsent(_insight(c), lambda g: g["info_key"].startswith("lab:")) if p["case_id"] == cid]
    assert len(labs) >= 2
    out = _estimate(c, labs)
    assert out["picked"] == len(labs) and out["unknown"] == []
    assert 0 < out["minutes"] < out["minutes_alone"]  # 같은 결과지: 연락 비용 한 번
    assert out["decidable_pairs"] >= out["decidable_pairs_before"]
    one = _estimate(c, labs[:1])
    assert one["minutes"] == one["minutes_alone"]  # 하나만이면 묶음 할인 없음
    pick = lambda xs: [{"case_id": p["case_id"], "proposal_id": p["proposal_id"]} for p in xs]  # noqa: E731
    both = c.post(URL, headers=D.STAFF, json={"picks": pick(labs), "groups": [pick(labs), pick(labs[:1])]}).json()
    assert [g["minutes"] for g in both["groups"]] == [out["minutes"], one["minutes"]]  # 묶음별 값 = 따로 부른 값


def test_all_unsent_matches_the_insight_total_and_nothing_is_created(env):
    for _ in range(2):
        D.create(h.browser(), ["T-A", "T-B", "T-C"])
    c = D.staff_client()
    ins = _insight(c)
    picks = _unsent(ins)
    out = _estimate(c, picks)
    assert out["minutes"] == pytest.approx(sum(p["minutes"] for p in picks), abs=0.11)  # 화면 카드의 분과 같은 원장
    assert out["decidable_pairs"] <= ins["summary"]["decidable_pairs"] or out["decidable_pairs_before"] > 0
    after = _insight(c)
    assert all(not p["request_id"] for g in after["questions"] for p in g["patients"] if g["route"] != "clinician_review")
    assert _estimate(c, [])["picked"] == 0


def test_sent_or_unknown_picks_are_not_counted(env):
    cid = D.create(h.browser(), ["T-A"])
    c = D.staff_client()
    picks = [p for p in _unsent(_insight(c)) if p["case_id"] == cid]
    D.make_requests(c, cid, picks[0]["proposal_id"])
    out = _estimate(c, picks[:1] + [{"case_id": cid, "proposal_id": "PA-nope"}])
    assert out["picked"] == 0 and out["minutes"] == 0
    assert f"{cid}:PA-nope" in out["unknown"]


def test_estimate_needs_staff_and_the_request_header(env):
    D.create(h.browser(), ["T-A"])
    assert h.browser().post(URL, json={"picks": []}).status_code in (401, 403)
    r = D.staff_client().post(URL, headers={**D.STAFF, "X-CTREC-Request": "0"}, json={"picks": []})
    assert r.status_code == 403
