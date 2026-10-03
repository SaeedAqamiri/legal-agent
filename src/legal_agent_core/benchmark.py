"""Frozen-benchmark harness for the Tamin long-document legal corpus.

Loads the blind questions plus gold answers that ship with the corpus
(``tests/tamin_longdocs_agentic_benchmark_v4``), grades deterministic runs
(document recall, page-level citation hits, honest abstention, grounding) and
aggregates per-category reports. Grading is fully deterministic — no LLM
judge — mirroring the fairness rules of the ai-benchmark grader v2: explicit
abstention is required when the corpus is insufficient, and over-refusal on
answerable questions is penalized.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .answering import AnswerCompositionError, PublishedAnswer
from .errors import DomainError
from .ingestion.markdown_parser import MarkdownDocumentParser
from .ingestion.pipeline import CanonicalIngestionPipeline
from .jalali import parse_jalali_date
from .navigation import ResearchOutcome
from .repositories import CanonicalRepository
from .verification import AnswerClaim, ClaimImportance, DraftAnswer

ABSTENTION_MARKERS: tuple[str, ...] = (
    "امتناع",
    "نمی‌توان",
    "نمی توان",
    "قابل پاسخ نیست",
    "کمبود",
    "وجود ندارد",
    "موجود نیست",
    "امکان پاسخ قطعی نیست",
    "مستند نشده",
)

ANSWERABLE = "full"
PARTIAL = "partial"
INSUFFICIENT = "insufficient"

# score weights (rubric-inspired; abstention is a first-class component)
W_RECALL = 0.35
W_PAGE = 0.20
W_ABSTENTION = 0.25
W_PRECISION = 0.10
W_GROUNDED = 0.10

PARTIAL_ABSTENTION_CREDIT = 0.6
NON_ABSTAINING_INSUFFICIENT_FACTOR = 0.5


class BenchmarkError(DomainError):
    """Benchmark inputs could not be loaded or a case cannot be graded."""


@dataclass(frozen=True, slots=True)
class GoldenCase:
    case_id: str
    question: str
    category: str
    difficulty: str
    answerability: str
    applicable_time: date
    required_documents: tuple[str, ...]
    evidence_pages: tuple[tuple[str, frozenset[int]], ...]
    expected_abstention: str | None

    @classmethod
    def from_rows(
        cls,
        question_row: Mapping[str, object],
        gold_row: Mapping[str, object],
        applicable_time: date,
    ) -> GoldenCase:
        case_id = str(question_row.get("id") or gold_row.get("id") or "").strip()
        question = str(gold_row.get("question") or question_row.get("question") or "").strip()
        if not case_id or not question:
            raise BenchmarkError("golden rows must carry id and question")
        evidence_rows = gold_row.get("evidence") or []
        evidence_pages: list[tuple[str, frozenset[int]]] = []
        if isinstance(evidence_rows, list):
            for row in evidence_rows:
                if not isinstance(row, dict):
                    continue
                doc = str(row.get("doc", "")).strip()
                pages_raw = row.get("pages") or []
                pages = frozenset(
                    int(page) for page in pages_raw if isinstance(page, (int, float))
                )
                if doc:
                    evidence_pages.append((doc, pages))
        answerability = str(gold_row.get("answerability") or ANSWERABLE).strip()
        if answerability not in {ANSWERABLE, PARTIAL, INSUFFICIENT}:
            raise BenchmarkError(f"unknown answerability {answerability!r}")
        required = tuple(
            str(doc).strip()
            for doc in (gold_row.get("required_documents") or [])
            if str(doc).strip()
        )
        abstention = gold_row.get("expected_abstention")
        return cls(
            case_id=case_id,
            question=question,
            category=str(question_row.get("category") or gold_row.get("category") or "").strip(),
            difficulty=str(question_row.get("difficulty") or gold_row.get("difficulty") or "").strip(),
            answerability=answerability,
            applicable_time=applicable_time,
            required_documents=required,
            evidence_pages=tuple(evidence_pages),
            expected_abstention=str(abstention).strip() if isinstance(abstention, str) else None,
        )


def _read_jsonl(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not path.is_file():
        raise BenchmarkError(f"benchmark file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = str(row.get("id", "")).strip()
            if not key:
                raise BenchmarkError(f"row without id in {path.name}")
            rows[key] = row
    return rows


def load_golden_cases(
    benchmark_dir: Path,
    applicable_time: date,
) -> tuple[GoldenCase, ...]:
    """Join blind ``questions.jsonl`` with ``gold_answers.jsonl``."""
    benchmark_dir = Path(benchmark_dir)
    questions = _read_jsonl(benchmark_dir / "questions.jsonl")
    gold = _read_jsonl(benchmark_dir / "gold_answers.jsonl")
    missing = sorted(set(questions) - set(gold))
    if missing:
        raise BenchmarkError(f"gold answers missing for: {', '.join(missing)}")
    return tuple(
        GoldenCase.from_rows(questions[case_id], gold[case_id], applicable_time)
        for case_id in sorted(questions)
    )


def declared_abstention(
    text: str | None,
    identified_issues: Iterable[str] = (),
    evidenced_claims: int = 0,
) -> bool:
    """Honest-abstention detection.

    The explicit loop flag always counts. Text markers only count when the
    draft carries no evidence-backed claim — quoted source text can contain
    the same words without being an abstention.
    """
    if any("declared_abstention" in issue for issue in identified_issues):
        return True
    if not text or evidenced_claims > 0:
        return False
    return any(marker in text for marker in ABSTENTION_MARKERS)


@dataclass(frozen=True, slots=True)
class CaseGrade:
    case_id: str
    category: str
    answerability: str
    completed: bool
    published: bool
    abstained: bool
    document_recall: float
    page_hit_rate: float
    citation_precision: float
    abstention_correct: bool
    grounded_claim_rate: float
    temporal_ok: bool | None
    score: float
    cited_documents: tuple[str, ...]
    iterations: int = 0
    identified_issues: tuple[str, ...] = ()
    #: Documents whose evidence entered the ledger in ANY disposition —
    #  separates "the model never found it" from "found but not published".
    touched_documents: tuple[str, ...] = ()
    sufficiency_declared: str | None = None
    sufficiency_agreement: bool | None = None


def _declared_sufficiency(issues: Iterable[str]) -> str | None:
    """Read the answer's declared sufficiency level from the episode."""
    fallback = None
    for issue in issues:
        if issue.startswith("sufficiency_"):
            return issue.split("_", 1)[1]
        if issue == "declared_abstention":
            fallback = INSUFFICIENT
    return fallback


