"""When can a technician be booked? Pure rules + one busy-time lookup.

Business hours are constants here for now (a real client's hours would come from config).
Everything is stored in UTC; customers see local time (America/Chicago).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # only for type hints: a real import here made `import app.availability` circular
    from app.tools.types import ToolContext

BUSINESS_TZ = ZoneInfo("America/Chicago")
OPEN_HOUR, CLOSE_HOUR = 8, 18  # visits must start at/after 8:00 and end by 18:00 local time
OPEN_WEEKDAYS = {0, 1, 2, 3, 4, 5}  # Monday..Saturday
GRID_MINUTES = 30  # visits start on the hour or half hour
MIN_LEAD = timedelta(hours=2)  # no booking for "in 10 minutes"
MAX_RANGE_DAYS = 14


@dataclass(frozen=True)
class Slot:
    slot_id: str
    technician_id: int
    start: datetime  # UTC
    end: datetime  # UTC

    @property
    def label(self) -> str:
        return local_label(self.start)


def local_label(dt: datetime) -> str:
    local = dt.astimezone(BUSINESS_TZ)
    return f"{local:%a %b} {local.day}, {local:%I:%M %p}".replace(" 0", " ")


def make_slot_id(technician_id: int, start: datetime) -> str:
    return f"{technician_id}-{int(start.timestamp())}"


def parse_slot_id(slot_id: str) -> tuple[int, datetime]:
    """Inverse of make_slot_id. Raises ValueError for anything the model made up."""
    try:
        tech, ts = slot_id.split("-")
        return int(tech), datetime.fromtimestamp(int(ts), tz=BUSINESS_TZ).astimezone()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"'{slot_id}' is not a slot_id from find_slots") from exc


def _busy(
    ctx: ToolContext, tech: int, start: datetime, end: datetime, exclude_booking_id: int | None = None
) -> list[tuple[datetime, datetime]]:
    """Our own bookings come from Postgres (the truth); other blocks come from the calendar adapter."""
    rows = ctx.conn.execute(
        "SELECT start_at, end_at FROM bookings WHERE technician_id = %s AND status = 'BOOKED'"
        " AND start_at < %s AND end_at > %s AND id IS DISTINCT FROM %s",
        (tech, end, start, exclude_booking_id),
    ).fetchall()
    return [(r[0], r[1]) for r in rows] + ctx.calendar.busy_intervals(tech, start, end)


def _overlaps(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    return any(b_start < end and b_end > start for b_start, b_end in busy)


def _within_hours(start: datetime, duration_min: int) -> bool:
    local = start.astimezone(BUSINESS_TZ)
    end_local = local + timedelta(minutes=duration_min)
    return (
        local.weekday() in OPEN_WEEKDAYS
        and local.minute % GRID_MINUTES == 0
        and local.second == 0
        and local.time() >= time(OPEN_HOUR)
        and end_local.date() == local.date()
        and end_local.time() <= time(CLOSE_HOUR)
    )


def check_bookable(
    ctx: ToolContext, tech: int, start: datetime, duration_min: int, exclude_booking_id: int | None = None
) -> str | None:
    """None if the slot can be booked right now, otherwise a short reason code."""
    active = ctx.conn.execute("SELECT 1 FROM technicians WHERE id = %s AND active", (tech,)).fetchone()
    if not active:
        return "unknown_slot"
    if start < ctx.now() + MIN_LEAD:
        return "too_soon"
    if not _within_hours(start, duration_min):
        return "outside_hours"
    end = start + timedelta(minutes=duration_min)
    if _overlaps(_busy(ctx, tech, start, end, exclude_booking_id), start, end):
        return "taken"
    return None


def available_slots(
    ctx: ToolContext, duration_min: int, d_from: date, d_to: date, part_of_day: str = "any"
) -> list[Slot]:
    """Every free start time in the date range, across all active technicians (earliest first)."""
    techs = [r[0] for r in ctx.conn.execute("SELECT id FROM technicians WHERE active ORDER BY id").fetchall()]
    day_start = datetime.combine(d_from, time(0), tzinfo=BUSINESS_TZ)
    day_end = datetime.combine(d_to + timedelta(days=1), time(0), tzinfo=BUSINESS_TZ)
    slots: list[Slot] = []
    for tech in techs:
        busy = _busy(ctx, tech, day_start, day_end)
        d = d_from
        while d <= d_to:
            local = datetime.combine(d, time(OPEN_HOUR), tzinfo=BUSINESS_TZ)
            while local.time() < time(CLOSE_HOUR):
                start = local.astimezone(ctx.now().tzinfo)
                end = start + timedelta(minutes=duration_min)
                hour = local.hour
                wanted = (
                    part_of_day == "any"
                    or (part_of_day == "morning" and hour < 12)
                    or (part_of_day == "afternoon" and hour >= 12)
                )
                if (
                    wanted
                    and start >= ctx.now() + MIN_LEAD
                    and _within_hours(start, duration_min)
                    and not _overlaps(busy, start, end)
                ):
                    slots.append(Slot(make_slot_id(tech, start), tech, start, end))
                local += timedelta(minutes=GRID_MINUTES)
            d += timedelta(days=1)
    slots.sort(key=lambda s: (s.start, s.technician_id))
    unique: dict[datetime, Slot] = {}
    for s in slots:  # two technicians free at the same time look identical to the customer: keep one
        unique.setdefault(s.start, s)
    return list(unique.values())


def choose_offers(slots: list[Slot], n: int = 3, spacing: timedelta = timedelta(hours=2)) -> list[Slot]:
    """Offer a few well-spread options: the earliest slot of different days first, then fill by spacing."""
    chosen: list[Slot] = []
    seen_days: set[date] = set()
    for s in slots:
        day = s.start.astimezone(BUSINESS_TZ).date()
        if day not in seen_days and len(chosen) < n:
            chosen.append(s)
            seen_days.add(day)
    for s in slots:
        if len(chosen) >= n:
            break
        if s not in chosen and all(abs(s.start - c.start) >= spacing for c in chosen):
            chosen.append(s)
    return sorted(chosen, key=lambda s: s.start)
