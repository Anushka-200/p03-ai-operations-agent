# ADR 003: Emergency handling outside the LLM

Status: accepted (2026-10-09)

## Context
"I smell gas", flooding, carbon monoxide or no heat in freezing weather are safety events. An LLM may answer
calmly, ask follow-up questions, or be down. The response must be immediate, identical every time, and work with
no model available.

## Options
- Let the agent decide (prompt says "escalate emergencies"): fails silently on outages and odd phrasing.
- LLM classifier alone: better recall on paraphrases, but slow, costs money, and can be down.
- **Deterministic keyword/regex rules first, classifier only as extra coverage.**

## Decision
- `config/emergency.yaml` keyword and regex rules run before the graph's agent. A match decides alone; when in doubt,
  escalate (a false alarm costs one phone call; a miss can cost a life).
- A cheap classifier (2 s timeout) runs alongside to catch paraphrases the keywords miss; if it fails, the keyword
  result stands.
- The emergency path makes no LLM call: fixed template reply to the customer (safety instructions), page the
  on-call technician, alert the owner, write `emergencies` + audit, set status `EMERGENCY` so the agent stays
  paused for that customer until a human clears it.

## Consequences
- Measured by a deterministic phrasing set (~40 items incl. negations like "no gas smell"): recall target 100%,
  false-positive rate reported.
- Negations will over-trigger; accepted and documented.
