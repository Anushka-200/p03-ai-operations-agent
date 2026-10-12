"""Talks to the MCP server the way Claude Desktop does: a real subprocess over stdio."""

import asyncio
import json
import os
import sys
from pathlib import Path

import psycopg
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from app.mcp_server import _owner_tool_names
from app.settings import get_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True, scope="module")
def _remove_audit_rows_this_module_creates():
    """The server commits for real (separate process), so clean up its audit rows afterwards."""
    try:
        owner = psycopg.connect(get_settings().migrator_database_url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        yield
        return
    last = owner.execute("SELECT coalesce(max(id), 0) FROM audit_log").fetchone()[0]
    yield
    owner.execute("DELETE FROM audit_log WHERE id > %s", (last,))
    owner.close()


def _need_db():
    try:
        psycopg.connect(get_settings().database_url, connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running (make up)")


def _run(coro_fn):
    _need_db()
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(ROOT),
        # the MCP client forwards only a minimal environment, so pass the database URLs on explicitly
        env={"PHONE_HASH_KEY": "mcp-test-key", **{k: os.environ[k] for k in ("DATABASE_URL", "MIGRATOR_DATABASE_URL") if k in os.environ}},
    )  # fmt: skip

    async def main():
        async with Client(params, read_timeout_seconds=30) as client:
            return await coro_fn(client)

    return asyncio.run(asyncio.wait_for(main(), timeout=60))


def _json(result) -> dict:
    return result.structured_content or json.loads(result.content[0].text)


def test_server_lists_exactly_the_owner_tools_with_real_schemas():
    async def go(client):
        return (await client.list_tools()).tools

    tools = _run(go)
    assert sorted(t.name for t in tools) == _owner_tool_names()
    assert "get_my_profile" not in {t.name for t in tools}  # customer-only tools are not exposed
    assert "book_appointment" not in {t.name for t in tools}
    discount = next(t for t in tools if t.name == "request_discount")
    assert set(discount.input_schema["properties"]) == {"booking_id", "pct", "reason"}
    assert "can NOT grant" in discount.description  # the description the model reads


def test_a_read_tool_works_and_prices_come_from_the_table():
    async def go(client):
        return await client.call_tool("get_price", {"service_code": "ac_diagnostic"})

    assert _json(_run(go))["price_usd"] == 89.0


def test_a_tool_the_owner_may_not_use_is_unknown_to_the_server():
    async def go(client):
        try:
            return await client.call_tool("get_my_profile", {})
        except Exception as exc:  # noqa: BLE001 - the SDK raises for unknown tools
            return exc

    out = _run(go)
    assert isinstance(out, Exception) or getattr(out, "is_error", False)


def test_bulk_send_through_mcp_only_creates_an_approval_request():
    async def go(client):
        draft = _json(
            await client.call_tool(
                "draft_bulk_followup", {"since_days": 1, "message": "Still need AC help? Reply to book."}
            )
        )
        sent = _json(await client.call_tool("send_bulk_followup", {"draft_id": draft["draft_id"]}))
        return draft, sent

    draft, sent = _run(go)
    assert sent["error"] == "needs_approval"
    with psycopg.connect(get_settings().migrator_database_url, autocommit=True) as owner:
        n = owner.execute(
            "SELECT count(*) FROM outbound_messages WHERE channel = 'sms' AND idempotency_key LIKE %s",
            (f"bulk:{draft['draft_id']}:%",),
        ).fetchone()[0]
        assert n == 0  # nothing was sent: a human has to approve first
        owner.execute("DELETE FROM approvals WHERE id = ANY(%s)", ([draft["draft_id"], sent["approval_id"]],))
        owner.execute(
            "DELETE FROM audit_log WHERE tool IN ('draft_bulk_followup','send_bulk_followup') AND caller_role = 'owner'"
        )


def test_resources_are_readable():
    async def go(client):
        resources = (await client.list_resources()).resources
        body = await client.read_resource("prices://services")
        return resources, body

    resources, body = _run(go)
    assert {str(r.uri) for r in resources} >= {"prices://services", "faq://all"}
    assert "ac_diagnostic" in body.contents[0].text
