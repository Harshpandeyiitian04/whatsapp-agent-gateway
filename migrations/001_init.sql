-- Core schema. Invariants that protect correctness live here as constraints, not only in code.

CREATE TABLE conversations (
    id               bigserial PRIMARY KEY,
    phone            text        NOT NULL UNIQUE,
    state            text        NOT NULL DEFAULT 'NEW'
                     CHECK (state IN ('NEW','COLLECTING','SLOTS_OFFERED','BOOKING_PENDING','BOOKED','HANDOFF')),
    context          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    history          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    -- queue fields: the conversation is the unit of work; the lease is the visibility timeout
    has_pending      boolean     NOT NULL DEFAULT false,
    pending_since    timestamptz,
    next_attempt_at  timestamptz NOT NULL DEFAULT '-infinity',
    lease_token      uuid,
    lease_until      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_pending_idx ON conversations (pending_since) WHERE has_pending;
CREATE INDEX conversations_updated_idx ON conversations (updated_at DESC);

CREATE TABLE inbound_messages (
    id               bigserial PRIMARY KEY,
    wa_message_id    text        NOT NULL,
    conversation_id  bigint      NOT NULL REFERENCES conversations(id),
    phone            text        NOT NULL,
    msg_type         text        NOT NULL,
    body             text        NOT NULL DEFAULT '',
    wa_timestamp     timestamptz NOT NULL,
    status           text        NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','done','rate_limited','held','dead')),
    attempts         int         NOT NULL DEFAULT 0,
    last_error       text,
    received_at      timestamptz NOT NULL DEFAULT now(),
    processed_at     timestamptz,
    CONSTRAINT inbound_messages_wa_message_id_key UNIQUE (wa_message_id)   -- dedupe
);
CREATE INDEX inbound_messages_conv_status_idx ON inbound_messages (conversation_id, status, id);
CREATE INDEX inbound_messages_queued_idx ON inbound_messages (received_at) WHERE status = 'queued';

-- Append-only so counting dedupe hits never contends on a single counter row.
CREATE TABLE duplicate_deliveries (
    id             bigserial PRIMARY KEY,
    wa_message_id  text        NOT NULL,
    received_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rate_limits (
    phone           text PRIMARY KEY,
    tokens          double precision NOT NULL CHECK (tokens >= 0),
    updated_at      timestamptz NOT NULL,
    last_notice_at  timestamptz
);

CREATE TABLE bookings (
    id               bigserial PRIMARY KEY,
    idempotency_key  text        NOT NULL,
    attempt_no       int         NOT NULL DEFAULT 1,
    conversation_id  bigint      NOT NULL REFERENCES conversations(id),
    phone            text        NOT NULL,
    test_code        text        NOT NULL,
    slot_id          text        NOT NULL,
    patient_name     text        NOT NULL,
    status           text        NOT NULL CHECK (status IN ('pending','confirmed','unknown','failed')),
    lab_booking_id   text,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bookings_idempotency_key_key UNIQUE (idempotency_key)
);
-- The two "never double-book" invariants. 'unknown' counts as active: a booking we cannot
-- disprove keeps its slot held.
CREATE UNIQUE INDEX bookings_active_slot_uq ON bookings (slot_id)
    WHERE status IN ('pending','confirmed','unknown');
CREATE UNIQUE INDEX bookings_active_patient_uq ON bookings (phone, test_code)
    WHERE status IN ('pending','confirmed','unknown');
CREATE INDEX bookings_unknown_idx ON bookings (updated_at) WHERE status = 'unknown';

CREATE TABLE lab_calls (
    id               bigserial PRIMARY KEY,
    endpoint         text        NOT NULL,
    idempotency_key  text,
    attempt          int         NOT NULL,
    outcome          text        NOT NULL,   -- ok | definite_error | server_error | no_response | short_circuited
    http_status      int,
    latency_ms       int         NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outbound_messages (
    id               bigserial PRIMARY KEY,
    conversation_id  bigint      REFERENCES conversations(id),
    phone            text        NOT NULL,
    body             text        NOT NULL,
    dedupe_key       text        NOT NULL,
    status           text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
    attempts         int         NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz NOT NULL DEFAULT '-infinity',
    created_at       timestamptz NOT NULL DEFAULT now(),
    sent_at          timestamptz,
    CONSTRAINT outbound_messages_dedupe_key_key UNIQUE (dedupe_key)
);
CREATE INDEX outbound_pending_idx ON outbound_messages (next_attempt_at) WHERE status = 'pending';

CREATE TABLE llm_usage (
    id                  bigserial PRIMARY KEY,
    conversation_id     bigint,
    mode                text        NOT NULL CHECK (mode IN ('mock','real')),
    model               text        NOT NULL,
    input_tokens        int         NOT NULL,
    output_tokens       int         NOT NULL,
    cache_read_tokens   int         NOT NULL DEFAULT 0,
    cache_write_tokens  int         NOT NULL DEFAULT 0,
    cost_usd            numeric(14,8),          -- NULL = model price unknown (never silently 0)
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_usage_conv_idx ON llm_usage (conversation_id);

-- Real spend only. Append-only: no code path deletes from it; resets never touch it.
CREATE TABLE llm_spend_ledger (
    id                  bigserial PRIMARY KEY,
    conversation_id     bigint,
    model               text        NOT NULL,
    input_tokens        int         NOT NULL,
    output_tokens       int         NOT NULL,
    cache_read_tokens   int         NOT NULL DEFAULT 0,
    cache_write_tokens  int         NOT NULL DEFAULT 0,
    cost_usd            numeric(14,8),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION forbid_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'llm_spend_ledger is append-only';
END $$;
CREATE TRIGGER llm_spend_ledger_append_only
    BEFORE UPDATE OR DELETE ON llm_spend_ledger
    FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
CREATE TRIGGER llm_spend_ledger_no_truncate
    BEFORE TRUNCATE ON llm_spend_ledger
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_ledger_mutation();

CREATE TABLE handoffs (
    id               bigserial PRIMARY KEY,
    conversation_id  bigint      NOT NULL REFERENCES conversations(id),
    reason           text        NOT NULL,
    summary          text        NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL DEFAULT now(),
    released_at      timestamptz
);
CREATE INDEX handoffs_conv_idx ON handoffs (conversation_id);

CREATE TABLE events (
    id               bigserial PRIMARY KEY,
    kind             text        NOT NULL,
    conversation_id  bigint,
    detail           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_kind_idx ON events (kind);

CREATE TABLE worker_heartbeats (
    worker_id  text PRIMARY KEY,
    last_seen  timestamptz NOT NULL,
    is_leader  boolean     NOT NULL DEFAULT false
);
