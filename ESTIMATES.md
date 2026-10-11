# ESTIMATES: P03 AI Operations Agent
Status: drafted 2026-10-09. Your O/M/P columns are empty on purpose: fill them for a phase **before** starting it.
My own estimates are sealed (git-ignored `.estimates_claude_view.json`) and revealed at the end of each phase, next to your numbers and the actuals.
Rules: PERT = (O + 4M + P) / 6. `x mult` comes from `03_ESTIMATION_TRACKER.md` section B (agent 2.0, integration 1.4, optimization 1.2, testing-evals 1.1, others 1.0); a +15% debugging buffer is added at phase level. Familiarity is my suggestion (Some = met it in P01/P02, New = never); change it if wrong. Estimates are YOUR hours with Claude writing the code; note learning/Q&A and build time separately (P02: learning dominated about 10:1). Every integration has its own "live run + fix" task (P01 lesson). SPIKE = timeboxed. Note the start time before each phase; at 150% of a task's planned hours, stop and diagnose.
Time available (owner, 2026-10-09): "just today". PLAN.md's schedule-risk section has the cut order.

## Phase 0: Foundations
Start time: 20:05 (2026-10-09). Owner's phase-level estimate: M 1 h (O/P not given). Order: 0.1, 0.2, 0.3, then 0.4, then 0.5-0.8.

| # | Task | Category | Familiarity | O | M | P | PERT | x mult | Planned h | Learning h | Build h | Actual total h | Ratio | Why it was off |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.1 | Repo scaffold: uv, ruff, mypy, pytest, pyproject, .env.example, Makefile skeleton | setup | Some | | | | | 1.0 | | | | | | |
| 0.2 | docker-compose: Postgres 16 on 5435 + DB roles (p03_migrator, p03_app) | infra | Some | | | | | 1.0 | | | | | | |
| 0.3 | Settings (pydantic-settings) + JSON logging with phone hashing | setup | Some | | | | | 1.0 | | | | | | |
| 0.4 | Accounts: NEW P03 Anthropic key with spend limit, Langfuse project; verify model IDs and prices | setup | Some | | | | | 1.0 | | | | | | |
| 0.5 | LiteLLM wrapper: timeout, retry+backoff+jitter, cost capture, cost guard ($0.50), fallback model | llm-logic | Some | | | | | 1.0 | | | | | | |
| 0.6 | Langfuse tracing wired + live run and fix | integration | Some | | | | | 1.4 | | | | | | |
| 0.7 | Migrations: full schema + seed (price table, ZIPs, technicians, FAQ, fake calendar) | data | Some | | | | | 1.0 | | | | | | |
| 0.8 | CI skeleton (GitHub Actions: lint + tests) | infra | Some | | | | | 1.0 | | | | | | |
| | **Phase total (+15% debugging buffer)** | | | | | | | | | | | | | |

## Phase 1: Tools + permission gate + MCP
Start time: 15:49 (2026-10-10). Owner's phase-level estimate: M 1 h ("complete the phase in the next hour"; O/P not given). Build order: 1.2-1.8 and 1.11 straight through; stop for 1.9/1.10 (needs the owner at the keyboard, Claude Desktop on Windows).

| # | Task | Category | Familiarity | O | M | P | PERT | x mult | Planned h | Learning h | Build h | Actual total h | Ratio | Why it was off |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 | Learn: MCP docs + Python SDK quickstart, 'Writing effective tools', tool-use docs | learning | New | | | | | 1.0 | | | | | | |
| 1.2 | Tool schemas (Pydantic) + registry + recoverable error format | agent | New | | | | | 2.0 | | | | | | |
| 1.3 | Fake adapters: calendar, CRM, messaging outbox (dry-run) | integration | New | | | | | 1.4 | | | | | | |
| 1.4 | Customer read tools: profile, service area, price, FAQ, find_slots | agent | New | | | | | 2.0 | | | | | | |
| 1.5 | Booking tools: book / reschedule / cancel with idempotency keys + double-booking guard + race test | agent | New | | | | | 2.0 | | | | | | |
| 1.6 | Permission gate + permissions.yaml + audit log + role x tool matrix tests | agent | New | | | | | 2.0 | | | | | | |
| 1.7 | Discount / refund / handoff -> approvals table; owner read tools; bulk follow-up draft | agent | New | | | | | 2.0 | | | | | | |
| 1.8 | MCP server (FastMCP, stdio) + resources + MCP-client test | integration | New | | | | | 1.4 | | | | | | |
| 1.9 | SPIKE (timebox 1 h): Claude Desktop on Windows launching the stdio server via wsl.exe | integration | New | | | | | 1.4 | | | | | | |
| 1.10 | Live run + fix: Claude Desktop lists today's jobs and is denied a forbidden action (dry-run) | integration | New | | | | | 1.4 | | | | | | |
| 1.11 | Draft the 20 scenario YAMLs (reviewed: false) - eval first | testing-evals | New | | | | | 1.1 | | | | | | |
| 1.12 | YOUR review of scenarios (sets reviewed: true; optional, P02 skipped it) | testing-evals | New | | | | | 1.1 | | | | | | |
| | **Phase total (+15% debugging buffer)** | | | | | | | | | | | | | |

