"""Tools a customer-facing agent may use that do not move money or create bookings."""

import re
from datetime import date
from typing import Any, Literal

from pydantic import Field

from app import crm_sync
from app.adapters.ports import AdapterUnavailable
from app.availability import BUSINESS_TZ, available_slots, choose_offers
from app.tools.common import (
    ToolArgs,
    booking_summary,
    clean_text,
    get_customer,
    get_service,
    parse_date_range,
)
from app.tools.registry import register
from app.tools.types import Caller, ToolContext, ToolError


class NoArgs(ToolArgs):
    pass


@register(
    "list_services",
    NoArgs,
    "List every service we offer with its code, name and price. Call this first if you are unsure "
    "which service code to use. Prices here are the only prices you may quote.",
)
def list_services(ctx: ToolContext, caller: Caller, a: NoArgs) -> dict[str, Any]:
    rows = ctx.conn.execute(
        "SELECT code, name, price_usd, duration_min FROM services WHERE active ORDER BY price_usd, code"
    ).fetchall()
    return {
        "ok": True,
        "services": [
            {"code": r[0], "name": r[1], "price_usd": float(r[2]), "duration_min": r[3]} for r in rows
        ],
    }


class PriceArgs(ToolArgs):
    service_code: str = Field(max_length=40, description="Service code from list_services")


@register(
    "get_price",
    PriceArgs,
    "Get the exact price and what is included for ONE service. This is the only source of prices: "
    "never state a price you did not get from this tool or list_services.",
)
def get_price(ctx: ToolContext, caller: Caller, a: PriceArgs) -> dict[str, Any]:
    s = get_service(ctx, a.service_code)
    return {"ok": True, **s}


class ZipArgs(ToolArgs):
    zip: str = Field(pattern=r"^\d{5}$", description="5-digit US ZIP code")


@register(
    "check_service_area",
    ZipArgs,
    "Check whether we serve a ZIP code. Ask for the ZIP before quoting or booking.",
)
def check_service_area(ctx: ToolContext, caller: Caller, a: ZipArgs) -> dict[str, Any]:
    row = ctx.conn.execute("SELECT note FROM service_area_zips WHERE zip = %s", (a.zip,)).fetchone()
    if row:
        return {"ok": True, "in_area": True, "area": row[0]}
    return {
        "ok": True,
        "in_area": False,
        "message": f"We do not serve {a.zip}. Apologise, do not book, and offer the owner will follow up "
        "if they think this is a mistake (call handoff_to_human only if they ask).",
    }


class FaqArgs(ToolArgs):
    question: str = Field(min_length=2, max_length=300)


_WORD = re.compile(r"[a-z0-9]+")


@register(
    "search_faq",
    FaqArgs,
    "Look up company policy answers (hours, payment, cancellation, warranty...). Answer ONLY from the "
    "returned entries; if nothing matches, say you will ask the team (handoff_to_human).",
)
def search_faq(ctx: ToolContext, caller: Caller, a: FaqArgs) -> dict[str, Any]:
    words = set(_WORD.findall(a.question.lower())) - {
        "the",
        "a",
        "is",
        "are",
        "do",
        "you",
        "your",
        "i",
        "my",
        "to",
        "of",
        "what",
        "how",
        "can",
    }
    scored = []
    for q, ans, kw in ctx.conn.execute("SELECT question, answer, keywords FROM faq").fetchall():
        haystack = set(_WORD.findall((q + " " + kw).lower()))
        score = len(words & haystack)
        if score:
            scored.append((score, q, ans))
    scored.sort(reverse=True)
    return {"ok": True, "matches": [{"question": q, "answer": ans} for _, q, ans in scored[:3]]}


@register(
    "get_my_profile",
    NoArgs,
    "Get this customer's saved details and their bookings. Use it to see which fields are still "
    "missing (name, address, ZIP) and which booking_id to use for changes.",
)
def get_my_profile(ctx: ToolContext, caller: Caller, a: NoArgs) -> dict[str, Any]:
    from app.tools.common import load_booking

    c = get_customer(ctx, caller.customer_id)
    ids = [
        r[0]
        for r in ctx.conn.execute(
            "SELECT id FROM bookings WHERE customer_id = %s ORDER BY start_at DESC LIMIT 10",
            (caller.customer_id,),
        )
    ]
    return {
        "ok": True,
        "name": c["name"],
        "email": c["email"],
        "address": c["address"],
        "zip": c["zip"],
        "missing": [k for k in ("name", "address", "zip") if not c[k]],
        "bookings": [booking_summary(load_booking(ctx, caller, i)) for i in ids],
    }


