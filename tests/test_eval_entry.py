"""WP1 평가 기본 진입점 (B-1 답변기 주입, B-4 별칭·누출 차단 기본 경로, B-2 누락 포함 지표). 실제 API 호출 없음."""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import question_eval as qe  # noqa: E402
from ctrec.eval_hygiene import (AliasMap, ScopedOracleAnswerer, load_cases_hygienic,  # noqa: E402
                                run_method_hygienic, scoped_answerer_factory)

PARSED = load_frozen()
ALL_TRIALS = load_trials()
TRIALS = {t: ALL_TRIALS[t] for t in PARSED}
SUBSET = ["M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1", "M-SYN-DEMO-A-DEMO-LUNG-001-rescue2-3",
          "M-SYN-DEMO-B-DEMO-LUNG-001-cleanup-1", "F-SYN-DEMO-A"]


def _comparable(run) -> str:
    """같은 입력이면 같아야 하는 실행 결과 (질문·판정·공개 수)."""
    return json.dumps({"status": run.status, "questions": run.questions, "final": run.final_eligibility,
                       "gt": run.final_gt_states, "revealed": run.revealed}, sort_keys=True, default=str)


# ------------------------------------------------------------ B-1 답변기 주입
def test_default_answerer_unchanged():
    """answerer_factory를 주지 않거나 OracleAnswerer를 직접 주면 결과가 같음 (기존 동작 유지)."""
    case, gt = qe.load_cases(PARSED, only=SUBSET[1:2])[0]
    with qe.OfflineEnvironment(3):
        default = qe.run_method("candidate", case, gt, TRIALS, 2, 3)
        explicit = qe.run_method("candidate", case, gt, TRIALS, 2, 3, answerer_factory=qe.OracleAnswerer)
    assert default.status == "ok" and _comparable(default) == _comparable(explicit)


def test_injected_answerer_used_without_module_patch():
    case, gt = qe.load_cases(PARSED, only=SUBSET[1:2])[0]
    made, seen = [], []
    original = qe.OracleAnswerer

    class Spy:
        def __init__(self, g):
            made.append(g)
            self.log = []

        def answer(self, questions, patient_id):
            seen.append(qe.OracleAnswerer is original)  # 실행 중에도 모듈 속성은 그대로
            self.log.extend({"question_id": q.question_id, "revealed": False} for q in questions)
            return {q.question_id: "모름" for q in questions}

    with qe.OfflineEnvironment(3):
        run = qe.run_method("candidate", case, gt, TRIALS, 2, 3, answerer_factory=Spy)
    assert run.status == "ok" and made == [gt] and seen and all(seen) and run.revealed == 0


def test_hygienic_runner_injects_scoped_answerer():
    hcs, _, scanner = load_cases_hygienic(PARSED, only=SUBSET[1:2], alias_map=AliasMap(salt="t"))
    factory = scoped_answerer_factory(hcs[0].scope)
    assert isinstance(factory(hcs[0].gt), ScopedOracleAnswerer)
    with qe.OfflineEnvironment(3):
        scoped, _ = run_method_hygienic("candidate", hcs[0], TRIALS, 2, 3, scanner)
        direct = qe.run_method("candidate", hcs[0].case, hcs[0].gt, TRIALS, 2, 3, answerer_factory=factory)
    assert scoped.status == "ok" and _comparable(scoped) == _comparable(direct)
    import ctrec.eval_hygiene as eh
    assert not hasattr(eh, "_scoped_oracle") and not hasattr(eh, "_ORACLE_LOCK")  # 모듈 속성 교체 제거


def test_hygienic_runner_rejects_direct_factory():
    hcs, _, scanner = load_cases_hygienic(PARSED, only=SUBSET[:1], alias_map=AliasMap(salt="t"))
    import pytest
    with pytest.raises(TypeError):
        run_method_hygienic("baseline", hcs[0], TRIALS, 1, 3, scanner, answerer_factory=qe.OracleAnswerer)


def test_concurrent_runs_keep_their_own_answerer():
    """두 실행이 다른 답변기를 동시에 써도 섞이지 않음 (모듈 속성 교체 방식에서는 보장 안 됨)."""
    case, gt = qe.load_cases(PARSED, only=SUBSET[1:2])[0]
    hcs, _, _ = load_cases_hygienic(PARSED, only=SUBSET[1:2], alias_map=AliasMap(salt="t"))
    with qe.OfflineEnvironment(3):
        ref_legacy = _comparable(qe.run_method("candidate", case, gt, TRIALS, 2, 3))
        ref_scoped = _comparable(qe.run_method("candidate", case, gt, TRIALS, 2, 3,
                                               answerer_factory=scoped_answerer_factory(hcs[0].scope)))
        out: dict = {}

        def work(name, **kw):
            out[name] = [_comparable(qe.run_method("candidate", case, gt, TRIALS, 2, 3, **kw)) for _ in range(3)]

        ts = [threading.Thread(target=work, args=("legacy",)),
              threading.Thread(target=work, args=("scoped",),
                               kwargs={"answerer_factory": scoped_answerer_factory(hcs[0].scope)})]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
    assert out["legacy"] == [ref_legacy] * 3 and out["scoped"] == [ref_scoped] * 3


