#!/usr/bin/env python3
"""Discovery client for the internal Baray ECM (192.168.8.41:9000).

Logs in, dumps the full form tree (Ui/GetSystemList), scans the Angular
bundles for API endpoint patterns, and stores everything under
data/qavanin_store/discovery/ so the sources schema (V0005) can be
reconciled with the sample law database designed there.

Usage:
    python scripts/baray_explore.py                 # full discovery pass
    python scripts/baray_explore.py --ping          # reachability check only
Env: BARAY_BASE_URL, BARAY_USER, BARAY_PASSWORD override defaults.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_BASE_URL = "http://192.168.8.41:9000"
DEFAULT_USER = "khobyari"
DEFAULT_PASSWORD = "Qavanin@123456789"
DEFAULT_OUT = Path("data/qavanin_store/discovery")

UA = {
    "User-Agent": "legal-agent-discovery/0.1",
    "Accept": "application/json, text/html;q=0.8",
}


class BarayClient:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.token: str | None = None

    def _request(
        self, path: str, data: dict | None = None, timeout: int = 20
    ) -> tuple[int, bytes]:
        body = json.dumps(data).encode() if data is not None else None
        headers = dict(UA)
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Token"] = self.token
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:  # non-2xx still returns content
            return exc.code, exc.read()

    def ping(self) -> bool:
        try:
            status, _ = self._request("/", timeout=6)
            return status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def login(self) -> None:
        status, body = self._request(
            "/Authentication/login", {"userName": self.user, "password": self.password}
        )
        payload = _json(body)
        token = payload.get("returnvalue") if isinstance(payload, dict) else None
        if status != 200 or not token:
            raise SystemExit(f"login failed (HTTP {status}): {_text(body)[:200]}")
        self.token = str(token)

    def systems(self) -> dict:
        status, body = self._request("/Ui/GetSystemList")
        if status != 200:
            raise SystemExit(f"GetSystemList failed (HTTP {status})")
        data = _json(body)
        if not isinstance(data, (list, dict)):
            raise SystemExit("GetSystemList returned unexpected payload")
        return data

    def get(self, path: str, timeout: int = 30) -> bytes:
        _, body = self._request(path, timeout=timeout)
        return body


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None


def _text(body: bytes) -> str:
    return body.decode("utf-8", "replace")


def walk_forms(node, out: list[dict], path: str = "") -> None:
    items = node if isinstance(node, list) else [node]
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name", "")).strip()
        form_id = item.get("ID")
        here = f"{path}/{name}" if path else name
        children = item.get("ChildMenu")
        if children:
            walk_forms(children, out, here)
        elif form_id is not None:
            out.append({"form_id": form_id, "path": here})


def scan_endpoints(main_js: str) -> list[str]:
    urls = set(
        re.findall(
            r"""['"](/(?:[A-Z][A-Za-z]+/)?[A-Z][A-Za-z]+(?:/[A-Za-z]+){0,3})['"]""",
            main_js,
        )
    )
    known = [
        u
        for u in urls
        if re.match(
            r"^/(Authentication|Ui|Form|Doc|Document|System|Grid|Data|File|Report|Workflow)",
            u,
        )
    ]
    return sorted(known)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--ping", action="store_true", help="reachability check only")
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    client = BarayClient(args.base_url, args.user, args.password)
    for attempt in range(1, args.retries + 1):
        if client.ping():
            break
        print(f"[{attempt}/{args.retries}] unreachable: {args.base_url}", flush=True)
        if attempt == args.retries:
            print("RESULT: host unreachable — nothing saved", flush=True)
            return 2
        time.sleep(4)

    if args.ping:
        print("RESULT: reachable")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    print("login…", flush=True)
    client.login()

    print("GetSystemList…", flush=True)
    systems = client.systems()
    (args.out / f"systems_{stamp}.json").write_text(
        json.dumps(systems, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out / "systems_latest.json").write_text(
        json.dumps(systems, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    forms: list[dict] = []
    walk_forms(systems, forms)
    (args.out / "forms_latest.json").write_text(
        json.dumps(forms, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for form in forms[:200]:
        print(f"  form {form['form_id']:>8}  {form['path']}")

    print("scanning bundles for endpoints…", flush=True)
    main_js = _text(client.get("/Content/MainJs"))
    endpoints = scan_endpoints(main_js)
    (args.out / "endpoints_latest.txt").write_text(
        "\n".join(endpoints), encoding="utf-8"
    )
    print(f"  {len(endpoints)} candidate endpoints")

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "form_count": len(forms),
        "endpoint_count": len(endpoints),
        "note": "form-level schema endpoints (GetFormStructure etc.) pending next pass",
    }
    (args.out / f"discovery_{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"RESULT: saved to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
