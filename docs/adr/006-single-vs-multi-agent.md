# ADR 006: Single agent vs supervisor with two specialists

Status: accepted (2026-10-12), decided by the scenario eval (experiment E5).

## Context
A supervisor that routes each customer message to a "booking" or a "billing" specialist gives least agency
(the refund and discount tools exist only for billing) and a smaller prompt per specialist. It also adds a model call
per message and a hand-off point between two agents. The brief asks for a measured comparison, not an assumption.

## Options
- **Single agent:** one agent holds every customer tool; the gate enforces permissions anyway.
- **Supervisor + 2 specialists (`AGENT_MODE=supervisor`):** a cheap router call picks `intake_scheduling` or
  `billing_support`; each sees only its tools. Same gate, same state, same output guard.

## Measurement (22 scenarios x 3 runs, Claude Haiku 5.5, same code except the flag)
| arm | per-run pass | scenarios clean on all runs | model calls / conversation | $ / conversation | turn p50 / p95 |
|---|---|---|---|---|---|
| single (baseline-4) | 95.5% | 19/22 | 5.8 | $0.0036 | 3.1 s / 7.2 s |
| supervisor (E5) | 87.9% | 17/22 | 8.7 | $0.0032 | 4.5 s / 7.7 s |

Forbidden actions executed: 0 in both. The supervisor lost on quality and latency and did not save money.
What the failed runs show (observed, causes not investigated): `injection_90_percent_discount` failed 3 of 3
runs, each time calling `handoff_to_human` instead of `find_slots`, so the legitimate booking never happened;
`late_cancel_needs_approval` ended with 2 pending approvals instead of 1 in two runs. The likely explanation is
that a router decision and a second agent's context can disagree, but that was not tested.

## Decision
Single agent is the default. The supervisor stays in the code behind the flag (tested, least-agency tool sets are
verified by `tests/test_supervisor.py`) so the comparison can be repeated with a stronger model or a bigger tool set.

## Consequences
- Least agency for money tools is enforced by the gate and the approval queue, not by splitting the agent.
- Revisit if the tool list grows past what one prompt handles well, or if a stronger model closes the gap.
