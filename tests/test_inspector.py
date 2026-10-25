"""Decision Loop Inspector (Phase 10-A). API 호출 없음: 오프라인 대역(question_eval.OfflineEnvironment)으로 만든 실행 폴더와
손으로 만든 기록만 씀. 고정 기준표(data/parsed)는 읽기만 합니다.

확인: 기록만 옮김(판정 불변) / 민감 정보 비노출(허용 목록) / HTML 이스케이프·스크립트 없음 / 출력 위치 제한.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import render_inspector as ri  # noqa: E402

from ctrec import config, inspector, llm  # noqa: E402
from ctrec import question_eval as qe  # noqa: E402

CASE = ri.DEFAULT_CASE


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    def no_real_client(*a, **k):
        raise AssertionError("실제 API 클라이언트 생성 금지")
    saved = anthropic.Anthropic
    anthropic.Anthropic = no_real_client  # 대역 밖으로 새면 바로 실패
    try:
        d = tmp_path_factory.mktemp("inspector") / CASE
        with redirect_stdout(io.StringIO()):
            ri.run_offline_demo(CASE, d)
    finally:
        anthropic.Anthropic = saved
    return d


def _case():
    from bench_incremental import load_frozen
    parsed = load_frozen()
    return parsed, qe.load_cases(parsed, only=[CASE])[0]


def test_run_dir_matches_cli_contract(demo_dir):
    for name in ("result.json", "trace.jsonl", "run_meta.json", f"facts/{CASE}.json"):
        assert (demo_dir / name).is_file(), name
    meta = json.loads((demo_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["llm_backend"] == "offline_stub" and meta["synthetic"] is True
    assert llm.client is not qe._blocked  # 대역 설치가 끝나면 되돌려짐


def test_decisions_equal_existing_offline_path(demo_dir):
    """Inspector용 실행(기록만 더 남김)이 기존 평가 경로(run_method)와 같은 판정을 냄."""
    from decision_scenarios import load_trials
    parsed, (case, gt) = _case()
    trials = load_trials()
    with qe.OfflineEnvironment(3):
        run = qe.run_method("candidate", case, gt, {t: trials[t] for t in gt.candidate_trials}, 2, 3)
    result = json.loads((demo_dir / "result.json").read_text(encoding="utf-8"))
    assert run.status == "ok"
    assert {t: m["eligibility"] for t, m in result["matches"].items()} == run.final_eligibility
    assert [q["round"] for q in result["qa_log"]] == [q["round_id"] for q in run.questions]


def test_view_sections_come_from_records(demo_dir):
    rec = inspector.load_run(demo_dir)
    v = inspector.build_view(rec)
    result = rec.result
    assert v["schema"] == inspector.SCHEMA and v["run"]["llm_backend"] == "offline_stub"
    assert len(v["facts"]) == len(rec.facts) and all("value" not in f for f in v["facts"])
    assert [t["trial_id"] for t in v["trials"]] == list(result["matches"])
    assert len(v["questions"]) == len(result["qa_log"])
    assert [h["eligibility"] for h in v["history"]] == [h["eligibility"] for h in result["eligibility_history"]]
    assert len(v["decision"]) == len(result["decision_diagnostic"])
    # 이 사례에 기록된 변화: 답변 뒤 I6이 자기 보고로만 확인되어 문서 확인 필요(UNVERIFIED)로 바뀜
    changed = {c["rule_id"]: c for t in v["trials"] for c in t["criteria"] if c["changes"]}
    nv = next(x for x in changed["I6"]["changes"] if x["field"] == "needs_verification")
    assert (nv["before"], nv["after"]) == (False, True) and changed["I6"]["needs_verification"] is True
    # 대역 실행이라 호출량 기록 없음 -> 비어 있다고 표시 (지어내지 않음)
    assert v["usage"] == {} and any("usage" in g for g in v["gaps"])
    assert any("직접 연결 기록은 없음" in g for g in v["gaps"])


def test_strict_html_has_no_patient_text_or_answers(demo_dir):
    rec = inspector.load_run(demo_dir)
    page = inspector.render_html(inspector.build_view(rec))
    _, (case, _gt) = _case()
    sentences = [s.strip() for s in case.raw_text.replace("\n", ". ").split(". ") if len(s.strip()) > 25]
    assert sentences and not [s for s in sentences if s in page]
    answers = [q["answer"] for q in rec.result["qa_log"] if q["answer"] != "모름"]
    assert answers and not [a for a in answers if a in page]
    quotes = [f["source_reference"]["quote"] for f in rec.facts if (f.get("source_reference") or {}).get("quote")]
    assert not [q for q in quotes if len(q) > 12 and q in page]
    assert rec.result["patient_id"] not in page
    assert "offline stub" not in page  # 대역 Matcher의 reasoning
    assert "<script" not in page.lower() and "default-src 'none'" in page


def test_synthetic_details_only_for_synthetic_runs(demo_dir, tmp_path):
    rec = inspector.load_run(demo_dir)
    page = inspector.render_html(inspector.build_view(rec, synthetic_details=True))
    assert rec.result["patient_id"] in page and "합성 데이터 상세 표시" in page
    rec.meta = {"llm_backend": "real"}
    with pytest.raises(inspector.InspectorError):
        inspector.build_view(rec, synthetic_details=True)
    rec.meta = None
    with pytest.raises(inspector.InspectorError):
        inspector.build_view(rec, synthetic_details=True)


def _record(**over) -> inspector.RunRecord:
    secret = "PATIENT-RAW-SECRET 72세 남성 혈소판 85"
    a = {"rule_id": "I1", "kind": "inclusion", "criterion_text": "<script>alert(1)</script> Age >= 18",
         "holds": "unknown", "passes": None, "method": "llm", "evidence": secret, "reasoning": secret,
         "missing_info": secret, "category": "lab", "applicable": True}
    result = {
        "patient_id": "REAL-123", "model": "claude-sonnet-5", "input_source": "unknown",
        "profile": {"other_facts": [{"description": secret, "source_quote": secret}]},
        "matches": {"NCT0": {"trial_id": "NCT0", "eligibility": "NOT_EVALUATED", "match_status": "NOT_EVALUATED",
                             "n_pass": 0, "n_fail": 0, "n_unknown": 1, "assessments": [a],
                             "evaluation_error": f"적격성 판정 실패: ValueError('{secret}')", "summary": secret}},
        "qa_log": [{"round": 1, "question_id": "Q1", "question": secret, "answer": secret, "purpose": "eligibility",
                    "target": ["NCT0:I1"]}],
        "eligibility_history": [{"step": "match(round=0)", "eligibility": {"NCT0": "UNCERTAIN"}}],
        "screening": {"pool_size": 2, "selected": [{"trial_id": "NCT0", "reason": secret}], "excluded": [],
                      "hard_filtered": [{"trial_id": "NCT9", "kind": "age", "reason": secret}]},
        "errors": [{"item": "NCT0", "error": f"RuntimeError('{secret}')"}],
        "usage": {"calls": 7, "input_tokens": 1000, "note": secret},
        "fact_shadow": [{"round": 0, "status": "ok", "added": 3, "session_id": "S-abc"}],
    }
    trace = [{"t": 0.1, "agent": "pipeline", "event": "error", "item": "NCT0", "message": f"KeyError('{secret}')"},
             {"t": 0.2, "agent": "matcher", "event": "done", "trial_id": "NCT0", "round": 0, "message": secret,
              "eligibility": "UNCERTAIN", "assessments": [{**a, "gap": "ask"}]},
             {"t": 0.3, "agent": "question_generator", "event": "questions", "round": 1,
              "questions": [{"question_id": "Q1", "question": secret, "why": secret, "target": ["NCT0:I1"],
                             "purpose": "eligibility", "answer_type": "free_text"}]}]
    facts = [{"field": "lab:platelets", "value": 85, "unit": "10^9/L", "source_type": "self_report",
              "verification_status": "unverified", "conflict_status": "none", "version": 1,
              "source_reference": {"kind": "answer", "ref": "qa:round1:Q1", "quote": secret}}]
    metering = {"calls": 7, "status": "metered", "cost_usd": "UNKNOWN", "api_key": "sk-ant-SECRETKEY",
                "owner_token": "tok-SECRET"}
    rec = inspector.RunRecord(result=result, trace=trace, facts=facts, metering=metering,
                              meta={"llm_backend": "real", "synthetic": False})
    for k, v in over.items():
        setattr(rec, k, v)
    return rec


def test_sensitive_fields_never_rendered():
    rec = _record()
    v = inspector.build_view(rec)
    page = inspector.render_html(v)
    for s in ("PATIENT-RAW-SECRET", "혈소판 85", "REAL-123", "sk-ant-SECRETKEY", "tok-SECRET", "S-abc"):
        assert s not in page and s not in json.dumps(v, ensure_ascii=False), s
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    # 오류는 단계·종류만
    assert v["trials"][0]["error"] == {"stage": "적격성 판정 실패", "type": "ValueError"}
    assert v["errors"] == [{"item": "NCT0", "type": "RuntimeError"}]
    assert v["steps"][0]["error_type"] == "KeyError"
    # 기록된 호출량은 숫자만, 비용은 기록된 코드 그대로 (계산하지 않음)
    assert v["usage"] == {"calls": 7, "input_tokens": 1000}
    assert v["metering"] == {"web": {"status": "metered", "calls": 7, "cost_usd": "UNKNOWN"}, "budget": {}}
    assert v["screening"]["hard_filtered"] == [{"trial_id": "NCT9", "kind": "age"}]
    assert v["questions"][0]["answer_status"] == "답변함" and "question" not in v["questions"][0]
    assert v["questions"][0]["overlapping_plan"] is None  # 진단 기록이 없으면 없다고 표시


def test_missing_records_are_reported_as_gaps():
    rec = _record(trace=None, facts=None, metering=None, meta=None)
    v = inspector.build_view(rec)
    gaps = " ".join(v["gaps"])
    for word in ("run_meta.json", "facts/", "trace.jsonl", "metering.json", "Decision Loop 진단", "의존 그래프"):
        assert word in gaps, word
    assert v["facts"] is None and v["trials"][0]["rounds"] == []
    assert inspector.render_html(v)  # 기록이 적어도 화면은 만듦


def test_load_run_requires_result(tmp_path):
    with pytest.raises(inspector.InspectorError):
        inspector.load_run(tmp_path)


def test_write_html_refuses_protected_paths(tmp_path):
    v = inspector.build_view(_record())
    with pytest.raises(inspector.InspectorError):
        inspector.write_html(v, config.PARSED_DIR / "x.html")
    with pytest.raises(inspector.InspectorError):
        inspector.write_html(v, tmp_path / "x.json")
    assert inspector.write_html(v, tmp_path / "out" / "x.html").is_file()


def test_render_cli_on_existing_run_dir(demo_dir, tmp_path):
    out = tmp_path / "page.html"
    with redirect_stdout(io.StringIO()) as buf:
        ri.main(["render", str(demo_dir), "--out", str(out)])
    assert out.is_file() and "Decision Loop Inspector" in out.read_text(encoding="utf-8")
    assert "기록에 없는 데이터" in buf.getvalue()


# ---------------------------------------------------------------- Phase 10-A-1 회귀 테스트

def test_demo_gap_uses_last_recorded_round(demo_dir):
    """rescue2-3의 I6: 라운드 0은 ask, 답변 뒤 라운드 1·2는 verify. 최종 표시는 마지막 기록(verify)."""
    rec = inspector.load_run(demo_dir)
    rounds = {t["round"]: t for t in rec.trace if t["agent"] == "matcher" and t["event"] == "done"}
    gaps = {r: next(a["gap"] for a in t["assessments"] if a["rule_id"] == "I6") for r, t in rounds.items()}
    assert gaps == {0: "ask", 1: "verify", 2: "verify"}
    v = inspector.build_view(rec)
    i6 = next(c for t in v["trials"] for c in t["criteria"] if c["rule_id"] == "I6")
    assert (i6["gap"], i6["gap_round"]) == ("verify", 2)
    # 모든 기준: 화면 값 == 마지막 라운드 기록
    last = {a["rule_id"]: a.get("gap") for a in rounds[max(rounds)]["assessments"]}
    assert {c["rule_id"]: c["gap"] for t in v["trials"] for c in t["criteria"]} == last
    page = inspector.render_html(v)
    assert '<code>verify</code> <span class="mut">문서 확인</span> <span class="mut">(라운드 2 기록)</span>' in page


def test_gap_multi_round_regression():
    """여러 라운드: 마지막으로 기록된 라운드의 gap. 기록이 없으면 만들지 않음."""
    base = {"kind": "inclusion", "criterion_text": "x", "holds": "unknown", "passes": None, "method": "llm"}

    def done(rnd, items):
        return {"agent": "matcher", "event": "done", "trial_id": "T1", "round": rnd, "eligibility": "UNCERTAIN",
                "assessments": [{**base, **a} for a in items]}
    trace = [done(0, [{"rule_id": "A", "gap": "ask"}, {"rule_id": "B", "gap": "ask"}, {"rule_id": "C"},
                      {"rule_id": "D", "gap": "physician"}]),
             done(1, [{"rule_id": "A", "gap": "verify"}, {"rule_id": "B", "gap": None}, {"rule_id": "C"}]),
             done(2, [{"rule_id": "A", "gap": None}, {"rule_id": "B", "gap": "ask"}])]
    final = [{**base, "rule_id": k} for k in ("A", "B", "C", "D", "E")]
    rec = inspector.RunRecord(result={"patient_id": "P1", "matches": {"T1": {"eligibility": "UNCERTAIN",
                                                                            "assessments": final}}}, trace=trace)
    crit = {c["rule_id"]: (c["gap"], c["gap_round"]) for c in inspector.build_view(rec)["trials"][0]["criteria"]}
    assert crit == {
        "A": (None, 2),         # 마지막 기록(라운드 2)은 해소 경로 없음 -> 이전 값으로 채우지 않음
        "B": ("ask", 2),
        "C": (None, None),      # gap 필드 자체가 기록되지 않음
        "D": ("physician", 0),  # 이 기준은 라운드 0에만 기록됨
        "E": (None, None),      # trace에 없음
    }


def _real_metering(tmp_path, with_budget: bool) -> tuple[dict, dict, dict]:
    """web/app.py와 같은 방식: run.metering = WebRunMeter.summary() + _budget_fields() (enforce면 RunBudget.summary())."""
    from ctrec import web_metering
    from ctrec.web import budget
    meter = web_metering.WebRunMeter("run-insp")
    meter.finish()
    web = meter.summary(llm_expected=False)
    bud: dict = {}
    if with_budget:
        price = budget.Price(config.MODEL, 2.0, 10.0, 2.5, 0.2, "test")
        big = budget.Limits(1000.0, 10_000, 10**9, 10**9)
        cfg = budget.BudgetConfig(run=big, shared=big, period="day", price=price,
                                  ledger_path=str(tmp_path / "ledger.db"), run_max_seconds=3600)
        led = budget.Ledger(cfg.ledger_path, cfg.shared, cfg.period).open(create=True)
        b = budget.RunBudget("run-insp", cfg, led)
        b.reserve(1000, 500)  # 예약만 (정산 전) -> reserved_cost_usd > 0
        bud = b.summary()
    return {**web, **bud}, web, bud


def test_metering_uses_real_schema_fields(tmp_path):
    metering, web, bud = _real_metering(tmp_path, with_budget=True)
    assert bud["reserved_cost_usd"] > 0  # 실제 RunBudget이 낸 값
    rec = _record(metering=metering)
    rec.result["decision_diagnostic"] = [{"round": 0, "status": "ok", "actions": [],
                                          "usage": {"iterations": 4, "tool_calls": 0, "cost_units": 3.0,
                                                    "questions": 3, "llm_calls": 0, "seconds": 0.0032}}]
    v = inspector.build_view(rec)
    b = v["metering"]["budget"]
    for k in ("estimated_cost_usd", "confirmed_cost_usd", "reserved_cost_usd", "unknown_cost_usd", "calls_used",
              "calls_remaining", "budget_mode", "budget_status", "cap_enforced"):
        assert b[k] == bud[k], k
    w = v["metering"]["web"]
    for k in ("calls", "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "status",
              "cost_usd", "usage_confirmed", "cost_cap_enforced"):
        assert w[k] == web[k], k
    assert w["cost_usd"] == "UNKNOWN"  # 기록된 그대로 (0이나 추정값으로 바꾸지 않음)
    # 의미가 다른 값은 섞거나 합산하지 않음: 웹 계측·예산·Decision Loop 단위가 각자 자리에만
    assert "cost_units" not in w and "cost_units" not in b and "calls_used" not in w and "calls" not in b
    assert v["decision"][0]["usage"] == {"iterations": 4, "questions": 3, "tool_calls": 0, "llm_calls": 0,
                                         "cost_units": 3.0, "seconds": 0.0032}
    assert not any("total" in k for k in {**w, **b})
    for k in ("note", "cost_note", "by_stage", "token_usage", "run_id"):  # 자유 문장·중첩·식별자는 읽지 않음
        assert k not in w and k not in b
    page = inspector.render_html(v)
    assert "예약 중(USD)" in page and "추상 비용 단위(USD 아님)" in page


def test_metering_without_budget_is_not_zero_filled(tmp_path):
    metering, _web, _ = _real_metering(tmp_path, with_budget=False)
    v = inspector.build_view(_record(metering=metering))
    assert v["metering"]["budget"] == {}  # 기록에 없는 USD는 0으로 채우지 않음
    assert any("USD 비용 값 없음" in g and "cost_usd=UNKNOWN" in g for g in v["gaps"])


def _write_run(d: Path, pid: str = "P-1", facts: bool = True) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps({"patient_id": pid, "matches": {}}), encoding="utf-8")
    if facts:
        (d / "facts").mkdir(exist_ok=True)
        (d / "facts" / f"{pid}.json").write_text(json.dumps({"facts": [{"field": "age"}]}), encoding="utf-8")
    return d


@pytest.mark.parametrize("pid", ["../outside", "..", "/abs/x", "C:/x", "a/b", "a\\b", "a/../b", "", ".hidden",
                                 "x" * 200, None, 5])
def test_patient_id_path_manipulation_rejected(tmp_path, pid):
    d = tmp_path / "run"
    d.mkdir()
    (d / "result.json").write_text(json.dumps({"patient_id": pid, "matches": {}}), encoding="utf-8")
    (tmp_path / "outside.json").write_text(json.dumps({"facts": [{"field": "LEAK"}]}), encoding="utf-8")
    with pytest.raises(inspector.InspectorError):
        inspector.load_run(d)


def test_normal_run_dir_still_loads(tmp_path):
    rec = inspector.load_run(_write_run(tmp_path / "run", "S001-a.b_c"))
    assert rec.facts == [{"field": "age"}] and rec.trace is None and rec.metering is None


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없음")


@pytest.mark.parametrize("name", ["facts_file", "facts_dir", "result", "metering"])
def test_symlink_outside_run_dir_rejected(tmp_path, name):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "P-1.json").write_text(json.dumps({"facts": [{"field": "LEAK"}]}), encoding="utf-8")
    (secret / "m.json").write_text("{}", encoding="utf-8")
    d = _write_run(tmp_path / "run", facts=False)
    if name == "facts_file":
        (d / "facts").mkdir()
        _symlink(d / "facts" / "P-1.json", secret / "P-1.json")
    elif name == "facts_dir":
        _symlink(d / "facts", secret)
    elif name == "result":
        (d / "result.json").unlink()
        (secret / "result.json").write_text(json.dumps({"patient_id": "P-1"}), encoding="utf-8")
        _symlink(d / "result.json", secret / "result.json")
    else:
        _symlink(d / "metering.json", secret / "m.json")
    if name == "result":  # 필수 입력이 밖을 가리키면 실행 전체를 거부
        with pytest.raises(inspector.InspectorError):
            inspector.load_run(d)
        return
    # 10-C-2: 선택 입력(facts·metering)은 읽지 않고 형식 문제로 남긴 채 표시를 계속 (밖의 내용은 읽지 않음)
    rec = inspector.load_run(d)
    v = inspector.build_view(rec)
    assert rec.facts is None and rec.metering is None and v["anomalies"]
    assert "LEAK" not in inspector.render_html(v) and "LEAK" not in json.dumps(v, ensure_ascii=False)


def test_junction_outside_run_dir_rejected(tmp_path):
    """Windows 디렉터리 정션(관리자 권한 불필요)으로 facts/가 밖을 가리키는 경우."""
    import subprocess
    if sys.platform != "win32":
        pytest.skip("Windows 정션 전용")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "P-1.json").write_text(json.dumps({"facts": [{"field": "LEAK"}]}), encoding="utf-8")
    d = _write_run(tmp_path / "run", facts=False)
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(d / "facts"), str(secret)], capture_output=True)
    if r.returncode != 0:
        pytest.skip("정션을 만들 수 없음")
    # 10-C-2: 밖을 가리키는 facts는 읽지 않고(내용 비노출) 형식 문제로 남긴 채 나머지는 표시 (이전: 실행 전체 거부)
    rec = inspector.load_run(d)
    v = inspector.build_view(rec)
    assert rec.facts is None and v["facts"] is None
    assert any(a.startswith("facts: 실행 폴더 밖을 가리키거나 파일이 아님") for a in v["anomalies"])
    page = inspector.render_html(v)
    assert "LEAK" not in page and "LEAK" not in json.dumps(v, ensure_ascii=False)


def test_link_resolving_outside_rejected(tmp_path, monkeypatch):
    """링크 종류와 무관하게: 입력 파일의 실제 위치(resolve)가 실행 폴더 밖이면 거부 (심볼릭 링크를 못 만드는 환경용)."""
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"facts": [{"field": "LEAK"}]}), encoding="utf-8")
    d = _write_run(tmp_path / "run")
    link = (d / "facts" / "P-1.json").resolve()
    real_resolve = Path.resolve

    def fake_resolve(self, *a, **k):
        return secret if real_resolve(self, *a, **k) == link else real_resolve(self, *a, **k)
    monkeypatch.setattr(Path, "resolve", fake_resolve)
    # 10-C-2: 밖을 가리키는 facts는 읽지 않고(내용 비노출) 형식 문제로 남긴 채 나머지는 표시 (이전: 실행 전체 거부)
    rec = inspector.load_run(d)
    v = inspector.build_view(rec)
    assert rec.facts is None and v["facts"] is None
    assert any(a.startswith("facts: 실행 폴더 밖을 가리키거나 파일이 아님") for a in v["anomalies"])
    page = inspector.render_html(v)
    assert "LEAK" not in page and "LEAK" not in json.dumps(v, ensure_ascii=False)


def test_write_html_never_overwrites(tmp_path):
    v = inspector.build_view(_record())
    out = tmp_path / "page.html"
    inspector.write_html(v, out)
    before = out.read_bytes()
    with pytest.raises(inspector.InspectorError):
        inspector.write_html(v, out)
    assert out.read_bytes() == before


def test_render_cli_refuses_existing_html(demo_dir, tmp_path):
    out = tmp_path / "exists.html"
    out.write_text("KEEP", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        ri.main(["render", str(demo_dir), "--out", str(out)])
    assert e.value.code == 2 and out.read_text(encoding="utf-8") == "KEEP"


def _hashes(d: Path) -> dict:
    import hashlib
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.rglob("*") if p.is_file()}


def test_demo_refuses_existing_run_dir(demo_dir, tmp_path):
    snap = _hashes(demo_dir)
    with pytest.raises(inspector.InspectorError):
        ri.run_offline_demo(CASE, demo_dir)
    with pytest.raises(SystemExit) as e:
        ri.main(["demo", "--out-dir", str(demo_dir)])
    assert e.value.code == 2 and _hashes(demo_dir) == snap
    not_dir = tmp_path / "not_a_dir"
    not_dir.write_text("x", encoding="utf-8")
    with pytest.raises(inspector.InspectorError):
        ri.run_offline_demo(CASE, not_dir)
    assert not_dir.read_text(encoding="utf-8") == "x"


def test_demo_creates_into_new_or_empty_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with redirect_stdout(io.StringIO()):
        ri.main(["demo", "--out-dir", str(empty)])
    assert (empty / "result.json").is_file() and (empty / "inspector.html").is_file()


# ---------------------------------------------------------------- Phase 10-B-1: 개요·타임라인·변화 요약

def test_timeline_rounds_in_order(demo_dir):
    v = inspector.build_view(inspector.load_run(demo_dir))
    tl = v["timeline"]
    assert [x["round"] for x in tl] == [0, 1, 2] and [x["compared_with"] for x in tl] == [None, 0, 1]
    # QA 라운드 r = 라운드 r-1 평가 뒤 생성·답변된 질문 (qa_log 기록 그대로 묶음)
    assert [x["qa"]["count"] for x in tl] == [0, 3, 3]
    assert [(x["qa"]["answered"], x["qa"]["declined"]) for x in tl] == [(0, 0), (1, 2), (0, 3)]
    assert [x["fact_changed_fields"] for x in tl] == [[], ["ecog", "other_fact"], []]
    assert [x["decision"]["stop_reason"] for x in tl] == ["awaiting_user_answers", "awaiting_external_input",
                                                         "awaiting_external_input"]
    o = v["overview"]
    assert (o["run_kind"], o["llm_backend"], o["n_candidates"], o["n_rounds"], o["n_questions"]) == \
        ("합성", "offline_stub", 1, 3, 6)
    page = inspector.render_html(v)
    assert "오프라인 합성 실행이며 실제 AI 판정 결과가 아닙니다." in page
    assert page.index("A. 실행 개요") < page.index("B. Decision Loop 타임라인") < page.index("C. 주요 변화 요약") \
        < page.index("D. 상세 기록") < page.index("D3. 환자 Fact Store")


def test_i6_gap_ask_to_verify_between_adjacent_rounds(demo_dir):
    v = inspector.build_view(inspector.load_run(demo_dir))
    i6 = {c["field"]: c for c in v["changes"]["evidence"] if c["rule_id"] == "I6"}
    assert (i6["gap"]["before"], i6["gap"]["after"], i6["gap"]["from_round"], i6["gap"]["to_round"]) == ("ask", "verify", 0, 1)
    assert (i6["self_reported_holds"]["before"], i6["self_reported_holds"]["after"]) == (None, "yes")
    assert (i6["needs_verification"]["before"], i6["needs_verification"]["after"]) == (False, True)
    assert not [c for c in v["changes"]["evidence"] if c["from_round"] == 1]  # 라운드 1 -> 2는 verify 유지
    assert [x["evidence_changes"] for x in v["timeline"]] == [[], ["DEMO-LUNG-001:I6"], []]


def test_verdict_and_evidence_changes_are_separated(demo_dir):
    v = inspector.build_view(inspector.load_run(demo_dir))
    ch = v["changes"]
    assert ch["verdict"] == [] and ch["trial"] == []  # 기준 판정·시험 판정은 바뀌지 않음
    assert {c["field"] for c in ch["evidence"]} == {"gap", "self_reported_holds", "needs_verification"}
    assert all(c["passes_after"] is None for c in ch["evidence"])  # I6은 계속 판정 불가 (기록 값)
    assert ch["stable_trials"] == [{"trial_id": "DEMO-LUNG-001", "eligibility": "UNCERTAIN", "rounds": [0, 1, 2]}]
    # 10-B-2: 비교 불가 건수(incomparable)가 계약에 추가됨. 기존 세 값은 그대로
    assert v["overview"]["n_changes"] == {"trial": 0, "verdict": 0, "evidence": 3, "incomparable": 0}


def _done(tid, rnd, elig, items):
    base = {"kind": "inclusion", "criterion_text": "x", "method": "rule_engine"}
    return {"agent": "matcher", "event": "done", "trial_id": tid, "round": rnd, "eligibility": elig,
            "assessments": [{**base, **a} for a in items]}


def test_adjacent_round_changes_multi_round_fixture():
    """0->1에서 바뀌고 2->3에서 되돌아가는 경우: 첫·끝 비교로는 안 보이는 변화를 인접 비교로 모두 잡음."""
    trace = [_done("T1", 0, "UNCERTAIN", [{"rule_id": "A", "passes": None, "holds": "unknown", "gap": "ask"}]),
             _done("T1", 1, "ELIGIBLE", [{"rule_id": "A", "passes": True, "holds": "yes", "gap": None}]),
             _done("T1", 2, "ELIGIBLE", [{"rule_id": "A", "passes": True, "holds": "yes", "gap": None}]),
             _done("T1", 3, "UNCERTAIN", [{"rule_id": "A", "passes": None, "holds": "unknown", "gap": "ask"}])]
    rec = inspector.RunRecord(result={"patient_id": "P1", "qa_log": [], "matches": {"T1": {
        "eligibility": "UNCERTAIN", "assessments": [{"rule_id": "A", "kind": "inclusion", "passes": None}]}}}, trace=trace)
    v = inspector.build_view(rec)
    trial = [(c["from_round"], c["to_round"], c["before"], c["after"]) for c in v["changes"]["trial"] if c["field"] == "eligibility"]
    assert trial == [(0, 1, "UNCERTAIN", "ELIGIBLE"), (2, 3, "ELIGIBLE", "UNCERTAIN")]
    passes = [(c["from_round"], c["to_round"], c["before"], c["after"]) for c in v["changes"]["verdict"] if c["field"] == "passes"]
    assert passes == [(0, 1, None, True), (2, 3, True, None)]
    gap = [(c["from_round"], c["to_round"]) for c in v["changes"]["evidence"] if c["field"] == "gap"]
    assert gap == [(0, 1), (2, 3)]
    assert [len(x["verdict_changes"]) for x in v["timeline"]] == [0, 1, 0, 1]
    assert v["changes"]["stable_trials"] == []


def test_missing_round_records_are_not_filled():
    """한 시험이 라운드 1에 다시 평가되지 않은 경우·qa_log·진단이 없는 경우: 기록 없음으로 두고 배정·추정하지 않음."""
    trace = [_done("T1", 0, "UNCERTAIN", [{"rule_id": "A", "passes": None}]),
             _done("T2", 0, "UNCERTAIN", [{"rule_id": "B", "passes": None}]),
             _done("T1", 1, "UNCERTAIN", [{"rule_id": "A", "passes": None}]),
             _done("T2", 2, "INELIGIBLE", [{"rule_id": "B", "passes": False}])]
    rec = inspector.RunRecord(result={"patient_id": "P1", "matches": {
        "T1": {"eligibility": "UNCERTAIN", "assessments": []}, "T2": {"eligibility": "INELIGIBLE", "assessments": []}}},
        trace=trace)
    v = inspector.build_view(rec)
    tl = {x["round"]: x for x in v["timeline"]}
    assert tl[1]["trials"] == {"T1": "UNCERTAIN", "T2": None}  # T2는 라운드 1 기록 없음
    assert all(x["qa"] is None and x["fact_changed_fields"] is None and x["decision"] is None for x in tl.values())
    assert [(c["from_round"], c["to_round"]) for c in v["changes"]["trial"]] == [(0, 2)]  # T2의 인접 기록끼리
    assert v["overview"]["n_questions"] is None and v["overview"]["run_kind"] == "확인 불가"
    page = inspector.render_html(v)
    assert "○ 기록 없음" in page
    # trace가 아예 없으면 타임라인·라운드 수도 없음 (0으로 두지 않음)
    v2 = inspector.build_view(inspector.RunRecord(result=rec.result))
    assert v2["timeline"] is None and v2["overview"]["n_rounds"] is None and v2["overview"]["n_changes"] is None


def test_run_kind_and_recorded_zero_vs_missing():
    real = _record(meta={"llm_backend": "real", "synthetic": False})
    real.result["usage"] = {"calls": 0}
    v = inspector.build_view(real)
    assert v["overview"]["run_kind"] == "실제" and v["usage"] == {"calls": 0}  # 기록된 0은 0
    assert not any("usage" in g for g in v["gaps"])
    del real.result["usage"]
    assert any("usage 항목 없음" in g for g in inspector.build_view(real)["gaps"])
    assert inspector.build_view(_record(meta={"llm_backend": "offline_stub"}))["overview"]["run_kind"] == "확인 불가"


def test_new_sections_do_not_leak_in_strict_mode():
    secret = "PATIENT-RAW-SECRET 72세 남성 혈소판 85"
    rec = _record()
    rec.result["qa_log"].append({"round": 1, "question_id": "Q2", "question": secret, "answer": secret, "target": []})
    rec.trace += [_done("NCT0", 1, "UNCERTAIN", [{"rule_id": "I1", "passes": None, "evidence": secret, "reasoning": secret,
                                                "missing_info": secret, "evidence_source": secret, "gap": secret}])]
    rec.result["dependency_diagnostic"] = [{"round": 1, "status": "ok", "changes": {"changed_fields": [secret, "ecog"]}}]
    rec.result["decision_diagnostic"] = [{"round": 1, "status": "ok", "stop_reason": secret, "actions": []}]
    v = inspector.build_view(rec)
    blob = json.dumps({k: v[k] for k in ("overview", "timeline", "changes")}, ensure_ascii=False)
    page = inspector.render_html(v)
    for s in ("PATIENT-RAW-SECRET", "혈소판 85", "REAL-123", "sk-ant-SECRETKEY", "tok-SECRET"):
        assert s not in blob and s not in page, s
    assert v["timeline"][-1]["fact_changed_fields"] == ["ecog"]


def test_run_without_questions():
    rec = inspector.RunRecord(result={"patient_id": "P1", "qa_log": [], "matches": {"T1": {"eligibility": "INELIGIBLE",
                              "assessments": []}}},
                              trace=[_done("T1", 0, "INELIGIBLE", [{"rule_id": "A", "passes": False}])])
    v = inspector.build_view(rec)
    assert not any("직접 연결" in g for g in v["gaps"])  # 질문이 없으면 연결 기록 문구를 붙이지 않음
    assert v["overview"]["n_questions"] == 0 and v["timeline"][0]["qa"]["count"] == 0
    page = inspector.render_html(v)
    assert "기록된 질문 없음" in page and "해당 없음 (질문은 라운드 1부터)" in page


def test_missing_screening_cause_not_asserted(demo_dir):
    page = inspector.render_html(inspector.build_view(inspector.load_run(demo_dir)))
    assert "사람이 정한" not in page and "추림을 하지 않았거나 기록되지 않음" in page


def test_new_sections_escaped_and_csp():
    """10-B-2: 형식이 다른 시험 ID(공격 문자열)는 표시하지 않고 형식 문제로만 남김. 표시되는 필드(기준 원문·
    질문 문구·판정 값)에 넣은 공격 문자열은 이스케이프되어야 함."""
    x = '"><img src=x onerror=alert(1)><script>z</script>'
    good = {"T1": {"eligibility": x, "assessments": [{"rule_id": "A", "kind": "inclusion", "criterion_text": x}]}}
    rec = inspector.RunRecord(result={"patient_id": "P1", "matches": {x: {"eligibility": x, "assessments": []}, **good},
                                      "qa_log": [{"round": 1, "question_id": "Q1", "question": x, "answer": x}]},
                              trace=[_done(x, 0, x, [{"rule_id": "A"}]), _done("T1", 0, "UNCERTAIN", [{"rule_id": "A"}]),
                                     _done("T1", 1, "UNCERTAIN", [{"rule_id": "A", "criterion_text": x}])],
                              meta={"llm_backend": x, "synthetic": True})
    for mode in (False, True):
        v = inspector.build_view(rec, synthetic_details=mode)
        page = inspector.render_html(v)
        assert "<img" not in page and "<script" not in page.lower() and "&lt;img" in page
        assert "default-src 'none'; style-src 'unsafe-inline'" in page
        assert any("matches: 형식이 다른 항목 1건" in a for a in v["anomalies"])


def test_new_layout_output_not_overwritten(demo_dir, tmp_path):
    out = tmp_path / "inspector_10b1.html"
    with redirect_stdout(io.StringIO()):
        ri.main(["render", str(demo_dir), "--out", str(out)])
    first = out.read_bytes()
    assert "B. Decision Loop 타임라인" in first.decode("utf-8")
    with pytest.raises(SystemExit) as e:
        ri.main(["render", str(demo_dir), "--out", str(out)])
    assert e.value.code == 2 and out.read_bytes() == first


# ---------------------------------------------------------------- Phase 10-B-1 검토 반영 (표시 문구)

def test_d1_badge_separates_verdict_and_evidence_changes(demo_dir):
    page = inspector.render_html(inspector.build_view(inspector.load_run(demo_dir)))
    summary = page[page.index("D1. 후보 임상시험"):page.index("</summary>", page.index("D1. 후보 임상시험"))]
    assert "근거·확인 상태가 변경된 기준 1개" in summary  # I6 하나 (변경 항목 3건이지만 기준은 1개)
    assert "기준 판정이 변경된 기준" not in summary and "기준 1개 변화" not in page
    assert "기준 판정 0건 (기준 0개)" in page and "근거·확인 상태 3건 (기준 1개)" in page


def test_d1_badge_counts_verdict_changes_generically():
    trace = [_done("T1", 0, "UNCERTAIN", [{"rule_id": "A", "passes": None, "gap": "ask"},
                                          {"rule_id": "B", "passes": None, "gap": "ask"},
                                          {"rule_id": "C", "passes": None, "gap": "ask"}]),
             _done("T1", 1, "UNCERTAIN", [{"rule_id": "A", "passes": True, "gap": None},
                                          {"rule_id": "B", "passes": None, "gap": "verify"},
                                          {"rule_id": "C", "passes": None, "gap": "ask"}])]
    rec = inspector.RunRecord(result={"patient_id": "P1", "qa_log": [], "matches": {"T1": {
        "eligibility": "UNCERTAIN", "assessments": [{"rule_id": k, "kind": "inclusion"} for k in "ABC"]}}}, trace=trace)
    page = inspector.render_html(inspector.build_view(rec))
    # A: 판정과 해소 경로가 함께 바뀜 -> 두 묶음에 각각 셈, B: 해소 경로만, C: 변화 없음
    assert "기준 판정이 변경된 기준 1개" in page and "근거·확인 상태가 변경된 기준 2개" in page


def test_overview_backend_before_model_config(demo_dir):
    page = inspector.render_html(inspector.build_view(inspector.load_run(demo_dir)))
    a = page[page.index("A. 실행 개요"):page.index("B. Decision Loop 타임라인")]
    cards = a[a.index('<div class="kv">'):a.index("</div></div>") + 12]
    assert cards.index("실행 백엔드") < cards.index("실행 유형")
    assert "offline_stub" in cards and "오프라인 대역 · 실제 모델 판정 아님" in cards
    assert "claude-sonnet-5" not in cards  # 모델 설정값은 카드가 아니라 참고 줄로
    assert a.index("offline_stub") < a.index("claude-sonnet-5")
    assert "모델 설정값(참고)" in a and "모델이 호출되었는지나 어떤 모델이 호출되었는지를 뜻하지 않습니다" in a


# ---------------------------------------------------------------- Phase 10-B-2: 기능 검증·정확성
# (A) 실제 파이프라인 오프라인 실행 기록 (question_eval.OfflineEnvironment 대역) / (B) 손으로 만든 최소 기록 fixture.
# 두 종류를 섞지 않음: (A)는 Session이 실제로 남긴 기록, (B)는 실제 실행에서 아직 관찰되지 않은 경우를 재현.

FAKE_TRIAL = {"NCT99999999": {"trial_id": "NCT99999999", "title": "fixture", "overall_status": "RECRUITING",
                              "eligibility_criteria": "Inclusion Criteria: age >= 18", "conditions": []}}


@pytest.fixture(scope="module")
def offline_runs(tmp_path_factory):
    """(A) 실제 Session 오프라인 실행: 질문 없이 종료 / 고정 기준표에 없는 시험(평가 실패) / 여러 후보 시험."""
    base = tmp_path_factory.mktemp("offline_runs")
    runs = {}
    with redirect_stdout(io.StringIO()):
        runs["no_questions"] = (ri.run_offline_session("F-SYN-DEMO-A", base / "noq", rounds=0), base / "noq")
        runs["not_evaluated"] = (ri.run_offline_session("F-SYN-DEMO-A", base / "ne", extra_trials=FAKE_TRIAL), base / "ne")
        runs["multi_trial"] = (ri.run_offline_session("F-SYN-S001-01", base / "multi"), base / "multi")
    return runs


def test_a_offline_run_without_questions(offline_runs):
    v = inspector.build_view(inspector.load_run(offline_runs["no_questions"][1]))
    assert v["overview"]["n_questions"] == 0 and v["overview"]["rounds"] == [0]  # 기록된 0 (누락 아님)
    assert v["anomalies"] == [] and v["changes"]["comparable"] is False
    assert not any("직접 연결" in g for g in v["gaps"])
    assert "해당 없음 (질문은 라운드 1부터)" in inspector.render_html(v)


def test_a_offline_run_with_not_evaluated_trial(offline_runs):
    v = inspector.build_view(inspector.load_run(offline_runs["not_evaluated"][1]))
    o = v["overview"]
    assert o["n_not_evaluated"] == 1 and o["n_errors"] == 1
    t = next(t for t in v["trials"] if t["trial_id"] == "NCT99999999")
    assert t["eligibility"] == "NOT_EVALUATED" and t["error"] == {"stage": "기준 파싱 실패", "type": "CompilationPending"}
    assert any("NCT99999999: trace에 판정 기록 없음" in g for g in v["gaps"])  # 판정 기록이 없음을 그대로 알림
    page = inspector.render_html(v)
    assert "평가 실패(NOT_EVALUATED) 시험 1개" in page and v["anomalies"] == []


def test_a_offline_multi_trial_run(offline_runs):
    v = inspector.build_view(inspector.load_run(offline_runs["multi_trial"][1]))
    assert len(v["trials"]) == 3 and v["anomalies"] == []
    for x in v["timeline"]:
        assert set(x["trial_evals"]) == {t["trial_id"] for t in v["trials"]}
    # 연속 기록 비교는 시험마다 따로 (시험 3개 x 라운드 쌍 2개)
    assert len([p for x in v["timeline"] for p in x["comparisons"]]) == 6


def test_a_record_from_session_matches_saved_run(offline_runs):
    """메모리 Session 경로(record_from_session)와 파일 경로(load_run)가 같은 화면 데이터를 냄."""
    session, d = offline_runs["multi_trial"]
    saved = inspector.load_run(d)
    mem = inspector.record_from_session(session, trace=saved.trace, meta=saved.meta)
    a, b = inspector.build_view(mem), inspector.build_view(saved)
    for k in ("overview", "timeline", "changes", "trials", "questions", "facts", "anomalies"):
        assert a[k] == b[k], k


def test_rescue_counts_are_unchanged(demo_dir):
    v = inspector.build_view(inspector.load_run(demo_dir))
    ch = v["changes"]
    assert len(ch["trial"]) == 0 and ch["n_criteria"]["verdict"] == 0 and ch["n_criteria"]["evidence"] == 1
    assert sorted(c["field"] for c in ch["evidence"] if c["rule_id"] == "I6") == ["gap", "needs_verification",
                                                                                 "self_reported_holds"]
    assert ch["incomparable"] == [] and v["anomalies"] == []


def _gen(rnd, ids):
    return {"agent": "question_generator", "event": "questions", "round": rnd,
            "questions": [{"question_id": q, "target": ["T1:A"], "purpose": "eligibility"} for q in ids]}


def _rec(trace, qa_log=..., matches=None, **result):
    r = {"patient_id": "P1", "matches": matches or {"T1": {"eligibility": "UNCERTAIN",
                                                            "assessments": [{"rule_id": "A", "kind": "inclusion"}]}}, **result}
    if qa_log is not ...:
        r["qa_log"] = qa_log
    return inspector.RunRecord(result=r, trace=trace)


def test_b_questions_generated_but_not_answered():
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [{"rule_id": "A"}]), _gen(1, ["Q1", "Q2"]),
                                   _done("T1", 1, "UNCERTAIN", [{"rule_id": "A"}])], qa_log=[]))
    assert [q["answer_status"] for q in v["questions"]] == ["미기록", "미기록"]  # 답변을 만들지 않음
    assert [q["source"] for q in v["questions"]] == ["trace만", "trace만"]
    assert any("질문 기록 불일치: trace에만 2건" in a for a in v["anomalies"])
    assert "불일치: trace에 질문 생성 기록 2개" in inspector.render_html(v)  # '질문 없음'으로 표시하지 않음


def test_b_qa_log_and_trace_round_mismatch_not_linked():
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [{"rule_id": "A"}]), _gen(1, ["Q1"]),
                                   _done("T1", 1, "UNCERTAIN", [{"rule_id": "A"}])],
                                  qa_log=[{"round": 2, "question_id": "Q1", "answer": "예", "target": []}]))
    assert [(q["round"], q["source"], q["answer_status"]) for q in v["questions"]] == \
        [(1, "trace만", "미기록"), (2, "qa_log만", "답변함")]  # 라운드를 맞춰 붙이지 않음
    assert v["unlinked_question_rounds"] == [2]
    assert any("trace에만 1건, qa_log에만 1건" in a for a in v["anomalies"])


@pytest.mark.parametrize("entry,status", [({}, "답변 값 없음"), ({"answer": None}, "답변 값 없음"), ({"answer": ""}, "빈 답변"),
                                          ({"answer": "  "}, "빈 답변"), ({"answer": 3}, "형식 오류"),
                                          ({"answer": "모름"}, "모름·확인 불가"), ({"answer": "예"}, "답변함")])
def test_b_answer_states_are_not_inflated(entry, status):
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [{"rule_id": "A"}]),
                                   _done("T1", 1, "UNCERTAIN", [{"rule_id": "A"}])],
                                  qa_log=[{"round": 1, "question_id": "Q1", **entry}]))
    assert v["questions"][0]["answer_status"] == status
    qa = v["timeline"][1]["qa"]
    assert qa["answered"] == (status == "답변함") and qa["declined"] == (status == "모름·확인 불가")
    assert qa["missing"] == (status not in ("답변함", "모름·확인 불가"))


def test_b_questions_in_one_round_only():
    v = inspector.build_view(_rec([_done("T1", r, "UNCERTAIN", [{"rule_id": "A"}]) for r in (0, 1, 2)] + [_gen(1, ["Q1"])],
                                  qa_log=[{"round": 1, "question_id": "Q1", "answer": "모름"}]))
    assert [x["qa"]["count"] for x in v["timeline"]] == [0, 1, 0]
    assert [x["questions_generated"] for x in v["timeline"]] == [None, 1, None]
    assert "이 평가 전 기록된 질문 없음" in inspector.render_html(v)


@pytest.mark.parametrize("bad_round", [..., None, "1", -1, 1.5, True])
def test_b_judgement_without_valid_round_is_not_assigned(bad_round):
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [{"rule_id": "A", "passes": None}]),
                                   _done("T1", bad_round, "ELIGIBLE", [{"rule_id": "A", "passes": True}])], qa_log=[]))
    assert v["timeline"][0]["trials"] == {"T1": "UNCERTAIN"}  # 라운드 0에 덮어쓰지 않음
    assert [x["round"] for x in v["timeline"]] == [0] and v["changes"]["trial"] == []
    assert any("라운드 번호가 없거나 형식이 다른 판정 기록 1건" in a for a in v["anomalies"])


def test_b_change_kinds_counted_by_criterion():
    """판정만 / 근거 상태만 / 둘 다 / 되돌아감 / 여러 기준 동시. 기준 수와 항목 수를 따로 셈."""
    tr = [_done("T1", 0, "UNCERTAIN", [{"rule_id": "V", "passes": None, "holds": "unknown", "gap": "ask"},
                                       {"rule_id": "E", "passes": None, "needs_verification": False, "gap": "ask"},
                                       {"rule_id": "B", "passes": None, "holds": "unknown", "gap": "ask"},
                                       {"rule_id": "R", "passes": None, "gap": "ask"}]),
          _done("T1", 1, "UNCERTAIN", [{"rule_id": "V", "passes": False, "holds": "no", "gap": "ask"},
                                       {"rule_id": "E", "passes": None, "needs_verification": True, "gap": "verify"},
                                       {"rule_id": "B", "passes": True, "holds": "yes", "gap": None},
                                       {"rule_id": "R", "passes": True, "gap": "ask"}]),
          _done("T1", 2, "UNCERTAIN", [{"rule_id": "V", "passes": False, "holds": "no", "gap": "ask"},
                                       {"rule_id": "E", "passes": None, "needs_verification": True, "gap": "verify"},
                                       {"rule_id": "B", "passes": True, "holds": "yes", "gap": None},
                                       {"rule_id": "R", "passes": None, "gap": "ask"}])]
    v = inspector.build_view(_rec(tr, qa_log=[]))
    ch = v["changes"]
    assert {c["rule_id"] for c in ch["verdict"]} == {"V", "B", "R"} and ch["n_criteria"]["verdict"] == 3
    assert {c["rule_id"] for c in ch["evidence"]} == {"E", "B"} and ch["n_criteria"]["evidence"] == 2
    assert len(ch["verdict"]) == 6 and len(ch["evidence"]) == 3  # 항목 수는 기준 수와 다름
    assert [(c["from_round"], c["to_round"]) for c in ch["verdict"] if c["rule_id"] == "R"] == [(0, 1), (1, 2)]  # 되돌아감
    assert v["timeline"][1]["verdict_changes"] == ["T1:B", "T1:R", "T1:V"] and v["timeline"][2]["verdict_changes"] == ["T1:R"]


def test_b_missing_or_new_criteria_and_fields_are_incomparable():
    tr = [_done("T1", 0, "UNCERTAIN", [{"rule_id": "A", "passes": None}, {"rule_id": "B", "passes": None},
                                       {"rule_id": "C", "passes": None, "gap": "ask"},
                                       {"rule_id": "D", "passes": None, "gap": None}]),
          _done("T1", 1, "UNCERTAIN", [{"rule_id": "A", "passes": None}, {"rule_id": "N", "passes": True},
                                       {"rule_id": "C", "passes": None}, {"rule_id": "D", "passes": None, "gap": None}])]
    v = inspector.build_view(_rec(tr, qa_log=[]))
    inc = {(c["rule_id"], c["field"], c["reason"]) for c in v["changes"]["incomparable"]}
    assert inc == {("B", None, "다음 기록에 기준 없음"), ("N", None, "이 기록에 새로 등장"),
                   ("C", "gap", "필드가 한쪽 기록에만 있음")}  # D: None과 None은 비교 가능(변화 없음)
    assert v["changes"]["verdict"] == [] and v["changes"]["evidence"] == []
    page = inspector.render_html(v)
    assert "비교 불가 3개" in page and "④ 비교 불가" in page
    assert "＝ 변화 없음</span> 비교한 시험" not in page  # 비교 불가가 있으면 '변화 없음'이라고 하지 않음


def test_b_trial_missing_in_middle_round():
    tr = [_done("T1", 0, "UNCERTAIN", [{"rule_id": "A"}]), _done("T2", 0, "UNCERTAIN", [{"rule_id": "B"}]),
          _done("T1", 1, "UNCERTAIN", [{"rule_id": "A"}]), _done("T2", 2, "INELIGIBLE", [{"rule_id": "B"}]),
          _done("T1", 2, "UNCERTAIN", [{"rule_id": "A"}])]
    m = {"T1": {"eligibility": "UNCERTAIN", "assessments": []}, "T2": {"eligibility": "INELIGIBLE", "assessments": []}}
    v = inspector.build_view(_rec(tr, qa_log=[], matches=m))
    r1 = v["timeline"][1]
    assert r1["trial_evals"] == {"T1": ["UNCERTAIN"], "T2": []}
    assert [p["trial_id"] for p in r1["comparisons"]] == ["T1"]  # T2는 라운드 1 비교 없음
    assert [(c["trial_id"], c["from_round"], c["to_round"]) for c in v["changes"]["trial"]] == [("T2", 0, 2)]
    page = inspector.render_html(v)
    seg = page[page.index("라운드 1 —"):page.index("</li>", page.index("라운드 1 —"))]
    assert 'T2 <span class="na">○ 기록 없음</span>' in seg


def test_b_final_result_and_trace_mismatch_flagged():
    tr = [_done("T1", 0, "UNCERTAIN", [{"rule_id": "A", "passes": None}])]
    m = {"T1": {"eligibility": "ELIGIBLE", "assessments": [{"rule_id": "A", "kind": "inclusion", "passes": True}]}}
    v = inspector.build_view(_rec(tr, qa_log=[], matches=m))
    assert any("최종 판정 불일치" in a and "ELIGIBLE" in a and "UNCERTAIN" in a for a in v["anomalies"])
    assert any("기준 판정이 result.json과 trace 마지막 기록에서 다름 ['A']" in a for a in v["anomalies"])
    assert "기록 간 불일치·형식 문제 2건" in inspector.render_html(v)


def test_b_same_round_reevaluation_compared_in_trace_order():
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [{"rule_id": "A"}]),
                                   _done("T1", 0, "INELIGIBLE", [{"rule_id": "A"}])], qa_log=[],
                                  matches={"T1": {"eligibility": "INELIGIBLE", "assessments": []}}))
    assert v["timeline"][0]["trial_evals"] == {"T1": ["UNCERTAIN", "INELIGIBLE"]}
    assert [(c["before"], c["after"], c["same_round"]) for c in v["changes"]["trial"]] == [("UNCERTAIN", "INELIGIBLE", True)]
    assert any("같은 라운드 재평가 기록 1건" in g for g in v["gaps"]) and v["anomalies"] == []
    assert "라운드 0 안 재평가" in inspector.render_html(v)


def test_b_multiple_diagnostics_in_one_round_not_merged():
    rec = _rec([_done("T1", 0, "UNCERTAIN", [{"rule_id": "A"}])], qa_log=[],
               decision_diagnostic=[{"round": 0, "status": "ok", "stop_reason": "awaiting_user_answers", "actions": []},
                                    {"round": 0, "status": "ok", "stop_reason": "needs_review", "actions": []}],
               dependency_diagnostic=[{"round": 0, "status": "ok", "changes": {"changed_fields": ["ecog"]}},
                                      {"round": 0, "status": "ok", "changes": {"changed_fields": ["age"]}}])
    x = inspector.build_view(rec)["timeline"][0]
    assert x["decision"] is None and [d["stop_reason"] for d in x["decisions"]] == ["awaiting_user_answers", "needs_review"]
    assert x["fact_changed_fields"] is None and len(x["fact_change_entries"]) == 2


def test_b_missing_errors_and_qa_log_are_unknown_not_zero():
    v = inspector.build_view(_rec([], qa_log=[]))
    assert v["overview"]["n_errors"] is None and any("errors 항목 없음" in g for g in v["gaps"])
    assert inspector.build_view(_rec([], qa_log=[], errors=[]))["overview"]["n_errors"] == 0
    assert inspector.build_view(_rec([]))["overview"]["n_questions"] is None  # qa_log 없음 != 0


SECRET = "PATIENT-RAW-SECRET 혈소판 85 sk-ant-SECRETKEY"


@pytest.mark.parametrize("field,value", [
    ("qa_log", SECRET), ("qa_log", [SECRET, 5]), ("matches", [SECRET]), ("matches", {"T1": SECRET}),
    ("matches", {"T1": {"eligibility": SECRET, "assessments": SECRET}}), ("usage", SECRET), ("errors", SECRET),
    ("errors", [{"item": SECRET, "error": SECRET}]), ("fact_shadow", SECRET), ("decision_diagnostic", {"x": SECRET}),
    ("dependency_diagnostic", [{"round": SECRET, "changes": SECRET}]),
    ("eligibility_history", [{"step": SECRET, "eligibility": SECRET}]), ("screening", SECRET),
    ("screening", {"selected": SECRET, "hard_filtered": [{"trial_id": SECRET, "reason": SECRET}]}),
    ("recommendation", {"ranked": SECRET}), ("model", SECRET), ("input_source", SECRET)])
def test_b_malformed_fields_do_not_crash_or_leak(field, value):
    rec = _rec([_done("T1", 0, "UNCERTAIN", [{"rule_id": "A"}]), SECRET,
                {"agent": "matcher", "event": "done", "trial_id": [SECRET], "round": 0},
                {"agent": "question_generator", "event": "questions", "round": 1, "questions": SECRET}], qa_log=[])
    rec.result[field] = value
    rec.metering = SECRET
    v = inspector.build_view(rec)
    page = inspector.render_html(v)
    blob = json.dumps(v, ensure_ascii=False)
    for s in ("PATIENT-RAW-SECRET", "혈소판 85", "sk-ant"):
        assert s not in page and s not in blob, (field, s)
    assert v["anomalies"]  # 형식 문제는 숨기지 않고 알림


def test_b_result_must_be_object():
    with pytest.raises(inspector.InspectorError):
        inspector.build_view(inspector.RunRecord(result=[SECRET]))


def test_b_corrupted_files(tmp_path):
    d = _write_run(tmp_path / "run")
    (d / "trace.jsonl").write_text(json.dumps(_done("T1", 0, "UNCERTAIN", [])) + '\n{"truncated": ' + SECRET + "\n",
                                   encoding="utf-8")
    rec = inspector.load_run(d)
    assert rec.issues == ["trace.jsonl: JSON으로 읽을 수 없는 줄 1건 - 읽지 않음"] and len(rec.trace) == 1
    v = inspector.build_view(rec)
    assert v["anomalies"][0].startswith("trace.jsonl") and "PATIENT-RAW-SECRET" not in inspector.render_html(v)
    (d / "result.json").write_text("{" + SECRET, encoding="utf-8")
    with pytest.raises(inspector.InspectorError) as e:
        inspector.load_run(d)
    assert "PATIENT-RAW-SECRET" not in str(e.value)


# ---------------------------------------------------------------- Phase 10-C-2: 사실 기록 출처 검증 (run_meta 교차 확인)
# 백엔드 CLI(commit 36728b6) run_meta.json 형식 그대로의 fixture. 실제 CLI 출력과의 확인은 scratch에서 별도로 함.

SECRET_FIELD = "lab:secret_marker_field"


def _cli_meta(fact_store, **over) -> dict:
    meta = {"format_version": 1, "written_by": "ctrec.cli", "run_id": "R-0123456789ab", "created_at": "2026-10-24T00:00:00+00:00",
            "llm_backend": "cli_sdk_path", "llm_calls_observed": None, "synthetic": None, "mode": "fixed",
            "candidates": "given", "requested_model": "claude-sonnet-5", "called_models": "UNKNOWN",
            "llm_responses_with_usage": None,
            "records": {"fact_store": fact_store, "decision_diagnostic": False, "dependency_diagnostic": False}}
    meta.update(over)
    return meta


def _prov_run(tmp_path: Path, meta, facts: bool) -> Path:
    d = tmp_path / "run"
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({"patient_id": "P-1", "matches": {}, "qa_log": [], "errors": []}),
                                   encoding="utf-8")
    if facts:
        (d / "facts").mkdir()
        (d / "facts" / "P-1.json").write_text(json.dumps({"facts": [{"field": SECRET_FIELD, "value": 85}]}), encoding="utf-8")
    if meta is not None:
        (d / "run_meta.json").write_text(meta if isinstance(meta, str) else json.dumps(meta), encoding="utf-8")
    return d


def _prov(d: Path):
    v = inspector.build_view(inspector.load_run(d))
    page = inspector.render_html(v)  # 모든 경우 렌더링 성공
    assert "P-1" not in page and "patient-" in page  # strict: 환자 ID 비노출
    return v, page


def test_c2_backend_run_dir_facts_are_verified(tmp_path):
    v, page = _prov(_prov_run(tmp_path, _cli_meta("run_dir"), facts=True))
    assert v["run"]["fact_provenance"] == {"status": "verified", "recorded": "run_dir"}
    assert v["facts"] and v["facts"][0]["field"] == SECRET_FIELD and "value" not in v["facts"][0]
    assert v["anomalies"] == [] and not any("사실 출처" in g or "facts/" in g for g in v["gaps"])
    # 표시 문구는 검증 범위(run_meta 자기 선언 + 파일 존재)를 넘지 않음
    assert "실행 메타데이터와 일치" in page and "독립적으로 증명한 것은 아님" in page and "출처 확인" not in page
    assert "85" not in page


def test_c2_backend_no_fact_store_no_facts(tmp_path):
    v, page = _prov(_prov_run(tmp_path, _cli_meta(None), facts=False))
    assert v["facts"] is None and v["anomalies"] == []
    assert any("이번 실행은 사실을 저장하지 않음" in g for g in v["gaps"])


def test_c2_stale_facts_with_fact_store_null_are_hidden(tmp_path):
    v, page = _prov(_prov_run(tmp_path, _cli_meta(None), facts=True))
    assert v["facts"] is None and v["run"]["fact_provenance"]["status"] == "hidden"
    assert any("fact_store=null" in a and "표시하지 않음" in a for a in v["anomalies"])
    assert SECRET_FIELD not in page and "사실 저장소(facts/) 없음" not in page


@pytest.mark.parametrize("meta", ["{broken", "[1, 2]"])
def test_c2_corrupted_run_meta_hides_facts(tmp_path, meta):
    d = _prov_run(tmp_path, meta, facts=True)
    rec = inspector.load_run(d)
    v, page = _prov(d)
    assert v["facts"] is None and SECRET_FIELD not in page
    if meta == "{broken":
        assert rec.meta_status == "corrupt" and any("run_meta.json을 읽을 수 없음" in a for a in v["anomalies"])
    else:  # JSON이지만 객체가 아님
        assert any("run_meta.json: 객체가 아님" in a for a in v["anomalies"])


@pytest.mark.parametrize("records", [{"fact_store": "elsewhere"}, {"other": 1}, "run_dir", None])
def test_c2_invalid_records_hide_facts(tmp_path, records):
    v, page = _prov(_prov_run(tmp_path, _cli_meta("run_dir", records=records), facts=True))
    assert v["facts"] is None and any("records.fact_store 형식이 계약과 다름" in a for a in v["anomalies"])


def test_c2_external_fact_store(tmp_path):
    v, _ = _prov(_prov_run(tmp_path, _cli_meta("external"), facts=False))
    assert v["facts"] is None and v["anomalies"] == []
    assert any("외부 경로에 저장됨" in g and "읽지 않음" in g for g in v["gaps"])
    v2, page = _prov(_prov_run(tmp_path / "b", _cli_meta("external"), facts=True))
    assert v2["facts"] is None and any("외부 경로 저장인데 실행 폴더에 facts/ 파일이 있음" in a for a in v2["anomalies"])


def test_c2_legacy_run_without_records_is_shown_as_unverified(tmp_path):
    """이전 형식(run_meta 없음): 오류로 처리하지 않고 표시하되 출처 미검증으로 명시."""
    v, page = _prov(_prov_run(tmp_path, None, facts=True))
    assert v["facts"] and v["run"]["fact_provenance"]["status"] == "unverified" and v["anomalies"] == []
    assert any("사실 출처 미검증" in g for g in v["gaps"]) and "출처 미검증: 이전 형식 기록" in page
    v2, _ = _prov(_prov_run(tmp_path / "b", {"llm_backend": "offline_stub", "synthetic": True}, facts=True))
    assert v2["run"]["fact_provenance"]["status"] == "unverified"  # run_meta는 있지만 records가 없는 이전 형식


def test_c2_run_dir_recorded_but_facts_link_outside(tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "P-1.json").write_text(json.dumps({"facts": [{"field": "LEAK"}]}), encoding="utf-8")
    d = _prov_run(tmp_path, _cli_meta("run_dir"), facts=False)
    import subprocess
    if sys.platform != "win32" or subprocess.run(["cmd", "/c", "mklink", "/J", str(d / "facts"), str(secret)],
                                                 capture_output=True).returncode:
        pytest.skip("정션을 만들 수 없음")
    v, page = _prov(d)
    assert v["facts"] is None and "LEAK" not in page
    assert any(a.startswith("facts: 실행 폴더 밖") for a in v["anomalies"])
    assert any("저장했다고 기록했으나 facts/ 파일을 읽지 못함" in a for a in v["anomalies"])


def test_c2_call_fields_are_not_overstated(tmp_path):
    v, page = _prov(_prov_run(tmp_path, _cli_meta("run_dir"), facts=True))
    m = v["run"]["meta"]
    assert m["called_models"] is None and m["llm_calls_observed"] is None and m["llm_responses_with_usage"] is None
    a = page[page.index("A. 실행 개요"):page.index("B. Decision")]
    assert "확인 못 함" in a and "0회라는 뜻 아님" in a and "기록 없음 (UNKNOWN)" in a
    assert "실제 API 실행으로 기록됨" not in page and "실제 호출 여부를 뜻하지 않음" in a  # cli_sdk_path는 호출 증거가 아님
    assert "synthetic=null" in a and v["overview"]["run_kind"] == "확인 불가"  # null은 실제 환자 실행이 아님
    v2, page2 = _prov(_prov_run(tmp_path / "b", _cli_meta("run_dir", llm_calls_observed=True, llm_responses_with_usage=7,
                                                           called_models=["claude-sonnet-5"]), facts=True))
    a2 = page2[page2.index("A. 실행 개요"):page2.index("B. Decision")]
    assert "호출 관찰됨 · 사용량 기록 응답 7건" in a2 and "claude-sonnet-5</code>" in a2


def test_c2_records_diagnostic_flags_cross_checked(tmp_path):
    d = _prov_run(tmp_path, _cli_meta("run_dir", records={"fact_store": "run_dir", "decision_diagnostic": True,
                                                          "dependency_diagnostic": False}), facts=True)
    v, _ = _prov(d)
    assert any("records.decision_diagnostic=true인데 result.json에는 없음" in a for a in v["anomalies"])


def test_c2_demo_run_meta_matches_contract(demo_dir):
    v = inspector.build_view(inspector.load_run(demo_dir))
    assert v["run"]["fact_provenance"]["status"] == "verified" and v["anomalies"] == []


def test_c2_session_facts_are_verified(offline_runs):
    session, d = offline_runs["multi_trial"]
    v = inspector.build_view(inspector.record_from_session(session))
    assert v["run"]["fact_provenance"]["status"] == "verified" and v["facts"]
