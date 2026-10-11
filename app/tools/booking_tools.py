"""Tools that create or change bookings and money. Every one runs behind the permission gate."""

from datetime import timedelta
from typing import Any

import psycopg.errors
from psycopg.types.json import Jsonb
from pydantic import Field

from app import crm_sync, followups
from app.adapters.ports import AdapterUnavailable
from app.availability import (
    BUSINESS_TZ,
    available_slots,
    check_bookable,
    choose_offers,
    parse_slot_id,
)
from app.tools.common import ToolArgs, booking_summary, get_customer, get_service, load_booking
from app.tools.registry import register
from app.tools.types import Caller, ToolContext, ToolError

_REASON_TEXT = {
    "taken": "that time was just taken",
    "too_soon": "that time is too soon (we need at least 2 hours notice)",
    "outside_hours": "that time is outside business hours (Mon-Sat, 8am-6pm)",
    "unknown_slot": "that slot_id is not valid",
}


def _idempotent_lookup(ctx: ToolContext, key: str) -> dict[str, Any] | None:
    """Return the stored result if this exact request was already done.

    The advisory lock makes two simultaneous requests with the same key take turns: the second one
    waits, then finds the first one's result instead of booking again."""
    ctx.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
    row = ctx.conn.execute("SELECT result FROM idempotency_keys WHERE key = %s", (key,)).fetchone()
    return row[0] if row else None


def _idempotent_store(ctx: ToolContext, key: str, tool: str, result: dict[str, Any]) -> None:
    ctx.conn.execute(
        "INSERT INTO idempotency_keys (key, tool, result) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (key, tool, Jsonb(result)),
    )


def _slot_error(ctx: ToolContext, reason: str, service: dict[str, Any]) -> ToolError:
    """A recoverable 'slot unavailable' error that already contains the next free times."""
    try:
        d = ctx.now().astimezone(BUSINESS_TZ).date()
        offers = choose_offers(available_slots(ctx, service["duration_min"], d, d + timedelta(days=6)))
        options = "; ".join(f"{s.label} (slot_id {s.slot_id})" for s in offers)
    except AdapterUnavailable:
        options = ""
    hint = (
        f" Free times: {options}. Offer these; do not retry the same slot."
        if options
        else (" Call find_slots for new options.")
    )
    return ToolError("slot_unavailable", f"Sorry, {_REASON_TEXT.get(reason, reason)}.{hint}")


def _require_ready_customer(ctx: ToolContext, caller: Caller) -> dict[str, Any]:
    c = get_customer(ctx, caller.customer_id)
    missing = [k for k in ("name", "address", "zip") if not c[k]]
    if missing:
        raise ToolError(
            "missing_info",
            f"Before booking, ask the customer for: {', '.join(missing)}, then call update_my_contact.",
        )
    if not ctx.conn.execute("SELECT 1 FROM service_area_zips WHERE zip = %s", (c["zip"],)).fetchone():
        raise ToolError(
            "out_of_area", f"We do not serve ZIP {c['zip']}. Apologise and do not book. Offer a follow-up."
        )
    return c


def _precheck_booking_owned(ctx: ToolContext, caller: Caller, a: Any) -> None:
    """Runs before an approval is created: a customer cannot even request things on someone else's booking."""
    b = load_booking(ctx, caller, a.booking_id)
    if b["status"] != "BOOKED":
        raise ToolError("booking_not_active", f"Booking #{a.booking_id} is {b['status']}, not active.")


def _precheck_booking_exists(ctx: ToolContext, caller: Caller, a: Any) -> None:
    """Ownership only (cancelling twice is harmless, so a cancelled booking is fine here)."""
    load_booking(ctx, caller, a.booking_id)


class BookArgs(ToolArgs):
    slot_id: str = Field(max_length=40, description="slot_id exactly as returned by find_slots")
    service_code: str = Field(max_length=40)
    idempotency_key: str = Field(
        min_length=8,
        max_length=80,
        description="A unique id for THIS booking decision. Reuse it only if you retry the same request.",
    )


