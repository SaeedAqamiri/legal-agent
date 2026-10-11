#!/usr/bin/env python3
"""Fetch the PDF linked from an ekhtebar document and enrich the corpus.

Some ekhtebar pages are only a transmittal letter / stub whose real content
lives in a linked PDF (e.g. «بخشنامه تعرفه حق‌التحریر …» points at
``bakhshname.pdf``). The scraper stored just the page body, so
``ingest_cats_data.py`` fell back to a single «متن» provision holding the
letter and the real tables/annex were never ingested.

For every such document this script:
  1. extracts the PDF URL from the stored markdown
  2. downloads the PDF (cached under ``<data-dir>/linked-pdf/<slug>.pdf``)
  3. extracts text with ``pdftotext``; scanned PDFs fall back to VLM OCR
     (``VLMOCRAdapter``), assembling ``## صفحه N`` page markers
  4. writes an enriched markdown (original page + extracted annex) under
     ``<data-dir>/linked-pdf-text/<slug>.md`` and points the source document
     at both the PDF and the enriched text
  5. appends the extracted annex to the canonical instrument as a second
     provision (append-only: the old provision is kept, nothing is mutated)
  6. regenerates the provision + instrument summaries (optional)

Idempotent: a document is skipped once its source row carries
``pdf_enriched_at`` (override with ``--force``); the pipeline's content
addressing makes re-runs no-ops.

Usage:
    LEGAL_AGENT_OCR_BASE_URL=https://api.avalai.ir/v1 \
    LEGAL_AGENT_OCR_API_KEY=... LEGAL_AGENT_OCR_MODEL=qwen3.8-flash \
    LEGAL_AGENT_POSTGRES_DSN=... \
        .venv/bin/python scripts/fetch_linked_pdfs.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from legal_agent_core.adapters.postgres import PostgresCanonicalRepository
from legal_agent_core.canonical import DocumentStatus, InstrumentType, ProvisionType
from legal_agent_core.errors import DomainError
from legal_agent_core.ingestion.models import (
    DocumentVersionInput,
    InstrumentInput,
    ParsedDocument,
    ProvisionInput,
    SourceInput,
)
from legal_agent_core.ingestion.normalization import normalize_legal_text
from legal_agent_core.ingestion.pipeline import CanonicalIngestionPipeline

DEFAULT_DATA_DIR = Path("~/laws-mcp/data").expanduser()
DEFAULT_DSN = "postgresql://legal_agent:legal_agent@localhost:5432/legal_agent"
SOURCE_ID = "ekhtebar"
USER_AGENT = "Mozilla/5.0 (legal-agent linked-pdf enrichment; contact: local)"
PDF_URL_RE = re.compile(r"https?://[^\s)\"']+\.pdf", re.IGNORECASE)
PDF_LINE_RE = re.compile(r"^>\s*پی[\u200c ]?دی[\u200c ]?اف[:：]?\s*(\S+)", re.MULTILINE)
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)

#: instruments whose canonical text is still the whole-markdown fallback
DISCOVERY_SQL = """
SELECT i.instrument_id, i.title, i.instrument_type,
       sd.document_uid, sd.external_id, sd.source_uri, sd.text_path, sd.metadata
  FROM canonical.legal_instruments i
  JOIN sources.instruments si ON si.canonical_key = i.canonical_title
  JOIN sources.source_documents sd ON sd.instrument_uid = si.instrument_uid
 WHERE sd.source_id = %s AND sd.text_path IS NOT NULL
 ORDER BY i.instrument_id
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def slug_of(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    return urllib.parse.unquote(tail) or "doc"


def pdf_url_in(markdown: str) -> str | None:
    line = PDF_LINE_RE.search(markdown)
    if line:
        return line.group(1).strip()
    match = PDF_URL_RE.search(markdown)
    return match.group(0) if match else None


def _quality(text: str) -> tuple[int, float]:
    collapsed = re.sub(r"\s+", " ", text)
    letters = [ch for ch in collapsed if ch.isalpha()]
    n = max(len(letters), 1)
    fa = sum(1 for ch in letters if "\u0600" <= ch <= "\u06ff") / n
    return len(collapsed), fa


def needs_ocr(text: str) -> bool:
    n, fa = _quality(text)
    return n < 200 or fa < 0.4


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def download(url: str, dest: Path, delay: float) -> bool:
    if dest.exists() and dest.stat().st_size > 1000:
        return True
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if len(data) < 1000 or not data[:5].startswith(b"%PDF"):
        raise ValueError(f"not a PDF ({len(data)} bytes)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    time.sleep(delay)
    return True


def pdftotext(pdf: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True,
        timeout=300,
    )
    return result.stdout.decode("utf-8", errors="replace")


def pdf_pages(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=60).stdout
    match = re.search(r"Pages:\s*(\d+)", out)
    return int(match.group(1)) if match else 0


#: verbatim Persian transcription prompt (same spirit as VLMOCRAdapter)
OCR_PROMPT = (
    "رونویسیِ دقیق (verbatim) متن این صفحه‌ی سند حقوقی فارسی. قاعده‌ها: "
    "۱) هر خط را همان‌طور که هست بنویس؛ واژه‌های ناخوانا را [ناخوانا] علامت بزن، حدس نزن. "
    "۲) شماره‌ها و تاریخ‌ها را دقیق رونویسی کن. "
    "۳) هیچ توضیح، مقدمه یا markdown اضافه ننویس."
)


class OCRClient:
    """Minimal OpenAI-compatible vision-OCR client with thinking/effort control."""

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 thinking: str | None = None, effort: str | None = None,
                 max_tokens: int = 8192, timeout: float = 180.0) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise DomainError("OCR base_url must be an http(s) endpoint")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.thinking = thinking
        self.effort = effort
        self.max_tokens = max_tokens
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> OCRClient:
        base_url = os.environ.get("LEGAL_AGENT_OCR_BASE_URL") or os.environ.get("LEGAL_AGENT_LLM_BASE_URL")
        api_key = os.environ.get("LEGAL_AGENT_OCR_API_KEY") or os.environ.get("LEGAL_AGENT_LLM_API_KEY")
        model = os.environ.get("LEGAL_AGENT_OCR_MODEL", "qwen3.8-flash")
        if not base_url or not api_key:
            raise DomainError("OCR needs LEGAL_AGENT_OCR_BASE_URL/LEGAL_AGENT_OCR_API_KEY")
        return cls(
            base_url, api_key, model,
            thinking=os.environ.get("LEGAL_AGENT_OCR_THINKING") or None,
            effort=os.environ.get("LEGAL_AGENT_OCR_EFFORT", "medium") or None,
            max_tokens=int(os.environ.get("LEGAL_AGENT_OCR_MAX_TOKENS", "16000")),
            timeout=float(os.environ.get("LEGAL_AGENT_OCR_TIMEOUT", "240")),
        )

    def extract(self, image: bytes) -> str:
        encoded = base64.b64encode(image).decode("ascii")
        payload: dict = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                {"type": "text", "text": OCR_PROMPT},
            ]}],
        }
        if self.thinking:
            payload["thinking"] = {"type": self.thinking}
        if self.effort:
            payload["reasoning_effort"] = self.effort
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        message = (data.get("choices") or [{}])[0].get("message") or {}
        text = (message.get("content") or message.get("reasoning_content") or "").strip()
        if not text:
            raise DomainError("empty OCR content (reasoning budget or provider issue)")
        return text


