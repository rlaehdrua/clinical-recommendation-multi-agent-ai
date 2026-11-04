"""WP7 오프라인 실행기 어댑터 (ctrec.offline_records): 실제 오프라인 Session -> DecisionRecord + 진단 파일. API 호출 없음."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, offline_records as orc  # noqa: E402
from ctrec.engine import audit  # noqa: E402
from ctrec.engine import record_producer as rp  # noqa: E402
from ctrec.eval_hygiene import AliasMap, load_cases_hygienic  # noqa: E402

PARSED = load_frozen()
ALL = load_trials()
TRIALS = {t: ALL[t] for t in PARSED}
CASES = ["M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1", "M-SYN-DEMO-A-DEMO-LUNG-001-rescue2-3",
         "M-SYN-DEMO-B-DEMO-LUNG-001-cleanup-1"]


def _load(salt="t"):
    # integration/dcvc의 load_cases_hygienic은 빈 AliasMap(len 0)을 버리고 무작위 salt 맵을 새로 만듦 (WP1 PR #1에서 수정,
    # 미병합). salt 고정이 실제로 적용되도록 맵을 미리 채워 넘김 (같은 salt면 같은 별칭이라 결과는 동일)
    amap = AliasMap(salt=salt)
    for c in CASES:
        amap.alias(c)
    return load_cases_hygienic(PARSED, only=CASES, alias_map=amap)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    hcs, _, scanner = _load()
    root = tmp_path_factory.mktemp("wp7")
    return hcs, [orc.record_case(hc, TRIALS, scanner, root / f"c{i}") for i, hc in enumerate(hcs)]


def test_files_written_and_runs_complete(runs):
    _, results = runs
    for r in results:
        assert r.method_status == "ok" and r.unsupported == [] and r.verification == []
        assert (r.run_dir / audit.RECORDS_FILE).exists()
        assert r.complete and r.audit.status == "complete"
        assert len(r.steps) == r.questions_in_session == len(r.rounds) >= 1


def test_load_run_v2_and_issue_reload_match_the_run(runs):
    _, results = runs
    for r in results:
        loaded = audit.load_run_v2(r.run_dir)
        assert loaded.issues == []
        assert [x.step for x in loaded.records] == list(range(1, len(r.rounds) + 1))
        assert [x.action.action_id for x in loaded.records] == [rd.action.action_id for rd in r.rounds]
        issues = rp.load_issues(r.run_dir / rp.ISSUES_FILE) if (r.run_dir / rp.ISSUES_FILE).exists() else []
        assert len(issues) == sum(len(s.issues) for s in r.steps)
        assert all(i.run_id == r.run_id and i.action_id == r.rounds[i.step - 1].action.action_id for i in issues)


def test_records_reflect_the_observed_session(runs):
    _, results = runs
    for r in results:
        for rec, rd in zip(audit.load_run_v2(r.run_dir).records, r.rounds):
            assert rec.fact_version_before == rd.snapshot.version
            assert {c.criterion_key.split(":")[0] for c in rec.criteria if c.reevaluated} == set(rd.rematch)
            assert rec.outcome.cost_charged == rd.cost
            assert rec.outcome.charge_keys == ((rd.charge_key,) if rd.charge_key else ())
            assert rec.facts_added == ()  # 질문별 출처가 없으므로 귀속하지 않음 (임의로 채우지 않음)
            assert rec.policy_version == rd.action.policy_version


def test_decline_marker_and_unattributed_facts_are_preserved_as_issues(runs):
    _, results = runs
    for r in results:
        for s, rd in zip(r.steps, r.rounds):
            if s.record is None:
                continue
            before = {f.fact_id for f in rd.snapshot.facts}
            nxt = r.rounds[r.rounds.index(rd) + 1].snapshot if rd is not r.rounds[-1] else None
            if nxt is None:
                continue
            new = {f.fact_id for f in nxt.facts} - before
            in_issues = {fid for i in s.issues for fid in i.fact_ids}
            assert new == in_issues  # 실제 새 사실은 모두 진단에 있음 (숨기지 않음)
            if s.record.outcome.status == "no_response":
                assert s.record.facts_added == ()


def test_no_original_ids_or_ground_truth_in_files(runs):
    hcs, results = runs
    for hc, r in zip(hcs, results):
        blob = "".join((r.run_dir / n).read_text(encoding="utf-8") for n in (audit.RECORDS_FILE, rp.ISSUES_FILE)
                       if (r.run_dir / n).exists())
        assert hc.gt.case_id not in blob and "rescue" not in blob and "cleanup" not in blob
        assert not any(f'"{h}"' in blob for h in hc.gt.masked_fact_ids)  # 숨긴 사실 ID
        assert not any(t in blob for _, t in hc.gt._hidden_texts)  # 숨긴 사실 원문
        assert r.run_id.startswith("RUN-PA-")


def test_deterministic_with_fixed_salt_and_policy(tmp_path):
    hcs1, _, sc1 = _load("fixed")
    hcs2, _, sc2 = _load("fixed")
    a = orc.record_case(hcs1[1], TRIALS, sc1, tmp_path / "a")
    b = orc.record_case(hcs2[1], TRIALS, sc2, tmp_path / "b")
    assert a.run_id == b.run_id and orc.normalized_files(a.run_dir) == orc.normalized_files(b.run_dir)
    c = orc.record_case(_load("other")[0][1], TRIALS, _load("other")[2], tmp_path / "c")
    assert c.run_id != a.run_id  # salt가 다르면 별칭·run_id·action_id가 다름


def test_violation_step_marks_run_incomplete_and_keeps_every_change(tmp_path, monkeypatch):
    """오프라인 대역에서는 확정 역전이 생기지 않으므로, 실제 관찰의 정책 전이를 상태와 어긋나게 바꿔 실제 생산기가 위반을
    내게 함. 위반 단계는 정상 기록으로 쓰지 않고, 그 단계의 실제 전이·사실은 진단에 모두 남아야 함 (verify_run 정합)."""
    import dataclasses

    _source_level_failures(monkeypatch)
    real = rp.build_step

    def tampered(obs):
        if obs.step == 1 and obs.policy_transitions:
            bad = tuple(p.model_copy(update={"after": rp.PairStatus.VERIFICATION_PENDING})
                        for p in obs.policy_transitions)
            obs = dataclasses.replace(obs, policy_transitions=bad)
        return real(obs)
    monkeypatch.setattr(rp, "build_step", tampered)
    hcs, _, sc = _load()
    hit = 0
    for i, hc in enumerate(hcs):
        r = orc.record_case(hc, TRIALS, sc, tmp_path / f"v{i}")
        if not (r.rounds and r.rounds[0].policy_transitions):
            continue
        hit += 1
        assert r.steps[0].status == "contract_violation" and not r.complete
        assert r.audit.status == "incomplete_contract_violation" and r.summary()["violation_steps"] == [1]
        assert r.verification == []  # 보류됐어도 전이·사실 누락 없음
        written = audit.load_run_v2(r.run_dir).records if (r.run_dir / audit.RECORDS_FILE).exists() else []
        assert 1 not in {x.step for x in written}
        saved = rp.load_issues(r.run_dir / rp.ISSUES_FILE)
        assert any(x.step == 1 and x.code == "policy_transition_disagrees_with_state" for x in saved)
    assert hit >= 1


def _source_level_failures(monkeypatch):
    """무응답을 연락 불가(정보원 단위)로 기록하게 함. WP5 이후 "모름"(no_answer)은 항목 단위 실패라 masked 사례에서
    정보원 소진 -> BLOCKED_FAILURE 정책 전이가 생기지 않으므로, 정책 전이가 있는 실행이 필요한 테스트는 이 경로로 만듦."""
    from ctrec.engine import contracts as C
    monkeypatch.setattr(rp, "NO_ANSWER_ERROR", C.UNREACHABLE)  # offline_records.settle과 생산기가 같은 상수를 읽음


def test_coverage_check_detects_dropped_transition(tmp_path, monkeypatch):
    """누락 검사 자체가 동작하는지: 정책 전이를 빼고 쓴 기록은 pair_transition_missing으로 잡힘."""
    _source_level_failures(monkeypatch)
    hcs, _, sc = _load()
    results = [orc.record_case(hc, TRIALS, sc, tmp_path / f"c{i}") for i, hc in enumerate(hcs)]
    r = next(x for x in results if x.rounds and x.rounds[0].policy_transitions)
    rec = audit.load_run_v2(r.run_dir).records[0]
    stripped = rec.model_copy(update={"pairs": ()})
    problems = orc._coverage_problems(r, {1: stripped}, [])
    assert any(p.startswith("pair_transition_missing:1:") for p in problems)


def test_verify_detects_tampered_or_missing_files(runs, tmp_path):
    _, results = runs
    r = results[1]
    import shutil
    shutil.copytree(r.run_dir, tmp_path / "copy")
    lines = (tmp_path / "copy" / audit.RECORDS_FILE).read_text(encoding="utf-8").splitlines()
    (tmp_path / "copy" / audit.RECORDS_FILE).write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    import dataclasses
    tampered = dataclasses.replace(r, run_dir=tmp_path / "copy", verification=[])
    problems = orc.verify_run(tampered)
    assert "step_coverage_mismatch" in problems and "file_count_mismatch" in problems


def test_default_three_question_setting_unchanged():
    assert orc.QUESTIONS_PER_ROUND == 1
    import ctrec.question_eval as qe
    assert qe.METHODS == ("baseline", "candidate")
    with qe.OfflineEnvironment(3):
        assert config.MAX_QUESTIONS_PER_ROUND == 3  # 기존 평가 경로는 그대로 3문항


def test_summary_file(runs, tmp_path):
    _, results = runs
    out = orc.write_summary(results, tmp_path / "summary.json")
    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert saved == out and out["total"]["runs"] == 3 and out["total"]["complete_runs"] == 3
    assert out["total"]["blocking"] == 0 and out["total"]["verification_problems"] == 0


def test_offline_records_never_builds_an_api_client(tmp_path, monkeypatch):
    """실제 API 클라이언트 생성·호출 경로가 막혀 있음 (OfflineEnvironment가 client/create/plain을 차단, structured는 대역)."""
    from ctrec import llm
    made = []
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda *a, **k: made.append(1) or (_ for _ in ()).throw(
        AssertionError("API client constructed")))
    monkeypatch.setattr(llm, "_client", None)
    hcs, _, sc = _load()
    r = orc.record_case(hcs[0], TRIALS, sc, tmp_path / "noapi")
    assert r.method_status == "ok" and r.complete and made == []
