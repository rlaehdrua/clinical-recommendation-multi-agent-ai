"""실행기 점검용 작은 합성 코호트 (환자 6명 x 시험 3개, NSCLC 형태). **실제 환자·실제 시험이 아니며 성능 근거가 아님.**

시험 기준은 규칙 엔진이 판정하는 구조화 기준(나이·ECOG·검사 수치)이 중심이고, 시험 T-B에는 LLM 기준(EGFR 변이)을 하나
넣어 오프라인 범위에서는 확정 적격이 될 수 없는 쌍을 일부러 둡니다 (규칙 범위 한계가 결과에 드러나는지 확인용).
검사 수치 기준은 진료 기록 필요(required_evidence=clinical_record): 환자 답변만으로 확정되지 않고 문서 확인이 필요.
"""

from __future__ import annotations

from types import MappingProxyType

from ..schemas import ParsedTrial, Rule
from .cohort import SimCohort, SimFact, SimTrial, make_cohort

REFERENCE_DATE = "2026-10-12"
RECORD = {"required_evidence": "clinical_record"}


def _rule(rule_id, text, category, field=None, operator=None, value=None, unit=None, kind="inclusion",
          structured=True) -> Rule:
    return Rule(rule_id=rule_id, kind=kind, text=text, category=category, field=field, operator=operator, value=value,
                unit=unit, time_window_days=None, structured_evaluable=structured, underspecified=False, cohort=None)


def _trial(tid: str, rules: list[Rule], meta: dict) -> SimTrial:
    parsed = ParsedTrial(trial_id=tid, title=f"synthetic {tid}", target_population="synthetic NSCLC",
                         intervention_summary="synthetic", key_conditions=[], cohorts=[], rules=rules,
                         parsing_notes="")
    trial = {"trial_id": tid, "title": f"synthetic {tid}", "eligibility_criteria": "synthetic",
             "overall_status": "RECRUITING", "minimum_age": "18 Years", "locations": [{"country": "Korea"}]}
    return SimTrial(parsed, trial, MappingProxyType(meta))


AGE18 = _rule("I1", "Age >= 18 years", "demographic", "age", ">=", "18", "years")


def trials() -> list[SimTrial]:
    return [
        _trial("T-A", [AGE18, _rule("I2", "ECOG 0-1", "performance_status", "ecog", "<=", "1"),
                       _rule("I3", "Platelets >= 100 x10^9/L", "lab", "lab:platelets", ">=", "100", "x10^9/L"),
                       _rule("I4", "ANC >= 1.5 x10^9/L", "lab", "lab:absolute neutrophil count", ">=", "1.5",
                             "x10^9/L")],
               {"I3": RECORD, "I4": RECORD}),
        _trial("T-B", [AGE18, _rule("I2", "ECOG 0-2", "performance_status", "ecog", "<=", "2"),
                       _rule("I3", "Hemoglobin >= 9 g/dL", "lab", "lab:hemoglobin", ">=", "9", "g/dL"),
                       _rule("I4", "EGFR sensitizing mutation", "biomarker", structured=False)],
               {"I3": RECORD}),
        _trial("T-C", [AGE18, _rule("I2", "Age <= 75 years", "demographic", "age", "<=", "75", "years"),
                       _rule("I3", "ECOG 0-1", "performance_status", "ecog", "<=", "1"),
                       _rule("I4", "Hemoglobin >= 10 g/dL", "lab", "lab:hemoglobin", ">=", "10", "g/dL")],
               {"I4": RECORD}),
    ]


def _f(field, value, unit=None, **kw) -> SimFact:
    obs = {"observed_at": "2026-10-01"} if field.startswith("lab:") else {}
    return SimFact(field=field, value=value, unit=unit, **{**obs, **kw})


def _labs(plt, anc, hb):
    return [_f("lab:platelets", plt, "x10^9/L"), _f("lab:absolute neutrophil count", anc, "x10^9/L"),
            _f("lab:hemoglobin", hb, "g/dL")]


def cohort(salt: str = "sim-demo") -> SimCohort:
    egfr = _f("biomarker", "EGFR exon 19 deletion")
    patients = [
        ("SYN-01", [_f("age", 64, "years")], [_f("age", 64, "years"), _f("ecog", 1), *_labs(180.0, 2.1, 12.1), egfr]),
        ("SYN-02", [_f("age", 58, "years"), _f("ecog", 2, source_type="self_report")],
         [_f("age", 58, "years"), _f("ecog", 2), *_labs(90.0, 2.0, 10.4)]),
        ("SYN-03", [_f("age", 71, "years"), _f("ecog", 0),
                    _f("lab:platelets", 150.0, "x10^9/L", source_type="self_report")],
         [_f("age", 71, "years"), _f("ecog", 0), *_labs(150.0, 0.9, 8.5)]),
        ("SYN-04", [_f("age", 80, "years"), _f("ecog", 1)], [_f("age", 80, "years"), _f("ecog", 1),
                                                             *_labs(210.0, 3.0, 13.0)]),
        ("SYN-05", [_f("age", 45, "years")], [_f("age", 45, "years"), _f("ecog", 1), *_labs(250.0, 4.0, 14.2), egfr]),
        ("SYN-06", [_f("age", 52, "years"), _f("ecog", 1), _f("ecog", 3, source_type="self_report")],
         [_f("age", 52, "years"), _f("ecog", 1), *_labs(160.0, 1.8, 11.0)]),
    ]
    return make_cohort("sim-demo-nsclc-6x3", REFERENCE_DATE, trials(), patients, salt)


__all__ = ["cohort", "trials", "REFERENCE_DATE"]
