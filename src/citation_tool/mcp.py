"""Small, stateless Streamable HTTP MCP surface for the literature service."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .entities import SearchCreate
from .search import SearchRequestError, SearchService
from .store import Store

PROTOCOL_VERSION = "2025-03-26"


class McpHandler:
    def __init__(self, service: SearchService, store: Store) -> None:
        self.service = service
        self.store = store

    async def handle(self, tenant_id: str, message: dict[str, Any]) -> dict[str, Any] | None:
        if not self._valid_envelope(message):
            return self._error(message.get("id"), -32600, "Invalid JSON-RPC request")
        method = message["method"]
        request_id = message.get("id")
        if method.startswith("notifications/"):
            return None
        if request_id is None:
            return self._error(None, -32600, "Request id is required")
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "citation-tool", "version": "0.3.0"},
                    "instructions": (
                        "检索真实题录/摘要或明确标注的 fixture。"
                        "候选不等于全文证据，不提供删除、导出或权限修改工具。"
                    ),
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": self._tools()})
        if method != "tools/call":
            return self._error(request_id, -32601, "Method not found")
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "Tool arguments must be an object")
        try:
            result = await self._call(tenant_id, name, arguments)
        except (ValidationError, SearchRequestError, ValueError, KeyError) as exc:
            return self._error(request_id, -32602, "Invalid tool arguments", type(exc).__name__)
        return self._result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    }
                ],
                "structuredContent": result,
            },
        )

    async def _call(self, tenant_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "search_literature":
            created, _ = self.service.create(
                tenant_id, SearchCreate.model_validate(arguments), None
            )
            return await self.service.tool_result(tenant_id, created)
        if name == "get_search_state":
            query_id = self._identifier(arguments, "query_id", "q_")
            state = self.service.state(tenant_id, query_id)
            if state is None:
                raise KeyError(query_id)
            return state.model_dump(mode="json")
        if name == "get_snapshot":
            snapshot_id = self._identifier(arguments, "snapshot_id", "snap_")
            snapshot = self.store.get_snapshot(tenant_id, snapshot_id)
            if snapshot is None:
                raise KeyError(snapshot_id)
            return json.loads(snapshot["manifest_json"])
        if name == "get_snapshot_evidence":
            snapshot_id = self._identifier(arguments, "snapshot_id", "snap_")
            evidence_id = self._identifier(arguments, "evidence_id", "ev_")
            evidence = self.store.get_evidence(tenant_id, snapshot_id, evidence_id)
            if evidence is None:
                raise KeyError(evidence_id)
            return evidence
        raise ValueError("unknown tool")

    @staticmethod
    def _identifier(arguments: dict[str, Any], field: str, prefix: str) -> str:
        value = arguments.get(field)
        if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 96:
            raise ValueError(f"invalid {field}")
        return value

    @staticmethod
    def _valid_envelope(message: dict[str, Any]) -> bool:
        return (
            message.get("jsonrpc") == "2.0"
            and isinstance(message.get("method"), str)
            and (
                "id" not in message
                or isinstance(message["id"], str | int)
                and not isinstance(message["id"], bool)
            )
        )

    @staticmethod
    def _result(request_id: str | int, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(
        request_id: str | int | None, code: int, message: str, data: str | None = None
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _tools() -> list[dict[str, Any]]:
        return [
            {
                "name": "search_literature",
                "description": (
                    "并行检索选中文献库并返回独立 query、候选、证据层级、真实模型理由和覆盖。"
                    "真实摘要只能作为待核查候选，不是可引用全文证据。"
                ),
                "inputSchema": SearchCreate.model_json_schema(),
            },
            {
                "name": "get_search_state",
                "description": "按 query_id 读取当前检索、Worker 覆盖、分组和候选诊断。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query_id": {"type": "string"}},
                    "required": ["query_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_snapshot",
                "description": "读取属于当前租户的不可变检索快照。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"snapshot_id": {"type": "string"}},
                    "required": ["snapshot_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_snapshot_evidence",
                "description": "按 snapshot_id 和 evidence_id 读取已保存的证据，不推测缺失定位。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "snapshot_id": {"type": "string"},
                        "evidence_id": {"type": "string"},
                    },
                    "required": ["snapshot_id", "evidence_id"],
                    "additionalProperties": False,
                },
            },
        ]
