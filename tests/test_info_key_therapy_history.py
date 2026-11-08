"""치료력 기준의 항목 키 (데모 시나리오 C 관찰, docs/dev/demo_nsclc_scenarios.md).

재현 (1f04229): NCT06216301:E12 "Had prior treatment with any other anti-PD-1, or PD-L1 or PD-L2 agent ... in the 12 months
prior to randomization" (category treatment_history, 구조화 필드 없음)의 항목 키가 원문의 낱말 PD-L1 때문에
biomarker:PD-L1이 되어, 다른 시험의 PD-L1 발현 검사 기준(NCT07416058:I8)과 한 질문으로 묶이고 병리 결과지로 요청됨.
PD-L1 검사 결과는 치료력 기준의 근거가 아님.

계약: 바이오마커 표적으로 묶는 것은 바이오마커 기준(category biomarker)만. 치료력·과민 반응 등 다른 범주의 원문에 표적 이름이
있어도 그 기준만의 키(criterion:<시험>:<기준>)와 그 범주의 후보 항목(treatment_history 등)을 씀. Frozen Store는 그대로.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.decision_state import info_key  # noqa: E402
from ctrec.schemas import ParsedTrial  # noqa: E402

from test_offline import _rule  # noqa: E402

PARSED = Path(__file__).resolve().parents[1] / "data" / "nsclc" / "parsed"


def _rules(tid: str) -> dict:
    d = json.loads((PARSED / f"{tid}.json").read_text(encoding="utf-8"))
    return {r.rule_id: r for r in ParsedTrial.model_validate(d["parsed"]).rules}


def test_therapy_history_wording_is_not_a_biomarker_item():
    e12 = _rules("NCT06216301")["E12"]
    assert e12.category == "treatment_history" and "PD-L1" in e12.text
    key, fields, candidate = info_key(e12, "NCT06216301")
    assert key == "criterion:NCT06216301:E12"
    assert "biomarker" not in fields and "treatment_history" in fields and candidate is True
    for tid, rid in (("NCT06928389", "E4"), ("NCT07416058", "E1"), ("NCT07027514", "E19")):
        assert not info_key(_rules(tid)[rid], tid)[0].startswith("biomarker:"), (tid, rid)


def test_biomarker_criteria_still_group_by_target():
    pdl1 = _rule(rule_id="I8", text="PD-L1 TPS >= 1%", category="biomarker", structured_evaluable=False)
    assert info_key(pdl1, "T")[0] == "biomarker:PD-L1"
    adc = _rule(rule_id="E2", kind="exclusion", text="Prior treatment with any EGFR-targeted antibody-drug conjugate",
                category="treatment_history", structured_evaluable=False)
    assert info_key(adc, "T")[0] == "criterion:T:E2"
