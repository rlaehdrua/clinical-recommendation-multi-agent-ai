"""CL-B1 후보 (팀 결정 전): 전해 들은 값은 전한 사람이 누구든 같은 보류 (합성 데이터, LLM 호출 없음).

같은 뜻의 문장 묶음은 모두 같은 결과여야 합니다: 간호사·의사·외부 기관·대명사·화자 없는 전언 모두 측정값으로 채택하지 않음.
보류 상태는 HEDGED(전언) 또는 UNLOCATED(다른 사람의 값으로 봄) 중 하나이며, 어느 쪽이든 판정 근거가 아님.
직접 측정 문장(기록 구간)은 그대로 채택하고, 서버 문서 확인 대조군도 그대로 확정됩니다.
표현은 임상 검수를 받지 않았습니다. value_grounding.CLB1_RELAY_HOLD = False이면 이전 동작입니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import test_p0_session_grounding as tsg  # noqa: E402
from test_p0_session_grounding import AGE, HB, PID, RECORD, SELF_OK, confirmed, hb, held, prof, run  # noqa: E402,F401

from ctrec import value_grounding as vg  # noqa: E402

H, U = vg.HEDGED, vg.UNLOCATED
NOT_ACCEPTED = (H, U)


def lab(text, value=12):
    return vg.ground_lab("hemoglobin", value, text, text)


# 같은 뜻의 문장 묶음: 묶음 안의 모든 문장이 보류 (이전에는 표현에 따라 채택/보류가 갈렸음)
GROUPS = {
    "nurse": ["Per the nurse, Hb 12 g/dL.", "The nurse said Hb 12 g/dL.", "The nurse called; Hb 12 g/dL.", "Nurse reports Hb 12 g/dL.",
              "간호사가 혈색소 12라고 했어요."],
    "doctor": ["Dr. Kim said Hb 12 g/dL.", "Dr Kim said Hb 12.", "According to Dr. Kim, Hb 12 g/dL.", "주치의 말로는 혈색소 12."],
    "outside": ["Per outside hospital, Hb 12 g/dL.", "Outside labs: Hb 12 g/dL.", "외부 병원 검사에서 혈색소 12라고 들었어요."],
    "pronoun_or_unknown": ["He reported Hb 12 g/dL.", "She says Hb 12 g/dL.", "들었는데 혈색소 12."],
}
CASES = [(g, t) for g, ts in GROUPS.items() for t in ts]

# 이전(dba06d9)에 채택(None)되던 문장: 정확히 HEDGED가 되어야 함
WAS_ACCEPTED = ["Per the nurse, Hb 12 g/dL.", "The nurse called; Hb 12 g/dL.", "Dr. Kim said Hb 12 g/dL.", "Dr Kim said Hb 12.",
                "According to Dr. Kim, Hb 12 g/dL.", "Per outside hospital, Hb 12 g/dL.", "Outside labs: Hb 12 g/dL.",
                "He reported Hb 12 g/dL.", "She says Hb 12 g/dL.", "들었는데 혈색소 12.",
                "The nurse rang back, she said my Hb is 12 g/dL.", "Outside hospital CBC 2026-10-01: Hb 12 g/dL."]


@pytest.mark.parametrize("group, text", CASES, ids=[f"{g}-{i}" for i, (g, _) in enumerate(CASES)])
def test_relayed_lab_is_not_a_measured_value(group, text):
    assert lab(text) in NOT_ACCEPTED, text


@pytest.mark.parametrize("group", list(GROUPS))
def test_same_meaning_group_has_one_outcome(group):
    """묶음 안에서 채택된 문장이 하나도 없어야 함 (같은 출처가 표현에 따라 갈리지 않음)."""
    outcomes = {t: lab(t) for t in GROUPS[group]}
    assert all(v is not None for v in outcomes.values()), outcomes


@pytest.mark.parametrize("text", WAS_ACCEPTED)
def test_previously_accepted_relay_is_now_hedged(text):
    assert lab(text) == H, text


@pytest.mark.parametrize("field, value, text", [
    ("age", 67, "Per outside hospital, age 67."),
    ("age", 67, "Nurse says he is 67."),
    ("age", 67, "Per the nurse, age 67."),
    ("age", 67, "Dr. Kim said he is 67."),
])
def test_relayed_age_is_held(field, value, text):
    assert vg.ground(field, value, text) in NOT_ACCEPTED, text


# ----------------------------------------------------------------------------- 대조군: 그대로 채택
@pytest.mark.parametrize("text", [
    "Hb 12 g/dL (2026-10-01).", "Lab: Hb 12 g/dL.", "Hb 12 g/dL (CBC 2026-11-01).", "CBC today: Hb 12 g/dL.",
    "Labs 2026-10-01: Hb 12 g/dL, Plt 200.", "Hb 12 g/dL per CBC today.", "Lab reported Hb 12.0 g/dL.", "Results reported: Hb 12.0 g/dL.",
    "Seen by Dr. Kim. Hb 12 g/dL today.", "Dr. Kim reviewed labs: Hb 12 g/dL.", "Patient reports fatigue and Hb 12 g/dL.",
    "Patient says she feels well; Hb 12.0 g/dL today.", "혈색소 12 g/dL이라고 결과지에 기재됨.", "혈색소 12.0 g/dL, 혈소판 210.",
    "Hb reported as 12 g/dL.", "Other labs: Hb 12 g/dL.", "Called patient to review labs; Hb 12 g/dL.",
])
def test_direct_measurement_in_the_record_is_still_adopted(text):
    assert lab(text) is None, text


@pytest.mark.parametrize("text", ["Age 67.", "67-year-old man.", "Patient says she is 67 years old.", "나이 67세"])
def test_plain_or_self_reported_age_is_still_adopted(text):
    assert vg.ground("age", 67, text) is None, text


def test_switch_off_restores_previous_behaviour(monkeypatch):
    """스위치는 이 브랜치에서 기본 보수(True). 끄면 의료진이 있는 문장의 이전 동작으로 돌아감 (비교용)."""
    assert vg.CLB1_RELAY_HOLD is True
    monkeypatch.setattr(vg, "CLB1_RELAY_HOLD", False)
    assert lab("Per the nurse, Hb 12 g/dL.") is None
    assert lab("Per family, Hb 12 g/dL.") == H  # 비의료인 전언 보류는 스위치와 무관


# ----------------------------------------------------------------------------- 판정까지 (Session, 오프라인)
@pytest.mark.parametrize("text", ["Per the nurse, Hb 12 g/dL.", "Dr. Kim said Hb 12 g/dL.", "The nurse called; Hb 12 g/dL.",
                                  "Per outside hospital, Hb 12 g/dL.", "Outside labs: Hb 12 g/dL.", "He reported Hb 12 g/dL."])
def test_relayed_lab_does_not_confirm_eligible(run, text):
    o = run(f"폐암 환자. {text}", [HB], prof(labs=[hb(12.0, text)]), meta=SELF_OK)
    held(o)
    assert o.status != "CONFIRMED_ELIGIBLE" and o.a.passes is None and o.a.evidence_directness == "hedged"
    assert [x.source_quote for x in o.session.profile.labs] == [text]  # 저장된 사실의 원문 인용은 그대로


@pytest.mark.parametrize("text", ["Per outside hospital, age 67.", "Nurse says he is 67."])
def test_relayed_age_does_not_confirm_eligible(run, text):
    o = run(f"폐암 환자. {text}", [AGE], prof(age=67))
    assert o.status != "CONFIRMED_ELIGIBLE" and o.a.passes is None


def test_direct_lab_still_confirms(run):
    text = "Hb 12 g/dL (2026-10-01)"
    confirmed(run(f"폐암 환자. {text}", [HB], prof(labs=[hb(12.0, text)]), meta=SELF_OK))


def test_document_verified_control_is_untouched(run, monkeypatch):
    """문서 확인 경로(합성 서버 증명)는 원문 문구 검사가 아니라 확인한 사실로 판정: 직접 측정 대조군은 그대로 확정."""
    pytest.importorskip("ctrec.document_authority")
    from document_support import attest_declared
    real = tsg.pipeline.Session.match

    def match(self, *a, **k):
        attest_declared(self.fact_store, PID)
        return real(self, *a, **k)

    monkeypatch.setattr(tsg.pipeline.Session, "match", match)
    text = "Hb 12 g/dL (2026-10-01)"
    confirmed(run(f"폐암 환자. {text}.", [HB], prof(labs=[hb(12.0, text)]), meta=RECORD, input_source="clinical_record"))
