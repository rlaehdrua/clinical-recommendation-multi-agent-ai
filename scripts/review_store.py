"""잠긴 기준표 검수: 패치 제안 -> (사람) 승인 -> 후보 저장소에만 적용 -> diff·검사. LLM·API를 쓰지 않습니다.

원본 data/parsed/와 FROZEN.json은 읽기만 합니다. 공식 기준표 교체(후보를 data/parsed/로 복사)와 다시 잠금
(scripts/freeze_parsed.py)은 팀 승인 후 사람이 합니다.

    # 1) 제안 만들기 (승인 아님, 모두 status=proposed)
    python scripts/review_store.py propose --out data/patches/frozen-review.json
    # 2) 검수 상태 보기
    python scripts/review_store.py status --patches data/patches/frozen-review.json --approvals data/patches/approvals.json
    # 3) 검수자가 결정 기록 (사람이 실행. 패치 내용 해시에 묶임)
    python scripts/review_store.py decide --patches ... --approvals ... --patch-id <ID> --decision approved --reviewer <이름>
    # 4) 승인된 패치만 후보 저장소에 적용 + 규칙 단위 diff + 오프라인 검사
    python scripts/review_store.py apply --patches ... --approvals ... --candidate outputs/candidate_store/review1
    python scripts/review_store.py diff --candidate outputs/candidate_store/review1
    python scripts/review_store.py check --store outputs/candidate_store/review1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ctrec import config, store_patch


def _load(path: str | None) -> dict | None:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).exists() else None


def _print_diff(diffs: list[dict]) -> None:
    if not diffs:
        print("(차이 없음)")
    for d in diffs:
        print(f"{d['trial_id']} {d['rule_id']} {d['area']}.{d['key']}: {d['before']!r} -> {d['after']!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose")
    p.add_argument("--out", required=True)
    for name in ("status", "decide", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--patches", required=True)
        p.add_argument("--approvals", required=True)
    sub.choices["decide"].add_argument("--patch-id", required=True)
    sub.choices["decide"].add_argument("--decision", choices=["approved", "rejected"], required=True)
    sub.choices["decide"].add_argument("--reviewer", required=True)
    sub.choices["decide"].add_argument("--note", default="")
    sub.choices["apply"].add_argument("--candidate", required=True)
    p = sub.add_parser("diff")
    p.add_argument("--candidate", required=True)
    p = sub.add_parser("check")
    p.add_argument("--store", default=str(config.PARSED_DIR))
    args = ap.parse_args()

    if args.cmd == "propose":
        out = Path(args.out)
        if out.exists():
            sys.exit(f"{out}가 이미 있습니다 (검수 중인 제안을 덮어쓰지 않음)")
        proposals = store_patch.propose()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(proposals, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"패치 제안 {len(proposals['patches'])}개 -> {out} (모두 proposed, 승인 아님)")
    elif args.cmd == "status":
        patches = _load(args.patches)
        for pid, s in store_patch.review_status(patches, _load(args.approvals)).items():
            print(f"{s:9} {pid}")
    elif args.cmd == "decide":
        patches = _load(args.patches)
        patch = next((x for x in patches["patches"] if x["patch_id"] == args.patch_id), None)
        if patch is None:
            sys.exit(f"패치 {args.patch_id}가 없습니다")
        e = store_patch.record_decision(Path(args.approvals), patch, args.decision, args.reviewer, args.note)
        print(f"{e['decision']}: {e['patch_id']} ({e['reviewer']}, {e['reviewed_at']})")
    elif args.cmd == "apply":
        try:
            m = store_patch.apply_patches(_load(args.patches), _load(args.approvals), Path(args.candidate))
        except store_patch.PatchError as e:
            sys.exit(f"적용 거부(아무것도 쓰지 않음): {e}")
        print(f"적용 {len(m['applied'])}개, 승인되지 않아 건너뜀 {len(m['skipped_not_approved'])}개 -> {args.candidate}")
        _print_diff(store_patch.rule_diff(config.PARSED_DIR, Path(args.candidate)))
    elif args.cmd == "diff":
        _print_diff(store_patch.rule_diff(config.PARSED_DIR, Path(args.candidate)))
    elif args.cmd == "check":
        store = Path(args.store)
        if store.resolve() == config.PARSED_DIR.resolve():
            frozen = store_patch.verify_frozen()
            print("원본 잠금 무결성: " + ("정상" if not frozen else "; ".join(frozen)))
        problems = store_patch.check_store(store)
        print(f"검사 문제 {len(problems)}개")
        for line in problems:
            print("  " + line)


if __name__ == "__main__":
    main()
