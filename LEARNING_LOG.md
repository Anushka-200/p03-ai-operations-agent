# Learning log: P03 AI Operations Agent

Plain-words notes on what I learned building this, written for the next person (and for me on a client call).

## Concepts, each with the everyday picture

| Concept | Everyday picture | Where it lives |
|---|---|---|
| Permission gate | A single doorman every request must pass; he checks your badge, your room and how many times you came in, and writes it in a ledger | `app/gate.py`, `app/tools/registry.py` |
| Least agency | Give the temp worker only the keys the job needs. No key to the cash box means nobody can talk them into opening it | ADR 002, ADR 007 |
| Audit log | A ledger in ink: you can add lines, never erase them (the database role has no UPDATE or DELETE on it) | `audit_log`, migration 0001 |
| Emergency pre-router | A smoke alarm: it trips on a rule, it does not "think" | `app/emergency.py`, ADR 003 |
| Idempotency key | A numbered ticket: showing the same ticket twice gets you the same prize once | `idempotency_keys`, outbox keys |
| Advisory lock | A "do not disturb" sign on one patient folder; other folders are untouched | `app/concurrency.py`, ADR 008 |
| Exclusion constraint | The building's own rule that two bookings for one technician cannot overlap, no matter what the code says | `no_double_booking`, migration 0001 |
| Checkpoint | A patient folder that is saved after every step, so a restart loses nothing | LangGraph Postgres checkpointer |
| Outbox pattern | Write the letter in the book first, a separate postman delivers it, retrying if the road is closed | `outbound_messages`, `crm_outbox` |
| Circuit breaker | A household fuse: after repeated failures stop trying for a while instead of hammering a broken service | `app/adapters/resilience.py` |
| Long polling | Asking the post office "anything for me?" and waiting at the window, instead of giving them our address | `app/adapters/telegram.py` |
| Compaction | Replacing old pages of a long chat with a short summary, keeping hard facts in the database | `compact_node`, ADR 005 |
| Output guard | A manager reading each outgoing letter for two mistakes: invented prices and a false "you're booked" | `app/agent/guard.py` |
| Eval with an audit-log grader | A driving test where the examiner reads the car's recorder, not the driver's story | `evals/run.py` |

## Things that surprised me (each one cost time or found a bug)
1. **Live runs find bugs fakes cannot.** The handoff alert had no recipient, so it passed every unit test and would never have
   reached the owner's phone. Found only when tracing what actually arrives on Telegram. The scheduler never called the CRM
   sync either: the function existed and was tested, nothing ran it.
2. **A safety check can be too eager.** The output guard blocked "once the visit is booked" as a false claim; the customer was
   handed to a human for a harmless sentence. A guard needs tests for the sentences it must allow, not only the ones it must block.
3. **Test the grader.** A grader that silently ignores an unknown check passes everything. Ours fails closed.
4. **Order of calendar calls matters.** Delete-then-create can leave a customer with no event if the second call fails;
   create-then-delete leaves at most a duplicate we can clean up.
5. **Tests that commit leak into the dev database.** The bot and `process_inbound` commit like production; tests wrap the
   connection so commit does nothing.
6. **Personal data hides in places you do not look.** The secrets scan flagged its own source file for containing an email;
   personal search terms now live in `.env`, not in the repo.
7. **Asking for a decision beats guessing.** Several rules (the 15% discount cap, 24-hour late-cancel window) are business
   choices; they live in `config/` where the owner can change them without touching code.

## Estimation notes
See `ESTIMATES.md`. In this project the learning and Q&A time was several times the build time, as in P02.
