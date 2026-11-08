"""값 주변의 문맥: 상태 표지가 누구를 서술하는가, 값이 결과로 보고되었는가 (합성 데이터, LLM 호출 없음).

`data/value_context_regression.json`은 이전 독립 평가 3회분과 개발용 세트입니다. 코드를 이 세트로 고쳤으므로 **더 이상 독립 평가가 아니고**
회귀 세트입니다. 통과 수를 일반 성능으로 읽으면 안 됩니다. 표현은 임상 검수를 받지 않았습니다.
`known_over_hold`는 알고 남겨 둔 과잉 보류입니다(안전 쪽, 사람 확인으로 넘어감).
`specimen_unstated`는 검체가 적혀 있지 않아 계약상 보류되는 값입니다(specimen.py). 측정 상태는 맞으므로 검체 규칙을 빼면 근거가 됩니다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import test_p0_session_grounding as tsg  # noqa: E402
from document_support import attest_declared  # noqa: E402
from test_expert_boundary import _run  # noqa: E402
from test_offline import _rule  # noqa: E402
from test_p0_session_grounding import HB, HB_HIGH, PID, Q_HB, RECORD, SELF_OK, confirmed, hb, prof, run  # noqa: E402,F401

from ctrec import value_grounding as vg  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.schemas import RuleMeta  # noqa: E402

DATA = json.loads((Path(__file__).parent / "data" / "value_context_regression.json").read_text(encoding="utf-8"))
KNOWN_OVER_HOLD = set(DATA["known_over_hold"])
SPECIMEN_UNSTATED = set(DATA["specimen_unstated"])
NEGATIVE = [(name, x) for name, part in DATA["sets"].items() for x in part["negative"]]
POSITIVE = [(name, x) for name, part in DATA["sets"].items() for x in part["positive"]]
HB_NARRATIVE = _rule(rule_id="I3", text="Adequate hemoglobin (Hb >= 9 g/dL)", category="lab", structured_evaluable=False)

# E의 2차 평가에서 남은 사례 (실제 Session으로 추적한 것)
LEAKED = ["채혈 자체를 안 했어요. 템플릿 칸에 혈색소 12 가 미리 찍혀 있었는데 그건 복사 오류로 보임, 실측 아님.",
          "Hgb 12 under the lab section header but the order was cancelled before the draw; no specimen ever reached the analyzer.",
          "Specimen never collected, phlebotomy log is blank for 11/06; the 헤모글로빈 12 g/dL field in the flowsheet is a carry-forward default.",
          "혈색소 12 항목은 시스템에 값만 있고 검체 번호가 없음. 실제로는 시행된 적 없는 검사.",
          "혈색소 12 는 기관 표준 참고치(상한)이고 이 환자 수치는 별도.",
          "Hgb 12 belongs to the bed next door; the wristband scan mismatched and the result was re-filed.",
          "If the transfusion works, expect Hgb 12 by morning; purely hypothetical at this point.",
          "비교군(건강 성인 50명) 혈색소 12 중앙값."]
OVER_HELD = ["Hgb 12 on today's CBC, verified. Pending: procalcitonin, still running.",
             "헤모글로빈 12 g/dL 검증. 검체 오염 의심 없음.",
             "앞선 헤모글로빈 수치(무효 처리)는 제외, 재검 결과 12 g/dL 이 유효."]


def wrap(text):
    return f"폐암 환자. {text}"


def grounded(x):
    field, value, text = x["field"], x["value"], x["text"]
    source = "폐암 환자.\n" + text
    if field == "ecog":
        return vg.ground("ecog", value, source)
    if field == "age":
        return vg.ground("age", value, source, reference_date="2026-11-07")
    return vg.ground_lab(field, float(value), text, source, unit=x.get("unit"), known={vg._text(float(value))})


def lab(text, **kw):
    return vg.ground_lab("hemoglobin", 12.0, text, wrap(text), unit="g/dL", known={"12"}, **kw)


def session_case(run, text, meta=SELF_OK, **kw):
    return run(wrap(text), [HB], prof(labs=[hb(quote=text)]), meta=meta, **kw)


def not_confirmed(o):
    assert o.a.passes is None and o.status != "CONFIRMED_ELIGIBLE" and o.match.eligibility != "ELIGIBLE"
    assert not o.session.fact_store.facts(PID, "lab:hemoglobin") or o.a.validation_flags


@pytest.fixture
def contract_run(run, monkeypatch):
    monkeypatch.setattr(tsg, "Session", ContractSession)
    return run


# ------------------------------------------------------------ 회귀 세트 (독립 평가 아님)
def test_the_regression_set_is_not_empty_and_lists_only_its_own_texts():
    assert len(NEGATIVE) >= 220 and len(POSITIVE) >= 215
    assert (KNOWN_OVER_HOLD | SPECIMEN_UNSTATED) <= {x["text"] for _, x in POSITIVE}


@pytest.mark.parametrize("name, x", NEGATIVE, ids=[f"{n}-{i}" for i, (n, _) in enumerate(NEGATIVE)])
def test_regression_negative_is_never_grounded(name, x):
    assert grounded(x) is not None


@pytest.mark.parametrize("name, x", POSITIVE, ids=[f"{n}-{i}" for i, (n, _) in enumerate(POSITIVE)])
def test_regression_positive_stays_grounded(name, x):
    if x["text"] in KNOWN_OVER_HOLD or x["text"] in SPECIMEN_UNSTATED:
        assert grounded(x) is not None  # 푸는 변경은 여기서 드러나고 목록에서 뺌
    else:
        assert grounded(x) is None


@pytest.mark.parametrize("x", [x for _, x in POSITIVE if x["text"] in SPECIMEN_UNSTATED], ids=lambda x: x["text"][:30])
def test_specimen_unstated_is_held_by_the_specimen_rule_alone(x, monkeypatch):
    monkeypatch.setattr(vg.specimen, "MULTI_SPECIMEN", frozenset())
    assert grounded(x) is None


# ------------------------------------------------------------ 잔여 8건, 과잉 보류 3건: 실제 경로
@pytest.mark.parametrize("text", LEAKED)
def test_leak_is_held_in_session_and_stores_no_fact(run, text):
    o = session_case(run, text)
    not_confirmed(o)
    assert o.session.fact_store.facts(PID, "lab:hemoglobin") == [] and o.status == "INFO_MISSING"


@pytest.mark.parametrize("text", LEAKED)
def test_leak_is_held_in_contract_session(contract_run, text):
    not_confirmed(session_case(contract_run, text))


@pytest.mark.parametrize("text", LEAKED)
def test_leak_is_not_grounds_for_ineligible_either(run, text):
    assert run(wrap(text), [HB_HIGH], prof(labs=[hb(quote=text)]), meta=SELF_OK).match.eligibility != "INELIGIBLE"


@pytest.mark.parametrize("text", LEAKED)
def test_leak_with_a_short_quote_is_still_held(run, text):
    """프로필러가 수치 주변만 인용해도 원문 전체로 봄."""
    quote = "12 g/dL" if "12 g/dL" in text else "12"
    not_confirmed(run(wrap(text), [HB], prof(labs=[hb(quote=quote)]), meta=SELF_OK))


@pytest.mark.parametrize("text", LEAKED)
def test_leak_in_an_llm_quote_is_held(monkeypatch, text):
    x = _run(monkeypatch, "T1", [HB_NARRATIVE], {"I3": RuleMeta(required_evidence="self_report_ok")}, {"I3": ("yes", text)}, wrap(text))
    assert x["match"].eligibility == "UNCERTAIN" and x["pair"].status.value == "INFO_MISSING"


@pytest.mark.parametrize("text", LEAKED)
def test_leak_on_a_record_rule_is_held_even_with_a_synthetic_document(run, monkeypatch, text):
    real = tsg.pipeline.Session.match

    def match(self, *a, **k):
        attest_declared(self.fact_store, PID)
        return real(self, *a, **k)

    monkeypatch.setattr(tsg.pipeline.Session, "match", match)
    not_confirmed(session_case(run, text, meta=RECORD, input_source="clinical_record"))


@pytest.mark.parametrize("text", OVER_HELD)
def test_valid_value_is_confirmed_on_a_self_report_rule(run, text):
    confirmed(session_case(run, text))


@pytest.mark.parametrize("text", OVER_HELD)
def test_valid_value_is_confirmed_in_contract_session(contract_run, text):
    confirmed(session_case(contract_run, text))


@pytest.mark.parametrize("text", OVER_HELD)
def test_valid_value_still_rules_out_on_a_failed_threshold(run, text):
    assert run(wrap(text), [HB_HIGH], prof(labs=[hb(quote=text)]), meta=SELF_OK).match.eligibility == "INELIGIBLE"


@pytest.mark.parametrize("text", OVER_HELD)
def test_valid_value_does_not_replace_the_document_on_a_record_rule(run, text):
    """자가보고 허용과 문서 필수의 구분은 그대로: 유효한 값이어도 서버 문서 없이는 문서 필수 기준을 확정하지 않음."""
    o = session_case(run, text, meta=RECORD, input_source="clinical_record")
    assert o.status != "CONFIRMED_ELIGIBLE"


# ------------------------------------------------------------ 구조별 대조
@pytest.mark.parametrize("text", ["혈색소 12 항목은 비어 있음", "the Hb 12 g/dL field", "Hb 12 g/dL entry", "혈색소 12 는 임시로 넣어 둔 숫자", "Hgb 12 is what we hope for",
                                  "Hb 12 comes from the neighbouring chart", "혈색소 12 라고 적어 둠", "Hgb 12 looks copied"])
def test_value_as_subject_is_not_a_result(text):
    assert lab(text) is not None


@pytest.mark.parametrize("text", ["Hb 12 g/dL는 정상 범위입니다", "혈색소 12 가 오늘 측정값", "Hb 12 g/dL이고 안정적", "Hb 12 g/dL입니다", "Hb is 12 g/dL", "Hb 12 g/dL is normal", "Hgb 12 was drawn this morning",
                                  "재채혈한 Hb 12 g/dL 가 유효값"])
def test_value_as_subject_with_an_affirming_predicate_stays(text):
    assert lab(text) is None


def test_relayed_value_with_an_affirming_predicate_is_held():
    """기대값 변경 (P0-B 전해 들은 값, 11_CODE_REVIEW B-2): 이전에는 위 목록에서 채택으로 고정했지만
    "~라고 들었어요"는 측정 결과가 아니라 전해 들은 수치이고 전한 사람이 의료진으로 적혀 있지 않으므로 확정된 직접 사실로 쓰지 않음
    (tests/test_p0_hearsay_values.py). 의료진이 전한 값의 채택 여부는 임상 결정 CL-B1로 남김."""
    assert lab("혈색소 12 라고 들었어요") == vg.HEDGED


@pytest.mark.parametrize("text", ["If she improves, Hgb 12 next visit", "만약 호전되면, Hb 12 g/dL 도달", "Assuming no bleed, Hb 12 g/dL; 추적 필요"])
def test_a_leading_conditional_covers_the_later_clause(text):
    assert lab(text) is not None


@pytest.mark.parametrize("text", ["Hb 12 g/dL; if it falls below 8, transfuse", "Hb 12 g/dL. If febrile, call"])
def test_a_later_conditional_does_not_touch_the_value(text):
    assert lab(text) is None


@pytest.mark.parametrize("text", ["검체 미채취. Hb 12 g/dL", "Blood draw refused by patient. Hb 12 g/dL", "Specimen never collected. Hb 12 g/dL", "Hb 12 g/dL. No specimen was received.",
                                  "Hb 12 g/dL\n\n[시행 상태]\n검사 미시행", "CBC not done.\nLabs:\nHb 12 g/dL", "채혈 못함. Hb 12 g/dL", "Hb 12 g/dL. 재검 필요"])
def test_a_process_statement_reaches_values_in_other_sentences(text):
    assert lab(text) is not None


@pytest.mark.parametrize("text", ["Hb 12 g/dL. heparin 오더 취소됨", "CT 시행 안 함. Hb 12 g/dL", "PET 예약 취소, Hb 12 g/dL", "Hb 12 g/dL. Pending: ferritin", "Hb 12 g/dL, TSAT pending", "Hb 12 g/dL. 소변 검체 오염으로 재검",
                                  "첫 검체는 clot 으로 무효. 재채혈 Hb 12 g/dL", "Hb 12 g/dL (재채혈). 첫 검체는 용혈로 폐기", "Hb 12 g/dL\n\n[시행 상태]\n완료"])
def test_a_status_about_something_else_does_not_reach_the_value(text):
    assert lab(text) is None


@pytest.mark.parametrize("text", ["Hb 12 g/dL carried forward from prior encounter", "Hb 12 auto-populated", "혈색소 12 자동입력", "Hb 12 g/dL 복사", "Hb 12 g/dL spurious", "Hb 12 g/dL hoped for"])
def test_a_value_with_no_measurement_behind_it(text):
    assert lab(text) is not None


@pytest.mark.parametrize("text", ["No labs pending. Hb 12 g/dL", "Hb 12 g/dL. 검체 오염 의심 없음", "Hb 12 g/dL, 용혈 징후 없음", "Hb 12 g/dL, no sample issue pending"])
def test_a_negated_state_is_not_a_state(text):
    assert lab(text) is None


def test_a_retest_value_needs_the_sentence_to_name_only_this_test():
    assert lab("앞선 헤모글로빈 수치는 제외, 재검 결과 12 g/dL") is None
    assert lab("앞선 혈소판 수치는 제외, 재검 결과 12 g/dL") is not None
    assert lab("헤모글로빈 8, 혈소판 20, 재검 결과 12 g/dL") is not None


@pytest.mark.parametrize("field, value, text", [("ecog", 1, "ECOG 1 항목은 템플릿 기본값"), ("ecog", 1, "ECOG 1 은 지난 외래 값을 옮겼다는 메모"), ("ecog", 1, "ECOG 1 으로 가정"), ("ecog", 2, "ECOG: not assessed at this visit; ECOG 2 recorded earlier"),
                                                ("ecog", 1, "If he recovers, ECOG 1 is possible"), ("age", 41, "Her sister, age 41, carries the variant"), ("age", 62, "나이 62세 항목은 기본값")])
def test_ecog_and_age_share_the_context_checks(field, value, text):
    assert grounded({"field": field, "value": value, "text": text}) is not None


@pytest.mark.parametrize("field, value, text", [("ecog", 1, "ECOG 1"), ("ecog", 1, "ECOG 1 입니다"), ("ecog", 1, "ECOG 1, CT 미시행"), ("age", 62, "62세 남성"), ("age", 41, "환자, 41세")])
def test_ecog_and_age_controls(field, value, text):
    assert grounded({"field": field, "value": value, "text": text}) is None


@pytest.mark.parametrize("field, value, text", [("total bilirubin", 1.2, "T-Bil 1.2 mg/dL"), ("total bilirubin", 1.2, "T.bil 1.2 mg/dL"), ("procalcitonin", 0.8, "PCT 0.8 ng/mL"), ("procalcitonin", 0.8, "프로칼시토닌 0.8 ng/mL"), ("ferritin", 640, "FER 640 ng/mL")])
def test_chart_abbreviations_reach_the_value(field, value, text):
    assert grounded({"field": field, "value": value, "text": text, "unit": text.split()[-1]}) is None


def test_a_value_named_absent_elsewhere_is_not_current():
    assert grounded({"field": "procalcitonin", "value": 0.05, "unit": None, "text": "PCT 0.05 from a prior admission; no PCT this time."}) is not None
    assert grounded({"field": "procalcitonin", "value": 0.05, "unit": None, "text": "PCT 0.05 today; no CRP this time."}) is None


# ------------------------------------------------------------ 답변 경로와 갱신
def test_a_valid_answer_after_an_unmeasured_record_confirms_on_a_self_report_rule(run):
    """무효 기록은 사실이 아니므로 충돌도 아님. 재평가에서 새 답변으로 확정됨."""
    text = "Specimen never collected."
    answer = "혈색소 12 g/dL 입니다"
    o = run(wrap(text), [HB], prof(), after=prof(labs=[hb(quote=answer)]), answer=answer, meta=SELF_OK, question=Q_HB, target="T1:I3")
    confirmed(o)


def test_an_answer_that_the_test_never_happened_overrides_a_stated_value(run):
    text = "Hb 12 g/dL"
    answer = "그 검사는 시행된 적 없어요"
    o = run(wrap(text), [HB, tsg.OPEN], prof(labs=[hb(quote=text)]), after=prof(labs=[hb(quote=text)]), answer=answer, meta=SELF_OK, question=Q_HB, target="T1:I3")  # OPEN: 질문 라운드가 돌게 함
    assert answer in o.session.case.raw_text  # 답변이 실제로 들어왔는지 함께 확인
    assert o.a.passes is None and o.status != "CONFIRMED_ELIGIBLE"


# ------------------------------------------------------------ 세 번째 평가에서 드러난 구조
@pytest.mark.parametrize("text", ["Serum Hb: 12 g/dL (today 08:00)", "Lipase pending. Hb 12 g/dL resulted at 09:05", "Hb = 12 g/dL"])
def test_a_clock_time_is_not_a_second_value(text):
    assert lab(text) is None


@pytest.mark.parametrize("text", ["이전 검사 Hb 12 g/dL", "Hb 12 g/dL (04/2023, prior admission)", "Hb 12 g/dL previously normal", "Hb 12 g/dL, sample from before transfusion is no longer current"])
def test_a_value_marked_past_is_not_current_on_its_own(text):
    assert lab(text) is not None


def test_a_past_value_next_to_a_current_one_changes_nothing():
    assert lab("이전 Hb 9 g/dL, 금일 Hb 12 g/dL") is None


@pytest.mark.parametrize("field, value, text", [("total bilirubin", 3.2, "직접 빌리루빈 3.2 mg/dL 만 확인됨"), ("total bilirubin", 3.2, "direct bilirubin 3.2 mg/dL")])
def test_a_qualified_variant_is_another_test(field, value, text):
    assert grounded({"field": field, "value": value, "text": text, "unit": "mg/dL"}) is not None


def test_a_full_unit_is_read_before_a_subject_particle():
    assert vg.ground_lab("platelets", 22.0, "혈소판 22 x10^3/uL 는 임시로 넣어 둔 숫자", wrap("혈소판 22 x10^3/uL 는 임시로 넣어 둔 숫자"), unit="x10^3/uL", known={"22"}) is not None


@pytest.mark.parametrize("text, value", [("Bone age 12 years by hand X-ray", 12), ("Registration typo gave age 6 years", 6), ("system temporarily shows 64 years", 64), ("담당 간호사 본인 나이가 29세", 29), ("보호자 말로는 71세", 71)])
def test_an_age_that_is_not_this_patients_stated_age(text, value):
    assert grounded({"field": "age", "value": value, "text": text}) is not None


def test_ecog_by_hearsay_is_held():
    assert grounded({"field": "ecog", "value": 0, "text": "ECOG 0 per mother's phone report"}) is not None
    assert grounded({"field": "ecog", "value": 0, "text": "Husband (ECOG 3) needs help; patient ECOG 0."}) is None


# ------------------------------------------------------------ 변이 검사에서 살아남은 동작
@pytest.mark.parametrize("text", ["결과 대기. Hb 12 g/dL", "CBC 미시행 (응급 수술로 생략). Hb 12 g/dL", "Hb 12 g/dL로 추정", "Hb 12 g/dL; no Hb this time"])
def test_held_by_a_single_structure(text):
    assert lab(text) is not None


@pytest.mark.parametrize("text", ["Hb 12 g/dL. Pending: zonulin", "Hb 12 g/dL. 첫 검체는 clot 무효", "첫 검체 무효. Hb 12 g/dL"])
def test_kept_by_a_single_structure(text):
    assert lab(text) is None
