"""GT 동결 (gt/FROZEN_GT.json). Frozen Store(data/parsed/FROZEN.json)와 같은 방식: 파일별 sha256 + 버전 + 시각.

동결 전에 모든 세트(g_u/, g_c/, g_p/의 *.json)를 스키마·동결 조건·누출 검사로 확인하고, 하나라도 실패하면 쓰지 않습니다.
원문을 저장소 밖에 두는 사전 등록은 --external DIR (해시만 기록, storage=external).

사용:
  python scripts/gt_freeze.py freeze --gt-root gt --version gt-v1.0 --reference-date 2026-11-07
      [--external D:/gt_private] [--trial-store data/parsed/FROZEN.json ...] [--labelers L-A L-B] [--adjudicator ADJ-1]
  python scripts/gt_freeze.py advance --gt-root gt --to measured_once --reason "..."
      --measurement gt/results/gt-v1.0/<sha>/report.json --engine-commit <sha>
  python scripts/gt_freeze.py advance --gt-root gt --to regression --reason "결과를 보고 코드 수정"
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_lib import freeze as fz  # noqa: E402
from gt_lib.labelio import read_csv  # noqa: E402
from gt_lib.leak import build_corpus, check_set  # noqa: E402
from gt_lib.models import GTSet, freeze_problems  # noqa: E402


def read_adjudication_log(bases) -> list[dict] | None:
    """gt 루트 또는 외부 보관소의 adjudication_log.csv 행 (둘 다 없으면 None)."""
    rows = None
    for base in bases:
        p = base / "adjudication_log.csv"
        if p.is_file():
            rows = (rows or []) + read_csv(p)
    return rows


def check_sets(bases, allow_fixture: bool, extra_corpus, repo_root=None) -> list[str]:
    problems, corpus = [], None
    adj_log = read_adjudication_log(bases)
    n_sets = 0
    for base in bases:
        for d in fz.SET_DIRS:
            for p in sorted((base / d).glob("*.json")) if (base / d).is_dir() else []:
                raw = json.loads(p.read_text(encoding="utf-8"))
                try:
                    s = GTSet.model_validate(raw)
                except Exception as e:
                    problems.append(f"{p}: 스키마 실패 {e}")
                    continue
                n_sets += 1
                problems += [f"{p}: {x}" for x in freeze_problems(s, allow_fixture=allow_fixture,
                                                                   adjudication_log=adj_log)]
                corpus = corpus or build_corpus(repo_root, extra_corpus)
                problems += [f"{p}: 누출 [{r['kind']}] {r['item_id']}" for r in
                             check_set(raw, corpus, allow_fixture=allow_fixture)]
    if n_sets == 0:
        problems.append("동결할 GT 세트(g_u/, g_c/, g_p/의 *.json)가 없습니다")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--gt-root", type=Path, default=Path("gt"))
    f.add_argument("--version", required=True)
    f.add_argument("--reference-date", required=True)
    f.add_argument("--external", type=Path)
    f.add_argument("--trial-store", nargs="*", type=Path, default=[])
    f.add_argument("--labelers", nargs="*", default=[])
    f.add_argument("--adjudicator")
    f.add_argument("--note", default="")
    f.add_argument("--extra-corpus", nargs="*", type=Path, default=[])
    f.add_argument("--repo-root", type=Path)
    f.add_argument("--allow-fixture", action="store_true", help="도구 테스트 전용. 실제 GT 동결에 쓰지 말 것")
    v = sub.add_parser("advance")
    v.add_argument("--gt-root", type=Path, default=Path("gt"))
    v.add_argument("--to", required=True, choices=fz.STATUSES)
    v.add_argument("--reason", required=True)
    v.add_argument("--measurement", type=Path)
    v.add_argument("--engine-commit")
    v.add_argument("--external", type=Path)
    v.add_argument("--repo-root", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "freeze":
        bases = [a.gt_root] + ([a.external] if a.external else [])
        problems = check_sets(bases, a.allow_fixture, a.extra_corpus, a.repo_root)
        if problems:
            print(f"동결 거부: 문제 {len(problems)}건")
            for x in problems[:100]:
                print("   -", x)
            return 1
        try:
            m = fz.build_manifest(a.gt_root, gt_version=a.version, reference_date=a.reference_date,
                                  external=a.external, trial_store=a.trial_store, labelers=a.labelers,
                                  adjudicator=a.adjudicator, note=a.note, repo_root=a.repo_root)
            path = fz.write_manifest(a.gt_root, m)
        except ValueError as e:
            print(f"동결 거부: {e}")
            return 1
        print(f"wrote {path}: {a.version}, 파일 {len(m['files'])}개, status=sealed")
        return 0
    try:
        m = fz.advance(a.gt_root, a.to, reason=a.reason, measurement=a.measurement, engine_commit=a.engine_commit,
                       external=a.external, repo_root=a.repo_root)
    except ValueError as e:
        print(f"거부: {e}")
        return 1
    print(f"status={m['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
