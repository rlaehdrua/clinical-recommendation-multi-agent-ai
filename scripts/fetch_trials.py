"""ClinicalTrials.gov에서 시험 프로토콜을 내려받아 data/trials/에 캐시.

    python scripts/fetch_trials.py --nct NCT01234567 NCT07654321
    python scripts/fetch_trials.py --condition "non-small cell lung cancer" --max 20
"""

from __future__ import annotations

import argparse

from ctrec.tools import ctgov


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nct", nargs="*", default=[])
    ap.add_argument("--condition")
    ap.add_argument("--intervention")
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--all-statuses", action="store_true", help="모집 완료 시험까지 포함")
    args = ap.parse_args()

    for nct in args.nct:
        rec = ctgov.fetch_study(nct, use_cache=False)
        print(f"saved {rec['trial_id']}: {rec['title']}")
    if args.condition:
        statuses = () if args.all_statuses else ("RECRUITING", "NOT_YET_RECRUITING")
        for rec in ctgov.search_studies(args.condition, intervention=args.intervention,
                                        statuses=statuses, max_results=args.max):
            print(f"saved {rec['trial_id']} [{rec['overall_status']}]: {rec['title']}")


if __name__ == "__main__":
    main()
