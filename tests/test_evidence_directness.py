"""근거 직접성 검증 (evidence_directness.py, matcher._enforce_directness).

출처(기록/답변)·근거 직접성(직접/간접/추측/상충)·검증 상태(사실 저장소)·판정을 구분합니다.
LLM 대역은 결정적이므로, 여기의 반복 실행은 코드 경로의 결정성만 보이며 실제 Claude의 재현성을 뜻하지 않습니다.

E9: A′ 평가 원본 출력은 저장소에 없어 원본 재현이 아니라, Frozen Store의 NCT06833073 E9 규칙(읽기 전용)으로 만든 합성 재현입니다.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import evidence_directness as ed, profile_facts, trial_store
from ctrec.agents import matcher
from ctrec.fact_store import FactStore
from ctrec.schemas import CriterionAssessment, Fact, LLMAssessment, MatcherOutput, TrialMatch

from test_decisions import TRIAL, _parsed
from test_offline import _profile, _rule

ROOT = Path(__file__).resolve().parent.parent
REC = "clinical_record"
INIT_CT = "62세 남성, 방광암(NMIBC). 2026-07-02 조영 CT를 받은 적 있음."
Q_ALLERGY = "\n\n[추가 확인 정보 - 라운드 1]\nQ: 조영제에 알레르기가 있나요?\nA: "


def _e9():
    """Frozen Store의 NCT06833073 E9 규칙과 메타(clinical_record)만 담은 사본 시험 (저장소는 읽기만)."""
    trial = json.loads((ROOT / "data/trials/NCT06833073.json").read_text(encoding="utf-8"))
    full = trial_store.load(trial)
    rule = next(r for r in full.rules if r.rule_id == "E9")
    one = full.model_copy(update={"rules": [rule], "cohorts": []})
    one.rule_meta = {"E9": full.rule_meta["E9"]}
    return one, trial


def _llm(monkeypatch, *verdicts):
    """(rule_id, holds, evidence_type, evidence[, applicable]) 목록을 그대로 돌려주는 LLM 대역."""
    out = [LLMAssessment(rule_id=v[0], applicable=v[4] if len(v) > 4 else True, holds=v[1], evidence_type=v[2],
                         confidence="high", evidence=v[3], reasoning="scripted", missing_info=None) for v in verdicts]
    monkeypatch.setattr(matcher.llm, "structured", lambda *a, **k: MatcherOutput(assessments=out))


def _match_e9(monkeypatch, text, holds, quote, input_source=REC):
    parsed, trial = _e9()
    _llm(monkeypatch, ("E9", holds, "explicit", quote))
    m = matcher.match_trial(_profile(age=62, sex="male", ecog=None), parsed, trial, reference_date="2026-10-24",
                            source_text=text, input_source=input_source)
    return m, m.assessments[0]


# ------------------------------------------------------------ E9 (합성 재현)
def test_e9_ct_history_does_not_confirm_no_contrast_allergy(monkeypatch):
    """A: 조영 CT 이력만 있음. LLM이 explicit로 '금기 없음'을 내도 간접 추론이라 확정하지 않음 (수정 전: CRITERIA_MATCH)."""
    m, a = _match_e9(monkeypatch, INIT_CT, "no", "조영 CT를 받은 적 있음")
    assert a.holds == "unknown" and a.passes is None
    assert a.evidence_directness == "indirect" and a.validation_flags == ["indirect_evidence"]
    assert a.llm_judgment == {"holds": "no", "applicable": True, "evidence_type": "explicit", "confidence": "high"}
    assert a.evidence_source == "none"  # 확정 판정이 없으므로 '기록 근거'로 표시하지 않음
    assert m.eligibility == "UNCERTAIN" and m.match_status == "INSUFFICIENT_INFORMATION"
    assert matcher.is_askable_unknown(a)  # 부적격이 아니라 확인 질문 대상


def test_e9_direct_record_statement_is_confirmed(monkeypatch):
    """B: 의료기록에 '조영제 알레르기 없음'이 직접 기재 -> 직접 근거, 출처 record (검증 상태와는 별개)."""
    m, a = _match_e9(monkeypatch, INIT_CT + "\n의무기록: 조영제 알레르기 없음.", "no", "조영제 알레르기 없음")
    assert a.passes is True and a.evidence_directness == "direct" and a.evidence_source == "record"
    assert m.match_status == "CRITERIA_MATCH"


def test_e9_patient_guess_is_not_confirmed(monkeypatch):
    """C: '알레르기는 없는 것 같아요' -> 추측. 답변상 충족(POTENTIAL_MATCH)으로도 표시하지 않음 (수정 전: POTENTIAL_MATCH)."""
    m, a = _match_e9(monkeypatch, INIT_CT + Q_ALLERGY + "알레르기는 없는 것 같아요.", "no", "알레르기는 없는 것 같아요")
    assert a.passes is None and a.evidence_directness == "hedged" and a.self_reported_holds is None
    assert m.match_status == "INSUFFICIENT_INFORMATION"


CONFLICT = ("62세 남성 방광암.\n2024-03 기록: 조영제 알레르기 없음.\n"
            "2026-09 기록: 조영 CT 후 두드러기, 조영제 알레르기 의심.")


@pytest.mark.parametrize("holds,quote", [("no", "2024-03 기록: 조영제 알레르기 없음"), ("yes", "조영제 알레르기 의심"),
                                         ("yes", "조영 CT 후 두드러기")])
def test_e9_conflicting_records_go_to_record_check(monkeypatch, holds, quote):
    """D: 과거 '없음' / 최신 '의심' -> 어느 쪽을 인용해도 확정하지 않고 기록 확인(UNVERIFIED). 부적격으로도 바꾸지 않음."""
    m, a = _match_e9(monkeypatch, CONFLICT, holds, quote)
    assert a.passes is None and a.evidence_directness == "conflicting" and a.needs_verification
    assert a.self_reported_holds is None and m.eligibility == "UNCERTAIN"
    assert m.match_status == "INSUFFICIENT_INFORMATION"
    assert not matcher.is_askable_unknown(a)  # 같은 질문을 반복하지 않고 문서 확인


@pytest.mark.parametrize("quote,expected", [("조영 CT를 받았지만", "indirect"), ("알레르기 여부는 모릅니다", "hedged")])
def test_e9_ct_done_but_allergy_unknown(monkeypatch, quote, expected):
    """E: '조영 CT를 받았지만 알레르기 여부는 모릅니다' -> 어느 절을 인용해도 확정하지 않음."""
    m, a = _match_e9(monkeypatch, INIT_CT + Q_ALLERGY + "조영 CT를 받았지만 알레르기 여부는 모릅니다.", "no", quote)
    assert a.passes is None and a.evidence_directness == expected and m.eligibility == "UNCERTAIN"


def test_e9_direct_patient_answer_stays_unverified(monkeypatch):
    """F: 환자가 직접 '알레르기 없습니다' -> 직접 근거지만 자기보고. E9는 진료 기록 필요 기준이라 UNVERIFIED 유지."""
    m, a = _match_e9(monkeypatch, INIT_CT + Q_ALLERGY + "알레르기 없습니다.", "no", "알레르기 없습니다")
    assert a.evidence_directness == "direct" and a.evidence_source == "self_report"
    assert a.passes is None and a.needs_verification and a.self_reported_holds == "no"
    assert m.match_status == "POTENTIAL_MATCH"


def test_e9_unknown_input_source_never_record(monkeypatch):
    m, a = _match_e9(monkeypatch, INIT_CT, "no", "조영 CT를 받은 적 있음", input_source="unknown")
    assert a.passes is None and a.evidence_source != "record"


def test_e9_english_record_tolerated_and_no_allergy_consistent(monkeypatch):
    """S004 형식의 영어 기록: 두 문장 모두 '반응·알레르기 없음' -> 상충은 아님.

    기대 변경(Phase 10-E9 승인 조건 3, 대상 일치): 기준은 일반 'IV contrast'와 gadolinium을 말하고 근거는 요오드·가돌리늄
    조영제를 따로 말함. 요오드 조영제가 이 기준의 'IV contrast'와 같은지는 임상 해석이라 코드가 정하지 않음 -> 확정하지 않음.
    """
    text = ("He tolerated iodinated contrast without reaction.\n"
            "He has no known allergy or contraindication to iodinated contrast or gadolinium.")
    m, a = _match_e9(monkeypatch, text, "no",
                     "He has no known allergy or contraindication to iodinated contrast or gadolinium")
    assert a.evidence_directness == "object_mismatch" and a.passes is None
    assert "conflicting_evidence" not in a.validation_flags and m.eligibility == "UNCERTAIN"


def test_e9_other_allergen_is_not_same_object(monkeypatch):
    text = INIT_CT + "\n페니실린 알레르기 있음.\n조영제 알레르기 없음."
    m, a = _match_e9(monkeypatch, text, "no", "조영제 알레르기 없음")
    assert a.passes is True and a.evidence_directness == "direct"


def test_e9_fact_store_keeps_source_separate_from_verification():
    """사실 저장소: 기록 인용은 clinical_record지만 검증 상태는 unknown, 답변 인용은 self_report (승격 없음)."""
    text = INIT_CT + Q_ALLERGY + "알레르기 없습니다."
    s = FactStore()
    profile_facts.ingest_profile(s, _profile(patient_id="E9X", other_facts=[
        Fact(description="조영 CT 이력", date=None, source_quote="조영 CT를 받은 적 있음"),
        Fact(description="알레르기 없음(답변)", date=None, source_quote="알레르기 없습니다.")]),
        raw_text=text, input_source=REC, session_id="S")
    ct, ans = s.facts("E9X", "other_fact")
    assert (ct.source_type, ct.verification_status) == ("clinical_record", "unknown")
    assert (ans.source_type, ans.verification_status) == ("self_report", "unknown")


# ------------------------------------------------------------ 기준별 대응 (전체 환자 정보를 바꾸지 않음)
SURG = _rule(rule_id="I1", text="Prior wrist surgery", category="procedure", structured_evaluable=False)
TFCC = _rule(rule_id="I2", text="Prior TFCC repair surgery", category="procedure", structured_evaluable=False)
LIG = _rule(rule_id="E1", kind="exclusion", text="Prior ligament reconstruction surgery", category="procedure",
            structured_evaluable=False)
B_TEXT = INIT_CT + "\n\n[추가 확인 정보 - 라운드 1]\nQ: 수술 받은 적 있나요?\nA: 손목 수술은 했는데 TFCC인지 인대재건술인지는 몰라요."


def _match(monkeypatch, rules, verdicts, text, input_source=REC, **profile):
    _llm(monkeypatch, *verdicts)
    return {a.rule_id: a for a in matcher.match_trial(_profile(**profile), _parsed(rules), TRIAL, source_text=text,
                                                       input_source=input_source).assessments}


def test_clause_scope_keeps_certain_clause_and_blocks_unknown_clause(monkeypatch):
    """같은 문장 안에서도 확실한 절(손목 수술 함)은 유지, '몰라요' 절을 근거로 한 판정만 보류."""
    a = _match(monkeypatch, [SURG, TFCC, LIG], [
        ("I1", "yes", "explicit", "손목 수술은 했는데"),
        ("I2", "no", "explicit", "TFCC인지 인대재건술인지는 몰라요"),
        ("E1", "no", "explicit", "인대재건술인지는 몰라요")], B_TEXT)
    assert a["I1"].passes is True and a["I1"].evidence_directness == "unchecked"
    assert a["I2"].passes is None and a["I2"].validation_flags == ["hedged_quote"]
    assert a["E1"].passes is None


def test_superclass_quote_for_subclass_is_caught(monkeypatch):
    """상위 개념 인용으로 하위 개념(TFCC)을 확정하지 않음.

    기대 변경(Phase 10-E9 승인 조건 3): 이전에는 막지 못하는 한계로 기록했던 반례. 이제 시술·치료 기준의 특정 약어(TFCC)가
    근거에 없으면 '있음' 판정을 확정하지 않음.
    """
    a = _match(monkeypatch, [TFCC], [("I2", "yes", "explicit", "손목 수술은 했는데")], B_TEXT)
    assert a["I2"].passes is None and a["I2"].validation_flags == ["specific_term_missing"]


@pytest.mark.parametrize("answer,quote", [
    ("수술했던 것 같기도 한데 정확히 기억이 안 나요.", "수술했던 것 같기도 한데"),
    ("인대 수술이었던 것 같아요.", "인대 수술이었던 것 같아요"),
    ("의사 선생님이 무슨 재건술이라고 했는데 기억이 안 나요.", "무슨 재건술이라고 했는데"),
    ("잘 모르겠어요.", "잘 모르겠어요"),
    ("아마 받았을 거예요.", "아마 받았을 거예요"),
    ("I think I probably had wrist surgery.", "I think I probably had wrist surgery"),
])
def test_hedged_answers_never_confirm(monkeypatch, answer, quote):
    text = INIT_CT + f"\n\n[추가 확인 정보 - 라운드 1]\nQ: 수술 받은 적 있나요?\nA: {answer}"
    for holds in ("yes", "no"):
        a = _match(monkeypatch, [LIG], [("E1", holds, "explicit", quote)], text)["E1"]
        assert a.passes is None and a.holds == "unknown" and a.evidence_directness == "hedged"
        assert a.llm_judgment["holds"] == holds  # LLM 원래 판정과 코드 검증 결과를 따로 남김


def test_quote_only_from_question_is_rejected(monkeypatch):
    text = INIT_CT + "\n\n[추가 확인 정보 - 라운드 1]\nQ: 인대 재건술을 받은 적이 있나요?\nA: 네"
    a = _match(monkeypatch, [LIG], [("E1", "yes", "explicit", "인대 재건술을 받은 적이 있나요")], text)["E1"]
    assert a.passes is None and a.evidence_directness == "question_only"


def test_hedge_elsewhere_does_not_touch_other_criteria(monkeypatch):
    """다른 문장의 '모르'·'의심'이 이 기준의 직접·확실한 근거를 무효로 만들지 않음."""
    text = "Wrist surgery in 2020. Ligament status unknown. 손목 통증 원인은 의심 단계."
    a = _match(monkeypatch, [SURG], [("I1", "yes", "explicit", "Wrist surgery in 2020")], text)["I1"]
    assert a.passes is True


def test_morphine_is_not_hedge():
    assert ed.check("Prior opioid use", "모르핀 복용 중", "모르핀 복용 중.").ok


# ------------------------------------------------------------ 임신·수유 개념군과 구조화 필드
PREG = _rule(rule_id="E2", kind="exclusion", text="Pregnant or breastfeeding women", category="reproductive",
             structured_evaluable=False)


def test_age_alone_does_not_settle_pregnancy(monkeypatch):
    a = _match(monkeypatch, [PREG], [("E2", "no", "explicit", "age: 62")], "62세 여성 폐암.")["E2"]
    assert a.passes is None and a.evidence_directness == "indirect"


def test_male_sex_settles_pregnancy_not_applicable(monkeypatch):
    a = _match(monkeypatch, [PREG], [("E2", "no", "explicit", "sex: male", False)], "62세 남성 폐암.",
               sex="male")["E2"]
    assert a.passes is True and a.evidence_directness == "structured"


def test_postmenopausal_record_is_direct(monkeypatch):
    """기대 변경(Phase 10-E9 승인 조건 2·3): '폐경 -> 임신·수유 아님'은 임상 추론이라 '임신 또는 수유' 부재를 확정하지 않음.

    (대상 집단 판정, 예: 가임 여성 대상 기준의 '해당 없음'에는 폐경 기록을 직접 근거로 씀 - 아래 테스트)
    """
    a = _match(monkeypatch, [PREG], [("E2", "no", "explicit", "postmenopausal since 2015")],
               "62-year-old woman, postmenopausal since 2015.")["E2"]
    assert a.passes is None and a.validation_flags == ["polarity_mismatch"]
    wocbp = _rule(rule_id="I5", text="Women of childbearing potential must use effective contraception",
                  category="reproductive", structured_evaluable=False)
    b = _match(monkeypatch, [wocbp], [("I5", "no", "explicit", "postmenopausal since 2015", False)],
               "62-year-old woman, postmenopausal since 2015.")["I5"]
    assert b.passes is True and b.applicable is False and b.evidence_directness == "direct"


def test_rule_engine_marked_structured(monkeypatch):
    age = _rule(rule_id="I1", text="Age >= 18", field="age", operator=">=", value="18")
    a = _match(monkeypatch, [age], [], "62세 남성.")["I1"]
    assert a.method == "rule_engine" and a.evidence_directness == "structured" and a.llm_judgment is None


# ------------------------------------------------------------ 보존·호환
@pytest.mark.parametrize("quote", ["조영 CT를 받은 적 있음", "조영제 알레르기 의심"])
def test_validation_failure_never_becomes_not_met(monkeypatch, quote):
    m, a = _match_e9(monkeypatch, CONFLICT + "\n2026-07 조영 CT를 받은 적 있음.", "yes", quote)
    assert a.passes is None and m.eligibility != "INELIGIBLE"


def test_existing_unknown_and_unverified_preserved(monkeypatch):
    m, a = _match_e9(monkeypatch, INIT_CT, "unknown", "근거 없음")
    assert a.passes is None and a.evidence_directness == "none" and a.validation_flags == []


def test_inferred_and_missing_quote_flags_recorded(monkeypatch):
    a = _match(monkeypatch, [SURG, TFCC], [("I1", "yes", "inferred", "손목 수술은 했는데"),
                                           ("I2", "yes", "explicit", "TFCC repair in 2021")], B_TEXT)
    assert a["I1"].validation_flags == ["inferred_not_confirmable"] and a["I1"].passes is None
    assert a["I2"].validation_flags == ["quote_not_in_source"] and a["I2"].passes is None


def test_old_saved_assessment_loads_with_defaults():
    old = {"rule_id": "I1", "kind": "inclusion", "criterion_text": "t", "holds": "yes", "passes": True,
           "confidence": "high", "evidence": "e", "reasoning": "r", "missing_info": None, "method": "llm"}
    a = CriterionAssessment.model_validate(old)
    assert a.evidence_directness == "none" and a.validation_flags == [] and a.llm_judgment is None
    m = TrialMatch(patient_id="P", trial_id="T", eligibility="ELIGIBLE", assessments=[a], n_pass=1, n_fail=0,
                   n_unknown=0, summary="s")
    assert TrialMatch.model_validate_json(m.model_dump_json()).assessments[0] == a


def test_repeated_runs_identical_with_fake_llm(monkeypatch):
    """같은 입력 3회 -> 같은 결과 (결정적 대역 기준. 실제 Claude 재현성의 증거가 아님)."""
    runs = [_match_e9(monkeypatch, CONFLICT, "no", "2024-03 기록: 조영제 알레르기 없음")[1].model_dump() for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_frozen_store_files_match_lock():
    lock = json.loads((ROOT / "data/parsed/FROZEN.json").read_text(encoding="utf-8"))
    for tid, digest in lock["files"].items():
        assert hashlib.sha256((ROOT / f"data/parsed/{tid}.json").read_bytes()).hexdigest() == digest, tid
