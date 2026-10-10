"""Cost guard for scripts that call paid models: project first, confirm, then stop on overrun.

Two protections (OWASP "unbounded consumption" at script level):
1. BEFORE: estimate the bill; above a threshold the script must be confirmed (--yes or a typed y).
2. DURING: add up the real cost of every call; stop if it passes a hard limit.
"""

import litellm

from app.settings import get_settings


class SpendDeclined(RuntimeError):
    """The owner did not confirm the projected spend."""


class BudgetExceeded(RuntimeError):
    """Real spend passed the hard limit; the script stopped itself."""


def estimate_llm_cost(
    model: str, input_tokens: int, output_tokens: int, calls: int = 1, cached_input_tokens: int = 0
) -> float | None:
    """Projected dollars for `calls` calls of this size, or None if the price is unknown.

    `cached_input_tokens` is the part of each call's input served from the prompt cache
    (billed at the cheaper cache-read rate when the model has one).
    """
    info = litellm.model_cost.get(model) or litellm.model_cost.get(f"anthropic/{model}")
    if not info or "input_cost_per_token" not in info or "output_cost_per_token" not in info:
        return None
    price_in, price_out = info["input_cost_per_token"], info["output_cost_per_token"]
    price_cached = info.get("cache_read_input_token_cost", price_in)
    fresh = input_tokens - cached_input_tokens
    return calls * (
        fresh * price_in + cached_input_tokens * price_cached + output_tokens * price_out
    )


def confirm_spend(projected_usd: float | None, what: str, *, assume_yes: bool = False) -> None:
    """Print the projection; above the threshold (or when the price is unknown) ask for a yes."""
    limit = get_settings().cost_confirm_above_usd
    if projected_usd is None:
        print(f"[cost guard] {what}: projected cost UNKNOWN (no price for the model).")
        needs_ok = True
    else:
        print(
            f"[cost guard] {what}: projected about ${projected_usd:.4f} (asks above ${limit:.2f})."
        )
        needs_ok = projected_usd > limit
    if needs_ok and not assume_yes:
        if input("Run it? type y to continue: ").strip().lower() != "y":
            raise SpendDeclined(f"{what} was not confirmed")


class SpendTracker:
    """Add up real costs during a script; raise once the hard limit is passed."""

    def __init__(self, hard_limit_usd: float) -> None:
        self.hard_limit_usd = hard_limit_usd
        self.total_usd = 0.0
        self.unpriced_calls = 0  # calls whose cost was unknown: counted, never treated as free

    def add(self, cost_usd: float | None) -> None:
        if cost_usd is None:
            self.unpriced_calls += 1
            return
        self.total_usd += cost_usd
        if self.total_usd > self.hard_limit_usd:
            raise BudgetExceeded(
                f"spent ${self.total_usd:.4f}, over the hard limit ${self.hard_limit_usd:.2f}"
            )
