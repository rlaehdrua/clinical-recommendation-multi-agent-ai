"""문서 검증자가 서류에서 기록한(record) 나이·ECOG가 판정에 쓰이는지 (#49 자동 실행에서 발견, 합성 데이터만).

전에는 검사값만 원문에 확인 줄을 붙여, 환자가 말한 적 없는 ECOG를 서류로 기록해도 Matcher가 출처를 찾지 못해('출처 불명')
진료 기록 필요 기준이 계속 보류됐음. 해제는 여전히 서버 권한이 인정한 문서 확인이 있을 때만 (권한이 없으면 보류 유지)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import test_document_clinician_completion as D  # noqa: E402
import web_harness as h  # noqa: E402

env = D.env
NO_ECOG = "Synthetic NSCLC patient. 64-year-old male with stage IV non-small cell lung cancer.\nPlatelets 180 x10^9/L"


def _record_all(cid, values):
    c = D.staff_client()
    case = D.staff_case(c, cid)
    pids = [p["proposal_id"] for p in case["proposals"] if p["action_type"] == "REQUEST_DOCUMENT" and p["info_key"] in values]
    reqs = {r["info_key"]: r["request_id"] for r in D.make_requests(c, cid, *pids)["case"]["requests"] if r["kind"] == "document_review"}
    doc = D.register(c, cid)["document"]
    out = None
    for key, rid in reqs.items():
        v = c.get(f"/api/followup/verifier/cases/{cid}", headers=D.VERIFIER).json()
        r = next(x for x in v["requests"] if x["request_id"] == rid)
        fact = next((f for f in r["facts"] if f["field"] == key and not f["document_verified"]), None)
        item = {"fact_id": fact["fact_id"]} if fact else {"field": key, **values[key]}
        out = D.verify(c, cid, rid, doc, [item])
    return out


def test_recorded_ecog_releases_the_record_criterion(env):
    cid = D.create(h.browser(), ["T-A"], text=NO_ECOG)
    out = _record_all(cid, {"ecog": {"value": 1}, "lab:platelets": {}, "lab:absolute neutrophil count":
                            {"value": 2.1, "unit": "x10^9/L", "observed_at": "2026-10-01"}})
    assert out["pairs"]["T-A"]["status"] == "CONFIRMED_ELIGIBLE"


def test_recorded_ecog_is_still_held_without_document_authority(env, monkeypatch):
    cid = D.create(h.browser(), ["T-A"], text=NO_ECOG)
    _record_all(cid, {"ecog": {"value": 1}, "lab:platelets": {}, "lab:absolute neutrophil count":
                      {"value": 2.1, "unit": "x10^9/L", "observed_at": "2026-10-01"}})
    D.synthetic_env(monkeypatch, mode=None)  # 권한이 꺼지면 같은 사례도 다시 보류
    D.restart(env)
    assert D.staff_case(D.staff_client(), cid)["pairs"]["T-A"]["status"] != "CONFIRMED_ELIGIBLE"
