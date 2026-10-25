"""P9 Inspector 통합: 계약 판정 출처(명제 키·계약 ID@버전·IR digest·사실 출처·정정 여부) 표시, 기록 형식 검사, 개인정보.

- v1 기록(기존 Session·CLI 기록, result.json에 contract 없음): 계약 절이 없고 기존 화면 그대로 (호환)
- v2 기록(ContractSession, result.json["contract"]): 계약 절 표시. 식별자는 형식 검사 후 옮기고, 형식이 다르면 '<text>'로
  가리고 형식 문제로 셈 (P9 수정: 명제 키·'계약@v버전'이 _scalar에서 '<text>'로 가려지던 문제)
- strict: 명제 값·질문 문장·답변 문장 없음 / synthetic_details: 명제 값 표시 (질문·답변 원문은 계약 절에 넣지 않음)
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import pipeline
from ctrec.contract_session import ContractSession
from ctrec.pipeline import Session

from contract_fixtures import QC_LACT, QC_PREG, QC_SURGERY, RULES
from test_contract_p5_session import (T1_RULES, _Answerer, _contract_env, _profile_from, _session,  # noqa: F401
                                      _trials, patched)

inspector = pytest.importorskip("ctrec.inspector", reason="Inspector(feat/decision-inspector)가 있는 통합 환경에서만")

META = {"format_version": 1, "llm_backend": "offline_stub", "synthetic": True, "written_by": "test"}
ANSWERS = {QC_SURGERY["question"]: "수술을 받은 적 없음", QC_PREG["question"]: "아니요", QC_LACT["question"]: "아니요"}
PRIVATE = [*ANSWERS, "수술을 받은 적 없음"]  # 질문 문장·자유 답 ('아니요'는 화면의 다른 열 값과 같은 낱말이라 빼고 봄)


def _run(monkeypatch, tmp_path, cls=ContractSession):
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": T1_RULES})
    kw = _contract_env(tmp_path) if cls is ContractSession else {}
    s = _session(cls, trials, _Answerer(ANSWERS), **kw)
    s.run_fixed()
    return s


def _record(s):
    return inspector.record_from_session(s, trace=s.tracer.records, meta=META)


def _view(rec, synthetic=False):
    v = inspector.build_view(rec, synthetic_details=synthetic)
    return v, inspector.render_html(v)


def test_v2_record_shows_proposition_keys_contract_versions_and_sources(monkeypatch, patched, tmp_path):
    s = _run(monkeypatch, tmp_path)
    v, page = _view(_record(s))
    facts = v["contract"]["facts"]
    assert {f["proposition_key"] for f in facts} == {"surgery/event/ever", "pregnancy/condition/current",
                                                     "lactation/condition/current"}
    assert all(f["contract_id"].startswith("QC-") and f["contract_version"] == 1 and f["source"] for f in facts)
    assert all(f["superseded"] is False for f in facts)
    crit = {c["criterion_id"]: c for c in v["contract"]["criteria"]}
    e1 = crit["T1:E1"]
    assert e1["proposition_keys"] and all(inspector._PROP_KEY.fullmatch(k) for k in e1["proposition_keys"])
    assert e1["contract_ids"] == ["QC-SURGERY-LAST@v1"]
    assert len(e1["ir_digest"]) == 12 and e1["method"] == "contract" and e1["outcome"] == "decided"
    assert e1["question_ids"] and e1["fact_ids"] and e1["round"] == 1
    for key in (*e1["proposition_keys"], "surgery/event/ever", "QC-SURGERY-LAST@v1", e1["ir_digest"]):
        assert key in page
    assert "&lt;text&gt;" not in page.split("계약 판정 출처")[1].split("</table>")[0]
    assert v["overview"]["n_anomalies"] == 0, v["anomalies"]  # 계약 적용 후 판정이 trace에 있어 기록 불일치 없음


def test_strict_hides_values_question_and_answer_text_synthetic_shows_values(monkeypatch, patched, tmp_path):
    s = _run(monkeypatch, tmp_path)
    rec = _record(s)
    v, page = _view(rec)
    assert all(f["value"] is None for f in v["contract"]["facts"]) and "(strict: 숨김)" in page
    for text in PRIVATE:
        assert text not in page
    sv, spage = _view(rec, synthetic=True)
    assert {f["value"] for f in sv["contract"]["facts"]} == {False} and "(strict: 숨김)" not in spage
    section = spage.split("계약 판정 출처")[1]
    for text in PRIVATE:  # 계약 절에는 synthetic에서도 질문 문장·자유 답을 넣지 않음 (값은 예/아니요로 표시)
        assert text not in section.split("<h3>")[0]


def test_superseded_fact_is_marked(monkeypatch, patched, tmp_path):
    rec = _record(_run(monkeypatch, tmp_path))
    rec = copy.deepcopy(rec)
    fid = rec.result["contract"]["facts"][0]["fact_id"]
    rec.result["contract"]["facts"][0]["superseded"] = True
    v, page = _view(rec)
    assert next(f for f in v["contract"]["facts"] if f["fact_id"] == fid)["superseded"] is True


LEAK = "환자가 임신 12주라고 답함"


@pytest.mark.parametrize("field,value", [
    ("propositions", [LEAK]), ("contracts", [f"QC-X@v1 {LEAK}"]), ("questions", [LEAK]), ("fact_ids", [LEAK]),
    ("sources", [LEAK]), ("ir_digest", LEAK), ("method", LEAK), ("outcome", LEAK),
])
def test_tampered_provenance_fields_are_masked_not_rendered(monkeypatch, patched, tmp_path, field, value):
    rec = copy.deepcopy(_record(_run(monkeypatch, tmp_path)))
    rec.result["contract"]["provenance"]["T1:E1"][field] = value
    v, page = _view(rec)
    assert LEAK not in page
    e1 = next(c for c in v["contract"]["criteria"] if c["criterion_id"] == "T1:E1")
    shown = {"propositions": "proposition_keys", "contracts": "contract_ids", "questions": "question_ids"}.get(field, field)
    assert "<text>" in (e1[shown] if isinstance(e1[shown], list) else [e1[shown]])


@pytest.mark.parametrize("bad", [["not", "a", "dict"], "text", 3])
def test_malformed_contract_sections_are_reported_not_crashing(monkeypatch, patched, tmp_path, bad):
    rec = copy.deepcopy(_record(_run(monkeypatch, tmp_path)))
    rec.result["contract"]["provenance"] = bad
    rec.result["contract"]["facts"] = bad
    v, page = _view(rec)
    assert v["contract"]["criteria"] == [] and v["contract"]["facts"] == []
    assert v["overview"]["n_anomalies"] >= 2 and "활성(승인된) IR이 없어" in page


def test_list_field_with_wrong_type_is_counted_as_anomaly(monkeypatch, patched, tmp_path):
    rec = copy.deepcopy(_record(_run(monkeypatch, tmp_path)))
    rec.result["contract"]["provenance"]["T1:E1"]["propositions"] = LEAK
    rec.result["contract"]["facts"][0]["proposition_key"] = LEAK
    v, page = _view(rec)
    e1 = next(c for c in v["contract"]["criteria"] if c["criterion_id"] == "T1:E1")
    assert e1["proposition_keys"] is None and v["contract"]["facts"][0]["proposition_key"] == "<text>"
    assert v["anomalies"] == ["contract.provenance.propositions: 목록이 아님 (str) - 읽지 않음"] and LEAK not in page


def test_v1_record_without_contract_section_is_unchanged(monkeypatch, patched, tmp_path):
    s = _run(monkeypatch, tmp_path, cls=Session)
    rec = _record(s)
    assert "contract" not in rec.result
    v, page = _view(rec)
    assert v["contract"] is None and "계약 판정 출처" not in page and v["schema"] == inspector.SCHEMA
