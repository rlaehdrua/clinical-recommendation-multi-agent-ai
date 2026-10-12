"""Stage 0 즉시 수정(아키텍처 §67): 외부 표현, 값싼 확정 제외, 웹 실행 기록 한도 (API 호출 없음)."""

import json

import pytest

from ctrec import config, hard_filter, metrics
from ctrec.agents import screener
from ctrec.schemas import CriterionAssessment, TrialMatch
from ctrec.status import external_status
from test_offline import _profile


def _a(rule_id, passes, **kw):
    base = dict(rule_id=rule_id, kind="inclusion", criterion_text="c", holds="unknown" if passes is None else "yes",
                passes=passes, confidence="high", evidence="e", reasoning="r", missing_info=None, method="llm")
    return CriterionAssessment(**{**base, **kw})


def _m(elig, assessments=()):
    return TrialMatch(patient_id="P", trial_id="T", eligibility=elig, assessments=list(assessments),
                      n_pass=0, n_fail=0, n_unknown=0, summary=elig)


# ------------------------------------------------------------------ 외부 표현 (§49)
def test_external_status_mapping():
    assert _m("ELIGIBLE").match_status == "CRITERIA_MATCH"
    assert _m("INELIGIBLE").match_status == "CRITERIA_MISMATCH"
    assert _m("NOT_EVALUATED").match_status == "NOT_EVALUATED"
    assert _m("UNCERTAIN", [_a("I1", None, category="lab")]).match_status == "INSUFFICIENT_INFORMATION"
    assert _m("UNCERTAIN", [_a("I1", None, underspecified=True)]).match_status == "NEEDS_REVIEW"
    unverified = _a("I1", None, category="biomarker", needs_verification=True, self_reported_holds="yes")
    assert _m("UNCERTAIN", [unverified]).match_status == "POTENTIAL_MATCH"
    # 질문으로 확인할 항목이 하나라도 남으면 '추가 정보 필요'
    assert external_status(_m("UNCERTAIN", [unverified, _a("I2", None, category="lab")])) == "INSUFFICIENT_INFORMATION"


# ------------------------------------------------------------------ 값싼 확정 제외 (§27)
def _trial(tid="T", **kw):
    return {"trial_id": tid, "overall_status": "RECRUITING", "sex": "ALL", "minimum_age": None, "maximum_age": None, **kw}


def test_hard_filter_age_sex_status(monkeypatch):
    monkeypatch.setattr(config, "HARD_FILTER_STATUS", True)
    adult = _profile(age=40, sex="female")
    assert hard_filter.check(_trial(minimum_age="18 Years", maximum_age="80 Years"), adult) is None
    assert hard_filter.check(_trial(maximum_age="17 Years"), adult).kind == "age"
    assert hard_filter.check(_trial(minimum_age="50 Years"), adult).kind == "age"
    assert hard_filter.check(_trial(sex="MALE"), adult).kind == "sex"
    assert hard_filter.check(_trial(overall_status="COMPLETED"), adult).kind == "status"
    # 상한 'N Years'는 N세까지 포함
    assert hard_filter.check(_trial(maximum_age="40 Years"), adult) is None
    monkeypatch.setattr(config, "HARD_FILTER_STATUS", False)
    assert hard_filter.check(_trial(overall_status="COMPLETED"), adult) is None


def test_hard_filter_keeps_when_unsure():
    # 나이·성별을 모르면, 단위를 해석할 수 없으면 빼지 않음
    assert hard_filter.check(_trial(minimum_age="18 Years", sex="MALE"), _profile(age=None, sex="unknown")) is None
    assert hard_filter.check(_trial(minimum_age="18 Fortnights"), _profile(age=10)) is None
    # 영아: 만 3개월(91일 이상)은 '12 Weeks'(12주 6일 = 90일까지) 상한을 넘음, 만 2개월(61~91일)은 확실하지 않아 남김
    infant = _profile(age=0, age_value=3, age_unit="months")
    assert hard_filter.check(_trial(maximum_age="12 Weeks"), infant).kind == "age"
    assert hard_filter.check(_trial(maximum_age="12 Weeks"), _profile(age=0, age_value=2, age_unit="months")) is None
    assert hard_filter.check(_trial(minimum_age="2 Years"), infant).kind == "age"
    assert hard_filter.check(_trial(maximum_age="12 Weeks"), _profile(age=0)) is None


def test_screener_skips_llm_when_all_filtered(monkeypatch):
    monkeypatch.setattr(config, "HARD_FILTER_STATUS", True)
    monkeypatch.setattr(screener.llm, "structured", lambda *a, **k: pytest.fail("모두 확정 제외면 LLM을 부르지 않음"))
    pool = {"A": _trial("A", overall_status="COMPLETED"), "B": _trial("B", sex="MALE")}
    r = screener.screen(_profile(age=40, sex="female"), pool, 5)
    assert r.pool_size == 2 and r.selected == [] and [h.trial_id for h in r.hard_filtered] == ["A", "B"]
    assert "먼저 뺀 시험 2개" in screener.report_section(r)


def test_metrics_hard_filtered(tmp_path):
    d = tmp_path / "P"
    d.mkdir()
    (d / "result.json").write_text(json.dumps({"patient_id": "P", "matches": {}, "screening": {
        "selected": [], "excluded": [], "over_limit": [],
        "hard_filtered": [{"trial_id": "A", "kind": "age", "reason": ""},
                          {"trial_id": "B", "kind": "status", "reason": ""}]}}))
    elig, _, _ = metrics.load_predictions(tmp_path)
    # 나이·성별 불일치는 부적격, 모집 종료는 적격성을 판정하지 않았으므로 예측 없음
    assert elig == {("P", "A"): "INELIGIBLE"}


# ------------------------------------------------------------------ 웹 실행 기록 한도
def test_web_runs_pruned(monkeypatch):
    app = pytest.importorskip("ctrec.web.app")
    monkeypatch.setattr(app, "RUNS", {})
    monkeypatch.setattr(app, "MAX_RUNS", 3)
    monkeypatch.setattr(app, "RUN_TTL_S", 100)
    now = 1000.0
    for i, (done, fin) in enumerate([(True, 800.0), (True, 950.0), (True, 990.0), (False, None)]):
        r = app.Run(run_id=f"r{i}", done=done, finished_at=fin, created_at=700.0 + i)
        app.RUNS[r.run_id] = r
    app._prune_runs(now)
    # r0: 끝난 지 200초(> TTL) -> 삭제, 그래도 4->3개라 한도(3) 이상 -> 가장 먼저 끝난 r1 삭제, 진행 중 r3은 유지
    assert set(app.RUNS) == {"r2", "r3"}


# ------------------------------------------------------------------ 기준선 통과 조건 (팀 결정: 1%p 이내 하락 허용)
def test_baseline_gate():
    assert metrics.baseline_gate({"accuracy": 0.795}, {"accuracy": 0.80})[0]
    assert metrics.baseline_gate({"accuracy": 0.79}, {"accuracy": 0.80})[0]  # 정확히 1%p
    assert not metrics.baseline_gate({"accuracy": 0.789}, {"accuracy": 0.80})[0]
    assert not metrics.baseline_gate({"accuracy": None}, {"accuracy": 0.80})[0]
