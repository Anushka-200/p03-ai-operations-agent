"""Tool registry and the single entry point `run_tool` (validate -> gate -> run -> audit).

Everything that can act on the world is registered here once. The agent, the MCP server and the
tests all call `run_tool`; none of them calls a tool function directly.
"""

import logging
from collections.abc import Callable
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from app import gate
from app.tools.types import Caller, ToolContext, ToolError, ToolSpec, error_result

logger = logging.getLogger(__name__)

REGISTRY: dict[str, ToolSpec] = {}


def register(
    name: str,
    args_model: type[BaseModel],
    description: str,
    precheck: Callable[[ToolContext, Caller, Any], None] | None = None,
):
    """Decorator: add a tool function to the registry."""

    def wrap(fn):
        REGISTRY[name] = ToolSpec(name, description, args_model, fn, precheck)
        return fn

    return wrap


def _validation_message(spec: ToolSpec, exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        field = ".".join(str(x) for x in err["loc"]) or "arguments"
        if err["type"] == "extra_forbidden":
            parts.append(
                f"'{field}' is not an accepted argument (the customer is identified automatically; "
                "never pass ids that identify who the customer is)"
            )
        else:
            parts.append(f"{field}: {err['msg']}")
    allowed = ", ".join(spec.args_model.model_fields) or "(none)"
    return f"Invalid arguments for {spec.name}: " + "; ".join(parts) + f". Accepted arguments: {allowed}."


def _create_approval(ctx: ToolContext, caller: Caller, spec: ToolSpec, args: BaseModel) -> int:
    payload = {
        "tool": spec.name,
        "args": args.model_dump(mode="json"),
        "caller": {"role": caller.role, "customer_id": caller.customer_id},
    }
    # The model may ask twice for the same thing: reuse the pending request instead of piling up.
    row = ctx.conn.execute(
        "SELECT id FROM approvals WHERE kind = %s AND customer_id IS NOT DISTINCT FROM %s"
        " AND status = 'PENDING' AND payload = %s",
        (spec.name, caller.customer_id, Jsonb(payload)),
    ).fetchone()
    if row:
        return row[0]
    created = ctx.conn.execute(
        "INSERT INTO approvals (kind, customer_id, payload, requested_by) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        (spec.name, caller.customer_id, Jsonb(payload), caller.role),
    ).fetchone()
    assert created is not None
    return created[0]


def run_tool(
    ctx: ToolContext,
    caller: Caller,
    name: str,
    raw_args: dict[str, Any] | None,
    *,
    approved_by: str | None = None,
) -> dict[str, Any]:
    """Run one tool call safely. Always returns a dict: {"ok": True, ...} or {"ok": False, error, message}."""
    spec = REGISTRY.get(name)
    if spec is None:
        result = error_result(
            "unknown_tool",
            f"There is no tool named '{name}'. Available: {', '.join(sorted(REGISTRY))}.",
        )
        gate.audit(ctx, caller, name, raw_args, "DENY", result, "unknown tool")
        return result

    if (denied := gate.role_decision(name, caller.role)) is not None:
        result = error_result(denied.code, denied.message)
        gate.audit(ctx, caller, name, raw_args, "DENY", result, denied.code)
        return result

    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        result = error_result("invalid_arguments", _validation_message(spec, exc))
        gate.audit(ctx, caller, name, raw_args, "DENY", result, "invalid arguments")
        return result

    decision = gate.authorize(ctx, caller, spec, args)
    if decision.outcome == "DENY":
        result = error_result(decision.code, decision.message)
        gate.audit(ctx, caller, name, args.model_dump(mode="json"), "DENY", result, decision.code)
        return result

    if decision.outcome == "NEEDS_APPROVAL" and approved_by is None:
        try:
            if spec.precheck:
                spec.precheck(ctx, caller, args)
            with ctx.conn.transaction():
                approval_id = _create_approval(ctx, caller, spec, args)
        except ToolError as exc:
            result = error_result(exc.code, exc.message)
            gate.audit(ctx, caller, name, args.model_dump(mode="json"), "DENY", result, exc.code)
            return result
        result = error_result(
            "needs_approval",
            f"{decision.message} A request (#{approval_id}) was sent to the owner. Tell the customer "
            "it is pending and that they will be told the outcome. Do not promise it will be approved.",
            approval_id=approval_id,
        )
        gate.audit(ctx, caller, name, args.model_dump(mode="json"), "NEEDS_APPROVAL", result, decision.code)
        return result

    try:
        with ctx.conn.transaction():  # all-or-nothing: a failing tool leaves no half-done writes
            if spec.precheck:
                spec.precheck(ctx, caller, args)
            result = spec.fn(ctx, caller, args)
    except ToolError as exc:
        result = error_result(exc.code, exc.message)
    except Exception:
        logger.exception("tool crashed", extra={"tool": name})
        result = error_result(
            "internal_error",
            "Something went wrong on our side. Do not retry; call handoff_to_human.",
        )
    reason = f"approved by {approved_by}" if approved_by else ""
    gate.audit(ctx, caller, name, args.model_dump(mode="json"), "ALLOW", result, reason)
    return result


def decide_approval(ctx: ToolContext, approval_id: int, approve: bool, decided_by: str) -> dict[str, Any]:
    """A HUMAN (the owner) approves or rejects a pending request.

    Deliberately NOT a registered tool: the model can never call it. Approving re-runs the original
    tool call, and the gate's other checks (caps, scope) still apply."""
    with ctx.conn.transaction():
        row = ctx.conn.execute(
            "SELECT status, payload FROM approvals WHERE id = %s FOR UPDATE", (approval_id,)
        ).fetchone()
        if row is None:
            return error_result("not_found", f"No approval #{approval_id}.")
        if row[0] != "PENDING":
            return error_result("already_decided", f"Approval #{approval_id} is already {row[0]}.")
        ctx.conn.execute(
            "UPDATE approvals SET status = %s, decided_by = %s, decided_at = %s WHERE id = %s",
            ("APPROVED" if approve else "REJECTED", decided_by, ctx.now(), approval_id),
        )
    payload = row[1]
    caller = Caller(payload["caller"]["role"], payload["caller"]["customer_id"])
    if not approve:
        gate.audit(ctx, caller, payload["tool"], payload["args"], "DENY", None, f"rejected by {decided_by}")
        return {"ok": True, "status": "REJECTED", "approval_id": approval_id}
    result = run_tool(ctx, caller, payload["tool"], payload["args"], approved_by=decided_by)
    return {"ok": True, "status": "APPROVED", "approval_id": approval_id, "result": result}
