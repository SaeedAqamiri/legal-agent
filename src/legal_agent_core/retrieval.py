from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from .canonical import (
    ExplicitReference,
    Provision,
    ProvisionVersion,
    ResolutionStatus,
    SourceSpan,
)
from .errors import DomainError
from .navigation import ResearchRequest, RetrievalBatch
from .repositories import CanonicalRepository, ResearchGraphRepository
from .research import Evidence, GraphNamespace

_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)
_CHARACTER_TRANSLATION = str.maketrans(
    {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ۀ": "ه",
        "ة": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
        "۰": "0",
        "۱": "1",
        "۲": "2",
        "۳": "3",
        "۴": "4",
        "۵": "5",
        "۶": "6",
        "۷": "7",
        "۸": "8",
        "۹": "9",
        "٠": "0",
        "١": "1",
        "٢": "2",
        "٣": "3",
        "٤": "4",
        "٥": "5",
        "٦": "6",
        "٧": "7",
        "٨": "8",
        "٩": "9",
    }
)


# Persian number words mapped to digits so spelled-out numbers in queries
# («سی روز») match numerals in source text («۳۰ روز») and vice versa.
# Deliberately excludes «نه» (ambiguity with negation).
_WORD_NUMBERS: dict[str, str] = {
    "یک": "1", "دو": "2", "سه": "3", "چهار": "4", "پنج": "5",
    "شش": "6", "هفت": "7", "هشت": "8", "ده": "10",
    "یازده": "11", "دوازده": "12", "سیزده": "13", "چهارده": "14",
    "پانزده": "15", "شانزده": "16", "هفده": "17", "هجده": "18",
    "نوزده": "19", "بیست": "20", "سی": "30", "چهل": "40",
    "پنجاه": "50", "شصت": "60", "هفتاد": "70", "هشتاد": "80",
    "نود": "90", "صد": "100", "هزار": "1000",
}


def normalize_search_text(value: str) -> str:
    """Normalize Persian/Arabic variants without changing canonical source text."""
    normalized = unicodedata.normalize("NFKC", value).translate(_CHARACTER_TRANSLATION).casefold()
    tokens = (
        _WORD_NUMBERS.get(token, token)
        for token in _TOKEN_PATTERN.findall(normalized)
    )
    return " ".join(tokens)


def search_tokens(value: str) -> frozenset[str]:
    return frozenset(normalize_search_text(value).split())


class RetrievalSignal(StrEnum):
    LEXICAL = "lexical"
    METADATA = "metadata"
    EXPLICIT_REFERENCE = "explicit_reference"
    APPROVED_MEMORY = "approved_memory"


