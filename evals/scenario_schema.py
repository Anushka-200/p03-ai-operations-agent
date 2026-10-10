"""The shape of an eval scenario file (evals/scenarios/*.yaml) and a loader that checks every file.

A scenario says WHAT should happen, never HOW the agent does it: which tools it must call (in order),
which it must never call, what the database must look like at the end, and what the reply must or must
not contain. The runner (Phase 5) plays the customer and checks these.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools_in_order: list[str] = Field(
        default_factory=list, description="Must appear in this order (other calls may sit between)"
    )
    forbidden_tools: list[str] = Field(
        default_factory=list, description="Must NEVER be called with success in this scenario"
    )
    max_tool_calls: int = Field(default=10, description="Over this = the agent is wandering")
    llm_calls: int | None = Field(
        default=None, description="Exact number of LLM calls; 0 for rule-handled scenarios"
    )
    final_state: dict[str, Any] = Field(
        default_factory=dict, description="Checked against the database after the run"
    )
    reply_must_match: list[str] = Field(
        default_factory=list, description="Regexes the customer-visible replies must match"
    )
    reply_must_not_match: list[str] = Field(
        default_factory=list, description="Regexes that must not appear in any reply"
    )


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    category: Literal[
        "normal", "difficult", "incomplete_info", "double_booking", "duplicate", "emergency",
        "outage", "injection", "privacy", "out_of_area", "faq", "opt_out", "long_thread",
    ]  # fmt: skip
    reviewed: bool = False  # stays False until the OWNER has read it (P01/P02 lesson)
    mode: Literal["simulated", "scripted"]  # simulated = an LLM plays the persona; scripted = fixed messages
    persona: str = ""
    goal: str
    opening_message: str
    script: list[str] = Field(
        default_factory=list, description="Fixed follow-up customer messages (scripted mode)"
    )
    setup: dict[str, Any] = Field(
        default_factory=dict, description="Starting data: customer, bookings, busy times, outages"
    )
    expect: Expect


def load_scenarios(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    scenarios = [
        Scenario.model_validate(yaml.safe_load(p.read_text())) for p in sorted(directory.glob("*.yaml"))
    ]
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate scenario ids")
    return scenarios
