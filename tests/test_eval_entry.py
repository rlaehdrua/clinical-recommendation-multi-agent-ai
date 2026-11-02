"""WP1 평가 기본 진입점 (B-1 답변기 주입, B-4 별칭·누출 차단 기본 경로, B-2 누락 포함 지표). 실제 API 호출 없음."""

from __future__ import annotations

import json
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

