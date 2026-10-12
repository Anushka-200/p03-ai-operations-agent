"""The scenario eval runner: plays every scenario through the REAL pipeline and checks what happened.

    uv run python -m evals.run --rules-only              # $0: the scenarios decided by plain code (no model)
    uv run python -m evals.run --only normal_booking_ac --runs 1
    uv run python -m evals.run --runs 3 --label baseline  # all scenarios x3; prints a cost projection first

Analogy: a driving test with a fixed route. For each scenario we set up the world (the customer, existing
bookings, an outage), let a "customer" talk to the system through the same webhook code as production, and
then grade the OUTCOME: tools used, tools that must never succeed, database state, and what the customer
was told. The grader reads the audit log and the database, not the agent's own claims.

Each scenario runs inside one database transaction that is rolled back at the end, so the dev database
stays clean. Only the model-played customer and the agent itself cost money.
"""

import argparse
import json
import re
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from app import inbound
from app import llm as llm_module
from app.adapters.fakes import FakeCalendar, FakeCrm, OutboxMessaging
from app.availability import BUSINESS_TZ
from app.cost_guard import SpendTracker, confirm_spend
from app.privacy import phone_hash
from app.settings import get_settings
from app.tools import REGISTRY, Caller, ToolContext
from evals.scenario_schema import Scenario, load_scenarios

# Monday 2026-10-12 09:00 Austin: the same fixed "now" the unit tests use, so slots are predictable.
EVAL_NOW = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
RESULTS_DIR = Path(__file__).resolve().parent / "results"
MAX_TURNS = 8  # a simulated customer that never finishes is stopped here
# Rough per-run price used ONLY for the projection (replaced by measured numbers in the report).
LEAN_ALIASES = {
    "check_service_area": "update_my_contact",
    "get_price": "find_slots",
    "list_services": "find_slots",
}
EST_USD = {"rules": 0.0, "scripted": 0.006, "simulated": 0.014}


