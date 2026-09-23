#!/usr/bin/env python3
"""Fetch the Iran Ministry of Sports legal corpus into ``data/sport_ministry_corpus``.

Sources (both official):
  * qavanin.ir  — سامانه ملی قوانین و مقررات (ArvanCloud WAF: challenge is solved
    programmatically by evaluating the obfuscated JS and setting ``__arcsjs``/``__arcsjsc``).
  * rc.majlis.ir — پایگاه ملی اطلاع‌رسانی قوانین و مقررات (print_version endpoint).

Outputs a benchmark-ready corpus mirroring tests/tamin_longdocs_agentic_benchmark_v4:
  documents_md/D*.md      — pdftotext-style markdown with ``## صفحه N`` markers
  metadata/document_map.{json,csv}
  metadata/text_extraction_report.csv
  manifest.json, SHA256SUMS.txt

The ``.md`` files are ingestible through ``MarkdownDocumentParser`` /
``scripts/ingest_markdown_samples.py``.

Usage:
    python scripts/fetch_sport_ministry_corpus.py [--out DIR] [--only D01,D02] [--delay 0.5]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_mod
import json
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


@dataclass
class CatalogItem:
    doc_id: str
    title: str
    source: str  # "qavanin" | "rc"
    ref: str  # qavanin IDS or rc.majlis law id
    date: str | None = None  # گاه‌شماری جلالی YYYY/MM/DD (اگر شناخته باشدیم)
    category: str = ""
    issuer: str = ""
    url: str = field(default="", repr=False)


def _q(ids: str) -> str:
    return f"https://qavanin.ir/Law/TreeText/?IDS={ids}"


def _rc(law_id: str) -> str:
    return f"https://rc.majlis.ir/fa/law/print_version/{law_id}"


def _catalog() -> list[CatalogItem]:
    items: list[CatalogItem] = [
        # --- قوانین و آیین‌نامه‌های اصلی وزارت ورزش (جاری) ---
        CatalogItem("D01", "قانون اهداف، وظایف و اختیارات وزارت ورزش و جوانان", "qavanin", "2548158419293780800", "1399/07/08", "قانون جاری", "مجلس شورای اسلامی"),
        CatalogItem("D02", "آیین‌نامه اجرایی قانون اهداف، وظایف و اختیارات وزارت ورزش و جوانان", "qavanin", "16423637706770635428", "1402/01/16", "آیین‌نامه جاری", "هیئت وزیران"),
        CatalogItem("D03", "قانون تبدیل سازمان‌های تربیت بدنی و ملی جوانان به وزارت ورزش و جوانان", "qavanin", "17290258803281690213", "1390/08/17", "قانون انتقال", "مجلس شورای اسلامی"),
        # --- المپیک / پارالمپیک ---
        CatalogItem("D04", "اساسنامه کمیته ملی المپیک ایران", "rc", "99568", "1358/12/15", "المپیک تاریخی", "مجلس شورای اسلامی"),
        CatalogItem("D05", "قانون اصلاح لایحه قانونی اساسنامه کمیته ملی المپیک ایران", "rc", "90190", "1360/04/03", "المپیک تاریخی", "مجلس شورای اسلامی"),
        CatalogItem("D06", "اساسنامه کمیته ملی المپیک جمهوری اسلامی ایران", "rc", "1678954", "1400/09/02", "المپیک جاری", "مجلس شورای اسلامی"),
        CatalogItem("D07", "اساسنامه کمیته ملی پارالمپیک", "rc", "1070528", "1397/05/27", "پارالمپیک", "مجلس شورای اسلامی"),
        CatalogItem("D08", "قانون انتزاع کمیته ملی پارالمپیک از کمیته ملی المپیک جمهوری اسلامی ایران", "qavanin", "8751840910837058242", "1395/06/10", "قانون پارالمپیک", "مجلس شورای اسلامی"),
        # --- فدراسیون‌ها ---
        CatalogItem("D09", "اساسنامه فدراسیون‌های ورزشی آماتوری جمهوری اسلامی ایران", "rc", "121941", "1381/02/11", "فدراسیون‌ها نسخه ۱۳۸۱", "مجلس شورای اسلامی"),
        CatalogItem("D10", "الحاق بند (۲۳) به ماده (۳) اساسنامه فدراسیون‌های ورزشی آماتوری", "rc", "845251", "1392/02/22", "فدراسیون‌ها الحاق", "مجلس شورای اسلامی"),
        CatalogItem("D11", "اساسنامه فدراسیون‌های ورزشی آماتوری جمهوری اسلامی ایران (بازنگری ۱۴۰۰)", "rc", "1667739", "1400/04/19", "فدراسیون‌ها نسخه ۱۴۰۰", "مجلس شورای اسلامی"),
        CatalogItem("D12", "رأی شماره ۲۷ـ۱۳۸۰/۲/۲ هیأت عمومی دیوان عدالت اداری: ابطال تبصره ماده ۲ آیین‌نامه مسابقات فوتبال", "rc", "102270", "1380/02/02", "فوتبال رأی دیوان", "دیوان عدالت اداری"),
        CatalogItem("D13", "رأی شماره ۱۶۶۵ هیأت عمومی دیوان عدالت اداری: فدراسیون فوتبال و شمول دستگاه‌های دولتی", "rc", "1678164", "1400/08/15", "فوتبال رأی دیوان", "دیوان عدالت اداری"),
        CatalogItem("D14", "رأی دیوان عدالت اداری: ابطال تصمیم شماره ۸۳۲۷/۲۳/۲۵۱ فدراسیون تکواندو", "rc", "104052", "1385/06/26", "تکواندو رأی دیوان", "دیوان عدالت اداری"),
        CatalogItem("D15", "رأی شماره ۱۴۵۲۲۲۹ هیأت تخصصی دیوان عدالت اداری: دستورالعمل فدراسیون", "rc", "1791608", "1402/09/12", "رأی دیوان ورزش", "دیوان عدالت اداری"),
        CatalogItem("D16", "رأی شماره ۱۰۵۷۷۹۲ هیأت تخصصی دیوان عدالت اداری: عبارت هیأت رئیسه در اساسنامه", "rc", "1783593", "1402/07/08", "رأی دیوان ورزش", "دیوان عدالت اداری"),
        CatalogItem("D17", "رأی شماره ۱۳۹۴۱۸۹ هیأت تخصصی دیوان عدالت اداری: جزء ب و ج بند ۲ ماده ۲", "rc", "1783840", "1402/07/10", "رأی دیوان ورزش", "دیوان عدالت اداری"),
        CatalogItem("D18", "رأی شماره‌های ۴۴۰ و ۴۳۹ هیأت عمومی دیوان عدالت اداری: ابطال دستورالعمل فدراسیون", "rc", "867226", "1392/07/08", "رأی دیوان ورزش", "دیوان عدالت اداری"),
        CatalogItem("D19", "تصویب‌نامه ترخیص کالاهای اهدایی فدراسیون فوتبال جمهوری اسلامی ایران", "rc", "121639", "1380/09/14", "فوتبال تصویب‌نامه", "هیئت وزیران"),
        CatalogItem("D20", "بخشنامه شورای عالی معادن: معافیت فدراسیون کشتی از اخذ گواهی صلاحیت", "qavanin", "7258243151774583520", None, "کشتی بخشنامه", "شورای عالی معادن"),
        CatalogItem("D21", "قانون عضویت دولت جمهوری اسلامی ایران در فدراسیون ورزش دانشگاه‌های آسیا", "qavanin", "17426611318034386851", None, "فدراسیون دانشگاهی", "مجلس شورای اسلامی"),
        CatalogItem("D22", "مصوبه تشکیل انجمن مجزا در فدراسیون‌های چندرشته‌ای", "rc", "1547344", "1399/03/07", "فدراسیون‌ها تصویب‌نامه", "هیئت وزیران"),
        CatalogItem("D23", "تصویب‌نامه حق استفاده فدراسیون‌های (هیئت‌های) ورزشی از اماکن ورزشی استان‌ها", "rc", "1807998", "1403/03/09", "فدراسیون‌ها اماکن", "هیئت وزیران"),
        # --- قوانین و مقررات تاریخی ---
        CatalogItem("D24", "قانون ورزش اجباری در مدارس جدیده", "qavanin", "4291461066965602037", "1306/06/14", "تاریخی", "مجلس مؤسسان/مجلس شورای ملی"),
        CatalogItem("D25", "لایحه قانونی واگذاری سازمان ورزشی و فرهنگی تاج به سازمان تربیت بدنی ایران", "qavanin", "9184540084965638514", "1359/05/04", "تاریخی", "شورای انقلاب"),
        CatalogItem("D26", "لایحه قانونی راجع به مردمی کردن باشگاه‌های ورزشی عمومی در سراسر کشور", "qavanin", "9750869455865471248", "1359/06/24", "تاریخی", "شورای انقلاب"),
        CatalogItem("D27", "لایحه قانونی اداره و انجام امور استخدامی و پرسنلی و بهره‌برداری از کانون‌های ورزشی", "qavanin", "15363726103905209104", None, "تاریخی", "شورای انقلاب"),
        CatalogItem("D28", "لایحه قانونی معافیت وسایل و لوازم ورزشی سفارشی موردنیاز فدراسیون‌ها و باشگاه‌ها", "qavanin", "2025313692737018355", "1358/07/26", "تاریخی", "شورای انقلاب"),
        CatalogItem("D29", "اساسنامه سازمان مراکز جهانگردی برای ورزش‌های زمستانی", "qavanin", "11703171788411680104", None, "تاریخی", "مجلس شورای ملی"),
        CatalogItem("D30", "اصلاح ماده ۹ اساسنامه سازمان مراکز جهانگردی برای ورزش‌های زمستانی", "qavanin", "9328100976740491559", None, "تاریخی", "مجلس شورای ملی"),
        CatalogItem("D31", "قانون اجازه تأسیس باشگاه ورزشی و ورزشگاه توسط مردم با نظارت دولت", "qavanin", "9562262196903032490", None, "تاریخی باشگاه‌ها", "مجلس شورای ملی"),
        CatalogItem("D32", "الحاق تبصره (۳) به ذیل ماده (۵) آیین‌نامه اجرایی قانون اجازه تأسیس باشگاه ورزشی و ورزشگاه", "qavanin", "3742632897642277482", None, "تاریخی باشگاه‌ها", "هیئت وزیران"),
        CatalogItem("D33", "قانون اصلاح ماده (۱۱) قانون تأسیس سازمان تربیت بدنی", "qavanin", "1239755077207852887", None, "تاریخی", "مجلس شورای اسلامی"),
        CatalogItem("D34", "قانون اصلاح قانون سرباز قهرمان", "qavanin", "4840544963707557208", "1392/02/18", "سرباز قهرمان", "مجلس شورای اسلامی"),
        CatalogItem("D35", "آیین‌نامه اجرایی قانون اصلاح قانون سرباز قهرمان مصوب ۱۳۹۱/۱۰/۰۳", "qavanin", "10982686171358582556", None, "سرباز قهرمان", "هیئت وزیران"),
        CatalogItem("D36", "آیین‌نامه اجرایی جذب و استخدام قهرمانان مشمول قانون سرباز قهرمان", "qavanin", "11627360265410472511", None, "سرباز قهرمان", "هیئت وزیران"),
        CatalogItem("D37", "قانون الحاق جمهوری اسلامی ایران به کنوانسیون بین‌المللی علیه آپارتاید در ورزش", "qavanin", "5886717331139469019", None, "بین‌الملل تاریخی", "مجلس شورای اسلامی"),
        # --- مصوبات شورای عالی انقلاب فرهنگی و سیاست‌ها ---
        CatalogItem("D38", "مصوبه شورای عالی انقلاب فرهنگی: سیاست‌های فرهنگی ـ اجتماعی ورزش زنان کشور", "qavanin", "9125642287389027945", None, "ورزش زنان", "شورای عالی انقلاب فرهنگی"),
        CatalogItem("D39", "اصلاح و تکمیل سیاست‌های فرهنگی ـ اجتماعی ورزش زنان کشور", "qavanin", "63030556329346539", None, "ورزش زنان", "شورای عالی انقلاب فرهنگی"),
        CatalogItem("D40", "مصوبه اولویت‌ها و اقدامات اساسی حوزه فرهنگی ورزش کشور (نظام‌نامه فرهنگی ورزش کشور)", "qavanin", "7118364368265559908", None, "سیاست‌های ورزش", "شورای عالی انقلاب فرهنگی"),
        # --- اماکن و زیرساخت ورزشی ---
        CatalogItem("D41", "قانون تأمین اعتبار احداث، تکمیل، توسعه و تجهیز اماکن ورزشی", "qavanin", "9570685504552361769", None, "اماکن ورزشی", "مجلس شورای اسلامی"),
        CatalogItem("D42", "اصلاحیه آیین‌نامه اجرایی قانون تأمین اعتبار احداث، تکمیل، توسعه و تجهیز اماکن ورزشی", "qavanin", "17419052972947847573", None, "اماکن ورزشی", "هیئت وزیران"),
        CatalogItem("D43", "مصوبه شورای عالی اداری: نگهداری و بهره‌برداری از اماکن ورزشی متعلق به دولت", "rc", "1517611", "1398/11/12", "اماکن ورزشی", "شورای عالی اداری"),
        # --- آیین‌نامه‌های تاریخی ورزش ---
        CatalogItem("D44", "آیین‌نامه وظایف، مسئولیت‌ها و نحوه همکاری دستگاه‌های اجرایی در زمینه رشد و شکوفایی ورزش قهرمانی کشور", "rc", "117293", "1376/05/08", "آیین‌نامه تاریخی", "هیئت وزیران"),
        CatalogItem("D45", "آیین‌نامه توسعه فضاهای ورزشی در مناطق روستایی", "qavanin", "1362889610488514370", None, "آیین‌نامه تاریخی", "هیئت وزیران"),
        CatalogItem("D46", "اصلاح ماده (۶) آیین‌نامه چگونگی توسعه و تعمیم ورزش کارمندان دولت", "qavanin", "12161619933739419160", None, "آیین‌نامه تاریخی", "هیئت وزیران"),
        # --- سایر تصویب‌نامه‌ها ---
        CatalogItem("D47", "تصویب‌نامه الزام وزارت ورزش و جوانان به عنوان دستگاه اصلی تعیین اهداف کلان", "rc", "1076952", "1397/06/21", "تصویب‌نامه", "هیئت وزیران"),
        CatalogItem("D48", "مصوبه اصلاح و تکمیل آیین‌نامه کمیسیون فرهنگی تربیت بدنی و ورزش کشور", "rc", "1675328", "1400/06/20", "آیین‌نامه", "هیئت وزیران"),
        CatalogItem("D49", "تعیین بسته اجرایی وزارت ورزش و جوانان موضوع ماده (۲۱۷) قانون برنامه پنجم توسعه", "qavanin", "12586618813603451702", None, "تصویب‌نامه", "هیئت وزیران"),
        CatalogItem("D50", "آیین‌نامه اجرایی بند (ت) ماده (۷۸) قانون برنامه هفتم پیشرفت", "rc", "1835426", "1404/01/17", "آیین‌نامه برنامه هفتم", "هیئت وزیران"),
        # --- تنقیح: قوانین منسوخ ---
        CatalogItem("D51", "قانون فهرست قوانین و احکام نامعتبر در حوزه ورزش", "qavanin", "11316015784636744549", "1400/11/17", "تنقیح منسوخ‌ها", "مجلس شورای اسلامی"),
    ]
    for item in items:
        item.url = _q(item.ref) if item.source == "qavanin" else _rc(item.ref)
    return items


# --------------------------------------------------------------------------
# ArvanCloud challenge (qavanin.ir)
# --------------------------------------------------------------------------

_JS_STRING_BASES = [
    ("([]['fill']+'')", "function fill() { [native code] }"),
    ("([]['entries']()+'')", "[object Array Iterator]"),
    ("([][[]]+[])", "undefined"),
    ("(![]+[])", "false"),
    ("(!![]+[])", "true"),
]


def _js_number(expr: str) -> int:
    """Evaluate JSFuck numeric expressions like ``!+[]+!+[]`` or ``+!+[]``."""
    expr = expr.strip()
    if not expr:
        return 0
    if expr.isdigit():
        return int(expr)
    total = 0
    for token in re.findall(r"\+?!?\+\[\]|!?\+\[\]|\[\]", expr):
        if token in ("", "[]"):
            continue
        if token in ("+!+[]", "!+[]"):
            total += 1
        elif token == "+[]":
            continue
    return total


def _split_top_level(expr: str, sep: str = "+") -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in expr:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _jsfuck_eval(expr: str) -> str:
    """Best-effort evaluator for the JSFuck subset emitted by the ArvanCloud challenge."""
    out = []
    for term in _split_top_level(expr):
        term = term.strip()
        if not term or term == "[]":
            continue
        matched = False
        for pattern, base in _JS_STRING_BASES:
            esc = re.escape(pattern)
            m = re.fullmatch(esc + r"\[(" + ".+".replace("+", r"\+") + r")\]", term)
            if not m:
                m = re.fullmatch(esc + r"\[(.+)\]", term)
            if m:
                idx = _js_number(m.group(1))
                out.append(base[idx] if 0 <= idx < len(base) else "")
                matched = True
                break
        if matched:
            continue
        m = re.fullmatch(r"\(\[\]\+\[\]\)\['constructor'\]\['fromCharCode'\]\((\d+)\)", term)
        if m:
            out.append(chr(int(m.group(1))))
            continue
        m = re.fullmatch(r"\[(.+)\](?:\+\[\])?", term)
        if m and set(m.group(1)) <= set("+![]"):
            out.append(str(_js_number(m.group(1))))
            continue
        if re.fullmatch(r"\d+", term):
            out.append(term)
            continue
        raise ValueError(f"unsupported challenge term: {term[:80]!r}")
    return "".join(out)


def solve_challenge(html: str) -> tuple[str, str]:
    """Return (cookie_header, __arcsjsc) for a challenge page from qavanin.ir."""
    scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)
    challenge = "\n;\n".join(s for s in scripts if "values()" in s or "__arcsjs" in s)
    if not challenge:
        raise RuntimeError("challenge scripts not found in response")
    probe = (
        "var module,exports,define;"
        'var document={addEventListener:function(){},cookie:"",'
        "getElementsByTagName:function(){return[{innerHTML:''}];},"
        "getElementById:function(){return null;}};"
        'var window={Intl:{DateTimeFormat:function(){return{resolvedOptions:function(){'
        'return{timeZone:"UTC"};}};}}};'
    )
    script = (
        f"const challenge={json.dumps(challenge)};\n"
        f"const probe={json.dumps(probe)};\n"
        "const r=new Function(probe+challenge+'\\nreturn [hash_v1, hash];')();\n"
        "console.log(JSON.stringify(r));\n"
    )
    try:
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        result = None
    if result is not None and result.returncode == 0:
        hash_v1, hash_v = json.loads(result.stdout.strip().splitlines()[-1])
        return f"__arcsjs={hash_v1}; __arcsjsc={hash_v}", hash_v
    # Pure-python fallback
    m_v1 = re.search(r"return \{'value_v1':\s*eval\(\"(.*?)\"\),\s*'value':\s*eval\(\"(.*?)\"\)", challenge, re.DOTALL)
    if not m_v1:
        raise RuntimeError("challenge value expressions not found")
    value_v1 = _jsfuck_eval(m_v1.group(1))
    value = _jsfuck_eval(m_v1.group(2))
    return f"__arcsjs={value_v1}; __arcsjsc={value}", value


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Fetcher:
    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay
        self._cookie: str | None = None
        self._cookie_at = 0.0

    def _http_get(self, url: str, cookie: str | None = None) -> str:
        headers = {"User-Agent": UA}
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=40) as response:
            return response.read().decode("utf-8", "replace")

    def get_qavanin(self, url: str, retries: int = 3) -> str:
        for attempt in range(retries):
            cookie = self._cookie if time.time() - self._cookie_at < 60 else None
            html = self._http_get(url, cookie)
            if "Transferring" not in html and ">Runtime Error<" not in html:
                return html
            self._cookie, _ = solve_challenge(html) if "values()" in html else (self._cookie, None)
            self._cookie_at = time.time()
            time.sleep(self.delay)
            if "values()" not in html:
                # not a challenge page (e.g. runtime error) — retry after backoff
                time.sleep(1.5)
        raise RuntimeError(f"qavanin fetch failed after {retries} attempts: {url}")

    def get_rc(self, url: str, retries: int = 3) -> str:
        for attempt in range(retries):
            try:
                html = self._http_get(url)
                if "مرکز پژوهشها - صفحه مورد نظر یافت نشد" not in html:
                    return html
                raise RuntimeError(f"rc page not found: {url}")
            except RuntimeError:
                raise
            except OSError:
                time.sleep(1.0 + attempt)
            except Exception as exc:  # noqa: BLE001 — retry any transient fetch failure
                print(f"rc retry: {exc}")
                time.sleep(1.0 + attempt)
        raise RuntimeError(f"rc fetch failed: {url}")


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def _clean_text(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = text.replace("\u00a0", " ").replace("\u200c", "\u200c")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def extract_qavanin(html: str) -> tuple[str, str | None, str]:
    """Return (title, approve_date_jalali, body_text)."""
    # Title + date live in a bold header paragraph.
    head = re.search(r"<p class='rounddiv'>(.*?)</p>", html, re.DOTALL)
    head_text = _clean_text(head.group(1)) if head else ""
    title = head_text.splitlines()[0].strip() if head_text else ""
    date_match = re.search(r"مصوب\s*([0-9۰-۹]{4})[/.،,]([0-9۰-۹]{1,2})[/.،,]([0-9۰-۹]{1,2})", head_text)
    date = None
    if date_match:
        y, m, d = (_fa_digits(date_match.group(i)) for i in (1, 2, 3))
        date = f"{y}/{int(m):02d}/{int(d):02d}"

    start = html.find("treeText")
    segment = html[start:] if start != -1 else html
    paragraphs = re.findall(r"<p class='SecTex[^>]*>(.*?)</p>", segment, re.DOTALL)
    if not paragraphs:
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", segment, re.DOTALL)
    cleaned = []
    for para in paragraphs:
        para = re.sub(r"<p class='rounddiv'>.*?</p>", "", para, flags=re.DOTALL)
        text = _clean_text(para)
        if text:
            cleaned.append(text)
    body = "\n\n".join(cleaned).strip()
    if not body and head_text:
        body = head_text
    return title, date, body


def _fa_digits(value: str) -> str:
    return value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


def extract_rc_print(html: str) -> tuple[str, str]:
    """Return (title, body_text) from an rc.majlis print_version page."""
    title_match = re.search(r"<title>([^<]*)</title>", html)
    body = _clean_text(html)
    lines = [line for line in body.splitlines() if line.strip()]
    # Drop navigation/footer noise around the legal text.
    start = 0
    for i, line in enumerate(lines):
        if "طرح ها و لوایح" in line or "طرح‌ها و لوایح" in line:
            start = i + 1
            break
    end = len(lines)
    for i, line in enumerate(lines):
        if re.match(r"^(چاپ|بازگشت|Print|Close|بستن)", line.strip()):
            end = i
            break
    content = "\n\n".join(lines[start:end]).strip()
    return (title_match.group(1).strip() if title_match else ""), content


# --------------------------------------------------------------------------
# Markdown corpus builder
# --------------------------------------------------------------------------

PAGE_TARGET_CHARS = 2300


def build_markdown(doc_id: str, title: str, date: str | None, body: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]
    pages: list[list[str]] = []
    current: list[str] = []
    size = 0
    for para in paragraphs:
        para_size = len(para)
        if current and size + para_size > PAGE_TARGET_CHARS:
            pages.append(current)
            current, size = [], 0
        current.append(para)
        size += para_size
    if current:
        pages.append(current)

    lines = [f"# {doc_id} — {title}"]
    if date:
        lines.append(f"<!-- مصوب: {date} -->")
    for number, page in enumerate(pages, start=1):
        lines.append(f"## صفحه {number}")
        lines.append("\n\n".join(page))
    return "\n\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/sport_ministry_corpus")
    parser.add_argument("--only", help="comma separated doc ids (D01,D02)")
    parser.add_argument("--delay", type=float, default=0.6)
    args = parser.parse_args()

    out_dir = Path(args.out)
    md_dir = out_dir / "documents_md"
    meta_dir = out_dir / "metadata"
    for directory in (md_dir, meta_dir):
        directory.mkdir(parents=True, exist_ok=True)

    catalog = _catalog()
    if args.only:
        keep = {item.strip().upper() for item in args.only.split(",")}
        catalog = [item for item in catalog if item.doc_id in keep]

    fetcher = Fetcher(delay=args.delay)
    report: list[dict] = []
    manifest_docs: dict[str, dict] = {}

    for item in catalog:
        t0 = time.time()
        try:
            if item.source == "qavanin":
                html = fetcher.get_qavanin(item.url)
                _page_title, date, body = extract_qavanin(html)
            else:
                html = fetcher.get_rc(item.url)
                _page_title, body = extract_rc_print(html)
                date = item.date
            title = item.title
            ok = len(body) >= 400
            status = "ok" if ok else "thin"
        except Exception as exc:  # noqa: BLE001 — report and continue
            date, body, ok = item.date, "", False
            status = f"error: {exc}"
        elapsed = time.time() - t0

        file_name = f"{item.doc_id}.md"
        if body:
            markdown = build_markdown(item.doc_id, title, date, body)
            (md_dir / file_name).write_text(markdown, encoding="utf-8")
            pages = markdown.count("## صفحه ")
        else:
            pages = 0

        row = {
            "doc_id": item.doc_id,
            "title": title,
            "category": item.category,
            "issuer": item.issuer,
            "date": date or "",
            "source": item.source,
            "ref": item.ref,
            "source_uri": item.url,
            "chars": len(body),
            "pages": pages,
            "status": status,
            "seconds": round(elapsed, 1),
        }
        report.append(row)
        if body:
            manifest_docs[item.doc_id] = {
                "file": file_name,
                "title": title,
                "category": item.category,
                "date": date,
                "pages": pages,
                "source": item.source,
                "source_uri": item.url,
            }
        print(
            f"[{status:>9}] {item.doc_id} {title[:52]:<52} "
            f"chars={len(body):>6} pages={pages:>3} ({elapsed:.1f}s)",
            flush=True,
        )
        time.sleep(args.delay)

    # document_map
    with (meta_dir / "document_map.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest_docs, handle, ensure_ascii=False, indent=1)
    with (meta_dir / "document_map.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["doc_id", "file", "title", "category", "date", "pages", "source", "source_uri"],
        )
        writer.writeheader()
        for doc_id, meta in manifest_docs.items():
            writer.writerow({"doc_id": doc_id, **meta})

    # extraction report
    with (meta_dir / "text_extraction_report.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report[0].keys()) if report else ["doc_id"])
        writer.writeheader()
        writer.writerows(report)

    # manifest
    categories: dict[str, int] = {}
    for meta in manifest_docs.values():
        key = meta["category"].split()[0] if meta["category"] else "سایر"
        categories[key] = categories.get(key, 0) + 1
    manifest = {
        "version": "1.0",
        "corpus": "قوانین، مقررات و آرای حوزه وزارت ورزش ایران (qavanin.ir + rc.majlis.ir)",
        "generated_at": datetime.now(UTC).isoformat(),
        "document_count": len(manifest_docs),
        "categories": categories,
        "documents": manifest_docs,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # checksums
    lines = []
    for path in sorted(out_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(out_dir)}")
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok_count = sum(1 for row in report if row["status"] == "ok")
    print(f"\nDone: {ok_count}/{len(report)} docs fetched OK -> {out_dir}")
    return 0 if ok_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
