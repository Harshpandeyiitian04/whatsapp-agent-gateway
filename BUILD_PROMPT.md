# BUILD_PROMPT.md: master prompt for the WhatsApp Agent Gateway build

This is the brief I will follow for the rest of the build. If later work conflicts with it, this file wins unless the user approves a change, and I update the file when that happens.

## Changelog vs v1
- **I1:** A `failed` booking row can be re-opened (`failed → pending`, `attempt_no+1`) with a guarded UPDATE. There is never a second active row (§3.4).
- **I2:** The rate-limit SQL is now a single atomic CTE. It never goes negative, has no rollback trick, takes an injected clock, and the capacity and refill come from env (§3.7).
- **I3:** Real LLM spend is stored in the append-only `llm_spend_ledger`. The load-test `--reset` never touches it, and the budget guard and `spend_report.py` read from it (§3.8).
- **I4:** Each worker process runs `WORKER_CONCURRENCY` (default 8) claim loops, with lease heartbeats and graceful SIGTERM. The reconciler and outbox sender run on one leader picked by `pg_try_advisory_lock`. The DB pool is sized to concurrency + 4 (§3.3).
- **I5:** There are three LLM adapters: mock, Anthropic, and OpenAI-compatible. `pricing.py` has cited prices. An unknown model reports its cost as `unknown`, never 0. Cache read and write tokens are recorded (§4.6).
- **I6:** The load profile now matches the limits. Normal phones send at most 5 messages, paced. A spammer cohort sends bursts. A contention cohort of 30 or more phones targets one slot. Inventory is 7 days × 8 slots, and the report includes the expected maximum (§8).
- **I7:** The webhook does one transaction built from one CTE per message. It makes no outbound calls, keeps `synchronous_commit` on, and is measured at 50 concurrent senders. If needed, it gets profiled and the fix is documented (§3.2).
- **I8:** `/admin/*` needs a bearer `ADMIN_TOKEN`. Phones are masked in logs. Metric labels use only `conversation_id`. The body is capped at 256 KB. A bad signature gets 401 before JSON parsing (§3.1, §6).
- **I9:** Outbox pattern. Replies are inserted into `outbound_messages` in the same transaction as the state change. A sender loop delivers them idempotently by `dedupe_key`. There is a mock sender and a Graph API stub (§3.9).
- **I10:** PLAN.md gets a residual-risk section.
- **I11:** There is an injectable clock and a seeded RNG. Slots are relative to today in Asia/Kolkata and are never in the past (§5).
- **I12:** Makefile, CI workflow, and pinned dependencies (§7, §10).

---

## 0. Session rules (non-negotiable)

1. **No git commands of any kind.** At the end of each phase I write `commit now: <message>` and wait.
2. **Synthetic data only.** Names come from a fixed fake list, and phone numbers use the reserved test range `+91 99999 0XXXX`.
3. **No secrets in the repo.** Only `.env.example` holds placeholders. Code reads keys from the environment. `.gitignore` covers `.env`, `__pycache__`, `.venv`, `*.log` and `pgdata/`.
4. **LLM spend stays under ₹500 in total.**
   - `LLM_MODE=mock` is the default for everything: tests, the load script and compose.
   - `LLM_MODE=real` is used only for a small demo run of about 20–40 messages.
   - A hard budget guard, `LLM_BUDGET_USD` (default `2.00`), makes the real adapter refuse calls once the recorded real spend in Postgres reaches the cap.
   - `scripts/spend_report.py` prints the actual spend for the README.
5. **Phases run in order.** Each phase stops for review. No code is written until the plan is approved.
6. **Keep it small.** Modules are small and typed (mypy-clean, ruff-clean). Failures are handled explicitly, with no clever tricks.

---

## 1. Stack decision

| Layer | Choice |
|---|---|
| Language/API | **Python 3.12 + FastAPI + uvicorn** |
| DB driver | **psycopg 3** (async, with pool) and raw SQL. No ORM. |
| Queue | **Postgres-backed queue** (`FOR UPDATE SKIP LOCKED` + leases) |
| Rate limit store | Postgres (token bucket row per phone) |
| Metrics | `prometheus_client`. Counters derived from the DB, plus in-process histograms. |
| Migrations | Plain numbered `.sql` files and a 30-line runner (one-shot `migrate` service) |
| HTTP client | `httpx` (async) with explicit timeouts |
| Tests | `pytest` + `pytest-asyncio`, run against the compose Postgres |
| Orchestration | Docker Compose: `postgres`, `migrate`, `api`, `worker` (×2), `mocklab` |

