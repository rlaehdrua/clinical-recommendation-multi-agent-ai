"""실제 LLM 파일럿 준비 (Phase 7): 요청·응답 검사 공용화와 파일럿 계측 강화, 구조화 출력 호환, 합성 사례 구성, 실행 전 점검,
웹과 파일럿 승인 구조의 분리. 네트워크·유료 호출 없음 (Fake SDK, 가짜 HTTP)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT / "scripts"))

import build_phase7_pilot_cases as build7  # noqa: E402
import pilot_preflight as pre  # noqa: E402
import web_harness as h  # noqa: E402
from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, llm, llm_pilot as lp, llm_pilot_cases as pc, llm_pilot_exec as lpx  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.llm_pilot_fake import FakeSDKClient  # noqa: E402
from ctrec.schemas import MatcherOutput  # noqa: E402
from ctrec.web import app as web, budget  # noqa: E402

PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: pytest.fail("실제 API 클라이언트 생성"))
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    monkeypatch.setenv("CTREC_FALLBACKS", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CTREC_ALLOW_PAID_EVAL", raising=False)


def _usage(**kw):
    base = dict(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                service_tier="standard")
    return SimpleNamespace(**{**base, **kw})


# ---------------------------------------------------------------- 1. 요청·응답 검사 (파일럿·웹 공용)


@pytest.mark.parametrize("usage, code", [
    ({"service_tier": None}, "service_tier_mismatch"),
    ({"service_tier": "priority"}, "service_tier_mismatch"),
    ({"cache_creation": SimpleNamespace(ephemeral_5m_input_tokens=0, ephemeral_1h_input_tokens=3)}, "cache_ttl_mismatch"),
    ({"inference_geo": "us"}, "inference_geo_mismatch"),
    ({"iterations": [SimpleNamespace(type="fallback_message")]}, "fallback_detected"),
    ({}, None),
    ({"inference_geo": "global", "cache_creation": SimpleNamespace(ephemeral_5m_input_tokens=7, ephemeral_1h_input_tokens=0)},
     None),
])
def test_usage_problem(usage, code):
    assert lp.usage_problem(_usage(**usage)) == code
    resp = SimpleNamespace(usage=_usage(**usage), model=config.MODEL)
    assert lp.response_problem(resp, config.MODEL) == code


def test_response_problem_usage_and_model():
    assert lp.response_problem(SimpleNamespace(usage=None, model=config.MODEL), config.MODEL) == "usage_missing"
    assert lp.response_problem(SimpleNamespace(usage=_usage(output_tokens=None), model=config.MODEL),
                               config.MODEL) == "usage_missing"
    assert lp.response_problem(SimpleNamespace(usage=_usage(), model="claude-other"), config.MODEL) == "model_mismatch"


@pytest.mark.parametrize("extra, code", [({"betas": ["x"]}, "fallback_requested"), ({"fallbacks": "default"}, "fallback_requested"),
                                         ({"temperature": 0}, "request_option_unsupported"),
                                         ({"service_tier": "auto"}, "request_option_unsupported"),
                                         ({"tools": [{"type": "web_search_20260209", "name": "w"}]}, "server_tool_requested"),
                                         ({"tools": [{"name": "t", "input_schema": {"type": "object"}}]}, None)])
def test_request_problem(extra, code):
    kw = {"model": config.MODEL, "max_tokens": 10, "system": "s", "messages": [], **extra}
    assert lp.request_problem(kw) == code


def test_web_budget_uses_the_same_checks():
    assert budget.request_tokens_bound is lp.request_tokens_bound and budget.ALLOWED_OPTIONS is lp.ALLOWED_OPTIONS
    assert budget.response_problem is lp.response_problem and budget.request_problem is lp.request_problem


# ---------------------------------------------------------------- 2. 파일럿 계측 강화


def _meter(**kw):
    lim = lp.PilotLimits(**{**dict(max_cost_usd=100.0, max_calls=100, max_input_tokens=10**9, max_output_tokens=10**9), **kw})
    return lp.PilotMeter(lim, lp.documented_price())


def _call():
    return llm.structured(MatcherOutput, system=matcher.SYSTEM, user="<rules_to_assess>[]</rules_to_assess>")


@pytest.mark.parametrize("fault, code", [("priority_tier", "service_tier_mismatch"), ("other_model", "model_mismatch"),
                                         ("fallback_iteration", "fallback_detected")])
def test_pilot_stops_on_unpriced_response(fault, code):
    fake, m = FakeSDKClient(faults=lambda i, s: fault), _meter()
    with lp.metered(m, fake), pytest.raises(lp.PilotStop, match=code):
        _call()
    assert m.stopped == code and len(fake.calls) == 1


@pytest.mark.parametrize("patch, code", [({"service_tier": None}, "service_tier_mismatch"),
                                         ({"inference_geo": "us"}, "inference_geo_mismatch"),
                                         ({"cache_creation": SimpleNamespace(ephemeral_1h_input_tokens=5)},
                                          "cache_ttl_mismatch")])
def test_pilot_stops_on_new_usage_problems(patch, code):
    fake, m = FakeSDKClient(), _meter()
    real = fake.messages.create

    def create(**kw):
        r = real(**kw)
        for k, v in patch.items():
            setattr(r.usage, k, v)
        return r
    fake.messages.create = create
    with lp.metered(m, fake), pytest.raises(lp.PilotStop, match=code):
        _call()
        _call()
    assert m.stopped == code and len(fake.calls) == 1


def test_pilot_reservation_counts_korean_bytes_and_stops_on_underestimate():
    korean = "한국어 임상시험 선정 기준 " * 200
    kw = {"model": config.MODEL, "max_tokens": 10, "system": [{"type": "text", "text": korean}],
          "messages": [{"role": "user", "content": korean}]}
    assert lp.request_tokens_bound(kw) >= 2 * len(korean.encode("utf-8")) + lp.REQUEST_OVERHEAD_TOKENS
    fake, m = FakeSDKClient(), _meter()
    real = fake.messages.create

    def create(**kw):
        r = real(**kw)
        r.usage.input_tokens = 10**7  # 예약보다 큰 실제 입력
        return r
    fake.messages.create = create
    with lp.metered(m, fake):
        _call()
        with pytest.raises(lp.PilotStop, match="estimate_exceeded"):
            _call()
    assert len(fake.calls) == 1 and m.input_tokens >= 10**7  # 실제값으로 정산하고 다음 호출은 보내지 않음


@pytest.mark.parametrize("extra, code", [({"temperature": 0.2}, "request_option_unsupported"),
                                         ({"tools": [{"type": "web_search_20260209", "name": "w"}]}, "server_tool_requested")])
def test_pilot_refuses_unsupported_requests_before_send(extra, code):
    fake, m = FakeSDKClient(), _meter()
    with lp.metered(m, fake), pytest.raises(lp.PilotStop, match=code):
        llm.current_client().messages.create(model=config.MODEL, max_tokens=10, system="s",
                                             messages=[{"role": "user", "content": "x"}], **extra)
    assert fake.calls == [] and m.calls == 0


def test_llm_create_sends_only_allowed_options_and_no_sampling_params():
    """claude-sonnet-5는 temperature·top_p·top_k를 기본값이 아닌 값으로 보내면 400 (공식 문서). 실제 요청 옵션을 확인."""
    seen = []
    fake, m = FakeSDKClient(on_call=lambda i, kw: seen.append(set(kw))), _meter()
    with lp.metered(m, fake):
        _call()
    assert seen and all(s - {"timeout"} <= lp.ALLOWED_OPTIONS for s in seen)
    assert not any(s & {"temperature", "top_p", "top_k", "thinking", "stream"} for s in seen)


# ---------------------------------------------------------------- 3. 구조화 출력 스키마 호환 (공식 제한)


def test_structured_output_schemas_within_documented_limits():
    sp = pre.schema_problems()
    for name, v in sp.items():
        assert v["union_params"] <= 16 and v["optional_params"] <= 24, (name, v)
        assert v["unsupported"] == [] and not v["recursive"], (name, v)
    assert sp["PatientProfile"]["union_params"] == 14  # 한도(16)에 가까움: 선택 필드를 더하면 이 테스트가 먼저 알림


# ---------------------------------------------------------------- 4. 합성 사례 구성


def test_phase7_case_files_are_generated_and_synthetic():
    assert build7.main(["--check"]) == 0
    data = json.loads(pc.PHASE7_CASES.read_text(encoding="utf-8"))
    assert all(c["synthetic"] is True and c["phase7"]["derived_from"] == "F-SYN-DEMO-A" for c in data)
    src = next(c for c in json.loads(build7.SOURCE_FILE.read_text(encoding="utf-8")) if c["patient_id"] == "F-SYN-DEMO-A")
    for c in data:  # 원본과 정확히 한 줄만 다름 (대조 사례)
        a, b = set(src["text"].splitlines()), set(c["text"].splitlines())
        assert len(b - a) == 1 and len(a - b) <= 1


def test_coverage_has_every_type_and_groups_are_separate():
    cov = pc.coverage()
    assert cov["missing"] == [] and set(cov["by_type"]) == set("ABCDEFGH")
    assert cov["not_in_experiment_a_batch"] == ["A", "B", "E", "H"]  # 첫 배치 밖 유형은 후속 배치 후보로만
    groups = {cid: pc.group_of(cid) for cid, *_ in pc.CASES}
    assert {c for c, g in groups.items() if g == "experiment_a_batch"} == set(pc.EXPERIMENT_A_BATCH)
    assert all(c not in lp.PILOT_CASE_POOL for c, g in groups.items() if g == "phase7_candidate")
    pairs = pc.load_pairs(PARSED)
    assert len(pairs) == len(pc.CASES) and all(gt.synthetic for _, gt in pairs)
    with pytest.raises(ValueError):
        pc.load_pairs(PARSED, ["NOT-A-CASE"])


def test_phase7_candidates_are_refused_by_unchanged_gate():
    ids = ["P7-SYN-DEMO-A-conflict-ecog"]
    plan = {"experiment": "A_pipeline", "case_ids": ids, "model": config.MODEL, "price": {}, "rounds": 1,
            "question_budget": 3, "limits": {}, "save_patient_data": False, "redact_outputs": True}
    problems = lp.approval_problems(plan, lp.PilotApproval("x", "A_pipeline", lp.plan_hash(plan), lp.documented_price(),
                                                           lp.PilotLimits(1, 1, 1, 1), "RUN"))
    assert any("PILOT_CASE_POOL" in p for p in problems)
    assert len(lp.PILOT_CASE_POOL) == 6  # 승인 후보 풀은 늘리지 않음


def test_phase7_cases_run_offline_through_experiment_a_path():
    ids = ["P7-SYN-DEMO-A-conflict-ecog", "P7-SYN-DEMO-A-conflict-plt", "P7-SYN-DEMO-A-units-hb"]
    pairs = pc.load_pairs(PARSED, ids)
    res = lpx.simulate({"experiment": "A_pipeline", "case_ids": ids, "rounds": 1, "question_budget": 3}, pairs, TRIALS)
    assert res["status"] == "succeeded" and res["client"] == "fake_sdk" and res["frozen_store_intact"]
    assert res["summary"]["labelled_trials"] == 3  # 기대 판정이 결과 지표(label_agreement)로 연결됨
    lp.assert_redacted(res, lp.forbidden_texts(pairs))
    exp = pc.phase7_expectations()
    assert {k: v["expected_eligibility"]["DEMO-LUNG-001"] for k, v in exp.items()} == {
        "P7-SYN-DEMO-A-conflict-ecog": "UNCERTAIN", "P7-SYN-DEMO-A-conflict-plt": "UNCERTAIN",
        "P7-SYN-DEMO-A-units-hb": "INELIGIBLE"}


def test_criteria_split_quantitative_and_human_review():
    for area, v in pc.CRITERIA.items():
        assert v["quantitative"] and v["human_review"], area


# ---------------------------------------------------------------- 5. 실행 전 점검


@pytest.mark.parametrize("experiment", ["A_pipeline", "B_question_plan"])
def test_preflight_has_no_failures_and_stays_locked(experiment):
    r, info = pre.check(experiment, pre.FIRST_BATCH)
    st = {i["area"]: i["status"] for i in r.items}
    assert "FAIL" not in st.values(), r.items
    assert st["approval_gate"] == st["paid_switch"] == "LOCKED" and st["price"] == "PENDING_USER"
    assert st["gate_blocks_before_client"] == st["unmetered_sdk_blocked"] == st["fake_rehearsal"] == "PASS"
    assert st["stop_on_failure"] == st["output_redaction"] == st["structured_output_schemas"] == "PASS"
    assert st["api_key"] == "PENDING_RUNTIME"
    assert info["limits"]["max_cost_usd"] <= pre.PHASE5_CAPS[experiment]["max_cost_usd"]


def test_preflight_flags_unsafe_environment(monkeypatch, capsys):
    monkeypatch.setenv("CTREC_FALLBACKS", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-preflight-value-0000")
    r, _ = pre.check("A_pipeline", ["P7-SYN-DEMO-A-conflict-ecog"])
    st = {i["area"]: i["status"] for i in r.items}
    assert st["fallbacks"] == "FAIL" and st["synthetic_cases"] == "FAIL" and st["api_key"] == "PASS"
    monkeypatch.setenv("CTREC_FALLBACKS", "0")
    assert pre.main(["--json"]) == 0
    out = capsys.readouterr().out
    assert "sk-ant-test-preflight-value" not in out and json.loads(out)["ready_to_execute"] is False


# ---------------------------------------------------------------- 6. 웹과 파일럿 승인 구조는 별개


def test_web_runs_do_not_use_the_pilot_approval_gate(monkeypatch, tmp_path):
    """웹 실행은 Phase 5 승인 게이트(PAID_EXECUTION_ENABLED·승인 기록·계획 해시)를 거치지 않는다.
    API 키가 있으면 웹은 그 키로 호출함: 웹의 비용 보호는 비밀번호·record/enforce·Console 한도 (문서 llm_pilot_phase7.md 7절)."""
    sdk = h.install_sdk(monkeypatch, tmp_path, h.SDKAdapter())
    monkeypatch.setenv("CTREC_WEB_METERING", "record")
    assert lp.PAID_EXECUTION_ENABLED is False
    c = h.browser()
    rid = c.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}).json()["run_id"]
    run = web.RUNS[rid]
    q = h.wait_for(run, "questions")
    c.post(f"/api/runs/{rid}/answers", json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}})
    end = h.wait_for(run, "end", 30)
    assert end["state"] == "completed" and len(sdk.requests) > 0  # 승인 없이 SDK 자리로 요청이 감
