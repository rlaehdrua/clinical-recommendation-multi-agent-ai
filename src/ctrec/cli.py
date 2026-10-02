"""명령행 인터페이스.

예)
  python -m ctrec run --patient data/patients/synthetic/SYN-001.json --trials data/trials/DEMO-LUNG-001.json
  python -m ctrec run --patient p.json --nct NCT01234567,NCT07654321 --answers interactive
  python -m ctrec batch --patients-dir data/patients/synthetic --trials-dir data/trials --answers simulated
"""

from __future__ import annotations

import argparse
import contextvars
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import DISCLAIMER, config, llm
from .agents.answerers import InteractiveAnswerer, NoAnswerer, SimulatedPatientAnswerer
from .orchestrator import Orchestrator
from .pipeline import PatientCase, Session, load_patients
from .tools import ctgov
from .trace import Tracer


def _load_trials(args) -> dict[str, dict]:
    trials: dict[str, dict] = {}
    for p in args.trials or []:
        rec = ctgov.load_local(Path(p))
        trials[rec["trial_id"]] = rec
    if args.trials_dir:
        for p in sorted(Path(args.trials_dir).glob("*.json")):
            rec = ctgov.load_local(p)
            trials[rec["trial_id"]] = rec
    for nct in filter(None, (args.nct or "").split(",")):
        rec = ctgov.fetch_study(nct.strip())
        trials[rec["trial_id"]] = rec
    return trials


def _answerer(kind: str, cases: list[PatientCase]):
    if kind == "interactive":
        return InteractiveAnswerer()
    if kind == "simulated":
        # 시뮬레이터는 공개 서술 + 숨긴 기록을 모두 알고 답함 (숨긴 기록이 "위와 동일"처럼 서술을 참조해도 동작)
        return SimulatedPatientAnswerer({
            c.patient_id: f"[공개 기록]\n{c.raw_text}\n\n[추가 기록]\n{c.hidden_details}"
            for c in cases if c.hidden_details
        })
    return NoAnswerer()


def _candidates(case: PatientCase, trials: dict[str, dict], topic_map: dict) -> dict[str, dict]:
    """환자별 후보 시험: 환자 파일의 candidate_trials > --topic-map > 전체."""
    ids = case.candidate_trials or topic_map.get(case.patient_id)
    if not ids:
        return dict(trials)
    missing = [t for t in ids if t not in trials]
    if missing:
        print(f"[{case.patient_id}] 후보 시험 {missing}이 로드되지 않았습니다. --trials-dir를 확인하세요.")
    return {t: trials[t] for t in ids if t in trials}