**Why:**
- With a Postgres queue, the **dedupe insert and the enqueue happen in one transaction**. A message is never stored without being queued, and never queued twice. There is also no dual-write problem between Redis and Postgres, and one fewer service that can fail.
- FastAPI plus raw SQL keeps the hot path (the webhook) to a single short transaction, which makes <200 ms easy to hit and easy to read.
- Python has first-class LLM SDKs and makes the agent loop and mock LLM quick to write and test.
- Postgres easily handles 1M messages a month (about 0.4 msg/s on average and maybe 50 msg/s in bursts). Redis would be premature here, and COST.md will say when to add it.

---

## 2. Open questions and the assumptions I'll make

| # | Ambiguity in the brief | Assumption |
|---|---|---|
| A1 | What "acknowledge in <200 ms" is measured on | **Server-side** time from request received to response sent, recorded in a histogram (`webhook_ack_seconds`). The target is **p99 < 200 ms** on local Docker during the 1,000-message load run (50 concurrent senders). The load script also reports client-observed p50/p95/p99. |
| A2 | Which queue | A Postgres table queue with leases (visibility timeout), attempts and a `dead` status (DLQ). See §3.3. |
| A3 | LLM provider and model | A provider-agnostic `LLMClient` interface with two adapters: **mock** (deterministic, ₹0) and **real**. Default real model: **Claude Haiku 5.5 (`claude-haiku-5-5`)**, the cheapest current Anthropic tier. Prices go in a dated `pricing.py` table copied from the provider's published pricing page in Phase 5, with nothing typed from memory. **I need you to confirm which provider you have a key for.** |
| A4 | Rate-limit algorithm and limits | A **token bucket per phone number** in Postgres: capacity 6, refill 1 token every 10 s (so 6 msgs/min sustained, bursts of 6). Configurable through env. |
| A5 | What happens when a message is rate-limited | The message is still stored (for dedupe and audit) with `status='rate_limited'` and is **not** sent to the LLM. **One** friendly reply is sent per phone per 60 s window ("You're sending messages quickly, please wait a minute"). After that, extra messages are dropped silently. Counter: `rate_limited_total`. |
| A6 | Is rate limiting applied before or after dedupe? | **After** dedupe, so WhatsApp's duplicate deliveries never use up a user's quota. |
| A7 | Test catalog shape | 5 tests: `CBC`, `LIPID`, `TSH`, `HBA1C`, `VITD`, each with a name, price in ₹ and fasting flag. |
| A8 | Slot shape (v2) | One collection centre. 8 slots a day (every 30 min, 08:00–11:30 IST) for the next **7 days starting tomorrow (Asia/Kolkata)**, so 56 slots, each with **capacity 1**. Slot id: `S-YYYYMMDD-HHMM`. Slots are generated relative to the injected clock and are never in the past. |
| A9 | What counts as a "double booking" | **(a)** Two active bookings on the same `slot_id` (slot double-booked), or **(b)** two active bookings for the same `(phone, test_code)` that came from one booking intent (patient double-booked by a retry). Both must be 0. A patient may hold at most one active booking per test. |
| A10 | Handoff behaviour | The conversation moves to `HANDOFF`. The user gets a "A team member will reply shortly" message, and a `handoffs` row is written with the reason. **The bot stays silent** on that conversation (messages are still stored) until `POST /admin/conversations/{id}/release`. |
| A11 | Outbound WhatsApp replies | `WHATSAPP_MODE=mock`: replies are written to `outbound_messages` and logged, and nothing calls Meta. A real Graph API sender is a stub behind the same interface (documented, not exercised). |
| A12 | Webhook payload coverage | Only `messages[].type == "text"`. Other message types get a canned "text only please" reply. `statuses[]` callbacks (delivered/read) are acknowledged with 200 and ignored. One webhook can carry several messages, and each is handled on its own. |
| A13 | Signature failure response | Return **401** with no processing. Bad JSON with a valid signature gets **400**. Everything else returns **200** quickly, because WhatsApp retries anything that isn't 2xx. |
| A14 | Conversation identity | One conversation per phone number (`wa_id`). |
| A15 | Patient name for booking | The agent asks for it if it's missing. The mock LLM uses a synthetic name. The phone number comes from the conversation and is **never** taken from LLM arguments. |
| A16 | ₹/$ conversion | `USD_INR` env, default `88.0`. Stated as an assumption in COST.md and the README. |
| A17 | Message coalescing | When several messages from one user are pending, the worker handles them **in order in one agent turn**. This preserves order and saves LLM tokens during bursts. |
| A18 | Multiple uvicorn workers | The API runs **1 uvicorn process**, so the in-process latency histogram stays exact. I scale with more API containers only in the COST.md discussion. |

---

## 3. Correctness design

### 3.1 Signature check
- Read the **raw body bytes** before JSON parsing.
- Compute `expected = "sha256=" + hmac_sha256(WHATSAPP_APP_SECRET, raw_body).hexdigest()`.
- Compare with `hmac.compare_digest(expected, header)` (constant time). A missing or malformed header gets 401.
- `GET /webhook` handles verification: if `hub.mode == "subscribe"` and `hub.verify_token == WHATSAPP_VERIFY_TOKEN`, echo `hub.challenge`; otherwise return 403.

