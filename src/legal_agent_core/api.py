import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .application import (
    KnowledgeReviewApplicationService,
    ResearchApplicationService,
    ResearchCommand,
    ResearchRecord,
)
from .errors import AuthorizationError, ConflictError, DomainError, NotFoundError
from .evaluation import EvaluationCase, EvaluationRunner
from .library import DocumentLibraryService
from .observability import MetricsRegistry
from .research import ProgressiveRelation, TrustStatus
from .security import (
    AuthenticationError,
    AuthorizationService,
    Permission,
    Principal,
    TokenVerifier,
)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_UI_DIRECTORY = Path(__file__).with_name("ui")
_REVIEW_QUEUE_STATUSES = frozenset(
    {TrustStatus.CANDIDATE, TrustStatus.NEEDS_REVALIDATION}
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchInput(StrictModel):
    organization_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=10_000)
    applicable_time: date
    document_scope: tuple[str, ...] = Field(default=(), max_length=100)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=200)


class ReviewInput(StrictModel):
    note: str | None = Field(default=None, max_length=4_000)


class RejectionInput(StrictModel):
    reason: str = Field(min_length=1, max_length=4_000)


class EvaluationCaseInput(StrictModel):
    case_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=10_000)
    applicable_time: date
    expected_provision_ids: frozenset[str]
    expected_provision_version_ids: frozenset[str] = frozenset()
    document_scope: tuple[str, ...] = Field(default=(), max_length=100)


class EvaluationInput(StrictModel):
    cases: tuple[EvaluationCaseInput, ...] = Field(min_length=1, max_length=500)


@dataclass(frozen=True, slots=True)
class APIContainer:
    research: ResearchApplicationService
    reviews: KnowledgeReviewApplicationService
    evaluations: EvaluationRunner
    token_verifier: TokenVerifier
    authorization: AuthorizationService
    metrics: MetricsRegistry
    library: DocumentLibraryService | None = None


