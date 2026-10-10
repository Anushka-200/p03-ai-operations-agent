-- P03 schema (PLAN.md "Schema"). Runs as p03_migrator, which owns every table created here.
-- The app role p03_app gets SELECT/INSERT/UPDATE/DELETE automatically (default privileges in
-- docker/postgres/init/01-roles.sh); audit_log is then locked down below.

-- Needed so the database itself can refuse overlapping bookings for one technician.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------- business data ----------
CREATE TABLE customers (
    id             bigserial PRIMARY KEY,
    phone          text NOT NULL UNIQUE,          -- E.164, e.g. +15125550123. PERSONAL DATA
    phone_hash     text NOT NULL UNIQUE,          -- keyed hash: safe to log, used for thread ids
    name           text,
    email          text,
    address        text,
    zip            text,
    lead_status    text NOT NULL DEFAULT 'NEW',   -- mirrors the agent state so the scheduler need not read checkpoints
    opted_out_at   timestamptz,                   -- set by "STOP"; permanent
    crm_contact_id text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- The price table: the ONLY source of prices anywhere in the system.
CREATE TABLE services (
    code         text PRIMARY KEY,
    name         text NOT NULL,
    price_usd    numeric(8,2) NOT NULL CHECK (price_usd >= 0),
    duration_min integer NOT NULL CHECK (duration_min > 0),
    includes     text NOT NULL,
    active       boolean NOT NULL DEFAULT true
);

CREATE TABLE service_area_zips (
    zip  text PRIMARY KEY,
    note text
);

CREATE TABLE technicians (
    id        bigserial PRIMARY KEY,
    name      text NOT NULL,
    phone     text NOT NULL,
    on_call   boolean NOT NULL DEFAULT false,
    active    boolean NOT NULL DEFAULT true
);

CREATE TABLE faq (
    id       bigserial PRIMARY KEY,
    question text NOT NULL,
    answer   text NOT NULL,
    keywords text NOT NULL                        -- space-separated, matched by search_faq
);

-- Source of truth for jobs. Checkpoints never hold the only copy of a booking (ADR 005).
CREATE TABLE bookings (
    id                bigserial PRIMARY KEY,
    customer_id       bigint NOT NULL REFERENCES customers(id),
    technician_id     bigint NOT NULL REFERENCES technicians(id),
    service_code      text NOT NULL REFERENCES services(code),
    start_at          timestamptz NOT NULL,
    end_at            timestamptz NOT NULL,
    status            text NOT NULL DEFAULT 'BOOKED'
                      CHECK (status IN ('BOOKED', 'CANCELLED', 'COMPLETED')),
    price_usd         numeric(8,2) NOT NULL,       -- price quoted at booking time, copied from services
    calendar_event_id text,
    crm_deal_id       text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (end_at > start_at),
    -- Two active bookings for the same technician may not overlap. Enforced by Postgres, so even
    -- two simultaneous requests cannot double-book (one of them gets an error).
    CONSTRAINT no_double_booking EXCLUDE USING gist (
        technician_id WITH =,
        tstzrange(start_at, end_at) WITH &&
    ) WHERE (status = 'BOOKED')
);
CREATE INDEX bookings_customer_idx ON bookings (customer_id, start_at);

-- The fake calendar: busy blocks that are not our bookings (lunch, other jobs, outages in tests).
CREATE TABLE calendar_events (
    id            bigserial PRIMARY KEY,
    technician_id bigint NOT NULL REFERENCES technicians(id),
    start_at      timestamptz NOT NULL,
    end_at        timestamptz NOT NULL,
    title         text NOT NULL,
    booking_id    bigint REFERENCES bookings(id),
    CHECK (end_at > start_at)
);

-- ---------- messaging ----------
CREATE TABLE inbound_messages (
    id           bigserial PRIMARY KEY,
    message_sid  text NOT NULL UNIQUE,             -- Twilio's id: a repeated delivery is ignored (dedupe)
    customer_id  bigint REFERENCES customers(id),
    channel      text NOT NULL CHECK (channel IN ('sms', 'whatsapp', 'telegram')),
    body         text NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

-- The outbox: every message we intend to send. dry_run rows are recorded but never leave the machine.
CREATE TABLE outbound_messages (
    id              bigserial PRIMARY KEY,
    customer_id     bigint REFERENCES customers(id),
    channel         text NOT NULL,
    body            text NOT NULL,
    dry_run         boolean NOT NULL,
    status          text NOT NULL DEFAULT 'QUEUED'
                    CHECK (status IN ('QUEUED', 'SENT', 'FAILED', 'DRY_RUN')),
    idempotency_key text UNIQUE,                   -- same key twice = one message
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz
);

-- ---------- control ----------
CREATE TABLE approvals (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL,                    -- discount | refund | late_cancel | bulk_followup ...
    customer_id  bigint REFERENCES customers(id),
    payload      jsonb NOT NULL,
    requested_by text NOT NULL,                    -- agent | owner | system
    status       text NOT NULL DEFAULT 'PENDING'
                 CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
    decided_by   text,
    decided_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE idempotency_keys (
    key        text PRIMARY KEY,
    tool       text NOT NULL,
    result     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Every decision the permission gate makes, allowed or not. Append-only (see REVOKE below).
CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    caller_role text NOT NULL,                     -- customer | owner | system
    customer_id bigint,
    tool        text NOT NULL,
    args        jsonb,                             -- already redacted by the caller
    decision    text NOT NULL CHECK (decision IN ('ALLOW', 'NEEDS_APPROVAL', 'DENY')),
    result      jsonb,
    reason      text,
    trace_id    text
);
CREATE INDEX audit_log_ts_idx ON audit_log (ts);
-- The app can add rows but never change or remove them, even if its code is hijacked.
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM p03_app;

CREATE TABLE emergencies (
    id                 bigserial PRIMARY KEY,
    customer_id        bigint REFERENCES customers(id),
    inbound_message_id bigint REFERENCES inbound_messages(id),
    matched_rule       text NOT NULL,
    detected_by        text NOT NULL CHECK (detected_by IN ('keyword', 'classifier')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    cleared_at         timestamptz,
    cleared_by         text
);

CREATE TABLE followups (
    id          bigserial PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id),
    rule        text NOT NULL,                     -- e.g. day1, day3, day7, reminder
    due_at      timestamptz NOT NULL,
    status      text NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'SENT', 'SKIPPED', 'CANCELLED')),
    skip_reason text,
    sent_at     timestamptz,
    UNIQUE (customer_id, rule)                     -- scheduling the same follow-up twice is a no-op
);

-- CRM updates are queued here and retried, so a CRM outage never fails a booking.
CREATE TABLE crm_outbox (
    id              bigserial PRIMARY KEY,
    customer_id     bigint NOT NULL REFERENCES customers(id),
    kind            text NOT NULL,                 -- upsert_contact | upsert_deal
    payload         jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'DONE', 'FAILED')),
    attempts        integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text
);
