"""복구 스트레스: 생성기 코호트 4개(80쌍), legacy_greedy, seed 0, horizon 30으로 실패가 몰리는 응답 설정을 끝까지 실행 (LLM 없음).

R1의 측정값(H: 모든 항목이 소진 보류인데 BLOCKED가 아닌 쌍 3·14)을 회귀로 고정하고, 모든 apply_outcome 직후의 불변식을 확인합니다.
- BLOCKED로 넘어가는 순간 그 시험에 아직 실행하지 않은 환자 질문·문서 요청이 없음 (문서가 정말 소진됐을 때만 BLOCKED)
- BLOCKED 전이는 정책 전이로 기록됨
"""

from __future__ import annotations

from collections import Counter

import pytest

from ctrec import config
from ctrec.cohort import generate as G
from ctrec.engine import contracts as C
from ctrec.engine.bundles import SourceCatalog
from ctrec.engine.cohort_state import OBTAINABLE_GAPS
from ctrec.engine.policy import EXHAUSTED_DEFERS, LegacyAdapterPolicy
from ctrec.sim import from_generator as FG, runner
from ctrec.sim.response_model import DEFAULT_RESPONSES, SourceResponse

BUDGET = C.EffortBudget(coordinator_minutes=100000, clinician_minutes=100000, horizon_steps=30)
DOWN = SourceResponse(p_unreachable=1, p_no_answer=0)
UP = SourceResponse(p_unreachable=0, p_no_answer=0)
SCENARIOS = {"default": None, "patient_unreachable": {**DEFAULT_RESPONSES, "patient": DOWN, "document": UP}}
# default: 문서 게이트가 있으면 3쌍이 requested_documents_unresolved로 끝나 INFO_MISSING에 남음. 소진 여부는 동겸 결정 대기
# M-2(기간 조건 검사 기준을 규칙 엔진이 검사일로 판정) 뒤 값. Q-M 점검에서 쌍 단위로 확인:
# - default 17 -> 19: NCT07087314 3쌍이 INFO_MISSING -> BLOCKED. 7일 기간 조건 I7·I8·I9의 값이 기간 밖이라 근거가 되지 않아
#   unverified -> missing이 되고 정보원이 소진됨 (K에서 원인 미상으로 남았던 requested_documents_unresolved 3쌍)
# - 두 시나리오 모두 1쌍(NCT07087314)이 BLOCKED -> CONFIRMED_INELIGIBLE: 기간 안 혈소판 I8이 규칙 엔진으로 불충족. 시뮬레이터
#   GT 라벨도 ineligible (확정 부적격 전체가 라벨과 일치, 확정 적격 0)
# - 아래 불변식(소진 쌍이 BLOCKED 아닌 채 남지 않음, 기록 없는 BLOCKED 0, 실행 가능한 행동이 남은 BLOCKED 0)은 M 전후 모두 성립
EXPECTED_BLOCKED = {"default": 19, "patient_unreachable": 21}


@pytest.fixture(scope="module")
def cohorts():
    cfg, sd, cat, parsed = G.load_inputs(config.DATA_DIR / "cohort" / "generator_config.json")
    smap = FG.load_source_map(config.ROOT / "data" / "cohort" / "source_map.json")
    cs = G.generate_all(cfg, sd, cat, parsed)[:4]
    trials = FG.load_trials(cs[0].public.trials)
    return [FG.to_sim_cohort(gc, sd, smap, trials) for gc in cs]


def _stranded(final: runner.CohortState) -> list:
    """얻을 수 있는 gap만 남았고 그 항목의 행동이 전부 소진 보류인데 BLOCKED가 아닌 쌍."""
    pol = LegacyAdapterPolicy(catalog=SourceCatalog())
    out = []
    for alias in final.aliases():
        acts: dict = {}
        for a in pol.propose(final):
            if a.patient_alias == alias and a.info_key:
                acts.setdefault(a.info_key, set()).add((a.action_type, a.reason_code))
        for p in final.patients[alias].pairs:
            t = final.patients[alias]._legacy.trials.get(p.pair.trial_id)
            if t is None or p.status not in (C.PairStatus.INFO_MISSING, C.PairStatus.VERIFICATION_PENDING):
                continue
            left = [c for c in t.criteria if c.requirement == "required" and (c.gap != "none" or c.conflict_on_decided)]
            if left and all(c.gap in OBTAINABLE_GAPS and not c.conflict_on_decided for c in left) and all(
                    acts.get(c.info_key) and all(k == "DEFER" and r in EXHAUSTED_DEFERS for k, r in acts[c.info_key])
                    for c in left):
                out.append(p.pair.key)
    return out


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_all_exhausted_pairs_are_blocked_and_blocks_are_real(cohorts, name, monkeypatch):
    seen = Counter()
    done: set = set()
    probe = LegacyAdapterPolicy(catalog=SourceCatalog())
    original = LegacyAdapterPolicy.apply_outcome

    def checked(self, cohort, action, outcome):
        new, transitions = original(self, cohort, action, outcome)
        alias = action.patient_alias
        done.add((alias, action.fingerprint, action.action_type, action.source_id))
        recorded = {t.pair.trial_id for t in transitions}
        moved = [p for p in new.patients[alias].pairs if p.status == C.PairStatus.BLOCKED_FAILURE
                 and cohort.pair(alias, p.pair.trial_id).status != p.status]
        for p in moved:
            seen["blocked"] += 1
            seen["unrecorded"] += p.pair.trial_id not in recorded
            seen["live"] += any(a.patient_alias == alias and p.pair.trial_id in a.target_trial_ids
                                and a.action_type in ("ASK_PATIENT", "REQUEST_DOCUMENT")
                                and (alias, a.fingerprint, a.action_type, a.source_id) not in done
                                for a in probe.propose(new))
        return new, transitions

    monkeypatch.setattr(LegacyAdapterPolicy, "apply_outcome", checked)
    final, stranded = Counter(), []
    for sim in cohorts:
        done.clear()
        r = runner.run(runner.build_spec("stress", "legacy_greedy", sim, BUDGET, seed=0), sim, responses=SCENARIOS[name])
        assert not r.issues
        final.update(p.status.value for p in r.final.pairs())
        stranded += _stranded(r.final)
    assert stranded == []  # H 기준 default 3쌍, patient_unreachable 14+8쌍
    assert final[C.PairStatus.BLOCKED_FAILURE.value] == EXPECTED_BLOCKED[name]
    assert seen["blocked"] and seen["unrecorded"] == 0 and seen["live"] == 0
