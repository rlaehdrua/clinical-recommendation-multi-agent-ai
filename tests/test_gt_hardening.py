"""GT 도구 보강 (코드 검토 D-1..D-4). FIXTURE_ONLY 가짜 데이터만 사용. 수치는 엔진 정확도와 무관합니다."""
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gt_eval  # noqa: E402
from gt_lib import freeze as fz  # noqa: E402
from gt_lib.metrics import ENGINE_FORMAT  # noqa: E402

FX = ROOT / "tests" / "data" / "gt_fixture"
FX_U = FX / "FIXTURE_ONLY_g_u.json"

# FIXTURE_ONLY_g_u 항목 전부에 대한 형식상 올바른 엔진 결과 (값 자체는 의미 없음)
FULL_U = {"FX-U-001": {"directness": "conflicting"}, "FX-U-002": {"directness": "direct"},
          "FX-U-003": {"accepted": True}, "FX-U-004": {"accepted": False}, "FX-U-005": {"accepted": False}}


def _engine_file(tmp_path: Path, gt_path: Path, results: dict, name="engine.json") -> Path:
    out = {"format": ENGINE_FORMAT, "gt_set_id": json.loads(gt_path.read_text(encoding="utf-8"))["set_id"],
           "gt_set_sha256": hashlib.sha256(gt_path.read_bytes()).hexdigest(), "level": "U",
           "items": [{"item_id": k, "result": v} for k, v in results.items()]}
    p = tmp_path / name
    p.write_text(json.dumps(out), encoding="utf-8")
    return p


def _frozen_root(tmp_path: Path) -> Path:
    g = tmp_path / "gt"
    (g / "g_u").mkdir(parents=True)
    shutil.copy(FX_U, g / "g_u" / "FIXTURE_ONLY_g_u.json")
    m = fz.build_manifest(g, gt_version="FIXTURE_ONLY-v0", reference_date="2026-11-07")
    fz.write_manifest(g, m)
    assert fz.verify(g) == []
    return g


# --------------------------------------------------------------------------- D-1

def test_d1_unfrozen_gt_file_is_never_official(tmp_path):
    """동결 루트는 깨끗하지만 --gt 파일이 동결 목록에 없으면 공식 결과가 아닙니다."""
    g = _frozen_root(tmp_path)
    raw = json.loads(FX_U.read_text(encoding="utf-8"))
    raw["description"] += " (FIXTURE_ONLY tampered copy, not frozen)"
    other = tmp_path / "unfrozen.json"
    other.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    eng = _engine_file(tmp_path, other, FULL_U)
    rep = gt_eval.run(other, eng, None, g)
    assert rep["official"] is False
    assert "탐색적" in rep["exploratory"]
    assert any("동결 목록에 없" in b for b in rep["official_blockers"])
    # 공식 결과 요청이면 CLI가 거부
    assert gt_eval.main(["--gt", str(other), "--engine", str(eng), "--frozen", str(g), "--official"]) == 1
    # 공식 요청이 아니면 탐색적 결과로 계산 (기존 동작)
    assert gt_eval.main(["--gt", str(other), "--engine", str(eng), "--frozen", str(g)]) == 0


