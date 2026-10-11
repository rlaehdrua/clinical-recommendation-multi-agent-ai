"""후보 추림: 화이트리스트·안전한 기본값·상한, Session 연동, 평가 지표 (API 호출 없음)."""

import json
from pathlib import Path

from ctrec.agents import screener
from ctrec.agents.screener import ScreenedTrial, ScreeningOutput
from ctrec.metrics import load_predictions, screening_metrics
from ctrec.pipeline import PatientCase, Session
from ctrec.schemas import PatientProfile

POOL = {t: {"trial_id": t, "title": t, "conditions": [], "eligibility_criteria": ""} for t in ("A", "B", "C", "D", "E")}


def _profile():
    return PatientProfile(patient_id="P", age=50, sex="male", primary_diagnosis="pancreatitis", disease_stage=None,
                          diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[], ecog=None,
                          other_facts=[], missing_or_ambiguous=[])


def _s(t, d, r="high"):
    return ScreenedTrial(trial_id=t, decision=d, relevance=r, reason="r")


def _fake(monkeypatch, items):
    monkeypatch.setattr(screener.llm, "structured", lambda *a, **k: ScreeningOutput(trials=items))


def test_screen_whitelist_default_keep_and_limit(monkeypatch):
    # A: low keep, B: high keep, C: exclude, D: 응답 누락(-> 안전하게 keep), Z: 없는 ID, B 중복
    _fake(monkeypatch, [_s("A", "keep", "low"), _s("B", "keep", "high"), _s("C", "exclude", "low"),
                        _s("Z", "keep"), _s("B", "exclude"), _s("E", "keep", "medium")])
    r = screener.screen(_profile(), POOL, max_candidates=2)
    assert [s.trial_id for s in r.selected] == ["B", "E"]          # 적합도 순
    assert [s.trial_id for s in r.over_limit] == ["A", "D"]        # 상한 초과는 따로 기록
    assert [s.trial_id for s in r.excluded] == ["C"]
    assert r.kept_by_default == ["D"] and r.pool_size == 5
    assert "후보 시험 추림" in screener.report_section(r)


class _Tracer:
    def log(self, *a, **k):
        pass


def test_session_evaluates_only_screened_candidates(monkeypatch):
    _fake(monkeypatch, [_s("A", "exclude"), _s("B", "keep"), _s("C", "exclude"), _s("D", "exclude"), _s("E", "keep")])
    s = Session(case=PatientCase("P", ""), trials=dict(POOL), answerer=None, tracer=_Tracer(), screen_trials=True)
    assert s.candidate_ids() == list(POOL)  # 추림 전에는 전체
    s.profile = _profile()
    s.screen()
    assert s.candidate_ids() == ["B", "E"]
    parsed = []
    monkeypatch.setattr("ctrec.config.LAZY_COMPILE", True)  # 고정 저장소에 없는 가짜 시험 -> 즉석 파싱
    monkeypatch.setattr("ctrec.pipeline.criteria_parser.parse_trial", lambda t: parsed.append(t["trial_id"]) or None)
    s.parse_criteria()
    assert sorted(parsed) == ["B", "E"]
    # 검색으로 찾은 시험은 바로 후보에 추가
    s.trials["F"] = {"trial_id": "F"}
    s.add_candidates(["F"])
    assert s.candidate_ids() == ["B", "E", "F"]


def test_screening_metrics_and_predictions(tmp_path: Path):
    d = tmp_path / "P"
    d.mkdir()
    sc = {"pool_size": 5, "selected": [{"trial_id": "A"}, {"trial_id": "X"}],
          "excluded": [{"trial_id": "B"}], "over_limit": [{"trial_id": "C"}], "kept_by_default": []}
    (d / "result.json").write_text(json.dumps({"patient_id": "P", "matches": {"A": {"eligibility": "ELIGIBLE"}},
                                               "screening": sc, "recommendation": None}), encoding="utf-8")
    rows = [{"patient_id": "P", "trial_id": "A", "label": "ELIGIBLE", "auto": "0"},
            {"patient_id": "P", "trial_id": "B", "label": "ELIGIBLE", "auto": "0"},
            {"patient_id": "P", "trial_id": "X", "label": "INELIGIBLE", "auto": "1"}]
    m = screening_metrics(rows, tmp_path)
    assert m["eligible_recall"] == 0.5 and m["missed_eligible"] == ["P/B"]
    assert m["off_topic_per_patient"] == 1 and m["selected_per_patient"] == 2
    pred, _, _ = load_predictions(tmp_path)
    assert pred[("P", "A")] == "ELIGIBLE" and pred[("P", "B")] == "INELIGIBLE" and pred[("P", "C")] == "UNCERTAIN"
