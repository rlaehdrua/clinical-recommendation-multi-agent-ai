"""GT 스키마 검증 (FIXTURE_ONLY 가짜 데이터만 사용)."""
import copy
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from gt_lib.models import GTSet, UtteranceInput, export_json_schemas, freeze_problems  # noqa: E402

FX = ROOT / "tests" / "data" / "gt_fixture"


def raw(name="FIXTURE_ONLY_g_u.json"):
    return json.loads((FX / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["FIXTURE_ONLY_g_u.json", "FIXTURE_ONLY_g_c.json", "FIXTURE_ONLY_g_p.json"])
def test_fixture_sets_validate_and_are_marked(name):
    s = GTSet.model_validate(raw(name))
    assert s.fixture_only and s.set_id.startswith("FIXTURE_ONLY")
    assert all(r.source.origin == "fixture" for r in s.items)
    # fixture는 GT로 동결 불가
    assert any("fixture_only" in p for p in freeze_problems(s))
    assert freeze_problems(s, allow_fixture=True) == []


def _bad(mutate, name="FIXTURE_ONLY_g_u.json"):
    d = raw(name)
    mutate(d)
    with pytest.raises(ValidationError):
        GTSet.model_validate(d)


def test_fixture_flag_and_set_id_must_agree():
    _bad(lambda d: d.update(fixture_only=False))
    _bad(lambda d: d.update(set_id="set7_conflict"))


def test_non_fixture_set_cannot_hold_fixture_items():
    _bad(lambda d: d.update(fixture_only=False, set_id="set7_x"))


def test_needs_expert_requires_reason():
    _bad(lambda d: d["items"][0].update(needs_expert={"flag": True, "reason": " "}))


def test_conflict_must_be_hold():
    def m(d):
        d["items"][0]["final"]["decision"] = "accept"
    _bad(m)


def test_hearsay_crossing_must_be_hold():
    def m(d):
        d["items"][3]["final"]["decision"] = "accept"
    _bad(m)


def test_adjudicator_cannot_be_labeler():
    def m(d):
        it = d["items"][4]
        it["adjudication"]["adjudicator_id"] = it["annotations"][0]["labeler_id"]
    _bad(m)


def test_status_and_final_consistency():
    _bad(lambda d: d["items"][0].update(final=None))  # agreed인데 final 없음

    def m(d):
        d["items"][4]["final"] = copy.deepcopy(d["items"][0]["final"])  # ambiguous인데 final 있음
    _bad(m)
    _bad(lambda d: d["items"][0].update(adjudication={"status": "adjudicated"}))  # 판정자·사유 없음


def test_input_rejects_engine_or_label_fields():
    """입력 모델은 extra=forbid: 엔진 출력·라벨 필드를 넣을 수 없음."""
    base = raw()["items"][0]["input"]
    for k, v in [("directness", "direct"), ("decision", "hold"), ("expected", "hold")]:
        with pytest.raises(ValidationError):
            UtteranceInput.model_validate({**base, k: v})


def test_quote_must_be_in_text():
    def m(d):
        d["items"][0]["input"]["quote"] = "not in the text at all"
    _bad(m)


def test_blinding_attestation_cannot_claim_exposure():
    def m(d):
        d["blinding"]["labelers_saw_engine_output"] = True
    _bad(m)


def test_freeze_problems_single_labeler_pending_and_missing_blinding():
    d = raw()
    d["items"][0]["annotations"] = d["items"][0]["annotations"][:1]
    d["items"][1]["adjudication"] = {"status": "pending"}
    d["items"][1]["final"] = None
    d["blinding"] = None
    p = freeze_problems(GTSet.model_validate(d), allow_fixture=True)
    assert any("라벨러 2명" in x for x in p)
    assert any("pending" in x for x in p)
    assert any("블라인딩" in x for x in p)


def test_agreed_with_disagreeing_labels_is_not_freezable():
    d = raw()
    d["items"][1]["annotations"][1]["label"]["category"] = "FIXTURE_ONLY_other"
    p = freeze_problems(GTSet.model_validate(d), allow_fixture=True)
    assert any("판정자 결정 필요" in x for x in p)


def test_g_p_requires_criterion_labels_and_gt_vocabulary():
    def m(d):
        d["items"][0]["final"]["criteria"] = []
    _bad(m, "FIXTURE_ONLY_g_p.json")

    def m2(d):
        d["items"][0]["final"]["pair_status"] = "CONFIRMED_ELIGIBLE"  # 엔진 상태는 GT 라벨이 아님
    _bad(m2, "FIXTURE_ONLY_g_p.json")


def test_g_c_scoped_needs_unit():
    def m(d):
        d["items"][0]["final"]["country_scope"]["scope_unit"] = "not_applicable"
    _bad(m, "FIXTURE_ONLY_g_c.json")


def test_exported_json_schema_files_match_models():
    for name, schema in export_json_schemas().items():
        on_disk = json.loads((ROOT / "gt" / "schema" / name).read_text(encoding="utf-8"))
        assert on_disk == schema, f"gt/schema/{name}가 모델과 다름: scripts/gt_validate.py --export-schema gt/schema"
