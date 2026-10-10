"""The permission gate: ONE function every tool call passes through, plus the audit log writer.

Used by the agent, the MCP server and the scheduler alike, so no caller can skip it. It decides
ALLOW / NEEDS_APPROVAL / DENY from config/permissions.yaml, and it writes every decision to the
append-only audit_log table. Prompts cannot change any of this.
"""

import json
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from psycopg.types.json import Jsonb

from app.privacy import redact_any
from app.tools.types import Caller, ToolContext, ToolSpec

PERMISSIONS_FILE = Path(__file__).resolve().parents[1] / "config" / "permissions.yaml"

Outcome = Literal["ALLOW", "NEEDS_APPROVAL", "DENY"]


@dataclass
class Decision:
    outcome: Outcome
    code: str = ""
    message: str = ""


@lru_cache
def load_permissions() -> dict[str, Any]:
    return yaml.safe_load(PERMISSIONS_FILE.read_text())


def _normalize(entry: Any) -> tuple[str, list[str]]:
    """A role entry is either 'auto' or {level: auto, rules: [...]}."""
    if isinstance(entry, str):
        return entry, []
    return entry["level"], list(entry.get("rules", []))


# ---------- rules: extra checks that can tighten a level ----------


def _rule_discount_cap(ctx, caller, args, limits) -> Decision | None:
    cap = limits["discount_cap_pct"]
    if args.pct > cap:
        return Decision(
            "DENY",
            "discount_too_high",
            f"Discounts above {cap}% are not allowed. Do not offer one. Tell the customer the owner "
            "reviews special requests and call handoff_to_human if they insist.",
        )
    return None


def _rule_late_change(ctx, caller, args, limits) -> Decision | None:
    row = ctx.conn.execute(
        "SELECT start_at FROM bookings WHERE id = %s AND customer_id = %s AND status = 'BOOKED'",
        (args.booking_id, caller.customer_id),
    ).fetchone()
    if row is None:
        return None  # not found / not yours: the tool's precheck reports that
    if row[0] - ctx.now() < timedelta(hours=limits["late_change_hours"]):
        return Decision(
            "NEEDS_APPROVAL",
            "late_change",
            f"Changes within {limits['late_change_hours']} hours of the visit need the owner's approval.",
        )
    return None


def _rule_max_active_bookings(ctx, caller, args, limits) -> Decision | None:
    n = ctx.conn.execute(
        "SELECT count(*) FROM bookings WHERE customer_id = %s AND status = 'BOOKED' AND start_at > %s",
        (caller.customer_id, ctx.now()),
    ).fetchone()[0]
    if n >= limits["max_active_bookings"]:
        return Decision(
            "DENY",
            "too_many_bookings",
            f"This customer already has {n} upcoming visits (the limit is "
            f"{limits['max_active_bookings']}). Offer to reschedule or cancel one instead.",
        )
    return None


RULES = {
    "discount_cap": _rule_discount_cap,
    "late_change": _rule_late_change,
    "max_active_bookings": _rule_max_active_bookings,
}


def role_decision(tool: str, role: str) -> Decision | None:
    """Step 1, before looking at any arguments: is this tool available to this role at all?

    Checked first so a caller that may not use a tool learns nothing about its arguments."""
    entry = load_permissions()["tools"].get(tool, {}).get(role)
    if entry is None or _normalize(entry)[0] == "forbidden":
        return Decision("DENY", "forbidden", f"The tool {tool} is not available to you.")
    return None


def authorize(ctx: ToolContext, caller: Caller, spec: ToolSpec, args: Any) -> Decision:
    """Step 2, with validated arguments: the level plus the rules (caps, late changes, limits)."""
    perms = load_permissions()
    if (denied := role_decision(spec.name, caller.role)) is not None:
        return denied
    level, rules = _normalize(perms["tools"][spec.name][caller.role])
    if caller.role == "customer" and caller.customer_id is None:
        return Decision("DENY", "no_customer", "No verified customer for this conversation.")

    needs_approval = level == "approval"
    reason = "requires approval" if needs_approval else ""
    for rule_name in rules:
        d = RULES[rule_name](ctx, caller, args, perms["limits"])
        if d is None:
            continue
        if d.outcome == "DENY":
            return d
        needs_approval, reason = True, d.message
    if needs_approval:
        return Decision(
            "NEEDS_APPROVAL",
            "needs_approval",
            reason or "The owner must approve this first.",
        )
    return Decision("ALLOW")


# ---------- audit ----------

_MAX_AUDIT_JSON = 4000  # keep rows small; big results are summarised


def _compact(data: Any) -> Any:
    safe = redact_any(json.loads(json.dumps(data, default=str)))
    if len(json.dumps(safe)) > _MAX_AUDIT_JSON:
        return {"truncated": True, "ok": safe.get("ok") if isinstance(safe, dict) else None}
    return safe


def audit(
    ctx: ToolContext,
    caller: Caller,
    tool: str,
    args: Any,
    outcome: Outcome,
    result: dict[str, Any] | None = None,
    reason: str = "",
) -> None:
    """Append one row to audit_log. Phone numbers in args/result are replaced by their keyed hash."""
    ctx.conn.execute(
        "INSERT INTO audit_log (caller_role, customer_id, tool, args, decision, result, reason, trace_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            caller.role,
            caller.customer_id,
            tool,
            Jsonb(_compact(args)) if args is not None else None,
            outcome,
            Jsonb(_compact(result)) if result is not None else None,
            reason[:500],
            ctx.trace_id,
        ),
    )
