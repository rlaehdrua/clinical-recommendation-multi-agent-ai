"""P3 질문 계약·명제 사실 장부, P4 결정적 평가기.

평가기 판정 매핑: 확정(holds yes/no) / 미확인(질문 대상) / UNVERIFIED(진료 기록 필요) / 상충(needs_verification).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec.condition_ir import CriterionIR
from ctrec.evaluator import evaluate
from ctrec.fact_store import FactStore
from ctrec.proposition_store import PropositionLedger
from ctrec.question_contract import ContractRegistry, QuestionContract, map_answer, non_answer, render
from ctrec.schemas import ClarifyingQuestion

from contract_fixtures import (CHEMO_IR, PREG_IR, PROXY, QC_CHEMO, QC_PREG, QC_SURGERY, REF, RITUX_IR, RULES, SID,
                               SURG_IR, atom, frozen_dir, ir_dict, sidecar)

PID = "P1"


@pytest.fixture()
def env(tmp_path):
    rules = [RULES["SURG"], RULES["CHEMO"], RULES["PREG"], RULES["RITUX"]]
    d = frozen_dir(tmp_path, rules)
    irs = [ir_dict(RULES["SURG"], d, SURG_IR, anchor_policy={"first_dose": "assessment_proxy"}),
           ir_dict(RULES["CHEMO"], d, CHEMO_IR, anchor_policy={"first_dose": "assessment_proxy"}),
           ir_dict(RULES["PREG"], d, PREG_IR),
           ir_dict(RULES["RITUX"], d, RITUX_IR, anchor_policy={"enrollment": "assessment_proxy"})]
    reg = sidecar(tmp_path, d, irs)
    return {"reg": reg, "contracts": ContractRegistry(reg), "ledger": PropositionLedger(FactStore()),
            "irs": {i["rule_id"]: CriterionIR.from_dict(i) for i in irs}}


def answer(env, cid, ans, *, round_no=1, qid="C1", session=SID, as_of=REF, mutate=None):
    c = env["contracts"].raw(cid)
    q = render(c, qid, ["T1:E1"])
    if mutate:
        mutate(q)
    return env["ledger"].record(PID, session, c, map_answer(c, q, ans), round_no=round_no, question_id=qid,
                                as_of=as_of)


def ev(env, key, *, ref=REF, session=SID, meta="self_report_ok"):
    r = {"SURG": RULES["SURG"], "CHEMO": RULES["CHEMO"], "PREG": RULES["PREG"], "RITUX": RULES["RITUX"]}[key]
    return evaluate(env["irs"][r.rule_id], r, env["ledger"].facts(PID, session), ref, env["contracts"],
                    required_evidence=meta)


# ------------------------------------------------------------ P3 계약
def test_all_fixture_contracts_valid_and_active(env):
    assert {c.contract_id for c in env["contracts"].active()} >= {"QC-SURGERY-LAST", "QC-PREGNANT-NOW"}


def test_render_is_verbatim_contract(env):
    c = env["contracts"].raw("QC-SURGERY-LAST")
    q = render(c, "C1", ["T1:E1", "T2:E5"])
    assert q.question == QC_SURGERY["question"] and q.options == [o["label"] for o in QC_SURGERY["options"]]
    assert q.target == ["T1:E1", "T2:E5"] and q.why.startswith("contract:QC-SURGERY-LAST@v1")


@pytest.mark.parametrize("mutate", [
    lambda q: setattr(q, "question", q.question.replace("수술", "수술 부작용")),
    lambda q: setattr(q, "options", list(reversed(q.options))),
    lambda q: setattr(q, "answer_type", "free_text"),
])
def test_tampered_question_answer_not_recorded(env, mutate):
    assert answer(env, "QC-SURGERY-LAST", "수술을 받은 적 없음", mutate=mutate) == []
    assert env["ledger"].rejected and env["ledger"].facts(PID, SID) == []


def test_contract_content_change_requires_new_review(env):
    root = env["reg"].root
    data = json.loads((root / "question_contracts.json").read_text(encoding="utf-8"))
    data["contracts"][0]["question"] += " (수정)"
    (root / "question_contracts.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert env["contracts"].status("QC-SURGERY-LAST")[0] == "unreviewed"


def test_option_mapping_creates_proposition_facts(env):
    facts = answer(env, "QC-SURGERY-LAST", "4주(28일)보다 전, 3개월(90일) 이내")
    assert {(f.field, f.value) for f in facts} == {("prop:surgery/event/w28d-assessment", False),
                                                   ("prop:surgery/event/w90d-assessment", True)}
    f = facts[0]
    assert f.source_type == "self_report" and f.verification_status == "unknown" and f.session_id == SID
    assert f.source_reference.ref.startswith("contract:QC-SURGERY-LAST@v1#") and f.observed_at == REF


@pytest.mark.parametrize("ans,kind", [("모름", "non_answer"), ("모름(답변 없음)", "non_answer"), ("거부", "non_answer"),
                                      ("한 달쯤 전에 받은 것 같아요", "free_text"), ("4주 이내", "free_text"),
                                      ("최근 4주(28일) 이내 ", "option")])
def test_answer_kinds(env, ans, kind):
    c = env["contracts"].raw("QC-SURGERY-LAST")
    assert map_answer(c, render(c, "C1", []), ans).kind == kind


def test_yes_no_variants(env):
    c = env["contracts"].raw("QC-PREGNANT-NOW")
    q = render(c, "C1", [])
    assert [map_answer(c, q, a).label for a in ("예", "네", "아니요", "아니오", "아뇨")] == \
        ["예", "예", "아니요", "아니요", "아니요"]
    assert map_answer(c, q, "아니요, 그런데 임신 가능성은 있어요").kind == "free_text"


def test_free_text_is_candidate_not_fact(env):
    assert answer(env, "QC-SURGERY-LAST", "작년에 맹장 수술했어요") == []
    assert env["ledger"].candidates and env["ledger"].facts(PID, SID) == []


def test_contract_internal_contradiction_detected():
    c = QuestionContract("QC-X", 1, "q", "single_choice", [
        {"label": "a", "assertions": [["surgery/event/w28d-assessment", True], ["surgery/event/ever", False]]},
        {"label": "b", "assertions": [["surgery/event/ever", False]]}])
    assert any("모순" in e for e in c.errors())
    c2 = QuestionContract("QC-X", 1, "q", "single_choice", [
        {"label": "a", "assertions": [["surgery/event/w28d-first_dose", True]]},
        {"label": "모름", "assertions": [["surgery/event/ever", False]]}])
    errs = c2.errors()
    assert any("assessment" in e for e in errs) and any("미응답" in e for e in errs)
    assert non_answer("") and not non_answer("예")


def test_session_isolation(env):
    answer(env, "QC-SURGERY-LAST", "수술을 받은 적 없음", session="S-other")
    assert env["ledger"].facts(PID, SID) == []
    assert ev(env, "SURG").decided is False


def test_correction_is_explicit_only(env):
    old = answer(env, "QC-PREGNANT-NOW", "아니요")
    answer(env, "QC-PREGNANT-NOW", "예", round_no=2)
    r = ev(env, "PREG")
    assert r.assessment.passes is None and "conflicting_contract_answers" in r.assessment.validation_flags
    # 명시적 정정: 이전 사실을 대체로 표시 (자동 최신 채택이 아님)
    env2 = env["ledger"]
    c = env["contracts"].raw("QC-PREGNANT-NOW")
    q = render(c, "C1", [])
    with pytest.raises(ValueError):
        env2.correct(PID, SID, c, map_answer(c, q, "예"), round_no=3, question_id="C1", as_of=REF,
                     previous=[old[0].fact_id], reason="")
    with pytest.raises(ValueError):
        env2.correct(PID, SID, c, map_answer(c, q, "예"), round_no=3, question_id="C1", as_of=REF,
                     previous=["F-nope"], reason="환자 정정")
    second = [f.fact_id for f in env2.facts(PID, SID) if f.value is True]
    env2.correct(PID, SID, c, map_answer(c, q, "예"), round_no=3, question_id="C1", as_of=REF,
                 previous=[old[0].fact_id], reason="환자가 처음 답을 정정")
    r = ev(env, "PREG")
    assert r.assessment.holds == "yes" and r.assessment.passes is False and second


# ------------------------------------------------------------ P4 평가기
@pytest.mark.parametrize("label,holds,passes", [
    ("수술을 받은 적 없음", "no", True),
    ("3개월(90일)보다 전", "no", True),
    ("4주(28일)보다 전, 3개월(90일) 이내", "no", True),
])
def test_surgery_negative_answers_decide(env, label, holds, passes):
    answer(env, "QC-SURGERY-LAST", label)
    r = ev(env, "SURG")
    assert r.decided and (r.assessment.holds, r.assessment.passes) == (holds, passes)
    assert r.assessment.method == "contract" and r.assessment.evidence_directness == "contract"
    assert r.provenance["contracts"] == ["QC-SURGERY-LAST@v1"] and r.provenance["fact_ids"]


def test_recent_surgery_does_not_imply_major(env):
    """'4주 이내 수술 있음'은 '4주 이내 큰 수술 있음'을 말하지 않음 -> 미확인 (잘못된 제외 방지)."""
    answer(env, "QC-SURGERY-LAST", "최근 4주(28일) 이내")
    r = ev(env, "SURG")
    assert r.decided is False and r.provenance["leaves"][0]["status"] == "missing"


def test_counterexample_b_with_contract_is_eligible_path(env):
    """반례 B: 평생 수술 있음 + 최근 4주 없음 -> 계약 답(3개월보다 전)이면 결정적으로 '해당 없음'."""
    answer(env, "QC-SURGERY-LAST", "3개월(90일)보다 전")
    assert ev(env, "SURG").assessment.passes is True


def test_counterexample_d_pregnancy_ever_does_not_decide_current(env):
    c = env["contracts"].raw("QC-PREGNANT-EVER")
    q = render(c, "C1", [])
    env["ledger"].record(PID, SID, c, map_answer(c, q, "예"), round_no=1, question_id="C1", as_of=REF)
    assert ev(env, "PREG").decided is False


def test_or_logic_pregnant_or_lactating(env):
    answer(env, "QC-PREGNANT-NOW", "아니요")
    assert ev(env, "PREG").decided is False  # 수유 미확인 -> OR 미확정
    answer(env, "QC-LACTATING-NOW", "예", qid="C2")
    r = ev(env, "PREG")
    assert r.assessment.holds == "yes" and r.assessment.passes is False


def test_or_both_no_decides_no(env):
    answer(env, "QC-PREGNANT-NOW", "아니요")
    answer(env, "QC-LACTATING-NOW", "아니요", qid="C2")
    assert ev(env, "PREG").assessment.passes is True


def test_not_and_concept_hierarchy(env):
    """'No rituximab within 6 months' 포함 기준 (NOT은 기준 해석: holds=yes가 '사용 없음'인 형태로 IR을 씀)."""
    rule = RULES["RITUX"]
    ir = CriterionIR.from_dict({**env["irs"]["I1"].to_dict(),
                                "condition": {"op": "not", "arg": RITUX_IR}})
    # 상위 개념 '항CD20 항체 없음' -> 리툭시맙 없음 -> 기준 성립
    c = env["contracts"].raw("QC-ANTICD20-6M")
    env["ledger"].record(PID, SID, c, map_answer(c, render(c, "C1", []), "아니요"), round_no=1, question_id="C1",
                         as_of=REF)
    r = evaluate(ir, rule, env["ledger"].facts(PID, SID), REF, env["contracts"])
    assert r.assessment.holds == "yes" and r.assessment.passes is True


def test_anchor_without_policy_is_undecided(env):
    ir = CriterionIR.from_dict({**env["irs"]["E1"].to_dict(), "anchor_policy": {}})
    answer(env, "QC-SURGERY-LAST", "수술을 받은 적 없음")
    r = evaluate(ir, RULES["SURG"], env["ledger"].facts(PID, SID), REF, env["contracts"])
    assert r.decided is False and r.provenance["leaves"][0]["status"] == "anchor_unresolved"


def test_stale_negative_answer_not_used_after_time_passes(env):
    answer(env, "QC-SURGERY-LAST", "4주(28일)보다 전, 3개월(90일) 이내", as_of="2026-10-01")
    assert ev(env, "SURG", ref="2026-10-24").decided is False  # 답변 뒤 23일 사이 일은 모름
    assert ev(env, "SURG", ref="2026-10-01").assessment.passes is True


def test_positive_answer_shifts_window_forward(env):
    answer(env, "QC-CHEMO-LAST", "최근 4주(28일) 이내", as_of="2026-10-20")
    r = ev(env, "CHEMO", ref="2026-10-24")  # 4주+4일 이내 있음 -> 4주 이내인지는 모름
    assert r.decided is False
    answer(env, "QC-CHEMO-LAST", "최근 4주(28일) 이내", as_of=REF, round_no=2)
    assert ev(env, "CHEMO").assessment.holds == "yes"


def test_future_answer_date_not_used(env):
    answer(env, "QC-PREGNANT-NOW", "예", as_of="2026-11-01")
    assert ev(env, "PREG").decided is False


def test_record_required_gives_unverified_with_self_report_preserved(env):
    answer(env, "QC-PREGNANT-NOW", "아니요")
    answer(env, "QC-LACTATING-NOW", "아니요", qid="C2")
    a = ev(env, "PREG", meta="clinical_record").assessment
    assert a.passes is None and a.needs_verification and a.self_reported_holds == "no"
    assert "contract_self_report_needs_record" in a.validation_flags


def test_partial_ir_never_confirms(env):
    ir = CriterionIR.from_dict({**env["irs"]["E3"].to_dict(), "completeness": "partial",
                                "unsupported_parts": ["to be confirmed on treatment day"]})
    answer(env, "QC-PREGNANT-NOW", "예")
    r = evaluate(ir, RULES["PREG"], env["ledger"].facts(PID, SID), REF, env["contracts"])
    assert r.decided is False and r.provenance["outcome"] == "partial_ir_not_used"


def test_unsupported_node_is_unknown_and_measurement_not_answered(env):
    ir = CriterionIR.from_dict({**env["irs"]["E3"].to_dict(), "completeness": "partial",
                                "condition": {"op": "or", "args": [atom("pregnancy/condition/current"),
                                                                   {"op": "unsupported", "text": "x", "reason": "y"}]}})
    answer(env, "QC-PREGNANT-NOW", "아니요")
    assert evaluate(ir, RULES["PREG"], env["ledger"].facts(PID, SID), REF, env["contracts"]).decided is False


def test_contract_revoked_after_answer_makes_fact_unusable(env):
    answer(env, "QC-SURGERY-LAST", "수술을 받은 적 없음")
    reg = env["reg"]
    reg.record_review("contracts", "QC-SURGERY-LAST", env["contracts"].raw("QC-SURGERY-LAST").digest(), "rejected",
                      reviewer="임상검수자B", note="선택지 경계 재검토", clinical_review_confirmed=False)
    assert ev(env, "SURG").decided is False


def test_applicability_false_passes_only_with_contract_fact(env):
    ir = CriterionIR.from_dict({**env["irs"]["E3"].to_dict(),
                                "applicability": atom("pregnancy/condition/ever")})
    assert evaluate(ir, RULES["PREG"], [], REF, env["contracts"]).decided is False
    c = env["contracts"].raw("QC-PREGNANT-EVER")
    env["ledger"].record(PID, SID, c, map_answer(c, render(c, "C1", []), "아니요"), round_no=1, question_id="C1",
                         as_of=REF)
    a = evaluate(ir, RULES["PREG"], env["ledger"].facts(PID, SID), REF, env["contracts"]).assessment
    assert a.applicable is False and a.passes is True


def test_profile_shadow_facts_never_used(env):
    env["ledger"].store.add(PID, "prop:pregnancy/condition/current", False, source_type="self_report",
                            source_reference={"kind": "input_text", "ref": "profile"}, observed_at=REF,
                            session_id=SID)
    env["ledger"].store.add(PID, "other_fact", "임신 아님", session_id=SID)
    assert env["ledger"].facts(PID, SID) == []