def render_page(pdf: Path, page: int, dpi: int) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        prefix = str(Path(tmp) / "pg")
        subprocess.run(
            ["pdftoppm", "-jpeg", "-jpegopt", "quality=85", "-r", str(dpi),
             "-f", str(page), "-l", str(page), str(pdf), prefix],
            check=True,
            capture_output=True,
            timeout=300,
        )
        produced = sorted(Path(tmp).glob("pg*.jpg"))
        if not produced:
            raise RuntimeError(f"pdftoppm produced no image for {pdf.name} page {page}")
        return produced[0].read_bytes()


def ocr_page(client: OCRClient, pdf: Path, page: int, dpi: int, attempts: int = 8) -> str:
    """Render one page and transcribe it, backing off on provider rate limits."""
    image = render_page(pdf, page, dpi)
    for attempt in range(attempts):
        try:
            return client.extract(image)
        except (DomainError, urllib.error.HTTPError, TimeoutError) as exc:
            if attempt == attempts - 1:
                raise
            wait = min(15 * (attempt + 1), 60)
            log(f"    OCR p{page} retry {attempt + 1}/{attempts - 1} after {wait}s ({str(exc)[:60]})")
            time.sleep(wait)
    raise AssertionError


def ocr_pdf(client: OCRClient, pdf: Path, dpi: int, workers: int, max_pages: int) -> str:
    total = pdf_pages(pdf)
    pages = list(range(1, total + 1))
    if max_pages:
        pages = pages[:max_pages]

    def run(page: int) -> tuple[int, str]:
        return page, ocr_page(client, pdf, page, dpi)

    results: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for page, text in pool.map(run, pages):
            results[page] = text
            log(f"    OCR {pdf.stem} p{page}/{len(pages)} ({len(text)} chars)")
    return "\n\n".join(f"## صفحه {page}\n\n{results[page].strip()}" for page in sorted(results))


