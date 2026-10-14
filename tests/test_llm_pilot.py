"""실제 LLM 소규모 평가 준비 (Phase 5-0): 실행 차단·승인·상한·재시도·취소·같은 상태 비교·비식별화. API 호출 없음.

가짜 클라이언트(네트워크 없음)로 계측 대리 객체의 상한 동작을 확인합니다.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prepare_llm_pilot as plp  # noqa: E402
import prepare_paid_pilot as ppp  # noqa: E402
from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, llm, llm_pilot as lp, question_eval as qe, store_patch  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.schemas import MatcherOutput  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}


# ---------------------------------------------------------------- 가짜 클라이언트

def _usage(i=100, o=50):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0)


class RateLimitError(Exception):  # 이름으로 일시 오류 판단 (SDK 예외와 같은 이름)
    status_code = 429


class BadRequest(Exception):
    status_code = 400


class FakeInner:
    def __init__(self, fail=None, text='{"assessments": []}', usage=None):
        self.calls, self.kwargs, self._fail, self._text, self._usage = 0, [], fail, text, usage or _usage()
        self._lock = threading.Lock()
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        with self._lock:
            self.calls += 1
            self.kwargs.append(kw)
        if self._fail:
            raise self._fail()
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)], stop_reason="end_turn",
                               usage=self._usage)


def _limits(**kw):
    base = dict(max_cost_usd=100.0, max_calls=100, max_input_tokens=10 ** 9, max_output_tokens=10 ** 9,
                max_retries_per_call=2, max_total_retries=50, max_consecutive_failures=50, max_tokens_per_call=1000)
    base.update(kw)
    return lp.PilotLimits(**base)


def _call(proxy, system="sys", n=1):
    for _ in range(n):
        proxy.messages.create(model="m", max_tokens=1000, system=[{"type": "text", "text": system}],
                              messages=[{"role": "user", "content": "x"}])


def _meter(**kw):
    return lp.PilotMeter(_limits(**kw), lp.ASSUMED_PRICE)


# ---------------------------------------------------------------- 실행 차단·승인

@pytest.fixture
def no_sdk_client(monkeypatch):
    """anthropic 클라이언트가 만들어지면 기록 (만들어지면 안 됨)."""
    import anthropic
    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: made.append(1) or pytest.fail("API 클라이언트 생성"))
    monkeypatch.setattr(llm, "_client", None)
    return made


def _plan(**kw):
    p = {"experiment": "A_pipeline", "case_ids": ["X"], "model": config.MODEL, "rounds": 1, "question_budget": 3,
         "limits": lp.asdict(_limits(max_cost_usd=5.0, max_calls=40)), "save_patient_data": False,
         "redact_outputs": True}
    p.update(kw)
    return p


def _approval(plan, **kw):
    h = lp.plan_hash(plan)
    a = dict(approved_by="user 2026-10-13", experiment=plan["experiment"], plan_hash=h,
             price=lp.PriceTable(model=config.MODEL, input=3, output=15, cache_write=3.75, cache_read=0.3,
                                 confirmed=True, source="test"),
             limits=_limits(max_cost_usd=5.0, max_calls=40), confirmation=f"RUN {plan['experiment']} {h[:12]}")
    a.update(kw)
    return lp.PilotApproval(**a)


def test_paid_execution_switch_is_off_and_not_env_controlled():
    assert lp.PAID_EXECUTION_ENABLED is False
    src = (ROOT / "src/ctrec/llm_pilot.py").read_text(encoding="utf-8")
    assert re.search(r"^PAID_EXECUTION_ENABLED = False$", src, re.M)
    assert "PAID_EXECUTION_ENABLED =" not in src.replace("PAID_EXECUTION_ENABLED = False", "")  # 다른 곳에서 바꾸지 않음


def test_fully_approved_plan_still_refused_and_no_client(monkeypatch, no_sdk_client):
    monkeypatch.setenv("CTREC_ALLOW_PAID_EVAL", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    plan = _plan()
    approval = _approval(plan)
    assert lp.approval_problems(plan, approval) == []  # 모든 조건을 갖춰도
    real_client = llm.client
    with pytest.raises(lp.PaidRunNotApproved, match="Phase 5-0"):
        lp.run_pilot(plan, approval, make_client=lambda: pytest.fail("클라이언트 생성 시도"))
    assert no_sdk_client == [] and llm.client is real_client


@pytest.mark.parametrize("change, expect", [
    (lambda p, a: (p, None), "명시적 사용자 승인 없음"),
    (lambda p, a: (p, lp.PilotApproval(**{**a.__dict__, "approved_by": " "})), "승인자 기록 없음"),
    (lambda p, a: ({**p, "case_ids": ["X", "Y"]}, a), "계획 해시 불일치"),
    (lambda p, a: (p, lp.PilotApproval(**{**a.__dict__, "confirmation": "RUN"})), "최종 확인 문구"),
    (lambda p, a: (p, lp.PilotApproval(**{**a.__dict__, "price": lp.ASSUMED_PRICE})), "모델 단가 미확인"),
    (lambda p, a: ({**p, "model": "other-model"}, lp.PilotApproval(**{**a.__dict__, "plan_hash": lp.plan_hash(
        {**p, "model": "other-model"})})), "실제 사용 모델"),
    (lambda p, a: (p, lp.PilotApproval(**{**a.__dict__, "limits": _limits(max_cost_usd=50.0, max_calls=40)})),
     "max_cost_usd"),
    (lambda p, a: ({**p, "save_patient_data": True}, a), "환자 데이터 저장"),
    (lambda p, a: ({**p, "redact_outputs": False}, a), "비식별화"),
])
def test_each_missing_condition_blocks(monkeypatch, change, expect):
    monkeypatch.setenv("CTREC_ALLOW_PAID_EVAL", "1")
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    plan = _plan()
    p, a = change(plan, _approval(plan))
    problems = lp.approval_problems(p, a)
    assert any(expect in x for x in problems), problems
    with pytest.raises(lp.PaidRunNotApproved):
        lp.authorize(p, a)


def test_env_flag_and_fallback_conditions(monkeypatch):
    plan = _plan()
    monkeypatch.delenv("CTREC_ALLOW_PAID_EVAL", raising=False)
    monkeypatch.setattr(config, "USE_FALLBACKS", True)
    problems = lp.approval_problems(plan, _approval(plan))
    assert any("CTREC_ALLOW_PAID_EVAL" in x for x in problems) and any("FALLBACKS" in x for x in problems)


def test_env_vars_and_flags_alone_never_reach_the_api(monkeypatch, no_sdk_client, capsys):
    """환경 변수·플래그를 모두 켜도 평가·준비 스크립트는 실제 클라이언트를 만들지 않음."""
    import eval_question_plans
    monkeypatch.setenv("CTREC_ALLOW_PAID_EVAL", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("CTREC_FALLBACKS", "0")
    with pytest.raises(qe.PaidRunNotApproved):
        eval_question_plans.main(["--approve-paid-run", "--max-cost-usd", "1000"])
    out = plp.build("representative")
    assert out["paid_execution_enabled"] is False and out["real_llm_blocked_offline"] is True
    assert all(p["gate"].startswith("closed") for p in out["plans"].values())
    assert ppp.plan()["paid_gate"].startswith("closed")
    assert no_sdk_client == []
    assert "sk-test-not-a-real-key" not in json.dumps(out) + capsys.readouterr().out  # 키를 출력하지 않음


# ---------------------------------------------------------------- 상한·재시도·취소

def test_call_cap_blocks_before_request():
    inner, m = FakeInner(), _meter(max_calls=2)
    with lp.metered(m, inner):
        _call(llm.client(), n=2)
        with pytest.raises(lp.PilotStop, match="call_cap"):
            _call(llm.client())
    assert inner.calls == 2 and m.report()["stopped"] == "call_cap"


def test_cost_cap_reserves_worst_case_before_request():
    inner = FakeInner()
    price = lp.PriceTable(model="m", input=3, output=15, cache_write=3.75, cache_read=0.3)
    m = lp.PilotMeter(_limits(max_cost_usd=0.01, max_tokens_per_call=1000), price)  # 1회 최악값(출력 1000토큰 $0.015) > 상한
    with lp.metered(m, inner), pytest.raises(lp.PilotStop, match="cost_cap"):
        _call(llm.client())
    assert inner.calls == 0


@pytest.mark.parametrize("kw, code", [({"max_input_tokens": 10}, "input_token_cap"),
                                      ({"max_output_tokens": 500}, "output_token_cap")])
def test_token_budget_caps(kw, code):
    inner, m = FakeInner(), _meter(**kw)
    with lp.metered(m, inner), pytest.raises(lp.PilotStop, match=code):
        _call(llm.client())
    assert inner.calls == 0


def test_actual_usage_over_cap_stops_next_call():
    inner = FakeInner(usage=_usage(i=5000, o=10))  # 추정보다 큰 실제 입력
    m = _meter(max_input_tokens=6000, chars_per_token=50.0)
    with lp.metered(m, inner):
        _call(llm.client())
        _call(llm.client())
        with pytest.raises(lp.PilotStop, match="input_token_cap"):
            _call(llm.client())
    assert inner.calls == 2


def test_retry_cap_on_transient_errors_and_no_retry_on_bad_request():
    sleeps = []
    inner, m = FakeInner(fail=RateLimitError), _meter(max_retries_per_call=2)
    with lp.metered(m, inner) as proxy:
        proxy._sleep = sleeps.append
        with pytest.raises(RateLimitError):
            _call(llm.client())
    assert inner.calls == 3 and m.retries == 2 and m.failures["RateLimitError"] == 3 and len(sleeps) == 2
    inner2, m2 = FakeInner(fail=BadRequest), _meter()
    with lp.metered(m2, inner2), pytest.raises(BadRequest):
        _call(llm.client())
    assert inner2.calls == 1 and m2.retries == 0


def test_total_retry_cap_and_consecutive_failures_stop_run():
    inner, m = FakeInner(fail=RateLimitError), _meter(max_retries_per_call=5, max_total_retries=1)
    with lp.metered(m, inner) as proxy:
        proxy._sleep = lambda s: None
        with pytest.raises(lp.PilotStop, match="retry_cap"):
            _call(llm.client())
    assert inner.calls == 2
    inner2, m2 = FakeInner(fail=BadRequest), _meter(max_consecutive_failures=2)
    with lp.metered(m2, inner2):
        for _ in range(2):
            with pytest.raises(BadRequest):
                _call(llm.client())
        with pytest.raises(lp.PilotStop, match="consecutive_failures"):
            _call(llm.client())
    assert inner2.calls == 2


def test_cancel_and_deadline_stop_before_request():
    inner = FakeInner()
    m = lp.PilotMeter(_limits(), lp.ASSUMED_PRICE, cancel=lambda: True)
    with lp.metered(m, inner), pytest.raises(lp.PilotStop, match="cancelled"):
        _call(llm.client())
    t = [0.0]
    m2 = lp.PilotMeter(_limits(run_deadline_s=10), lp.ASSUMED_PRICE, clock=lambda: t[0])
    with lp.metered(m2, inner):
        _call(llm.client())
        t[0] = 11.0
        with pytest.raises(lp.PilotStop, match="timeout"):
            _call(llm.client())
    assert inner.calls == 1
    assert issubclass(lp.PilotStop, llm.RunCancelled)  # 기존 취소 처리(run_method 상태 기록)를 그대로 탐


def test_per_request_timeout_and_max_tokens_passed():
    inner, m = FakeInner(), _meter(call_timeout_s=42.0, max_tokens_per_call=800)
    with lp.metered(m, inner):
        _call(llm.client())
    assert inner.kwargs[0]["timeout"] == 42.0 and inner.kwargs[0]["max_tokens"] == 800


def test_concurrent_calls_never_exceed_cap():
    inner, m = FakeInner(), _meter(max_calls=5)
    errors = []

    def work():
        try:
            _call(lp_proxy)
        except lp.PilotStop:
            errors.append(1)
    with lp.metered(m, inner) as lp_proxy:
        ts = [threading.Thread(target=work) for _ in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    assert inner.calls == 5 and len(errors) == 7


def test_structured_call_through_meter_counts_stage(monkeypatch):
    """실제 llm.structured -> create -> client() 경로가 계측 대리 객체를 지나고 단계가 구분됨 (가짜 응답)."""
    monkeypatch.setattr(config, "USE_FALLBACKS", False)
    inner, m = FakeInner(text='{"assessments": []}'), _meter()
    with lp.metered(m, inner):
        out = llm.structured(MatcherOutput, system=matcher.SYSTEM, user="u")
    assert out.assessments == [] and m.report()["by_stage"]["matcher"]["calls"] == 1


def test_metered_restores_client_and_import_has_no_side_effects():
    real = llm.client
    with lp.metered(_meter(), FakeInner()):
        assert llm.client is not real
    assert llm.client is real


def test_report_has_counts_only():
    inner, m = FakeInner(fail=BadRequest), _meter()
    with lp.metered(m, inner), pytest.raises(BadRequest):
        _call(llm.client(), system="환자 원문이 들어간 시스템")
    blob = json.dumps(m.report(), ensure_ascii=False)
    assert "환자 원문" not in blob and m.report()["failures"] == {"BadRequest": 1}


# ---------------------------------------------------------------- 실험 정의·호출 경로

def test_call_path_inventory_covers_every_llm_call_site():
    sites = set()
    for p in (ROOT / "src/ctrec").rglob("*.py"):
        if p.name in ("llm.py", "llm_pilot.py", "question_eval.py"):
            continue
        if re.search(r"\bllm\.(structured|plain|create)\(", p.read_text(encoding="utf-8")):
            sites.add(p.relative_to(ROOT / "src/ctrec").as_posix())
    listed = " ".join(c["site"] for c in lp.CALL_PATHS)
    assert sites and all(s in listed for s in sites), sites


def test_experiments_separate_purposes_and_llm_stages():
    a, b = lp.EXPERIMENTS["A_pipeline"], lp.EXPERIMENTS["B_question_plan"]
    assert "question_generator" not in a["llm_stages"] and "question_generator" in b["llm_stages"]
    assert "recommender" not in b["llm_stages"] and b["rounds"] == 0  # B는 답변을 반영하지 않음
    for c in lp.CALL_PATHS:
        if c["stage"] in ("simulated_patient", "explainer", "screener", "orchestrator"):
            assert c["experiments"] == []  # 비용에서 제외되는 단계


@pytest.fixture(scope="module")
def rep():
    return plp.build("representative")


def test_estimates_by_experiment_and_scenario(rep):
    est = rep["estimates"]
    for exp in lp.EXPERIMENTS:
        assert est["low"][exp]["usd"] <= est["base"][exp]["usd"] <= est["worst"][exp]["usd"]
        lim = rep["plans"][exp]["limits"]
        assert lim["max_calls"] >= est["worst"][exp]["calls"] and lim["max_cost_usd"] >= est["base"][exp]["usd"]
    assert rep["price"]["confirmed"] is False and set(rep["unknown"].values()) == {lp.UNKNOWN}
    stages_a = set(rep["measured_offline"]["A_pipeline"]["calls"])
    assert stages_a == {"patient_profiler", "matcher", "recommender"}  # 질문 생성·답변 LLM 없음
    assert set(rep["measured_offline"]["B_question_plan"]["calls"]) == {"patient_profiler", "matcher",
                                                                        "question_generator"}


def test_representative_cases_synthetic_and_balanced(rep):
    cases = rep["cases"]
    assert len(cases) == 5 and rep["target_kinds"] == {"inclusion": 3, "exclusion": 2}
    assert {c["case_type"] for c in cases} == {"rescue", "shared", "biomarker", "cohort", "confirmed"}
    all16 = {c["case_id"] for c in ppp.select(PARSED)}
    assert {c["case_id"] for c in cases} <= all16
    for c in cases:
        raw = json.loads(plp.PATHS[c["set"]].read_text(encoding="utf-8"))
        r = next(x for x in raw if x["patient_id"] == c["case_id"])
        assert r.get("synthetic") is True and r.get("clinical_validity", "unverified_synthetic") == "unverified_synthetic"


def test_all16_synthetic_balanced():
    picked = ppp.select(PARSED)
    kinds = [k for c in picked for k in c["target_kinds"]]
    assert len(picked) == 16 and kinds.count("inclusion") == kinds.count("exclusion") == 8
    for c in picked:
        raw = json.loads(plp.PATHS[c["set"]].read_text(encoding="utf-8"))
        assert next(x for x in raw if x["patient_id"] == c["case_id"]).get("synthetic") is True


# ---------------------------------------------------------------- 실험 B: 같은 상태

def _pair(cid):
    pool = next(c["set"] for c in ppp.select(PARSED) if c["case_id"] == cid)
    return qe.load_cases(PARSED, plp.PATHS[pool], only=[cid])[0]


@pytest.mark.parametrize("cid", [c for c, _ in plp.REPRESENTATIVE])
def test_both_plans_from_same_matcher_state(cid):
    case, gt = _pair(cid)

    def mutating_generator(profile, matches, qa_log, n, acquisition_plan=None):
        out = qe.baseline_generator(profile, matches, qa_log, n, acquisition_plan)
        profile.age = 1  # 생성기가 받은 사본을 바꿔도 비교 상태는 그대로
        for m in matches:
            m.eligibility = "INELIGIBLE"
        return out
    with qe.OfflineEnvironment(3):
        r = lp.compare_plans(case, gt, TRIALS, 3, llm_generate=mutating_generator)
    assert r["status"] == "ok" and r["same_state"] is True
    assert set(r["plans"]) == {"decision_loop", "llm_generator"}
    for p in r["plans"].values():
        assert p["metrics"]["within_budget"] and p["metrics"]["n_questions"] <= 3


def test_state_fingerprint_detects_change():
    case, gt = _pair(plp.REPRESENTATIVE[0][0])
    from ctrec.pipeline import PatientCase, Session
    with qe.OfflineEnvironment(3):
        s = Session(case=PatientCase(patient_id=case.patient_id, raw_text=case.raw_text,
                                     candidate_trials=list(case.candidate_trials)),
                    trials={t: TRIALS[t] for t in gt.candidate_trials}, answerer=lp._NoAnswer(),
                    tracer=qe._NullTracer())
        s.profile_patient()
        s.match()
        fp = lp.state_fingerprint(s)
        s.fact_store.add(case.patient_id, "biomarker", "EGFR negative", source_type="self_report")
        assert lp.state_fingerprint(s) != fp


def test_generator_inputs_have_no_hidden_ground_truth():
    case, gt = _pair("S-NCT07369063-I9-biomarker")
    seen = []
    with qe.OfflineEnvironment(3):
        lp.compare_plans(case, gt, TRIALS, 3, llm_generate=qe.baseline_generator,
                         spy=lambda *a: seen.append(json.dumps([x.model_dump() if hasattr(x, "model_dump") else
                                                                [m.model_dump() for m in x] if isinstance(x, list) else x
                                                                for x in a], ensure_ascii=False, default=str)))
    hidden = [t for _, t in gt._hidden_texts]
    assert seen and hidden and not any(h in seen[0] for h in hidden)


# ---------------------------------------------------------------- 실험 A 지표·비식별화

def test_experiment_a_metrics_offline_redacted():
    pairs = [_pair(c) for c, _ in plp.REPRESENTATIVE[:2]]
    with qe.OfflineEnvironment(3):
        rows = [lp.run_experiment_a(c, g, TRIALS) for c, g in pairs]
    for r in rows:
        assert r["status"] == "ok"
        f = r["facts"]
        assert f["answer_facts_not_self_report"] == 0 and f["promoted_to_document_verified"] == 0
        assert f["unlocated_not_unknown"] == 0
        assert r["matcher"]["not_evaluated"] == 0
    lp.assert_redacted(rows, lp.forbidden_texts(pairs))


def test_egfr_addon_kidney_fact_not_linked_to_gene():
    case, gt = qe.load_cases(PARSED, plp.PATHS[plp.EGFR_ADDON[0]], only=[plp.EGFR_ADDON[1]])[0]
    with qe.OfflineEnvironment(3):
        r = lp.run_experiment_a(case, gt, TRIALS)
    assert r["egfr"].get("kidney_egfr_facts", 0) >= 1 and r["egfr"].get("kidney_egfr_linked_to_EGFR", 0) == 0


def test_redaction_check_catches_raw_text_and_keys(monkeypatch):
    pairs = [_pair(plp.REPRESENTATIVE[0][0])]
    line = next(ln.strip() for ln in pairs[0][0].raw_text.splitlines() if len(ln.strip()) >= 12)
    with pytest.raises(ValueError):
        lp.assert_redacted({"x": line}, lp.forbidden_texts(pairs))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret-value")
    with pytest.raises(ValueError) as e:
        lp.assert_redacted({"k": "sk-test-secret-value"}, lp.forbidden_texts(pairs))
    assert "sk-test" not in str(e.value)  # 오류 메시지에도 내용은 넣지 않음


def test_dry_run_output_redacted_and_no_patient_saving(rep):
    pairs = plp.load_pairs(PARSED, "representative")[1]
    lp.assert_redacted(rep, lp.forbidden_texts(pairs))
    assert all(p["save_patient_data"] is False and p["redact_outputs"] for p in rep["plans"].values())
    assert not (ROOT / "outputs").exists()


def test_frozen_store_intact():
    assert store_patch.verify_frozen() == []
