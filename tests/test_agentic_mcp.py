"""Parity tests: MCP stdio transport vs direct in-process tool dispatch."""

from __future__ import annotations

import io
import json
import unittest

from legal_agent_core.agentic.envelope import RefStore
from legal_agent_core.agentic.mcp import McpToolServer, demo_tools, tool_schema
from legal_agent_core.agentic.tools import TOOL_SPECS


def _request(method: str, params: dict | None = None, request_id: int | None = 1) -> dict:
    request: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return request


class McpHandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = McpToolServer(demo_tools())

    def test_initialize_and_ping(self) -> None:
        response = self.server.handle(_request("initialize", {}))
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(response["result"]["serverInfo"]["name"], "legal-agent-tools")

        notification = self.server.handle(
            _request("notifications/initialized", request_id=None)
        )
        self.assertIsNone(notification)

        pong = self.server.handle(_request("ping", request_id=2))
        self.assertEqual(pong["result"], {})

    def test_tools_list_matches_catalog(self) -> None:
        response = self.server.handle(_request("tools/list"))
        tools = response["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            [spec.name for spec in TOOL_SPECS],
        )
        schema = tool_schema("search_provisions")
        self.assertEqual(schema["required"], ["query", "applicable_time"])
        self.assertIn("document_scope", schema["properties"])

    def test_unknown_method_is_jsonrpc_error(self) -> None:
        response = self.server.handle(_request("tools/unknown"))
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32602)


class McpParityTests(unittest.TestCase):
    """Same tool call over both transports must return the same envelope."""

    CALLS: tuple[tuple[str, dict], ...] = (
        (
            "search_provisions",
            {"query": "مهلت اعتراض", "applicable_time": "2025-06-01"},
        ),
        ("get_provision_version", {"provision_version_id": "provision-demo-336-v1", "applicable_time": "2025-06-01"}),
        ("temporal_check", {"provision_id": "provision-demo-336", "applicable_time": "2025-06-01"}),
        ("temporal_check", {"provision_id": "ghost", "applicable_time": "2025-06-01"}),
    )

    def test_envelope_payloads_are_identical(self) -> None:
        direct_tools = demo_tools()
        direct_store = RefStore()
        mcp_server = McpToolServer(demo_tools())

        for name, args in self.CALLS:
            with self.subTest(tool=name, args=args):
                direct = direct_tools.dispatch(direct_store, name, dict(args))
                response = mcp_server.handle(
                    _request("tools/call", {"name": name, "arguments": dict(args)})
                )
                self.assertNotIn("error", response)
                content = response["result"]["content"]
                self.assertEqual(content[0]["type"], "text")
                over_mcp = json.loads(content[0]["text"])
                self.assertEqual(over_mcp, direct.envelope.to_payload())

    def test_stdio_frame_loop(self) -> None:
        tools = demo_tools()
        lines = [
            json.dumps(_request("initialize", {})),
            json.dumps(_request("tools/call", {
                "name": "search_provisions",
                "arguments": {"query": "مهلت", "applicable_time": "2025-06-01"},
            }, request_id=7)),
            "not-json",  # parse errors get an error frame with null id
            "",
        ]
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()
        from legal_agent_core.agentic.mcp import _Streams, serve

        serve(tools, _Streams(stdin, stdout))
        frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0]["id"], 1)
        self.assertEqual(frames[1]["id"], 7)
        payload = json.loads(frames[1]["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(frames[2]["error"]["code"], -32700)
        self.assertIsNone(frames[2]["id"])


if __name__ == "__main__":
    unittest.main()
