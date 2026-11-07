"""NSCLC 최소 파일럿 (P): 실제 NSCLC 프로토콜 6개(data/nsclc/parsed, 저장소에 동결된 파싱 기준) x 합성 환자 8명.

- 시험 기준은 실제 프로토콜의 파싱 결과 그대로입니다 (data/nsclc/parsed/*.json, data/nsclc/trials/*.json).
- 환자는 **합성**입니다 (SYN-NSCLC-*). 실제 환자가 아닙니다.
- 판정기(sim.judge) 없음 -> LLM이 필요한 기준(조직학·치료력·장기 기능 등)은 오프라인에서 '판정 불가'. 이 파일럿에서
  확정 적격(CONFIRMED_ELIGIBLE)은 원리적으로 나올 수 없고, 나오면 결함입니다. 확정 부적격은 규칙 엔진 기준(나이·ECOG)으로만.
- GT: 독립 GT 없음. 대신 작성자가 프로토콜 원문을 읽고 붙인 **부적격 전용 라벨**(MANUAL_INELIGIBLE: 나이·ECOG 상한·하한이
  원문에서 명백히 불충족)만 둡니다. 임상 검수 전이며, 그 밖의 쌍은 라벨 없음입니다. 적격 라벨은 없습니다.
  SUSPECT_FALSE_INELIGIBLE: 원문상 부적격이 아니라고 읽히는 쌍 (예: NCT06216301 I1 "미국에서는 22세 이상" - 국가 조건,
  한국 기관의 20세 환자는 I2 "18세 이상"이 적용). 시스템이 확정 부적격이면 false_ineligible로 셉니다.
"""

from __future__ import annotations

from pathlib import Path

from .. import config as app_config
from .cohort import SimCohort, SimFact, make_cohort
from .from_generator import load_trials
from .metrics import ELIGIBLE, INELIGIBLE, UNLABELED

REFERENCE_DATE = "2026-10-12"
TRIALS = ("NCT06216301", "NCT06928389", "NCT07027514", "NCT07098338", "NCT07416058", "NCT07680790")
GT_KIND = "manual_protocol_reading_ineligible_only"


def _f(field, value, unit=None, **kw) -> SimFact:
    return SimFact(field=field, value=value, unit=unit, **kw)


def _age(v):
    return _f("age", v, "years")


# (키, 공개 사실, 정답 사실). 나이·성별은 진료 기록 출처로 공개, ECOG는 숨김(환자·진료 기록이 앎)
PATIENTS = [
    ("SYN-NSCLC-01", [_age(64), _f("sex", "male")], [_age(64), _f("sex", "male"), _f("ecog", 1)]),
    ("SYN-NSCLC-02", [_age(78), _f("sex", "female")], [_age(78), _f("sex", "female"), _f("ecog", 1)]),
    ("SYN-NSCLC-03", [_age(20), _f("sex", "female")], [_age(20), _f("sex", "female"), _f("ecog", 0)]),
    ("SYN-NSCLC-04", [_age(55), _f("sex", "male")], [_age(55), _f("sex", "male"), _f("ecog", 2)]),
    ("SYN-NSCLC-05", [_age(60), _f("sex", "male"), _f("ecog", 1, source_type="self_report")],
     [_age(60), _f("sex", "male"), _f("ecog", 1)]),
    ("SYN-NSCLC-06", [_age(70), _f("sex", "female")], [_age(70), _f("sex", "female"), _f("ecog", 0)]),
    ("SYN-NSCLC-07", [_age(45)], [_age(45), _f("sex", "male"), _f("ecog", 1)]),
    ("SYN-NSCLC-08", [_age(81), _f("sex", "male")], [_age(81), _f("sex", "male"), _f("ecog", 3)]),
]
_AGE_MAX75 = ("NCT06928389", "NCT07416058")  # 원문 "Age ≤ 75"
_ECOG_MAX1 = TRIALS  # 6개 모두 원문 ECOG 0-1
MANUAL_INELIGIBLE = {
    ("SYN-NSCLC-02", t) for t in _AGE_MAX75} | {
    ("SYN-NSCLC-08", t) for t in _AGE_MAX75} | {
    ("SYN-NSCLC-04", t) for t in _ECOG_MAX1} | {
    ("SYN-NSCLC-08", t) for t in _ECOG_MAX1}
SUSPECT_FALSE_INELIGIBLE = {("SYN-NSCLC-03", "NCT06216301")}


def cohort(salt: str = "sim-nsclc-pilot") -> SimCohort:
    root = app_config.ROOT / "data" / "nsclc"
    trials = load_trials(list(TRIALS), Path(root / "parsed"), Path(root / "trials"))
    return make_cohort("sim-nsclc-pilot-8x6", REFERENCE_DATE, trials, PATIENTS, salt)


def labels(co: SimCohort) -> dict[str, str]:
    """PairKey.key -> ineligible (작성자 원문 판독) / eligible (원문상 부적격이 아님이 분명한 '의심' 쌍 표시용) / unlabeled.

    SUSPECT_FALSE_INELIGIBLE 쌍은 '적격'이 아니라 '이 기준으로 부적격이 아님'이지만, false_ineligible을 세려고 eligible
    자리에 둡니다. 따라서 verified_eligible·false_eligible 칸은 이 파일럿에서 의미가 없습니다 (확정 적격이 나오면 결함)."""
    alias = co.aliases()
    out = {}
    for key, _, _ in PATIENTS:
        for t in TRIALS:
            lab = INELIGIBLE if (key, t) in MANUAL_INELIGIBLE else ELIGIBLE if (key, t) in SUSPECT_FALSE_INELIGIBLE \
                else UNLABELED
            out[f"{alias[key]}|{t}"] = lab
    return out


def suite_cohort():
    from .suite import SuiteCohort
    co = cohort()
    return SuiteCohort(co, labels(co), GT_KIND, "실제 NSCLC 프로토콜 6개 x 합성 환자 8명, 작성자 원문 판독 부적격 라벨만")


__all__ = ["cohort", "labels", "suite_cohort", "TRIALS", "PATIENTS", "MANUAL_INELIGIBLE", "SUSPECT_FALSE_INELIGIBLE",
           "GT_KIND"]
