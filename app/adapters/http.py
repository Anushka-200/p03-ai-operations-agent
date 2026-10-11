"""One way to make an HTTP call to an outside service, shared by the Google, HubSpot and Twilio adapters.

Turns "the network or the service is having a bad moment" (timeouts, 429, 5xx) into AdapterUnavailable,
which the tools and the CRM outbox already know how to handle. Anything else the service refuses (a wrong
token, a bad request) is OUR bug and raises AdapterRejected: retrying would fail the same way.
Messages carry the service name and status only, never the URL, headers or body (they hold secrets/PII).
"""

from typing import Any

import httpx

from app.adapters.ports import AdapterUnavailable


class AdapterRejected(RuntimeError):
    """The service understood us and said no (4xx). Not retryable."""


def call(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    name: str,
    ok: tuple[int, ...] = (200, 201, 204),
    tolerate: tuple[int, ...] = (),
    **kwargs: Any,
) -> httpx.Response:
    """`tolerate` = statuses the caller wants to inspect itself (e.g. 404 on delete, 409 on re-create)."""
    try:
        resp = client.request(method, url, timeout=kwargs.pop("timeout", 10.0), **kwargs)
    except httpx.HTTPError as exc:
        raise AdapterUnavailable(f"{name}: {type(exc).__name__}") from None  # exc text can hold the URL
    if resp.status_code in ok or resp.status_code in tolerate:
        return resp
    if resp.status_code == 429 or resp.status_code >= 500:
        raise AdapterUnavailable(f"{name}: HTTP {resp.status_code}")
    raise AdapterRejected(f"{name}: HTTP {resp.status_code}")
