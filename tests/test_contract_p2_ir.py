"""P2 명제·Condition IR·검수: 해시 불일치, 미승인, 미지원 조건, 논리식, 기준 시점, 경계, 개념 포함, 정규화."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.condition_ir import CriterionIR, validate_node
from ctrec.ir_review import Registry, ReviewError
from ctrec.propositions import Proposition, PropositionError, TimeScope, entails, normalize_key, prop

from contract_fixtures import PROXY, REVIEWER, RULES, SURG_IR, atom, frozen_dir, ir_dict, sidecar


# ------------------------------------------------------------ 명제
def test_same_proposition_normalizes_to_one_key():
    a = normalize_key("surgery(non_diagnostic.major)/event/w28d-first_dose")
    b = normalize_key("surgery(major.non_diagnostic)/event/w28d-first_dose")
    assert a == b == prop("surgery", "w28d-first_dose", "major", "non_diagnostic").key


@pytest.mark.parametrize("key", ["surgery_side_effect/event/ever", "surgery/condition/ever",
                                 "surgery(minor)/event/ever", "pregnancy/condition/w0d-assessment",
                                 "surgery/event/w28d-tomorrow", "Surgery/event/ever", "surgery/event/w28d"])
def test_unregistered_or_malformed_propositions_rejected(key):
    with pytest.raises(PropositionError):
        Proposition.parse(key)


def test_temporal_entailment_direction():
    ever, w90, w28, cur = (prop("surgery", s) for s in ("ever", "w90d-assessment", "w28d-assessment", "current"))
    assert entails(w28, True, w90) is True and entails(w28, True, ever) is True  # 좁은 기간 있음 -> 넓은 기간 있음
    assert entails(ever, False, w28) is False and entails(w90, False, w28) is False  # 넓은 기간 없음 -> 좁은 기간 없음
    assert entails(ever, True, w28) is None  # 반례 B: 평생 있음은 최근 4주에 대해 결론 없음
    assert entails(w28, False, ever) is None and entails(w28, False, w90) is None
    assert entails(cur, True, w28) is True and entails(w28, True, cur) is None


def test_boundary_days_are_exact():
    w28, w30 = prop("chemotherapy", "w28d-assessment"), prop("chemotherapy", "w30d-assessment")
    assert entails(w28, False, w30) is None  # 28일 없음은 30일 없음을 말하지 않음
    assert entails(w30, False, w28) is False
    assert entails(w28, True, w30) is True and entails(w30, True, w28) is None


def test_anchor_mismatch_has_no_entailment():
    a, b = prop("surgery", "w28d-assessment"), prop("surgery", "w28d-first_dose")
    assert entails(a, False, b) is None and entails(a, True, b) is None
    assert entails(prop("surgery", "current"), True, b) is None  # 현재 -> 다른 기준 시점 기간도 결론 없음


def test_concept_hierarchy_direction():
    ritux, cd20 = prop("rituximab", "w180d-assessment"), prop("anti_cd20_antibody", "w180d-assessment")
    assert entails(ritux, True, cd20) is True and entails(cd20, False, ritux) is False
    assert entails(cd20, True, ritux) is None and entails(ritux, False, cd20) is None


def test_qualifier_narrows():
    surg, major = prop("surgery", "w28d-assessment"), prop("surgery", "w28d-assessment", "major")
    assert entails(surg, False, major) is False and entails(major, True, surg) is True
    assert entails(surg, True, major) is None  # 수술 있음 != 큰 수술 있음


def test_different_propositions_never_merged():
    preg, lact, cur = (prop("pregnancy", "current"), prop("lactation", "current"),
                       prop("pregnancy", "ever"))
    assert entails(preg, False, lact) is None and entails(lact, False, preg) is None
    assert entails(cur, True, preg) is None  # 임신한 적 있음 -> 현재 임신 결론 없음 (반례 D)
    assert prop("surgery", "ever").narrower_or_equal(prop("chemotherapy", "ever")) is False


def test_timescope_validation():
    with pytest.raises(PropositionError):
        TimeScope("ever", 3)
    with pytest.raises(PropositionError):
        TimeScope("window", 0)


# ------------------------------------------------------------ IR 형식
def test_logic_nodes_validate():
    ok = {"op": "and", "args": [atom("pregnancy/condition/current"),
                                {"op": "not", "arg": {"op": "or", "args": [atom("lactation/condition/current"),
                                                                           atom("surgery/event/ever")]}}]}
    assert validate_node(ok) == []
    assert validate_node({"op": "and", "args": [atom("surgery/event/ever")]})  # 인자 1개
    assert validate_node({"op": "xor", "args": []})
    assert validate_node({"op": "compare", "prop": "surgery/event/ever", "cmp": ">", "value": 1, "unit": "x"})
    assert validate_node({"op": "unsupported", "text": "", "reason": "x"})


def test_unsupported_requires_partial(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    cond = {"op": "and", "args": [SURG_IR, {"op": "unsupported", "text": "Major", "reason": "큰 수술 정의 없음"}]}
    ir = CriterionIR.from_dict(ir_dict(RULES["SURG"], d, cond))
    assert any("completeness" in e for e in ir.errors())
    ir2 = CriterionIR.from_dict(ir_dict(RULES["SURG"], d, cond, completeness="partial",
                                        unsupported_parts=["Major의 정의"]))
    assert ir2.errors() == []


def test_source_span_must_be_in_original(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    ir = CriterionIR.from_dict(ir_dict(RULES["SURG"], d, SURG_IR, span="surgery within 2 weeks"))
    assert any("source_span" in e for e in ir.errors())


def test_unknown_field_rejected(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    with pytest.raises(ValueError):
        CriterionIR.from_dict({**ir_dict(RULES["SURG"], d, SURG_IR), "approved": True})


# ------------------------------------------------------------ 검수·활성화
def test_active_only_with_human_approval_of_current_digest(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    irs = [ir_dict(RULES["SURG"], d, SURG_IR, anchor_policy=PROXY)]
    reg = sidecar(tmp_path, d, irs, approve_ir=False)
    assert reg.ir_record("T1", "E1").status == "unreviewed" and reg.active_irs("T1") == {}
    ir = CriterionIR.from_dict(irs[0])
    reg.record_review("ir", "T1:E1", ir.digest(), "approved", reviewer=REVIEWER, note="ok",
                      clinical_review_confirmed=True)
    assert reg.ir_record("T1", "E1").status == "active"
    # 내용이 바뀌면 이전 승인은 효력 없음
    changed = {**irs[0], "condition": atom("surgery/event/w28d-first_dose")}
    (reg.root / "ir" / "T1.json").write_text(json.dumps({"criteria": [changed]}), encoding="utf-8")
    assert reg.ir_record("T1", "E1").status == "unreviewed"


def test_file_review_status_field_does_not_activate(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    reg = sidecar(tmp_path, d, [ir_dict(RULES["SURG"], d, SURG_IR, review_status="approved")], approve_ir=False)
    assert reg.ir_record("T1", "E1").status == "unreviewed"


@pytest.mark.parametrize("who", ["claude", "Claude Opus", "auto-review", "LLM", "", "ci-bot"])
def test_automated_or_empty_reviewer_cannot_approve(tmp_path, who):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    irs = [ir_dict(RULES["SURG"], d, SURG_IR)]
    reg = sidecar(tmp_path, d, irs, approve_ir=False)
    with pytest.raises(ReviewError):
        reg.record_review("ir", "T1:E1", CriterionIR.from_dict(irs[0]).digest(), "approved", reviewer=who,
                          note="ok", clinical_review_confirmed=True)


def test_approval_requires_clinical_confirmation_and_note(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    irs = [ir_dict(RULES["SURG"], d, SURG_IR)]
    reg = sidecar(tmp_path, d, irs, approve_ir=False)
    dg = CriterionIR.from_dict(irs[0]).digest()
    with pytest.raises(ReviewError):
        reg.record_review("ir", "T1:E1", dg, "approved", reviewer=REVIEWER, note="ok", clinical_review_confirmed=False)
    with pytest.raises(ReviewError):
        reg.record_review("ir", "T1:E1", dg, "approved", reviewer=REVIEWER, note=" ", clinical_review_confirmed=True)


def test_rejection_after_approval_deactivates(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    irs = [ir_dict(RULES["SURG"], d, SURG_IR)]
    reg = sidecar(tmp_path, d, irs)
    assert reg.ir_record("T1", "E1").active
    reg.record_review("ir", "T1:E1", CriterionIR.from_dict(irs[0]).digest(), "rejected", reviewer=REVIEWER,
                      note="기간 해석 재검토", clinical_review_confirmed=False)
    assert reg.ir_record("T1", "E1").status == "rejected"


def test_frozen_hash_mismatch_invalidates(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    irs = [ir_dict(RULES["SURG"], d, SURG_IR)]
    reg = sidecar(tmp_path, d, irs)
    assert reg.ir_record("T1", "E1").active
    # 잠긴 기준표 파일이 바뀜 (FROZEN.json은 그대로) -> 실제 해시 불일치
    p = d / "T1.json"
    p.write_text(p.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert reg.ir_record("T1", "E1").status == "stale_frozen"


def test_rule_text_mismatch_invalidates(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    irs = [ir_dict(RULES["SURG"].model_copy(update={"text": "Major surgery within 2 weeks before first dose"}), d,
                   SURG_IR, span="Major surgery")]
    reg = sidecar(tmp_path, d, irs, approve_ir=False)
    assert reg.ir_record("T1", "E1").status == "stale_frozen"
    with pytest.raises(ReviewError):  # 쓸 수 없는 IR은 승인도 거부
        reg.record_review("ir", "T1:E1", CriterionIR.from_dict(irs[0]).digest(), "approved", reviewer=REVIEWER,
                          note="ok", clinical_review_confirmed=True)


def test_missing_rule_and_duplicates(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    one = ir_dict(RULES["SURG"], d, SURG_IR)
    reg = sidecar(tmp_path, d, [one, one], approve_ir=False)
    assert reg.ir_record("T1", "E1").status == "invalid"
    assert reg.ir_record("T1", "E7").status == "missing_rule"


def test_unfrozen_trial_is_stale(tmp_path):
    d = frozen_dir(tmp_path, [RULES["SURG"]])
    irs = [ir_dict(RULES["SURG"], d, SURG_IR)]
    reg = sidecar(tmp_path, d, irs, approve_ir=False)
    (d / "FROZEN.json").write_text(json.dumps({"files": {}}), encoding="utf-8")
    assert reg.ir_record("T1", "E1").status == "stale_frozen"


def test_registry_reads_real_frozen_store_without_writing(tmp_path):
    """실제 data/parsed는 읽기만 (사이드카 root는 tmp)."""
    from ctrec import config
    before = {p.name: p.read_bytes() for p in config.PARSED_DIR.glob("*.json")}
    reg = Registry(tmp_path / "contracts")
    assert reg.ir_record("DEMO-LUNG-001", "E4").status == "missing_rule"
    assert {p.name: p.read_bytes() for p in config.PARSED_DIR.glob("*.json")} == before
