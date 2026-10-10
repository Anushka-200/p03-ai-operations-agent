"""LLM wrapper tests: no network, no key, no real sleeping (fakes are injected)."""

from types import SimpleNamespace

import pytest
from litellm import exceptions as llm_exc

from app.cost_guard import (
    BudgetExceeded,
    SpendDeclined,
    SpendTracker,
    confirm_spend,
    estimate_llm_cost,
)
from app.llm import LLMError, backoff_delay, complete
from app.settings import Settings


def _response(text="hi", pin=100, pout=20):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(
            prompt_tokens=pin, completion_tokens=pout, prompt_tokens_details=None
        ),
    )


class FakeLLM:
    """Plays back a script of outcomes: an exception instance is raised, anything else is returned."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _rate_limit():
    return llm_exc.RateLimitError("slow down", "anthropic", "claude-haiku-5-5")


def _bad_request():
    return llm_exc.BadRequestError("nope", "claude-haiku-5-5", "anthropic")


def _settings():
    return Settings(_env_file=None, llm_max_retries=2)


def run(fake, **kw):
    sleeps = []
    result = complete(
        [{"role": "user", "content": "x"}],
        settings=_settings(),
        completion_fn=fake,
        cost_fn=lambda completion_response: 0.001,
        sleep_fn=sleeps.append,
        **kw,
    )
    return result, sleeps


def test_success_captures_tokens_cost_and_latency():
    result, _ = run(FakeLLM(_response("hello", 120, 30)))
    assert (result.text, result.input_tokens, result.output_tokens) == ("hello", 120, 30)
    assert result.cost_usd == 0.001 and result.attempts == 1 and not result.used_fallback


def test_retries_temporary_errors_then_succeeds_with_backoff():
    fake = FakeLLM(_rate_limit(), _rate_limit(), _response())
    result, sleeps = run(fake)
    assert result.attempts == 3 and len(sleeps) == 2 and len(fake.calls) == 3


def test_bad_request_is_not_retried():
    fake = FakeLLM(_bad_request())
    with pytest.raises(LLMError):
        run(fake)
    assert len(fake.calls) == 1


def test_provider_outage_falls_back_to_the_other_model():
    # default model fails 3 times (1 try + 2 retries), then the fallback model answers
    fake = FakeLLM(_rate_limit(), _rate_limit(), _rate_limit(), _response("from fallback"))
    result, _ = run(fake)
    assert result.used_fallback and result.model == "claude-sonnet-5-5"
    assert fake.calls[-1]["model"] == "claude-sonnet-5-5"


def test_fallback_can_be_switched_off():
    with pytest.raises(LLMError):
        run(FakeLLM(_rate_limit(), _rate_limit(), _rate_limit()), allow_fallback=False)


def test_bad_request_does_not_trigger_fallback():
    fake = FakeLLM(_bad_request())
    with pytest.raises(LLMError):
        run(fake)
    assert len(fake.calls) == 1


def test_temperature_is_not_sent_unless_asked():
    fake = FakeLLM(_response(), _response())
    run(fake)
    assert "temperature" not in fake.calls[0]
    run(fake, temperature=0.2)
    assert fake.calls[1]["temperature"] == 0.2


def test_retries_are_ours_not_litellms():
    fake = FakeLLM(_response())
    run(fake)
    assert fake.calls[0]["num_retries"] == 0


def test_unknown_price_is_none_not_zero():
    def boom(**_):
        raise ValueError("no price")

    result = complete(
        [{"role": "user", "content": "x"}],
        settings=_settings(),
        completion_fn=FakeLLM(_response()),
        cost_fn=boom,
    )
    assert result.cost_usd is None


def test_backoff_grows_and_is_capped():
    assert all(0 <= backoff_delay(1) <= 1 for _ in range(50))
    assert all(0 <= backoff_delay(10) <= 20 for _ in range(50))


def test_estimate_cost_matches_published_haiku_price():
    # 1M input + 1M output tokens of Haiku 5.5 = $0.10 + $0.50 (checked against the docs 2026-10-09)
    assert estimate_llm_cost("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)


def test_estimate_unknown_model_is_none():
    assert estimate_llm_cost("no-such-model", 10, 10) is None


def test_confirm_spend_asks_only_above_threshold(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("should not ask"))
    confirm_spend(0.10, "small run")  # below $0.50: no prompt
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(SpendDeclined):
        confirm_spend(0.75, "big run")
    confirm_spend(0.75, "big run", assume_yes=True)  # --yes skips the prompt


def test_unknown_cost_always_asks(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(SpendDeclined):
        confirm_spend(None, "mystery model")


def test_spend_tracker_stops_at_hard_limit_and_counts_unpriced_calls():
    t = SpendTracker(hard_limit_usd=0.01)
    t.add(0.004)
    t.add(None)
    assert t.unpriced_calls == 1
    with pytest.raises(BudgetExceeded):
        t.add(0.008)


def test_tests_never_talk_to_langfuse():
    # Regression: the first live run found fake test calls uploaded to the real project.
    from app import tracing

    assert tracing.get_client() is None
