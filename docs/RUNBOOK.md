# Runbook: when something goes wrong

Everything here is one page on purpose. Commands assume the project folder and `docker compose up` running.
Database shell: `docker compose exec postgres psql -U p03_migrator -d p03` (the migrator role can read everything).

## 0. First look (2 minutes)
1. `curl -s localhost:8003/health` should say `{"status":"ok","dry_run":...}`.
2. `docker compose ps` shows api, scheduler (and telegram-bot if used) running.
3. Last decisions: `SELECT ts, tool, decision, reason FROM audit_log ORDER BY id DESC LIMIT 30;`
4. Messages stuck: `SELECT status, count(*) FROM outbound_messages GROUP BY 1;` and
   `SELECT status, count(*) FROM crm_outbox GROUP BY 1;`

## 1. A provider is down (Twilio, Google Calendar, HubSpot, Anthropic, Telegram)
Symptoms: `AdapterUnavailable` in logs, circuit breaker "open", customers get "we'll confirm by text".
- The breaker opens after 3 failures in 60 s and retries after 120 s by itself. Do nothing for short outages.
- Twilio: messages stay `QUEUED` and go out when it is back. Failed rows (`FAILED`) were rejected: check the reason, do not resend blindly.
- Calendar: bookings are refused with a clear message and a handoff; no half-bookings are created.
- HubSpot: nothing is lost; `crm_outbox` rows retry with backoff (5 attempts, then `FAILED`). To retry:
  `UPDATE crm_outbox SET status='PENDING', attempts=0, next_attempt_at=now() WHERE status='FAILED';`
- Anthropic: the wrapper falls back to the second model; if both fail the customer gets a handoff text and you get an alert.

## 2. Cost spike
- Look at Langfuse (cost per trace and per day), or the `llm call` lines in the JSON logs (`cost_usd`).
- Caps stop a runaway conversation by themselves ($0.25 per conversation, 8 tool calls per message).
- Hard stop: lower the monthly limit on the Anthropic workspace key, or set `DRY_RUN=true` and restart.

## 3. The agent did something wrong (bad action rollback)
1. Find it: `SELECT * FROM audit_log WHERE customer_id = <id> ORDER BY id;` (append-only, cannot be edited).
2. Reverse by hand with the same tools a human would use: cancel the booking (`UPDATE bookings SET status='CANCELLED'` and delete the calendar event), fix the CRM deal, send a correction.
3. Pause the customer so the agent stops: set their status to HANDOFF (`UPDATE customers SET lead_status='HANDOFF' WHERE id=<id>`).
4. Write down what happened and add a scenario to `evals/scenarios/` so it cannot come back.

## 4. Stuck approvals
- Telegram: send `/pending` to the bot to show every waiting approval again.
- Database: `SELECT id, kind, customer_id, created_at FROM approvals WHERE status='PENDING';`
- A customer who wrote again while waiting is not stuck: the owner's decision is delivered as a new turn.
- Expired by hand: `UPDATE approvals SET status='EXPIRED' WHERE id=<id>;` (the customer is not told; tell them yourself).

## 5. Emergency or handoff waiting for a person
- Telegram `/cases` lists customers the agent is paused for (with phone numbers).
- Deal with the customer (call them). Then `/resume <number>` hands them back to the agent.
- No Telegram? `UPDATE customers SET lead_status='QUALIFYING' WHERE id=<id>;` and
  `UPDATE emergencies SET cleared_at=now(), cleared_by='manual' WHERE customer_id=<id> AND cleared_at IS NULL;`

## 6. Reset demo data (clears every store together)
Use the migrator shell and delete by customer id in this order:
checkpoint tables by `thread_id = phone_hash`, then outbound/inbound messages, approvals, followups, crm_outbox, calendar_events, bookings, idempotency_keys, audit_log, customers. `scripts/demo.py` shows the exact statements.

## 7. Go-live checklist (not done in the portfolio version)
US SMS needs A2P 10DLC brand and campaign registration (days to weeks). Until then use the WhatsApp sandbox or a trial number texting a verified phone. Put the webhook behind HTTPS, rotate every key, set `DRY_RUN=false` per integration, one at a time.
