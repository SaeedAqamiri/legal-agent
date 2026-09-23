import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .adapters.oidc import (
    OIDCAuthorizationClient,
    OIDCTokenVerifier,
    SessionTokenVerifier,
    secrets_compare,
)
from .agentic.nlq import parse_search_filters
from .application import (
    KnowledgeReviewApplicationService,
    ResearchApplicationService,
    ResearchCommand,
    ResearchHistoryStore,
    ResearchRecord,
    ResearchStrategy,
)
from .errors import (
    AuthorizationError,
    ConflictError,
    DomainError,
    NotFoundError,
)
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
    strategy: Literal["navigation", "agentic"] = "navigation"


class SearchParseInput(StrictModel):
    question: str = Field(min_length=1, max_length=4_000)


class ReviewInput(StrictModel):
    note: str | None = Field(default=None, max_length=4_000)


class RejectionInput(StrictModel):
    reason: str = Field(min_length=1, max_length=4_000)


class ReviewRequestInput(StrictModel):
    action: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=4_000)
    reason: str | None = Field(default=None, max_length=4_000)


class ReviewConfirmInput(StrictModel):
    ticket_id: str = Field(min_length=8, max_length=128)
    action: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=4_000)
    reason: str | None = Field(default=None, max_length=4_000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


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
    history: ResearchHistoryStore | None = None
    oidc: "OIDCProvider | None" = None


@dataclass(frozen=True, slots=True)
class OIDCProvider:
    client: "OIDCAuthorizationClient"
    verifier: "OIDCTokenVerifier"
    session: "SessionTokenVerifier"
    cookie_name: str = "legal_agent_session"


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
    history = container.history or ResearchHistoryStore()
    bearer = HTTPBearer(auto_error=False)

    def authenticate(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        if credentials is not None and credentials.scheme.lower() == "bearer":
            return container.token_verifier.verify(credentials.credentials)
        if container.oidc is not None:
            cookie = request.cookies.get(container.oidc.cookie_name)
            if cookie:
                try:
                    return container.oidc.session.verify(cookie)
                except AuthenticationError:
                    raise AuthenticationError("session is invalid or expired")
        raise AuthenticationError("authenticated session or bearer token is required")

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

    if container.oidc is not None:
        from .adapters.oidc import new_auth_request

        oidc = container.oidc
        _state_cookie = "legal_agent_oauth_state"

        @app.get("/auth/login", include_in_schema=False)
        def login(response: JSONResponse) -> RedirectResponse:
            auth_request = new_auth_request()
            response.set_cookie(
                _state_cookie,
                auth_request.state,
                max_age=600,
                httponly=True,
                samesite="lax",
            )
            return RedirectResponse(
                oidc.client.authorization_uri(auth_request.state, auth_request.nonce),
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            )

        @app.get("/auth/callback", include_in_schema=False)
        def auth_callback(
            code: str,
            state: str,
            request: Request,
        ) -> RedirectResponse:
            expected_state = request.cookies.get(_state_cookie)
            if not expected_state or not secrets_compare(expected_state, state):
                raise AuthenticationError("OIDC state mismatch; restart login")
            tokens = oidc.client.exchange_code(code, oidc.client.provider.redirect_uri)
            principal = oidc.verifier.verify_id_token(tokens["id_token"])
            session_token = oidc.session.issue(principal)
            redirect = RedirectResponse(
                "/workspace", status_code=status.HTTP_303_SEE_OTHER
            )
            redirect.set_cookie(
                oidc.cookie_name,
                session_token,
                max_age=oidc.session.ttl_seconds,
                httponly=True,
                samesite="lax",
                secure=bool(request.url.scheme == "https"),
            )
            return redirect

        @app.get("/v1/me", tags=["research"])
        def whoami(principal: CurrentPrincipal) -> dict[str, Any]:
            return {
                "actor_id": principal.actor_id,
                "organization_id": principal.organization_id,
                "roles": sorted(role.value for role in principal.roles),
            }

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
                strategy=ResearchStrategy(payload.strategy),
            ),
        )
        payload = _research_response(record)
        history.save(
            record.organization_id,
            record.outcome.episode.episode_id,
            payload,
        )
        return payload

    @app.post(
        "/v1/research/stream",
        tags=["research"],
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    def stream_research(
        payload: ResearchInput, principal: CurrentPrincipal
    ) -> StreamingResponse:
        """Server-sent events for a research run.

        Events: ``started``, ``tool_call``/``tool_error``/``contract_error``/
        ``verification_*``/``abstained`` (agentic strategy) and a final
        ``result`` carrying the same payload as the blocking endpoint.
        """

        def event_frames():
            command = ResearchCommand(
                organization_id=payload.organization_id,
                question=payload.question,
                applicable_time=payload.applicable_time,
                document_scope=payload.document_scope,
                conversation_id=payload.conversation_id,
                strategy=ResearchStrategy(payload.strategy),
            )
            try:
                for event in container.research.stream_events(principal, command):
                    if event.get("type") == "completed":
                        record = event["record"]
                        body = _research_response(record)
                        history.save(
                            record.organization_id,
                            record.outcome.episode.episode_id,
                            body,
                        )
                        yield _sse_frame({"type": "result", "data": body})
                        yield "event: done\ndata: {}\n\n"
                    else:
                        yield _sse_frame(event)
            except DomainError as exc:
                yield _sse_frame({"type": "failed", "message": str(exc)})

        return StreamingResponse(
            event_frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/v1/organizations/{organization_id}/search/parse",
        tags=["library"],
    )
    def parse_search(
        organization_id: str,
        payload: SearchParseInput,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        container.authorization.require(
            principal, Permission.READ_RESEARCH, organization_id
        )
        known_documents: dict[str, str] = {}
        canonical = container.research.citations.canonical_repository
        for provision in canonical.list_provisions():
            try:
                instrument = canonical.get_instrument(provision.instrument_id)
            except NotFoundError:
                continue
            for title in (instrument.title, instrument.canonical_title):
                if title:
                    known_documents.setdefault(title, provision.instrument_id)
        filters = parse_search_filters(payload.question, known_documents)
        return {
            "organization_id": organization_id,
            "filters": filters.to_payload(),
        }

    @app.get(
        "/v1/organizations/{organization_id}/research",
        tags=["research"],
    )
    def research_history(
        organization_id: str,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        container.authorization.require(
            principal, Permission.READ_RESEARCH, organization_id
        )
        episodes = history.list(organization_id)
        return {
            "organization_id": organization_id,
            "count": len(episodes),
            "episodes": [
                {
                    "episode_id": item["episode_id"],
                    "conversation_id": item["conversation_id"],
                    "question": item["trace"]["question"],
                    "completed": item["completed"],
                    "iterations": item["iterations"],
                    "created_at": item["trace"]["created_at"],
                    "has_answer": item["answer"] is not None,
                }
                for item in reversed(episodes)
            ],
        }

    @app.get(
        "/v1/organizations/{organization_id}/research/{episode_id}",
        tags=["research"],
    )
    def get_research(
        organization_id: str,
        episode_id: str,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        container.authorization.require(
            principal, Permission.READ_RESEARCH, organization_id
        )
        return history.get(organization_id, episode_id)

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
        "/v1/organizations/{organization_id}/relations/{relation_id}/review",
        tags=["knowledge-review"],
    )
    def request_review_confirmation(
        organization_id: str,
        relation_id: str,
        payload: ReviewRequestInput,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        if payload.action == "reject" and not (payload.reason or "").strip():
            raise DomainError("rejecting a relation requires a reason")
        ticket = container.reviews.request_confirmation(
            principal,
            organization_id,
            relation_id,
            payload.action,
            payload.note,
            payload.reason,
        )
        return {
            "ticket_id": ticket.ticket_id,
            "action": ticket.action,
            "relation_id": ticket.relation_id,
            "payload_hash": ticket.payload_hash,
            "expires_at": ticket.expires_at.isoformat(),
            "instruction": (
                "برای اجرای نهایی، همین عملیات را با همین متن note/reason و ticket_id تأیید کنید."
            ),
        }

    @app.post(
        "/v1/organizations/{organization_id}/relations/{relation_id}/review/confirm",
        tags=["knowledge-review"],
    )
    def confirm_review(
        organization_id: str,
        relation_id: str,
        payload: ReviewConfirmInput,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        relation = container.reviews.confirm(
            principal,
            organization_id,
            relation_id,
            payload.action,
            payload.ticket_id,
            payload.note,
            payload.reason,
            payload.idempotency_key,
        )
        return _relation_response(relation)

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

    @app.get(
        "/v1/organizations/{organization_id}/research-graph",
        tags=["knowledge-review"],
    )
    def research_graph(
        organization_id: str,
        principal: CurrentPrincipal,
    ) -> dict[str, Any]:
        container.authorization.require(
            principal, Permission.READ_RESEARCH, organization_id
        )
        memory = container.reviews.memory
        relations = memory.repository.list_relations(organization_id)
        canonical = container.research.citations.canonical_repository
        node_labels: dict[str, str] = {}

        def resolve(node_id: str) -> str:
            if node_id in node_labels:
                return node_labels[node_id]
            label = _graph_node_label(canonical, node_id)
            node_labels[node_id] = label
            return label

        nodes = tuple(
            {
                "id": node_id,
                "label": resolve(node_id),
                "namespaces": tuple(
                    sorted(
                        {
                            relation.source_namespace.value
                            for relation in relations
                            if relation.source_node_id == node_id
                        }
                        | {
                            relation.target_namespace.value
                            for relation in relations
                            if relation.target_node_id == node_id
                        }
                    )
                ),
            }
            for node_id in sorted(
                {
                    node
                    for relation in relations
                    for node in (
                        relation.source_node_id,
                        relation.target_node_id,
                    )
                }
            )
        )
        events = tuple(
            {
                "event_id": event.event_id,
                "action": event.action.value,
                "actor_id": event.actor_id,
                "target_id": event.target_id,
                "previous_state": event.previous_state.value if event.previous_state else None,
                "new_state": event.new_state.value,
                "reason": event.reason,
                "source_episode_id": event.source_episode_id,
                "timestamp": event.timestamp.isoformat(),
            }
            for event in memory.repository.events(organization_id)
        )
        return {
            "organization_id": organization_id,
            "node_count": len(nodes),
            "edges": [_graph_edge(relation) for relation in relations],
            "nodes": list(nodes),
            "events": list(events),
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


def _sse_frame(event: dict[str, Any]) -> str:
    name = event.get("type", "message")
    return f"event: {name}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _research_response(record: ResearchRecord) -> dict[str, Any]:
    outcome = record.outcome
    result: dict[str, Any] = {
        "organization_id": record.organization_id,
        "episode_id": outcome.episode.episode_id,
        "conversation_id": outcome.episode.conversation_id,
        "user_id": record.user_id,
        "completed": outcome.completed,
        "iterations": outcome.iterations,
        "trace": {
            "created_at": outcome.episode.created_at.isoformat(),
            "question": outcome.episode.question,
            "applicable_time": outcome.episode.applicable_time.isoformat(),
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


def _graph_node_label(canonical: Any, node_id: str) -> str:
    from .errors import NotFoundError

    if node_id.startswith("issue:"):
        return node_id
    try:
        provision = canonical.get_provision(node_id)
    except NotFoundError:
        return node_id
    return f"{provision.label} · {provision.instrument_id}"


def _graph_edge(relation: ProgressiveRelation) -> dict[str, Any]:
    return {
        "edge_id": relation.relation_id,
        "source": relation.source_node_id,
        "target": relation.target_node_id,
        "type": relation.edge_type.value,
        "truth_class": relation.truth_class.value,
        "status": relation.status.value,
        "confidence": relation.confidence,
        "source_episode_id": relation.source_episode_id,
        "approved_by": relation.approval.approved_by if relation.approval else None,
    }


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
