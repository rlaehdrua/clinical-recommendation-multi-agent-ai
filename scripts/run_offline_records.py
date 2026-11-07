"""WP7 오프라인 감사 기록 실행기: masked/validation 사례를 오프라인 대역으로 실행해 사례별 DecisionRecord·진단 파일을 만들고
다시 읽어 검증합니다. 실제 API 호출 없음(OfflineEnvironment). 라운드당 1문항 (docs/dev/wp7_offline_records.md).

    PYTHONPATH=src python scripts/run_offline_records.py --out <DIR> [--set masked] [--limit N] [--salt S] [--repeat]

--out 아래 <사례 순번>/decision_records.jsonl, decision_record_issues.jsonl, summary.json, reversal_metrics.json을 씁니다.
reversal_metrics.json은 사례 scanner로 검사한 export_metrics 결과입니다 (value_scan 참고. 비식별화를 뜻하지 않음).
정답 대조(숨긴 사실 ID·원문, 원래 케이스 ID가 파일에 없는지)는 이 평가기 스크립트에서만 합니다.
--repeat: 같은 salt로 한 번 더 실행해 fact_id 정규화 후 파일이 같은지 확인 (결정성).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, offline_records as orc, question_eval as qe  # noqa: E402
from ctrec.engine import audit  # noqa: E402
from ctrec.engine import record_producer as rp  # noqa: E402
from ctrec.engine import reversal_metrics as rm  # noqa: E402
from ctrec.eval_hygiene import AliasMap, load_cases_hygienic  # noqa: E402

SETS = {"masked": qe.DEFAULT_CASES,
        "validation": config.DATA_DIR / "patients" / "masked_validation" / "validation_cases.json"}


def _cases(parsed, path, salt):
    ids = [r["patient_id"] for r in json.loads(Path(path).read_text(encoding="utf-8"))]
    amap = AliasMap(salt=salt)
    for i in ids:  # 빈 맵은 integration/dcvc의 load_cases_hygienic이 버림 (WP1 PR #1에서 수정) -> 미리 채움
        amap.alias(i)
    return load_cases_hygienic(parsed, path, alias_map=amap)


def truth_leaks(hc, run_dir: Path) -> list[str]:
    """평가기 전용: 기록·진단 파일에 원래 ID·정답이 있는지 (종류만)."""
    blob = "".join((run_dir / n).read_text(encoding="utf-8") for n in (audit.RECORDS_FILE, rp.ISSUES_FILE)
                   if (run_dir / n).exists())
    out = []
    if hc.gt.case_id in blob:
        out.append("original_case_id")
    if any(f'"{h}"' in blob for h in hc.gt.masked_fact_ids):
        out.append("hidden_fact_id")
    if any(t and t in blob for _, t in hc.gt._hidden_texts):
        out.append("hidden_fact_text")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--set", choices=sorted(SETS), default="masked")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--salt", default="wp7-offline")
    ap.add_argument("--repeat", action="store_true")
    args = ap.parse_args(argv)
    parsed = load_frozen()
    allt = load_trials()
    trials = {t: allt[t] for t in parsed}
    hcs, _, scanner = _cases(parsed, SETS[args.set], args.salt)
    hcs = hcs[: args.limit] if args.limit else hcs
    results, leaks, repeat_diff = [], {}, []
    for i, hc in enumerate(hcs):
        r = orc.record_case(hc, trials, scanner, args.out / "run" / f"{i:03d}")
        results.append(r)
        found = truth_leaks(hc, r.run_dir)
        if found:
            leaks[f"{i:03d}"] = found
    if args.repeat:
        hcs2, _, scanner2 = _cases(parsed, SETS[args.set], args.salt)
        for i, hc in enumerate(hcs2[: len(hcs)]):
            r2 = orc.record_case(hc, trials, scanner2, args.out / "repeat" / f"{i:03d}")
            if orc.normalized_files(r2.run_dir) != orc.normalized_files(results[i].run_dir):
                repeat_diff.append(f"{i:03d}")
    summary = orc.write_summary(results, args.out / "summary.json")
    summary["truth_leaks"] = leaks
    summary["repeat_checked"] = args.repeat
    summary["repeat_mismatch"] = repeat_diff
    summary["set"] = args.set
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    metrics = rm.export_metrics([r.run_dir for r in results], scanner)
    (args.out / "reversal_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("set", "total", "issue_codes", "truth_leaks", "repeat_checked",
                                              "repeat_mismatch")}, ensure_ascii=False, indent=1))
    ok = (summary["total"]["complete_runs"] == len(results) and not leaks and not repeat_diff
          and metrics["value_scan"]["status"] == "no_findings")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
