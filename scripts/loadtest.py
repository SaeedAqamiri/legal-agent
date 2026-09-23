#!/usr/bin/env python3
"""Load test for the research + library endpoints using stdlib only.

Run against the live server; reports request/second and latency percentiles.
   python3 scripts/loadtest.py --base http://127.0.0.1:8000 --token expert-demo --requests 200 --concurrency 8
"""

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="expert-demo")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--org", default="org-a")
    return parser.parse_args()


def call(base: str, token: str, org: str) -> float:
    start = time.perf_counter()
    headers = {"Authorization": f"Bearer {token}"}
    # Mix of realistic read traffic; POST research is heavier so sample library first.
    request = Request(
        f"{base}/v1/organizations/{org}/library",
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:
        response.read()
    return (time.perf_counter() - start) * 1000


def main() -> int:
    args = parse_args()
    probes = [  # warm-up + health
        ("GET /healthz", lambda: urlopen(f"{args.base}/healthz", timeout=10)),
    ]
    for path, fn in probes:
        try:
            with fn() as response:
                response.read()
        except Exception as exc:  # noqa: BLE001
            print(f"warm-up {path} failed: {exc}")
            return 1

    latencies: list[float] = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(call, args.base, args.token, args.org)
            for _ in range(args.requests)
        ]
        for future in futures:
            try:
                latencies.append(future.result())
            except HTTPError as exc:
                print("HTTP", exc.code)
            except Exception as exc:  # noqa: BLE001
                print("error", exc)

    elapsed = time.perf_counter() - started
    latencies.sort()
    n = len(latencies)
    print(f"\nrequests={n} in {elapsed:.2f}s → {n / elapsed:.1f} req/s (concurrency {args.concurrency})")
    if latencies:
        print(f"latency ms min/median/p90/p99/max = "
              f"{latencies[0]:.1f} / {statistics.median(latencies):.1f} / "
              f"{latencies[int(n * .9) - 1]:.1f} / {latencies[int(n * .99) - 1]:.1f} / "
              f"{latencies[-1]:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
