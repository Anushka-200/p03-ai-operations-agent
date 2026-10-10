"""Shared types for the tool layer: who is calling, what a tool needs to run, how a tool fails."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg
from pydantic import BaseModel

from app.adapters.ports import CalendarPort, CrmPort, MessagingPort
from app.settings import Settings

Role = Literal["customer", "owner", "system"]


@dataclass(frozen=True)
class Caller:
    """Who is asking. For customers, customer_id comes from the verified phone number, set by our
    code when the message arrives. It is never read from anything the model or customer typed."""

    role: Role
    customer_id: int | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ToolContext:
    conn: psycopg.Connection
    calendar: CalendarPort
    crm: CrmPort
    messaging: MessagingPort
    settings: Settings
    now: Callable[[], datetime] = field(default=_utcnow)  # injectable so tests control "today"
    trace_id: str | None = None


class ToolError(Exception):
    """A failure the model can recover from. `message` says what happened AND what to do next."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ToolSpec:
    name: str
    description: str  # written like instructions to a new employee: the model reads this
    args_model: type[BaseModel]
    fn: Callable[[ToolContext, Caller, Any], dict[str, Any]]
    # Optional scope check run BEFORE an approval is created (e.g. "is this booking yours?").
    precheck: Callable[[ToolContext, Caller, Any], None] | None = None


def error_result(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message, **extra}
