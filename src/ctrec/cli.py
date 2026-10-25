"""명령행 인터페이스.

예)
  python -m ctrec run --patient data/patients/synthetic/SYN-DEMO-A.json --trials data/trials/DEMO-LUNG-001.json --demo
  python -m ctrec run --patient p.json --nct NCT01234567,NCT07654321 --answers interactive
  python -m ctrec batch --patients-dir data/patients/synthetic --trials-dir data/trials --answers simulated
"""

from __future__ import annotations

import argparse
import contextvars
import json
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import DISCLAIMER, config, llm
from .agents.answerers import (FactRevealAnswerer, InteractiveAnswerer, NoAnswerer, RoutingAnswerer,
                               SimulatedPatientAnswerer)
from .agents.recommender import screen_pool
from .orchestrator import Orchestrator
from .pipeline import PatientCase, Session, load_patients
from .status import MATCH_NOTE
from .status import label as status_label
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
        legacy = SimulatedPatientAnswerer({
            c.patient_id: f"[공개 기록]\n{c.raw_text}\n\n[추가 기록]\n{c.hidden_details}"
            for c in cases if c.hidden_details
        })
        # 사실 목록이 있는 환자(정보 가리기 평가셋)는 질문이 묻는 사실만 공개하는 시뮬레이터 사용
        facts = {c.patient_id: c.oracle_facts for c in cases if c.oracle_facts}
        if not facts:
            return legacy
        fact_answerer = FactRevealAnswerer(facts)
        return RoutingAnswerer({pid: fact_answerer for pid in facts}, legacy)
    return NoAnswerer()


def _candidates(case: PatientCase, trials: dict[str, dict], topic_map: dict, mode: str = "given") -> dict[str, dict]:
    """환자별 후보 시험.

    screen: 불러온 시험 전체(가상 데모 시험은 --demo일 때만)를 풀로 넘기고, 후보 추림 에이전트가 고름 (candidate_trials·topic-map 무시)
    given : 환자 파일의 candidate_trials > --topic-map > 전체 (사람이 정한 후보, 이전 방식)
    """
    if mode == "screen":
        return screen_pool(trials)
    ids = case.candidate_trials or topic_map.get(case.patient_id)
    if not ids:
        return dict(trials)
    missing = [t for t in ids if t not in trials]
    if missing:
        print(f"[{case.patient_id}] 후보 시험 {missing}이 로드되지 않았습니다. --trials-dir를 확인하세요.")
    return {t: trials[t] for t in ids if t in trials}


# --fact-store-dir를 값 없이 주면 실행 폴더 안 facts/ (Decision Loop Inspector가 읽는 위치)
FACTS_IN_RUN_DIR = "<run_dir>/facts"


def _fact_store_dir(args, out_dir: Path) -> Path | None:
    v = getattr(args, "fact_store_dir", None)
    if v is None:
        return None
    return out_dir / "facts" if v == FACTS_IN_RUN_DIR else Path(v)


# 환자 ID는 출력 폴더·사실 파일 이름이 됨: 사실 저장소(FactStore)와 같은 규칙 + '..' 금지 (경로 구분자·절대 경로 불가)
_SAFE_PATIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