## Phase 2: Agent graph + emergency router + webhook
Start time: 

| # | Task | Category | Familiarity | O | M | P | PERT | x mult | Planned h | Learning h | Build h | Actual total h | Ratio | Why it was off |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2.1 | Learn: LangGraph checkpointers + interrupts, context engineering post | learning | New | | | | | 1.0 | | | | | | |
| 2.2 | State + graph skeleton + Postgres checkpointer + restart/resume test | agent | New | | | | | 2.0 | | | | | | |
| 2.3 | Agent node: system prompt, tool binding, step / cost caps, handoff, AI disclosure | agent | New | | | | | 2.0 | | | | | | |
| 2.4 | Approval interrupt + resume after owner decision | agent | New | | | | | 2.0 | | | | | | |
| 2.5 | Output guard (prices from table, no 'booked' without booking_id) + repair-then-fallback | llm-logic | New | | | | | 1.0 | | | | | | |
| 2.6 | Compaction node (summary over token threshold) | llm-logic | New | | | | | 1.0 | | | | | | |
| 2.7 | Emergency pre-router: rules yaml, ~40-phrase set, classifier with timeout, fixed escalation path | llm-logic | New | | | | | 1.0 | | | | | | |
| 2.8 | Twilio webhook: signature check, MessageSid dedupe, save-first, background processing, outbox | integration | Some | | | | | 1.4 | | | | | | |
| 2.9 | Integration test: Maria flow via signed fake webhooks with API restart mid-conversation | testing-evals | New | | | | | 1.1 | | | | | | |
| 2.10 | Live run + fix: Maria flow with the real LLM (dry-run), traces in Langfuse | integration | New | | | | | 1.4 | | | | | | |
| | **Phase total (+15% debugging buffer)** | | | | | | | | | | | | | |

## Phase 3: Follow-ups + owner assistant (Telegram)
Start time: 

| # | Task | Category | Familiarity | O | M | P | PERT | x mult | Planned h | Learning h | Build h | Actual total h | Ratio | Why it was off |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3.1 | Scheduler process: APScheduler Postgres jobstore, followups.yaml, quiet hours, STOP, fake-clock tests | infra | New | | | | | 1.0 | | | | | | |
| 3.2 | Telegram setup: BotFather token, owner user-id allow-list | setup | New | | | | | 1.0 | | | | | | |
| 3.3 | Telegram bot (long polling) + owner agent for read queries | integration | New | | | | | 1.4 | | | | | | |
| 3.4 | Approval buttons resume a paused thread; bulk follow-up approve-then-send | integration | New | | | | | 1.4 | | | | | | |
| 3.5 | Live run + fix: real Telegram bot | integration | New | | | | | 1.4 | | | | | | |
| | **Phase total (+15% debugging buffer)** | | | | | | | | | | | | | |

## Phase 4: Real integrations
Start time: 

| # | Task | Category | Familiarity | O | M | P | PERT | x mult | Planned h | Learning h | Build h | Actual total h | Ratio | Why it was off |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4.1 | Google Cloud setup: project, Calendar API, service account, test calendar | setup | New | | | | | 1.0 | | | | | | |
| 4.2 | Google Calendar adapter + contract tests shared with the fake | integration | New | | | | | 1.4 | | | | | | |
| 4.3 | Live run + fix: Google Calendar | integration | New | | | | | 1.4 | | | | | | |
| 4.4 | HubSpot developer account + private app token | setup | New | | | | | 1.0 | | | | | | |
| 4.5 | HubSpot adapter (contact + deal) + crm_outbox retry + contract tests | integration | New | | | | | 1.4 | | | | | | |
| 4.6 | Live run + fix: HubSpot | integration | New | | | | | 1.4 | | | | | | |
| 4.7 | Twilio: account / WhatsApp sandbox, verified number | setup | Some | | | | | 1.0 | | | | | | |
| 4.8 | Twilio outbound adapter + contract tests | integration | Some | | | | | 1.4 | | | | | | |
| 4.9 | Live run + fix: Twilio outbound (inbound only with tunnel approval) | integration | Some | | | | | 1.4 | | | | | | |
| 4.10 | Circuit breakers per adapter + outage tests | infra | New | | | | | 1.0 | | | | | | |
| | **Phase total (+15% debugging buffer)** | | | | | | | | | | | | | |

