"""GT 세트 스키마 검증 (+ 동결 조건). 엔진을 import하지 않습니다.

사용:
  python scripts/gt_validate.py SET.json [SET2.json ...] [--freezable] [--allow-fixture]
  python scripts/gt_validate.py --export-schema gt/schema     # pydantic 모델 -> JSON Schema 파일
종료 코드: 0 통과, 1 실패.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_lib.models import export_json_schemas, freeze_problems, load_set  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sets", nargs="*", type=Path)
    ap.add_argument("--freezable", action="store_true", help="동결 조건(라벨러 2명, 판정 완료, 블라인딩)까지 확인")
    ap.add_argument("--allow-fixture", action="store_true", help="FIXTURE_ONLY 세트 허용 (도구 테스트 전용)")
    ap.add_argument("--export-schema", type=Path, metavar="DIR")
    a = ap.parse_args(argv)
    if a.export_schema:
        a.export_schema.mkdir(parents=True, exist_ok=True)
        for name, schema in export_json_schemas().items():
            (a.export_schema / name).write_text(json.dumps(schema, ensure_ascii=False, indent=1) + "\n",
                                                encoding="utf-8")
            print(f"wrote {a.export_schema / name}")
    bad = 0
    for p in a.sets:
        try:
            s = load_set(p)
        except Exception as e:  # pydantic ValidationError 포함
            print(f"FAIL {p}: {e}")
            bad += 1
            continue
        probs = freeze_problems(s, allow_fixture=a.allow_fixture) if a.freezable else []
        if probs:
            bad += 1
            print(f"FAIL {p}: 동결 조건 {len(probs)}건")
            for x in probs:
                print("   -", x)
        else:
            tag = " [FIXTURE_ONLY]" if s.fixture_only else ""
            print(f"OK   {p}: {s.set_id} {s.layer} items={len(s.items)}{tag}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
