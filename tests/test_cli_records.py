"""CLI 실행 기록 계약 (Phase 10-C-1): 진단·사실 저장 옵션, run_meta.json, 출력 폴더 재사용·경로 사전 검증.

API 호출 없음: question_eval.OfflineEnvironment(실제 클라이언트 차단, 결정적 대역) + 기존 대역 질문 생성기로 cli.main을 그대로 실행.
고정 기준표(data/parsed)는 읽기만 합니다. 기존 기록은 지우거나 옮기지 않고, 섞이거나 덮어쓸 위험이 있으면 실행을 거부합니다.
구조화된 평가 실패 이벤트(not_evaluated)는 보호 파일(pipeline.py) 정책으로 보류했으며,
여기서는 평가 실패 시 기존 기록(NOT_EVALUATED 결과·error 이벤트)이 그대로인지만 확인합니다.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from ctrec import cli, config, pipeline
from ctrec import question_eval as qe

DEMO = config.TRIALS_DIR / "DEMO-LUNG-001.json"
TEXT = ("62-year-old female with stage IV lung adenocarcinoma. ECOG performance status 1. "
        "Platelets 185 x10^9/L on 2026-09-10. Has hypertension and takes amlodipine.")
SENTENCE = "Has hypertension and takes amlodipine"


def _patient(tmp_path: Path, pid: str = "CLI-T1", trials=("DEMO-LUNG-001",)) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "patient_input.json"  # 입력 파일 이름은 고정 (환자 ID를 경로에 쓰지 않음)
    p.write_text(json.dumps({"patient_id": pid, "text": TEXT, "candidate_trials": list(trials)}), encoding="utf-8")
    return p


def _fake_trial(tmp_path: Path, tid: str = "NCT99999998") -> Path:
    """고정 기준표에 없는 시험 (DEMO 시험 사본, ID만 바꿈) -> 파이프라인이 기준 파싱 단계에서 NOT_EVALUATED로 기록."""
    rec = json.loads(DEMO.read_text(encoding="utf-8"))
    rec["trial_id"] = tid
    p = tmp_path / f"{tid}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def _run(tmp_path: Path, *extra: str, trials: list[Path] | None = None, pid: str = "CLI-T1", tids=("DEMO-LUNG-001",),
         out: Path | None = None):
    out = out or tmp_path / "out"
    argv = ["run", "--patient", str(_patient(tmp_path, pid, tids)), "--trials", *map(str, trials or [DEMO]),
            "--mode", "fixed", "--candidates", "given", "--answers", "none", "--max-rounds", "1", "--out", str(out), *extra]
    buf = io.StringIO()
    with qe.OfflineEnvironment(2), pipeline.question_generator.use_generator(qe.baseline_generator), redirect_stdout(buf):
        cli.main(argv)
    return out / pid, buf.getvalue()


def _meta(d: Path) -> dict:
    return json.loads((d / "run_meta.json").read_text(encoding="utf-8"))


def _trace(d: Path) -> list[dict]:
    return [json.loads(x) for x in (d / "trace.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def _judgements(d: Path) -> dict:
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    return {"matches": {t: (m["eligibility"], [(a["rule_id"], a["passes"], a["holds"]) for a in m["assessments"]])
                        for t, m in r["matches"].items()},
            "qa": [(q["round"], q["question_id"], q["target"], q["answer"]) for q in r["qa_log"]],
            "history": r["eligibility_history"]}


# ---------------------------------------------------------------- 1. 옵션 조합

def test_default_run_keeps_existing_behaviour(tmp_path):
    d, _ = _run(tmp_path)
    result = json.loads((d / "result.json").read_text(encoding="utf-8"))
    assert not (d / "facts").exists()  # 기본: 사실 파일 저장 안 함
    assert "decision_diagnostic" not in result and "dependency_diagnostic" not in result
    assert {p.name for p in d.iterdir()} == {"result.json", "trace.jsonl", "run_meta.json"}  # WRITE_REPORT=False 대역
    assert result["qa_log"]  # 질문 라운드가 실제로 돈 실행 (비교 대상이 비어 있지 않음)


@pytest.mark.parametrize("flags,facts,decision,dependency", [
    (["--fact-store-dir"], True, False, False),
    (["--decision-diagnostic"], False, True, False),
    (["--dependency-diagnostic"], False, False, True),
    (["--fact-store-dir", "--decision-diagnostic", "--dependency-diagnostic"], True, True, True),
])
def test_option_combinations(tmp_path, flags, facts, decision, dependency):
    d, _ = _run(tmp_path, *flags)
    result = json.loads((d / "result.json").read_text(encoding="utf-8"))
    assert (d / "facts" / "CLI-T1.json").is_file() == facts
    assert ("decision_diagnostic" in result) == decision and ("dependency_diagnostic" in result) == dependency
    assert _meta(d)["records"] == {"fact_store": "run_dir" if facts else None,
                                   "decision_diagnostic": decision, "dependency_diagnostic": dependency}
    if facts:
        saved = json.loads((d / "facts" / "CLI-T1.json").read_text(encoding="utf-8"))
        assert saved["patient_id"] == "CLI-T1" and saved["facts"]  # FactStore.save 형식 그대로
    if decision:
        assert all(e["status"] == "ok" for e in result["decision_diagnostic"])


def test_external_fact_store_dir(tmp_path):
    ext = tmp_path / "facts_ext"
    d, _ = _run(tmp_path, "--fact-store-dir", str(ext))
    assert (ext / "CLI-T1.json").is_file() and not (d / "facts").exists()
    assert _meta(d)["records"]["fact_store"] == "external"


def test_options_do_not_change_judgements(tmp_path):
    base, _ = _run(tmp_path / "a")
    full, _ = _run(tmp_path / "b", "--fact-store-dir", "--decision-diagnostic", "--dependency-diagnostic")
    assert _judgements(base) == _judgements(full)


def test_options_add_no_patient_text_to_console(tmp_path):
    _, plain = _run(tmp_path / "a")
    _, full = _run(tmp_path / "b", "--fact-store-dir", "--decision-diagnostic", "--dependency-diagnostic")
    new_lines = set(full.splitlines()) - set(plain.splitlines())
    assert not [ln for ln in new_lines if SENTENCE in ln or "amlodipine" in ln or "185" in ln]


# ---------------------------------------------------------------- 2. 출력 폴더 재사용·경로 사전 검증 (실행 거부, 기존 파일 불변)

def _snapshot(*roots: Path) -> dict:
    return {str(p): p.read_bytes() for r in roots if r.exists() for p in sorted(r.rglob("*")) if p.is_file()}


def _refused(tmp_path: Path, *extra: str, **kw) -> str:
    with pytest.raises(SystemExit) as e:
        _run(tmp_path, *extra, **kw)
    msg = str(e.value.code)
    assert msg.startswith("실행하지 않았습니다")
    return msg


def test_new_out_dir_runs_with_and_without_fact_option(tmp_path):
    a, _ = _run(tmp_path / "a")
    b, _ = _run(tmp_path / "b", "--fact-store-dir")
    assert (a / "result.json").is_file() and (b / "facts" / "CLI-T1.json").is_file()


def test_rerun_same_out_dir_without_facts_is_allowed(tmp_path):
    """사실 파일이 없는 출력 폴더 재사용은 기존 CLI 동작 그대로 허용 (result.json·trace.jsonl을 새로 씀)."""
    d, _ = _run(tmp_path)
    first = _meta(d)["run_id"]
    _run(tmp_path)
    assert _meta(d)["run_id"] != first


@pytest.mark.parametrize("second", [[], ["--fact-store-dir"], ["--decision-diagnostic"]])
def test_rerun_over_previous_facts_is_refused_and_files_unchanged(tmp_path, second):
    """이전 실행의 사실 파일이 있으면 옵션과 상관없이 거부 (옵션이 없으면 혼입, 있으면 덮어쓰기). 아무것도 쓰지 않음."""
    d, _ = _run(tmp_path, "--fact-store-dir")
    before = _snapshot(d)
    msg = _refused(tmp_path, *second)
    assert _snapshot(d) == before  # result.json·trace.jsonl·run_meta.json·facts 모두 그대로
    assert not (d / "facts_archive").exists() and "새 --out" in msg and "CLI-T1" not in msg


def test_rerun_with_external_dir_over_previous_run_facts_is_refused(tmp_path):
    d, _ = _run(tmp_path, "--fact-store-dir")
    before = _snapshot(d)
    _refused(tmp_path, "--fact-store-dir", str(tmp_path / "ext"))
    assert _snapshot(d) == before and not (tmp_path / "ext").exists()


def test_external_dir_existing_file_not_overwritten(tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "CLI-T1.json").write_text('{"keep": 1}', encoding="utf-8")
    (ext / "OTHER.json").write_text('{"other": 1}', encoding="utf-8")
    before = _snapshot(ext)
    _refused(tmp_path, "--fact-store-dir", str(ext))
    assert _snapshot(ext) == before and not (tmp_path / "out").exists()  # 결과 파일도 쓰기 전에 거부


def test_external_dir_with_other_patients_only_is_allowed(tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "OTHER.json").write_text('{"other": 1}', encoding="utf-8")
    _run(tmp_path, "--fact-store-dir", str(ext))
    assert (ext / "OTHER.json").read_text(encoding="utf-8") == '{"other": 1}' and (ext / "CLI-T1.json").is_file()


def test_fact_store_dir_inside_data_or_src_refused(tmp_path):
    for target in (config.DATA_DIR / "facts_probe", config.ROOT / "src" / "facts_probe"):
        _refused(tmp_path, "--fact-store-dir", str(target))
        assert not target.exists()


@pytest.mark.parametrize("pid", ["x/../../victim", "../victim", "..victim", "a\\b", "/abs", "C:victim", ".hidden",
                                 "has space", "-dash"])
def test_unsafe_patient_id_refused_before_writing(tmp_path, pid):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "victim.json").write_text('{"victim": 1}', encoding="utf-8")
    before = _snapshot(tmp_path)
    msg = _refused(tmp_path / "in", pid=pid, out=tmp_path / "out")
    after = {k: v for k, v in _snapshot(tmp_path).items() if "\\in\\" not in k and "/in/" not in k}
    assert after == {k: v for k, v in before.items()}  # 피해 폴더·출력 폴더에 아무것도 쓰거나 옮기지 않음
    assert pid not in msg


def test_duplicate_patient_ids_refused_when_saving_facts(tmp_path):
    p = tmp_path / "dup.json"
    p.write_text(json.dumps([{"patient_id": "CLI-T1", "text": TEXT, "candidate_trials": ["DEMO-LUNG-001"]}] * 2),
                 encoding="utf-8")
    argv = ["run", "--patient", str(p), "--trials", str(DEMO), "--mode", "fixed", "--candidates", "given",
            "--max-rounds", "0", "--out", str(tmp_path / "out"), "--fact-store-dir"]
    with qe.OfflineEnvironment(2), redirect_stdout(io.StringIO()), pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert "같은 환자 ID" in str(e.value.code) and not (tmp_path / "out").exists()


def _junction(link: Path, target: Path) -> None:
    import subprocess
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows 정션 전용")
    link.parent.mkdir(parents=True, exist_ok=True)
    if subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True).returncode:
        pytest.skip("정션을 만들 수 없음")


def test_facts_junction_to_other_run_refused(tmp_path):
    other = tmp_path / "other_run" / "facts"
    other.mkdir(parents=True)
    (other / "CLI-T1.json").write_text('{"other_run": 1}', encoding="utf-8")
    _junction(tmp_path / "out" / "CLI-T1" / "facts", other)
    before = _snapshot(tmp_path / "other_run")
    msg = _refused(tmp_path)
    assert _snapshot(tmp_path / "other_run") == before and "다른 위치를 가리킵니다" in msg
    assert str(other) not in msg  # 링크 대상 경로는 메시지에 넣지 않음


def test_run_dir_junction_outside_out_refused(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "result.json").write_text('{"keep": 1}', encoding="utf-8")
    _junction(tmp_path / "out" / "CLI-T1", outside)
    before = _snapshot(outside)
    msg = _refused(tmp_path)
    assert _snapshot(outside) == before and "--out 밖" in msg


def test_facts_symlink_to_other_run_refused(tmp_path):
    other = tmp_path / "other_run"
    other.mkdir()
    (other / "CLI-T1.json").write_text('{"other_run": 1}', encoding="utf-8")
    link = tmp_path / "out" / "CLI-T1" / "facts"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(other, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없음")
    before = _snapshot(other)
    _refused(tmp_path)
    assert _snapshot(other) == before


# ---------------------------------------------------------------- 3. run_meta.json: 설정값과 실제 사실 구분

def test_run_meta_does_not_claim_real_calls(tmp_path):
    d, _ = _run(tmp_path)
    meta = _meta(d)
    assert meta["llm_backend"] == "cli_sdk_path"  # 실행 경로 (실제 호출을 뜻하는 "real"이 아님)
    assert meta["llm_calls_observed"] is None and meta["llm_responses_with_usage"] is None  # 대역 실행: 확인 못 함 (0·false 아님)
    assert meta["requested_model"] == config.MODEL and meta["called_models"] == "UNKNOWN"
    assert meta["mode"] == "fixed" and meta["candidates"] == "given" and meta["synthetic"] is None
    assert json.loads((d / "result.json").read_text(encoding="utf-8"))["usage"] == {}  # result.json과 모순 없음
    assert TEXT[:20] not in json.dumps(meta) and "CLI-T1" not in json.dumps(meta)


def test_run_meta_records_observed_calls_when_usage_recorded(tmp_path, monkeypatch):
    real_save = pipeline.Session.save

    def save(self, out_dir):  # 사용량 집계가 있었던 실행을 흉내 (Session.save가 usage_snapshot으로 채우는 값)
        real_save(self, out_dir)
        self.usage = {"calls": 4, "input_tokens": 100}
    monkeypatch.setattr(pipeline.Session, "save", save)
    d, _ = _run(tmp_path)
    meta = _meta(d)
    assert meta["llm_responses_with_usage"] == 4 and meta["llm_calls_observed"] is True
    assert meta["called_models"] == "UNKNOWN"  # 호출이 있어도 모델 이름은 이 경로에서 확인 못 함


# ---------------------------------------------------------------- 4. 평가 실패: 기존 기록 그대로 (새 이벤트 없음)

def test_parse_failure_keeps_existing_records(tmp_path):
    fake = _fake_trial(tmp_path)
    d, _ = _run(tmp_path, trials=[DEMO, fake], tids=("DEMO-LUNG-001", "NCT99999998"))
    result = json.loads((d / "result.json").read_text(encoding="utf-8"))
    assert result["matches"]["NCT99999998"]["eligibility"] == "NOT_EVALUATED"
    assert any(t["event"] == "error" and t.get("item") == "NCT99999998" for t in _trace(d))
    assert not [t for t in _trace(d) if t["event"] == "not_evaluated"]  # 보류된 이벤트는 기록하지 않음
    assert _meta(d)["records"]["fact_store"] is None


def test_match_failure_keeps_existing_records(tmp_path, monkeypatch):
    real = pipeline.matcher.match_trial

    def boom(profile, parsed, trial, **kw):
        if trial["trial_id"] == "DEMO-LUNG-001":
            raise RuntimeError("secret detail")
        return real(profile, parsed, trial, **kw)
    monkeypatch.setattr(pipeline.matcher, "match_trial", boom)
    d, _ = _run(tmp_path)
    result = json.loads((d / "result.json").read_text(encoding="utf-8"))
    assert result["matches"]["DEMO-LUNG-001"]["eligibility"] == "NOT_EVALUATED"
    assert (d / "run_meta.json").is_file() and "secret detail" not in (d / "run_meta.json").read_text(encoding="utf-8")