def extract_annex(pdf: Path, args: argparse.Namespace) -> tuple[str, str]:
    """Return (extracted_text, method)."""
    text = pdftotext(pdf)
    if not needs_ocr(text):
        return text, "pdftotext"
    client = OCRClient.from_env()
    method = f"vlm_ocr:{client.model}"
    if client.effort:
        method += f":{client.effort}"
    return ocr_pdf(client, pdf, args.dpi, args.ocr_workers, args.max_pages), method


def is_fallback(db: psycopg.Connection, instrument_id: str) -> bool:
    row = db.execute(
        "SELECT count(*), bool_and(p.label = 'متن') FROM canonical.provisions p WHERE p.instrument_id = %s",
        (instrument_id,),
    ).fetchone()
    return bool(row and row[0] == 1 and row[1])


def attachment_label(markdown: str) -> str:
    headings = HEADING_RE.findall(markdown)
    return headings[-1].strip() if headings else "پیوست"


def instrument_type_of(db: psycopg.Connection, instrument_id: str) -> InstrumentType:
    raw = db.execute(
        "SELECT instrument_type FROM canonical.legal_instruments WHERE instrument_id = %s",
        (instrument_id,),
    ).fetchone()
    try:
        return InstrumentType(raw[0]) if raw else InstrumentType.OTHER
    except ValueError:
        return InstrumentType.OTHER


def append_annex(
    repo: PostgresCanonicalRepository,
    pipeline: CanonicalIngestionPipeline,
    *,
    instrument_id: str,
    instrument_type: InstrumentType,
    title: str,
    slug: str,
    source_uri: str,
    md_path: Path,
    annex_text: str,
    label: str,
) -> tuple[str, ...]:
    checksum = sha256_bytes(md_path.read_bytes())
    parsed = ParsedDocument(
        source=SourceInput(
            filename=md_path.name,
            media_type="text/markdown",
            checksum=checksum,
            file_size=md_path.stat().st_size,
            ingested_at=datetime.now(UTC),
            ingested_by="linked_pdf_enrich",
            source_uri=source_uri,
        ),
        instrument=InstrumentInput(
            instrument_id=instrument_id,
            title=title,
            canonical_title=normalize_legal_text(title),
            instrument_type=instrument_type,
            jurisdiction="IR",
            external_identifier=slug,
        ),
        version=DocumentVersionInput(status=DocumentStatus.PUBLISHED),
        provisions=(
            ProvisionInput(
                provision_type=ProvisionType.PARAGRAPH,
                label=label,
                text=annex_text.strip(),
                page_number=1,
                ordinal=1,
                title=label,
                created_from="linked-pdf-enrich",
            ),
        ),
    )
    result = pipeline.ingest(parsed)
    repo.connection.commit()
    return result.provision_ids


