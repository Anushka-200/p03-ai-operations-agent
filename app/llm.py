"""The one place every LLM call goes through.

Why a wrapper: timeouts, retries, cost and latency capture, tracing and the fallback model live here
once, instead of being re-implemented (and forgotten) in every module. Swapping the model, adding
caching or a circuit breaker later is a change in this file only.
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import litellm
from litellm import exceptions as llm_exc

from app.settings import Settings, get_settings
from app.tracing import observation

logger = logging.getLogger(__name__)
# LiteLLM logs every call at INFO in its own format; we already log one JSON line per call.
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# Temporary problems: trying again can work. Everything else (bad request, wrong key) is our
# mistake and would fail the same way every time, so retrying only wastes time and money.
RETRYABLE = (
    llm_exc.RateLimitError,
    llm_exc.APIConnectionError,
    llm_exc.Timeout,
    llm_exc.ServiceUnavailableError,
    llm_exc.InternalServerError,
)


class LLMError(RuntimeError):
    """Raised when a call still fails after all retries, or fails in a way retries cannot fix."""


@dataclass
class LLMResult:
    text: str
    model: str  # the model that actually answered (the fallback, if it was used)
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None if LiteLLM has no price for this model yet; never silently 0
    latency_s: float
    attempts: int
    cached_input_tokens: int = 0  # part of the input served from the prompt cache
    used_fallback: bool = False
    # Tool use (agent): what the model asked to run, and the assistant message to append to history.
    tool_calls: list[dict[str, Any]] | None = None  # [{"id", "name", "arguments": json string}]
    message: dict[str, Any] | None = None


def _cached_tokens(usage: Any) -> int:
    """Tokens read from the prompt cache; providers name this differently, so try each."""
    details = getattr(usage, "prompt_tokens_details", None)
    return int(getattr(usage, "cache_read_input_tokens", 0) or getattr(details, "cached_tokens", 0) or 0)


def _assistant_message(content: str | None, calls: list[dict[str, Any]]) -> dict[str, Any]:
    """The assistant turn in OpenAI shape, plain JSON so it can be stored in a checkpoint."""
    m: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if calls:
        m["tool_calls"] = [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in calls
        ]
    return m


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 20.0) -> float:
    """Seconds to wait before retry number `attempt` (1, 2, 3...).

    Exponential: the ceiling doubles each time (1, 2, 4...) up to `cap`.
    Full jitter: pick a random point below the ceiling, so many clients don't retry in lockstep.
    """
    return random.uniform(0, min(cap, base * 2 ** (attempt - 1)))


def complete(
    messages: list[dict[str, Any]],
    *,
    name: str = "llm-call",
    model: str | None = None,
    max_tokens: int = 1024,
    # None = do not send it: Haiku 5.5 and Sonnet 5.5 reject non-default sampling values.
    temperature: float | None = None,
    allow_fallback: bool = True,
    tools: list[dict[str, Any]] | None = None,  # OpenAI-style tool schemas; LiteLLM translates
    settings: Settings | None = None,
    # Injected so tests can run without network, real sleeping or a real key.
    completion_fn: Callable[..., Any] = litellm.completion,
    cost_fn: Callable[..., float] = litellm.completion_cost,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> LLMResult:
    s = settings or get_settings()
    model = model or s.llm_default_model
    with observation(
        as_type="generation",
        name=name,
        model=model,
        input=messages,
        model_parameters={"max_tokens": max_tokens, "temperature": temperature},
    ) as obs:
        try:
            result = _call_with_retries(
                messages, model, max_tokens, temperature, s, completion_fn, cost_fn, sleep_fn, tools
            )
        except LLMError as exc:
            # Provider trouble on the default model: one try on a different model, so a customer
            # message is not lost to a single model's outage. Only for retryable failures.
            if not (allow_fallback and exc.__cause__ and isinstance(exc.__cause__, RETRYABLE)):
                raise
            if model == s.llm_fallback_model:
                raise
            logger.warning(
                "falling back to another model",
                extra={"failed_model": model, "fallback_model": s.llm_fallback_model},
            )
            result = _call_with_retries(
                messages, s.llm_fallback_model, max_tokens, temperature, s,
                completion_fn, cost_fn, sleep_fn, tools,
            )  # fmt: skip
            result.used_fallback = True
        if obs is not None:
            obs.update(
                model=result.model,
                output=result.text,
                usage_details={"input": result.input_tokens, "output": result.output_tokens},
                cost_details={"total": result.cost_usd} if result.cost_usd is not None else None,
                metadata={
                    "attempts": result.attempts,
                    "latency_s": result.latency_s,
                    "used_fallback": result.used_fallback,
                },
            )
    return result


def _call_with_retries(
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    temperature: float | None,
    s: Settings,
    completion_fn: Callable[..., Any],
    cost_fn: Callable[..., float],
    sleep_fn: Callable[[float], None],
    tools: list[dict[str, Any]] | None = None,
) -> LLMResult:
    api_key = s.anthropic_api_key.get_secret_value() or None
    max_attempts = s.llm_max_retries + 1
    started = time.perf_counter()

    for attempt in range(1, max_attempts + 1):
        try:
            extra: dict[str, Any] = {} if temperature is None else {"temperature": temperature}
            if tools:
                extra["tools"] = tools
            if s.llm_prompt_caching:
                # One cache breakpoint after the system message; Anthropic's prefix order (tools, system)
                # means the tool schemas are cached with it. Only pays off above the model's minimum size (E2).
                extra["cache_control_injection_points"] = [{"location": "message", "role": "system"}]
            response = completion_fn(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                timeout=s.llm_timeout_s,
                api_key=api_key,
                num_retries=0,  # we retry ourselves, so nothing happens behind our back
                **extra,
            )
            break
        except RETRYABLE as exc:
            if attempt == max_attempts:
                raise LLMError(f"{model} failed after {attempt} attempts: {exc}") from exc
            delay = backoff_delay(attempt)
            logger.warning(
                "llm call failed, retrying",
                extra={
                    "model": model,
                    "attempt": attempt,
                    "retry_in_s": round(delay, 2),
                    "error": type(exc).__name__,
                },
            )
            sleep_fn(delay)
        except Exception as exc:  # not retryable: fail fast with a readable message
            raise LLMError(f"{model} call failed ({type(exc).__name__}): {exc}") from exc

    try:
        cost = cost_fn(completion_response=response)
    except Exception:
        cost = None
        logger.warning("no price known for model", extra={"model": model})

    msg = response.choices[0].message
    calls = [
        {"id": c.id, "name": c.function.name, "arguments": c.function.arguments or "{}"}
        for c in (getattr(msg, "tool_calls", None) or [])
    ]
    result = LLMResult(
        text=msg.content or "",
        model=model,
        input_tokens=response.usage.prompt_tokens,
        output_tokens=response.usage.completion_tokens,
        cost_usd=cost,
        latency_s=round(time.perf_counter() - started, 3),
        attempts=attempt,
        cached_input_tokens=_cached_tokens(response.usage),
        tool_calls=calls or None,
        message=_assistant_message(msg.content, calls),
    )
    logger.info(
        "llm call",
        extra={
            "model": model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": cost,
            "latency_s": result.latency_s,
            "attempts": attempt,
        },
    )
    return result
