"""GT 누출 검사. 거부 사유: 기존 held-out·실패·회귀 세트나 테스트 문장과 겹침, 엔진 출력 필드, 블라인딩 증명 없음,
FIXTURE_ONLY 표시.

비교 대상(기본): docs/dev/*.json (held-out 4·5·6 포함), tests/data/**/*.json (회귀 세트 1-3·dev),
eval/**/*.json, tests/**/*.py의 문자열 상수. GT 도구 자신의 가짜 데이터(tests/data/gt_fixture/, tests/test_gt_*.py)는
비교 대상에서 빼고 FIXTURE_ONLY 표시로 따로 거부합니다.

사용:
  python scripts/gt_leak_check.py SET.json [...] [--extra-corpus PATH ...] [--allow-fixture] [--json OUT]
종료 코드: 0 통과, 1 거부 있음.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_lib.leak import build_corpus, check_set  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sets", nargs="+", type=Path)
    ap.add_argument("--extra-corpus", nargs="*", type=Path, default=[], help="추가 비교 대상 (파일·폴더)")
    ap.add_argument("--repo-root", type=Path, default=None)
    ap.add_argument("--allow-fixture", action="store_true", help="FIXTURE_ONLY 허용 (도구 테스트 전용)")
    ap.add_argument("--json", type=Path, help="거부 목록을 JSON으로 저장")
    a = ap.parse_args(argv)
    corpus = build_corpus(a.repo_root, a.extra_corpus)
    print(f"비교 문장 {len(corpus.entries)}개")
    total = {}
    for p in a.sets:
        raw = json.loads(p.read_text(encoding="utf-8"))
        refusals = check_set(raw, corpus, allow_fixture=a.allow_fixture)
        total[str(p)] = refusals
        print(f"{'REFUSE' if refusals else 'PASS  '} {p}: 거부 {len(refusals)}건")
        for r in refusals[:50]:
            print(f"   - [{r['kind']}] {r['item_id']}: {r['detail']}")
    if a.json:
        a.json.write_text(json.dumps(total, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if any(total.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