# ------------------------------------------------------------ B-4 기본 평가 경로 = 별칭 + 누출 차단
def _record_model_inputs(monkeypatch):
    """대역 모델(프로필·Matcher·추천)에 들어가는 모든 입력 문자열을 모음 (PromptLeakGuard와 독립된 확인)."""
    texts = []
    real_profiler, real_structured = qe.stub_profiler, qe.StubLLM.structured

    def profiler(pid, raw_text, reference_date=None):
        texts.append(f"{pid}\n{raw_text}")
        return real_profiler(pid, raw_text, reference_date)

    def structured(self, model_cls, *, system, user, **k):
        texts.append(f"{system}\n{user}")
        return real_structured(self, model_cls, system=system, user=user, **k)
    monkeypatch.setattr(qe, "stub_profiler", profiler)
    monkeypatch.setattr(qe.StubLLM, "structured", structured)
    return texts


def test_default_evaluation_never_sends_original_ids(monkeypatch):
    texts = _record_model_inputs(monkeypatch)
    r = qe.run_evaluation(PARSED, TRIALS, only=SUBSET, rounds=2, budget=3)
    h = r["hygiene"]
    assert h["mode"] == "alias_prompt_guard" and h["oracle_scope"] == "scoped"
    assert h["prompts_scanned"] > 0 and h["prompt_leaks"] == {} and h["cases_with_visible_issues"] == 0
    assert all(m["failed"] == 0 for m in r["methods"].values())
    assert texts and not any(cid in t for t in texts for cid in SUBSET)
    assert not any(re.search(r"\b(rescue2?|cleanup)\b", t, re.I) for t in texts)
    assert any(re.search(r"PA-[0-9a-f]{16}", t) for t in texts)  # 별칭은 들어감
    assert {row["case_id"] for row in r["case_rows"]} == set(SUBSET)  # 결과는 평가기 쪽 원래 ID (정답과 연결)
    assert "salt" not in json.dumps(h) and "PA-" not in json.dumps(h)


def test_original_id_path_only_when_explicit(monkeypatch):
    texts = _record_model_inputs(monkeypatch)
    r = qe.run_evaluation(PARSED, TRIALS, only=SUBSET[:1], rounds=1, budget=3, hygiene=False)
    assert r["hygiene"]["mode"] == "original_ids" and r["hygiene"]["prompts_scanned"] == 0
    assert any(SUBSET[0] in t for t in texts)  # 기존 경로는 ID가 들어감 (감사 P0-2) -> 명시적으로만
    assert qe.ORIGINAL_ID_NOTE in r["limitations"]
    import pytest
    with pytest.raises(ValueError):
        qe.run_evaluation(PARSED, TRIALS, only=SUBSET[:1], hygiene=False, oracle_scope="scoped")
    with pytest.raises(ValueError):
        qe.run_evaluation(PARSED, TRIALS, only=SUBSET[:1], hygiene=False, alias_map=AliasMap(salt="t"))
    with pytest.raises(ValueError):
        qe.run_evaluation(PARSED, TRIALS, only=SUBSET[:1], oracle_scope="all")


def test_alias_and_guard_do_not_change_results():
    """별칭·누출 검사만으로는 결과가 바뀌지 않음 (답변기를 같게 두면 행·질문 기록이 원래 ID 경로와 같음)."""
    a = qe.run_evaluation(PARSED, TRIALS, only=SUBSET, rounds=2, budget=3, hygiene=False)
    b = qe.run_evaluation(PARSED, TRIALS, only=SUBSET, rounds=2, budget=3, oracle_scope="legacy")
    dump = lambda r: json.dumps([r["case_rows"], r["question_records"], r["methods"]], sort_keys=True, default=str)  # noqa: E731
    assert dump(a) == dump(b)
    # 기본 = 범위 제한 답변기: 공개 내용이 달라 라운드 2부터 상태가 갈라질 수 있음. 공개 '횟수'는 단조가 아님
    # (1라운드에 덜 공개하면 2라운드 질문이 다시 맞아 한 번 더 공개될 수 있음) -> 결과 차이는 기록·설명으로 다룸
    c = qe.run_evaluation(PARSED, TRIALS, only=SUBSET, rounds=2, budget=3)
    assert c["hygiene"]["oracle_scope"] == "scoped" and qe.SCOPED_ORACLE_NOTE in c["limitations"]
    assert all(c["methods"][m]["failed"] == 0 for m in ("baseline", "candidate"))
    r1 = lambda r: [{k: v for k, v in q.items() if k != "answered"}  # noqa: E731 - 질문 자체는 같고 답 공개 여부만 다름
                    for q in r["question_records"] if q["round_id"] == 1]
    assert json.dumps(r1(c), sort_keys=True, default=str) == json.dumps(r1(a), sort_keys=True, default=str)