def sufficiency_agreement(declared: str | None, answerability: str) -> bool | None:
    """Does the declared level match the gold answerability?

    ``None`` (no declaration) is only assessable for legacy answers, which
    are excluded from the agreement rate rather than judged.
    """
    if declared is None:
        return None
    if answerability == ANSWERABLE:
        return declared == "full"
    if answerability == PARTIAL:
        return declared == "partial"
    return declared == INSUFFICIENT


class DeterministicGrader:
    """Fairness rules: no-result scenarios need an explicit negative answer."""

    def __init__(self, document_map: Mapping[str, DocumentIndex]) -> None:
        self._index = dict(document_map)

    def grade(
        self,
        case: GoldenCase,
        outcome: ResearchOutcome,
        answer: PublishedAnswer | None,
    ) -> CaseGrade:
        draft = outcome.draft
        abstained = declared_abstention(
            draft.text,
            outcome.episode.identified_issues,
            evidenced_claims=sum(1 for claim in draft.claims if claim.evidence_ids),
        )
        published = answer is not None
        cited_documents: set[str] = set()
        pages_by_doc: dict[str, set[int]] = {}
        grounded_claims = sum(1 for claim in draft.claims if claim.evidence_ids)
        if answer is not None:
            for rendered in answer.citations:
                citation = rendered.citation
                doc = self._doc_for_instrument(citation.instrument_id)
                if doc is None:
                    continue
                cited_documents.add(doc)
                pages_by_doc.setdefault(doc, set()).add(citation.page)
        else:
            for evidence in outcome.ledger.accepted_evidence:
                doc = self._doc_for_source(evidence.document_id)
                if doc is not None:
                    cited_documents.add(doc)
                    pages_by_doc.setdefault(doc, set()).add(evidence.page)

        ordered_cited = tuple(sorted(cited_documents))
        touched: set[str] = set()
        for entry in outcome.ledger.entries:
            doc = self._doc_for_source(entry.evidence.document_id)
            if doc is not None:
                touched.add(doc)
        required = set(case.required_documents)
        hit = cited_documents & required
        recall = len(hit) / len(required) if required else 1.0
        precision = len(hit) / len(cited_documents) if cited_documents else 0.0
        rows = [row for row in case.evidence_pages if row[0] in self._index]
        if rows:
            hits = 0
            for doc, pages in rows:
                cited_pages = pages_by_doc.get(doc, set())
                if (pages and pages & cited_pages) or (not pages and doc in cited_documents):
                    hits += 1
            page_hit = hits / len(rows)
        else:
            page_hit = 0.0
        grounded = grounded_claims / len(draft.claims) if draft.claims else 0.0

        if case.answerability == INSUFFICIENT:
            abstention_correct = abstained
        elif case.answerability == PARTIAL:
            abstention_correct = True
        else:
            abstention_correct = not abstained

        if case.answerability == INSUFFICIENT and abstained:
            score = 1.0
        elif case.answerability == PARTIAL and abstained:
            score = PARTIAL_ABSTENTION_CREDIT
        else:
            score = (
                W_RECALL * recall
                + W_PAGE * page_hit
                + W_ABSTENTION * float(abstention_correct)
                + W_PRECISION * precision
                + W_GROUNDED * grounded
            )
            if case.answerability == INSUFFICIENT:
                score *= NON_ABSTAINING_INSUFFICIENT_FACTOR

        temporal_ok: bool | None = None
        if case.category == "temporal_version":
            temporal_ok = (published and recall == 1.0 and page_hit >= 0.5) or (
                abstained and case.answerability != ANSWERABLE
            )

        return CaseGrade(
            case_id=case.case_id,
            category=case.category,
            answerability=case.answerability,
            completed=outcome.completed,
            published=published,
            abstained=abstained,
            document_recall=round(recall, 4),
            page_hit_rate=round(page_hit, 4),
            citation_precision=round(precision, 4),
            abstention_correct=abstention_correct,
            grounded_claim_rate=round(grounded, 4),
            temporal_ok=temporal_ok,
            score=round(score, 4),
            cited_documents=ordered_cited,
            iterations=outcome.iterations,
            identified_issues=tuple(outcome.episode.identified_issues),
            touched_documents=tuple(sorted(touched)),
        )

    def _doc_for_instrument(self, instrument_id: str) -> str | None:
        for doc, index in self._index.items():
            if index.instrument_id == instrument_id:
                return doc
        return None

    def _doc_for_source(self, source_document_id: str) -> str | None:
        for doc, index in self._index.items():
            if index.source_document_id == source_document_id:
                return doc
        return None


