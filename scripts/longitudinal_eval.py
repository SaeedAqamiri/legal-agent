#!/usr/bin/env python3
"""Longitudinal evaluation harness: run the evaluation suite across a set of
``applicable_time`` windows and record per-window metrics (completion rate,
citation precision/recall, temporal accuracy, grounded-claim rate).

The project's in-process ``EvaluationRunner`` computes per-case metrics; this
script aggregates them into a longitudinal table so drift over time can be
tracked across releases. Run with a real composition root via ``run_evaluation``.
"""

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Longitudinal evaluation")
    parser.add_argument("--org", default="org-a")
    parser.add_argument(
        "--token", default="researcher-demo", help="bearer token for the API"
    )
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--dates",
        nargs="+",
        type=lambda value: date.fromisoformat(value),
        default=[date(2024, 6, 1), date(2025, 1, 1), date(2026, 8, 11)],
        help="applicable_time windows to evaluate",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        import requests  # type: ignore
    except ImportError:
        print("install httpx/requests to call the live API for longitudinal eval")
        return 1

    cases = [
        {
            "case_id": "appeal-deadline",
            "question": "مهلت تجدیدنظر برای اشخاص مقیم ایران چند روز است؟",
            "applicable_time": str(dt),
            "expected_provision_ids": ["provision-demo-336"],
            "expected_provision_version_ids": ["provision-demo-336-v1"],
        }
        for dt in args.dates
    ]
    headers = {"Authorization": f"Bearer {args.token}"}
    response = requests.post(
        f"{args.base}/v1/organizations/{args.org}/evaluations",
        headers=headers,
        json={"cases": cases},
        timeout=60,
    )
    response.raise_for_status()
    import json

    report = response.json()
    print(f"== Longitudinal evaluation for org={args.org} ==")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