def _run_case(case: PatientCase, trials: dict[str, dict], args, answerer) -> Session:
    out_dir = Path(args.out) / case.patient_id
    tracer = Tracer(out_dir / "trace.jsonl", verbose=not args.quiet,
                    label=case.patient_id if args.workers > 1 else "")
    session = Session(
        case=case, trials=trials, answerer=answerer, tracer=tracer,
        max_rounds=args.max_rounds, allow_search=args.search,
    )
    llm.reset_usage()
    tracer.log("pipeline", "start", message=f"mode={args.mode} model={config.MODEL} trials={len(trials)}")
    if args.mode == "agent":
        Orchestrator(session).run()
    else:
        session.run_fixed()
    session.save(out_dir)
    u = session.usage
    tracer.log("pipeline", "saved", message=(
        f"{out_dir} | calls={u.get('calls', 0)} in={u.get('input_tokens', 0)} out={u.get('output_tokens', 0)} "
        f"cache_read={u.get('cache_read_input_tokens', 0)} cache_write={u.get('cache_creation_input_tokens', 0)}"))
    return session


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trials", nargs="*", help="시험 JSON 파일 경로(들)")
    p.add_argument("--trials-dir", help="시험 JSON 파일 디렉터리")
    p.add_argument("--nct", help="ClinicalTrials.gov NCT ID (쉼표 구분)")
    p.add_argument("--topic-map", help="환자 ID -> 후보 시험 ID 목록 JSON (예: data/trials/topic_map.json)")
    p.add_argument("--search", action="store_true", help="오케스트레이터의 CT.gov 추가 검색 허용 (agent 모드)")
    p.add_argument("--mode", choices=["agent", "fixed"], default="agent")
    p.add_argument("--answers", choices=["interactive", "simulated", "none"], default="none")
    p.add_argument("--max-rounds", type=int, default=config.MAX_CLARIFY_ROUNDS)
    p.add_argument("--out", default=str(config.OUTPUT_DIR))
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--workers", type=int, default=1,
                   help="동시에 처리할 환자 수 (batch 또는 여러 환자 파일). interactive 답변 모드에서는 1로 고정")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ctrec", description="Interactive Clinical Trial Recommendation")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="환자 1명 실행")
    run.add_argument("--patient", required=True)
    run.add_argument("--patient-id", help="여러 환자가 든 파일에서 실행할 환자 ID (예: S001)")
    _add_common(run)

    batch = sub.add_parser("batch", help="디렉터리의 모든 환자 실행")
    batch.add_argument("--patients-dir", required=True)
    _add_common(batch)

    args = parser.parse_args(argv)
    print(DISCLAIMER + "\n")

    trials = _load_trials(args)
    if not trials and not args.search:
        sys.exit("후보 시험이 없습니다. --trials / --trials-dir / --nct 중 하나를 지정하거나 --search를 사용하세요.")

    if args.cmd == "run":
        cases = load_patients(Path(args.patient))
        if args.patient_id:
            cases = [c for c in cases if c.patient_id == args.patient_id]
            if not cases:
                sys.exit(f"{args.patient}에 환자 ID '{args.patient_id}'가 없습니다.")
    else:
        cases = [c for p in sorted(Path(args.patients_dir).iterdir())
                 if p.suffix.lower() in (".json", ".txt") for c in load_patients(p)]
    answerer = _answerer(args.answers, cases)

    topic_map = json.loads(Path(args.topic_map).read_text(encoding="utf-8")) if args.topic_map else {}
    if args.answers == "interactive" and args.workers > 1:
        print("interactive 답변 모드에서는 질문이 섞이지 않도록 --workers 1로 실행합니다.")
        args.workers = 1

    def work(case: PatientCase):
        # 환자마다 새 컨텍스트: 토큰 사용량을 환자별로 집계
        return contextvars.copy_context().run(_run_case, case, _candidates(case, trials, topic_map), args, answerer)

    sessions: list[tuple[PatientCase, Session | None]] = []
    if args.workers > 1 and len(cases) > 1:
        print(f"환자 {len(cases)}명을 최대 {args.workers}명씩 동시에 처리합니다.\n")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [(c, pool.submit(work, c)) for c in cases]
            for case, fut in futures:
                try:
                    sessions.append((case, fut.result()))
                except Exception as e:  # 한 환자의 실패가 전체를 멈추지 않도록
                    print(f"[{case.patient_id}] 실패: {e!r}")
                    sessions.append((case, None))
    else:
        sessions = [(c, work(c)) for c in cases]

    total: dict[str, int] = {}
    for case, session in sessions:
        if session is None:
            continue
        for k, v in session.usage.items():
            total[k] = total.get(k, 0) + v
        if session.recommendation:
            rec = session.recommendation
            print(f"\n[{case.patient_id}] 최종 추천" + (" (공동 1순위)" if len(rec.recommended) > 1 else ""))
            for r in rec.recommended:
                print(f"  ★ {r.trial_id} ({r.eligibility}) - {r.rationale[:100]}")
                for note in r.physician_review:
                    print(f"     [의사 확인 필요] {note[:160]}")
            if rec.recommended and rec.selection_reason:
                print(f"  선정 이유: {rec.selection_reason[:200]}")
            others = [r for r in rec.ranked if r.rank != 1]
            if others:
                print("  차순위 후보: " + ", ".join(f"{r.rank}. {r.trial_id}({r.eligibility})" for r in others))
            if session.recommendation.no_recommendation_reason:
                print(f"  {session.recommendation.no_recommendation_reason}")
        print(f"  -> {Path(args.out) / case.patient_id / 'report.md'}\n")
    failed = [c.patient_id for c, s in sessions if s is None]
    print(f"완료 {len(sessions) - len(failed)}/{len(sessions)}명" + (f" (실패: {', '.join(failed)})" if failed else "")
          + f" | 총 호출 {total.get('calls', 0)}회, 입력 {total.get('input_tokens', 0):,} / 출력 "
          f"{total.get('output_tokens', 0):,} / 캐시 읽기 {total.get('cache_read_input_tokens', 0):,} 토큰")


if __name__ == "__main__":
    main()
