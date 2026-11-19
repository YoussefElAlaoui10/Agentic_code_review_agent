"""Expose the reviewer over MCP (JSON-RPC 2.0 on stdio).

The deliverable asks for "a CLI or an MCP server"; this is the second one, so the
same engine can be driven from an editor (Zed, Continue, Claude Code) instead of
a terminal. Only the handful of methods a host actually calls are implemented --
`initialize`, `tools/list`, `tools/call` -- which is enough to be usable and
small enough to audit.

Transport detail: stdin is read in a worker thread via `asyncio.to_thread`, so a
blocking `readline` never freezes the event loop that the reviewer agents run
on. Blocking stdin on the loop would serialise every review -- exactly the
failure mode this project is about.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .ghclient import TargetError, fetch_pr_files, load_diff_file, parse_pr_target
from .judge import JudgeConfig
from .llm import LLMClient
from .orchestrator import OrchestratorConfig, ReviewOrchestrator
from .render import render_markdown
from .reviewer import DEFAULT_ROLES, ROLES

PROTOCOL_VERSION = "2024-11-05"

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "review_pull_request",
        "description": (
            "Review a GitHub pull request, or a local unified diff, with parallel "
            "reviewer agents and a judge that deduplicates and filters findings. "
            "Returns a markdown report."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string",
                           "description": "PR URL, owner/repo#N, or a path to a .diff/.patch file."},
                "concurrency": {"type": "integer", "default": 6,
                                "description": "Maximum reviewer agents in flight."},
                "roles": {"type": "array", "items": {"type": "string"},
                          "description": f"Reviewer roles; any of {sorted(ROLES)}."},
                "min_confidence": {"type": "number", "default": 0.45},
                "repo_root": {"type": "string",
                              "description": "Local checkout, enabling the grep and context tools."},
            },
            "required": ["target"],
        },
    },
    {
        "name": "list_reviewer_roles",
        "description": "List the available reviewer roles and what each one looks for.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class MCPServer:
    def __init__(self, client: LLMClient, *, roles: Sequence[str] = DEFAULT_ROLES) -> None:
        self.client = client
        self.roles = tuple(roles)

    # -- dispatch ----------------------------------------------------------- #

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method", "")
        req_id = request.get("id")
        # A notification (no id) gets no response, per JSON-RPC.
        if req_id is None and method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "prreview", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": TOOL_SCHEMAS}
            elif method == "tools/call":
                result = await self._call_tool(request.get("params") or {})
            elif method == "ping":
                result = {}
            else:
                return self._error(req_id, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except TargetError as exc:
            return self._error(req_id, -32602, str(exc))
        except Exception as exc:  # a crash must not kill the server
            return self._error(req_id, -32603, f"{type(exc).__name__}: {exc}")

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "list_reviewer_roles":
            body = "\n".join(f"- **{r}**: {ROLES[r]}" for r in sorted(ROLES))
            return {"content": [{"type": "text", "text": body}]}
        if name != "review_pull_request":
            return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}],
                    "isError": True}

        target = str(args.get("target") or "").strip()
        if not target:
            return {"content": [{"type": "text", "text": "target is required"}],
                    "isError": True}

        if Path(target).exists():
            files, label = load_diff_file(target), f"{target} (local diff)"
        else:
            ref = parse_pr_target(target)
            files, label = await fetch_pr_files(ref), ref.slug

        roles = tuple(r for r in (args.get("roles") or self.roles) if r in ROLES) or self.roles
        cfg = OrchestratorConfig(
            concurrency=max(1, int(args.get("concurrency") or 6)),
            roles=roles,
            judge=JudgeConfig(min_confidence=float(args.get("min_confidence") or 0.45)),
        )
        root = args.get("repo_root")
        report = await ReviewOrchestrator(self.client, cfg).run(
            files, target=label, repo_root=Path(root) if root else None)
        return {"content": [{"type": "text", "text": render_markdown(report)}]}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}


async def serve(client: LLMClient, *, roles: Sequence[str] = DEFAULT_ROLES,
                stdin: Any = None, stdout: Any = None) -> None:
    """Read line-delimited JSON-RPC from stdin until EOF."""
    server = MCPServer(client, roles=roles)
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    print("[mcp] prreview server ready on stdio", file=sys.stderr, flush=True)

    while True:
        line = await asyncio.to_thread(inp.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            out.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}}) + "\n")
            out.flush()
            continue
        response = await server.handle(request)
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()