def test_scoped_oracle_never_reveals_more_facts_per_question_than_targeted():
    """기본 답변기의 공개 사실은 질문이 EXACT로 겨냥한 기준에 대응하는 사실뿐 (전 사례, 실행 결과 기준)."""
    hcs, _, scanner = load_cases_hygienic(PARSED, only=SUBSET, alias_map=AliasMap(salt="t"))
    with qe.OfflineEnvironment(3):
        for hc in hcs:
            made = []
            factory = scoped_answerer_factory(hc.scope)
            qe.run_method("candidate", hc.case, hc.gt, TRIALS, 2, 3, answerer_factory=lambda g: made.append(factory(g))
                          or made[-1])
            allowed = {i for ids in hc.scope.by_rule.values() for i in ids}
            assert all(set(e["fact_ids"]) <= allowed for e in made[0].log)
            assert not set(hc.scope.unassigned) & {i for e in made[0].log for i in e["fact_ids"]}


def test_caller_alias_map_is_used_even_when_empty():
    """빈 AliasMap(len 0)을 넘겨도 그 맵을 씀 (호출자가 평가기 쪽에서 별칭을 되돌릴 수 있어야 함)."""
    amap = AliasMap(salt="t")
    hcs, used, _ = load_cases_hygienic(PARSED, only=SUBSET[:1], alias_map=amap)
    assert used is amap and amap.resolve(hcs[0].case.patient_id) == SUBSET[0]


def test_leaking_case_is_blocked_and_counted_as_failure(tmp_path):
    raw = json.loads(qe.DEFAULT_CASES.read_text(encoding="utf-8"))
    case = next(c for c in raw if c["patient_id"] == SUBSET[0])
    leaky = dict(case, text=case["text"] + f"\nReferral note: {SUBSET[0]}.")  # 서술에 원래 ID가 섞인 사례
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([leaky], ensure_ascii=False), encoding="utf-8")
    r = qe.run_evaluation(PARSED, TRIALS, rounds=1, budget=3, cases_path=path, labels_path=tmp_path / "none.csv")
    assert r["hygiene"]["cases_with_visible_issues"] == 1
    assert r["hygiene"]["prompt_leaks"].get("original_id", 0) >= 1
    for m in ("baseline", "candidate"):
        assert r["methods"][m]["failed"] == 1 and r["methods"][m]["ok"] == 0  # 결과에서 빠지지 않고 실패로 집계



def test_cli_protocol_flags(capsys):
    """실행기 CLI: 기본 = 위생 경로, --original-ids = WP1 이전 재현, --legacy-oracle = 별칭 + 기존 답변기."""
    import eval_question_plans as eqp
    expect = {(): ("alias_prompt_guard", "scoped"), ("--legacy-oracle",): ("alias_prompt_guard", "legacy"),
              ("--original-ids",): ("original_ids", "legacy")}
    for flags, (mode, scope) in expect.items():
        eqp.main(["--cases", SUBSET[0], "--rounds", "1", *flags])
        h = json.loads(capsys.readouterr().out)["hygiene"]
        assert (h["mode"], h["oracle_scope"]) == (mode, scope), flags

# ------------------------------------------------------------ B-2 누락 포함 지표를 공개 평가 인터페이스로
ROOT = Path(__file__).resolve().parents[1]
GOLD = {("P1", "T1"): "ELIGIBLE", ("P1", "T2"): "INELIGIBLE", ("P2", "T1"): "ELIGIBLE", ("P2", "T2"): "INELIGIBLE"}


def test_metrics_exposes_cohort_metrics_and_legacy_module_is_same_object():
    from ctrec import cohort_metrics, metrics
    assert metrics.cohort_eligibility_metrics is cohort_metrics.cohort_eligibility_metrics
    assert metrics.cohort_baseline_gate is cohort_metrics.cohort_baseline_gate
    pred = {("P1", "T1"): "ELIGIBLE", ("P1", "T2"): "INELIGIBLE"}
    assert metrics.eligibility_metrics(GOLD, pred)["accuracy"] == 1.0  # 기존 함수는 그대로 (내부 조인)
    m = metrics.cohort_eligibility_metrics(GOLD, pred)
    assert m["accuracy"] == 0.5 and m["missing_predictions"] == 2 and m["denominator"] == "all_gold_pairs"


