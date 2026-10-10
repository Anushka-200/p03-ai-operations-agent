# ADR 002: Tool permission model

Status: accepted (2026-10-09)

## Context
The agent acts on real systems (calendar, CRM, messages, money). Prompt rules can be talked past (prompt
injection, social engineering); a client needs guarantees that hold even if the model is fully hijacked.

## Options
- Rules in the system prompt only: cheap, not a guarantee.
- Per-tool checks scattered inside each tool: easy to forget one; no single audit point.
- **One gate function every call passes through**, driven by a permissions table: level + scope + caps + audit.

## Decision
`gate.authorize(caller, tool, args) -> Allow | NeedsApproval | Deny`, used by the agent, the MCP server and the
scheduler alike.
- **Level** per (role, tool) in `config/permissions.yaml`: auto / approval / forbidden (table in PLAN.md).
- **Scope:** for customers, `customer_id` comes from the verified phone number, never from model arguments.
  Bookings/records of other customers are invisible.
- **Limits:** discount <= 15% needs approval, above is forbidden; late cancel/reschedule (< 24 h) needs approval;
  max 2 active bookings per customer; 8 tool calls per message, 40 per conversation per day, $0.25 per
  conversation, then handoff.
- **Audit:** the gate writes every decision to the append-only `audit_log` (DB role cannot UPDATE/DELETE it).
- **No free outbound tool:** the reply always goes to the verified sender; there is no `send_sms(to=...)`.
- Unknown tool names are denied and audited.

## Consequences
- One place to test (parametrized role x tool x case matrix) and one place to read for a client.
- Approvals add owner work; the 15% / 24 h thresholds are config, tuned per client.