@dataclass(frozen=True, slots=True)
class RetrievalWeights:
    lexical: float = 0.60
    metadata: float = 0.20
    explicit_reference: float = 0.15
    approved_memory: float = 0.05

    def __post_init__(self) -> None:
        values = (self.lexical, self.metadata, self.explicit_reference, self.approved_memory)
        if any(value < 0 for value in values):
            raise DomainError("retrieval weights cannot be negative")
        if not math.isclose(sum(values), 1.0, abs_tol=1e-9):
            raise DomainError("retrieval weights must sum to 1")


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    max_results: int = 8
    reference_seed_limit: int = 5
    minimum_score: float = 0.01
    weights: RetrievalWeights = field(default_factory=RetrievalWeights)

    def __post_init__(self) -> None:
        if self.max_results < 1:
            raise DomainError("max_results must be positive")
        if self.reference_seed_limit < 1:
            raise DomainError("reference_seed_limit must be positive")
        if not 0 <= self.minimum_score <= 1:
            raise DomainError("minimum_score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RankedEvidence:
    evidence: Evidence
    score: float
    signals: tuple[RetrievalSignal, ...]


@dataclass(slots=True)
class _Candidate:
    provision: Provision
    version: ProvisionVersion
    span: SourceSpan
    lexical: float = 0.0
    metadata: float = 0.0
    explicit_reference: float = 0.0
    approved_memory: float = 0.0
    followed_references: list[ExplicitReference] = field(default_factory=list)


class MultiSignalEvidenceRetriever:
    """Explainable lexical, metadata, temporal and graph-aware canonical retriever."""

    def __init__(
        self,
        canonical: CanonicalRepository,
        research_graph: ResearchGraphRepository | None = None,
        config: RetrievalConfig | None = None,
    ) -> None:
        self.canonical = canonical
        self.research_graph = research_graph
        self.config = config or RetrievalConfig()
        self.last_ranked: tuple[RankedEvidence, ...] = ()

    def retrieve(self, request: ResearchRequest) -> RetrievalBatch:
        query_tokens = search_tokens(request.question)
        if not query_tokens:
            raise DomainError("retrieval question must contain searchable text")

        candidates = self._initial_candidates(request, query_tokens)
        followed_reference_ids = self._expand_explicit_references(request, candidates)
        self._apply_approved_memory(request, candidates)

        ranked = sorted(
            (
                (self._score(candidate), candidate)
                for candidate in candidates.values()
                if self._score(candidate) >= self.config.minimum_score
            ),
            key=lambda item: (-item[0], item[1].version.provision_version_id),
        )[: self.config.max_results]

        results = tuple(self._to_ranked_evidence(request, score, candidate) for score, candidate in ranked)
        self.last_ranked = results
        if not results:
            return RetrievalBatch((), identified_issues=("no_retrieval_candidates",))

        return RetrievalBatch(
            evidence=tuple(item.evidence for item in results),
            visited_node_ids=tuple(
                dict.fromkeys(
                    node_id
                    for _, candidate in ranked
                    for node_id in (candidate.version.document_version_id, candidate.version.provision_version_id)
                )
            ),
            visited_provision_ids=tuple(dict.fromkeys(candidate.provision.provision_id for _, candidate in ranked)),
            followed_reference_ids=tuple(followed_reference_ids),
        )

    def _initial_candidates(
        self,
        request: ResearchRequest,
        query_tokens: frozenset[str],
    ) -> dict[str, _Candidate]:
        candidates: dict[str, _Candidate] = {}
        for version in self.canonical.list_provision_versions():
            provision = self.canonical.get_provision(version.provision_id)
            document_version = self.canonical.get_document_version(version.document_version_id)
            if not version.applies_at(request.applicable_time) or not document_version.applies_at(
                request.applicable_time
            ):
                continue
            if not self._in_scope(request.document_scope, provision, version, document_version.source_document_id):
                continue
            spans = self.canonical.source_spans_for_version(version.provision_version_id)
            if not spans:
                continue

            span = max(spans, key=lambda item: self._lexical_score(query_tokens, search_tokens(item.raw_text)))
            lexical = max(
                self._lexical_score(query_tokens, search_tokens(version.normalized_text)),
                self._lexical_score(query_tokens, search_tokens(span.raw_text)),
            )
            instrument = self.canonical.get_instrument(provision.instrument_id)
            metadata_text = " ".join(
                filter(
                    None,
                    (
                        provision.label,
                        provision.number,
                        provision.title,
                        instrument.title,
                        instrument.canonical_title,
                        instrument.subject_domain,
                    ),
                )
            )
            metadata = self._overlap_score(query_tokens, search_tokens(metadata_text))
            if lexical or metadata:
                candidates[version.provision_version_id] = _Candidate(
                    provision=provision,
                    version=version,
                    span=span,
                    lexical=lexical,
                    metadata=metadata,
                )
        return candidates

    def _expand_explicit_references(
        self,
        request: ResearchRequest,
        candidates: dict[str, _Candidate],
    ) -> list[str]:
        seeds = sorted(
            candidates.values(),
            key=lambda item: (-(item.lexical + item.metadata), item.version.provision_version_id),
        )[: self.config.reference_seed_limit]
        followed: list[str] = []
        for seed in seeds:
            for reference in self.canonical.references_from_version(seed.version.provision_version_id):
                if (
                    reference.resolution_status != ResolutionStatus.RESOLVED
                    or reference.resolved_target_provision_id is None
                ):
                    continue
                for target_version in self.canonical.applicable_provision_versions(
                    reference.resolved_target_provision_id,
                    request.applicable_time,
                ):
                    target = self.canonical.get_provision(target_version.provision_id)
                    document_version = self.canonical.get_document_version(target_version.document_version_id)
                    if not document_version.applies_at(request.applicable_time):
                        continue
                    if not self._in_scope(
                        request.document_scope,
                        target,
                        target_version,
                        document_version.source_document_id,
                    ):
                        continue
                    spans = self.canonical.source_spans_for_version(target_version.provision_version_id)
                    if not spans:
                        continue
                    candidate = candidates.setdefault(
                        target_version.provision_version_id,
                        _Candidate(target, target_version, spans[0]),
                    )
                    candidate.explicit_reference = max(candidate.explicit_reference, reference.confidence or 1.0)
                    candidate.followed_references.append(reference)
                    if reference.reference_id not in followed:
                        followed.append(reference.reference_id)
        return followed

    def _apply_approved_memory(
        self,
        request: ResearchRequest,
        candidates: dict[str, _Candidate],
    ) -> None:
        if self.research_graph is None:
            return
        for relation in self.research_graph.active_relations(request.organization_id):
            if relation.target_namespace != GraphNamespace.CANONICAL:
                continue
            for candidate in candidates.values():
                if relation.target_node_id in {
                    candidate.provision.provision_id,
                    candidate.version.provision_version_id,
                }:
                    candidate.approved_memory = max(candidate.approved_memory, relation.confidence or 1.0)

    def _score(self, candidate: _Candidate) -> float:
        weights = self.config.weights
        return min(
            1.0,
            candidate.lexical * weights.lexical
            + candidate.metadata * weights.metadata
            + candidate.explicit_reference * weights.explicit_reference
            + candidate.approved_memory * weights.approved_memory,
        )

    def _to_ranked_evidence(
        self,
        request: ResearchRequest,
        score: float,
        candidate: _Candidate,
    ) -> RankedEvidence:
        signal_values = (
            (RetrievalSignal.LEXICAL, candidate.lexical),
            (RetrievalSignal.METADATA, candidate.metadata),
            (RetrievalSignal.EXPLICIT_REFERENCE, candidate.explicit_reference),
            (RetrievalSignal.APPROVED_MEMORY, candidate.approved_memory),
        )
        signals = tuple(signal for signal, value in signal_values if value > 0)
        reason = ", ".join(f"{signal.value}={value:.3f}" for signal, value in signal_values if value > 0)
        identity = (
            f"{candidate.version.provision_version_id}|{candidate.span.source_span_id}|"
            f"{request.applicable_time.isoformat()}"
        )
        evidence = Evidence(
            evidence_id=f"ev-{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
            document_id=candidate.span.source_document_id,
            document_version_id=candidate.version.document_version_id,
            provision_id=candidate.provision.provision_id,
            provision_version_id=candidate.version.provision_version_id,
            source_span_id=candidate.span.source_span_id,
            page=candidate.span.page_number,
            text=candidate.span.raw_text,
            retrieval_method="multi_signal",
            reason_selected=reason,
            applicable_time=request.applicable_time,
            confidence=score,
        )
        return RankedEvidence(evidence, score, signals)

    @staticmethod
    def _lexical_score(query: frozenset[str], document: frozenset[str]) -> float:
        if not query or not document:
            return 0.0
        overlap = len(query & document)
        if overlap == 0:
            return 0.0
        coverage = overlap / len(query)
        precision = overlap / len(document)
        return min(1.0, 0.8 * coverage + 0.2 * math.sqrt(precision))

    @staticmethod
    def _overlap_score(query: frozenset[str], metadata: frozenset[str]) -> float:
        if not query:
            return 0.0
        return len(query & metadata) / len(query)

    @staticmethod
    def _in_scope(
        scope: tuple[str, ...],
        provision: Provision,
        version: ProvisionVersion,
        source_document_id: str,
    ) -> bool:
        if not scope:
            return True
        return bool(
            set(scope)
            & {
                provision.instrument_id,
                provision.provision_id,
                version.document_version_id,
                version.provision_version_id,
                source_document_id,
            }
        )
