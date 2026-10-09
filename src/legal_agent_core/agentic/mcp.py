"""Minimal MCP (Model Context Protocol) stdio server for the legal tools.

Speaks newline-delimited JSON-RPC 2.0 with the small MCP surface needed for
tool exposure: ``initialize``, ``notifications/initialized``, ``ping``,
``tools/list`` and ``tools/call``. Dependency-free on purpose — the same
``LegalResearchTools`` surface is served here and called in-process, and the
parity test asserts identical envelopes over both transports.

Run against the in-memory demo corpus:

    python -m legal_agent_core.agentic.mcp
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from ..canonical import InstrumentType, ProvisionType
from ..errors import DomainError
from ..in_memory import InMemoryCanonicalRepository
from .envelope import RefStore
from .tools import TOOL_SPECS, LegalResearchTools

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "legal-agent-tools"
SERVER_VERSION = "0.1.0"

_ARG_PROPERTIES: dict[str, dict[str, Any]] = {
    "query": {"type": "string"},
    "applicable_time": {"type": "string", "format": "date"},
    "provision_id": {"type": "string"},
    "provision_version_id": {"type": "string"},
    "source_span_id": {"type": "string"},
    "document": {"type": "string"},
    "document_scope": {"type": "array", "items": {"type": "string"}},
    "instrument": {"type": "string"},
    "page_from": {"type": "integer", "minimum": 1},
    "page_to": {"type": "integer", "minimum": 1},
    "cursor": {"type": "string"},
}


def tool_schema(name: str) -> dict[str, Any]:
    """Derive the JSON schema from the tool's declared signature.

    ``ToolSpec.args`` marks optional arguments with a trailing ``?``.
    """
    spec = next(item for item in TOOL_SPECS if item.name == name)
    names = [arg.removesuffix("?") for arg in spec.args]
    required = [
        arg.removesuffix("?") for arg in spec.args if not arg.endswith("?")
    ]
    return {
        "type": "object",
        "properties": {arg: _ARG_PROPERTIES[arg] for arg in names},
        "required": required,
        "additionalProperties": True,
    }


class McpToolServer:
    """Session-scoped MCP handler over one ``LegalResearchTools`` instance."""

    def __init__(self, tools: LegalResearchTools) -> None:
        self.tools = tools
        self.store = RefStore()

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        is_notification = request_id is None
        try:
            result = self._dispatch(method, request.get("params") or {})
        except DomainError as exc:
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": str(exc)},
            }
        except Exception as exc:  # noqa: BLE001 — JSON-RPC error envelope
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method in ("notifications/initialized", "initialized"):
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "inputSchema": tool_schema(spec.name),
                    }
                    for spec in TOOL_SPECS
                    if self.tools.spec_available(spec.name)
                ]
            }
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str):
                raise DomainError("tools/call requires a tool name")
            result = self.tools.dispatch(self.store, name, arguments)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            result.envelope.to_payload(),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    }
                ],
                "isError": result.envelope.status.value in ("error", "not_found"),
            }
        raise DomainError(f"unsupported method {method!r}")


@dataclass(slots=True)
class _Streams:
    stdin: TextIO
    stdout: TextIO


def serve(tools: LegalResearchTools, streams: _Streams | None = None) -> None:
    """Blocking JSON-RPC loop over stdio (newline-delimited)."""
    connection = streams or _Streams(sys.stdin, sys.stdout)
    server = McpToolServer(tools)
    for line in connection.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }
        else:
            response = server.handle(request)
        if response is not None:
            connection.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            connection.stdout.flush()


def demo_tools() -> LegalResearchTools:
    """In-memory demo corpus for local MCP runs and the parity test."""
    from datetime import date

    from ..canonical import (
        DocumentStatus,
        DocumentVersion,
        LegalInstrument,
        Provision,
        ProvisionVersion,
        SourceDocument,
        SourceSpan,
    )

    repository = InMemoryCanonicalRepository()

    repository.add_source_document(
        SourceDocument(
            "source-demo", None, "sample-appeal-law.pdf", "application/pdf",
            "sha256:demo-source", 2048, "demo", "https://example.invalid/law.pdf",
        )
    )
    repository.add_instrument(
        LegalInstrument(
            "instrument-demo", "قانون نمونه آیین دادرسی", "قانون نمونه آیین دادرسی",
            InstrumentType.STATUTE, "IR",
        )
    )
    repository.add_document_version(
        DocumentVersion(
            "document-demo-v1", "instrument-demo", "source-demo",
            DocumentStatus.EFFECTIVE, effective_from=date(2024, 1, 1),
        )
    )
    repository.add_provision(
        Provision(
            "provision-demo-336", "instrument-demo", ProvisionType.ARTICLE,
            "336", "ماده ۳۳۶",
        )
    )
    text = "مهلت درخواست تجدیدنظر اشخاص مقیم ایران بیست روز است."
    repository.add_provision_version(
        ProvisionVersion(
            "provision-demo-336-v1", "provision-demo-336", "document-demo-v1",
            text, text, DocumentStatus.EFFECTIVE, "demo", date(2024, 1, 1),
        )
    )
    repository.add_source_span(
        SourceSpan(
            "span-demo-336", "source-demo", "document-demo-v1",
            "provision-demo-336-v1", 42, text, char_start=0, char_end=len(text),
        )
    )
    return LegalResearchTools(repository)


def main() -> int:
    serve(demo_tools())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PROTOCOL_VERSION",
    "McpToolServer",
    "demo_tools",
    "main",
    "serve",
    "tool_schema",
]
