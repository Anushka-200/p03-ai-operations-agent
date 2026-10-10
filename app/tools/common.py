"""Helpers shared by the tool modules."""

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.availability import local_label
from app.tools.types import Caller, ToolContext, ToolError


class ToolArgs(BaseModel):
    """Base for every tool's arguments. extra='forbid' means a model (or attacker) that adds
    `customer_id=2` gets a clear error instead of silently reading someone else's data."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(value: str) -> str:
    """Customer-typed text is data, never instructions. Drop control characters and collapse newlines
    (so a saved name can't smuggle a fake 'SYSTEM:' line into a later prompt)."""
    return " ".join(_CONTROL_CHARS.sub("", value).split())


def get_service(ctx: ToolContext, code: str) -> dict[str, Any]:
    row = ctx.conn.execute(
        "SELECT code, name, price_usd, duration_min, includes FROM services WHERE code = %s AND active",
        (code,),
    ).fetchone()
    if row is None:
        codes = [r[0] for r in ctx.conn.execute("SELECT code FROM services WHERE active ORDER BY code")]
        raise ToolError(
            "unknown_service",
            f"There is no service '{code}'. Valid service codes: {', '.join(codes)}. "
            "Call list_services to see what each one is.",
        )
    return {
        "code": row[0],
        "name": row[1],
        "price_usd": float(row[2]),
        "duration_min": row[3],
        "includes": row[4],
    }


def get_customer(ctx: ToolContext, customer_id: int | None) -> dict[str, Any]:
    row = ctx.conn.execute(
        "SELECT id, name, email, address, zip, lead_status FROM customers WHERE id = %s", (customer_id,)
    ).fetchone()
    if row is None:
        raise ToolError("no_customer", "No customer record for this conversation. Call handoff_to_human.")
    return dict(zip(("id", "name", "email", "address", "zip", "lead_status"), row, strict=True))


def load_booking(ctx: ToolContext, caller: Caller, booking_id: int) -> dict[str, Any]:
    """Load a booking. A customer can only ever see their OWN bookings: other ids look 'not found'."""
    sql = (
        "SELECT id, customer_id, technician_id, service_code, start_at, end_at, status, price_usd,"
        " discount_pct, refunded_usd, calendar_event_id FROM bookings WHERE id = %s"
    )
    params: tuple = (booking_id,)
    if caller.role == "customer":
        sql += " AND customer_id = %s"
        params = (booking_id, caller.customer_id)
    row = ctx.conn.execute(sql, params).fetchone()
    if row is None:
        raise ToolError(
            "booking_not_found",
            f"No booking #{booking_id} for this customer. Call get_my_profile to see their bookings.",
        )
    cols = ("id", "customer_id", "technician_id", "service_code", "start_at", "end_at", "status",
            "price_usd", "discount_pct", "refunded_usd", "calendar_event_id")  # fmt: skip
    return dict(zip(cols, row, strict=True))


def booking_summary(b: dict[str, Any]) -> dict[str, Any]:
    price = float(b["price_usd"])
    discount = float(b["discount_pct"])
    return {
        "booking_id": b["id"],
        "service": b["service_code"],
        "when": local_label(b["start_at"]),
        "status": b["status"],
        "price_usd": price,
        "discount_pct": discount,
        "total_usd": round(price * (1 - discount / 100), 2),
    }


def parse_date_range(d_from: date, d_to: date | None, today: datetime) -> tuple[date, date]:
    from datetime import timedelta

    from app.availability import BUSINESS_TZ, MAX_RANGE_DAYS

    local_today = today.astimezone(BUSINESS_TZ).date()
    if d_from < local_today:
        raise ToolError("date_in_past", f"{d_from} is in the past. Today is {local_today}.")
    d_to = d_to or d_from + timedelta(days=6)
    if d_to < d_from or (d_to - d_from).days > MAX_RANGE_DAYS:
        raise ToolError("bad_date_range", f"Use a range of at most {MAX_RANGE_DAYS} days, end after start.")
    return d_from, d_to