### 3.2 Dedupe and enqueue in one transaction
(v2: these steps are collapsed into **one CTE statement per message**. There is no outbound I/O on the request path, and `synchronous_commit` stays on.)
```sql
BEGIN;
INSERT INTO inbound_messages (wa_message_id, phone, body, wa_timestamp, raw)
VALUES (...) ON CONFLICT (wa_message_id) DO NOTHING
RETURNING id;                        -- no row returned => duplicate: count dedupe_hits, skip
-- rate limit (token bucket UPDATE on rate_limits row, see 3.7)
-- if allowed:
INSERT INTO conversations (phone) VALUES ($1) ON CONFLICT (phone) DO NOTHING;
UPDATE inbound_messages SET status='queued' WHERE id=$id;
UPDATE conversations SET has_pending = true, pending_since = coalesce(pending_since, now()) WHERE phone=$1;
COMMIT;
```
- `wa_message_id` is `UNIQUE`. Duplicates that arrive at the same moment race on the unique index, and exactly one wins.
- Duplicate hits increment the `dedupe_hits` column in `webhook_stats` (one tiny upsert), so the count survives restarts.

### 3.3 Queue: at-least-once, leases, DLQ, per-conversation ordering
The **unit of work is a conversation**, not a single message. That gives ordering for free.

Claim (each worker loops):
```sql
UPDATE conversations SET lease_owner=$worker, lease_until=now()+interval '60 seconds'
WHERE id = (
  SELECT id FROM conversations
  WHERE has_pending AND state <> 'HANDOFF'
    AND (lease_until IS NULL OR lease_until < now())
    AND next_attempt_at <= now()
  ORDER BY pending_since
  FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id;
```
- **Only one worker per conversation** can hold the lease at a time. `SKIP LOCKED` keeps workers from blocking each other.
- **Visibility timeout:** if a worker crashes, `lease_until` expires and another worker picks the conversation up. The worker heartbeats (extends the lease) every 20 s during long LLM or lab calls.
- **Processing:** the worker loads the `queued` messages for the conversation `ORDER BY wa_timestamp, id`, runs one agent turn, then in one transaction marks those messages `done`, saves state, clears `has_pending` (unless new messages arrived, checked with `pending_since` / `max(id)`) and releases the lease.
- **Failure:** on an unexpected exception, it increments `attempts` on those messages, sets `next_attempt_at = now() + backoff` and releases the lease. When `attempts >= 5`, the messages move to `status='dead'` (the **DLQ**), the conversation is handed off with reason `processing_failure`, and `dlq_total` increments. `POST /admin/dlq/{id}/requeue` re-drives a dead message.
- **Idempotent side effects:** because delivery is at-least-once, every side effect is idempotent. Outbound replies have a unique `(inbound_batch_key)`, and bookings rely on idempotency keys (3.4).

### 3.4 Booking idempotency and no double booking
**Idempotency key:** `sha256(phone | test_code | slot_id)`. It is deterministic, so any replay of the same intent (worker retry, LLM repeating a tool call, a duplicate webhook that slipped past) produces the same key.

**Local guards (Postgres `bookings` table):**
- `UNIQUE (idempotency_key)`
- `UNIQUE (slot_id) WHERE status IN ('pending','confirmed','unknown')`, a partial index that prevents a slot double booking
- `UNIQUE (phone, test_code) WHERE status IN ('pending','confirmed','unknown')`, which prevents a patient double booking

**`book_test` flow:**
1. `INSERT INTO bookings (..., status='pending') ON CONFLICT DO NOTHING`.
   - If it conflicts on the idempotency key, that's a replay of this intent. Return the existing booking's status and don't call the lab again if it's `confirmed`.
   - If it conflicts on the slot or the patient, return `slot_taken` / `already_booked` to the LLM so it can offer other slots.