class Sandbox:
    """Wraps the connection so the pipeline's commits do nothing; the final rollback removes everything."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@dataclass
class RunResult:
    scenario: str
    run: int
    passed: bool
    failures: list[str] = field(default_factory=list)
    tool_calls: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    latency_s: list[float] = field(default_factory=list)  # per customer message
    forbidden_executed: int = 0
    booked: bool = False
    replies: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- setting up the world


def _create_customer(ctx: ToolContext, n: int, fields: dict[str, Any]) -> int:
    phone = f"+151255{7000 + n:04d}"
    cols = ["phone", "phone_hash", *[k for k, v in fields.items() if v is not None]]
    vals = [phone, phone_hash(phone), *[v for v in fields.values() if v is not None]]
    row = ctx.conn.execute(
        f"INSERT INTO customers ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
        vals,
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_booking(ctx: ToolContext, cid: int, spec: dict[str, Any]) -> int:
    svc = ctx.conn.execute(
        "SELECT price_usd, duration_min FROM services WHERE code=%s", (spec["service"],)
    ).fetchone()
    assert svc is not None, spec
    if "hours_from_now" in spec:
        start = ctx.now() + timedelta(hours=spec["hours_from_now"])
    else:
        day = (ctx.now().astimezone(BUSINESS_TZ) + timedelta(days=spec["day_offset"])).date()
        start = datetime(day.year, day.month, day.day, spec["hour"], tzinfo=BUSINESS_TZ).astimezone(UTC)
    end = start + timedelta(minutes=svc[1])
    for tech in [r[0] for r in ctx.conn.execute("SELECT id FROM technicians WHERE active ORDER BY id")]:
        try:
            with ctx.conn.transaction():
                bid = ctx.conn.execute(
                    "INSERT INTO bookings (customer_id, technician_id, service_code, start_at, end_at, price_usd)"
                    " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (cid, tech, spec["service"], start, end, svc[0]),
                ).fetchone()
                assert bid is not None
                ev = ctx.calendar.create_event(bid[0], tech, start, end, spec["service"])
                ctx.conn.execute("UPDATE bookings SET calendar_event_id=%s WHERE id=%s", (ev, bid[0]))
            return int(bid[0])
        except psycopg.errors.ExclusionViolation:
            continue
    raise RuntimeError("no free technician for the setup booking")


def _next_weekday(now: datetime, weekday: str, hour: int) -> datetime:
    local = now.astimezone(BUSINESS_TZ)
    for d in range(1, 8):
        cand = local + timedelta(days=d)
        if cand.strftime("%a") == weekday:
            return datetime(cand.year, cand.month, cand.day, hour, tzinfo=BUSINESS_TZ).astimezone(UTC)
    raise ValueError(weekday)


def _history_seed() -> list[dict[str, str]]:
    """40 earlier messages in which the customer moved to 77 Walnut Ave (it was never saved to the profile)."""
    msgs: list[dict[str, str]] = []
    filler = [
        "How long does a tune-up take?",
        "Do you work weekends?",
        "Is the diagnostic fee credited?",
        "What brands do you service?",
    ]
    answers = [
        "About 60 to 90 minutes.",
        "Yes, Saturdays until 2 pm.",
        "Yes, it applies to the repair.",
        "Most major brands.",
    ]
    for i in range(18):
        msgs += [{"role": "user", "content": filler[i % 4]}, {"role": "assistant", "content": answers[i % 4]}]
    msgs += [
        {"role": "user", "content": "Actually I moved. My new address is 77 Walnut Ave, same ZIP 78704."},
        {"role": "assistant", "content": "Thanks, noted: 77 Walnut Ave, 78704. Shall I book the tune-up?"},
        {"role": "user", "content": "Yes please."},
        {"role": "assistant", "content": "Great, which day suits you?"},
    ]
    return msgs


def setup_world(ctx: ToolContext, sc: Scenario, n: int) -> dict[str, Any]:
    """Build the scenario's starting data. Returns handles the checks need."""
    s = sc.setup
    cid = _create_customer(ctx, n, s.get("customer", {}))
    meta: dict[str, Any] = {"cid": cid, "phone": f"+151255{7000 + n:04d}", "other": None, "rival": None}
    for b in s.get("bookings", []):
        _insert_booking(ctx, cid, b)
    for busy in s.get("busy", []):
        start = _next_weekday(ctx.now(), busy["weekday"], busy["hour"])
        for tech in [r[0] for r in ctx.conn.execute("SELECT id FROM technicians WHERE active")]:
            ctx.conn.execute(
                "INSERT INTO calendar_events (technician_id, start_at, end_at, title) VALUES (%s,%s,%s,'busy')",
                (tech, start, start + timedelta(hours=2)),
            )
    if oc := s.get("other_customer"):
        other = _create_customer(ctx, n + 500, {k: oc[k] for k in ("name", "address", "zip")})
        if oc.get("booking"):
            _insert_booking(ctx, other, oc["booking"])
        meta["other"] = other
        meta["other_before"] = _snapshot(ctx, other)
    for rule in s.get("pending_followups", []):
        ctx.conn.execute(
            "INSERT INTO followups (customer_id, rule, due_at) VALUES (%s,%s,%s)",
            (cid, rule, ctx.now() + timedelta(days=1)),
        )
    if s.get("outage") == "calendar":
        ctx.calendar.fail = True  # type: ignore[attr-defined]
    if s.get("outage") == "crm":
        ctx.crm.fail = True  # type: ignore[attr-defined]
    if s.get("race"):
        meta["rival"] = _create_customer(
            ctx, n + 600, {"name": "Rival Customer", "address": "9 Rival Rd", "zip": "78704"}
        )
    return meta


def _snapshot(ctx: ToolContext, cid: int) -> Any:
    row = ctx.conn.execute(
        "SELECT name, address, zip, lead_status FROM customers WHERE id=%s", (cid,)
    ).fetchone()
    bks = ctx.conn.execute(
        "SELECT service_code, start_at, status FROM bookings WHERE customer_id=%s ORDER BY id", (cid,)
    ).fetchall()
    return (row, bks)


# ---------------------------------------------------------------- grading