def test_d1_frozen_file_with_clean_manifest_is_official(tmp_path):
    g = _frozen_root(tmp_path)
    gt_path = g / "g_u" / "FIXTURE_ONLY_g_u.json"
    eng = _engine_file(tmp_path, gt_path, FULL_U)
    rep = gt_eval.run(gt_path, eng, None, g)
    assert rep["official_blockers"] == [] and rep["official"] is True and "exploratory" not in rep
    out = tmp_path / "rep.json"
    assert gt_eval.main(["--gt", str(gt_path), "--engine", str(eng), "--frozen", str(g), "--official",
                         "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["official"] is True


def test_d1_official_refused_when_manifest_fails_or_no_frozen(tmp_path):
    g = _frozen_root(tmp_path)
    gt_path = g / "g_u" / "FIXTURE_ONLY_g_u.json"
    eng = _engine_file(tmp_path, gt_path, FULL_U)
    # 동결 목록 밖 파일이 생기면 verify 실패 -> 공식 아님
    (g / "g_u" / "sneaked_in.json").write_text("{}", encoding="utf-8")
    rep = gt_eval.run(gt_path, eng, None, g)
    assert rep["official"] is False and rep["frozen"]["problems"]
    assert gt_eval.main(["--gt", str(gt_path), "--engine", str(eng), "--frozen", str(g), "--official"]) == 1
    # --frozen 없이 --official 요청 -> 거부
    assert gt_eval.main(["--gt", str(gt_path), "--engine", str(eng), "--official"]) == 1


def test_d1_manifest_hash_of_non_set_file_does_not_count(tmp_path):
    """GT 파일 해시가 세트 폴더(g_u/g_c/g_p)가 아닌 항목(예: protocol/)으로 등록되어 있으면 공식 아님."""
    g = tmp_path / "gt"
    (g / "g_u").mkdir(parents=True)
    (g / "protocol").mkdir()
    shutil.copy(FX_U, g / "protocol" / "copy.json")
    real = json.loads(FX_U.read_text(encoding="utf-8"))
    real["description"] += " (FIXTURE_ONLY second set)"
    (g / "g_u" / "other.json").write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
    fz.write_manifest(g, fz.build_manifest(g, gt_version="FIXTURE_ONLY-v0", reference_date="2026-11-07"))
    gt_path = g / "protocol" / "copy.json"
    eng = _engine_file(tmp_path, gt_path, FULL_U)
    rep = gt_eval.run(gt_path, eng, None, g)
    assert rep["official"] is False


# --------------------------------------------------------------------------- D-2

def _gu_gt():
    from gt_lib.models import GTSet
    return GTSet.model_validate(json.loads(FX_U.read_text(encoding="utf-8")))


def _eng(gt, results):
    return {"format": ENGINE_FORMAT, "gt_set_id": gt.set_id, "level": "U",
            "items": [{"item_id": k, "result": v} for k, v in results.items()]}


def test_d2_missing_engine_item_blocks_official_result(tmp_path):
    """빠진 항목은 coverage에만 남고 공식 결과가 될 수 없습니다 (거짓 확정 항목을 빼서 A1=0을 만드는 것을 막음)."""
    from gt_lib.metrics import evaluate
    gt = _gu_gt()
    res = dict(FULL_U)
    res.pop("FX-U-002")
    rep = evaluate(gt, _eng(gt, res))
    assert rep["coverage"]["complete"] is False
    assert any("FX-U-002" in b for b in rep["official_blockers"])
    # 동결 조건을 모두 맞춰도 커버리지가 100%가 아니면 공식이 아님
    g = _frozen_root(tmp_path)
    gt_path = g / "g_u" / "FIXTURE_ONLY_g_u.json"
    eng = _engine_file(tmp_path, gt_path, res)
    r = gt_eval.run(gt_path, eng, None, g)
    assert r["official"] is False and "탐색적" in r["exploratory"]
    assert gt_eval.main(["--gt", str(gt_path), "--engine", str(eng), "--frozen", str(g), "--official"]) == 1


def test_d2_full_coverage_has_no_coverage_blocker():
    from gt_lib.metrics import evaluate
    gt = _gu_gt()
    rep = evaluate(gt, _eng(gt, FULL_U))
    assert rep["coverage"]["complete"] is True and rep["official_blockers"] == []


def test_d2_extra_or_incomplete_baseline_blocks_official():
    from gt_lib.metrics import evaluate
    gt = _gu_gt()
    rep = evaluate(gt, _eng(gt, dict(FULL_U, **{"NOT-IN-GT": {"accepted": True}})))
    assert any("NOT-IN-GT" in b for b in rep["official_blockers"])
    base = dict(FULL_U)
    base.pop("FX-U-003")
    rep = evaluate(gt, _eng(gt, FULL_U), _eng(gt, base))
    assert any("기준선" in b and "FX-U-003" in b for b in rep["official_blockers"])


@pytest.mark.parametrize("item_id,bad", [
    ("FX-U-003", {}),                              # accepted 키 없음 (예전: 조용히 보류로 셈)
    ("FX-U-003", {"accepted": "yes"}),             # bool 아님
    ("FX-U-003", {"accepted": None}),
    ("FX-U-005", {"grounding": None}),             # ambiguous 항목도 검사
    ("FX-U-001", {}),                              # directness 없음
    ("FX-U-001", {"directness": None}),
    ("FX-U-001", {"directness": ["direct"]}),
    ("FX-U-001", "direct"),                        # result가 dict가 아님
])
def test_d2_malformed_engine_record_is_error(item_id, bad):
    from gt_lib.metrics import evaluate
    gt = _gu_gt()
    with pytest.raises(ValueError):
        evaluate(gt, _eng(gt, dict(FULL_U, **{item_id: bad})))
    # 기준선도 같은 검사
    with pytest.raises(ValueError):
        evaluate(gt, _eng(gt, FULL_U), _eng(gt, dict(FULL_U, **{item_id: bad})))


def test_d2_structurally_broken_engine_items_are_errors():
    from gt_lib.metrics import evaluate
    gt = _gu_gt()
    for items in ([{"result": {"accepted": True}}],                                  # item_id 없음
                  [{"item_id": "FX-U-003"}],                                          # result 없음
                  [{"item_id": "FX-U-003", "result": {"accepted": True}}] * 2,        # 중복
                  "not-a-list"):
        eng = {"format": ENGINE_FORMAT, "gt_set_id": gt.set_id, "level": "U", "items": items}
        with pytest.raises(ValueError):
            evaluate(gt, eng)


def test_d2_pair_and_criterion_records_validated():
    from gt_lib.metrics import evaluate
    from gt_lib.models import GTSet
    gp = GTSet.model_validate(json.loads((FX / "FIXTURE_ONLY_g_p.json").read_text(encoding="utf-8")))
    ok = {r.item_id: {"pair_status": "INFO_MISSING"} for r in gp.items}
    assert evaluate(gp, _eng(gp, ok))["official_blockers"] == []
    first = gp.items[0].item_id
    for bad in ({}, {"pair_status": "INFO_MISSING", "deciding_criteria": "I1"}):
        with pytest.raises(ValueError):
            evaluate(gp, _eng(gp, dict(ok, **{first: bad})))
    gc = GTSet.model_validate(json.loads((FX / "FIXTURE_ONLY_g_c.json").read_text(encoding="utf-8")))
    with pytest.raises(ValueError):
        evaluate(gc, _eng(gc, {r.item_id: {"scope_represented": "yes"} for r in gc.items}))


# --------------------------------------------------------------------------- D-3

def _raw_u():
    return json.loads(FX_U.read_text(encoding="utf-8"))


def _fp(d, **kw):
    from gt_lib.models import GTSet, freeze_problems
    return freeze_problems(GTSet.model_validate(d), allow_fixture=True, **kw)


def _adjudicate(d, idx=1, log_id="FIXTURE-ADJ-0009"):
    """항목 idx를 adjudicated로 바꾸고 맞는 판정 기록 행을 돌려줌 (FIXTURE_ONLY 값)."""
    it = d["items"][idx]
    it["adjudication"] = {"status": "adjudicated", "adjudicator_id": "FIXTURE_ADJ",
                          "adjudicated_at": "2026-11-08T00:00:00+00:00", "reason": "FIXTURE_ONLY",
                          "log_ref": log_id}
    return {"log_id": log_id, "item_id": it["item_id"], "layer": "G-U", "status": "adjudicated",
            "adjudicator_id": "FIXTURE_ADJ", "final_label_json": json.dumps(it["final"])}


AMB_ROW = {"log_id": "FIXTURE-ADJ-0001", "item_id": "FX-U-005", "status": "ambiguous"}


def test_d3_agreed_final_must_equal_annotations():
    d = _raw_u()
    assert _fp(d) == []
    it = d["items"][2]  # 두 라벨러 모두 같은 라벨, final만 다르게
    it["final"]["category"] = "FIXTURE_ONLY_flipped_category"
    assert any("FX-U-003" in p and "final" in p for p in _fp(d))
    d = _raw_u()
    d["items"][0]["final"]["concept_cluster"] = "FIXTURE_other_cluster"
    assert any("FX-U-001" in p and "final" in p for p in _fp(d))


def test_d3_adjudicated_requires_matching_log_row():
    d = _raw_u()
    row = _adjudicate(d)
    assert _fp(d, adjudication_log=[row, AMB_ROW]) == []
    # 판정 기록을 주지 않음
    assert any("FX-U-002" in p and "adjudication_log" in p for p in _fp(d))
    # log_ref가 기록에 없음
    assert any("FX-U-002" in p for p in _fp(d, adjudication_log=[AMB_ROW]))
    # 기록의 최종 라벨이 final과 다름
    bad = dict(row, final_label_json=json.dumps(dict(d["items"][1]["final"], category="FIXTURE_ONLY_x")))
    assert any("FX-U-002" in p for p in _fp(d, adjudication_log=[bad, AMB_ROW]))
    # 기록 행이 다른 항목을 가리킴
    assert any("FX-U-002" in p for p in _fp(d, adjudication_log=[dict(row, item_id="FX-U-001"), AMB_ROW]))
    # 기록 status가 adjudicated가 아님
    assert any("FX-U-002" in p for p in _fp(d, adjudication_log=[dict(row, status="ambiguous"), AMB_ROW]))
    # log_ref 없음
    d["items"][1]["adjudication"]["log_ref"] = None
    assert any("FX-U-002" in p and "log_ref" in p for p in _fp(d, adjudication_log=[row, AMB_ROW]))


def test_d3_ambiguous_log_ref_must_exist_when_log_given():
    d = _raw_u()
    assert _fp(d, adjudication_log=[AMB_ROW]) == []
    assert any("FX-U-005" in p for p in _fp(d, adjudication_log=[]))


def test_d3_empty_set_does_not_freeze():
    d = _raw_u()
    d["items"] = []
    assert any("항목" in p and "없" in p for p in _fp(d))


@pytest.mark.parametrize("where,text", [
    ("source.detail", "engine said: confirmed_eligible"),
    ("annotation.note", "EngineStatus=CONFIRMED_ELIGIBLE copied"),
    ("annotation.note", "looked like PairStatus.CONFIRMED_INELIGIBLE"),
    ("adjudication.reason", "Matcher returned MatchStatus.ELIGIBLE"),
    ("final.category", "FIXTURE_ONLY_ VERIFICATION_PENDING _case"),
    ("source.detail", "copied from engine_status column"),
    ("annotation.note", "engine output was likely_eligible"),
    ("description", "set mirrors ExpertReview queue"),
])
def test_d3_engine_status_strings_in_free_text_are_leaks(where, text):
    from gt_lib.leak import Corpus, check_set
    d = _raw_u()
    it = d["items"][0]
    if where == "source.detail":
        it["source"]["detail"] = text
    elif where == "annotation.note":
        it["annotations"][0]["note"] = text
    elif where == "adjudication.reason":
        it = d["items"][4]
        it["adjudication"]["reason"] = text
    elif where == "final.category":
        it["final"]["category"] = text
    else:
        d["description"] = text
    hits = [r for r in check_set(d, Corpus(), allow_fixture=True) if r["kind"] == "engine_field"]
    assert hits, text


def test_d3_gt_vocabulary_and_plain_words_are_not_engine_leaks():
    from gt_lib.leak import Corpus, check_set
    d = _raw_u()
    d["items"][0]["annotations"][0]["note"] = ("Labeller thinks the patient is ELIGIBLE / INELIGIBLE depends on "
                                               "the document; needs expert review; not evaluated by labeller B")
    assert [r for r in check_set(d, Corpus(), allow_fixture=True) if r["kind"] == "engine_field"] == []


def test_d3_freeze_cli_checks_adjudication_log(tmp_path):
    import gt_freeze
    from gt_lib.labelio import ADJ_COLS, write_csv
    g = tmp_path / "gt"
    (g / "g_u").mkdir(parents=True)
    d = _raw_u()
    row = _adjudicate(d)
    (g / "g_u" / "FIXTURE_ONLY_g_u.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    freeze = lambda v: gt_freeze.main(["freeze", "--gt-root", str(g), "--version", v,  # noqa: E731
                                       "--reference-date", "2026-11-07", "--allow-fixture"])
    assert freeze("FIXTURE_ONLY-v0") == 1  # 판정 기록 없음
    bad = dict(row, final_label_json=json.dumps(dict(d["items"][1]["final"], category="FIXTURE_ONLY_x")))
    write_csv(g / "adjudication_log.csv", ADJ_COLS, [bad, AMB_ROW])
    assert freeze("FIXTURE_ONLY-v0") == 1  # 기록의 최종 라벨이 final과 다름
    assert not (g / "FROZEN_GT.json").exists()
    write_csv(g / "adjudication_log.csv", ADJ_COLS, [row, AMB_ROW])
    assert freeze("FIXTURE_ONLY-v0") == 0


def test_d3_freeze_cli_refuses_empty_set(tmp_path):
    import gt_freeze
    g = tmp_path / "gt"
    (g / "g_u").mkdir(parents=True)
    d = _raw_u()
    d["items"] = []
    (g / "g_u" / "FIXTURE_ONLY_g_u.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert gt_freeze.main(["freeze", "--gt-root", str(g), "--version", "FIXTURE_ONLY-v0",
                           "--reference-date", "2026-11-07", "--allow-fixture"]) == 1
    assert not (g / "FROZEN_GT.json").exists()


