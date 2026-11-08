"""발표용 데모 서버 --seed-demo: 비어 있는 저장소에 합성 NSCLC 환자 6명(sim.demo SYN-01~06)을 신청 상태로 넣음.

공개 사실만 쓰고(숨긴 검사 수치·유전자는 넣지 않음), 다시 켜도 중복으로 넣지 않으며, 연구진 화면이 처음부터 여러 환자를
함께 봄. 합성 시험·오프라인 대역만."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
sys.path.insert(0, str(Path(__file__).parent))

import test_document_clinician_completion as D  # noqa: E402
from ctrec.sim import demo  # noqa: E402
from ctrec.web import followup_api  # noqa: E402

env = D.env


def _server():
    spec = importlib.util.spec_from_file_location("demo_server", Path(__file__).parents[1] / "scripts" / "followup_demo_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_seed_texts_use_public_facts_only():
    texts = dict(_server().seed_texts())
    co = demo.cohort()
    assert list(texts) == [p.key for p in co.patients]
    for p in co.patients:
        public = {f.field for f in p.public}
        t = texts[p.key].lower()
        for word, field in (("hemoglobin", "lab:hemoglobin"), ("neutrophil", "lab:absolute neutrophil count"),
                            ("egfr", "biomarker"), ("platelet", "lab:platelets")):
            assert (word in t) == (field in public), (p.key, word)  # 서류가 필요한 숨긴 값은 소개글에 없음
        assert "synthetic" in t and "not a real person" in t
    assert "80-year-old" in texts["SYN-04"]  # 나이 기준 불충족 장면 (근거 확인 경로)


def test_seed_fills_an_empty_store_once(env):
    srv, svc = _server(), followup_api.service()
    assert srv.seed_demo(svc, gap=0) == 6
    assert len(svc.store.case_ids()) == 6
    assert srv.seed_demo(svc, gap=0) == 0  # 다시 켜도 중복으로 넣지 않음
    listed = D.staff_client().get("/api/followup/staff/cases", headers=D.STAFF).json()["cases"]
    assert len(listed) == 6 and all(c["synthetic"] for c in listed)
    ins = D.staff_client().get("/api/followup/staff/insight", headers=D.STAFF).json()
    assert ins["summary"]["cases"] == 6
    rem = [p for g in ins["questions"] if g["route"] != "clinician_review" for p in g["patients"] if not p["request_id"]]
    assert sum(p["minutes"] for p in rem) < sum(p["minutes_alone"] for p in rem)  # 묶으면 하나씩보다 짧음