def create_app(container: APIContainer) -> FastAPI:
    app = FastAPI(
        title="Temporal Legal Agent API",
        version="0.1.0",
        description="Evidence-bound, tenant-isolated legal research backend.",
    )
    app.mount("/ui", StaticFiles(directory=_UI_DIRECTORY), name="ui")
    library = container.library or DocumentLibraryService(
        container.research.citations.canonical_repository,
        container.authorization,
    )
    bearer = HTTPBearer(auto_error=False)

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise AuthenticationError("bearer token is required")
        return container.token_verifier.verify(credentials.credentials)

    CurrentPrincipal = Annotated[Principal, Depends(authenticate)]

    @app.middleware("http")
    async def request_observability(request: Request, call_next: Any) -> Any:
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if _REQUEST_ID.fullmatch(supplied_request_id)
            else uuid4().hex
        )
        started = perf_counter()
        try:
            response = await call_next(request)
            return response
        finally:
            container.metrics.increment("http_requests_total")
            container.metrics.observe(
                "http_request_duration_ms", (perf_counter() - started) * 1000
            )
            if "response" in locals():
                response.headers["X-Request-ID"] = request_id
                if request.url.path == "/workspace" or request.url.path.startswith(
                    "/ui/"
                ):
                    response.headers["Content-Security-Policy"] = (
                        "default-src 'self'; script-src 'self'; style-src 'self'; "
                        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                    )
                    response.headers["X-Content-Type-Options"] = "nosniff"
                    response.headers["Referrer-Policy"] = "no-referrer"
                    response.headers["Permissions-Policy"] = (
                        "camera=(), microphone=(), geolocation=()"
                    )

    @app.exception_handler(AuthenticationError)
    async def authentication_error(
        _: Request, exc: AuthenticationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(AuthorizationError)
    async def authorization_error(_: Request, exc: AuthorizationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)}
        )

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)}
        )

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)}
        )

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc)},
        )

    @app.get("/healthz", tags=["operations"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": app.version}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(
            "/workspace", status_code=status.HTTP_307_TEMPORARY_REDIRECT
        )

    @app.get("/workspace", include_in_schema=False)
    def workspace() -> FileResponse:
        return FileResponse(_UI_DIRECTORY / "index.html", media_type="text/html")

    @app.post("/v1/research", tags=["research"])
    def run_research(
        payload: ResearchInput, principal: CurrentPrincipal
    ) -> dict[str, Any]:
        record = container.research.run(
            principal,
            ResearchCommand(
                organization_id=payload.organization_id,
                question=payload.question,
                applicable_time=payload.applicable_time,
                document_scope=payload.document_scope,
                conversation_id=payload.conversation_id,
            ),
        )
        return _research_response(record)

    @app.get(
        "/v1/organizations/{organization_id}/research/{episode_id}",
        tags=["research"],
    )
    def get_research(
        organization_id: str,
        episode_id: str,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        return _research_response(
            container.research.get(principal, organization_id, episode_id)
        )

    @app.get(
        "/v1/organizations/{organization_id}/library",
        tags=["library"],
    )
    def search_library(
        organization_id: str,
        principal: CurrentPrincipal,
        query: str = "",
        applicable_time: date | None = None,
    ) -> dict[str, Any]:
        documents = library.search(
            principal,
            organization_id,
            query=query,
            applicable_time=applicable_time,
        )
        return {
            "organization_id": organization_id,
            "query": query,
            "applicable_time": applicable_time,
            "count": len(documents),
            "documents": [asdict(document) for document in documents],
        }

    @app.get(
        "/v1/organizations/{organization_id}/library/documents/{document_version_id}",
        tags=["library"],
    )
    def get_library_document(
        organization_id: str,
        document_version_id: str,
        principal: CurrentPrincipal,
        source_span_id: str | None = None,
    ) -> dict[str, Any]:
        detail = library.get_document(principal, organization_id, document_version_id)
        known_span_ids = {
            span.source_span_id
            for provision in detail.provisions
            for span in provision.spans
        }
        if source_span_id is not None and source_span_id not in known_span_ids:
            raise NotFoundError(
                f"source span {source_span_id!r} does not belong to document version"
            )
        result = asdict(detail)
        result["selected_source_span_id"] = source_span_id
        return result

    @app.post(
        "/v1/organizations/{organization_id}/relations/{relation_id}/approve",
        tags=["knowledge-review"],
    )
    def approve_relation(
        organization_id: str,
        relation_id: str,
        payload: ReviewInput,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        return _relation_response(
            container.reviews.approve(
                principal, organization_id, relation_id, payload.note
            )
        )

    @app.get(
        "/v1/organizations/{organization_id}/relations",
        tags=["knowledge-review"],
    )
    def review_queue(
        organization_id: str,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        relations = container.reviews.list_queue(
            principal,
            organization_id,
            _REVIEW_QUEUE_STATUSES,
        )
        return {
            "organization_id": organization_id,
            "count": len(relations),
            "relations": [_relation_response(item) for item in relations],
        }

    @app.post(
        "/v1/organizations/{organization_id}/relations/{relation_id}/reject",
        tags=["knowledge-review"],
    )
    def reject_relation(
        organization_id: str,
        relation_id: str,
        payload: RejectionInput,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        return _relation_response(
            container.reviews.reject(
                principal, organization_id, relation_id, payload.reason
            )
        )

    @app.post(
        "/v1/organizations/{organization_id}/evaluations",
        tags=["evaluation"],
    )
    def run_evaluation(
        organization_id: str,
        payload: EvaluationInput,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        cases = tuple(
            EvaluationCase(
                item.case_id,
                item.question,
                item.applicable_time,
                item.expected_provision_ids,
                item.expected_provision_version_ids,
                item.document_scope,
            )
            for item in payload.cases
        )
        return asdict(container.evaluations.run(principal, organization_id, cases))

    @app.get(
        "/v1/organizations/{organization_id}/metrics",
        tags=["operations"],
    )
    def metrics(organization_id: str, principal: CurrentPrincipal) -> dict[str, Any]:
        container.authorization.require(
            principal, Permission.READ_METRICS, organization_id
        )
        return asdict(container.metrics.snapshot(organization_id))

    return app


def _research_response(record: ResearchRecord) -> dict[str, Any]:
    outcome = record.outcome
    result: dict[str, Any] = {
        "organization_id": record.organization_id,
        "episode_id": outcome.episode.episode_id,
        "conversation_id": outcome.episode.conversation_id,
        "completed": outcome.completed,
        "iterations": outcome.iterations,
        "trace": {
            "created_at": outcome.episode.created_at.isoformat(),
            "identified_issues": outcome.episode.identified_issues,
            "visited_node_ids": outcome.episode.visited_nodes,
            "visited_provision_ids": outcome.episode.visited_provisions,
            "actions": [
                {
                    "type": action.action_type.value,
                    "target_id": action.target_id,
                    "metadata": action.metadata,
                    "occurred_at": action.occurred_at.isoformat(),
                }
                for action in outcome.episode.actions
            ],
            "evidence": [
                {
                    "evidence_id": entry.evidence.evidence_id,
                    "provision_id": entry.evidence.provision_id,
                    "provision_version_id": entry.evidence.provision_version_id,
                    "page": entry.evidence.page,
                    "retrieval_method": entry.evidence.retrieval_method,
                    "reason_selected": entry.evidence.reason_selected,
                    "confidence": entry.evidence.confidence,
                    "stance": entry.evidence.stance.value,
                    "disposition": entry.disposition.value,
                    "disposition_reason": entry.reason,
                }
                for entry in outcome.ledger.entries
            ],
        },
        "verification_issues": [
            {
                "code": item.code.value,
                "severity": item.severity.value,
                "message": item.message,
                "claim_id": item.claim_id,
                "evidence_id": item.evidence_id,
                "target_id": item.target_id,
            }
            for item in outcome.verification.issues
        ],
        "answer": None,
    }
    if record.answer is not None:
        answer = record.answer
        result["answer"] = {
            "answer_id": answer.answer_id,
            "text": answer.text,
            "rendered_text": answer.rendered_text,
            "claims": [
                {
                    "claim_id": item.claim_id,
                    "text": item.text,
                    "evidence_ids": item.evidence_ids,
                    "importance": item.importance.value,
                }
                for item in answer.claims
            ],
            "citations": [
                {
                    "citation_id": item.citation.citation_id,
                    "marker": item.marker,
                    "instrument_id": item.citation.instrument_id,
                    "instrument_title": item.instrument_title,
                    "provision_id": item.citation.provision_id,
                    "provision_label": item.provision_label,
                    "provision_version_id": item.citation.provision_version_id,
                    "document_version_id": item.citation.document_version_id,
                    "source_span_id": item.citation.source_span_id,
                    "page": item.citation.page,
                    "quoted_text": item.citation.quoted_text,
                    "source_uri": item.source_uri,
                }
                for item in answer.citations
            ],
            "audit": {
                "applicable_time": answer.audit.applicable_time.isoformat(),
                "verified_evidence_ids": answer.audit.verified_evidence_ids,
                "generation": asdict(answer.audit.generation)
                if answer.audit.generation
                else None,
            },
        }
    return result


def _relation_response(relation: ProgressiveRelation) -> dict[str, Any]:
    return {
        "relation_id": relation.relation_id,
        "organization_id": relation.organization_id,
        "source_node_id": relation.source_node_id,
        "target_node_id": relation.target_node_id,
        "edge_type": relation.edge_type.value,
        "truth_class": relation.truth_class.value,
        "source_episode_id": relation.source_episode_id,
        "evidence_version_ids": relation.evidence_version_ids,
        "confidence": relation.confidence,
        "status": relation.status.value,
        "approval": asdict(relation.approval) if relation.approval else None,
        "generation": {
            "model": relation.generated_by_model,
            "model_profile": relation.model_profile,
            "prompt_version": relation.prompt_version,
            "agent_version": relation.agent_version,
        },
    }
