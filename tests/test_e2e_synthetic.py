"""끝까지 자동 실행(scripts/run_e2e_synthetic.py)과 가상 서류 검사기(sim.documents). 오프라인(틀 글, LLM 호출 0), 합성 데이터만."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ctrec.sim import documents as D  # noqa: E402

LABS = [D.DocItem("lab:platelets", 180.0, "x10^9/L", "2026-10-01"), D.DocItem("lab:hemoglobin", 12.1, "g/dL", "2026-10-01")]


def test_template_document_passes_its_own_check_and_reads_back():
    text = D.template_document("SYN-01", "lab_report", LABS, "2026-10-12")
    assert text.startswith(D.WATERMARK) and D.validate_document(text, LABS) == []
    got = D.template_extract(text, ["lab:platelets", "lab:hemoglobin"])
    assert got["lab:platelets"]["value"] == 180 and got["lab:hemoglobin"]["value"] == 12.1
    assert got["lab:platelets"]["observed_at"] == "2026-10-01"


def test_document_check_rejects_changed_missing_and_unexpected_values():
    good = D.template_document("SYN-01", "lab_report", LABS, "2026-10-12")
    assert "lab:platelets:value_missing_or_wrong" in D.validate_document(good.replace("180", "18"), LABS)
    assert "watermark_missing" in D.validate_document(good.replace(D.WATERMARK, ""), LABS)
    assert "lab:hemoglobin:date_missing" in D.validate_document(good.replace("(검사일 2026-10-01)", "", 2), LABS)
    absent = [LABS[0], D.DocItem("lab:hemoglobin", None, scenario="missing")]
    assert "lab:hemoglobin:should_be_absent" in D.validate_document(good, absent)  # 빼야 할 항목이 서류에 있음


def test_narrative_check_blocks_hidden_and_oracle_values():
    public = [D.DocItem("age", 64, "years"), D.DocItem("ecog", 3, scenario="self_report")]
    text = D.template_narrative(public, set())
    assert D.validate_narrative(text, public, set(), [D.DocItem("lab:platelets", 180.0)]) == []
    assert "64-year-old" in text and text.splitlines()[-1].startswith("Patient reports ECOG 3")  # 자기 보고는 맨 뒤 줄
    hidden = D.template_narrative(public, {"age"})
    assert "64" not in hidden and D.validate_narrative(hidden, public, {"age"}, []) == []
    assert "age:hidden_value_leaked" in D.validate_narrative(text, public, {"age"}, [])
    assert "lab:platelets:oracle_value_leaked" in D.validate_narrative(text + "\nPlatelets 180 x10^9/L", public, set(),
                                                                         [D.DocItem("lab:platelets", 180.0)])


def test_stage_is_not_read_as_age():
    assert D._lines_with("Stage IV non-small cell lung cancer.", "age") == []


def test_offline_end_to_end_run_is_safe_and_reaches_confirmed_results(tmp_path):
    import run_e2e_synthetic as E
    args = argparse.Namespace(mode="offline", env_file=None, patients=None, missing=0.15, clinician="MET", seed=1, budget=600,
                              per_round=6, max_rounds=12, url=None, keep_server=False, cache=None, out=str(tmp_path / "run"))
    r = E.run(args)
    s, st = r["score"], r["stats"]
    assert r["safety_violations"] == 0  # 확정이 규칙 GT와 반대인 쌍 없음
    assert s.get("correct_confirmed", 0) >= 1 and s.get("correct_ineligible", 0) >= 1 and s.get("confirmed_after_clinician", 0) >= 1
    assert st["patient_answers"] >= 1 and st["verified_requests"] >= 1 and st["clinician_judgments"] >= 1
    assert st.get("document_unavailable", 0) >= 1  # 서류에 없는 항목은 '서류 없음'으로 (값을 지어내지 않음)
    assert st.get("read_wrong", 0) == 0 and not r["llm_usage"]
    docs = list((tmp_path / "run" / "docs").glob("*_r*.txt"))
    assert docs and all(p.read_text(encoding="utf-8").startswith(D.WATERMARK) for p in docs)