## Phase 5: Scenario evals + optimization
Start time: 

| # | Task | Category | Familiarity | O | M | P | PERT | x mult | Planned h | Learning h | Build h | Actual total h | Ratio | Why it was off |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5.1 | Learn: Hamel evals FAQ (agent sections), OWASP Agentic Top 10 | learning | New | | | | | 1.0 | | | | | | |
| 5.2 | Scenario runner: simulated + scripted customer, assertions, report | testing-evals | New | | | | | 1.1 | | | | | | |
| 5.3 | Emergency phrasing suite + permission matrix wired into make eval | testing-evals | New | | | | | 1.1 | | | | | | |
| 5.4 | Baseline 20 x 3 (cost-guarded) + failure analysis | testing-evals | New | | | | | 1.1 | | | | | | |
| 5.5 | Fix top failures from the baseline | agent | New | | | | | 2.0 | | | | | | |
| 5.6 | Supervisor multi-agent mode (2 specialists, same tools and gate) | agent | New | | | | | 2.0 | | | | | | |
| 5.7 | E1 per-step model routing | optimization | New | | | | | 1.2 | | | | | | |
| 5.8 | E2 prompt caching (check minimum cacheable size first) | optimization | New | | | | | 1.2 | | | | | | |
| 5.9 | E3 merged availability + offer (fewer tool calls) | optimization | New | | | | | 1.2 | | | | | | |
| 5.10 | E4 compaction on vs off (long thread) | optimization | New | | | | | 1.2 | | | | | | |
| 5.11 | E5 single vs supervisor + ADR 006 | optimization | New | | | | | 1.2 | | | | | | |
| 5.12 | E6 in-process vs via MCP | optimization | New | | | | | 1.2 | | | | | | |
| 5.13 | ADR 007 OWASP Agentic Top 10 mapping + missing tests | testing-evals | New | | | | | 1.1 | | | | | | |
| | **Phase total (+15% debugging buffer)** | | | | | | | | | | | | | |

## Phase 6: Package
Start time: 

| # | Task | Category | Familiarity | O | M | P | PERT | x mult | Planned h | Learning h | Build h | Actual total h | Ratio | Why it was off |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 6.1 | README: impact line, results, permission table, MCP usage, diagram, limitations | docs-video | Some | | | | | 1.0 | | | | | | |
| 6.2 | docs/architecture.mmd, DEMO_SCRIPT, LEARNING_LOG, RUNBOOK | docs-video | Some | | | | | 1.0 | | | | | | |
| 6.3 | Full compose (api, scheduler, postgres) + Makefile targets + make demo | infra | Some | | | | | 1.0 | | | | | | |
| 6.4 | CI: lint + tests + 5 mocked scenarios | infra | Some | | | | | 1.0 | | | | | | |
| 6.5 | Record 4-minute demo video | docs-video | Some | | | | | 1.0 | | | | | | |
| 6.6 | Edit and publish video | docs-video | Some | | | | | 1.0 | | | | | | |
| 6.7 | Fresh-clone quick start (live run + fix) | integration | Some | | | | | 1.4 | | | | | | |
| 6.8 | Secrets + personal-data scan of the whole history | testing-evals | Some | | | | | 1.1 | | | | | | |
| 6.9 | ESTIMATES summary + tracker row | docs-video | Some | | | | | 1.0 | | | | | | |
| | **Phase total (+15% debugging buffer)** | | | | | | | | | | | | | |

