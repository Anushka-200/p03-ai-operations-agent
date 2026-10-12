"""Output guard: code that checks the agent's reply BEFORE the customer sees it.

Analogy: a manager reading every outgoing letter for two mistakes only: a price that is not on the
price list, and "you're booked!" when nothing was booked. The model is not trusted to avoid them.
"""

import re

AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
# "you're booked", "is confirmed", "I've scheduled", "booking confirmed", etc.
BOOKED_CLAIM = re.compile(
    r"\b(you(?:'re| are)|is|are|has been|have been|all)\s+(?:all\s+)?(?:set|booked|confirmed|scheduled)\b"
    r"|\b(?:booking|appointment)\s+(?:is\s+)?confirmed\b"
    r"|\bI(?:'ve| have)\s+(?:booked|scheduled|confirmed)\b",
    re.IGNORECASE,
)

# "...once the visit is booked", "if it is confirmed": talking about a future or possible booking is not a claim.
CONDITIONAL = re.compile(
    r"\b(if|once|when|whenever|after|until|unless|before|whether|as soon as|should)\b", re.IGNORECASE
)


def claims_booking(text: str) -> bool:
    """True when the reply states that something IS booked/confirmed (not "once it is booked")."""
    for m in BOOKED_CLAIM.finditer(text):
        sentence_start = max(text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
        if not CONDITIONAL.search(text[sentence_start : m.start()]):
            return True
    return False


SAFE_FALLBACK = (
    "Sorry, I want to make sure I give you accurate information. I've asked a team member to follow up "
    "with you shortly."
)


def amounts_in(text: str) -> set[float]:
    return {round(float(m.replace(",", "")), 2) for m in AMOUNT.findall(text)}


def check_reply(text: str, allowed_amounts: set[float], booked_ids: list[int]) -> str | None:
    """Return a short description of what is wrong with `text`, or None when it is fine."""
    allowed = {round(a, 2) for a in allowed_amounts}
    bad = sorted(a for a in amounts_in(text) if a not in allowed)
    if bad:
        shown = ", ".join(f"${a:g}" for a in bad)
        return f"it quotes {shown}, which is not on the price list or in a tool result. Only quote prices that tools returned."
    if claims_booking(text) and not booked_ids:
        return "it says an appointment is booked/confirmed, but no booking exists yet. Say what is still needed instead."
    return None