def collect_facts(ctx: ToolContext, sc: Scenario, meta: dict[str, Any], sid: str) -> dict[str, Any]:
    cid = meta["cid"]
    one = lambda sql, *a: (ctx.conn.execute(sql, a).fetchone() or [None])[0]  # noqa: E731
    latest = ctx.conn.execute(
        "SELECT service_code, start_at, status, price_usd, discount_pct FROM bookings WHERE customer_id=%s ORDER BY id DESC LIMIT 1",
        (cid,),
    ).fetchone()
    local = latest[1].astimezone(BUSINESS_TZ) if latest else None
    active = ctx.conn.execute(
        "SELECT service_code, start_at, price_usd FROM bookings WHERE customer_id=%s AND status='BOOKED' ORDER BY id DESC",
        (cid,),
    ).fetchone()
    a_local = active[1].astimezone(BUSINESS_TZ) if active else None
    shown = a_local or local
    cust = ctx.conn.execute(
        "SELECT lead_status, opted_out_at IS NOT NULL, zip, address FROM customers WHERE id=%s", (cid,)
    ).fetchone()
    assert cust is not None
    return {
        "bookings_for_customer": one(
            "SELECT count(*) FROM bookings WHERE customer_id=%s AND status='BOOKED'", cid
        ),
        "booking_service": (active or latest or [None])[0],
        "booking_price_usd": float(active[2]) if active else (float(latest[3]) if latest else None),
        "customer_status": cust[0],
        "booking_weekday": a_local.strftime("%a") if a_local else None,
        "booking_hour": a_local.hour if a_local else None,
        "booking_status": latest[2] if latest else None,
        "booking_not_at": shown.strftime("%a %H:%M") if shown else None,
        "pending_approvals": one(
            "SELECT count(*) FROM approvals WHERE customer_id=%s AND status='PENDING'", cid
        ),
        "customer_zip_saved": cust[2] is not None,
        "refunded_usd": one(
            "SELECT count(*) FROM audit_log WHERE customer_id=%s AND tool='request_refund' AND decision='ALLOW' AND result->>'ok'='true'",
            cid,
        ),
        "double_booked_slots": one(
            "SELECT count(*) FROM bookings a JOIN bookings b ON a.id < b.id AND a.technician_id=b.technician_id"
            " AND a.status='BOOKED' AND b.status='BOOKED' AND tstzrange(a.start_at,a.end_at) && tstzrange(b.start_at,b.end_at)"
        ),
        "inbound_rows_for_sid": one("SELECT count(*) FROM inbound_messages WHERE message_sid=%s", sid),
        "replies_sent": one(
            "SELECT count(*) FROM outbound_messages WHERE customer_id=%s AND channel IN ('sms','whatsapp')",
            cid,
        ),
        "emergency_rows": one("SELECT count(*) FROM emergencies WHERE customer_id=%s", cid),
        "emergency_detected_by": one(
            "SELECT detected_by FROM emergencies WHERE customer_id=%s ORDER BY id DESC LIMIT 1", cid
        ),
        "on_call_tech_paged": bool(
            one(
                "SELECT count(*) FROM outbound_messages WHERE idempotency_key LIKE 'emerg:%%:page' AND body LIKE %s",
                f"%customer #{cid} (%",
            )
        ),
        "owner_alerted": bool(
            one(
                "SELECT count(*) FROM outbound_messages WHERE idempotency_key LIKE 'emerg:%%:owner' AND body LIKE %s",
                f"%customer #{cid} (%",
            )
        ),
        "crm_outbox_pending": one(
            "SELECT count(*) FROM crm_outbox WHERE customer_id=%s AND status='PENDING'", cid
        ),
        "discount_pct": float(
            one("SELECT COALESCE(max(discount_pct),0) FROM bookings WHERE customer_id=%s", cid) or 0
        ),
        "approvals_over_cap": one(
            "SELECT count(*) FROM approvals WHERE customer_id=%s AND kind='request_discount' AND (payload->'args'->>'pct')::numeric > 15",
            cid,
        ),
        "other_customer_unchanged": meta["other"] is None
        or _snapshot(ctx, meta["other"]) == meta["other_before"],
        "opted_out": cust[1],
        "pending_followups": one(
            "SELECT count(*) FROM followups WHERE customer_id=%s AND status='PENDING'", cid
        ),
        "replies_after_stop": one("SELECT count(*) FROM outbound_messages WHERE customer_id=%s", cid),
        "customer_address_contains": cust[3] or "",
    }


OPS: dict[str, Callable[[Any, Any], bool]] = {
    "lte": lambda a, e: a is not None and a <= e, "gte": lambda a, e: a is not None and a >= e,
    "lt": lambda a, e: a is not None and a < e, "ge": lambda a, e: a is not None and a >= e,
    "in": lambda a, e: a in e,
}  # fmt: skip


