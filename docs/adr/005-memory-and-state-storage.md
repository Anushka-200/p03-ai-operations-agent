# ADR 005: Memory and state storage

Status: accepted (2026-10-09)

## Context
Conversations last days; the server restarts; long threads grow past what the model handles well (context rot)
and cost more per message. Bookings must never depend on chat history being intact.

## Options
- Chat history only (facts live in messages): simplest; facts get lost or contradicted in long threads.
- LangGraph long-term store for customer facts: convenient, harder to audit and delete.
- **Our tables are the truth; checkpoints hold conversation state; compaction for long threads.**

## Decision
- **Source of truth:** `customers`, `bookings`, `approvals`, `audit_log` in Postgres. The checkpoint holds only
  conversation state (messages, summary, typed `intake`, offered slots, counters) keyed by
  `thread_id = hmac(phone)`.
- **Compaction:** when history passes a token threshold, older turns become a short summary (one cheap call);
  structured facts are already in typed state so they cannot be lost. Experiment E4 measures with vs without.
- **No Redis in v1:** scheduler jobs (APScheduler Postgres jobstore), webhook dedupe (`inbound_messages.message_sid`
  unique) and approvals all live in Postgres. Saves a container on a 6 GB machine; port 6381 stays reserved.
- Deleting a customer deletes their rows and their checkpoint thread together (one function, tested).

## Consequences
- Any lost or compacted checkpoint still leaves bookings correct.
- Postgres is a single point of failure for everything; acceptable for a single small business.
