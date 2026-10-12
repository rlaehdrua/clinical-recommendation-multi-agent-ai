"""시험 기준 고정 저장소(data/parsed/) 잠금.

현재 저장소의 모든 시험을 공식 기준표로 잠그고 파일 해시를 data/parsed/FROZEN.json에 기록합니다.
잠긴 시험은 compile_trials.py가 덮어쓰지 않고(--unfreeze 제외), 테스트가 파일이 바뀌지 않았는지 확인합니다.

    python scripts/freeze_parsed.py --note "2026-10-12 팀 결정: 애매함 검토 반영 후 잠금"
"""

from __future__ import annotations

import argparse

from ctrec import config, trial_store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    m = trial_store.freeze(config.PARSED_DIR, args.note)
    print(f"{len(m['files'])}개 시험 잠금 -> {config.PARSED_DIR / trial_store.FREEZE_FILE}")


if __name__ == "__main__":
    main()