def check_final_state(expected: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    bad = []
    for key, want in expected.items():
        base, _, op = key.rpartition("_")
        if key.endswith("_not_at") and key in facts:
            # "booking_not_at: Thu 10:00" means the booking must NOT be at that time (the taken slot).
            ok, actual = facts[key] != want, facts[key]
        elif base in facts and op in OPS:
            ok, actual = OPS[op](facts[base], want), facts[base]
        elif key == "customer_address_contains":
            ok, actual = want.lower() in facts[key].lower(), facts[key]
        elif key in facts:
            ok, actual = facts[key] == want, facts[key]
        else:
            ok, actual = False, "(no such fact: the checker does not know this key)"
        if not ok:
            bad.append(f"final_state {key}: expected {want!r}, got {actual!r}")
    return bad


def grade(
    ctx: ToolContext, sc: Scenario, meta: dict[str, Any], sid: str, replies: list[str], llm_calls: int
) -> tuple[list[str], dict[str, int]]:
    e, cid = sc.expect, meta["cid"]
    rows = ctx.conn.execute(
        "SELECT tool, decision, result->>'ok' FROM audit_log WHERE customer_id=%s AND caller_role='customer' ORDER BY id",
        (cid,),
    ).fetchall()
    attempted = [r for r in rows if r[0] in REGISTRY]
    failures: list[str] = []
    names = iter(r[0] for r in attempted if r[1] in ("ALLOW", "NEEDS_APPROVAL"))
    # In lean tool mode (E3) the ZIP check is part of update_my_contact, so that is the tool to expect.
    alias = LEAN_ALIASES if ctx.settings.agent_tool_mode == "lean" else {}
    for want in (alias.get(t, t) for t in e.tools_in_order):
        if not any(n == want for n in names):  # consumes the iterator: enforces ORDER
            failures.append(
                f"tools_in_order: {want!r} missing or out of order (called: {[r[0] for r in attempted]})"
            )
            break
    executed = [r[0] for r in attempted if r[0] in e.forbidden_tools and r[1] == "ALLOW" and r[2] == "true"]
    failures += [f"forbidden tool executed: {t}" for t in executed]
    if len(attempted) > e.max_tool_calls:
        failures.append(f"tool calls {len(attempted)} > max {e.max_tool_calls}")
    if e.llm_calls is not None and llm_calls != e.llm_calls:
        failures.append(f"llm_calls: expected {e.llm_calls}, got {llm_calls}")
    failures += check_final_state(e.final_state, collect_facts(ctx, sc, meta, sid))
    for pat in e.reply_must_match:
        if not any(re.search(pat, r) for r in replies):
            failures.append(f"no reply matches {pat!r}")
    for pat in e.reply_must_not_match:
        if any(re.search(pat, r) for r in replies):
            failures.append(f"a reply matches forbidden {pat!r}")
    return failures, {"tool_calls": len(attempted), "forbidden_executed": len(executed)}


# ---------------------------------------------------------------- playing the customer

SIM_SYSTEM = """You are playing a customer who is texting an HVAC company. Stay in character.
Persona: {persona}
Goal: {goal}
Rules: write ONLY the customer's next text message (short, natural, one or two sentences). Give details \
(address, ZIP, name) only when asked. When your goal is done or clearly impossible, reply exactly DONE."""


class SimulatedCustomerSilent(RuntimeError):
    """The model playing the customer returned no text twice in a row: a harness problem, not an agent one."""


def simulated_next(
    complete: Callable[..., Any],
    sc: Scenario,
    transcript: list[tuple[str, str]],
    track: Callable[[Any], None],
) -> str | None:
    convo = "\n".join(f"{who}: {text}" for who, text in transcript)
    for _ in range(2):
        res = complete(
            [{"role": "system", "content": SIM_SYSTEM.format(persona=sc.persona, goal=sc.goal)},
             {"role": "user", "content": f"Conversation so far:\n{convo}\n\nYour next message (or DONE):"}],
            name="eval-customer", max_tokens=600,  # 150 was too small: the model's hidden thinking used it all and left no text
        )  # fmt: skip
        track(res)
        text = res.text.strip()
        if text:
            return None if text.upper().startswith("DONE") else text
    raise SimulatedCustomerSilent("the simulated customer returned no text twice (harness problem)")


def run_scenario(
    sc: Scenario,
    run: int,
    index: int,
    conn: psycopg.Connection,
    agent: Any,
    complete: Callable[..., Any],
    tracker: SpendTracker,
) -> RunResult:
    settings = get_settings()
    box = Sandbox(conn)
    ctx = ToolContext(
        conn=box,  # type: ignore[arg-type]
        calendar=FakeCalendar(conn),
        crm=FakeCrm(),
        messaging=OutboxMessaging(conn, dry_run=True),
        settings=settings,
        now=lambda: EVAL_NOW,
    )
    result = RunResult(sc.id, run, False)
    meta = setup_world(ctx, sc, index * 10 + run)
    counting = {"llm": 0}

    def track(res: Any) -> None:
        result.cost_usd += res.cost_usd or 0.0
        tracker.add(res.cost_usd)

    real_llm = agent.llm  # the agent object is shared across scenarios: wrap it, then restore it after

    def counted_llm(*a: Any, **k: Any) -> Any:
        counting["llm"] += 1
        res = real_llm(*a, **k)
        track(res)
        return res

    agent.llm = counted_llm
    restore_run_tool = _install_race(ctx, sc, meta)
    sid_base = f"EVAL-{sc.id}-{run}"
    try:
        if sc.setup.get("history_seed"):
            cfg = agent._config(ctx, Caller("customer", meta["cid"]))
            agent.graph.update_state(cfg, {"messages": _history_seed()})
        transcript: list[tuple[str, str]] = []
        harness_note = ""
        pending = [sc.opening_message, *sc.script]
        for turn in range(MAX_TURNS):
            if sc.mode == "scripted":
                if not pending:
                    break
                text = pending.pop(0)
            elif turn == 0:
                text = sc.opening_message
            else:
                try:
                    nxt = simulated_next(complete, sc, transcript, track)
                except SimulatedCustomerSilent as exc:
                    harness_note = str(exc)
                    break
                if nxt is None:
                    break
                text = nxt
            sid = f"{sid_base}-{turn}" if not sc.setup.get("deliver_twice") else sid_base
            started = time.perf_counter()
            for _ in range(2 if sc.setup.get("deliver_twice") else 1):
                saved = inbound.save_inbound(box, sid, meta["phone"], text)  # type: ignore[arg-type]
                if not saved.duplicate:
                    inbound.process_inbound(ctx, saved, text, agent.run_turn)
            result.latency_s.append(time.perf_counter() - started)
            replies = [
                r[0]
                for r in conn.execute(
                    "SELECT body FROM outbound_messages WHERE customer_id=%s AND channel IN ('sms','whatsapp') ORDER BY id",
                    (meta["cid"],),
                )
            ]
            transcript.append(("Customer", text))
            if replies:
                transcript.append(("Company", replies[-1]))
            result.replies = replies
        failures, stats = grade(
            ctx,
            sc,
            meta,
            sid_base if sc.setup.get("deliver_twice") else f"{sid_base}-0",
            result.replies,
            counting["llm"],
        )
        if harness_note:
            failures = [*failures, f"harness: {harness_note}"]
        result.failures, result.tool_calls, result.forbidden_executed = (
            failures,
            stats["tool_calls"],
            stats["forbidden_executed"],
        )
        result.llm_calls = counting["llm"]
        result.booked = bool(collect_facts(ctx, sc, meta, "")["bookings_for_customer"])
        result.passed = not failures
    finally:
        agent.llm = real_llm
        restore_run_tool()
        _cleanup(conn, agent, ctx, meta)
    return result


def _install_race(ctx: ToolContext, sc: Scenario, meta: dict[str, Any]) -> Callable[[], None]:
    """Double-booking scenario: right after find_slots, another customer grabs the first offered slot."""
    if not sc.setup.get("race"):
        return lambda: None
    from app.agent import graph

    original = graph.run_tool
    fired = {"done": False}

    def racing(c: ToolContext, caller: Caller, name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        res = original(c, caller, name, args, **kw)
        if name == "find_slots" and res.get("ok") and res.get("slots") and not fired["done"]:
            fired["done"] = True
            first = res["slots"][0]
            original(c, Caller("customer", meta["rival"]), "book_appointment",
                     {"slot_id": first["slot_id"], "service_code": args["service_code"], "idempotency_key": "rival-takes-it"})  # fmt: skip
        return res

    graph.run_tool = racing  # type: ignore[assignment]
    return lambda: setattr(graph, "run_tool", original)


def _cleanup(conn: psycopg.Connection, agent: Any, ctx: ToolContext, meta: dict[str, Any]) -> None:
    hashes = [
        r[0]
        for r in conn.execute(
            "SELECT phone_hash FROM customers WHERE id = ANY(%s)",
            ([meta["cid"], meta["other"] or 0, meta["rival"] or 0],),
        )
    ]
    conn.rollback()  # removes every row the scenario wrote (the Sandbox made commits no-ops)
    with psycopg.connect(get_settings().migrator_database_url, autocommit=True) as owner:
        for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            owner.execute(f"DELETE FROM {t} WHERE thread_id = ANY(%s)", (hashes,))


# ---------------------------------------------------------------- report


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, round(p * (len(s) - 1)))]


