"""임상의 판단 경계의 WP6 경로 (PR #20 OfflineEvaluator). API 호출 없음, 값은 모두 합성.

ctrec.sim은 PR #20이 들어와야 있으므로, 그 전에는 모듈 전체가 건너뜁니다. 건너뜀은 검증이 아닙니다.
실행 결과는 PR #20을 합친 트리에서만 근거가 됩니다.

저장 규칙에 임의 reviewed 연결(Binding)을 붙인 판정기로 '검수 완료 연결이 임상 판단을 대신하는' 우회를 재현합니다.
임상의 판단 완료 입력은 만드는 코드가 없어 시험하지 않습니다.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("ctrec.sim.judge")

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config, expert_boundary as xb  # noqa: E402
from ctrec.cohort.spec import Binding, SlotSpec  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.eval_hygiene import AliasMap  # noqa: E402
from ctrec.schemas import LLMAssessment, ParsedTrial  # noqa: E402
from ctrec.sim import from_generator as FG  # noqa: E402
from ctrec.sim.cohort import SimFact, SimTrial  # noqa: E402
from ctrec.sim.evaluator import OfflineEvaluator, make_fact  # noqa: E402
from ctrec.sim.judge import CategoricalJudge  # noqa: E402

from test_offline import _rule  # noqa: E402

REF = "2026-10-01"
NSCLC = config.ROOT / "data" / "nsclc"
SLOT = "other.probe"
SPEC = SlotSpec(slot_id=SLOT, kind="categorical", category="other", source="S_chart", values=["yes", "no"])
QUOTE = "other_fact: probe"
PASSING, FAILING = "pass", "fail"
CLASSIFIED = [("NCT07680790", "E3"), ("NCT07098338", "I4"), ("NCT07098338", "I5"), ("NCT07098338", "E10"), ("NCT07680790", "I2"),
              ("NCT07027514", "I7"), ("NCT06928389", "I5")]


def _stored(trial_id):
    return FG.load_trials([trial_id], NSCLC / "parsed", NSCLC / "trials")[0]


def _binding(trial_id, rule, outcome):
    holds = "yes" if (outcome == PASSING) == (rule.kind == "inclusion") else "no"  # 값 'yes'는 op in ['yes']로 성립
    return Binding(trial_id=trial_id, rule_id=rule.rule_id, kind=rule.kind, category=rule.category, text="probe", status="reviewed",
                   slot=SLOT, truth="categorical", op="in", values=[holds])


class _Fixed(CategoricalJudge):
    """슬롯 값을 사실 표기와 무관하게 yes로 고정. 판정기 대상 기준의 입력만 통제합니다."""

    def values(self, facts):
        return {SLOT: "yes"}, {SLOT: QUOTE}


def _judge(bindings):
    return _Fixed(bindings, {SLOT: SPEC}, REF)


def _facts(alias, *, age=50):
    plain = [SimFact(field="age", value=age, unit="years"), SimFact(field="sex", value="female"), SimFact(field="ecog", value=0),
             SimFact(field="other_fact", value="probe")]
    return [make_fact(alias, f, seq=0, origin=f"p{i}", source_type="clinical_record", verified=True, ref_kind="document", ref=None)
            for i, f in enumerate(plain)]


def _pair(trial, judge, **kw):
    alias = AliasMap(salt="wp6-boundary").alias("probe")
    state, _ = OfflineEvaluator((trial,), REF, judge).evaluate(alias, _facts(alias, **kw))
    return CohortState.from_decision_states({alias: state}).pairs()[0]


class _PassAll:
    """모든 LLM 기준을 통과로 답하는 최악의 판정기 (임상의 판단 기준 포함)."""
    judge_id, include_draft = "pass_all", False

    def digest(self):
        return "pass_all"

    def assess(self, trial_id, rule, facts):
        return LLMAssessment(rule_id=rule.rule_id, applicable=True, holds="yes" if rule.kind == "inclusion" else "no",
                             evidence_type="explicit", confidence="high", evidence=QUOTE, reasoning="pass_all", missing_info=None)


# ---------------------------------------------------------------------------
# reviewed 연결로 임상 판단을 대신하지 못함
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", [PASSING, FAILING])
@pytest.mark.parametrize("trial_id, rule_id", CLASSIFIED)
def test_reviewed_binding_cannot_settle_a_clinician_rule(trial_id, rule_id, outcome):
    trial = _stored(trial_id)
    rule = next(r for r in trial.parsed.rules if r.rule_id == rule_id)
    assert xb.clinician_reason(trial_id, rule)
    judge = _judge([_binding(trial_id, rule, outcome)])
    c = next(c for c in _pair(trial, judge).criteria if c.rule_id == rule_id)
    assert (c.state, c.adjudication, c.gap, c.decided_by) == ("UNKNOWN", "expert", "physician_review", "none")
    assert judge.judged == 1  # 판정기는 실제로 값을 냈고, 경계가 막음


@pytest.mark.parametrize("trial_id", sorted({t for t, _ in CLASSIFIED}))
def test_pass_all_judge_cannot_confirm_a_trial_with_a_clinician_rule(trial_id):
    pair = _pair(_stored(trial_id), _PassAll())
    assert pair.status.value != "CONFIRMED_ELIGIBLE"
    assert any(c.adjudication == "expert" and c.gap == "physician_review" for c in pair.criteria)


# ---------------------------------------------------------------------------
# 대조군: 문서 검증된 결정적 근거와 일반 reviewed 연결은 그대로
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trial_id", ["NCT07098338", "NCT07027514", "NCT06928389"])
def test_document_verified_failure_stays_confirmed_ineligible(trial_id):
    """나이 10세가 문서 검증됨: 임상의 판단 기준이 미확정이어도 부적격 확정."""
    pair = _pair(_stored(trial_id), _PassAll(), age=10)
    assert pair.status.value == "CONFIRMED_INELIGIBLE"
    assert any(c.adjudication == "expert" and c.state == "UNKNOWN" for c in pair.criteria)


def _synthetic(*, underspecified):
    age = _rule(rule_id="I1", text="Age 18 years or older", field="age", operator=">=", value="18", unit="years")
    dx = _rule(rule_id="I2", text="Histologically confirmed NSCLC", category="diagnosis", structured_evaluable=False,
               underspecified=underspecified)
    parsed = ParsedTrial(trial_id="T1", title="t", target_population="", intervention_summary="", key_conditions=[], cohorts=[],
                         rules=[age, dx], parsing_notes="")
    return SimTrial(parsed, {"trial_id": "T1", "status": "RECRUITING"}), dx


@pytest.mark.parametrize("outcome, status", [(PASSING, "CONFIRMED_ELIGIBLE"), (FAILING, "CONFIRMED_INELIGIBLE")])
def test_ordinary_reviewed_binding_still_decides(outcome, status):
    trial, dx = _synthetic(underspecified=False)
    assert _pair(trial, _judge([_binding("T1", dx, outcome)])).status.value == status


@pytest.mark.parametrize("outcome", [PASSING, FAILING])
def test_the_same_binding_is_held_once_the_rule_is_clinical(outcome):
    """바로 위 대조군과 플래그만 다름: 차단의 원인이 경계임을 보임."""
    trial, dx = _synthetic(underspecified=True)
    assert _pair(trial, _judge([_binding("T1", dx, outcome)])).status.value == "EXPERT_REVIEW"
