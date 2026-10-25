"""P14 백엔드 잔여 문제 (팀원 보고) - 재현·분류·수정 검증. 오프라인 대역, 유료 API 호출 없음.

A. NCT04063540이 답한 뒤에도 '정보 부족 13개': 웹 테스트 대역(web_harness.FakeLLM)이 모든 기준을 항상 unknown(missing_info="확인 필요")으로
   돌려주고, 질문도 대상 기준이 없는(target=[]) "EGFR 검사 결과를 아시나요?" 하나뿐이라 생기는 테스트 한계(대역 고정 출력).
   실제 웹 경로는 답변 뒤 다시 매칭하고, 기준을 겨냥한 검증 가능한 답은 반영함 (P12) - 아래 웹 수준 테스트로 확인
B. 상세 화면의 '필요: 확인 필요': 같은 대역 값. 실제 경로에서도 비었거나 일반 문구뿐인 missing_info는 원문 기준 문장으로 바꿈 (새 요구사항 없음)
C. 웹 검색 결과가 data/trials에 저장되어 시험 목록·잠긴 기준표 검사가 바뀌던 문제: 검색 결과는 data/cache/ctgov로 분리
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config, qa_guard
from ctrec.agents import matcher
from ctrec.schemas import ClarifyingQuestion, LLMAssessment, MatcherOutput, QuestionSet
from ctrec.tools import ctgov
from test_offline import _profile, _rule
from test_stage05 import _trial


# ============================================================ C. CT.gov 검색 결과 저장 위치
class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def _study(nct):
    return {"protocolSection": {"identificationModule": {"nctId": nct, "briefTitle": f"t {nct}"},
                                "statusModule": {"overallStatus": "RECRUITING"},
                                "eligibilityModule": {"eligibilityCriteria": "Inclusion Criteria:\n- Adults"}}}


@pytest.fixture()
def dirs(monkeypatch, tmp_path):
    trials, cache = tmp_path / "trials", tmp_path / "cache" / "ctgov"
    trials.mkdir()
    monkeypatch.setattr(config, "TRIALS_DIR", trials)
    monkeypatch.setattr(config, "CTGOV_CACHE_DIR", cache)
    return trials, cache


def test_search_results_go_to_cache_not_trials_dir(monkeypatch, dirs):
    trials, cache = dirs
    (trials / "NCT00000001.json").write_text('{"trial_id": "NCT00000001", "curated": true}', encoding="utf-8")
    before = {p.name: p.read_bytes() for p in trials.iterdir()}
    monkeypatch.setattr(ctgov.requests, "get", lambda *a, **k: _Resp({"studies": [_study("NCT00000001"),
                                                                                 _study("NCT00000002")]}))
    found = ctgov.search_studies("NSCLC", max_results=5)
    assert [r["trial_id"] for r in found] == ["NCT00000001", "NCT00000002"]
    assert {p.name: p.read_bytes() for p in trials.iterdir()} == before  # 대회 시험 파일: 추가·덮어쓰기 없음
    saved = json.loads((cache / "NCT00000002.json").read_text(encoding="utf-8"))
    assert saved["source"]["retrieved_at"] and saved["trial_id"] == "NCT00000002"  # 출처 보존


def test_cli_fetch_study_keeps_writing_trials_dir(monkeypatch, dirs):
    trials, cache = dirs
    monkeypatch.setattr(ctgov.requests, "get", lambda *a, **k: _Resp(_study("NCT00000003")))
    ctgov.fetch_study("NCT00000003")
    assert (trials / "NCT00000003.json").exists() and not cache.exists()  # 사람이 고른 시험 추가(기존 동작)


def test_cache_dir_is_git_ignored():
    gi = (config.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/cache/" in gi and config.CTGOV_CACHE_DIR.is_relative_to(config.CACHE_DIR)


# ============================================================ B. missing_info
RULE = _rule(rule_id="E9", kind="exclusion", text="Pregnant or lactating patient", category="reproductive",
             structured_evaluable=False)


def _match(monkeypatch, missing, holds="unknown", evidence="근거 없음"):
    monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    a = LLMAssessment(rule_id="E9", applicable=True, holds=holds, evidence_type="absent" if holds == "unknown"
                      else "explicit", confidence="low", evidence=evidence, reasoning="r", missing_info=missing)
    monkeypatch.setattr(matcher.llm, "structured", lambda *x, **k: MatcherOutput(assessments=[a]))
    return matcher.match_trial(_profile(), _trial(RULE), {"brief_summary": ""}, reference_date="2026-10-25",
                               source_text="34세 여성. 임신 여부 기록 없음.").assessments[0]


@pytest.mark.parametrize("generic", ["확인 필요", "", None, "정보 부족", "unknown", "확인 필요."])
def test_generic_missing_info_shows_original_criterion(monkeypatch, generic):
    a = _match(monkeypatch, generic)
    assert a.passes is None and a.missing_info == "이 기준을 확인할 정보 (원문): Pregnant or lactating patient"


def test_specific_missing_info_is_kept(monkeypatch):
    a = _match(monkeypatch, "현재 임신·수유 여부")
    assert a.missing_info == "현재 임신·수유 여부"


def test_decided_criteria_are_unchanged(monkeypatch):
    a = _match(monkeypatch, None, holds="no", evidence="임신 여부 기록 없음")  # 근거가 부재 진술이 아님 -> 보류
    assert a.passes is None and a.missing_info and "확인 필요" != a.missing_info


# ============================================================ A. 웹 경로 재평가 (NCT04063540)
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
import web_harness as h  # noqa: E402

from ctrec.web import app as web  # noqa: E402

PREG_Q = "현재 임신 중이거나 수유 중인가요?"


class _TargetedFake(h.FakeLLM):
    """NCT04063540:E9를 겨냥한 예/아니요 질문 + 답한 뒤에는 모델이 근거를 의역해 'no'로 판정 (실제 실행에서 본 양상)."""

    def structured(self, model_cls, *, system, user, **k):
        if model_cls is QuestionSet:
            self.calls.append("QuestionSet")
            return QuestionSet(questions=[ClarifyingQuestion(
                question_id="Q1", question=PREG_Q, answer_type="yes_no", target=["NCT04063540:E9"],
                purpose="eligibility", why="E9")])
        if model_cls is MatcherOutput and "NCT04063540" in user and self.calls.count("QuestionSet"):
            out = super().structured(model_cls, system=system, user=user, **k)
            for a in out.assessments:
                if a.rule_id == "E9":
                    a.holds, a.evidence_type, a.confidence, a.evidence = "no", "explicit", "high", "임신·수유 아님"
            return out
        return super().structured(model_cls, system=system, user=user, **k)


def _web_run(monkeypatch, tmp_path, fake, answer):
    monkeypatch.delenv("CTREC_SESSION", raising=False)
    monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    h.install(monkeypatch, tmp_path, fake)
    c = h.browser()
    r = c.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False})
    rid = r.json()["run_id"]
    run = web.RUNS[rid]
    q = h.wait_for(run, "questions")
    c.post(f"/api/runs/{rid}/answers", json={"answers": {x["question_id"]: answer for x in q["questions"]}})
    h.wait_for(run, "end", 30)
    events = h.sse_events(c.get(f"/api/runs/{rid}/events").text)
    return q, next(e for e in events if e["type"] == "result")["result"]


def test_default_harness_question_targets_no_criterion_so_count_cannot_drop(monkeypatch, tmp_path):
    """팀원 보고 재현: 대역 질문은 대상 기준이 없고 Matcher 대역은 항상 unknown -> NCT04063540 판정 불가 수 그대로 (테스트 한계)."""
    q, res = _web_run(monkeypatch, tmp_path, h.FakeLLM(), h.SYN_ANSWER)
    assert q["questions"][0]["target"] == []
    m = res["matches"]["NCT04063540"]
    assert m["n_pass"] + m["n_fail"] == sum(a["method"] == "rule_engine" for a in m["assessments"] if a["passes"] is not None)
    assert all(a["missing_info"] != "확인 필요" for a in m["assessments"] if a["passes"] is None)  # B 개선 반영


def test_web_reevaluation_reflects_verified_targeted_answer(monkeypatch, tmp_path):
    fake = _TargetedFake()
    _, res = _web_run(monkeypatch, tmp_path, fake, "아니요")
    assert fake.calls.count("MatcherOutput") >= 4  # 답변 뒤 다시 매칭 (2개 시험 x 2라운드)
    m = res["matches"]["NCT04063540"]
    e9 = next(a for a in m["assessments"] if a["rule_id"] == "E9")
    assert e9["passes"] is True and e9["evidence"] == f"Q: {PREG_Q} A: 아니요"
    assert e9["validation_flags"] == ["answer_verified"] and e9["evidence_source"] == "self_report"
    assert res["qa_log"][0]["answer"] == "아니요"


def test_web_reevaluation_keeps_unverifiable_answer_unresolved(monkeypatch, tmp_path):
    fake = _TargetedFake()
    _, res = _web_run(monkeypatch, tmp_path, fake, "모름")
    e9 = next(a for a in res["matches"]["NCT04063540"]["assessments"] if a["rule_id"] == "E9")
    assert e9["passes"] is None  # 모름은 근거가 아님 (P12 조건 그대로)
