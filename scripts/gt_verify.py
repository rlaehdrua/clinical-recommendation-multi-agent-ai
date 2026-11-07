"""gt/FROZEN_GT.json 확인: 파일 해시, 목록에 없는 파일, 지표 정의 해시, 파싱본 해시, 상태 이력 순서.

사용: python scripts/gt_verify.py [--gt-root gt] [--external D:/gt_private]
종료 코드: 0 정상, 1 문제 있음.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_lib import freeze as fz  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gt-root", type=Path, default=Path("gt"))
    ap.add_argument("--external", type=Path)
    ap.add_argument("--repo-root", type=Path)
    a = ap.parse_args(argv)
    problems = fz.verify(a.gt_root, external=a.external, repo_root=a.repo_root)
    if problems:
        print(f"FROZEN_GT 확인 실패: {len(problems)}건")
        for p in problems:
            print("   -", p)
        return 1
    print("FROZEN_GT 확인: 정상")
    return 0


if __name__ == "__main__":
    sys.exit(main())