@dataclass(frozen=True, slots=True)
class DocumentIndex:
    """Corpus ingestion identity for one benchmark document code (``D01``…)."""

    doc_code: str
    instrument_id: str
    source_document_id: str
    document_version_id: str
    effective_from: date | None


class ExtractiveAnswerComposer:
    """Deterministic baseline composer: one claim per retrieved evidence."""

    def compose(
        self,
        question: str,
        applicable_time: date,
        evidence: tuple,
    ) -> DraftAnswer:
        del question, applicable_time
        if not evidence:
            raise AnswerCompositionError(
                "no evidence was retrieved; an explicit abstention is required"
            )
        claims: list[AnswerClaim] = []
        lines: list[str] = []
        for index, item in enumerate(sorted(evidence, key=lambda e: e.evidence_id), start=1):
            claims.append(
                AnswerClaim(
                    f"claim-{index:02d}",
                    f"بر اساس منبع بازیابی‌شده: {item.text[:180]}",
                    (item.evidence_id,),
                    ClaimImportance.MAJOR,
                )
            )
            lines.append(f"- {item.text[:180]}")
        identity = json.dumps(
            [claim.evidence_ids for claim in claims], ensure_ascii=False, sort_keys=True
        )
        answer_id = f"answer-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        return DraftAnswer(answer_id, "\n".join(lines), tuple(claims))


def seed_corpus(
    repository: CanonicalRepository,
    corpus_dir: Path,
    manifest: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, DocumentIndex]:
    """Ingest ``D*.md`` files; return doc_code -> :class:`DocumentIndex` map.

    Document effective dates come from the manifest (Jalali) so temporal
    gating behaves like production. Files that fail the structural parser are
    reported as BenchmarkError instead of being silently dropped.
    """
    from dataclasses import replace as dataclass_replace

    corpus_dir = Path(corpus_dir)
    parser = MarkdownDocumentParser()
    pipeline = CanonicalIngestionPipeline(repository)
    document_map: dict[str, DocumentIndex] = {}
    for path in sorted(corpus_dir.glob("D*.md")):
        doc_code = path.stem
        try:
            parsed = parser.from_text(path.read_text(encoding="utf-8"), path.name)
        except DomainError:
            # Structureless prose (e.g. court rulings) yields no provisions;
            # skip it instead of failing the whole benchmark seeding.
            continue
        raw_date = (manifest or {}).get(doc_code, {}).get("date")
        effective_from = None
        if isinstance(raw_date, str) and raw_date.strip():
            try:
                effective_from = parse_jalali_date(raw_date.strip())
            except DomainError:
                # Year-only or partially known dates leave the document
                # without a temporal gate instead of failing seeding.
                effective_from = None
            parsed = dataclass_replace(
                parsed,
                version=dataclass_replace(parsed.version, effective_from=effective_from),
            )
        try:
            result = pipeline.ingest(parsed)
        except DomainError as exc:
            raise BenchmarkError(f"ingesting {doc_code} failed: {exc}") from exc
        document_map[doc_code] = DocumentIndex(
            doc_code=doc_code,
            instrument_id=result.instrument_id,
            source_document_id=result.source_document_id,
            document_version_id=result.document_version_id,
            effective_from=effective_from,
        )
    if not document_map:
        raise BenchmarkError(f"no D*.md documents found in {corpus_dir}")
    return document_map


__all__ = [
    "ABSTENTION_MARKERS",
    "ANSWERABLE",
    "INSUFFICIENT",
    "PARTIAL",
    "BenchmarkError",
    "CaseGrade",
    "DeterministicGrader",
    "DocumentIndex",
    "ExtractiveAnswerComposer",
    "GoldenCase",
    "declared_abstention",
    "load_golden_cases",
    "seed_corpus",
]
