"""시험 기준 사전 컴파일 (팀 결정 D8 Option C + D1 Option D + D6 Option C).

시험마다 기준을 여러 번 파싱하고, 그중 가장 대표적인 실행(다른 실행과 규칙 분할이 가장 많이 겹치는 실행)을 고른 뒤
파싱 검증(agents/parser_validator.py)을 거쳐 고정 저장소 data/parsed/<trial_id>.json에 저장합니다.
환자 평가 때는 이 저장소만 씁니다(다시 파싱하지 않음).

- 실행별 파싱 결과는 --runs-dir에 있으면 다시 쓰고(scripts/parse_consistency.py 결과와 같은 형식), 없으면 새로 파싱합니다.
- 다른 실행과 속성이 다른 규칙은 위험 항목으로 LLM 의미 검증을 받습니다.

    python scripts/compile_trials.py                      # data/trials 전체, 이미 최신인 시험은 건너뜀
    python scripts/compile_trials.py --only NCT07516951 --force
    python scripts/compile_trials.py --no-llm             # 결정적 검사·위험 탐지만 (비용 없음, 미리보기)

잠긴 시험(data/parsed/FROZEN.json)은 건너뜁니다. 팀 검토 후 바꿀 때만 --unfreeze를 붙이고, 끝나면 scripts/freeze_parsed.py로 다시 잠급니다.
"""

from __future__ import annotations

import argparse
import contextvars
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ctrec import config, llm, trial_store
from ctrec.agents import criteria_parser, parser_validator
from ctrec.schemas import ParsedTrial
from ctrec.tools import ctgov


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def medoid(runs: list[ParsedTrial]) -> int:
    """다른 실행과 규칙 문장이 가장 많이 겹치는 실행의 위치 (동점이면 앞선 실행)."""
    keys = [{_key(r.text) for r in p.rules} for p in runs]

    def overlap(a: set, b: set) -> float:
        return len(a & b) / len(a | b) if a | b else 1.0

    scores = [sum(overlap(keys[i], keys[j]) for j in range(len(runs)) if j != i) for i in range(len(runs))]
    return max(range(len(runs)), key=lambda i: (scores[i], -i))


def load_runs(trial: dict, runs_dir: Path, n: int) -> list[ParsedTrial]:
    runs = []
    for i in range(1, n + 1):
        path = runs_dir / f"{trial['trial_id']}_run{i}.json"
        if path.exists():
            p = ParsedTrial.model_validate_json(path.read_text(encoding="utf-8"))
        else:
            p = criteria_parser.parse_trial(trial, use_cache=False)
            runs_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(p.model_dump_json(indent=1), encoding="utf-8")
        p.integrity_issues = criteria_parser.check_integrity(p, trial)
        runs.append(p)
    return runs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials-dir", default="data/trials")
    ap.add_argument("--runs-dir", default="outputs/parse_consistency")
    ap.add_argument("--runs", type=int, default=3, help="시험당 파싱 실행 수 (없는 실행만 새로 파싱)")
    ap.add_argument("--store", default=str(config.PARSED_DIR))
    ap.add_argument("--only", help="쉼표로 구분한 trial_id만")
    ap.add_argument("--force", action="store_true", help="이미 최신이어도 다시 컴파일")
    ap.add_argument("--no-llm", action="store_true", help="LLM 의미 검증 없이 미리보기 (저장하지 않음)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--unfreeze", action="store_true", help="잠긴 시험도 다시 컴파일해 덮어씀 (팀 검토 후에만)")
    args = ap.parse_args()

    store = Path(args.store)
    trials = [ctgov.load_local(p) for p in sorted(Path(args.trials_dir).glob("*.json"))]
    if args.only:
        only = set(args.only.split(","))
        trials = [t for t in trials if t["trial_id"] in only]
    if not args.unfreeze and not args.no_llm:
        frozen = [t["trial_id"] for t in trials if trial_store.is_frozen(t["trial_id"], store)]
        trials = [t for t in trials if t["trial_id"] not in frozen]
        if frozen:
            print(f"잠긴 기준표라 건너뜀: {len(frozen)}개 (바꾸려면 --unfreeze)")
    if not args.force and not args.no_llm:
        skipped = [t["trial_id"] for t in trials if trial_store.is_current(t, store)]
        trials = [t for t in trials if t["trial_id"] not in skipped]
        if skipped:
            print(f"이미 최신이라 건너뜀: {len(skipped)}개")
    llm.reset_usage()

    def work(trial: dict):
        runs = load_runs(trial, Path(args.runs_dir), args.runs)
        best = medoid(runs)
        canonical = runs[best].model_copy(deep=True)
        canonical.integrity_issues = list(runs[best].integrity_issues)
        others = [r for i, r in enumerate(runs) if i != best]
        parsed, meta, report = parser_validator.validate(canonical, trial, others, use_llm=not args.no_llm)
        if not args.no_llm:
            trial_store.save(trial, parsed, meta, report, store,
                             extra={"runs": len(runs), "canonical_run": best + 1}, allow_frozen=args.unfreeze)
        return trial["trial_id"], parsed, report

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(contextvars.copy_context().run, work, t) for t in trials]
        results = [f.result() for f in futures]

    print(f"\n{'시험':<14}{'규칙':>5}{'위험':>5}{'검증':>5}{'변경':>5}{'의사확인':>8}{'추가':>5}{'누락후보':>8}")
    tot = {"rules": 0, "risk": 0, "checked": 0, "changes": 0, "review": 0, "added": 0, "gaps": 0}
    for tid, parsed, rep in results:
        row = {"rules": len(parsed.rules), "risk": len(rep.risk), "checked": len(rep.llm_checked),
               "changes": len(rep.changes), "review": len(rep.review), "added": len(rep.added_rules),
               "gaps": len(rep.coverage_gaps)}
        for k, v in row.items():
            tot[k] += v
        print(f"{tid:<14}{row['rules']:>5}{row['risk']:>5}{row['checked']:>5}{row['changes']:>5}"
              f"{row['review']:>8}{row['added']:>5}{row['gaps']:>8}")
    print(f"{'합계':<14}{tot['rules']:>5}{tot['risk']:>5}{tot['checked']:>5}{tot['changes']:>5}"
          f"{tot['review']:>8}{tot['added']:>5}{tot['gaps']:>8}")
    u = llm.usage_snapshot()
    print(f"\n사용량: {json.dumps(u)}")
    if not args.no_llm:
        print(f"-> {store}/")


if __name__ == "__main__":
    main()