# --------------------------------------------------------------------------- D-4

HELD_KO = "간질성 폐질환 병력 없음. 흉부 CT상 간질성 폐질환 의심 소견 있음."


def test_d4_korean_spacing_variant_is_a_leak():
    from gt_lib.leak import Corpus
    c = Corpus()
    c.add("FIXTURE_ONLY_corpus", HELD_KO)
    assert c.match(HELD_KO)
    assert c.match(HELD_KO.replace("간질성 폐질환", "간질성폐질환"))          # 띄어쓰기 제거
    assert c.match(HELD_KO.replace("흉부 CT상", "흉부CT 상"))              # 띄어쓰기 위치 이동
    # 복사한 문장을 새 문장 안에 넣고 띄어쓰기만 바꿈
    assert c.match("새로 쓴 문장입니다. " + HELD_KO.replace("의심 소견", "의심소견") + " 추가 문장도 있습니다.")
    # 관계없는 한국어 문장은 통과
    assert c.match("환자는 어제 산책을 했고 오늘은 식사를 잘 했습니다.") == []


def _repo_copy(tmp_path: Path) -> Path:
    rr = tmp_path / "repo"
    for rel in fz.METRIC_DEFINITION_FILES:
        (rr / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, rr / rel)
    return rr


@pytest.mark.parametrize("rel", ["scripts/gt_eval.py", "scripts/gt_engine_adapter.py", "scripts/gt_lib/labelio.py",
                                 "scripts/gt_lib/models.py", "scripts/gt_lib/metrics.py", "scripts/gt_lib/stats.py"])