def run_summaries(instrument_id: str, dsn: str) -> bool:
    scripts = Path(__file__).resolve().parent
    steps = [
        [sys.executable, str(scripts / "summarize_provisions.py"), "--dsn", dsn,
         "--instrument", instrument_id, "--workers", "1"],
        [sys.executable, str(scripts / "summarize_instruments.py"), "--dsn", dsn,
         "--instrument", instrument_id],
    ]
    for step in steps:
        proc = subprocess.run(step, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            log(f"    summary step failed: {proc.stderr.strip()[:200]}")
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("LAWS_DATA_DIR", DEFAULT_DATA_DIR)))
    parser.add_argument("--dsn", default=os.environ.get("LEGAL_AGENT_POSTGRES_DSN", DEFAULT_DSN))
    parser.add_argument("--report", type=Path, default=Path("out/linked_pdfs_report.json"))
    parser.add_argument("--instrument", default=None, help="process only this instrument_id")
    parser.add_argument("--shard", default=None, help="i/n: process only shard i of n (parallel runs)")
    parser.add_argument("--limit", type=int, default=0, help="max documents (0 = all)")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--ocr-workers", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=0, help="cap OCR pages per PDF (0 = all)")
    parser.add_argument("--force", action="store_true", help="reprocess already-enriched docs")
    parser.add_argument("--no-summaries", action="store_true", help="skip summary regeneration")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pdf_dir = args.data_dir / "linked-pdf"
    text_dir = args.data_dir / "linked-pdf-text"

    repo = PostgresCanonicalRepository.connect(args.dsn, migrate=True)
    pipeline = CanonicalIngestionPipeline(repo)
    db = psycopg.connect(args.dsn, autocommit=True)

    rows = db.execute(DISCOVERY_SQL, (SOURCE_ID,)).fetchall()
    targets: dict[str, tuple] = {}
    for instrument_id, title, itype, doc_uid, external_id, source_uri, text_path, metadata in rows:
        if args.instrument and instrument_id != args.instrument:
            continue
        targets.setdefault(instrument_id, (title, itype, doc_uid, external_id, source_uri, text_path, metadata))
    log(f"candidate documents: {len(targets)}")

    if args.shard:
        shard_i, shard_n = (int(x) for x in args.shard.split("/"))
        ordered = sorted(targets.items())
        targets = dict(ordered[shard_i::shard_n])
        log(f"shard {shard_i}/{shard_n}: {len(targets)} documents")

    report: dict[str, dict] = {}
    stats = {"processed": 0, "skipped_done": 0, "skipped_not_fallback": 0,
             "skipped_no_pdf": 0, "failed": 0, "ocr": 0, "pdftotext": 0}
    started = time.time()

    for n, (instrument_id, (title, itype, doc_uid, external_id, source_uri, text_path, metadata)) in enumerate(
        sorted(targets.items()), start=1
    ):
        metadata = metadata or {}
        if metadata.get("pdf_enriched_at") and not args.force:
            stats["skipped_done"] += 1
            continue
        if not is_fallback(db, instrument_id):
            stats["skipped_not_fallback"] += 1
            continue
        md_path = Path(text_path)
        if not md_path.exists():
            report[instrument_id] = {"title": title, "status": "failed", "error": "markdown missing"}
            stats["failed"] += 1
            continue
        original = md_path.read_text(encoding="utf-8", errors="replace")
        pdf_url = pdf_url_in(original)
        if not pdf_url:
            stats["skipped_no_pdf"] += 1
            continue

        slug = slug_of(source_uri) if source_uri else (external_id or slug_of(pdf_url))
        log(f"[{n}/{len(targets)}] {title[:60]}")
        if args.limit and stats["processed"] >= args.limit:
            break
        if args.dry_run:
            log(f"    DRY  would fetch {pdf_url[:90]}")
            stats["processed"] += 1
            continue

        pdf_dst = pdf_dir / f"{slug}.pdf"
        try:
            download(pdf_url, pdf_dst, args.delay)
            annex_text, method = extract_annex(pdf_dst, args)
            if not annex_text.strip():
                raise ValueError("no text extracted from PDF")
            stats["ocr" if method.startswith("vlm_ocr") else "pdftotext"] += 1

            enriched = original.rstrip() + "\n\n" + annex_text.strip() + "\n"
            md_dst = text_dir / f"{slug}.md"
            md_dst.parent.mkdir(parents=True, exist_ok=True)
            md_dst.write_text(enriched, encoding="utf-8")

            db.execute(
                """UPDATE sources.source_documents
                      SET original_path = %s, text_path = %s, text_extract_method = %s,
                          metadata = metadata || %s, last_seen_at = now()
                    WHERE document_uid = %s""",
                (
                    str(pdf_dst), str(md_dst), method.split(":")[0],
                    json.dumps({
                        "pdf_url": pdf_url,
                        "pdf_text_path": str(md_dst),
                        "pdf_extract_method": method,
                        "pdf_enriched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    }, ensure_ascii=False),
                    str(doc_uid),
                ),
            )

            provision_ids = append_annex(
                repo, pipeline,
                instrument_id=instrument_id,
                instrument_type=instrument_type_of(db, instrument_id),
                title=title, slug=slug, source_uri=source_uri or pdf_url,
                md_path=md_dst, annex_text=annex_text,
                label=attachment_label(original),
            )
            summarized = True
            if not args.no_summaries:
                summarized = run_summaries(instrument_id, args.dsn)

            stats["processed"] += 1
            report[instrument_id] = {
                "title": title, "status": "ok", "method": method,
                "pdf": str(pdf_dst), "text": str(md_dst),
                "annex_chars": len(annex_text), "provisions_added": list(provision_ids),
                "summarized": summarized,
            }
            log(f"    OK  {method} | annex {len(annex_text)} chars | provisions +{len(provision_ids)}")
        except urllib.error.HTTPError as exc:
            stats["failed"] += 1
            report[instrument_id] = {"title": title, "status": "failed", "error": f"HTTP {exc.code}"}
            log(f"    FAIL HTTP {exc.code}: {pdf_url[:80]}")
        except Exception as exc:  # noqa: BLE001 — one bad doc must not stop the batch
            stats["failed"] += 1
            report[instrument_id] = {"title": title, "status": "failed",
                                     "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            log(f"    FAIL {type(exc).__name__}: {str(exc)[:120]}")

    elapsed = time.time() - started
    report["_summary"] = {"generated": datetime.now(UTC).isoformat(timespec="seconds"),
                          "data_dir": str(args.data_dir), "seconds": round(elapsed, 1), **stats}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"done in {elapsed:.0f}s: {json.dumps(stats, ensure_ascii=False)}")
    log(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
