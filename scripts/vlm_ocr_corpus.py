#!/usr/bin/env python3
"""Re-OCR scanned legal PDFs with a vision model into parser-ready markdown.

Renders each PDF page with ``pdftoppm``, transcribes it through a VLM
(``VLMOCRAdapter``) and assembles per-document markdown files in the exact
structure the frozen corpus uses (``## صفحه N`` markers), so parser v3
ingests them unchanged. Page numbering follows the PHYSICAL page order —
matching how the original corpus was produced.

Output provenance: every file carries an engine/model/date header block.

Usage (pilot first!):
    LEGAL_AGENT_OCR_* python scripts/vlm_ocr_corpus.py \
        --pdf-dir tests/tamin_longdocs_agentic_benchmark_v4/documents_pdf \
        --out data/tamin_vlm_v2/documents_md --docs D07 --pages 11-13
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legal_agent_core.adapters.vlm_ocr import VLMOCRAdapter
from legal_agent_core.errors import DomainError
from legal_agent_core.ingestion.operations import SourceArtifact


def physical_pages(pdf: Path) -> int:
    out = subprocess.run(
        ["pdfinfo", str(pdf)], capture_output=True, text=True, check=True
    ).stdout
    for line in out.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise SystemExit(f"pdfinfo could not read page count for {pdf}")


def render_page(pdf: Path, page: int, dpi: int) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        prefix = handle.name[:-4]
    try:
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page), str(pdf), prefix],
            check=True,
            capture_output=True,
        )
        produced = sorted(Path(prefix).parent.glob(Path(prefix).name + "*.png"))
        if not produced:
            raise SystemExit(f"pdftoppm produced no image for {pdf} page {page}")
        return produced[0].read_bytes()
    finally:
        for leftover in Path(prefix).parent.glob(Path(prefix).name + "*.png"):
            leftover.unlink(missing_ok=True)


def ocr_page(adapter: VLMOCRAdapter, pdf: Path, page: int, dpi: int) -> str:
    image = render_page(pdf, page, dpi)
    artifact = SourceArtifact(
        source_uri=f"{pdf.name}#page={page}",
        content=image,
        media_type="image/png",
        checksum=f"sha256:{hashlib.sha256(image).hexdigest()}",
    )
    return adapter.extract(artifact).text


def parse_page_spec(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(1, total + 1))
    pages: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            pages.extend(range(int(lo), int(hi) + 1))
        else:
            pages.append(int(part))
    return [page for page in pages if 1 <= page <= total]


def process_document(
    adapter: VLMOCRAdapter,
    pdf: Path,
    out_dir: Path,
    pages_spec: str | None,
    dpi: int,
    workers: int,
) -> Path:
    doc_code = pdf.stem.upper()
    total = physical_pages(pdf)
    pages = parse_page_spec(pages_spec, total)
    print(f"=== {doc_code}: {len(pages)}/{total} pages ===", flush=True)

    results: dict[int, str] = {}
    if os.path.exists(out_dir / f"{doc_code}.md") and pages_spec is None:
        print(f"  skip — {out_dir / (doc_code + '.md')} exists (delete to redo)", flush=True)
        return out_dir / f"{doc_code}.md"

    def run(page: int) -> tuple[int, str]:
        for attempt in range(3):
            try:
                return page, ocr_page(adapter, pdf, page, dpi)
            except (DomainError, subprocess.SubprocessError) as exc:
                if attempt == 2:
                    print(f"  !! {doc_code} p{page} failed: {exc}", flush=True)
                    return page, f"[OCR-FAILED: {exc}]"
                time.sleep(2 * (attempt + 1))
        raise AssertionError

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for page, text in pool.map(run, pages):
            results[page] = text
            print(f"  p{page:>3} ok ({len(text)} chars)", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {doc_code} — VLM re-OCR",
        "",
        f"- Source PDF: `{pdf.name}`",
        f"- Physical pages: {total}",
        f"- Extraction: VLM OCR (`{adapter.model}` via {adapter.base_url}), verbatim prompt",
        f"- Generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
    ]
    for page in sorted(results):
        lines.append(f"## صفحه {page}")
        lines.append("")
        lines.append(results[page].strip())
        lines.append("")
    out_file = out_dir / f"{doc_code}.md"
    out_file.write_text("\n".join(lines), encoding="utf-8")
    digest = hashlib.sha256(out_file.read_bytes()).hexdigest()
    print(f"  wrote {out_file} (sha256 {digest[:16]}…)", flush=True)
    return out_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", default="tests/tamin_longdocs_agentic_benchmark_v4/documents_pdf")
    parser.add_argument("--out", default="data/tamin_vlm_v2/documents_md")
    parser.add_argument("--docs", nargs="*", default=None, help="document codes, e.g. D07 D03 (default: all PDFs)")
    parser.add_argument("--pages", default=None, help="e.g. 11-13 or 1,2,5 (applies to every selected doc)")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    adapter = VLMOCRAdapter.from_env()
    pdf_dir = Path(args.pdf_dir)
    pdfs = (
        [pdf_dir / f"{code}.pdf" for code in args.docs]
        if args.docs
        else sorted(pdf_dir.glob("*.pdf"))
    )
    missing = [pdf for pdf in pdfs if not pdf.is_file()]
    if missing:
        raise SystemExit(f"missing PDFs: {', '.join(str(p) for p in missing)}")

    manifest = {"generated": datetime.now(UTC).isoformat(timespec="seconds"), "engine": adapter.model, "documents": {}}
    out_dir = Path(args.out)
    for pdf in pdfs:
        started = time.monotonic()
        out_file = process_document(adapter, pdf, out_dir, args.pages, args.dpi, args.workers)
        manifest["documents"][pdf.stem.upper()] = {
            "file": out_file.name,
            "sha256": hashlib.sha256(out_file.read_bytes()).hexdigest(),
            "seconds": round(time.monotonic() - started, 1),
        }
        (out_dir.parent / "manifest_vlm.json").parent.mkdir(parents=True, exist_ok=True)
        (out_dir.parent / "manifest_vlm.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print("ALL DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
