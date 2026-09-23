from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from .canonical import DocumentStatus, ResolutionStatus
from .errors import ConflictError, DomainError, NotFoundError
from .ingestion.normalization import normalize_legal_text
from .repositories import CanonicalRepository
from .research import Evidence, EvidenceStance


class ClaimImportance(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


class EvidenceDisposition(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class VerificationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class VerificationCode(StrEnum):
    NO_CLAIMS = "no_claims"
    MISSING_EVIDENCE = "missing_evidence"
    UNKNOWN_EVIDENCE = "unknown_evidence"
    CANONICAL_IDENTITY_MISMATCH = "canonical_identity_mismatch"
    TEXT_NOT_IN_SOURCE = "text_not_in_source"
    TEMPORAL_MISMATCH = "temporal_mismatch"
    NON_EFFECTIVE_STATUS = "non_effective_status"
    AMBIGUOUS_APPLICABLE_VERSION = "ambiguous_applicable_version"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    UNFOLLOWED_EXPLICIT_REFERENCE = "unfollowed_explicit_reference"


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    claim_id: str
    text: str
    evidence_ids: tuple[str, ...]
    importance: ClaimImportance = ClaimImportance.MAJOR

    def __post_init__(self) -> None:
        if not self.claim_id.strip() or not self.text.strip():
            raise DomainError("claim_id and claim text must not be blank")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise DomainError("claim evidence_ids must be unique")


@dataclass(frozen=True, slots=True)
class GenerationMetadata:
    provider_id: str
    model: str
    model_profile_id: str
    model_profile_version: int
    prompt_id: str
    prompt_version: int
    agent_version: str
    provider_response_id: str
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class DraftAnswer:
    answer_id: str
    text: str
    claims: tuple[AnswerClaim, ...]
    generation: GenerationMetadata | None = None

    def __post_init__(self) -> None:
        if not self.answer_id.strip() or not self.text.strip():
            raise DomainError("answer_id and answer text must not be blank")
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise DomainError("claim IDs must be unique within an answer")


@dataclass(slots=True)
class EvidenceLedgerEntry:
    evidence: Evidence
    disposition: EvidenceDisposition = EvidenceDisposition.PENDING
    reason: str | None = None


class EvidenceLedger:
    def __init__(self) -> None:
        self._entries: dict[str, EvidenceLedgerEntry] = {}

    def add(self, evidence: Evidence) -> bool:
        existing = self._entries.get(evidence.evidence_id)
        if existing is not None:
            if existing.evidence != evidence:
                raise ConflictError(f"evidence identity {evidence.evidence_id!r} was reused with different data")
            return False
        self._entries[evidence.evidence_id] = EvidenceLedgerEntry(evidence)
        return True

    def get(self, evidence_id: str) -> EvidenceLedgerEntry | None:
        return self._entries.get(evidence_id)

    def accept(self, evidence_id: str, reason: str = "canonical evidence verified") -> None:
        entry = self._entries[evidence_id]
        if entry.disposition != EvidenceDisposition.REJECTED:
            entry.disposition = EvidenceDisposition.ACCEPTED
            entry.reason = reason

    def reject(self, evidence_id: str, reason: str) -> None:
        entry = self._entries[evidence_id]
        entry.disposition = EvidenceDisposition.REJECTED
        entry.reason = reason

    @property
    def entries(self) -> tuple[EvidenceLedgerEntry, ...]:
        return tuple(self._entries.values())

    @property
    def usable_evidence(self) -> tuple[Evidence, ...]:
        return tuple(
            entry.evidence
            for entry in self._entries.values()
            if entry.disposition != EvidenceDisposition.REJECTED
        )

    @property
    def accepted_evidence(self) -> tuple[Evidence, ...]:
        return tuple(
            entry.evidence
            for entry in self._entries.values()
            if entry.disposition == EvidenceDisposition.ACCEPTED
        )


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    code: VerificationCode
    severity: VerificationSeverity
    message: str
    claim_id: str | None = None
    evidence_id: str | None = None
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationReport:
    answer_id: str
    applicable_time: date
    accepted: bool
    requires_more_research: bool
    issues: tuple[VerificationIssue, ...]
    verified_evidence_ids: tuple[str, ...]


class EvidenceVerifier:
    def __init__(self, canonical_repository: CanonicalRepository) -> None:
        self.canonical_repository = canonical_repository

    def verify(
        self,
        draft: DraftAnswer,
        ledger: EvidenceLedger,
        applicable_time: date,
    ) -> VerificationReport:
        issues: list[VerificationIssue] = []
        if not draft.claims:
            issues.append(
                VerificationIssue(
                    VerificationCode.NO_CLAIMS,
                    VerificationSeverity.ERROR,
                    "answer contains no structured claims",
                )
            )

        referenced_ids = {evidence_id for claim in draft.claims for evidence_id in claim.evidence_ids}
        for claim in draft.claims:
            if not claim.evidence_ids:
                # The publish gate forbids ANY unevidenced claim, so verification
                # must be equally strict — a MINOR warning here would let the
                # report pass and then crash CitationPipeline.publish.
                issues.append(
                    VerificationIssue(
                        VerificationCode.MISSING_EVIDENCE,
                        VerificationSeverity.ERROR,
                        "claim has no source evidence",
                        claim_id=claim.claim_id,
                    )
                )

        verified: dict[str, Evidence] = {}
        for evidence_id in sorted(referenced_ids):
            entry = ledger.get(evidence_id)
            if entry is None:
                issues.append(
                    VerificationIssue(
                        VerificationCode.UNKNOWN_EVIDENCE,
                        VerificationSeverity.ERROR,
                        "claim references evidence absent from the ledger",
                        evidence_id=evidence_id,
                    )
                )
                continue
            if entry.disposition == EvidenceDisposition.REJECTED:
                issues.append(
                    VerificationIssue(
                        VerificationCode.UNKNOWN_EVIDENCE,
                        VerificationSeverity.ERROR,
                        entry.reason or "evidence was previously rejected",
                        evidence_id=evidence_id,
                    )
                )
                continue
            evidence_issues = self._verify_canonical_identity(entry.evidence, applicable_time)
            issues.extend(evidence_issues)
            if any(issue.severity == VerificationSeverity.ERROR for issue in evidence_issues):
                ledger.reject(evidence_id, evidence_issues[0].message)
                continue
            ledger.accept(evidence_id)
            verified[evidence_id] = entry.evidence

        for claim in draft.claims:
            claim_evidence = [verified[item] for item in claim.evidence_ids if item in verified]
            if any(item.stance == EvidenceStance.CONTRADICTS for item in claim_evidence):
                issues.append(
                    VerificationIssue(
                        VerificationCode.CONTRADICTORY_EVIDENCE,
                        VerificationSeverity.ERROR,
                        "claim has unresolved contradictory canonical evidence",
                        claim_id=claim.claim_id,
                    )
                )

        evidenced_provisions = {item.provision_id for item in verified.values()}
        for evidence in verified.values():
            for reference in self.canonical_repository.references_from_version(evidence.provision_version_id):
                target = reference.resolved_target_provision_id
                if (
                    reference.resolution_status == ResolutionStatus.RESOLVED
                    and target is not None
                    and target not in evidenced_provisions
                ):
                    issues.append(
                        VerificationIssue(
                            VerificationCode.UNFOLLOWED_EXPLICIT_REFERENCE,
                            VerificationSeverity.ERROR,
                            "resolved explicit reference was not opened and evidenced",
                            evidence_id=evidence.evidence_id,
                            target_id=target,
                        )
                    )

        issues = self._deduplicate(issues)
        accepted = not any(issue.severity == VerificationSeverity.ERROR for issue in issues)
        return VerificationReport(
            answer_id=draft.answer_id,
            applicable_time=applicable_time,
            accepted=accepted,
            requires_more_research=not accepted,
            issues=tuple(issues),
            verified_evidence_ids=tuple(sorted(verified)),
        )

    def _verify_canonical_identity(
        self, evidence: Evidence, applicable_time: date
    ) -> list[VerificationIssue]:
        issues: list[VerificationIssue] = []
        try:
            provision = self.canonical_repository.get_provision(evidence.provision_id)
            version = self.canonical_repository.get_provision_version(evidence.provision_version_id)
            document_version = self.canonical_repository.get_document_version(evidence.document_version_id)
            span = self.canonical_repository.get_source_span(evidence.source_span_id)
        except NotFoundError as exc:
            return [
                VerificationIssue(
                    VerificationCode.CANONICAL_IDENTITY_MISMATCH,
                    VerificationSeverity.ERROR,
                    str(exc),
                    evidence_id=evidence.evidence_id,
                )
            ]

        identity_matches = (
            version.provision_id == provision.provision_id
            and version.document_version_id == document_version.document_version_id
            and document_version.instrument_id == provision.instrument_id
            and document_version.source_document_id == evidence.document_id
            and span.provision_version_id == version.provision_version_id
            and span.document_version_id == document_version.document_version_id
            and span.source_document_id == evidence.document_id
            and span.page_number == evidence.page
        )
        if not identity_matches:
            issues.append(
                VerificationIssue(
                    VerificationCode.CANONICAL_IDENTITY_MISMATCH,
                    VerificationSeverity.ERROR,
                    "evidence citation fields do not resolve to one canonical source identity",
                    evidence_id=evidence.evidence_id,
                )
            )

        excerpt = normalize_legal_text(evidence.text)
        source_texts = {
            normalize_legal_text(span.raw_text),
            normalize_legal_text(version.text),
            normalize_legal_text(version.normalized_text),
        }
        if not excerpt or not any(excerpt in source_text for source_text in source_texts):
            issues.append(
                VerificationIssue(
                    VerificationCode.TEXT_NOT_IN_SOURCE,
                    VerificationSeverity.ERROR,
                    "evidence text cannot be located in its canonical source span/version",
                    evidence_id=evidence.evidence_id,
                )
            )

        if evidence.applicable_time != applicable_time:
            issues.append(
                VerificationIssue(
                    VerificationCode.TEMPORAL_MISMATCH,
                    VerificationSeverity.ERROR,
                    "evidence was retrieved for a different applicable_time",
                    evidence_id=evidence.evidence_id,
                )
            )
        candidates = self.canonical_repository.applicable_provision_versions(evidence.provision_id, applicable_time)
        if version.provision_version_id not in {item.provision_version_id for item in candidates}:
            issues.append(
                VerificationIssue(
                    VerificationCode.TEMPORAL_MISMATCH,
                    VerificationSeverity.ERROR,
                    "provision version is not temporally applicable to the question",
                    evidence_id=evidence.evidence_id,
                )
            )
        if len(candidates) > 1:
            issues.append(
                VerificationIssue(
                    VerificationCode.AMBIGUOUS_APPLICABLE_VERSION,
                    VerificationSeverity.ERROR,
                    "multiple provision versions overlap at applicable_time",
                    evidence_id=evidence.evidence_id,
                )
            )

        invalid_statuses = {DocumentStatus.DRAFT, DocumentStatus.SUSPENDED, DocumentStatus.UNKNOWN}
        if version.status in invalid_statuses or document_version.status in invalid_statuses:
            issues.append(
                VerificationIssue(
                    VerificationCode.NON_EFFECTIVE_STATUS,
                    VerificationSeverity.ERROR,
                    "source version status requires legal review before citation",
                    evidence_id=evidence.evidence_id,
                )
            )
        closed_statuses = {DocumentStatus.EXPIRED, DocumentStatus.REPEALED, DocumentStatus.SUPERSEDED}
        version_has_unknown_end = version.status in closed_statuses and version.effective_to is None
        document_has_unknown_end = (
            document_version.status in closed_statuses
            and document_version.effective_to is None
            and (
                document_version.repealed_at is None
                or applicable_time >= document_version.repealed_at
            )
        )
        if version_has_unknown_end or document_has_unknown_end:
            issues.append(
                VerificationIssue(
                    VerificationCode.NON_EFFECTIVE_STATUS,
                    VerificationSeverity.ERROR,
                    "closed/superseded source has no safe temporal end boundary",
                    evidence_id=evidence.evidence_id,
                )
            )
        return issues

    @staticmethod
    def _deduplicate(issues: list[VerificationIssue]) -> list[VerificationIssue]:
        seen: set[tuple[object, ...]] = set()
        output: list[VerificationIssue] = []
        for issue in issues:
            key = (issue.code, issue.claim_id, issue.evidence_id, issue.target_id, issue.message)
            if key not in seen:
                seen.add(key)
                output.append(issue)
        return output
