"""잠긴 기준표 검수·패치 체계 (Phase 0-4): 원본 불변, 승인된 패치만 후보 저장소에 적용."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from ctrec import config, store_patch, trial_store
from ctrec.store_patch import PatchError


@pytest.fixture()
def store(tmp_path) -> Path:
    """실제 잠긴 기준표의 복사본 (테스트가 원본을 건드리지 않도록)."""
    d = tmp_path / "parsed"
    shutil.copytree(config.PARSED_DIR, d)
    return d


@pytest.fixture()
def proposals(store) -> dict:
    return store_patch.propose(store_dir=store)


def _patch(proposals, pid) -> dict:
    return next(p for p in proposals["patches"] if p["patch_id"] == pid)


def _approve(tmp_path, proposals, *pids, decision="approved", reviewer="검수자A") -> dict:
    path = tmp_path / "approvals.json"
    for pid in pids:
        store_patch.record_decision(path, _patch(proposals, pid), decision, reviewer)
    return json.loads(path.read_text(encoding="utf-8"))


def _hashes(d: Path) -> dict:
    return {p.name: trial_store.file_hash(p) for p in sorted(d.glob("*.json"))}


AGE = "NCT06878144-I1-restore-age"
AGE2 = "NCT07679893-I1-restore-age"
E4 = "NCT06125405-E4-flag-review"
ORPHAN = "NCT07679893-E26-clear-orphan-unit"


# ------------------------------------------------------------ 제안
def test_proposals_from_frozen_store(proposals):
    by_kind = {}
    for p in proposals["patches"]:
        by_kind.setdefault(p["kind"], []).append(p["patch_id"])
        assert p["status"] == "proposed"
    assert sorted(by_kind["restore_engine_fields"]) == [AGE, AGE2]
    assert by_kind["flag_review"] == [E4]
    assert len(by_kind["clear_orphan_unit"]) == 7  # 단위만 남은 9개 중 나이 복구 2개 제외
    age = _patch(proposals, AGE)
    assert age["after"]["rule"] == {"field": "age", "operator": ">=", "value": "18", "unit": "years",
                                    "structured_evaluable": True}
    assert age["evidence"]["ctgov"] == "minimum_age=18 Years" and age["evidence"]["range_partner"].startswith("I2")
    # 단위 정리는 다른 구조화 필드를 만들지 않음
    for p in proposals["patches"]:
        if p["kind"] == "clear_orphan_unit":
            assert p["after"]["rule"] == {"field": None, "unit": None}
    # E4는 값·단위를 고치지 않고 검토 표시만
    e4 = _patch(proposals, E4)
    assert set(e4["after"]) == {"meta"} and "10^12/L" in e4["after"]["meta"]["review"]


def test_age_restore_not_proposed_without_ctgov_evidence(store, tmp_path):
    trials = tmp_path / "trials"
    shutil.copytree(config.TRIALS_DIR, trials)
    t = json.loads((trials / "NCT06878144.json").read_text(encoding="utf-8"))
    t["minimum_age"] = "21 Years"  # 원문 범위(18)와 CT.gov 하한이 다르면 복구 근거 부족
    (trials / "NCT06878144.json").write_text(json.dumps(t), encoding="utf-8")
    ids = [p["patch_id"] for p in store_patch.propose(store_dir=store, trials_dir=trials)["patches"]]
    assert AGE not in ids and "NCT06878144-I1-clear-orphan-unit" in ids  # 필드는 만들지 않고 단위 정리만 제안


# ------------------------------------------------------------ 승인 상태
def test_unapproved_patches_are_not_applied(store, proposals, tmp_path):
    out = tmp_path / "cand"
    m = store_patch.apply_patches(proposals, None, out, store)
    assert m["applied"] == [] and len(m["skipped_not_approved"]) == len(proposals["patches"])
    assert store_patch.rule_diff(store, out) == []


def test_rejected_and_stale_approvals_are_not_applied(store, proposals, tmp_path):
    approvals = _approve(tmp_path, proposals, AGE, decision="rejected")
    status = store_patch.review_status(proposals, approvals)
    assert status[AGE] == "rejected"
    approvals = _approve(tmp_path, proposals, AGE2)
    edited = copy.deepcopy(proposals)
    _patch(edited, AGE2)["after"]["rule"]["value"] = "41"  # 승인 뒤 내용이 바뀜
    assert store_patch.review_status(edited, approvals)[AGE2] == "stale"
    m = store_patch.apply_patches(edited, approvals, tmp_path / "cand", store)
    assert m["applied"] == []


def test_decision_requires_reviewer(tmp_path, proposals):
    with pytest.raises(PatchError):
        store_patch.record_decision(tmp_path / "a.json", _patch(proposals, AGE), "approved", "  ")


# ------------------------------------------------------------ 후보 저장소에만 적용
def test_approved_patches_applied_to_candidate_only(store, proposals, tmp_path):
    before = _hashes(store)
    approvals = _approve(tmp_path, proposals, AGE, E4, ORPHAN)
    out = tmp_path / "cand"
    m = store_patch.apply_patches(proposals, approvals, out, store)
    assert [a["patch_id"] for a in m["applied"]] == [E4, AGE, ORPHAN]
    assert _hashes(store) == before and store_patch.verify_frozen(store) == []  # 원본 불변
    diff = {(d["trial_id"], d["rule_id"], d["area"], d["key"]): (d["before"], d["after"])
            for d in store_patch.rule_diff(store, out)}
    assert diff == {
        ("NCT06878144", "I1", "rule", "field"): (None, "age"),
        ("NCT06878144", "I1", "rule", "operator"): (None, ">="),
        ("NCT06878144", "I1", "rule", "value"): (None, "18"),
        ("NCT06878144", "I1", "rule", "structured_evaluable"): (False, True),
        ("NCT06125405", "E4", "meta", "review"): (None, _patch(proposals, E4)["after"]["meta"]["review"]),
        ("NCT07679893", "E26", "rule", "unit"): ("L/min", None),
    }
    # 후보는 로드되고, 패치 이력이 기록되며, 패치하지 않은 시험은 원본과 바이트까지 같음
    rec = json.loads((out / "NCT06878144.json").read_text(encoding="utf-8"))
    assert rec["review_patches"][0]["patch_id"] == AGE and rec["review_patches"][0]["reviewer"] == "검수자A"
    assert trial_store.file_hash(out / "NCT00195949.json") == trial_store.file_hash(store / "NCT00195949.json")
    trial = json.loads((config.TRIALS_DIR / "NCT06878144.json").read_text(encoding="utf-8"))
    i1 = next(r for r in trial_store.load(trial, out).rules if r.rule_id == "I1")
    assert (i1.field, i1.operator, i1.value) == ("age", ">=", "18")


def test_all_patches_applied_candidate_passes_check(store, proposals, tmp_path):
    original_problems = store_patch.check_store(store)
    assert len(original_problems) == 9  # 단위만 남은 규칙 9개
    approvals = _approve(tmp_path, proposals, *[p["patch_id"] for p in proposals["patches"]])
    out = tmp_path / "cand"
    store_patch.apply_patches(proposals, approvals, out, store)
    assert store_patch.check_store(out) == []


def test_flagged_rule_is_not_auto_decided(store, proposals, tmp_path):
    """검토 표시한 E4는 원문 값 그대로 두고 규칙 엔진·LLM 자동 판정 대신 의사 확인 항목이 됨."""
    from ctrec.agents import matcher

    approvals = _approve(tmp_path, proposals, E4)
    out = tmp_path / "cand"
    store_patch.apply_patches(proposals, approvals, out, store)
    trial = json.loads((config.TRIALS_DIR / "NCT06125405.json").read_text(encoding="utf-8"))
    parsed = trial_store.load(trial, out)
    e4 = next(r for r in parsed.rules if r.rule_id == "E4")
    assert (e4.operator, e4.value, e4.unit) == ("<", "100", "10^12/L")
    a = matcher._review_assessment(e4, parsed.rule_meta["E4"].review)
    assert a.passes is None and matcher.needs_physician_review(a)


# ------------------------------------------------------------ 거부 (아무것도 쓰지 않음)
def _approved_custom(tmp_path, proposals, patch) -> tuple[dict, dict]:
    proposals = copy.deepcopy(proposals)
    proposals["patches"] = [patch]
    return proposals, _approve(tmp_path, proposals, patch["patch_id"])


@pytest.mark.parametrize("change,needle", [
    ({"trial_id": "NCT99999999"}, "기준표에 없음"),
    ({"rule_id": "I99"}, "규칙 I99가 없음"),
])
def test_unknown_trial_or_rule_rejected(store, proposals, tmp_path, change, needle):
    patch = {**copy.deepcopy(_patch(proposals, AGE)), **change}
    p, a = _approved_custom(tmp_path, proposals, patch)
    out = tmp_path / "cand"
    with pytest.raises(PatchError, match=needle):
        store_patch.apply_patches(p, a, out, store)
    assert not out.exists()


def test_before_value_mismatch_rejected(store, proposals, tmp_path):
    patch = copy.deepcopy(_patch(proposals, ORPHAN))
    patch["before"]["rule"]["unit"] = "mL/min"
    p, a = _approved_custom(tmp_path, proposals, patch)
    with pytest.raises(PatchError, match="기존 값이 다름"):
        store_patch.apply_patches(p, a, tmp_path / "cand", store)


def test_disallowed_key_rejected(store, proposals, tmp_path):
    patch = copy.deepcopy(_patch(proposals, ORPHAN))
    patch["before"]["rule"]["text"] = "Requires more than 6 L/min of oxygen while at rest."
    patch["after"]["rule"]["text"] = "changed"
    p, a = _approved_custom(tmp_path, proposals, patch)
    with pytest.raises(PatchError, match="바꿀 수 없는"):
        store_patch.apply_patches(p, a, tmp_path / "cand", store)


def test_one_bad_patch_writes_nothing(store, proposals, tmp_path):
    bad = copy.deepcopy(_patch(proposals, ORPHAN))
    bad["patch_id"] = "bad"
    bad["before"]["rule"]["unit"] = "wrong"
    p = copy.deepcopy(proposals)
    p["patches"] = [_patch(proposals, AGE), bad]
    a = _approve(tmp_path, p, AGE, "bad")
    out = tmp_path / "cand"
    with pytest.raises(PatchError):
        store_patch.apply_patches(p, a, out, store)
    assert not out.exists()


def test_duplicate_patches_rejected(store, proposals, tmp_path):
    approvals = _approve(tmp_path, proposals, AGE)
    out = tmp_path / "cand"
    store_patch.apply_patches(proposals, approvals, out, store)
    snapshot = _hashes(out)
    with pytest.raises(PatchError, match="이미 이 후보 저장소에 적용"):  # 같은 후보에 다시 적용
        store_patch.apply_patches(proposals, approvals, out, store)
    assert _hashes(out) == snapshot
    p = copy.deepcopy(proposals)
    p["patches"].append(copy.deepcopy(_patch(proposals, AGE)))  # 같은 patch_id 두 번
    with pytest.raises(PatchError, match="두 번"):
        store_patch.apply_patches(p, approvals, tmp_path / "cand2", store)
    twin = copy.deepcopy(_patch(proposals, AGE))
    twin["patch_id"] = "twin"  # 다른 ID로 같은 규칙·키를 다시 바꿈
    p = copy.deepcopy(proposals)
    p["patches"] = [_patch(proposals, AGE), twin]
    a = _approve(tmp_path, p, AGE, "twin")
    with pytest.raises(PatchError):
        store_patch.apply_patches(p, a, tmp_path / "cand3", store)


# ------------------------------------------------------------ 원본 보호
def test_candidate_cannot_be_original_store(store, proposals, tmp_path):
    approvals = _approve(tmp_path, proposals, AGE)
    for target in (store, store / "sub"):
        with pytest.raises(PatchError, match="원본 저장소"):
            store_patch.apply_patches(proposals, approvals, target, store)
    assert store_patch.verify_frozen(store) == []


def test_tampered_original_blocks_apply(store, proposals, tmp_path):
    approvals = _approve(tmp_path, proposals, AGE)
    path = store / "NCT00195949.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert store_patch.verify_frozen(store)
    with pytest.raises(PatchError, match="FROZEN"):
        store_patch.apply_patches(proposals, approvals, tmp_path / "cand", store)


def test_repository_frozen_store_intact():
    assert store_patch.verify_frozen() == []


# ------------------------------------------------------------ E4 검토 표시의 실제 효과 (Matcher 경로)
def _e4_candidate(store, proposals, tmp_path) -> Path:
    approvals = _approve(tmp_path, proposals, E4)  # 테스트용 임시 승인 (임시 디렉터리)
    out = tmp_path / "cand"
    store_patch.apply_patches(proposals, approvals, out, store)
    return out


SRC = "Clinical summary: patient record reviewed."


def _mock_llm(monkeypatch):
    """LLM 대역: 요청받은 모든 규칙을 선정 충족·제외 비해당으로, 원문에 있는 인용과 함께 판정.
    요청받지 않았어도 E4에 '해당 안 됨'을 항상 덧붙여, 검토 표시 규칙이 LLM 답으로 판정되지 않는지 확인."""
    from ctrec.agents import matcher
    from ctrec.schemas import LLMAssessment, MatcherOutput

    def fake(model, system, user, **k):
        rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
        answers = [(r["rule_id"], "yes" if r["kind"] == "inclusion" else "no") for r in rules]
        if "E4" not in {rid for rid, _ in answers}:
            answers.append(("E4", "no"))
        return MatcherOutput(assessments=[
            LLMAssessment(rule_id=rid, applicable=True, holds=holds, evidence_type="explicit", confidence="high",
                          evidence=SRC, reasoning="mock", missing_info=None) for rid, holds in answers])
    monkeypatch.setattr(matcher.llm, "structured", fake)


def _match(parsed, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from test_offline import _profile

    from ctrec.agents import matcher
    _mock_llm(monkeypatch)
    trial = json.loads((config.TRIALS_DIR / "NCT06125405.json").read_text(encoding="utf-8"))
    return matcher.match_trial(_profile(age=10, labs=[]), parsed, trial, source_text=SRC, input_source="clinical_record")


def _load(store_dir: Path):
    trial = json.loads((config.TRIALS_DIR / "NCT06125405.json").read_text(encoding="utf-8"))
    return trial_store.load(trial, store_dir)


def test_flagged_e4_goes_to_review_in_matcher(store, proposals, tmp_path, monkeypatch):
    """검토 표시한 E4: LLM이 '해당 안 됨'이라고 답해도 자동 판정하지 않고 의사 확인 항목, 시험은 CRITERIA_MATCH가 아님."""
    from ctrec.agents import matcher

    m = _match(_load(_e4_candidate(store, proposals, tmp_path)), monkeypatch)
    e4 = next(a for a in m.assessments if a.rule_id == "E4")
    assert e4.passes is None and e4.holds == "unknown" and matcher.needs_physician_review(e4)
    assert "10^12/L" in e4.reasoning
    assert m.eligibility != "ELIGIBLE" and m.match_status != "CRITERIA_MATCH"
    # 대조: 원본(검토 표시 없음)에서는 E4가 LLM 답대로 자동 판정됨
    control = _match(_load(store), monkeypatch)
    assert next(a for a in control.assessments if a.rule_id == "E4").passes is True


def test_flagged_e4_alone_blocks_criteria_match(store, proposals, tmp_path, monkeypatch):
    """실제 시험은 E14(underspecified)로 이미 의료진 검토 상태이므로, E14만 뺀 기준표로 E4 표시의 시험 단위 효과를 확인."""
    flagged, original = _load(_e4_candidate(store, proposals, tmp_path)), _load(store)
    for p in (flagged, original):
        p.rules = [r for r in p.rules if r.rule_id != "E14"]
    assert _match(original, monkeypatch).match_status == "CRITERIA_MATCH"
    m = _match(flagged, monkeypatch)
    assert m.eligibility == "UNCERTAIN" and m.match_status == "NEEDS_REVIEW"


def test_flagged_e4_through_session(store, proposals, tmp_path, monkeypatch):
    """파이프라인(Session.match -> trial_store.get -> matcher)도 후보 기준표의 검토 표시를 따름."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from test_offline import _profile

    from ctrec.agents.answerers import NoAnswerer
    from ctrec.pipeline import PatientCase, Session

    out = _e4_candidate(store, proposals, tmp_path)
    monkeypatch.setattr(config, "PARSED_DIR", out)
    _mock_llm(monkeypatch)
    trial = json.loads((config.TRIALS_DIR / "NCT06125405.json").read_text(encoding="utf-8"))

    class _T:
        def log(self, *a, **k):
            pass

    s = Session(case=PatientCase("P", SRC, input_source="clinical_record"), trials={"NCT06125405": trial},
                answerer=NoAnswerer(), tracer=_T())
    s.profile = _profile(age=10, labs=[])
    m = s.match(["NCT06125405"])[0]
    e4 = next(a for a in m.assessments if a.rule_id == "E4")
    assert e4.passes is None and e4.underspecified and m.match_status != "CRITERIA_MATCH"


