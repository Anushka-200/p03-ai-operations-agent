"""Emergency router: the deterministic phrasing set, the no-LLM guarantee, and the escalation path."""

from pathlib import Path

import pytest
import yaml

from app import emergency
from app.llm import LLMError
from tests.helpers import make_customer

PHRASES = yaml.safe_load((Path(__file__).parent.parent / "evals" / "emergency_phrases.yaml").read_text())["phrases"]


def test_recall_is_100_percent_and_no_false_positives_on_ordinary_requests():
    missed = [p["text"] for p in PHRASES if p["expect"] and emergency.detect(p["text"]) is None]
    false_alarms = [p["text"] for p in PHRASES if not p["expect"] and emergency.detect(p["text"]) is not None]
    positives = [p for p in PHRASES if p["expect"]]
    overtrigger = [p for p in positives if p.get("overtrigger")]
    print(f"\nphrases={len(PHRASES)} recall={(len(positives)-len(missed))}/{len(positives)} "
          f"false_alarms={len(false_alarms)} overtrigger(accepted)={len(overtrigger)}")
    assert missed == []
    assert false_alarms == []


def test_detect_makes_no_llm_call(monkeypatch):
    import app.llm

    def boom(*a, **k):
        raise AssertionError("the emergency rules must never call an LLM")

    monkeypatch.setattr(app.llm, "complete", boom)
    assert emergency.detect("I smell gas").kind == "gas"


def test_cold_needs_both_no_heat_and_a_low_temperature():
    assert emergency.detect("no heat, it's 30 degrees inside").rule == "no_heat_cold"
    assert emergency.detect("no heat but it's 70 degrees") is None
    assert emergency.detect("it's 30 degrees outside, book me a tune-up") is None


def test_classifier_failure_means_continue_normally():
    def down(*a, **k):
        raise LLMError("provider down")

    assert emergency.classifier_says_emergency("my basement is turning into a pool", complete_fn=down) is None


def test_classifier_can_add_coverage():
    class R:
        text = "EMERGENCY"

    m = emergency.classifier_says_emergency("my basement is turning into a pool", complete_fn=lambda *a, **k: R())
    assert m and m.detected_by == "classifier"


def _inbound(ctx, customer_id, sid, body):
    return ctx.conn.execute(
        "INSERT INTO inbound_messages (message_sid, customer_id, channel, body) VALUES (%s,%s,'sms',%s) RETURNING id",
        (sid, customer_id, body),
    ).fetchone()[0]


def test_escalation_writes_everything_and_is_idempotent(ctx):
    cust = make_customer(ctx, 901)
    inbound = _inbound(ctx, cust.customer_id, "SM-emerg-1", "I smell gas")
    match = emergency.detect("I smell gas")

    text = emergency.escalate(ctx, cust.customer_id, inbound, match)
    assert "Leave the building" in text
    # a duplicate delivery must not page twice or send a second text
    emergency.escalate(ctx, cust.customer_id, inbound, match)

    msgs = ctx.conn.execute(
        "SELECT customer_id, recipient, status FROM outbound_messages WHERE idempotency_key LIKE %s ORDER BY id",
        (f"emerg:{inbound}:%",),
    ).fetchall()
    assert len(msgs) == 3  # customer, on-call tech, owner: once each
    assert {m[1] for m in msgs} >= {"owner"}
    assert all(m[2] == "DRY_RUN" for m in msgs)
    assert ctx.conn.execute("SELECT lead_status FROM customers WHERE id=%s", (cust.customer_id,)).fetchone()[0] == "EMERGENCY"
    audit_rows = ctx.conn.execute(
        "SELECT decision FROM audit_log WHERE tool='emergency_escalation' AND customer_id=%s", (cust.customer_id,)
    ).fetchall()
    assert audit_rows


@pytest.mark.parametrize("kind", ["gas", "co", "water", "fire", "cold"])
def test_every_kind_has_a_template(kind):
    assert kind in emergency._config()["templates"]["customer"]