## Time log (IST; START logged when the owner says go, END only when the owner says it)
| Task | Start | End | Note |
|---|---|---|---|
| Phase 0 | 2026-10-09 20:05 | | owner said go; estimate 1 h for the phase |
| Phase 0 (after 0.1-0.3) | | | 0.1-0.3 built; owner left for dinner, pause noted at ~20:29 IST by the system clock (not an end time); 0.4 (owner's accounts) and 0.5-0.8 resume when the owner returns |
| Phase 0 (resume) | 22:15 (2026-10-09) | | owner resumed after dinner, no time stated; start taken from system clock; asked to confirm |
| 0.7 + 0.8 (schema, seed, migration runner, CI skeleton) | ~22:20 (2026-10-09, approx, from system clock) | | built while owner sets up accounts; end only when owner says |
| 0.1-0.3, 0.7, 0.8 (block done so far) | see rows above | 22:33 (2026-10-09) | END stated by owner ("phase 1" in their message; read as Phase 0 tasks done so far). Phase 0 itself stays open until 0.4-0.6 pass |
| 0.4 (accounts) | n/a | n/a | done by owner (workspace, key, Langfuse project); no times given; my part (model IDs + prices) verified 2026-10-09: haiku-5-5 $0.10/$0.50, sonnet-5-5 $2/$10, opus-5-5 $4/$20 per 1M tokens, LiteLLM 1.104.2 knows all three |
| 0.5 + 0.6 (LLM wrapper, cost guard, tracing, live run + fix) | ~22:33 (owner said go right after stating the 22:33 end of the earlier block; approximate) | | built and run live at $0.000021; end only when owner says (system clock was 22:47 when the last check finished) |
| Day 1 stop | | | owner stopped for the day at ~23:31 IST (system clock). No end times logged for 0.5/0.6 (owner has not stated them); resume tomorrow with: Phase 0 close-out hours, quiz answers for 0.5/0.6, commit decision, then Phase 1 |
| Phase 0 | 2026-10-09 20:05 | 2026-10-10 15:49 | END stated by owner. Wall-clock spans a night and a day; actual learning/build hours still to be given at close-out |
| Phase 1 | 2026-10-10 15:49 | 18:01 (2026-10-10) | END stated by owner; estimate was 1 h for the phase (elapsed 2 h 12 min incl. explanations, Q&A and Claude Desktop setup); 1.9/1.10 live run done, close-out hours still to collect |
| 1.2-1.8 + 1.11 (tools, fakes, gate, audit, approvals, MCP server, 22 scenario drafts) | 2026-10-10 15:49 | | built straight through; stopped for 1.9/1.10 (owner at keyboard) at ~16:07 IST system clock; end only when owner says |
| Phase 2 + first half of Phase 3 | 2026-10-10 ~19:20 IST (system clock 13:49 UTC + 5:30; owner said "let's start") | | owner estimate: 1 h for Phase 2 plus half of Phase 3 (O/P not given). Claude's flag: Phase 1 ran 2.2x its 1 h estimate and Phase 2 is larger; build order = 2.7 emergency router, 2.8 webhook + dedupe + outbox, 2.2 graph + checkpointer, 2.3 agent node + caps, 2.4 approval interrupt, 2.5 output guard, then stop for owner. End only when owner says |
| 2.7, 2.8, 2.2-2.5, 2.9-2.10 (partly), 3.1 built in one stretch | ~19:20 IST | | stopped for owner at ~14:10 UTC system clock (~19:40 IST, about 21 min of tool time; the owner's clock decides). Built: emergency router, Twilio webhook + dedupe + save-first + STOP, agent graph + Postgres checkpoints + approval pause/resume + caps + output guard, live Maria run ($0.0055), follow-up tick + scheduler process. NOT built: 2.6 compaction, 3.2-3.5 Telegram. End only when owner says |
| 2.6 compaction node | after owner's commit request (2026-10-11 system clock) | | built (reducer with replace marker, summary in system prompt as labelled data, cut only at customer messages, failure keeps history); live summary check on real Haiku $0.0003; owner states the end |
| Phase 3 (3.2-3.5 Telegram owner bot) | 2026-10-11 14:40 IST (system clock 09:10 UTC + 5:30; owner said "mark start of phase 3") | | owner contributes the BotFather step while Claude builds 3.3/3.4 on a fake Telegram; estimate for the remaining half of Phase 3 not given by owner. End only when owner says |
| 3.3 + 3.4 Telegram bot logic, approval buttons, owner assistant (fake Telegram) | 2026-10-11 ~14:40 IST | | built and tested (19 tests, suite 207 green); about 35 min of tool time by the system clock; 3.2 (BotFather) and 3.5 (live run) wait for the owner. End only when owner says |
| Phase 2 (incl. 3.1, 2.6) | 2026-10-10 ~19:20 | 2026-10-11 15:01 | END stated by owner. Wall-clock spans an overnight break and long Q&A; actual learning/build hours still to collect at close-out |
