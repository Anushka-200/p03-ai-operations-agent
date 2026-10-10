"""Phone numbers are personal data: logs and thread ids only ever see a keyed hash of them."""

import hashlib
import hmac
import re

from app.settings import get_settings

# Real phone shapes only: +country code numbers (+15125550123, +1 512 555 0123) and US 3-3-4
# formats ((512) 555-0123, 512-555-0123). A looser "any 10 digits" pattern also ate dates
# like 2026-10-09 in the first live run.
PHONE_RE = re.compile(
    r"\+\d{1,3}[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"
    r"|\(\d{3}\)\s?\d{3}[\s.-]\d{4}\b"
    r"|\b\d{3}[.-]\d{3}[.-]\d{4}\b"
)


def normalize_phone(raw: str) -> str:
    """Keep digits only, with a leading + (so '+1 (512) 555-0123' and '+15125550123' match)."""
    digits = re.sub(r"\D", "", raw)
    return "+" + digits


def phone_hash(raw: str) -> str:
    key = get_settings().phone_hash_key.get_secret_value()
    if not key:
        # Fail loudly: an empty key would make the hash brute-forceable again.
        raise RuntimeError("PHONE_HASH_KEY is not set")
    digest = hmac.new(key.encode(), normalize_phone(raw).encode(), hashlib.sha256)
    return "ph_" + digest.hexdigest()[:16]


def redact_phones(text: str) -> str:
    return PHONE_RE.sub(lambda m: phone_hash(m.group()), text)


def redact_any(data):
    """Redact phone numbers inside any nested str/list/dict (used to scrub traces before upload)."""
    if isinstance(data, str):
        return redact_phones(data)
    if isinstance(data, list):
        return [redact_any(x) for x in data]
    if isinstance(data, dict):
        return {k: redact_any(v) for k, v in data.items()}
    return data
