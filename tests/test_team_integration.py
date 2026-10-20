"""Phase 9: 팀원 커밋(4ea12d6) 통합 - 객관식 질문·한국어 번역·다시 보기 비활성화·trace 개인정보·규칙 엔진 의미론.
합성 데이터 + 오프라인 대역만 (유료 호출 없음)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import web_harness as h  # noqa: E402

from ctrec import config  # noqa: E402
from ctrec.schemas import ClarifyingQuestion, LabValue, PatientProfile, QuestionSet  # noqa: E402
from ctrec.tools import rule_engine  # noqa: E402
from ctrec.web import app as web  # noqa: E402

BODY = {"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}
OPTS_SINGLE = ["4주 이내", "4주~3개월 전", "3개월 이상 전", "기타(직접 입력)"]
OPTS_MULTI = ["고혈압", "당뇨, 2형", "해당 없음"]


class ChoiceLLM(h.FakeLLM):
    """모든 질문 형식을 한 번에 묻고, 다시 정리할 때 프로파일러가 받은 원문을 기록."""

    def __init__(self):
        super().__init__()
        self.profiler_inputs: list[str] = []

    def structured(self, model_cls, *, system, user, **k):
        if model_cls is PatientProfile:
            self.profiler_inputs.append(user)
        if model_cls is QuestionSet:
            self.calls.append("QuestionSet")
            q = lambda i, t, **kw: ClarifyingQuestion(question_id=i, question=f"합성 질문 {i}", answer_type=t,  # noqa: E731
                                                      target=[], purpose="eligibility", why="합성", **kw)
            return QuestionSet(questions=[q("Q1", "yes_no"), q("Q2", "single_choice", options=OPTS_SINGLE),
                                          q("Q3", "multi_choice", options=OPTS_MULTI), q("Q4", "number", unit="g/dL"),
                                          q("Q5", "free_text")])
        return super().structured(model_cls, system=system, user=user, **k)


@pytest.fixture()
def choice(monkeypatch, tmp_path):
    return h.install(monkeypatch, tmp_path, ChoiceLLM())


def _start(c):
    r = c.post("/api/runs", json=BODY)
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    return rid, web.RUNS[rid]


# ---------------------------------------------------------------- 객관식 질문: 화면 -> 서버 검증 -> 반영 -> 재분석


def test_questions_carry_choice_fields_to_browser(choice):
    c = h.browser()
    rid, run = _start(c)
    q = h.wait_for(run, "questions")
    by = {x["question_id"]: x for x in q["questions"]}
    assert by["Q2"]["options"] == OPTS_SINGLE and by["Q3"]["answer_type"] == "multi_choice" and by["Q4"]["unit"] == "g/dL"
    assert by["Q1"]["options"] == [] and by["Q5"]["unit"] is None


@pytest.mark.parametrize("qid, bad", [("Q1", "yes"), ("Q1", "아마도"), ("Q2", "2주"), ("Q2", "4주 이내, 3개월 이상 전"),
                                      ("Q3", "고혈압, 천식"), ("Q3", "당뇨")])
def test_invalid_choice_rejected_and_question_stays_open(choice, qid, bad):
    c = h.browser()
    rid, run = _start(c)
    h.wait_for(run, "questions")
    r = c.post(f"/api/runs/{rid}/answers", json={"answers": {qid: bad}})
    assert r.status_code == 400 and "선택지에 없는 답변" in r.json()["detail"] and bad not in r.text
    assert run.pending_questions  # 질문은 그대로 대기 -> 고쳐서 다시 보낼 수 있음
    ok = c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "예"}})
    assert ok.status_code == 200
    assert c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "예"}}).status_code == 409  # 중복 제출
    h.wait_for(run, "end", 30)


def test_all_answer_types_reach_reanalysis_as_stored_values(choice):
    c = h.browser()
    rid, run = _start(c)
    h.wait_for(run, "questions")
    answers = {"Q1": "아니요", "Q2": "기타: 6주 전", "Q3": "고혈압, 당뇨, 2형", "Q4": "11.2 g/dL", "Q5": "모름"}
    assert c.post(f"/api/runs/{rid}/answers", json={"answers": answers}).status_code == 200
    end = h.wait_for(run, "end", 30)
    assert end["state"] == "completed"
    second = choice.profiler_inputs[-1]  # 답변을 반영해 다시 정리할 때 받은 원문
    for v in ("아니요", "기타: 6주 전", "고혈압, 당뇨, 2형", "11.2 g/dL"):
        assert v in second
    events = h.sse_events(c.get(f"/api/runs/{rid}/events").text)
    ans = [e for e in events if e["type"] == "answers"]
    assert ans and all("answers" not in e for e in ans)  # 답변 내용은 이벤트로 다시 보내지 않음


def test_unknown_and_empty_accepted_for_every_type(choice):
    c = h.browser()
    rid, run = _start(c)
    h.wait_for(run, "questions")
    r = c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "모름", "Q2": "", "Q3": "모름", "Q4": "", "Q5": ""}})
    assert r.status_code == 200
    assert h.wait_for(run, "end", 30)["state"] == "completed"


def test_answer_problem_rules():
    p = web.answer_problem
    assert not p({"answer_type": "multi_choice", "options": OPTS_MULTI}, "당뇨, 2형")  # 선택지 안의 쉼표
    assert not p({"answer_type": "multi_choice", "options": OPTS_MULTI + ["기타"]}, "고혈압, 기타: 천식")
    assert p({"answer_type": "single_choice", "options": OPTS_SINGLE[:3]}, "기타: 6주")  # 기타 선택지가 없으면 거절
    assert not p({"answer_type": "single_choice", "options": ["하나"]}, "아무 말")  # 선택지 1개 이하는 자유 입력으로 받음
    assert not p({"answer_type": "number", "options": []}, "abc") and not p(None, "anything")
    assert not p({"answer_type": "yes_no", "options": []}, " 모르겠어요 ")


# ---------------------------------------------------------------- 다시 보기 비활성화, SSE 개인정보


def test_replay_endpoints_disabled(choice):
    c = h.browser()
    assert c.get("/api/replays").status_code == 404
    assert c.post("/api/replays/anything").status_code in (404, 405)


def test_sse_omits_patient_text_profile_assessments_and_errors(choice, monkeypatch):
    c = h.browser()
    rid, run = _start(c)
    h.wait_for(run, "questions")
    c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "예"}})
    h.wait_for(run, "end", 30)
    events = h.sse_events(c.get(f"/api/runs/{rid}/events").text)
    blob = json.dumps(events, ensure_ascii=False)
    assert h.SYN_TEXT not in blob and "patient_text" not in blob
    traces = [e["record"] for e in events if e["type"] == "trace"]
    assert not any(k in t for t in traces for k in ("profile", "screening", "assessments", "evaluation_error"))
    md = [t for t in traces if (t.get("agent"), t.get("event")) == ("matcher", "done")]
    assert md and all("eligibility" in t and "round" in t for t in md)  # 팀원 trace 요약 필드는 유지
    result = next(e for e in events if e["type"] == "result")
    assert "match_views" not in result


def test_saving_still_off_by_default(choice, tmp_path):
    c = h.browser()
    rid, run = _start(c)
    h.wait_for(run, "questions")
    c.post(f"/api/runs/{rid}/answers", json={"answers": {"Q1": "예"}})
    h.wait_for(run, "end", 30)
    assert not (tmp_path / "outputs").exists()


# ---------------------------------------------------------------- 번역 파일


def test_translations_endpoint_is_public_trial_text_only(choice):
    ko = h.browser().get("/api/ko").json()
    assert set(ko) <= set(web._load_trials()) and len(ko) == 26
    for tid, v in ko.items():
        assert set(v) <= {"title_en", "title", "conditions", "rules", "legacy"}
        trial = web._load_trials()[tid]
        assert v["title_en"] == trial["title"]  # 번역 당시 원문 = 현재 원문 (같을 때만 화면이 번역을 씀)
    blob = json.dumps(ko, ensure_ascii=False)
    assert "<script" not in blob.lower() and "<img" not in blob.lower()


# ---------------------------------------------------------------- 규칙 엔진 의미론 (한국어 문구만 바뀌고 판정은 그대로)


from test_offline import _profile as _base_profile, _rule as _base_rule  # noqa: E402  (기존 오프라인 테스트 도우미)


def _profile(**kw):
    return _base_profile(**kw)


def _rule(field, op, value, unit=None, kind="inclusion"):
    return _base_rule(rule_id="I1", kind=kind, text="t", category="lab" if field.startswith("lab:") else "demographic",
                      field=field, operator=op, value=value, unit=unit)


def test_unit_conversion_reasoning_uses_converted_value_and_korean():
    p = _profile(labs=[LabValue(name="platelets", value=150000, unit="/µL", date="2026-09-10", source_quote="PLT 150,000/µL")])
    a = rule_engine.evaluate(_rule("lab:platelets", ">=", "100", "x10^9/L"), p)
    assert a.holds == "yes" and "= 150 x10^9/L" in a.reasoning and "150 >= 100 → 조건 성립" in a.reasoning
    p2 = _profile(labs=[LabValue(name="platelets", value=85000, unit="/µL", date="2026-09-10", source_quote="PLT 85,000/µL")])
    assert "→ 조건 불성립" in rule_engine.evaluate(_rule("lab:platelets", ">=", "100", "x10^9/L"), p2).reasoning


def test_rule_engine_semantics_unchanged():
    # 정보 부족·단위 불명·같은 날 다른 값은 판정하지 않음 (부적격이 아니라 LLM·질문으로)
    assert rule_engine.evaluate(_rule("lab:hemoglobin", ">=", "9", "g/dL"), _profile()) is None
    hb_gl = _profile(labs=[LabValue(name="hemoglobin", value=88, unit="g/L", date="2026-09-10", source_quote="Hb 88 g/L")])
    assert rule_engine.evaluate(_rule("lab:hemoglobin", ">=", "9", "g/dL"), hb_gl) is None
    same_day = _profile(labs=[LabValue(name="platelets", value=185, unit="x10^9/L", date="2026-09-10", source_quote="a"),
                              LabValue(name="platelets", value=85, unit="x10^9/L", date="2026-09-10", source_quote="b")])
    assert rule_engine.evaluate(_rule("lab:platelets", ">=", "100", "x10^9/L"), same_day) is None
    assert rule_engine.evaluate(_rule("ecog", "<=", "1"), _profile(ecog=None)) is None
    sex = rule_engine.evaluate(_rule("sex", "==", "male"), _profile(sex="female"))
    assert sex.holds == "no" and "여성" in sex.evidence and "조건 불성립" in sex.reasoning
    assert rule_engine.evaluate(_rule("sex", "==", "male"), _profile(sex="unknown")) is None
    age = rule_engine.evaluate(_rule("age", ">=", "18", "years"), _profile(age=60))
    assert age.holds == "yes" and "조건 성립" in age.reasoning


def test_question_normalization_from_team_commit_kept():
    from ctrec.agents.question_generator import _normalize
    q = _normalize(ClarifyingQuestion(question_id="Q", question="q", answer_type="single_choice", options=["모름", "A"],
                                      target=[], purpose="eligibility", why="w"))
    assert q.answer_type == "free_text" and q.options == []  # 선택지 부족 -> 자유 입력 (서버도 형식 검사 안 함)


def test_no_new_sdk_paths_in_merged_code():
    src = Path(config.ROOT / "src" / "ctrec")
    hits = [str(f.relative_to(src)) for f in src.rglob("*.py") if "anthropic.Anthropic(" in f.read_text(encoding="utf-8")
            or "AsyncAnthropic" in f.read_text(encoding="utf-8")]
    assert sorted(hits) == ["llm.py", "llm_pilot.py", "web\\budget.py" if "\\" in str(src / "x") else "web/budget.py"]
