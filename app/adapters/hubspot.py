"""HubSpot adapter (free developer account, private-app token). Contact + deal upserts only.

"Upsert" = update if it exists, else create, so calling twice never makes a duplicate. Reads (search) are
always allowed; writes only when dry_run is False. Failures raise AdapterUnavailable and the crm_outbox
retries later with backoff: a booking never fails because the CRM is down.
"""

import logging
from collections.abc import Callable
from typing import Any

import httpx

from app.adapters.http import AdapterRejected, call
from app.adapters.resilience import CircuitBreaker, retry

logger = logging.getLogger(__name__)
API = "https://api.hubapi.com"
DEAL_TO_CONTACT = {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}


class HubSpotCrm:
    def __init__(
        self,
        token: str,
        dry_run: bool = True,
        client: httpx.Client | None = None,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._token, self.dry_run = token, dry_run
        self.client = client or httpx.Client()
        self.breaker = breaker or CircuitBreaker("hubspot")
        self._retry_kw: dict[str, Any] = {"sleep": sleep} if sleep else {}

    def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        return retry(
            lambda: self.breaker.call(lambda: call(self.client, method, f"{API}{path}", name="hubspot", headers=headers, ok=(200, 201), **kw)),
            **self._retry_kw,
        )  # fmt: skip

    def _search(self, obj: str, prop: str, value: str) -> str | None:
        body = {
            "filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}],
            "limit": 1,
        }
        results = self._req("POST", f"/crm/v3/objects/{obj}/search", json=body).json().get("results", [])
        return str(results[0]["id"]) if results else None

    def _patch_known(self, kind: str, object_id: str | None, props: dict[str, Any]) -> str | None:
        """Update by the id we stored. None = no usable id (or HubSpot no longer has it): caller falls back to search."""
        if not object_id:
            return None
        try:
            self._req("PATCH", f"/crm/v3/objects/{kind}/{object_id}", json={"properties": props})
        except AdapterRejected:  # e.g. 404: the record was deleted in HubSpot
            return None
        return object_id

    def upsert_contact(self, customer: dict[str, Any]) -> str:
        if self.dry_run:
            logger.info("crm dry-run: would upsert contact", extra={"customer_id": customer["id"]})
            return f"dryrun-contact-{customer['id']}"
        first, _, last = (customer.get("name") or "").partition(" ")
        props = {"firstname": first, "lastname": last, "email": customer.get("email"),
                 "phone": customer.get("phone"), "address": customer.get("address"), "zip": customer.get("zip")}  # fmt: skip
        props = {k: v for k, v in props.items() if v}
        existing = customer.get("crm_contact_id")
        if existing and existing.startswith("dryrun-"):
            existing = None  # a placeholder from an earlier dry-run is not a real HubSpot id
        # The id we stored is the reliable key. Search is only the fallback: HubSpot's search index lags a few
        # seconds behind writes, so searching right after creating can miss the record and create a duplicate.
        done = self._patch_known("contacts", existing, props)
        if done:
            return done
        found = self._search("contacts", "phone", customer["phone"]) if customer.get("phone") else None
        if found:
            self._req("PATCH", f"/crm/v3/objects/contacts/{found}", json={"properties": props})
            return found
        return str(self._req("POST", "/crm/v3/objects/contacts", json={"properties": props}).json()["id"])

    def upsert_deal(self, crm_contact_id: str, booking: dict[str, Any]) -> str:
        if self.dry_run:
            logger.info("crm dry-run: would upsert deal", extra={"booking_id": booking["id"]})
            return f"dryrun-deal-{booking['id']}"
        name = f"P03 booking #{booking['id']}"  # the natural key: same booking twice = same deal
        price = float(booking["price"] or 0) * (1 - float(booking.get("discount") or 0) / 100)
        stage = "closedlost" if booking.get("status") == "CANCELLED" else "appointmentscheduled"
        props = {"dealname": name, "amount": f"{price:.2f}", "dealstage": stage, "pipeline": "default"}
        known = booking.get("crm_deal_id")
        done = self._patch_known("deals", None if (known or "").startswith("dryrun-") else known, props)
        if done:
            return done
        existing = self._search("deals", "dealname", name)  # fallback only: the search index lags writes
        if existing:
            self._req("PATCH", f"/crm/v3/objects/deals/{existing}", json={"properties": props})
            return existing
        body = {
            "properties": props,
            "associations": [{"to": {"id": crm_contact_id}, "types": [DEAL_TO_CONTACT]}],
        }
        return str(self._req("POST", "/crm/v3/objects/deals", json=body).json()["id"])
