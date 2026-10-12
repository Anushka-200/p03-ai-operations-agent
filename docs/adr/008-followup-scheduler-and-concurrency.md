# ADR 008: Follow-up scheduling without a job store, and one conversation at a time

Status: accepted (2026-10-12). Changes the plan in ADR 005 ("APScheduler Postgres jobstore").

## Context
Follow-ups (Day 0/1/3/7, visit reminders) must survive restarts, never fire twice, and re-check the
customer's state at send time. Separately, two messages from one customer can arrive at the same moment.

## Options
- APScheduler with a Postgres jobstore: one scheduled job per follow-up, stored by the library.
  Two sources of truth (our `followups` table and the library's job table) that can disagree.
- **Our `followups` table is the schedule; APScheduler only fires a `tick()` every 60 s.** The tick selects due
  PENDING rows with `FOR UPDATE SKIP LOCKED`, re-checks the customer, then sends or skips.
- A cron-style external scheduler: another moving part for no gain at this size.

## Decision
- `followups(customer_id, rule, due_at, status)` with `UNIQUE (customer_id, rule)`: scheduling twice is a no-op,
  cancelling is an UPDATE, and the whole schedule is visible with one SELECT.
- At send time the tick re-checks opted_out, HANDOFF/EMERGENCY, already booked and booking still active, and
  defers to 8:00 Austin time outside quiet hours. A crash between "send" and "mark SENT" can repeat one
  message at most (idempotency key on the outbox row removes even that).
- Concurrency: `pg_advisory_xact_lock(hashtext('conv:<customer_id>'))` is taken at the start of handling an
  inbound message and before resuming a paused thread after an owner tap. The second message of the same
  customer waits; other customers never wait. Postgres releases the lock at COMMIT/ROLLBACK, so a crashed
  worker cannot leave a customer locked.

## Consequences
- One series of follow-ups per customer ever (the unique key); a second enquiry later starts no new series.
  Accepted for v1 and listed in the README limitations.
- Same-customer messages are handled in order, at the cost of the second reply waiting for the first.
- The tick has up to 60 s of timing jitter, which is fine for hour/day-scale follow-ups.