def test_d4_evaluation_code_is_hashed_and_changes_detected(tmp_path, rel):
    rr = _repo_copy(tmp_path)
    g = tmp_path / "gt"
    (g / "g_u").mkdir(parents=True)
    shutil.copy(FX_U, g / "g_u" / "FIXTURE_ONLY_g_u.json")
    m = fz.build_manifest(g, gt_version="FIXTURE_ONLY-v0", reference_date="2026-11-07", repo_root=rr)
    fz.write_manifest(g, m)
    assert rel in m["metric_definitions"]
    assert fz.verify(g, repo_root=rr) == []
    (rr / rel).write_text((rr / rel).read_text(encoding="utf-8") + "\n# changed after freeze\n", encoding="utf-8")
    assert any(rel in p for p in fz.verify(g, repo_root=rr))


def test_d4_advance_runs_verify_first(tmp_path):
    import gt_freeze
    g = _frozen_root(tmp_path)
    p = g / "g_u" / "FIXTURE_ONLY_g_u.json"
    p.write_bytes(p.read_bytes().replace(b"FIXTURE_ONLY", b"FIXTURE_ONLY", 1) + b" ")  # 한 바이트 변조
    rep = tmp_path / "report.json"
    rep.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        fz.advance(g, "measured_once", reason="FIXTURE", measurement=rep, engine_commit="abc")
    assert gt_freeze.main(["advance", "--gt-root", str(g), "--to", "regression", "--reason", "x"]) == 1
    assert json.loads((g / fz.FROZEN_NAME).read_text(encoding="utf-8"))["status"] == "sealed"