@register(
    "book_appointment",
    BookArgs,
    "Book the visit once the customer has chosen one of the offered slots and you have their name, "
    "address and ZIP. Pass the slot_id exactly as returned by find_slots. Only tell the customer it "
    "is booked if this tool returns ok=true.",
)
def book_appointment(ctx: ToolContext, caller: Caller, a: BookArgs) -> dict[str, Any]:
    # Scoped by customer, so one customer cannot replay another customer's key.
    key = f"book:{caller.customer_id}:{a.idempotency_key}"
    if (cached := _idempotent_lookup(ctx, key)) is not None:
        return {**cached, "already_existed": True}

    service = get_service(ctx, a.service_code)
    _require_ready_customer(ctx, caller)
    try:
        tech, start = parse_slot_id(a.slot_id)
    except ValueError as exc:
        raise ToolError("unknown_slot", f"{exc}. Call find_slots and use a slot_id it returns.") from exc

    try:
        reason = check_bookable(ctx, tech, start, service["duration_min"])
    except AdapterUnavailable as exc:
        raise ToolError(
            "calendar_unavailable",
            "The calendar is not responding. Do not say it is booked. Tell the customer you will "
            "confirm shortly and call handoff_to_human.",
        ) from exc
    if reason:
        raise _slot_error(ctx, reason, service)

    end = start + timedelta(minutes=service["duration_min"])
    try:
        with ctx.conn.transaction():  # savepoint: a refused insert must not poison the outer transaction
            booking_id = ctx.conn.execute(
                "INSERT INTO bookings (customer_id, technician_id, service_code, start_at, end_at, price_usd)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (caller.customer_id, tech, a.service_code, start, end, service["price_usd"]),
            ).fetchone()[0]  # type: ignore[index]
    except psycopg.errors.ExclusionViolation as exc:
        # Two requests passed the check at the same moment; the database's own rule picked the winner.
        raise _slot_error(ctx, "taken", service) from exc

    try:
        event_id = ctx.calendar.create_event(
            booking_id, tech, start, end, f"Job #{booking_id}: {a.service_code}"
        )
    except AdapterUnavailable as exc:
        raise ToolError(  # raising rolls the whole booking back: no booking without a calendar entry
            "calendar_unavailable",
            "The calendar is not responding, so nothing was booked. Tell the customer you will confirm "
            "shortly and call handoff_to_human.",
        ) from exc
    ctx.conn.execute("UPDATE bookings SET calendar_event_id = %s WHERE id = %s", (event_id, booking_id))
    ctx.conn.execute("UPDATE customers SET lead_status = 'BOOKED' WHERE id = %s", (caller.customer_id,))
    ctx.conn.execute(  # a booked customer must not get "still need help?" nudges
        "UPDATE followups SET status = 'CANCELLED' WHERE customer_id = %s AND status = 'PENDING'",
        (caller.customer_id,),
    )
    followups.schedule_reminder(ctx, caller.customer_id, booking_id, start)  # type: ignore[arg-type]
    crm_sync.enqueue(ctx, caller.customer_id, "upsert_deal", {"booking_id": booking_id})  # type: ignore[arg-type]

    result = {"ok": True, **booking_summary(load_booking(ctx, caller, booking_id))}
    _idempotent_store(ctx, key, "book_appointment", result)
    return result


class RescheduleArgs(ToolArgs):
    booking_id: int
    new_slot_id: str = Field(max_length=40)
    idempotency_key: str = Field(min_length=8, max_length=80)


@register(
    "reschedule_appointment",
    RescheduleArgs,
    "Move an existing booking to a new slot from find_slots. Use booking_id from get_my_profile. Visits "
    "less than 24 hours away need owner approval.",
    precheck=_precheck_booking_owned,
)
def reschedule_appointment(ctx: ToolContext, caller: Caller, a: RescheduleArgs) -> dict[str, Any]:
    key = f"resched:{caller.customer_id}:{a.idempotency_key}"
    if (cached := _idempotent_lookup(ctx, key)) is not None:
        return {**cached, "already_existed": True}
    b = load_booking(ctx, caller, a.booking_id)
    service = get_service(ctx, b["service_code"])
    try:
        tech, start = parse_slot_id(a.new_slot_id)
    except ValueError as exc:
        raise ToolError("unknown_slot", f"{exc}. Call find_slots and use a slot_id it returns.") from exc
    try:
        reason = check_bookable(ctx, tech, start, service["duration_min"], exclude_booking_id=b["id"])
    except AdapterUnavailable as exc:
        raise ToolError(
            "calendar_unavailable", "The calendar is not responding. Call handoff_to_human."
        ) from exc
    if reason:
        raise _slot_error(ctx, reason, service)
    end = start + timedelta(minutes=service["duration_min"])
    try:
        with ctx.conn.transaction():
            ctx.conn.execute(
                "UPDATE bookings SET technician_id = %s, start_at = %s, end_at = %s WHERE id = %s",
                (tech, start, end, b["id"]),
            )
    except psycopg.errors.ExclusionViolation as exc:
        raise _slot_error(ctx, "taken", service) from exc
    try:
        if b["calendar_event_id"]:
            ctx.calendar.delete_event(b["calendar_event_id"])
        event_id = ctx.calendar.create_event(
            b["id"], tech, start, end, f"Job #{b['id']}: {b['service_code']}"
        )
    except AdapterUnavailable as exc:
        raise ToolError(
            "calendar_unavailable", "The calendar is not responding. Call handoff_to_human."
        ) from exc
    ctx.conn.execute("UPDATE bookings SET calendar_event_id = %s WHERE id = %s", (event_id, b["id"]))
    followups.schedule_reminder(ctx, b["customer_id"], b["id"], start)  # moves the reminder to the new time
    result = {"ok": True, **booking_summary(load_booking(ctx, caller, b["id"]))}
    _idempotent_store(ctx, key, "reschedule_appointment", result)
    return result


