import json
import logging

import pytest

from app.log import JsonFormatter
from app.privacy import phone_hash, redact_phones
from app.settings import get_settings


def test_same_number_in_different_formats_hashes_the_same():
    assert phone_hash("+1 (512) 555-0123") == phone_hash("+15125550123")


def test_hash_depends_on_the_key(monkeypatch):
    before = phone_hash("+15125550123")
    monkeypatch.setenv("PHONE_HASH_KEY", "another-key")
    get_settings.cache_clear()
    assert phone_hash("+15125550123") != before


def test_empty_key_is_refused(monkeypatch):
    monkeypatch.setenv("PHONE_HASH_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        phone_hash("+15125550123")


def test_redact_leaves_short_numbers_alone():
    out = redact_phones("Call +1 512-555-0123 about the $89 visit at 10:00")
    assert "555" not in out and "$89" in out and "10:00" in out


def test_json_log_line_redacts_message_and_extras():
    record = logging.makeLogRecord(
        {
            "msg": "inbound from +15125550123",
            "levelname": "INFO",
            "name": "t",
            "customer_phone": "+15125550123",
        }
    )
    line = JsonFormatter().format(record)
    data = json.loads(line)
    assert "5550123" not in line
    assert data["msg"].startswith("inbound from ph_")
    assert data["customer_phone"].startswith("ph_")


def test_dates_times_and_prices_are_not_phone_numbers():
    # Regression: the first live run redacted the ISO timestamp 2026-10-09.
    text = "2026-10-09T14:37:53 booked 2026-10-10 10:00, $89, job 1234567890"
    assert redact_phones(text) == text


def test_common_phone_formats_are_redacted():
    for raw in [
        "+15125550123",
        "+1 512 555 0123",
        "(512) 555-0123",
        "512-555-0123",
        "512.555.0123",
    ]:
        assert redact_phones(f"call {raw} now").startswith("call ph_"), raw


def test_timestamp_survives_in_log_line():
    record = logging.makeLogRecord({"msg": "hi", "levelname": "INFO", "name": "t"})
    data = json.loads(JsonFormatter().format(record))
    assert data["ts"][:2] == "20" and "ph_" not in data["ts"]
