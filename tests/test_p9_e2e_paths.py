"""P9 실행 경로 E2E 보강 (결정적 대역, 유료 API 없음). P8 E2E(test_p8_e2e.py)에 없던 경로:

- 계약 실행 결과 파일(result.json·trace.jsonl) -> Inspector load_run -> 화면 (v2 기록 왕복). 기록 불일치 경고 없음
- 기존 Session 실행 결과 파일 -> Inspector (v1 기록, 계약 절 없음)
- 기본 웹(기존 Session, legacy 정책)과 기존 Session + 계약 정책(CTREC_QA_POLICY=contract) 웹 실행
- 기본값: 정책 legacy, Session 클래스 기존 Session (서비스 기본 동작 변경 없음)
실제 LLM 호출 없음 - 결정적 대역의 통과는 실제 Claude 출력의 재현성 증거가 아닙니다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import pipeline, qa_guard
from ctrec.contract_session import ContractSession, session_class
from ctrec.pipeline import PatientCase, Session
from ctrec.trace import Tracer

from contract_fixtures import QC_LACT, QC_PREG, QC_SURGERY
from test_contract_p5_session import (REF, T1_RULES, _Answerer, _contract_env, _profile_from,  # noqa: F401
                                      _trials, patched)

inspector = pytest.importorskip("ctrec.inspector", reason="Inspector(feat/decision-inspector)가 있는 통합 환경에서만")

ANSWERS = {QC_SURGERY["question"]: "수술을 받은 적 없음", QC_PREG["question"]: "아니요", QC_LACT["question"]: "아니요"}
META = {"format_version": 1, "llm_backend": "offline_stub", "synthetic": True, "written_by": "test"}


def _run_to_dir(monkeypatch, tmp_path, cls):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": T1_RULES})
    out = tmp_path / "run"
    kw = _contract_env(tmp_path) if cls is ContractSession else {}
    s = cls(case=PatientCase("P1", "45세 여성.", input_source="clinical_record"), trials=trials,
            answerer=_Answerer(ANSWERS), tracer=Tracer(out / "trace.jsonl", verbose=False), **kw)
    s.reference_date = REF
    s.run_fixed()
    s.save(out)
    (out / "run_meta.json").write_text(json.dumps(META), encoding="utf-8")
    return s, out


def test_defaults_keep_legacy_policy_and_base_session(monkeypatch):
    monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    monkeypatch.delenv("CTREC_SESSION", raising=False)
    assert qa_guard.policy() == "legacy" and session_class() is Session


def test_contract_run_files_roundtrip_through_inspector(monkeypatch, patched, tmp_path):
    s, out = _run_to_dir(monkeypatch, tmp_path, ContractSession)
    trace = [json.loads(x) for x in (out / "trace.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    applied = [t for t in trace if t["agent"] == "contract" and t["event"] == "applied"]
    assert applied and all("assessments" in t and "eligibility" in t for t in applied)
    rec = inspector.load_run(out)
    v = inspector.build_view(rec)
    assert not [a for a in v["anomalies"] if "불일치" in a or "다름" in a], v["anomalies"]
    final = {t["trial_id"]: t["eligibility"] for t in v["overview"]["final"]}
    assert final == {"T1": s.matches["T1"].eligibility}
    crit = {c["criterion_id"]: c for c in v["contract"]["criteria"]}
    assert crit["T1:E1"]["outcome"] == "decided" and crit["T1:E1"]["contract_ids"] == ["QC-SURGERY-LAST@v1"]
    page = inspector.render_html(v)
    for text in [*ANSWERS, "수술을 받은 적 없음"]:  # strict: 질문 문장·자유 답 없음
        assert text not in page
    sv = inspector.build_view(rec, synthetic_details=True)
    assert {f["value"] for f in sv["contract"]["facts"]} == {False}


def test_base_session_run_files_are_v1_records_for_inspector(monkeypatch, patched, tmp_path):
    s, out = _run_to_dir(monkeypatch, tmp_path, Session)
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert "contract" not in result
    v = inspector.build_view(inspector.load_run(out))
    assert v["contract"] is None and not [a for a in v["anomalies"] if "불일치" in a or "다름" in a]
    assert "계약 판정 출처" not in inspector.render_html(v)


@pytest.mark.parametrize("policy", [None, "contract"])
def test_web_base_session_flow_under_each_policy(monkeypatch, tmp_path, policy):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import web_harness as h
    from ctrec.web import app as web
    monkeypatch.delenv("CTREC_SESSION", raising=False)
    if policy:
        monkeypatch.setenv(qa_guard.POLICY_ENV, policy)
    else:
        monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    h.install(monkeypatch, tmp_path)
    c = h.browser()
    r = c.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    run = web.RUNS[rid]
    q = h.wait_for(run, "questions")
    assert c.post(f"/api/runs/{rid}/answers",
                  json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}}).status_code == 200
    h.wait_for(run, "end", 30)
    events = h.sse_events(c.get(f"/api/runs/{rid}/events").text)
    res = next(e for e in events if e["type"] == "result")["result"]
    assert "contract" not in res and res["qa_log"][0]["answer"] == h.SYN_ANSWER
    for m in res["matches"].values():
        assert m["eligibility"] in ("ELIGIBLE", "UNCERTAIN", "INELIGIBLE", "NOT_EVALUATED")
        if policy == "contract":  # 비계약 답에 기댄 확정은 남지 않음 (머리말 모드 경계)
            assert not [a for a in m["assessments"] if a.get("passes") is not None
                        and "qa_uncontracted" in (a.get("validation_flags") or [])]
