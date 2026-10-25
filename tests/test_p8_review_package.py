"""P8 검수 패키지: 기준별 검수 자료 동기화, 계약 문구 재검증 결과(조사·현재 참여·예방 백신), 수정 필요 IR 처리, 미승인 유지."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_review_package  # noqa: E402
import report_contract_drafts  # noqa: E402

from ctrec.ir_review import Registry  # noqa: E402

CONTRACTS = {c["contract_id"]: c for c in
             json.loads((ROOT / "contracts" / "question_contracts.json").read_text(encoding="utf-8"))["contracts"]}


def test_review_package_in_sync():
    assert build_review_package.main(["--check"]) == 0


def test_still_no_review_records_and_no_active_ir():
    assert not (ROOT / "contracts" / "reviews.json").exists()
    reg = Registry()
    assert all(not reg.active_irs(t) for t in reg.trial_ids())


def test_no_ir_left_in_fix_needed():
    """P7 '수정 필요' 1건(NCT06756191:E1)은 한정어로 표현을 보강하고 남은 쟁점은 임상 검토로 넘김."""
    assert not [k for k, (c, _) in report_contract_drafts.IR_REVIEW.items() if c == "fix"]
    assert report_contract_drafts.IR_REVIEW["NCT06756191:E1"][0] == "clinical"
    keys = {k for o in CONTRACTS["QC-AMPHOTERICIN-COURSE"]["options"] for k, _ in o["assertions"]}
    assert "amphotericin_b(over_5d)/event/ever" in keys and "QC-AMPHOTERICIN-EVER" not in CONTRACTS


_PARTICLE = re.compile(r"([가-힣])(\s*\([^)]*\))?(을|를)(?=\s)")


def test_object_particles_match_final_consonant():
    bad = []
    for cid, c in CONTRACTS.items():
        for m in _PARTICLE.finditer(c["question"]):
            want = "을" if (ord(m.group(1)) - 0xAC00) % 28 else "를"
            if m.group(3) != want:
                bad.append((cid, m.group(0)))
    assert not bad


def test_trial_participation_question_covers_ongoing_participation():
    q = CONTRACTS["QC-TRIAL-PARTICIPATION-LAST"]["question"]
    assert "마지막으로" in q and "지금도 참여 중" in q


def test_cancer_vaccine_question_excludes_prophylactic_vaccines():
    q = CONTRACTS["QC-CANCER-VACCINE-EVER"]["question"]
    assert "치료" in q and "예방 백신" in q
