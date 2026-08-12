from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .application import ResearchApplicationService, ResearchCommand
from .canonical import require_text
from .security import AuthorizationService, Permission, Principal


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    question: str
    applicable_time: date
    expected_provision_ids: frozenset[str]
    expected_provision_version_ids: frozenset[str] = frozenset()
    document_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.case_id, "case_id")
        require_text(self.question, "question")


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    case_id: str
    completed: bool
    citation_precision: float
    citation_recall: float
    temporal_accuracy: float
    grounded_claim_rate: float


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    case_count: int
    completion_rate: float
    citation_precision: float
    citation_recall: float
    temporal_accuracy: float
    grounded_claim_rate: float
    cases: tuple[CaseEvaluation, ...]


class EvaluationRunner:
    def __init__(
        self,
        research: ResearchApplicationService,
        authorization: AuthorizationService,
    ) -> None:
        self.research = research
        self.authorization = authorization

    def run(
        self,
        principal: Principal,
        organization_id: str,
        cases: tuple[EvaluationCase, ...],
    ) -> EvaluationReport:
        self.authorization.require(
            principal, Permission.RUN_EVALUATION, organization_id
        )
        results = tuple(
            self._run_case(principal, organization_id, case) for case in cases
        )
        return EvaluationReport(
            case_count=len(results),
            completion_rate=self._average(results, "completed"),
            citation_precision=self._average(results, "citation_precision"),
            citation_recall=self._average(results, "citation_recall"),
            temporal_accuracy=self._average(results, "temporal_accuracy"),
            grounded_claim_rate=self._average(results, "grounded_claim_rate"),
            cases=results,
        )

    def _run_case(
        self,
        principal: Principal,
        organization_id: str,
        case: EvaluationCase,
    ) -> CaseEvaluation:
        record = self.research.run(
            principal,
            ResearchCommand(
                organization_id,
                case.question,
                case.applicable_time,
                case.document_scope,
                f"evaluation-{case.case_id}",
            ),
        )
        answer = record.answer
        if answer is None:
            return CaseEvaluation(case.case_id, False, 0.0, 0.0, 0.0, 0.0)

        cited_provisions = {item.citation.provision_id for item in answer.citations}
        cited_versions = {
            item.citation.provision_version_id for item in answer.citations
        }
        relevant = cited_provisions & case.expected_provision_ids
        precision = self._ratio(
            len(relevant), len(cited_provisions), empty=not case.expected_provision_ids
        )
        recall = self._ratio(
            len(relevant), len(case.expected_provision_ids), empty=True
        )
        version_match = (
            not case.expected_provision_version_ids
            or case.expected_provision_version_ids <= cited_versions
        )
        temporal = float(
            answer.audit.applicable_time == case.applicable_time and version_match
        )
        grounded = self._ratio(
            sum(bool(item.citation_ids) for item in answer.claim_evidence_map),
            len(answer.claim_evidence_map),
            empty=True,
        )
        return CaseEvaluation(case.case_id, True, precision, recall, temporal, grounded)

    @staticmethod
    def _ratio(numerator: int, denominator: int, *, empty: bool) -> float:
        return numerator / denominator if denominator else float(empty)

    @staticmethod
    def _average(results: tuple[CaseEvaluation, ...], field: str) -> float:
        if not results:
            return 0.0
        return sum(float(getattr(item, field)) for item in results) / len(results)