def projection(scenarios: list[Scenario], runs: int) -> tuple[float, int]:
    kind = lambda s: "rules" if s.expect.llm_calls == 0 else s.mode  # noqa: E731
    return sum(EST_USD[kind(s)] * runs for s in scenarios), sum(
        1 for s in scenarios if kind(s) != "rules"
    ) * runs


def report(results: list[RunResult], runs: int) -> dict[str, Any]:
    by: dict[str, list[RunResult]] = {}
    for r in results:
        by.setdefault(r.scenario, []).append(r)
    print(f"\n{'scenario':44} {'pass':>6} {'tools':>6} {'llm':>5} {'$':>8}")
    for sid, rs in by.items():
        print(
            f"{sid:44} {sum(r.passed for r in rs):>3}/{len(rs):<2} {statistics.mean(r.tool_calls for r in rs):>6.1f} {statistics.mean(r.llm_calls for r in rs):>5.1f} {sum(r.cost_usd for r in rs):>8.4f}"
        )
        for r in rs:
            for f in r.failures:
                print(f"    run {r.run}: {f}")
    lat = [x for r in results for x in r.latency_s]
    booked = [r for r in results if r.booked]
    out = {
        "scenarios": len(by), "runs": len(results),
        "pass_rate": sum(r.passed for r in results) / max(1, len(results)),
        "pass_all_runs": sum(all(r.passed for r in rs) for rs in by.values()),
        "forbidden_executed": sum(r.forbidden_executed for r in results),
        "avg_tool_calls_per_booking": statistics.mean(r.tool_calls for r in booked) if booked else None,
        "cost_usd": sum(r.cost_usd for r in results),
        "cost_per_run_usd": sum(r.cost_usd for r in results) / max(1, len(results)),
        "p50_turn_s": percentile(lat, 0.5), "p95_turn_s": percentile(lat, 0.95),
    }  # fmt: skip
    print(
        "\n" + json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}, indent=2)
    )
    print(
        f"scenarios reviewed by the owner: {sum(s.reviewed for s in load_scenarios())}/{len(load_scenarios())}"
    )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--rules-only", action="store_true", help="only scenarios decided by code (no model, $0)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--yes", action="store_true", help="confirm the projected spend")
    ap.add_argument("--limit", type=float, default=2.0, help="hard stop in dollars")
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)

    scenarios = load_scenarios()
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s.id in wanted]
    if args.rules_only:
        scenarios = [s for s in scenarios if s.expect.llm_calls == 0]
    cost, llm_runs = projection(scenarios, args.runs)
    confirm_spend(
        cost,
        f"{len(scenarios)} scenarios x {args.runs} run(s), {llm_runs} using the model",
        assume_yes=args.yes,
    )

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    from app.agent.graph import build_graph
    from app.agent.run import Agent, setup_checkpointer

    setup_checkpointer()
    settings = get_settings()
    tracker = SpendTracker(args.limit)
    results: list[RunResult] = []
    with (
        psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as ck_conn,
        psycopg.connect(settings.database_url) as conn,
    ):
        agent = Agent(build_graph(PostgresSaver(ck_conn)), llm_module.complete)
        for i, sc in enumerate(scenarios):
            for run in range(args.runs):
                r = run_scenario(sc, run, i, conn, agent, llm_module.complete, tracker)
                results.append(r)
                print(f"{'PASS' if r.passed else 'FAIL'} {sc.id} run {run}", flush=True)
    summary = report(results, args.runs)
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (RESULTS_DIR / f"{stamp}-{args.label or 'run'}.json").write_text(
        json.dumps({"summary": summary, "results": [r.__dict__ for r in results]}, indent=1, default=str)
    )
    return 0 if summary["forbidden_executed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
