"""P6 우선순위 기준 IR·질문 계약 초안 검증.

- 저장소 contracts/의 초안은 모두 검수 전(unreviewed)이고 실제 판정 경로에서 활성화되지 않음
- 초안은 잠긴 기준표(data/parsed, 읽기만)의 해시·원문과 일치
- 기준별 긍정·부정·미확인 사례와 기간 경계: tmp 사이드카에 '가정 승인'(기준 시점 근사 포함)을 적용해
  IR이 의도대로 평가되는지만 확인 (저장소 초안의 승인이 아님)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config
from ctrec.condition_ir import CriterionIR, walk
from ctrec.evaluator import evaluate, leaf_value
from ctrec.fact_store import FactStore
from ctrec.ir_review import Registry
from ctrec.proposition_store import PropositionLedger
from ctrec.propositions import Proposition, TimeScope
from ctrec.question_contract import ContractRegistry, map_answer, render
from ctrec.schemas import Rule

from contract_fixtures import REF, REVIEWER

ROOT = Path(__file__).resolve().parent.parent
DRAFTS = ROOT / "contracts"
CASES = json.loads((DRAFTS / "ir_cases.json").read_text(encoding="utf-8"))["criteria"]
SID = "S-p6"


def _all_irs() -> list[CriterionIR]:
    return [CriterionIR.from_dict(c) for p in sorted((DRAFTS / "ir").glob("*.json"))
            for c in json.loads(p.read_text(encoding="utf-8"))["criteria"]]


IRS = {ir.key: ir for ir in _all_irs()}


def _rule(ir: CriterionIR) -> Rule:
    frozen = json.loads((config.PARSED_DIR / f"{ir.trial_id}.json").read_text(encoding="utf-8"))
    return Rule(**next(r for r in frozen["parsed"]["rules"] if r["rule_id"] == ir.rule_id))


# ------------------------------------------------------------ 저장소 초안 상태
def test_drafts_in_sync_with_generator():
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_contract_drafts.py"), "--check"],
                       capture_output=True, text=True, encoding="utf-8", env={**__import__("os").environ,
                                                                             "PYTHONDONTWRITEBYTECODE": "1",
                                                                             "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 0, p.stdout + p.stderr


def test_draft_count_and_no_review_records():
    assert len(IRS) == 57 and len(CASES) == 57
    assert not (DRAFTS / "reviews.json").exists()  # 사람 검수 기록 없음 -> 아무것도 활성화되지 않음


def test_every_repo_draft_matches_frozen_store_but_is_inactive():
    reg = Registry()  # 저장소 contracts/ + 실제 data/parsed (읽기만)
    statuses = {ir.key: reg.ir_record(ir.trial_id, ir.rule_id) for ir in IRS.values()}
    assert {k: (r.status, r.reasons) for k, r in statuses.items() if r.status != "unreviewed"} == {}
    assert all(reg.active_irs(t) == {} for t in reg.trial_ids())
    assert all(ir.review_status == "draft" for ir in IRS.values())


def test_every_repo_contract_valid_but_inactive():
    cr = ContractRegistry(Registry())
    ids = [c["contract_id"] for c in json.loads((DRAFTS / "question_contracts.json").read_text(encoding="utf-8"))
           ["contracts"]]
    assert len(ids) == len(set(ids)) == 49
    assert {i: cr.status(i) for i in ids if cr.status(i)[0] != "unreviewed"} == {}
    assert cr.active() == []


def test_drafts_never_lower_evidence_requirement_and_keep_text():
    for ir in IRS.values():
        r = _rule(ir)
        meta = json.loads((config.PARSED_DIR / f"{ir.trial_id}.json").read_text(encoding="utf-8"))["rule_meta"]
        assert ir.criterion_text == r.text and ir.source_span in r.text
        if meta[ir.rule_id]["required_evidence"] == "clinical_record":
            assert ir.source_requirement == "clinical_record"


def test_drafts_set_no_anchor_policy_and_document_unsupported():
    for ir in IRS.values():
        assert ir.anchor_policy == {}  # 기준 시점 근사는 팀 결정 후 검수자가 추가
        has_u = any(n["op"] == "unsupported" for n in walk(ir.condition))
        assert (ir.completeness == "partial") == bool(has_u or ir.unsupported_parts), ir.key
        if ir.anchors() - {"assessment"}:
            assert "기준 시점" in ir.notes, ir.key


def test_repo_drafts_do_not_change_session_outcome(tmp_path):
    """저장소 초안이 있어도(미승인) 결정적 평가에 쓰이지 않음: 활성 IR 0개."""
    reg = Registry()
    assert sum(len(reg.active_irs(t)) for t in reg.trial_ids()) == 0


# ------------------------------------------------------------ 가정 승인 아래 IR 의도 확인
@pytest.fixture(scope="module")
def hypo(tmp_path_factory):
    """tmp 사이드카: 초안 복사 + 모든 기준 시점 근사(가정) + 사실 키별 단일 계약. 승인은 tmp에만 기록."""
    tmp = tmp_path_factory.mktemp("p6")
    root = tmp / "contracts"
    (root / "ir").mkdir(parents=True)
    for p in (DRAFTS / "ir").glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        for c in data["criteria"]:
            c["anchor_policy"] = {a: "assessment_proxy" for a in CriterionIR.from_dict(c).anchors() - {"assessment"}}
        (root / "ir" / p.name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    keys = sorted({k for v in CASES.values() for case in v["cases"] for k, _ in case["facts"]}
                  | {_boundary_key(ir, n, d) for ir, n, _, _ in _windows() for d in (0, 1, -1)})
    contracts = [{"contract_id": f"QC-T{i}", "version": 1, "question": f"[test] {k}", "answer_type": "yes_no",
                  "options": [{"label": "예", "assertions": [[k, True]]}, {"label": "아니요", "assertions": [[k, False]]}]}
                 for i, k in enumerate(keys)]
    shutil.copy(DRAFTS / "question_contracts.json", root / "draft_contracts.json")
    (root / "question_contracts.json").write_text(json.dumps({"contracts": contracts}, ensure_ascii=False),
                                                  encoding="utf-8")
    reg = Registry(root)
    for t in reg.trial_ids():
        for rid, rec in reg.trial_records(t).items():
            assert rec.status == "unreviewed", (t, rid, rec.reasons)
            reg.record_review("ir", f"{t}:{rid}", rec.ir.digest(), "approved", reviewer=REVIEWER,
                              note="P6 테스트용 가정 승인 (tmp)", clinical_review_confirmed=True)
    cr = ContractRegistry(reg)
    for c in contracts:
        reg.record_review("contracts", c["contract_id"], cr.raw(c["contract_id"]).digest(), "approved",
                          reviewer=REVIEWER, note="P6 테스트용", clinical_review_confirmed=True)
    return {"reg": reg, "cr": cr, "by_key": {k: f"QC-T{i}" for i, k in enumerate(keys)}}


def _facts(hypo, facts: list) -> list:
    ledger = PropositionLedger(FactStore())
    for i, (k, v) in enumerate(facts):
        c = hypo["cr"].raw(hypo["by_key"][k])
        q = render(c, f"C{i + 1}", [])
        ledger.record("P6", SID, c, map_answer(c, q, "예" if v else "아니요"), round_no=1, question_id=f"C{i + 1}",
                      as_of=REF)
    return ledger.facts("P6", SID)


@pytest.mark.parametrize("key", sorted(CASES))
def test_positive_negative_unknown_cases(hypo, key):
    trial, rid = key.split(":")
    rec = hypo["reg"].ir_record(trial, rid)
    assert rec.active
    ir, rule = rec.ir, _rule(rec.ir)
    req = ir.source_requirement
    for case in CASES[key]["cases"]:
        r = evaluate(ir, rule, _facts(hypo, case["facts"]), REF, hypo["cr"], required_evidence=req)
        exp = case["expect"]
        where = f"{key} {case}"
        if ir.completeness == "partial":
            assert r.decided is False, where  # 부분 IR은 확정에 쓰지 않음
            if exp in ("yes", "no"):
                assert r.provenance["provisional"]["holds"] is (exp == "yes"), where
            continue
        if exp == "unknown":
            assert r.decided is False, where
        elif req == "clinical_record":
            a = r.assessment
            assert a.passes is None and a.needs_verification and a.self_reported_holds == exp, where
        else:
            assert (r.assessment.holds, r.assessment.method) == (exp, "contract"), where


def _windows():
    for ir in IRS.values():
        for n in walk(ir.condition):
            if n.get("op") == "atom":
                p = Proposition.parse(n["prop"])
                if p.scope.kind == "window":
                    yield ir, n["prop"], p, p.scope.days


def _boundary_key(ir, prop_key, delta):
    p = Proposition.parse(prop_key)
    return p.with_scope(TimeScope("window", p.scope.days + delta)).key


@pytest.mark.parametrize("ir_key,prop_key", sorted({(ir.key, k) for ir, k, _, _ in _windows()}))
def test_window_boundaries(hypo, ir_key, prop_key):
    """경계: 기간 N일 '있음' -> 참, N+1일 '있음' -> 모름, N일 '없음' -> 거짓, N-1일 '없음' -> 모름."""
    trial, rid = ir_key.split(":")
    ir = hypo["reg"].ir_record(trial, rid).ir
    target = Proposition.parse(prop_key)
    for delta, val, expect in ((0, True, True), (1, True, None), (0, False, False), (-1, False, None)):
        k = _boundary_key(ir, prop_key, delta)
        lf = leaf_value(target, ir, _facts(hypo, [[k, val]]), REF, hypo["cr"])
        assert lf.value is expect, (ir_key, k, val, lf)


def test_contract_candidates_can_decide_some_atom():
    """기준별 질문 계약 후보는 그 IR의 명제 중 하나 이상을 코드 함의로 정할 수 있어야 함 (가정 승인 하에서)."""
    from ctrec.propositions import entails
    contracts = {c["contract_id"]: c for c in
                 json.loads((DRAFTS / "question_contracts.json").read_text(encoding="utf-8"))["contracts"]}
    for key, v in CASES.items():
        ir = IRS[key]
        atoms = []
        for n in walk(ir.condition):
            if n.get("op") == "atom":
                p = Proposition.parse(n["prop"])
                if p.scope.kind == "window":
                    p = p.with_scope(TimeScope("window", p.scope.days))
                atoms.append(p)
        for cid in v["contract_candidates"]:
            opts = contracts[cid]["options"]
            ok = any(entails(Proposition.parse(k), val, a) is not None
                     for o in opts for k, val in o["assertions"] for a in atoms)
            assert ok, (key, cid)


def test_review_checklist_in_sync_and_complete():
    """검수표(docs/review/contract_draft_review.md)가 초안과 맞고, 모든 IR에 사전 분류가 있음 (승인 아님)."""
    import os
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "report_contract_drafts.py"), "--check"],
                       capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 0, p.stdout + p.stderr
