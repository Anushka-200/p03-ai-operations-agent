"""E6: the same read-only tools in-process vs through the MCP server (stdio subprocess).

Measures (1) that both paths return identical results and (2) the extra latency MCP adds per call.
Scope note (ADR 004): the MCP server is owner-role only, so this compares the owner's read tools. Running
the customer agent through MCP would need one server per verified customer and committed data (the eval
harness rolls back), which is not worth building for a measurement.

Usage: uv run python -m evals.bench_mcp        (no model calls, $0; leaves no rows behind)
"""

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import psycopg
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from app.adapters.fakes import FakeCalendar, FakeCrm, OutboxMessaging
from app.settings import get_settings
from app.tools import Caller, ToolContext, run_tool

ROOT = Path(__file__).resolve().parents[1]
CALLS = [
    ("list_services", {}),
    ("get_price", {"service_code": "ac_diagnostic"}),
    ("list_leads", {}),
    ("list_jobs", {"day": "2026-10-13"}),
]
N = 30


def pct(values: list[float], p: float) -> float:
    xs = sorted(values)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


async def via_mcp() -> tuple[dict[str, list[float]], dict[str, str], float]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        cwd=str(ROOT),
        env={"PHONE_HASH_KEY": "bench-key"},
    )
    times: dict[str, list[float]] = {n: [] for n, _ in CALLS}
    out: dict[str, str] = {}
    t0 = time.perf_counter()
    async with Client(params, read_timeout_seconds=30) as client:
        startup = time.perf_counter() - t0
        for name, args in CALLS:
            for _ in range(N):
                t = time.perf_counter()
                res = await client.call_tool(name, args)
                times[name].append(time.perf_counter() - t)
            out[name] = json.dumps(
                res.structured_content or getattr(res.content[0], "text", ""), sort_keys=True, default=str
            )
    return times, out, startup


def in_process() -> tuple[dict[str, list[float]], dict[str, str]]:
    s = get_settings()
    times: dict[str, list[float]] = {n: [] for n, _ in CALLS}
    out: dict[str, str] = {}
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        ctx = ToolContext(
            conn=conn,
            calendar=FakeCalendar(conn),
            crm=FakeCrm(),
            messaging=OutboxMessaging(conn, dry_run=True),
            settings=s,
        )
        for name, args in CALLS:
            for _ in range(N):
                t = time.perf_counter()
                res = run_tool(ctx, Caller("owner"), name, args)
                times[name].append(time.perf_counter() - t)
            out[name] = json.dumps(res, sort_keys=True, default=str)
    return times, out


def normalise(text: str) -> object:
    data = json.loads(text)
    return (
        data.get("result", data) if isinstance(data, dict) and "result" in data and len(data) == 1 else data
    )


def main() -> int:
    s = get_settings()
    with psycopg.connect(s.migrator_database_url, autocommit=True) as owner:
        row = owner.execute("SELECT coalesce(max(id), 0) FROM audit_log").fetchone()
        last = row[0] if row else 0
        try:
            local_t, local_out = in_process()
            mcp_t, mcp_out, startup = asyncio.run(via_mcp())
        finally:
            owner.execute("DELETE FROM audit_log WHERE id > %s", (last,))  # the bench's own audit rows
    print(
        f"{'tool':16} {'same result':12} {'in-process p50/p95 ms':24} {'MCP p50/p95 ms':20} {'extra p50 ms':>12}"
    )
    extra = []
    for name, _ in CALLS:
        same = (
            normalise(local_out[name]) == normalise(mcp_out[name]) and '"ok": true' in local_out[name]
        )  # an error on both sides is not "same"
        lp50, lp95 = statistics.median(local_t[name]) * 1000, pct(local_t[name], 0.95) * 1000
        mp50, mp95 = statistics.median(mcp_t[name]) * 1000, pct(mcp_t[name], 0.95) * 1000
        extra.append(mp50 - lp50)
        print(
            f"{name:16} {str(same):12} {lp50:9.1f} / {lp95:8.1f}      {mp50:8.1f} / {mp95:7.1f}   {mp50 - lp50:12.1f}"
        )
    print(
        f"MCP server start-up + handshake: {startup * 1000:.0f} ms (once per session); mean extra per call: {statistics.mean(extra):.1f} ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