# ------------------------------------------------------------ 승인 파일·후보 저장소 무결성
def test_edited_approval_file(store, proposals, tmp_path):
    """승인 파일의 해시를 고치거나 결정을 rejected로 바꾸면 적용되지 않음. reviewer 이름은 감사 기록일 뿐 인증이 아님."""
    approvals = _approve(tmp_path, proposals, AGE)
    forged = copy.deepcopy(approvals)
    forged["decisions"][0]["patch_digest"] = "0" * 64
    assert store_patch.review_status(proposals, forged)[AGE] == "stale"
    later = copy.deepcopy(approvals)
    later["decisions"].append({**approvals["decisions"][0], "decision": "rejected"})  # 마지막 결정이 유효
    assert store_patch.review_status(proposals, later)[AGE] == "rejected"
    assert store_patch.apply_patches(proposals, later, tmp_path / "cand", store)["applied"] == []


def test_candidate_records_file_hashes_and_check_detects_tampering(store, proposals, tmp_path):
    approvals = _approve(tmp_path, proposals, AGE)
    out = tmp_path / "cand"
    m = store_patch.apply_patches(proposals, approvals, out, store)
    assert set(m["files"]) == set(trial_store.frozen_manifest(store)["files"])
    assert m["files"]["NCT06878144"] == trial_store.file_hash(out / "NCT06878144.json")
    assert store_patch.verify_candidate(out) == [] and not any("후보 파일" in p for p in store_patch.check_store(out))
    # 적용 뒤 후보 파일을 손으로 고치면 check가 실패하고, 이어서 적용도 거부
    path = out / "NCT06878144.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    next(r for r in rec["parsed"]["rules"] if r["rule_id"] == "I2")["value"] = "99"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    assert any("NCT06878144: 후보 파일이 적용 기록 이후 바뀜" in p for p in store_patch.check_store(out))
    approvals = _approve(tmp_path, proposals, AGE2)
    with pytest.raises(PatchError, match="기록과 다름"):
        store_patch.apply_patches(proposals, approvals, out, store)
    # 기록에 없는 파일이 끼어들어도 잡음
    path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8"))), encoding="utf-8")
    (out / "EXTRA.json").write_text("{}", encoding="utf-8")
    assert any("EXTRA: 기록에 없는 후보 파일" in p for p in store_patch.verify_candidate(out))
