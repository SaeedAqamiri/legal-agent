from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .errors import DomainError
from .research import Evidence, ResearchAction, ResearchActionType, ResearchEpisode
from .verification import (
    DraftAnswer,
    EvidenceDisposition,
    EvidenceLedger,
    EvidenceVerifier,
    VerificationIssue,
    VerificationReport,
)


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    organization_id: str
    question: str
    applicable_time: date
    document_scope: tuple[str, ...]
    iteration: int
    unresolved_issues: tuple[VerificationIssue, ...]
    visited_provision_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetrievalBatch:
    evidence: tuple[Evidence, ...]
    visited_node_ids: tuple[str, ...] = ()
    visited_provision_ids: tuple[str, ...] = ()
    followed_reference_ids: tuple[str, ...] = ()
    identified_issues: tuple[str, ...] = ()


class EvidenceRetriever(Protocol):
    def retrieve(self, request: ResearchRequest) -> RetrievalBatch: ...


class AnswerComposer(Protocol):
    def compose(
        self,
        question: str,
        applicable_time: date,
        evidence: tuple[Evidence, ...],
    ) -> DraftAnswer: ...


@dataclass(frozen=True, slots=True)
class NavigationConfig:
    max_iterations: int = 5
    stop_on_stall: bool = True

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise DomainError("max_iterations must be positive")


@dataclass(frozen=True, slots=True)
class ResearchOutcome:
    episode: ResearchEpisode
    ledger: EvidenceLedger
    draft: DraftAnswer
    verification: VerificationReport
    completed: bool
    iterations: int


class NavigationLoop:
    def __init__(
        self,
        retriever: EvidenceRetriever,
        composer: AnswerComposer,
        verifier: EvidenceVerifier,
        config: NavigationConfig | None = None,
    ) -> None:
        self.retriever = retriever
        self.composer = composer
        self.verifier = verifier
        self.config = config or NavigationConfig()

    def run(
        self,
        *,
        episode_id: str,
        organization_id: str,
        user_id: str,
        conversation_id: str,
        question: str,
        applicable_time: date,
        document_scope: tuple[str, ...] = (),
    ) -> ResearchOutcome:
        if not question.strip():
            raise DomainError("research question must not be blank")
        episode = ResearchEpisode(
            episode_id=episode_id,
            organization_id=organization_id,
            user_id=user_id,
            conversation_id=conversation_id,
            question=question,
            applicable_time=applicable_time,
            document_scope=document_scope,
        )
        ledger = EvidenceLedger()
        unresolved: tuple[VerificationIssue, ...] = ()
        previous_evidence_ids: frozenset[str] = frozenset()
        recorded_dispositions: set[tuple[str, EvidenceDisposition]] = set()
        draft: DraftAnswer | None = None
        report: VerificationReport | None = None

        for iteration in range(1, self.config.max_iterations + 1):
            episode.record(
                ResearchAction(
                    ResearchActionType.SEARCHED,
                    metadata={
                        "iteration": iteration,
                        "unresolved_issue_codes": [item.code.value for item in unresolved],
                    },
                )
            )
            batch = self.retriever.retrieve(
                ResearchRequest(
                    organization_id=organization_id,
                    question=question,
                    applicable_time=applicable_time,
                    document_scope=document_scope,
                    iteration=iteration,
                    unresolved_issues=unresolved,
                    visited_provision_ids=tuple(episode.visited_provisions),
                )
            )
            self._record_batch(episode, ledger, batch)
            draft = self.composer.compose(question, applicable_time, ledger.usable_evidence)
            report = self.verifier.verify(draft, ledger, applicable_time)
            self._record_dispositions(episode, ledger, recorded_dispositions)
            episode.evidence_ids = list(report.verified_evidence_ids)
            episode.answer_id = draft.answer_id

            if report.accepted:
                return ResearchOutcome(episode, ledger, draft, report, True, iteration)

            unresolved = report.issues
            current_evidence_ids = frozenset(entry.evidence.evidence_id for entry in ledger.entries)
            stalled = current_evidence_ids == previous_evidence_ids
            previous_evidence_ids = current_evidence_ids
            if self.config.stop_on_stall and stalled:
                return ResearchOutcome(episode, ledger, draft, report, False, iteration)

        assert draft is not None and report is not None
        return ResearchOutcome(
            episode,
            ledger,
            draft,
            report,
            False,
            self.config.max_iterations,
        )

    @staticmethod
    def _record_batch(
        episode: ResearchEpisode,
        ledger: EvidenceLedger,
        batch: RetrievalBatch,
    ) -> None:
        for issue in batch.identified_issues:
            if issue not in episode.identified_issues:
                episode.identified_issues.append(issue)
        for node_id in batch.visited_node_ids:
            if node_id not in episode.visited_nodes:
                episode.visited_nodes.append(node_id)
        for provision_id in batch.visited_provision_ids:
            if provision_id not in episode.visited_provisions:
                episode.visited_provisions.append(provision_id)
        for reference_id in batch.followed_reference_ids:
            episode.record(ResearchAction(ResearchActionType.FOLLOWED_REFERENCE, reference_id))
        for evidence in batch.evidence:
            if ledger.add(evidence):
                episode.record(ResearchAction(ResearchActionType.OPENED, evidence.provision_version_id))
                episode.record(ResearchAction(ResearchActionType.CHECKED_VERSION, evidence.provision_version_id))

    @staticmethod
    def _record_dispositions(
        episode: ResearchEpisode,
        ledger: EvidenceLedger,
        recorded: set[tuple[str, EvidenceDisposition]],
    ) -> None:
        for entry in ledger.entries:
            if entry.disposition == EvidenceDisposition.PENDING:
                continue
            key = (entry.evidence.evidence_id, entry.disposition)
            if key in recorded:
                continue
            action_type = (
                ResearchActionType.ACCEPTED_EVIDENCE
                if entry.disposition == EvidenceDisposition.ACCEPTED
                else ResearchActionType.REJECTED_EVIDENCE
            )
            episode.record(
                ResearchAction(
                    action_type,
                    entry.evidence.evidence_id,
                    {"reason": entry.reason},
                )
            )
            recorded.add(key)
