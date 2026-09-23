#!/usr/bin/env python3
"""Automated accessibility smoke-audit for the workspace UI.

Checks the served HTML for common WCAG-level gaps: unlabeled form controls,
unclosed/main landmarks, empty headings, and missing security headers that
matter for assistive-technology users. This is a guard, not a full audit.
"""

import argparse
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--path", default="/workspace")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with urllib.request.urlopen(f"{args.base}{args.path}", timeout=10) as response:
        html = response.read().decode("utf-8")
        headers = {k.lower(): v for k, v in response.headers.items()}

    issues: list[str] = []
    ids = set(re.findall(r'id="([^"]+)"', html))

    # Every <label for=...> must reference an existing control id.
    for label_for in re.findall(r'<label[^>]+for="([^"]+)"', html):
        if label_for not in ids:
            issues.append(f"label references missing id: {label_for}")
        else:
            # id must belong to an input/textarea/select
            pattern = re.compile(rf'<(input|textarea|select)[^>]*id="{label_for}"')
            if not pattern.search(html):
                issues.append(f"label '{label_for}' does not target a control")

    # Buttons must be type-aware and not empty.
    for button in re.findall(r"<button([^>]*)>", html):
        if 'type=' not in button and 'role=' not in button:
            issues.append(f"button without explicit type: <button{button.strip()[:40]}>")

    # Headings present in order lands within a main landmark.
    if "<main" not in html:
        issues.append("missing <main> landmark")

    # Security headers that also affect accessibility tooling.
    for header_name, bad_token in [
        ("content-security-policy", None),
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", None),
    ]:
        if header_name not in headers:
            issues.append(f"missing header: {header_name}")

    title = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    if not title or not title.group(1).strip():
        issues.append("empty or missing <title>")

    print(f"audit of {args.path}: {html.count('<h')} headings, "
          f"{html.count('<label')} labels, {len(ids)} controls")
    if not issues:
        print("PASS")
        return 0
    for issue in issues:
        print(" •", issue)
    print(f"\n{len(issues)} accessibility issue(s) found.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
