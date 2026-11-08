"""임신·수유 기준의 성별 적용 대상 (데모 시나리오 C 관찰, docs/dev/demo_nsclc_scenarios.md).

재현 (1f04229): P061(남성, 성별 공개)에 NCT06216301:E16 "Pregnant or breastfeeding"을 환자에게 묻고(step 917) 문서까지
요청함. GT(rule_gt.csv)는 sex=male -> NOT_APPLICABLE. 원인은 오프라인 판정기(sim.judge.CategoricalJudge):
- 성별 사실(field sex, 값 male)을 슬롯 sex로 읽지 않아 연결의 적용 대상 조건(also: sex in female)이 늘 '모름'
- 판정 결과가 성별로 정해져도 인용은 주 슬롯(임신·수유) 줄이라 Matcher 근거 검사에서 보류

기존 Matcher 의미(바꾸지 않음): 임신·수유 기준에서 구조화 필드 성별(sex: male) 근거의 '해당 없음'은 인정
(evidence_directness.check_structured 예외). 판정기는 "LLM이 완벽하게 읽었다면"의 상한이므로 그 형식으로 답함.
여성·성별 미상은 그대로 (임신·수유 값이 없으면 판정 불가). '해당 없음' 외의 확정 근거는 늘리지 않음.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_nsclc_eval as B  # noqa: E402
from ctrec.cohort import generate as gen  # noqa: E402
from ctrec.cohort.spec import Cohort  # noqa: E402
from ctrec.engine.policy import LegacyAdapterPolicy  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.sim.evaluator import OfflineEvaluator, make_fact  # noqa: E402

TID = "NCT06216301"


def _sim():
    c = Cohort.model_validate(json.loads(B.GENERATED.read_text(encoding="utf-8")))
    _, sd, _, _ = gen.load_inputs(B.CONFIG)
    return B.sim_cohort(c, sd)


def _state(sim, key, drop=()):
    alias = sim.aliases()[key]
    p = next(x for x in sim.patients if x.key == key)
    facts = [make_fact(alias, f, seq=0, origin=f"public{i}", source_type=f.source_type, verified=f.verified,
                       ref_kind="input_text", ref=None) for i, f in enumerate(p.public) if f.field not in drop]
    st, _ = OfflineEvaluator(sim.trials, sim.reference_date, sim.judge).evaluate(alias, facts, p.trials)
    return alias, st


def _crit(st, rid):
    return next(c for c in st.trials[TID].criteria if c.rule_id == rid)


def test_male_patient_pregnancy_criteria_are_not_applicable_and_not_asked():
    sim = _sim()
    for drop in ((), ("reproductive",)):  # 임신·수유 값이 공개돼 있어도, 없어도
        alias, st = _state(sim, "P061", drop)
        for rid in ("E16", "I9"):
            c = _crit(st, rid)
            assert c.status == "NOT_APPLICABLE" or (c.status == "MET" and c.gap == "none"), (drop, rid, c)
            assert c.gap == "none", (drop, rid, c)
        co = CohortState.from_decision_states({alias: st})
        acts = LegacyAdapterPolicy().propose(co)
        asked = [a for a in acts if {f"{TID}:E16", f"{TID}:I9"} & set(a.target_criteria)]
        assert not asked, [(a.action_type, a.info_key) for a in asked]
        pair = co.pair(alias, TID)
        assert pair.status.value != "CONFIRMED_ELIGIBLE"  # 다른 미연결 기준(I8·E11·E12)이 남음


def test_female_or_unknown_sex_still_needs_the_answer():
    sim = _sim()
    female = next(p.key for p in sim.patients if TID in (p.trials or ())
                  and any(f.field == "sex" and f.value == "female" for f in p.public))
    _, st = _state(sim, female, ("reproductive",))
    assert _crit(st, "E16").gap != "none" and _crit(st, "E16").status == "UNKNOWN"
    _, st = _state(sim, "P061", ("reproductive", "sex"))
    assert _crit(st, "E16").gap != "none" and _crit(st, "E16").status == "UNKNOWN"
