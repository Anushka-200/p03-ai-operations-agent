"""Live smoke test: one real model call through app.llm.complete(), traced to Langfuse.

Run: uv run python -m scripts.smoke_llm
Costs a fraction of a cent; the cost guard prints the projection first.
"""

from app.cost_guard import confirm_spend, estimate_llm_cost
from app.llm import complete
from app.log import setup_logging
from app.settings import get_settings
from app.tracing import flush


def main() -> None:
    setup_logging()
    s = get_settings()
    projected = estimate_llm_cost(s.llm_default_model, input_tokens=60, output_tokens=300)
    confirm_spend(projected, "smoke test (1 call)")
    result = complete(
        [
            {
                "role": "user",
                "content": "Reply with one short sentence: what does an HVAC technician do?",
            }
        ],
        name="smoke-test",
        max_tokens=300,
    )
    print("\nANSWER :", result.text.strip())
    print(f"MODEL  : {result.model} (fallback used: {result.used_fallback})")
    print(f"TOKENS : {result.input_tokens} in / {result.output_tokens} out")
    print(f"COST   : ${result.cost_usd:.6f}" if result.cost_usd is not None else "COST   : unknown")
    print(f"LATENCY: {result.latency_s}s, attempts: {result.attempts}")
    flush()  # a short script would otherwise exit before the trace is uploaded


if __name__ == "__main__":
    main()