class ContactArgs(ToolArgs):
    name: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    address: str | None = Field(default=None, max_length=160)
    zip: str | None = Field(default=None, pattern=r"^\d{5}$")


@register(
    "update_my_contact",
    ContactArgs,
    "Save details the customer told you (name, email, street address, ZIP). Only pass the fields they "
    "actually gave. Never invent or guess values.",
)
def update_my_contact(ctx: ToolContext, caller: Caller, a: ContactArgs) -> dict[str, Any]:
    changes = {k: clean_text(v) for k, v in a.model_dump(exclude_none=True).items()}
    if not changes:
        raise ToolError("nothing_to_update", "Pass at least one of: name, email, address, zip.")
    sets = ", ".join(f"{k} = %s" for k in changes)  # keys come from our own model, never from input
    ctx.conn.execute(f"UPDATE customers SET {sets} WHERE id = %s", (*changes.values(), caller.customer_id))
    ctx.conn.execute(
        "UPDATE customers SET lead_status = 'QUALIFYING' WHERE id = %s AND lead_status = 'NEW'",
        (caller.customer_id,),
    )
    crm_sync.enqueue(ctx, caller.customer_id, "upsert_contact")  # type: ignore[arg-type]
    result: dict[str, Any] = {"ok": True, "saved": sorted(changes)}
    if "zip" in changes:  # saves the model a check_service_area call
        result["service_area"] = check_service_area(ctx, caller, ZipArgs(zip=changes["zip"]))
    return result


class SlotArgs(ToolArgs):
    service_code: str = Field(max_length=40)
    date_from: date = Field(description="First day to look at, YYYY-MM-DD (customer local time)")
    date_to: date | None = Field(default=None, description="Last day; default 6 days after date_from")
    part_of_day: Literal["any", "morning", "afternoon"] = "any"


@register(
    "find_slots",
    SlotArgs,
    "Find up to 3 free appointment times for a service. Offer them to the customer exactly as returned; "
    "each has a slot_id you must pass to book_appointment. Times are in the customer's local time.",
)
def find_slots(ctx: ToolContext, caller: Caller, a: SlotArgs) -> dict[str, Any]:
    service = get_service(ctx, a.service_code)
    d_from, d_to = parse_date_range(a.date_from, a.date_to, ctx.now())
    try:
        slots = available_slots(ctx, service["duration_min"], d_from, d_to, a.part_of_day)
    except AdapterUnavailable as exc:
        raise ToolError(
            "calendar_unavailable",
            "The calendar is not responding. Tell the customer you will confirm times shortly and "
            "call handoff_to_human.",
        ) from exc
    offers = choose_offers(slots)
    if not offers:
        return {
            "ok": True,
            "slots": [],
            "message": "No openings in that range. Try a later date range or another part of the day.",
        }
    return {
        "ok": True,
        "timezone": str(BUSINESS_TZ),
        "slots": [{"slot_id": s.slot_id, "when": s.label} for s in offers],
    }


class HandoffArgs(ToolArgs):
    reason: str = Field(min_length=3, max_length=200, description="Why a human is needed, in one sentence")


@register(
    "handoff_to_human",
    HandoffArgs,
    "Hand the conversation to a person at the company. Use it when the customer asks for a human, is "
    "upset, wants something you cannot do, or a tool tells you to. Tell the customer someone will follow up.",
)
def handoff_to_human(ctx: ToolContext, caller: Caller, a: HandoffArgs) -> dict[str, Any]:
    ctx.conn.execute("UPDATE customers SET lead_status = 'HANDOFF' WHERE id = %s", (caller.customer_id,))
    ctx.messaging.send(
        customer_id=None,
        channel="telegram",
        recipient="owner",  # without this the Telegram bot's alert query would never pick the row up
        body=f"Customer #{caller.customer_id} needs a person: {clean_text(a.reason)}",
        idempotency_key=f"handoff:{caller.customer_id}:{ctx.now():%Y%m%d%H}",
    )
    return {
        "ok": True,
        "message": "The team has been notified. If the customer is upset, begin your reply with a short, sincere "
        'apology ("I\'m sorry about that"). Then tell the customer someone will follow up.',
    }
