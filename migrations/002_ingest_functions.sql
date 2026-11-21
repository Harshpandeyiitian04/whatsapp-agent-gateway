-- Hot-path logic as SQL functions so each inbound message costs exactly one round trip.

-- Token bucket. The row lock (FOR UPDATE) makes refill + take atomic. Tokens never go below 0:
-- a token is taken only when at least one is available. p_now comes from the app's injectable
-- clock; greatest(0, elapsed) guards against clock skew going backwards.
CREATE OR REPLACE FUNCTION rate_limit_take(
    p_phone text, p_now timestamptz, p_capacity double precision, p_refill_per_sec double precision
) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE
    v_tokens double precision;
    v_allowed boolean;
BEGIN
    INSERT INTO rate_limits (phone, tokens, updated_at) VALUES (p_phone, p_capacity, p_now)
    ON CONFLICT (phone) DO NOTHING;

    SELECT least(p_capacity,
                 tokens + greatest(0, extract(epoch FROM (p_now - updated_at))) * p_refill_per_sec)
      INTO v_tokens
      FROM rate_limits WHERE phone = p_phone
       FOR UPDATE;

    v_allowed := v_tokens >= 1;
    UPDATE rate_limits
       SET tokens = CASE WHEN v_allowed THEN v_tokens - 1 ELSE v_tokens END,
           updated_at = greatest(updated_at, p_now)
     WHERE phone = p_phone;
    RETURN v_allowed;
END $$;

-- Dedupe + rate limit + enqueue for one inbound message. Called inside the webhook transaction.
-- Outcomes: duplicate | queued | rate_limited | held (conversation is with a human).
CREATE OR REPLACE FUNCTION ingest_message(
    p_wa_message_id text, p_phone text, p_msg_type text, p_body text, p_wa_ts timestamptz,
    p_now timestamptz, p_capacity double precision, p_refill_per_sec double precision,
    p_notice_window_s double precision, p_notice_body text
) RETURNS TABLE (outcome text, conversation_id bigint, message_id bigint)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
    v_conv bigint;
    v_state text;
    v_msg bigint;
BEGIN
    SELECT c.id, c.state INTO v_conv, v_state FROM conversations c WHERE c.phone = p_phone;
    IF v_conv IS NULL THEN
        INSERT INTO conversations (phone, created_at, updated_at) VALUES (p_phone, p_now, p_now)
        ON CONFLICT (phone) DO NOTHING;
        SELECT c.id, c.state INTO v_conv, v_state FROM conversations c WHERE c.phone = p_phone;
    END IF;

    INSERT INTO inbound_messages (wa_message_id, conversation_id, phone, msg_type, body, wa_timestamp,
                                  received_at)
    VALUES (p_wa_message_id, v_conv, p_phone, p_msg_type, p_body, p_wa_ts, p_now)
    ON CONFLICT (wa_message_id) DO NOTHING
    RETURNING id INTO v_msg;

    IF v_msg IS NULL THEN
        INSERT INTO duplicate_deliveries (wa_message_id, received_at) VALUES (p_wa_message_id, p_now);
        RETURN QUERY SELECT 'duplicate'::text, v_conv, NULL::bigint;
        RETURN;
    END IF;

    IF v_state = 'HANDOFF' THEN
        UPDATE inbound_messages SET status = 'held' WHERE id = v_msg;
        RETURN QUERY SELECT 'held'::text, v_conv, v_msg;
        RETURN;
    END IF;

    IF NOT rate_limit_take(p_phone, p_now, p_capacity, p_refill_per_sec) THEN
        UPDATE inbound_messages SET status = 'rate_limited' WHERE id = v_msg;
        -- One friendly notice per phone per window; further limited messages are dropped silently.
        UPDATE rate_limits SET last_notice_at = p_now
         WHERE phone = p_phone
           AND (last_notice_at IS NULL OR last_notice_at <= p_now - make_interval(secs => p_notice_window_s));
        IF FOUND THEN
            INSERT INTO outbound_messages (conversation_id, phone, body, dedupe_key, created_at)
            VALUES (v_conv, p_phone, p_notice_body, 'ratelimit:' || p_wa_message_id, p_now)
            ON CONFLICT (dedupe_key) DO NOTHING;
        END IF;
        RETURN QUERY SELECT 'rate_limited'::text, v_conv, v_msg;
        RETURN;
    END IF;

    UPDATE conversations c
       SET has_pending = true,
           pending_since = coalesce(c.pending_since, p_now),
           updated_at = p_now
     WHERE c.id = v_conv;
    RETURN QUERY SELECT 'queued'::text, v_conv, v_msg;
END $$;
