#!/usr/bin/env python3
"""Integrate the user-provided sport-law collection into the corpus.

Input:  data/sport_ministry_corpus/user_provided/iran-legal-sport.md
        (24 documents converted from ``قوانین وزارت ورزش.xlsx`` by the user)

Output: documents_md/D52..D75.md plus updated manifest.json, document_map,
        text_extraction_report.csv, SHA256SUMS.txt and cross_references.json.

Documents that duplicate an already-fetched official document are kept as
variants (useful for temporal/versioning tests) and linked through
``cross_references.json``.

Usage:
    python scripts/build_sport_corpus_from_user_md.py [--out data/sport_ministry_corpus]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_sport_ministry_corpus import build_markdown

# law-NN -> (doc_id, category, issuer, date, relation)
USER_CATALOG: dict[str, dict] = {
    "law-01": {"doc_id": "D52", "category": "قانون اساسی", "issuer": "مجلس خبرگان قانون اساسی", "date": "1358/11/24", "relation": "اصول حاکم بر ورزش (تربیت بدنی رایگان، جزء ۳)"},
    "law-02": {"doc_id": "D53", "category": "قانون انتقال", "issuer": "مجلس شورای اسلامی", "date": "1389/10/12", "relation": {"type": "variant_of", "doc": "D03", "note": "متن کامل با جمله پایانی تصویب/تأیید شورای نگهبان"}},
    "law-03": {"doc_id": "D54", "category": "قانون جاری", "issuer": "مجلس شورای اسلامی", "date": "1399/06/16", "relation": {"type": "variant_of", "doc": "D01", "note": "نسخه تأیید شورای نگهبان ۱۳۹۹/۰۷/۰۲"}},
    "law-04": {"doc_id": "D55", "category": "قانون مرتبط", "issuer": "مجلس شورای اسلامی", "date": "1373/04/19", "relation": {"type": "related_to", "docs": ["D06", "D07", "D09", "D69", "D72"], "note": "المپیک (بند ۷)، پارالمپیک (الحاقی ۱۳۹۵ بند ۲۱)، فدراسیون‌های آماتوری (الحاقی ۱۳۷۶ بند ۱۱)، صندوق قهرمانان (الحاقی ۱۳۹۹ بند ۲۴) عضو نهادهای عمومی غیردولتی‌اند"}},
    "law-05": {"doc_id": "D56", "category": "تاریخی باشگاه‌ها", "issuer": "مجلس شورای اسلامی", "date": "1369/10/26", "relation": {"type": "upgrade_of", "doc": "D31", "note": "متن کامل با تبصره‌های ۱ تا ۴ و اصلاحی ۱۳۹۰"}},
    "law-06": {"doc_id": "D57", "category": "آیین‌نامه اماکن", "issuer": "هیئت وزیران", "date": "1393/12/13", "relation": {"type": "duplicate_of", "doc": "D68", "note": "آیین‌نامه اجرایی ماده ۵ قانون الحاق (۲) با اصلاحی ۱۳۹۶/۰۳/۲۱"}},
    "law-07": {"doc_id": "D58", "category": "آیین‌نامه برنامه هفتم", "issuer": "هیئت وزیران", "date": "1404", "relation": {"type": "variant_of", "doc": "D50"}},
    "law-08": {"doc_id": "D59", "category": "دستورالعمل واگذاری", "issuer": "شورای اقتصاد", "date": None, "relation": {"type": "related_to", "docs": ["D49"], "note": "چارچوب واگذاری پروژه‌های تملک دارایی سرمایه‌ای به بخش غیردولتی"}},
    "law-09": {"doc_id": "D60", "category": "اماکن ورزشی", "issuer": "مجلس شورای اسلامی", "date": None, "relation": {"type": "related_to", "docs": ["D41", "D43"], "note": "مالکیت دولت بر املاک دستگاه‌ها و واگذاری حق استفاده"}},
    "law-10": {"doc_id": "D61", "category": "قانون اصلاحی", "issuer": "مجلس شورای اسلامی", "date": "1384/08/11", "relation": {"type": "related_to", "docs": ["D60"], "note": "افزودن «مناطق کمتر توسعه یافته و دارندگان سرانه‌های ورزشی زیر میانگین کشوری» بعد از عبارت تربیت بدنی در ماده ۸۸"}},
    "law-11": {"doc_id": "D62", "category": "سرباز قهرمان", "issuer": "مجلس شورای اسلامی", "date": "1376/12/24", "relation": {"type": "variant_of", "doc": "D34", "note": "متن تنقیح‌شده با نشان‌گذاری اصلاحی‌ها ۱۳۸۰ و ۱۳۹۱"}},
    "law-12": {"doc_id": "D63", "category": "سرباز قهرمان", "issuer": "مجلس شورای اسلامی", "date": "1391/10/12", "relation": {"type": "related_to", "docs": ["D62", "D34"], "note": "قانون مستقل اصلاح سرباز قهرمان؛ جایگزین تبصره ۴ و افزودن تبصره‌های ۵ و ۶"}},
    "law-13": {"doc_id": "D64", "category": "آیین‌نامه جاری", "issuer": "هیئت وزیران", "date": "1402/01/16", "relation": {"type": "variant_of", "doc": "D02"}},
    "law-14": {"doc_id": "D65", "category": "آیین‌نامه برنامه هفتم", "issuer": "هیئت وزیران", "date": "1404", "relation": {"type": "duplicate_of", "doc": "D58", "note": "همان آیین‌نامه بند (ت) ماده ۷۸ با عنوان رسمی‌تر"}},
    "law-15": {"doc_id": "D66", "category": "فدراسیون‌ها هیئت‌ها", "issuer": "وزارت ورزش و جوانان", "date": "1400/04/19", "relation": {"type": "related_to", "docs": ["D69", "D11"], "note": "مصوب در اجرای ماده ۳۴ اساسنامه ۱۴۰۰ فدراسیون‌های آماتوری"}},
    "law-16": {"doc_id": "D67", "category": "باشگاه‌ها", "issuer": "هیئت وزیران", "date": "1397/03/13", "relation": {"type": "related_to", "docs": ["D56", "D31"], "note": "اصلاح صدر ماده ۵ آیین‌نامه اجرایی قانون تأسیس باشگاه (تصویب‌نامه ۱۳۷۰)"}},
    "law-17": {"doc_id": "D68", "category": "آیین‌نامه اماکن", "issuer": "هیئت وزیران", "date": "1393/12/13", "relation": {"type": "duplicate_of", "doc": "D57"}},
    "law-18": {"doc_id": "D69", "category": "فدراسیون‌ها نسخه ۱۴۰۰", "issuer": "هیئت وزیران", "date": "1400/04/19", "relation": {"type": "variant_of", "doc": "D11", "note": "متن کامل ۳۶ ماده با یادداشت تأیید شورای نگهبان ۱۴۰۰/۰۴/۱۲؛ زنجیره نسخه‌ها: D09 (۱۳۸۱) → D10 (الحاق ۱۳۹۲) → D69 (۱۴۰۰)"}},
    "law-19": {"doc_id": "D70", "category": "فدراسیون تنیس", "issuer": "فدراسیون تنیس / وزارت ورزش", "date": "1400", "relation": {"type": "related_to", "docs": ["D69"], "note": "نمونه فدراسیون‌محور از دستورالعمل موضوع تبصره ۲ ماده ۱۴ اساسنامه ۱۴۰۰"}},
    "law-20": {"doc_id": "D71", "category": "اماکن ورزشی", "issuer": "هیئت وزیران", "date": "1383/02/16", "relation": {"type": "related_to", "docs": ["D43"], "note": "انتقال مجموعه‌های شهید شیرودی، انقلاب و آزادی به شرکت توسعه و نگهداری اماکن ورزشی"}},
    "law-21": {"doc_id": "D72", "category": "نهاد ورزشی", "issuer": "هیئت وزیران", "date": "1403", "relation": {"type": "related_to", "docs": ["D54", "D55"], "note": "اساسنامه صندوق موضوع ماده ۶ قانون اهداف و بند ۲۴ الحاقی ۱۳۹۹ قانون فهرست نهادها"}},
    "law-22": {"doc_id": "D73", "category": "ورزش تیراندازی", "issuer": "وزارت ورزش / وزارت دفاع", "date": None, "relation": {"type": "related_to", "docs": ["D54"], "note": "خلاصه توصیفی (نه متن رسمی) تبصره بند ۱۱ ماده ۴ قانون اهداف"}},
    "law-23": {"doc_id": "D74", "category": "امور جوانان", "issuer": "هیئت وزیران", "date": "1386/10/26", "relation": {"type": "related_to", "docs": ["D54"], "note": "ترکیب اعضای ستاد ملی ساماندهی امور جوانان"}},
    "law-24": {"doc_id": "D75", "category": "فدراسیون‌ها هیئت‌ها", "issuer": "وزارت ورزش و جوانان", "date": None, "relation": {"type": "related_to", "docs": ["D66", "D69"], "note": "متن در فایل مبدأ خالی است — نیازمند گردآوری از منبع رسمی"}},
}

QUALITY_NOTES = {
    "law-22": "summary: خلاصه توصیفی است نه متن رسمی دستورالعمل",
    "law-24": "empty: متن در فایل مبدأ خالی است",
}


def parse_user_file(path: Path) -> dict[str, dict]:
    """Split the markdown into law-NN sections with title and body."""
    content = path.read_text(encoding="utf-8")
    parts = re.split(r'<a id="law-(\d+)"></a>', content)
    sections: dict[str, dict] = {}
    for index in range(1, len(parts), 2):
        law_id = f"law-{parts[index].zfill(2)}"
        body = parts[index + 1]
        body = body.split("\n---", 1)[0]
        title_match = re.search(r"^##\s+(.+?)\s*$", body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else law_id
        text = re.sub(r"^##\s+.+?\n", "", body, count=1, flags=re.MULTILINE).strip()
        sections[law_id] = {"title": title, "text": text}
    return sections


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/sport_ministry_corpus")
    args = parser.parse_args()

    out_dir = Path(args.out)
    md_dir = out_dir / "documents_md"
    meta_dir = out_dir / "metadata"
    source_file = out_dir / "user_provided" / "iran-legal-sport.md"
    if not source_file.is_file():
        print(f"input not found: {source_file}")
        return 2

    sections = parse_user_file(source_file)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc_map_path = meta_dir / "document_map.json"
    doc_map = json.loads(doc_map_path.read_text(encoding="utf-8"))
    report_path = meta_dir / "text_extraction_report.csv"
    with report_path.open(encoding="utf-8", newline="") as handle:
        report = list(csv.DictReader(handle))
    known_ids = {row["doc_id"] for row in report}

    cross_refs: list[dict] = []
    added = 0
    for law_id, meta in USER_CATALOG.items():
        doc_id = meta["doc_id"]
        section = sections.get(law_id)
        if section is None:
            print(f"[missing] {law_id} not present in source file")
            continue
        title = section["title"]
        text = section["text"]
        quality = QUALITY_NOTES.get(law_id, "verbatim")
        if len(text.strip()) < 150 or quality.startswith("empty"):
            manifest_docs = manifest["documents"]
            if doc_id in known_ids:
                continue
            print(f"[empty ] {doc_id} {title[:60]} (metadata-only, no md file)")
            manifest_docs[doc_id] = {
                "file": None,
                "title": title,
                "category": meta["category"],
                "date": meta["date"],
                "pages": 0,
                "source": "user_xlsx",
                "source_uri": f"user_provided/iran-legal-sport.md#{law_id}",
                "quality": quality,
            }
            report.append({
                "doc_id": doc_id, "title": title, "category": meta["category"],
                "issuer": meta["issuer"], "date": meta["date"] or "",
                "source": "user_xlsx", "ref": law_id,
                "source_uri": f"user_provided/iran-legal-sport.md#{law_id}",
                "chars": str(len(text)), "pages": "0", "status": "empty", "seconds": "0",
            })
            continue

        markdown = build_markdown(doc_id, title, meta["date"], text)
        file_name = f"{doc_id}.md"
        (md_dir / file_name).write_text(markdown, encoding="utf-8")
        pages = markdown.count("## صفحه ")
        if doc_id in known_ids:
            print(f"[skip  ] {doc_id} already present")
            continue
        manifest["documents"][doc_id] = {
            "file": file_name,
            "title": title,
            "category": meta["category"],
            "date": meta["date"],
            "pages": pages,
            "source": "user_xlsx",
            "source_uri": f"user_provided/iran-legal-sport.md#{law_id}",
            "quality": quality,
        }
        doc_map[doc_id] = manifest["documents"][doc_id]
        report.append({
            "doc_id": doc_id, "title": title, "category": meta["category"],
            "issuer": meta["issuer"], "date": meta["date"] or "",
            "source": "user_xlsx", "ref": law_id,
            "source_uri": f"user_provided/iran-legal-sport.md#{law_id}",
            "chars": str(len(text)), "pages": str(pages), "status": "ok", "seconds": "0",
        })
        relation = meta.get("relation") or {}
        entry = {"from": doc_id, "to": None, "type": None, "note": None}
        if isinstance(relation, dict) and relation.get("type"):
            entry["type"] = relation["type"]
            if relation.get("doc"):
                entry["to"] = relation["doc"]
            elif relation.get("docs"):
                entry["to"] = relation["docs"]
            entry["note"] = relation.get("note")
        cross_refs.append({"law_id": law_id, **entry, "category": meta["category"]})
        added += 1
        print(f"[ok    ] {doc_id} {title[:60]} pages={pages}")

    # D03's true approval date is confirmed by the user copy (1389/10/12).
    if "D03" in manifest["documents"]:
        manifest["documents"]["D03"]["date"] = "1389/10/12"
        doc_map.setdefault("D03", manifest["documents"]["D03"])["date"] = "1389/10/12"

    # regenerate categories/counts
    categories: dict[str, int] = {}
    for meta_ in manifest["documents"].values():
        key = (meta_.get("category") or "سایر").split()[0]
        categories[key] = categories.get(key, 0) + 1
    manifest["categories"] = categories
    manifest["document_count"] = len(manifest["documents"])

    (meta_dir / "cross_references.json").write_text(
        json.dumps(cross_refs, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    doc_map_path.write_text(
        json.dumps(doc_map, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(report[0].keys()) if report else ["doc_id"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report)

    lines = []
    for path in sorted(out_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(out_dir)}")
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nAdded {added} documents from user collection; corpus total: {len(manifest['documents'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