class RecordConflict(Exception):
    """실행 기록 사전 검증 실패 (아무 파일도 쓰기 전에 실행을 거부)."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _check_records(case: PatientCase, args, index: int) -> None:
    """실행 전 사전 검증. 이전 실행의 사실 파일이 이번 실행 기록처럼 읽히거나(Decision Loop Inspector는 실행 폴더의
    facts/<환자>.json을 이번 실행 기록으로 읽음), 기존 파일을 덮어쓰거나, 실행 폴더 밖으로 이어질 수 있으면 거부.
    기존 파일은 읽기만 하고 지우거나 옮기지 않음. 오류 메시지에는 환자 ID 원문·링크 대상 경로를 넣지 않음."""
    pid = case.patient_id
    who = f"환자 #{index + 1}"
    if not isinstance(pid, str) or not _SAFE_PATIENT_ID.match(pid) or ".." in pid:
        raise RecordConflict(f"{who}: 환자 ID를 출력 폴더 이름으로 안전하게 쓸 수 없는 형식입니다 "
                             "(영문·숫자로 시작하고 영문·숫자·_ . -만, '..' 불가).")
    root = Path(args.out)
    out_dir = root / pid
    if os.path.lexists(out_dir) and not _inside(out_dir, root):
        raise RecordConflict(f"{who}: 실행 폴더가 --out 밖을 가리킵니다 (링크·정션). 새 --out 경로를 지정하세요.")
    run_facts = out_dir / "facts"
    if os.path.lexists(run_facts) and run_facts.resolve() != out_dir.resolve() / "facts":
        raise RecordConflict(f"{who}: 실행 폴더의 facts가 다른 위치를 가리킵니다 (링크·정션). 새 --out 경로를 지정하세요.")
    if os.path.lexists(run_facts / f"{pid}.json"):
        raise RecordConflict(f"{who}: --out {root}의 실행 폴더에 이전 실행의 사실 파일(facts/)이 있어 이번 실행 기록과 "
                             "섞일 수 있습니다. 기존 파일은 그대로 두었습니다. 새 --out 경로를 지정하세요.")
    fact_dir = _fact_store_dir(args, out_dir)
    if fact_dir is not None and fact_dir.resolve() != (out_dir / "facts").resolve():
        for protected in (config.DATA_DIR, config.ROOT / "src"):
            if _inside(fact_dir, protected):
                raise RecordConflict(f"{who}: --fact-store-dir를 데이터·소스 폴더 안으로 지정할 수 없습니다.")
        if os.path.lexists(fact_dir / f"{pid}.json"):
            raise RecordConflict(f"{who}: --fact-store-dir {fact_dir}에 이 환자의 사실 파일이 이미 있습니다. "
                                 "덮어쓰지 않습니다. 다른 경로를 지정하세요.")


def _write_run_meta(out_dir: Path, session: Session, args, fact_dir: Path | None, run_id: str) -> None:
    """실행 메타데이터 (Decision Loop Inspector 입력 계약의 run_meta.json). 환자 정보·경로 원문은 넣지 않음.

    설정값과 실제 사실을 나눔:
    - llm_backend: 이 실행이 지나간 경로(CLI -> SDK 클라이언트 경로)일 뿐 실제 API 호출을 뜻하지 않음 ("real"이 아님).
      테스트·오프라인 대역으로 바꿔 실행해도 같은 값이므로, 실제 호출 여부는 아래 관찰 기록으로만 판단
    - llm_calls_observed: 사용량이 기록된 응답이 있었으면 true. 없으면 null(확인 못 함 - false나 0으로 단정하지 않음)
    - synthetic: CLI는 입력이 합성 환자인지 알 수 없음(환자 파일의 synthetic 표시는 파이프라인에 넘어오지 않음) -> null
    - requested_model: 설정값. called_models: 이 경로는 응답의 모델 이름을 기록하지 않으므로 "UNKNOWN"
    """
    calls = session.usage.get("calls")
    calls = calls if isinstance(calls, int) and not isinstance(calls, bool) else None
    meta = {
        "format_version": 1, "written_by": "ctrec.cli", "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "llm_backend": "cli_sdk_path", "llm_calls_observed": True if calls else None,
        "synthetic": None, "mode": args.mode, "candidates": args.candidates,
        "requested_model": config.MODEL, "called_models": "UNKNOWN",
        "llm_responses_with_usage": calls,
        "records": {
            # 이번 실행에서 사실 저장을 켰는지와 위치 (run_dir = 실행 폴더의 facts/, external = 다른 폴더, null = 저장 안 함)
            "fact_store": None if fact_dir is None else
            ("run_dir" if fact_dir.resolve() == (out_dir / "facts").resolve() else "external"),
            "decision_diagnostic": session.decision_loop == "diagnostic",
            "dependency_diagnostic": session.dependency_diagnostic,
        },
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _run_case(case: PatientCase, trials: dict[str, dict], args, answerer) -> Session:
    out_dir = Path(args.out) / case.patient_id
    run_id = f"R-{uuid.uuid4().hex[:12]}"
    tracer = Tracer(out_dir / "trace.jsonl", verbose=not args.quiet,
                    label=case.patient_id if args.workers > 1 else "")
    fact_dir = _fact_store_dir(args, out_dir)
    from .contract_session import session_class  # CTREC_SESSION=contract -> ContractSession (기본: 기존 Session)
    session = session_class()(
        case=case, trials=trials, answerer=answerer, tracer=tracer,
        max_rounds=args.max_rounds, allow_search=args.search, question_budget=args.question_budget,
        screen_trials=args.candidates == "screen" and len(trials) > args.max_candidates,
        max_candidates=args.max_candidates,
        # 진단·사실 기록은 옵션을 준 실행에만 (기본 동작 그대로: 저장 안 함, 진단 꺼짐)
        fact_store_dir=fact_dir,
        decision_loop="diagnostic" if getattr(args, "decision_diagnostic", False) else "off",
        dependency_diagnostic=bool(getattr(args, "dependency_diagnostic", False)),
    )
    llm.reset_usage()
    tracer.log("pipeline", "start", message=f"mode={args.mode} model={config.MODEL} trials={len(trials)}")
    if args.mode == "agent":
        Orchestrator(session).run()
    else:
        session.run_fixed()
    session.save(out_dir)
    _write_run_meta(out_dir, session, args, fact_dir, run_id)
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
    p.add_argument("--candidates", choices=["screen", "given"], default="screen",
                   help="screen: 불러온 시험 전체에서 후보 추림 에이전트가 고름 / given: 환자 파일·topic-map에 정한 후보만 평가")
    p.add_argument("--max-candidates", type=int, default=config.SCREEN_MAX_CANDIDATES,
                   help="후보 추림 후 자세히 평가할 최대 시험 수")
    p.add_argument("--question-budget", type=int, default=None,
                   help="환자 1명당 전체 라운드 합산 최대 질문 수 (질문 예산 실험용, 기본: 라운드당 한도만 적용)")
    p.add_argument("--out", default=str(config.OUTPUT_DIR))
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--workers", type=int, default=1,
                   help="동시에 처리할 환자 수 (batch 또는 여러 환자 파일). interactive 답변 모드에서는 1로 고정")
    p.add_argument("--input-source", choices=["clinical_record", "self_report", "unknown"], default=None,
                   help="환자 원문의 출처를 모든 환자에 지정 (환자 파일의 input_source보다 우선). "
                        "지정하지 않고 파일에도 없으면 unknown: 진료 기록이 필요한 기준은 UNVERIFIED")
    p.add_argument("--fact-store-dir", nargs="?", const=FACTS_IN_RUN_DIR, default=None,
                   help="환자 사실 저장소를 파일로 저장 (기본: 저장 안 함). 값 없이 주면 실행 폴더의 facts/, "
                        "경로를 주면 그 폴더. 파일에 환자 원문 인용이 들어가므로 보관·공유에 주의")
    p.add_argument("--decision-diagnostic", action="store_true",
                   help="Decision Loop 진단을 result.json에 기록 (판정·질문·추천에는 쓰지 않음, 기본 꺼짐)")
    p.add_argument("--dependency-diagnostic", action="store_true",
                   help="의존 그래프·부분 재평가 진단을 result.json에 기록 (판정·질문·추천에는 쓰지 않음, 기본 꺼짐)")
    p.add_argument("--demo", action="store_true",
                   help="데모 모드: 가상 데모 시험(DEMO-LUNG-001 등)도 추천에 표시 (기본은 판정만 하고 추천에서 제외)")


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
    if args.demo:
        config.DEMO_MODE = True

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
    if args.input_source:
        for c in cases:
            c.input_source = args.input_source
    try:
        for i, c in enumerate(cases):
            _check_records(c, args, i)
        ids = [c.patient_id for c in cases]
        if args.fact_store_dir is not None and len(ids) != len(set(ids)):  # 같은 실행 안에서 사실 파일을 서로 덮어씀
            raise RecordConflict("같은 환자 ID가 여러 번 있어 사실 파일이 서로 덮어써집니다. 환자 ID를 확인하세요.")
    except RecordConflict as e:
        sys.exit(f"실행하지 않았습니다: {e}")
    answerer = _answerer(args.answers, cases)

    topic_map = json.loads(Path(args.topic_map).read_text(encoding="utf-8")) if args.topic_map else {}
    if args.answers == "interactive" and args.workers > 1:
        print("interactive 답변 모드에서는 질문이 섞이지 않도록 --workers 1로 실행합니다.")
        args.workers = 1

    def work(case: PatientCase):
        # 환자마다 새 컨텍스트: 토큰 사용량을 환자별로 집계
        return contextvars.copy_context().run(_run_case, case, _candidates(case, trials, topic_map, args.candidates), args, answerer)

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
            if rec.recommended:
                print(f"\n[{case.patient_id}] 추천 그룹: {status_label(rec.recommended[0].match_status)}")
            for r in rec.recommended:
                print(f"  ★ {r.trial_id} ({status_label(r.match_status)}) - {r.rationale[:100]}")
                for note in r.physician_review:
                    print(f"     [의사 확인 필요] {note[:160]}")
            if rec.recommended:
                print(f"  {MATCH_NOTE}\n  {rec.selection_reason}")
            others = [r for r in rec.ranked if r not in rec.recommended]
            if others:
                print("  다른 후보(표시 순서): " + ", ".join(f"{r.rank}. {r.trial_id}({status_label(r.match_status)})"
                                                    for r in others))
            if session.recommendation.no_recommendation_reason:
                print(f"  {session.recommendation.no_recommendation_reason}")
        print(f"  -> {Path(args.out) / case.patient_id / 'report.md'}\n")
    failed = [c.patient_id for c, s in sessions if s is None]
    print(f"완료 {len(sessions) - len(failed)}/{len(sessions)}명" + (f" (실패: {', '.join(failed)})" if failed else "")
          + f" | 총 호출 {total.get('calls', 0)}회, 입력 {total.get('input_tokens', 0):,} / 출력 "
          f"{total.get('output_tokens', 0):,} / 캐시 읽기 {total.get('cache_read_input_tokens', 0):,} 토큰")


if __name__ == "__main__":
    main()