class CancelArgs(ToolArgs):
    booking_id: int
    reason: str = Field(min_length=2, max_length=200)


@register(
    "cancel_appointment",
    CancelArgs,
    "Cancel a booking (booking_id from get_my_profile). Visits less than 24 hours away need owner "
    "approval because a fee may apply.",
    precheck=_precheck_booking_exists,
)
def cancel_appointment(ctx: ToolContext, caller: Caller, a: CancelArgs) -> dict[str, Any]:
    b = load_booking(ctx, caller, a.booking_id)
    if b["status"] == "CANCELLED":
        return {"ok": True, **booking_summary(b), "already_cancelled": True}
    try:
        if b["calendar_event_id"]:
            ctx.calendar.delete_event(b["calendar_event_id"])
    except AdapterUnavailable as exc:
        raise ToolError(
            "calendar_unavailable", "The calendar is not responding. Call handoff_to_human."
        ) from exc
    ctx.conn.execute("UPDATE bookings SET status = 'CANCELLED' WHERE id = %s", (b["id"],))
    followups.cancel_reminder(ctx, b["customer_id"], b["id"])
    return {"ok": True, **booking_summary(load_booking(ctx, caller, b["id"]))}


class DiscountArgs(ToolArgs):
    booking_id: int
    pct: float = Field(gt=0, le=100, description="Percent off the quoted price")
    reason: str = Field(min_length=2, max_length=200)


@register(
    "request_discount",
    DiscountArgs,
    "Ask the owner to approve a discount on a booking. You can NOT grant one yourself. Discounts above "
    "15% are refused. Never promise a discount; tell the customer it is pending approval.",
    precheck=_precheck_booking_owned,
)
def request_discount(ctx: ToolContext, caller: Caller, a: DiscountArgs) -> dict[str, Any]:
    # This body only runs AFTER the owner approved (the gate turns every customer call into a request).
    ctx.conn.execute("UPDATE bookings SET discount_pct = %s WHERE id = %s", (a.pct, a.booking_id))
    return {"ok": True, **booking_summary(load_booking(ctx, caller, a.booking_id))}


class RefundArgs(ToolArgs):
    booking_id: int
    amount_usd: float = Field(gt=0, le=10000)
    reason: str = Field(min_length=2, max_length=200)


def _precheck_refund(ctx: ToolContext, caller: Caller, a: RefundArgs) -> None:
    _precheck_booking_owned(ctx, caller, a)
    b = load_booking(ctx, caller, a.booking_id)
    room = float(b["price_usd"]) * (1 - float(b["discount_pct"]) / 100) - float(b["refunded_usd"])
    if a.amount_usd > room + 0.001:
        raise ToolError(
            "refund_too_large", f"A refund can be at most ${room:.2f} for booking #{a.booking_id}."
        )


@register(
    "request_refund",
    RefundArgs,
    "Ask the owner to approve a refund for a booking. You can NOT issue one yourself. Tell the customer "
    "it is pending approval.",
    precheck=_precheck_refund,
)
def request_refund(ctx: ToolContext, caller: Caller, a: RefundArgs) -> dict[str, Any]:
    _precheck_refund(ctx, caller, a)  # re-check at approval time: the booking may have changed since
    ctx.conn.execute(
        "UPDATE bookings SET refunded_usd = refunded_usd + %s WHERE id = %s", (a.amount_usd, a.booking_id)
    )
    return {"ok": True, **booking_summary(load_booking(ctx, caller, a.booking_id))}
