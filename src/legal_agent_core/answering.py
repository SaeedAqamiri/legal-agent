from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from .canonical import Citation
from .errors import DomainError
from .llm import LLMService, LLMTask, StructuredOutput
from .repositories import CanonicalRepository
from .research import Evidence
from .verification import (
    AnswerClaim,
    ClaimImportance,
    DraftAnswer,
    EvidenceDisposition,
    EvidenceLedger,
    GenerationMetadata,
    VerificationReport,
)

_ANSWER_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "text": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "string", "enum": [item.value for item in ClaimImportance]},
                },
                "required": ["claim_id", "text", "evidence_ids", "importance"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answer", "claims"],
    "additionalProperties": False,
}


class AnswerCompositionError(DomainError):
    """LLM output could not be converted into the canonical answer contract."""


class AnswerPublicationError(DomainError):
    """A draft failed the evidence-bound publication policy."""


class LLMAnswerComposer:
    def __init__(self, llm_service: LLMService) -> None:
        self.llm_service = llm_service

    def compose(
        self,
        question: str,
        applicable_time: date,
        evidence: tuple[Evidence, ...],
    ) -> DraftAnswer:
        evidence_payload = [self._evidence_payload(item) for item in evidence]
        result = self.llm_service.generate(
            LLMTask.ANSWER_COMPOSITION,
            {
                "question": question,
                "evidence": json.dumps(evidence_payload, ensure_ascii=False, sort_keys=True),
            },
            metadata={
                "applicable_time": applicable_time.isoformat(),
                "evidence_count": str(len(evidence)),
            },
            structured_output=StructuredOutput("legal_answer", _ANSWER_SCHEMA),
        )
        payload = self._parse_payload(result.response.text)
        claims = self._parse_claims(payload["claims"])
        identity = json.dumps(
            {
                "question": question,
                "applicable_time": applicable_time.isoformat(),
                "provider_response_id": result.response.response_id,
                "claims": [claim.claim_id for claim in claims],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        answer_id = f"answer-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        return DraftAnswer(
            answer_id,
            payload["answer"],
            claims,
            GenerationMetadata(
                provider_id=result.provider_id,
                model=result.response.model,
                model_profile_id=result.model_profile_id,
                model_profile_version=result.model_profile_version,
                prompt_id=result.prompt_id,
                prompt_version=result.prompt_version,
                agent_version=result.agent_version,
                provider_response_id=result.response.response_id,
                provider_request_id=result.response.request_id,
            ),
        )

    @staticmethod
    def _evidence_payload(evidence: Evidence) -> dict[str, object]:
        return {
            "evidence_id": evidence.evidence_id,
            "document_version_id": evidence.document_version_id,
            "provision_id": evidence.provision_id,
            "provision_version_id": evidence.provision_version_id,
            "source_span_id": evidence.source_span_id,
            "page": evidence.page,
            "text": evidence.text,
            "stance": evidence.stance.value,
        }

    @staticmethod
    def _parse_payload(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AnswerCompositionError("answer model returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AnswerCompositionError("answer model output must be a JSON object")
        answer = value.get("answer")
        claims = value.get("claims")
        if not isinstance(answer, str) or not answer.strip():
            raise AnswerCompositionError("answer model output is missing answer text")
        if not isinstance(claims, list):
            raise AnswerCompositionError("answer model output is missing claims")
        return {"answer": answer, "claims": claims}

    @staticmethod
    def _parse_claims(raw_claims: list[object]) -> tuple[AnswerClaim, ...]:
        claims: list[AnswerClaim] = []
        for raw in raw_claims:
            if not isinstance(raw, dict):
                raise AnswerCompositionError("each answer claim must be an object")
            claim_id = raw.get("claim_id")
            text = raw.get("text")
            evidence_ids = raw.get("evidence_ids")
            importance = raw.get("importance")
            if not isinstance(claim_id, str) or not isinstance(text, str):
                raise AnswerCompositionError("claim_id and claim text must be strings")
            if not isinstance(evidence_ids, list) or not all(isinstance(item, str) for item in evidence_ids):
                raise AnswerCompositionError("claim evidence_ids must be a string array")
            try:
                parsed_importance = ClaimImportance(importance)
            except (TypeError, ValueError) as exc:
                raise AnswerCompositionError("claim importance is invalid") from exc
            try:
                claims.append(AnswerClaim(claim_id, text, tuple(evidence_ids), parsed_importance))
            except DomainError as exc:
                raise AnswerCompositionError(str(exc)) from exc
        return tuple(claims)


@dataclass(frozen=True, slots=True)
class RenderedCitation:
    marker: str
    evidence_id: str
    citation: Citation
    instrument_title: str
    provision_label: str
    source_uri: str
    rendered_text: str


@dataclass(frozen=True, slots=True)
class ClaimEvidenceMap:
    claim_id: str
    evidence_ids: tuple[str, ...]
    citation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublicationAudit:
    answer_id: str
    applicable_time: date
    verified_evidence_ids: tuple[str, ...]
    generation: GenerationMetadata | None


@dataclass(frozen=True, slots=True)
class PublishedAnswer:
    answer_id: str
    text: str
    rendered_text: str
    claims: tuple[AnswerClaim, ...]
    citations: tuple[RenderedCitation, ...]
    claim_evidence_map: tuple[ClaimEvidenceMap, ...]
    audit: PublicationAudit


class CitationPipeline:
    def __init__(self, canonical_repository: CanonicalRepository) -> None:
        self.canonical_repository = canonical_repository

    def publish(
        self,
        draft: DraftAnswer,
        verification: VerificationReport,
        ledger: EvidenceLedger,
        applicable_time: date,
    ) -> PublishedAnswer:
        if verification.answer_id != draft.answer_id:
            raise AnswerPublicationError("verification report belongs to another answer")
        if verification.applicable_time != applicable_time:
            raise AnswerPublicationError("verification report belongs to another applicable_time")
        if not verification.accepted or verification.requires_more_research:
            raise AnswerPublicationError("answer cannot be published before verification succeeds")

        verified_ids = set(verification.verified_evidence_ids)
        if not verified_ids:
            raise AnswerPublicationError("answer cannot be published without verified evidence")
        ordered_evidence_ids = tuple(
            dict.fromkeys(evidence_id for claim in draft.claims for evidence_id in claim.evidence_ids)
        )
        for claim in draft.claims:
            if not claim.evidence_ids or not set(claim.evidence_ids) <= verified_ids:
                raise AnswerPublicationError(f"claim {claim.claim_id!r} lacks fully verified evidence")

        evidence_by_id: dict[str, Evidence] = {}
        for evidence_id in ordered_evidence_ids:
            entry = ledger.get(evidence_id)
            if entry is None or entry.disposition != EvidenceDisposition.ACCEPTED:
                raise AnswerPublicationError(f"evidence {evidence_id!r} is not accepted in the ledger")
            evidence_by_id[evidence_id] = entry.evidence

        citations = tuple(
            self._citation(draft.answer_id, index, evidence_by_id[evidence_id])
            for index, evidence_id in enumerate(ordered_evidence_ids, start=1)
        )
        citation_by_evidence = {item.evidence_id: item for item in citations}
        claim_map = tuple(
            ClaimEvidenceMap(
                claim.claim_id,
                claim.evidence_ids,
                tuple(citation_by_evidence[item].citation.citation_id for item in claim.evidence_ids),
            )
            for claim in draft.claims
        )
        rendered = self._render(draft, citations, citation_by_evidence)
        return PublishedAnswer(
            answer_id=draft.answer_id,
            text=draft.text,
            rendered_text=rendered,
            claims=draft.claims,
            citations=citations,
            claim_evidence_map=claim_map,
            audit=PublicationAudit(
                draft.answer_id,
                applicable_time,
                verification.verified_evidence_ids,
                draft.generation,
            ),
        )

    def _citation(self, answer_id: str, index: int, evidence: Evidence) -> RenderedCitation:
        provision = self.canonical_repository.get_provision(evidence.provision_id)
        instrument = self.canonical_repository.get_instrument(provision.instrument_id)
        source = self.canonical_repository.get_source_document(evidence.document_id)
        identity = f"{answer_id}|{evidence.evidence_id}"
        citation = Citation(
            citation_id=f"citation-{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
            instrument_id=instrument.instrument_id,
            document_version_id=evidence.document_version_id,
            provision_id=evidence.provision_id,
            provision_version_id=evidence.provision_version_id,
            source_span_id=evidence.source_span_id,
            page=evidence.page,
            quoted_text=evidence.text,
        )
        marker = f"[{index}]"
        rendered = (
            f"{marker} {instrument.title}، {provision.label}، صفحه {evidence.page}، "
            f"نسخه {evidence.document_version_id}: «{evidence.text}»"
        )
        return RenderedCitation(
            marker,
            evidence.evidence_id,
            citation,
            instrument.title,
            provision.label,
            source.source_uri,
            rendered,
        )

    @staticmethod
    def _render(
        draft: DraftAnswer,
        citations: tuple[RenderedCitation, ...],
        citation_by_evidence: Mapping[str, RenderedCitation],
    ) -> str:
        claim_lines = [
            f"- {claim.text} "
            + "".join(citation_by_evidence[evidence_id].marker for evidence_id in claim.evidence_ids)
            for claim in draft.claims
        ]
        source_lines = [item.rendered_text for item in citations]
        return "\n\n".join(
            (
                draft.text,
                "مبنای ادعاها:\n" + "\n".join(claim_lines),
                "منابع:\n" + "\n".join(source_lines),
            )
        )
