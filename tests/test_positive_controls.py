"""합성 대조군: 기준이 숫자·범주뿐인 시험에서 Session 판정이 독립 계산 결과와 같은가 (LLM 호출 없음).

고정 시험 비교에는 ELIGIBLE이 없어 정상 판정의 회귀를 볼 수 없습니다. 여기 기대값은 판정 코드를 쓰지 않고 파이썬 비교로 계산합니다.
임상적 정답이 아니라 수치·범주 논리의 대조군입니다.
"""

from __future__ import annotations

import itertools
import operator
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_p0_lab_semantics_session import ranked  # noqa: E402
from test_p0_session_grounding import prof, run  # noqa: E402,F401
from test_offline import _rule  # noqa: E402

from ctrec.schemas import LabValue, RuleMeta  # noqa: E402

OPS = {">=": operator.ge, "<=": operator.le, "<": operator.lt, ">": operator.gt}
# (이름, 나이, 성별, ECOG, Hb, 혈소판) 원문 표현은 환자마다 다르게 적음
PATIENTS = [
    ("kr", 62, "female", 1, 12.0, 200.0, "62세 여성. ECOG 1. 혈색소 12 g/dL, 혈소판 200 x10^9/L."),
    ("en", 45, "male", 0, 14.5, 310.0, "45-year-old man. ECOG PS 0. Hemoglobin 14.5 g/dL. Platelets 310 x10^9/L."),
    ("short", 71, "female", 2, 9.0, 95.0, "71F, ECOG 2, Hb 9 g/dL, Plt 95 x10^9/L"),
    ("words", 62, "male", 1, 8.9, 150.0, "sixty-two years old male. ECOG one. Hb 8.9 g/dL. Platelets 150 x10^9/L."),
    ("table", 30, "female", 0, 13.0, 100.0, "30세 여성\nECOG 0\nHb | 13 | g/dL\n혈소판 | 100 | x10^9/L"),
    ("dated", 80, "male", 3, 11.0, 250.0, "80세 남자. ECOG 3. 2026-03-01 Hb 11 g/dL. 2026-03-01 Plt 250 x10^9/L."),
]


def age(rid, op, v):
    return _rule(rule_id=rid, text=f"Age {op} {v} years", field="age", operator=op, value=str(v), unit="years")


def ecog(rid, op, v):
    return _rule(rule_id=rid, text=f"ECOG {op} {v}", category="performance_status", field="ecog", operator=op, value=str(v))


def hgb(rid, op, v):
    return _rule(rule_id=rid, text=f"Hemoglobin {op} {v} g/dL", category="lab", field="lab:hemoglobin", operator=op, value=str(v), unit="g/dL")


def plt(rid, op, v):
    return _rule(rule_id=rid, text=f"Platelets {op} {v} x10^9/L", category="lab", field="lab:platelets", operator=op, value=str(v), unit="x10^9/L")


def sex(rid, v):
    return _rule(rule_id=rid, text=f"Sex: {v}", field="sex", operator="==", value=v)


# 시험: (규칙, 판정 함수의 입력 (값 위치, 연산자, 기준))
TRIALS = {
    "broad": [(age("I1", ">=", 18), 1, ">=", 18), (hgb("I3", ">=", 9), 4, ">=", 9)],
    "older": [(age("I1", ">=", 65), 1, ">=", 65), (ecog("I2", "<=", 2), 3, "<=", 2)],
    "fit": [(ecog("I2", "<=", 1), 3, "<=", 1), (hgb("I3", ">=", 10), 4, ">=", 10), (plt("I4", ">=", 100), 5, ">=", 100)],
    "female": [(sex("I1", "female"), 2, "==", "female"), (age("I2", "<=", 65), 1, "<=", 65)],
    "boundary": [(hgb("I3", ">=", 9), 4, ">=", 9), (plt("I4", ">", 95), 5, ">", 95)],
}
META = {r.rule_id: RuleMeta(required_evidence="clinical_record") for rules in TRIALS.values() for r, *_ in rules
        if r.category in ("lab", "performance_status")}


def expected(patient, rules) -> bool:
    return all((patient[i] == v) if op == "==" else OPS[op](patient[i], v) for _, i, op, v in rules)


def profile_of(p):
    _, a, s, e, h, t, text = p
    labs = [LabValue(name="hemoglobin", value=h, unit="g/dL", date=None, source_quote=_sentence_with(text, ("혈색소", "Hemoglobin", "Hb"))),
            LabValue(name="platelets", value=t, unit="x10^9/L", date=None, source_quote=_sentence_with(text, ("혈소판", "Platelets", "Plt")))]
    return prof(age=a, sex=s, ecog=e, labs=labs)


def _sentence_with(text, names):
    import re
    for part in re.split(r"(?<=[.])\s|\n|,\s", text):
        if any(n in part for n in names):
            return part.rstrip(".")
    raise AssertionError(names)


PAIRS = list(itertools.product(PATIENTS, TRIALS))


def test_both_outcomes_are_exercised():
    outcomes = [expected(p, TRIALS[t]) for p, t in PAIRS]
    assert outcomes.count(True) >= 8 and outcomes.count(False) >= 8


@pytest.mark.parametrize("p,trial", PAIRS, ids=[f"{p[0]}-{t}" for p, t in PAIRS])
def test_session_matches_the_independent_comparison(run, monkeypatch, p, trial):
    rules = [r for r, *_ in TRIALS[trial]]
    # 적격 쪽은 합성 서버 문서로 확인한 대조군. 부적격 쪽은 선언만으로도 잠정 부적격을 유지
    o = run(p[6], rules, profile_of(p), meta=META, input_source="clinical_record", documents=expected(p, TRIALS[trial]))
    if expected(p, TRIALS[trial]):
        assert (o.match.eligibility, o.status, o.match.match_status) == ("ELIGIBLE", "CONFIRMED_ELIGIBLE", "CRITERIA_MATCH")
        assert ranked(o, monkeypatch).ranked[0].rank == 1
    else:
        assert o.match.eligibility == "INELIGIBLE" and o.match.match_status == "CRITERIA_MISMATCH"
        assert o.status == "VERIFICATION_PENDING" and ranked(o, monkeypatch).ranked == []
