"""MCP server: exposes the SAME tool registry and permission gate to Claude Desktop / Cursor.

Run by the MCP host as a subprocess over stdio (no network port):  uv run python -m app.mcp_server

Why it is safe: this file contains no business logic. Every call goes through `run_tool`, so the
gate, the argument validation and the audit log apply exactly as they do for the agent. The caller
is the *owner* role: owner tools only, never customer data tools, and money/bulk actions still
create approval requests that only a human can release (there is deliberately no approve tool).
"""

import inspect
import json
import sys
from collections.abc import Callable
from typing import Annotated, Any

import psycopg
from mcp.server.mcpserver import MCPServer

from app.adapters.fakes import FakeCalendar, FakeCrm, OutboxMessaging
from app.gate import load_permissions
from app.log import setup_logging
from app.settings import get_settings
from app.tools import REGISTRY, Caller, ToolContext, run_tool

OWNER = Caller("owner")


def _owner_tool_names() -> list[str]:
    perms = load_permissions()["tools"]
    return sorted(t for t in REGISTRY if "owner" in perms.get(t, {}) and perms[t]["owner"] != "forbidden")


def _call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """One connection per call: a long-lived stdio server must survive database restarts."""
    s = get_settings()
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        ctx = ToolContext(
            conn=conn,
            calendar=FakeCalendar(conn),  # swapped for Google Calendar in Phase 4
            crm=FakeCrm(),
            messaging=OutboxMessaging(conn, dry_run=s.dry_run),
            settings=s,
        )
        return run_tool(ctx, OWNER, tool, {k: v for k, v in args.items() if v is not None})


def _make_tool_fn(tool: str) -> Callable[..., dict[str, Any]]:
    """Build a function whose signature mirrors the tool's Pydantic model, so MCP publishes the same schema."""
    spec = REGISTRY[tool]
    params = []
    for name, field in spec.args_model.model_fields.items():
        default = inspect.Parameter.empty if field.is_required() else field.default
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=Annotated[field.annotation, field],
            )
        )

    def fn(**kwargs: Any) -> dict[str, Any]:
        return _call(tool, kwargs)

    fn.__name__ = tool
    fn.__doc__ = spec.description
    fn.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    return fn


def build_server() -> MCPServer:
    server = MCPServer(
        "coolbreeze-ops",
        instructions=(
            "Operations tools for CoolBreeze HVAC (owner view). Prices come only from the price table. "
            "Anything that sends messages or moves money is created as an approval request that the "
            "owner approves in Telegram; you cannot approve it."
        ),
    )
    for tool in _owner_tool_names():
        server.add_tool(_make_tool_fn(tool), name=tool, description=REGISTRY[tool].description)

    @server.resource(
        "prices://services",
        name="price_table",
        mime_type="application/json",
        description="The price table: the only prices the company quotes.",
    )
    def price_table() -> str:
        return json.dumps(_call("list_services", {})["services"], indent=2)

    @server.resource(
        "faq://all",
        name="faq",
        mime_type="application/json",
        description="Company policy answers (hours, payment, cancellation, warranty).",
    )
    def faq() -> str:
        s = get_settings()
        with psycopg.connect(s.database_url) as conn:
            rows = conn.execute("SELECT question, answer FROM faq ORDER BY id").fetchall()
        return json.dumps([{"question": q, "answer": a} for q, a in rows], indent=2)

    return server


def main() -> None:
    setup_logging(sys.stderr)  # stdout is the protocol channel: anything printed there would corrupt it
    build_server().run()


if __name__ == "__main__":
    main()