def test_alias_keyed_predictions_join_only_after_unalias():
    from ctrec.metrics import cohort_eligibility_metrics
    amap = AliasMap(salt="t")
    by_alias = {(amap.alias(p), t): v for (p, t), v in GOLD.items()}
    raw = cohort_eligibility_metrics(GOLD, by_alias)  # 별칭 키를 그대로 넣으면 정답과 연결되지 않음
    assert raw["missing_predictions"] == 4 and raw["accuracy"] == 0.0
    assert raw["extra_predictions"] == 4 and raw["unresolved_alias_predictions"] == 4
    ok = cohort_eligibility_metrics(GOLD, amap.unalias_keys(by_alias))  # 평가기에서 되돌린 뒤
    assert ok["accuracy"] == 1.0 and ok["unresolved_alias_predictions"] == 0 and ok["extra_predictions"] == 0


def _evaluate(tmp_path, *extra):
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, str(ROOT / "eval" / "evaluate.py"), "--labels", str(tmp_path / "labels.csv"),
                           "--outputs", str(tmp_path / "out"), "--trials-dir", str(tmp_path / "trials"), *extra],
                          cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)


def _write_run(tmp_path, preds: dict):
    (tmp_path / "trials").mkdir(parents=True, exist_ok=True)
    rows = ["patient_id,trial_id,label"] + [f"{p},{t},{v}" for (p, t), v in GOLD.items()]
    (tmp_path / "labels.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    by_pid: dict = {}
    for (p, t), v in preds.items():
        by_pid.setdefault(p, {})[t] = {"eligibility": v}
    for p, matches in by_pid.items():
        d = tmp_path / "out" / p
        d.mkdir(parents=True, exist_ok=True)
        (d / "result.json").write_text(json.dumps({"patient_id": p, "matches": matches}), encoding="utf-8")


def test_evaluate_cli_keeps_existing_output_and_adds_cohort(tmp_path):
    from ctrec.metrics import cohort_eligibility_metrics, eligibility_metrics
    pred = {("P1", "T1"): "ELIGIBLE", ("P1", "T2"): "INELIGIBLE"}  # P2 실행 실패로 결과 없음
    _write_run(tmp_path, pred)
    r = _evaluate(tmp_path, "--json", str(tmp_path / "m.json"))
    assert r.returncode == 0, r.stderr
    assert "평가 쌍: 2 (예측 누락 2)" in r.stdout and "전체 정확도 (UNCERTAIN·NOT_EVALUATED 예측=오답): 1.000" in r.stdout
    assert "누락 포함 지표" in r.stdout and "정답 쌍: 4 (예측 누락 2" in r.stdout
    saved = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    legacy = eligibility_metrics(GOLD, pred)
    for k in ("pairs", "missing_predictions", "accuracy", "coverage", "decided_accuracy", "false_removal",
              "premature_match"):
        assert saved[k] == legacy[k], k  # 기존 키·값 불변
    cohort = cohort_eligibility_metrics(GOLD, pred)
    assert saved["cohort"]["accuracy"] == cohort["accuracy"] == 0.5 and saved["cohort"]["pairs"] == 4
    # 기존 기준선 게이트는 그대로(내부 조인) 통과, 누락 포함 게이트는 같은 결과끼리 통과
    assert _evaluate(tmp_path, "--baseline", str(tmp_path / "m.json")).returncode == 0
    assert _evaluate(tmp_path, "--cohort-baseline", str(tmp_path / "m.json")).returncode == 0


def test_evaluate_cli_cohort_gate_catches_dropped_patient(tmp_path):
    full = dict(GOLD)
    _write_run(tmp_path / "base", full)
    assert _evaluate(tmp_path / "base", "--json", str(tmp_path / "base.json")).returncode == 0
    partial = {k: v for k, v in GOLD.items() if k[0] == "P1"}  # 환자 하나가 빠진 실행
    _write_run(tmp_path / "cur", partial)
    legacy_gate = _evaluate(tmp_path / "cur", "--baseline", str(tmp_path / "base.json"))
    assert legacy_gate.returncode == 0  # 기존 게이트는 누락을 못 봄 (감사 P1-6 재현, 동작 유지)
    cohort_gate = _evaluate(tmp_path / "cur", "--cohort-baseline", str(tmp_path / "base.json"))
    assert cohort_gate.returncode == 1 and "불합격" in cohort_gate.stdout