2. `POST /bookings` to the lab with header `Idempotency-Key: <key>` and a 3 s client timeout, wrapped in retry plus circuit breaker (3.5).
3. Map the outcome:
   - **2xx:** `confirmed`, and store `lab_booking_id`.
   - **409 slot taken (definite):** `failed`, the local slot claim is released, and the LLM is told `slot_taken`.
   - **4xx other (definite):** `failed`.
   - **Ambiguous** (timeout, connection reset, 5xx: the request *may* have taken effect): mark `unknown`, then **reconcile before any re-POST**. Call `GET /bookings?idempotency_key=<key>`. If found, the booking is `confirmed`. If not found, re-POST with the same key. That's safe either way: if the lab honours idempotency it's a no-op, and if it doesn't, we already proved there was no booking.
   - If retries run out while the status is still `unknown`, the booking stays `unknown` (the slot stays claimed locally, so it's safe). The user gets "We're confirming your booking and will message you shortly", and a **reconciler loop** in the worker re-checks `unknown` bookings every 30 s with the same GET. After 10 minutes still unknown, it hands off with reason `booking_unconfirmed`.
4. The status changes are single-row `UPDATE ... WHERE idempotency_key=$k AND status IN (...)`, a guarded transition, so a stale worker can't move a confirmed booking backwards.
5. **Re-attempt after `failed` (I1).** If the insert conflicts on the idempotency key and the existing row is `failed`, run `UPDATE bookings SET status='pending', attempt_no=attempt_no+1 WHERE idempotency_key=$k AND status='failed'`.
   - The partial unique indexes on slot and patient still apply, so if someone else holds the slot now, the UPDATE raises a unique violation, which maps to `slot_taken`.
   - The lab POST uses `Idempotency-Key = key:attempt_no`. Once the lab has definitively rejected an attempt, the next attempt is a new request to the lab. Reconciliation always looks up the current attempt's key.
6. **Ambiguous wait (I10).** Before re-POSTing after an ambiguous result, wait at least `LAB_TIMEOUT + 0.5 s` from the original send, so a request still in flight has landed before we reconcile.

### 3.5 Retries and circuit breaker
- **Backoff:** exponential with full jitter, `sleep = random(0, min(cap, base * 2**attempt))`, with `base = 0.2 s`, `cap = 3 s` and **max 4 attempts** per lab call. Retries apply only to ambiguous or 5xx failures, never to definite 4xx.
- **Circuit breaker:** one per worker process, around the lab client. It **opens after 5 consecutive failures**, stays open 30 s, then goes half-open and allows 1 trial call. While open, calls fail immediately with `LabUnavailable`. The agent then hands off with reason `lab_outage` (or for `check_slots`, tells the user "booking system is busy, a team member will follow up").
  - With independent 20% failures, the chance of 5 in a row is about 0.03%, so the breaker trips on real outages, not on normal flakiness.
- Every attempt is logged to `lab_calls` (endpoint, attempt, outcome, latency_ms, idempotency_key). The retry and breaker metrics come from this table.

### 3.6 Worker-level retries vs call-level retries
- **Call-level:** the lab retries in 3.5.
- **Job-level:** the conversation reschedule in 3.3, for crashes, DB errors and LLM errors.

They don't multiply unsafely, because every side effect is idempotent.

### 3.7 Rate limit (token bucket in Postgres, v2)
`$now` is passed in from the injectable clock, `$cap` is `RATE_LIMIT_CAPACITY` and `$rate` is `RATE_LIMIT_REFILL_PER_SEC`.
```sql
INSERT INTO rate_limits AS r (phone, tokens, updated_at)
VALUES ($phone, $cap - 1, $now)              -- first message: allowed, one token used
ON CONFLICT (phone) DO UPDATE SET
  tokens = CASE
    WHEN least($cap, r.tokens + greatest(0, extract(epoch FROM $now - r.updated_at)) * $rate) >= 1
    THEN least($cap, r.tokens + greatest(0, extract(epoch FROM $now - r.updated_at)) * $rate) - 1
    ELSE least($cap, r.tokens + greatest(0, extract(epoch FROM $now - r.updated_at)) * $rate)
  END,
  updated_at = greatest(r.updated_at, $now)
RETURNING (xmax = 0) AS inserted, tokens, ...;
```
- The ON CONFLICT path takes a row lock, so the read-modify-write is atomic.
- The allowed or denied decision comes from comparing the old refilled value. The statement returns `allowed` through a CTE that computes the refilled value once.
- Tokens are never below 0, `greatest(0, …)` guards against clock skew, and nothing depends on a rollback.
- **Notice once per window:** `UPDATE rate_limits SET last_notice_at=$now WHERE phone=$p AND (last_notice_at IS NULL OR last_notice_at < $now - window) RETURNING 1`. One row returned means we queue one friendly outbound reply.

### 3.8 Spend ledger (survives resets)
- `llm_usage` holds every call, mock and real, for metrics. `--reset` deletes it.
- `llm_spend_ledger` is append-only and holds **only `mode='real'`** calls. Nothing in the app or scripts ever truncates it.
- The budget guard runs `SELECT coalesce(sum(cost_usd),0) FROM llm_spend_ledger` before each real call, and refuses once the sum reaches `LLM_BUDGET_USD`.

### 3.9 Outbox
- Agent replies, rate-limit notices and handoff messages are inserted into `outbound_messages` (`dedupe_key UNIQUE`, `status='pending'`) **in the same transaction** that saves state and marks inbound rows `done`.
- The sender loop (leader only) claims pending rows with `SKIP LOCKED`, calls the `Sender` (mock: logs and marks `sent`; Graph stub: builds the request), then marks `sent`. Failures go back with backoff. A redelivery can't send twice because of the dedupe key.
- `dedupe_key = sha256(conversation_id | max inbound id handled | reply index)`.

---

## 4. Agent and tool design

### 4.1 Tools (JSON Schema, provider-neutral)
```json
{
  "name": "check_slots",
  "description": "List available collection slots for a lab test. Returns at most 5 slots.",
  "input_schema": {
    "type": "object",
    "properties": {
      "test_code": {"type": "string", "enum": ["CBC","LIPID","TSH","HBA1C","VITD"]},
      "date": {"type": "string", "format": "date", "description": "YYYY-MM-DD; omit for the next available days"},
      "part_of_day": {"type": "string", "enum": ["early","late","any"], "default": "any"}
    },
    "required": ["test_code"],
    "additionalProperties": false
  }
}
```
```json
{
  "name": "book_test",
  "description": "Book a slot the user has explicitly confirmed. Only use a slot_id returned by check_slots in this conversation.",
  "input_schema": {
    "type": "object",
    "properties": {
      "test_code": {"type": "string", "enum": ["CBC","LIPID","TSH","HBA1C","VITD"]},
      "slot_id": {"type": "string", "pattern": "^S-\\d{8}-\\d{4}$"},
      "patient_name": {"type": "string", "minLength": 2, "maxLength": 80}
    },
    "required": ["test_code","slot_id","patient_name"],
    "additionalProperties": false
  }
}
```
```json
{
  "name": "handoff_to_human",
  "description": "Transfer the conversation to lab staff. Use for medical questions, complaints or angry users, or when booking keeps failing.",
  "input_schema": {
    "type": "object",
    "properties": {
      "reason": {"type": "string", "enum": ["medical_question","angry_user","booking_failed","lab_outage","user_request","other"]},
      "summary": {"type": "string", "maxLength": 300}
    },
    "required": ["reason","summary"],
    "additionalProperties": false
  }
}
```

### 4.6 LLM adapters (I5)
- **`mock`:** deterministic.
- **`anthropic`:** uses the `anthropic` SDK, `LLM_MODEL` (default `claude-haiku-5-5`) and `ANTHROPIC_API_KEY`.
- **`openai`:** OpenAI-compatible chat completions over raw httpx, using `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY`.
- All three return a normalised `LLMResponse(text, tool_calls, usage{input, output, cache_read, cache_write})`.
- `pricing.py` maps model id to $/MTok for input, output, cache_read and cache_write, with the source URL and fetch date. An unknown model gives `cost_usd = NULL`, and metrics report it as `llm_cost_unknown_calls_total`.

**Tool results** are compact JSON, for example `{"ok":true,"slots":[{"slot_id":"S-20261121-0830","time":"Sat 08:30"}]}` or `{"ok":false,"error":"slot_taken"}`.

**Server-side validation** of every tool call:
- Arguments are checked against the schema.
- `book_test.slot_id` must appear in `context.offered_slots`.
- The phone number comes from the conversation, never from the model.
- Invalid arguments come back to the model as `{"ok":false,"error":"invalid_args",...}` and count toward the loop guard.

### 4.2 System prompt (kept short to save tokens)
```
You are the WhatsApp booking assistant for {LAB_NAME}, a diagnostic lab.
You can: list tests, check slots, and book a test collection slot.
Tests: CBC, LIPID (fasting 10-12h), TSH, HBA1C, VITD.
Rules:
- Be brief (max 2 short sentences), friendly, plain English.
- Always call check_slots before offering times. Never invent slots.
- Only call book_test after the user confirms a specific slot and gives their name.
- Never give medical advice or interpret results: call handoff_to_human(reason="medical_question").
- If the user is upset, asks for a person, or booking fails repeatedly: call handoff_to_human.
Today is {DATE}. Conversation state: {STATE}. Known: {CONTEXT_JSON}
```
- History is trimmed to the **last 8 turns** plus the state and context summary, which caps input tokens.
- Tool definitions are sent once per call. Prompt caching is evaluated in COST.md.

### 4.3 Conversation state machine
```
NEW ──first msg──▶ COLLECTING ──check_slots ok──▶ SLOTS_OFFERED ──book_test──▶ BOOKING_PENDING
                      ▲                                │                         │   │
                      │                    slot_taken / new date                 │   └─ unknown ─▶ (reconciler)
                      └────────────────────────────────┘                        ok│
                                                                                  ▼
                                                                               BOOKED ──new request──▶ COLLECTING
ANY ──handoff_to_human / auto-handoff──▶ HANDOFF ──admin release──▶ COLLECTING
```
`conversations.context` (jsonb) holds `test_code`, `patient_name`, `offered_slots[]`, `failed_booking_attempts` and `last_booking_key`.

### 4.4 Guards and automatic handoff
- **Max tool loop:** at most **4 LLM calls per turn**. If the limit is exceeded, the agent sends a fallback reply and hands off with reason `other` ("agent_loop_limit").
- **Automatic handoffs**, enforced in code and not left to the LLM:
  - `failed_booking_attempts >= 3` → `booking_failed`
  - Circuit open or lab unavailable after retries → `lab_outage`
  - LLM provider error after job retries → DLQ path → `processing_failure`
  - Budget guard hit in real mode → `other` ("llm_budget")
- **Handoffs decided by the model:** medical questions, angry users and explicit requests for a person.
  - The mock LLM detects these by keyword: `pain|fever|result|report|dose|medicine|pregnan` for medical, `angry|worst|useless|complaint|refund|!!!` for anger, and `human|agent|person|call me` for a request for a person.

### 4.5 Mock LLM (`LLM_MODE=mock`)
- A deterministic rule-based adapter that returns the **same response structure** as the real adapter: text blocks, tool_use blocks and usage.
- Intent rules:
  - Booking keywords and a test name lead to `check_slots`.
  - A slot reference ("1", "first", "8:30") or "yes" leads to `book_test`.
  - Handoff keywords lead to `handoff_to_human`.
  - Anything else gets a greeting or menu.
- Token usage is estimated as `ceil(chars/4)` for input and output. Cost is computed with the *real* model's prices (and labelled `model="mock"`), so the load test shows what the run *would* cost.

---

## 5. Mock lab API spec (`mocklab` service, FastAPI, in-memory)

| Endpoint | Behaviour |
|---|---|
| `GET /tests` | The catalog |
| `GET /slots?test_code&date&part_of_day` | Available slots |
| `POST /bookings` (header `Idempotency-Key`) body `{test_code, slot_id, patient_name, phone}` | 201 `{booking_id,...}` or 409 `slot_taken` |
| `GET /bookings?idempotency_key=` | 200 `[booking]` or `[]` (used for reconciliation) |
| `GET /admin/double-bookings` | `{slot_violations:[...], patient_violations:[...], total_bookings:n}` |
| `POST /admin/chaos` `{failure_rate, latency_min_ms, latency_max_ms}` | Change failure behaviour at runtime (for example, a 100% outage to show the breaker) |
| `POST /admin/reset` | Reset slots and bookings (for tests and load runs) |
| `GET /health` | Liveness |

- **Latency:** uniform **300–1500 ms** on every call.
- **Failure: 20% of calls in total**, split as follows:
  - 8%: **500 before effect** (nothing written)
  - 6%: **timeout before effect** (sleeps 5 s, longer than the client's 3 s timeout, and writes nothing)
  - 6%: **succeeded but response lost**. For POST /bookings, the booking is committed, then the lab returns 500 or hangs past the timeout. For GETs, a plain 500.
- **Deliberately naive by default:** `LAB_ENFORCE_IDEMPOTENCY=false` and `LAB_ENFORCE_CAPACITY=false`. The lab writes whatever it's told, so **the gateway alone is responsible for preventing double bookings**. The detector endpoint then proves the gateway did its job. Both flags can be turned on to show the defence in depth.
- Random numbers come from a seeded `random.Random(LAB_SEED)`, so runs are reproducible.

---

## 6. Observability: `GET /metrics` (Prometheus text format, on the API)

**In-process metrics (API):**
- `webhook_ack_seconds` histogram (buckets 5 ms–1 s)
- `webhook_requests_total{result="ok|bad_signature|bad_payload"}`

**DB-derived metrics (computed at scrape time with a few indexed aggregate queries).** These are correct across all worker replicas and survive restarts:
- `messages_received_total`, `dedupe_hits_total`, `rate_limited_total`
- `queue_depth`: count of queued messages
- `queue_oldest_age_seconds`, `dlq_total`
- `lab_calls_total{endpoint,outcome}`, `lab_retries_total`, `circuit_open_total`
- `bookings_total{status}`, `handoffs_total{reason}`
- `llm_tokens_total{direction="in|out",model}`, `llm_cost_usd_total{model}`, `llm_cost_inr_total{model}`
- `conversation_llm_tokens{conversation_id,direction}`, `conversation_llm_cost_usd{conversation_id}` and `conversation_llm_cost_inr{conversation_id}`
  - **These are capped to the 200 most recently active conversations** to bound cardinality. This trade-off is documented.
  - The full per-conversation breakdown is at `GET /metrics/conversations` (JSON).

**Other:**
- `GET /health` checks the DB and returns `{status, db}`. The worker's liveness comes from the lease heartbeat and is visible as `worker_last_heartbeat_seconds` in the DB-derived metrics.
- Structured JSON logs include `conversation_id`, `wa_message_id` and `idempotency_key` wherever they're relevant.

---

## 7. Test plan

**Unit tests** (`tests/unit`, no network):
- `test_signature.py`: valid, tampered body, wrong secret, missing header, and malformed prefix are all handled. Uses constant-time compare.
- `test_payload.py`: parses the WhatsApp Cloud API sample, handles multiple messages, skips statuses, handles non-text messages.
- `test_backoff.py`: delays stay within `[0, cap]`, max attempts are respected, no retry on 4xx.
- `test_circuit.py`: closed → open after N failures → half-open → closed or open again.
- `test_mock_llm.py`: intents map to the expected tool calls, and token estimates are deterministic.
- `test_tool_validation.py`: rejects a slot not offered and bad enums. The phone number is never taken from arguments.

**DB tests** (`tests/db`, against compose Postgres, each test in its own schema or truncated):
- `test_dedupe.py`: the same `wa_message_id` inserted twice, including 20 concurrent tasks, results in 1 row, 1 queued and the rest counted as dedupe hits.
- `test_ratelimit.py`: the bucket allows 6, then limits, then refills after time passes (time is injected).
- `test_booking_idempotency.py`: replaying the same key leads to one lab POST. Two conversations racing for one slot produce exactly one active booking. The ambiguous path (lab commits, then times out) reconciles to `confirmed` with one lab booking.
- `test_queue.py`: two workers never hold the same conversation. An expired lease is reclaimed. Five failures move the messages to the DLQ.

**Integration test** (`tests/integration/test_failure_injection.py`):
- Brings the stack up with `LAB_FAILURE_RATE=0.4` and sends 100 signed messages (including duplicates) from 10 phones.
- Waits for the queue to drain, then asserts zero double bookings (lab detector and DB), every unique message ends in `done` or `rate_limited` or the DLQ, and `lab_retries_total > 0`.
- Then sets chaos to a 100% outage and asserts that the circuit opens and handoffs with reason `lab_outage` are created.

**Commands:**
```bash
docker compose up -d --build
docker compose run --rm tests pytest tests/unit -q
docker compose run --rm tests pytest tests/db -q
docker compose run --rm tests pytest tests/integration -q
docker compose run --rm tests pytest -q           # everything
docker compose run --rm tests ruff check . && docker compose run --rm tests mypy app mocklab
```

---

## 8. Load script plan (`scripts/load_test.py`, `LLM_MODE=mock`)

> **v2 profile (I6) replaces the cohort details below where they conflict:**
> - Normal phones send ≤5 messages, paced ≥10 s apart per phone, so they stay within the bucket.
> - A spammer cohort sends bursts of 20 in under 1 s each.
> - A contention cohort of 40 phones all ask for the same slot (the first slot tomorrow). Exactly one may win.
> - The report computes the expected maximum confirmed bookings as `min(56 slots, distinct booking-intent phones)`, broken down per cohort.

- **Composition: 1,000 webhook messages in total.**
  - About 850 unique messages and about 150 (15%) duplicates.
  - Duplicates are re-sent with the identical body and signature. About half are sent concurrently with the original and about half are delayed 1–5 s, which mimics WhatsApp's retry.
- **Phones:**
  - 120 synthetic phones.
  - Most send 3–8 messages following realistic scripts (greeting, ask for a test, pick a slot, confirm with a name).
  - 5 "spammer" phones send 40 messages each in bursts, to trigger the rate limit.
  - A few scripts contain medical or angry phrases, to trigger handoffs.
- **Bursts:** messages go out in waves of 50–100 at once via `asyncio` + `httpx`, with 50 concurrent connections. About 5% of webhooks batch 2–3 messages in one payload.
- **Extras, counted separately and not part of the 1,000:**
  - 10 bad-signature requests (expect 401)
  - 10 `statuses` callbacks
- **Run sequence:**
  1. `POST /admin/reset` on the lab and truncate the gateway tables (`--reset`).
  2. Send everything.
  3. Poll `/metrics` until `queue_depth == 0` and no unknown bookings remain, with a timeout.
  4. Collect results from `/metrics`, `/metrics/conversations`, the lab `/admin/double-bookings` and a DB summary query.
- **`LOAD_REPORT.md` contains:**
  - Counts: sent, unique processed, duplicates dropped, rate-limited, bookings created (confirmed/failed/unknown), **double bookings (slot and patient, must be 0)**, lab calls, retries, circuit opens, handoffs by reason, DLQ count
  - Webhook ack latency p50/p95/p99, both client-observed and from the server histogram
  - Total tokens in/out and the cost in $ and ₹ (labelled as mock-estimated)
  - Wall-clock time and the run parameters (seed, failure rate)
- **Every number comes from the real run.** The report file is generated by the script, never hand-edited.

---

## 9. Data model (draft; finalised in PLAN.md)

| Table | Contents |
|---|---|
| `conversations` | id PK, phone UNIQUE, state, context jsonb, has_pending, pending_since, next_attempt_at, lease_owner, lease_until, created_at, updated_at. Index `(has_pending, next_attempt_at)`. |
| `inbound_messages` | id PK, wa_message_id **UNIQUE**, conversation_id FK, phone, body, wa_timestamp, status (`queued/done/rate_limited/dead`), attempts, raw jsonb, received_at. Index `(conversation_id, status)`. |
| `outbound_messages` | id PK, conversation_id, body, dedupe_key UNIQUE, created_at |
| `bookings` | id PK, idempotency_key **UNIQUE**, conversation_id, phone, test_code, slot_id, patient_name, status, lab_booking_id, attempts, created_at, updated_at, plus the **partial unique** indexes from §3.4 |
| `lab_calls` | id, endpoint, idempotency_key, attempt, outcome, http_status, latency_ms, created_at |
| `llm_usage` | id, conversation_id, model, mode, input_tokens, output_tokens, cost_usd, created_at. Index `(conversation_id)`. |
| `handoffs` | id, conversation_id, reason, summary, created_at, released_at |
| `rate_limits` | phone PK, tokens, updated_at, last_notice_at |
| `webhook_stats` | key PK, value bigint (dedupe_hits, bad_signatures) |

---

## 10. Repo layout
```
.
├── app/
│   ├── config.py            # pydantic-settings, all env vars
│   ├── db.py                # pool, tx helper
│   ├── api/                 # main.py, webhook.py, metrics.py, admin.py, health.py
│   ├── whatsapp/            # signature.py, payload.py (pydantic models), outbound.py
│   ├── ingest/              # dedupe+ratelimit+enqueue (one tx) – ratelimit.py, enqueue.py
│   ├── worker/              # main.py (claim loop, heartbeat), processor.py, reconciler.py
│   ├── agent/               # loop.py, tools.py, prompts.py, state.py
│   │   └── llm/             # base.py, mock.py, anthropic_client.py, pricing.py, budget.py
│   └── lab/                 # client.py, retry.py, circuit.py, booking.py
├── mocklab/                 # main.py, chaos.py, store.py
├── migrations/              # 001_init.sql, ... + migrate.py
├── scripts/                 # load_test.py, demo_real_llm.py, spend_report.py
├── tests/                   # unit/, db/, integration/, conftest.py
├── docker-compose.yml  Dockerfile  pyproject.toml  Makefile
├── .env.example  .gitignore
├── BUILD_PROMPT.md  PLAN.md  COST.md  LOAD_REPORT.md  README.md  AI_USAGE.md
```

---

## 11. Phase checklist (each phase ends with: verify commands, a suggested commit message, STOP)

| # | Phase | Verification |
|---|---|---|
| 0 | BUILD_PROMPT.md | You review and approve it |
| 1 | PLAN.md (problem, users, architecture diagram, data model, API contract, failure-mode table, cost estimate) | You review it. **First commit.** |
| 2 | Compose skeleton, Dockerfile, migrations, config, `.env.example`, `.gitignore` | `docker compose up -d --build` → `docker compose ps` shows all services healthy. `docker compose exec postgres psql -U app -c '\dt'` lists the tables. |
| 3 | Webhook (GET verify, POST signature check, dedupe, rate-limit hook, enqueue) | `curl` the verify challenge. A signed sample returns 200. A tampered one returns 401. Sending the same id twice leaves 1 row. A quick `hey`/script check shows p99 < 200 ms. |
| 4 | Mock lab | `curl` slots and bookings. `/admin/chaos` changes behaviour. The detector runs. A loop of 100 calls shows about 20% failures. |
| 5 | Worker, agent loop, tools, mock LLM, then the real LLM adapter | A signed "book CBC tomorrow" conversation ends `BOOKED` in mock mode. Then **one** real-LLM conversation runs, with the spend printed by `spend_report.py`. |
| 6 | Rate limiting and metrics | A burst of 10 from one phone gives 6 accepted, 4 limited and 1 notice. `curl /metrics` shows all the series in §6. |
| 7 | Tests | All the commands in §7 pass. Ruff and mypy are clean. |
| 8 | Load script and LOAD_REPORT.md | `docker compose run --rm loadtest` generates the report with 0 double bookings. |
| 9 | COST.md | Prices are cited with the source URL and date. The arithmetic is shown. It includes a cheaper-model and caching alternative. |
| 10 | README.md, AI_USAGE.md, video script outline | A fresh clone with `cp .env.example .env && docker compose up` works by following the README word for word. |
| 11 | Free-tier deployment notes (Render, Fly.io or Railway) | The notes are reviewed. Optionally, a live link. |
