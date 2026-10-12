"""The output guard: real claims are blocked, talk about a future or possible booking is not."""

import pytest

from app.agent.guard import check_reply


@pytest.mark.parametrize(
    "text",
    [
        "You're booked for Tuesday at 10am.",
        "Great, your appointment is confirmed.",
        "I've scheduled the technician for Monday.",
        "All set! See you then.",
        "Thanks for waiting. You are all set for 8:00 AM.",
        "Once you reply, I will check. Your visit is confirmed for Monday.",  # a later sentence is still a claim
    ],
)
def test_a_booking_claim_without_a_booking_is_blocked(text):
    assert check_reply(text, set(), []) is not None


@pytest.mark.parametrize(
    "text",
    [
        "I can send your 10% request to the owner once the visit is booked.",
        "If the slot is confirmed I will text you.",
        "You will get a reminder after your appointment is confirmed.",
        "Which time works for you?",
    ],
)
def test_talking_about_a_possible_booking_is_allowed(text):
    assert check_reply(text, set(), []) is None


def test_a_real_claim_passes_when_a_booking_exists():
    assert check_reply("You're booked for Tuesday at 10am.", set(), [42]) is None


def test_an_unknown_price_is_still_blocked_and_a_tool_price_is_allowed():
    assert check_reply("The visit costs $120.", {89.0}, []) is not None
    assert check_reply("The visit costs $89.", {89.0}, []) is None
