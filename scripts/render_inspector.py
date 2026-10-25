"""Decision Loop Inspector 실행기 (Phase 10-A). 기록된 실행 폴더를 읽기 전용 정적 HTML로 만듭니다.

    # 오프라인 합성 데모: 대역 LLM으로 Session을 한 번 돌려 실행 폴더를 만들고 HTML까지 (API 호출 없음)
    PYTHONPATH=src python scripts/render_inspector.py demo [--case M-SYN-DEMO-A-DEMO-LUNG-001-rescue2-3]
    # 이미 있는 실행 폴더(CLI outputs/<patient_id>/ 등) -> HTML
    PYTHONPATH=src python scripts/render_inspector.py render outputs/S001 [--out outputs/S001/inspector.html]

- 출력은 기본으로 실행 폴더 안 inspector.html입니다(outputs/는 git에 올라가지 않음). data/·src/에는 쓰지 않습니다.
- 기존 파일은 덮어쓰지 않습니다: 출력 HTML이 이미 있으면 거부(--out으로 새 경로 지정), demo는 비어 있지 않은 폴더를 거부.
- demo는 question_eval.OfflineEnvironment 안에서만 돌며 실제 API 클라이언트·호출을 차단합니다. 고정 기준표(data/parsed)는 읽기만 합니다.
- --synthetic-details는 run_meta.json에 synthetic: true가 있는 실행에서만 허용됩니다(사실 값·질문 문구·답변 표시).
"""

from __future__ import annotations

import argparse
import contextvars
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import config, inspector  # noqa: E402

DEFAULT_CASE = "M-SYN-DEMO-A-DEMO-LUNG-001-rescue2-3"  # 답변 뒤 기준 I6이 문서 확인 필요로 바뀌는 기록이 있는 사례


def run_offline_demo(case_id: str, out_dir: Path, rounds: int = 2, budget: int = 3) -> Path:
    """합성 사례 1건을 오프라인 대역으로 실행해 CLI와 같은 형식의 실행 폴더를 남김 (question_eval.run_method와 같은 배선)."""
    run_offline_session(case_id, out_dir, rounds, budget)
    return Path(out_dir)


def run_offline_session(case_id: str, out_dir: Path, rounds: int = 2, budget: int = 3,
                        extra_trials: dict[str, dict] | None = None):
    """run_offline_demo의 실행 본체. 실행이 끝난 Session을 돌려줌 (Inspector 검증용).

    extra_trials: 사례 후보에 더할 시험 (예: 고정 기준표에 없는 시험 -> 파이프라인이 NOT_EVALUATED로 기록하는 경로 확인).
    rounds=0이면 질문 없이 끝나는 실행."""
    from bench_incremental import load_frozen
    from decision_scenarios import load_trials

    from ctrec import pipeline
    from ctrec import question_eval as qe
    from ctrec.decision_loop import DecisionTracker, LoopConfig
    from ctrec.decision_state import Budget
    from ctrec.pipeline import PatientCase, Session
    from ctrec.trace import Tracer

    out_dir = Path(out_dir)
    if out_dir.is_symlink() or (out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir()))):
        raise inspector.InspectorError(f"이미 내용이 있는 위치에는 실행 기록을 만들지 않음 (덮어쓰기 방지): {out_dir}")
    parsed = load_frozen()
    cases = qe.load_cases(parsed, only=[case_id])
    if not cases:
        raise SystemExit(f"사례를 찾을 수 없음: {case_id}")
    case, gt = cases[0]
    all_trials = load_trials()
    c = PatientCase(patient_id=case.patient_id, raw_text=case.raw_text, input_source=case.input_source,
                    candidate_trials=list(case.candidate_trials or []))
    out_dir.mkdir(parents=True, exist_ok=True)
    ref: dict = {}
    with qe.OfflineEnvironment(budget):
        trials = {**{t: all_trials[t] for t in gt.candidate_trials}, **(extra_trials or {})}
        s = Session(case=c, trials=trials, answerer=qe.OracleAnswerer(gt),
                    tracer=Tracer(out_dir / "trace.jsonl", verbose=False), max_rounds=rounds,
                    decision_loop="diagnostic", dependency_diagnostic=True, fact_store_dir=out_dir / "facts")
        s._decision_tracker = DecisionTracker(LoopConfig(budget=Budget(max_questions=budget)))
        ref["session"] = s
        if gt.setup_verified_fields:  # run_method와 같은 평가용 상태 구성 (확정 불충족 합성 사례)
            real_profile = s.profile_patient

            def profile_and_verify():
                out = real_profile()
                for f in s.fact_store.facts(c.patient_id):
                    if f.field in gt.setup_verified_fields and f.verification_status != "document_verified":
                        s.fact_store.set_verification(f.fact_id, "document_verified", reason="합성 평가 상태",
                                                      by="question_eval", document_reference="SYNTHETIC")
                return out
            s.profile_patient = profile_and_verify

        def go():
            with pipeline.question_generator.use_generator(qe.candidate_generator(ref)):
                s.run_fixed()
        contextvars.copy_context().run(go)
        s.save(out_dir)
        if s.fact_store_dir is not None:  # 마지막 라운드 사실까지 저장 (병행 기록이 이미 저장하지만 확인 차원)
            s.fact_store.root = s.fact_store_dir
            s.fact_store.save()
    (out_dir / "run_meta.json").write_text(json.dumps({
        "llm_backend": "offline_stub", "synthetic": True, "case_id": case_id, "runner": "render_inspector.demo",
        # 백엔드 CLI run_meta 계약과 같은 records: 이 실행이 실제로 남긴 기록 (사실은 실행 폴더 facts/, 진단 둘 다 켬)
        "records": {"fact_store": "run_dir", "decision_diagnostic": True, "dependency_diagnostic": True},
        "note": "question_eval.OfflineEnvironment: 환자 정보 정규식 대역 + LLM 기준 '판정 불가' 대역 + 정답 답변 시뮬레이터",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="오프라인 합성 실행 + HTML")
    d.add_argument("--case", default=DEFAULT_CASE)
    d.add_argument("--out-dir", type=Path)
    d.add_argument("--rounds", type=int, default=2)
    d.add_argument("--budget", type=int, default=3)
    d.add_argument("--synthetic-details", action="store_true")
    r = sub.add_parser("render", help="실행 폴더 -> HTML")
    r.add_argument("run_dir", type=Path)
    r.add_argument("--out", type=Path)
    r.add_argument("--synthetic-details", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "demo":
            run_dir = run_offline_demo(args.case, args.out_dir or config.OUTPUT_DIR / "inspector" / args.case,
                                       args.rounds, args.budget)
            out = run_dir / "inspector.html"
        else:
            run_dir, out = args.run_dir, args.out or args.run_dir / "inspector.html"
            if Path(out).exists():  # 실행 기록을 읽기 전에 먼저 거부
                raise inspector.InspectorError(f"이미 있는 파일은 덮어쓰지 않음: {out} (다른 --out 경로를 지정하세요)")
        view = inspector.build_view(inspector.load_run(run_dir), synthetic_details=args.synthetic_details)
        path = inspector.write_html(view, out)
    except inspector.InspectorError as e:
        print(f"거부: {e}", file=sys.stderr)
        raise SystemExit(2)
    print(f"run dir : {run_dir}")
    print(f"inspector: {path}")
    print("기록에 없는 데이터:")
    for g in view["gaps"]:
        print(f"  - {g}")


if __name__ == "__main__":
    main()
