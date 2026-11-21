# WhatsApp Agent Gateway

A lab receives booking requests on WhatsApp. Webhooks arrive in bursts, and sometimes twice. The lab's booking API is slow and flaky. **Nobody may be double-booked.**

This repo is a gateway that handles those requests:
- acknowledges webhooks in milliseconds
- processes each message once, in order per user
- runs an LLM agent with `check_slots`, `book_test` and `handoff_to_human` tools
- books against a deliberately hostile mock lab without ever double-booking
- tracks tokens and cost per conversation at `/metrics`

**Results from the measured 1,000-message load test** ([LOAD_REPORT.md](LOAD_REPORT.md)):

| Metric | Result |
|---|---|
| Duplicates dropped | 150 / 150 |
| Rate-limited | 140, all from the spammer cohort |
| Bookings | 49 confirmed, against a computed maximum of 49 |
| Contested-slot winners | 1 of 40 racing phones |
| **Double bookings** | **0**, in both the gateway DB and the lab's own detector |
| Webhook ack p99 | **12.9 ms** client-side, 9.6 ms server-side |
| Lab retries | 44, against a 20% failure rate |

Docs: [PLAN.md](PLAN.md) (architecture, data model, API contract, failure modes) · [COST.md](COST.md) (1M messages a month) · [LOAD_REPORT.md](LOAD_REPORT.md) · [docs/REAL_LLM_DEMO.md](docs/REAL_LLM_DEMO.md) · [AI_USAGE.md](AI_USAGE.md) · [DEPLOY.md](DEPLOY.md) · [VIDEO_SCRIPT.md](VIDEO_SCRIPT.md) · [BUILD_PROMPT.md](BUILD_PROMPT.md) (the build spec)

## Run it

Requires Docker with Compose v2. Nothing else needs to be installed on the host.

```bash
cp .env.example .env
docker compose up            # or: make up   (detached, waits for health checks)
```

| Service | Address |
|---|---|
| API | http://localhost:8000 (`/webhook`, `/metrics`, `/metrics/conversations`, `/health`, `/admin/*`) |
| Mock lab | http://localhost:8100 |
| Postgres | localhost:5455 |

Host ports are set in `.env`. `LLM_MODE=mock` is the default, so running everything costs ₹0.

Send a signed WhatsApp-format message:

```bash
docker compose run --rm tests python -m scripts.wa --api http://api:8000 --phone 919999900001 "Hi, I want a CBC test tomorrow"
docker compose run --rm tests python -m scripts.wa --api http://api:8000 --phone 919999900001 "Slot 2, my name is Asha Rao"
docker compose exec postgres psql -U app -d gateway -c "select body from outbound_messages order by id"
```

| Task | Command |
|---|---|
| Unit + DB tests (76) | `make test` |
| End-to-end failure injection against the running stack | `make test-integration` |
| ruff + ruff format + mypy --strict | `make lint` |
| 1,000-message load test → `LOAD_REPORT.md` | `make load` |
| Scripted video walkthrough (mock) | `make demo` |
| Live LLM smoke check (≤3 calls) | `make smoke-real` |
| Real-LLM demo (7 scenarios, ≤40 msgs) | `make demo-real` → `docs/REAL_LLM_DEMO.md` |
| Actual real-LLM spend | `make spend` |
| Stop and wipe | `make down` |

## Using a real LLM (Gemini)

1. Put your key in `.env`: `API_KEY=...`. `LLM_API_KEY` is an accepted alias.
   - `.env` is listed in both `.gitignore` and `.dockerignore`.
   - Compose passes the key **only to the worker container**. Every other service gets it blanked.
   - It is never baked into an image.
   - Don't paste `docker compose config` output anywhere: it prints interpolated values.
2. `.env.example` already selects the model:
   - `LLM_PROVIDER=gemini`
   - `LLM_MODEL=gemini-3.1-flash-lite`
   - `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`

   `LLM_MODE` stays `mock` by default.
3. `make smoke-real` makes ≤3 live calls: a plain completion, then a `check_slots` tool round trip.
4. `make demo-real` switches the workers to `LLM_MODE=real` and runs the 7 synthetic scenarios against the flaky lab. It writes `docs/REAL_LLM_DEMO.md`, then switches the workers back to mock.
5. `make spend` shows the billed spend and the paid-tier-equivalent spend.

**The 1,000-message load test always runs in mock mode.**

**Model choice (2026-11-21):**
- The docs (https://aistudio.google.com/docs/models, /pricing) list `gemini-2.5-flash-lite` as the cheapest Flash-class model at $0.10/$0.40. Live, it returned **404 "no longer available to new users"**.
- So the default is `gemini-3.1-flash-lite`, a stable model at $0.25/$1.50. It has a free tier, where content is used to improve Google's products. That's acceptable only because every demo conversation is synthetic.

**Gemini specifics, verified live:**
- Bearer auth works.
- The full tool JSON schemas are accepted (`pattern`, `format`, `additionalProperties`, `minLength`). A schema-simplifier exists but is off.
- Tool-call ids round-trip.
- Multi-turn tool calls work when the provider's assistant message is replayed verbatim. Gemini 3 requires its thought signatures back.
- `reasoning_effort: "minimal"` is the lowest thinking level on Gemini 3.

**Rate limits.** 429, 408 and 5xx responses are retried with jittered backoff that honours `Retry-After`. If the provider keeps failing, the turn fails and goes through the existing job retry, then DLQ plus handoff, so no message is dropped. The demo also paces sends at least 4 s apart.

## Architecture

```
WhatsApp ──POST /webhook──▶ api ──1 tx: ingest_message() per message──▶ Postgres
   (HMAC-SHA256 on raw body, 401 before parsing; 200 for duplicates)       │  conversations (= queue, leases)
                                                                            │  inbound_messages UNIQUE(wa_message_id)
            worker ×2 (8 claim loops each) ◀──FOR UPDATE SKIP LOCKED─────────┤  bookings (partial UNIQUE indexes)
              agent loop ──▶ LLM (mock │ Anthropic │ OpenAI-compatible)      │  outbound_messages (outbox)
              tools ──retry+jitter, circuit breaker, Idempotency-Key──▶ mock lab (300–1500 ms, 20% failures)
            leader (advisory lock): outbox sender · reconciler · stranded sweep
```

**Ingest.** The webhook does no LLM, lab or outbound HTTP calls. Each message costs one round trip to the `ingest_message()` SQL function:
1. dedupe on `ON CONFLICT (wa_message_id)`;
2. token bucket per phone, checked *after* dedupe so retried deliveries don't use up quota;
3. one "slow down" notice per minute, written to the outbox;
4. mark the conversation pending.

**Queue.** The unit of work is the **conversation**:
- A worker leases it with `SKIP LOCKED`. The 60 s lease acts as the visibility timeout, and the worker heartbeats every 20 s.
- The worker processes every queued message for that user in order, coalescing them into one agent turn.
- It commits replies, state and "done" in a single transaction **guarded by the lease token**. A worker that stalled and lost its lease can never commit stale results.
- After 5 failures the messages go to the DLQ and the conversation is handed off.

**Booking.** Correctness here rests on Postgres constraints, not on code paths:
- **Postgres constraints:**
  - `UNIQUE(idempotency_key)`, where the key is `sha256(phone|test|slot)`;
  - a partial `UNIQUE(slot_id)` over active bookings;
  - a partial `UNIQUE(phone, test_code)` over active bookings.
  "Active" includes `unknown`, so a booking we can't disprove keeps its slot held.
- **Slot claimed first:** the slot is claimed locally before the lab is called.
- **Ambiguous lab failures:** a 5xx or a timeout is **reconciled by looking the booking up by key before any re-POST**. After a timeout, the lookup waits out the in-flight window first.
- **Unresolved bookings:** if nothing can be proven, the booking stays `unknown` and a leader-only reconciler finishes it later.
- **Rebooking after a failure:** a definite failure can be re-opened (`failed → pending`, `attempt_no+1`) through a guarded UPDATE.

**Resilience.** Lab calls use exponential backoff with full jitter (4 attempts, base 0.2 s, cap 3 s). Each worker process has a circuit breaker that opens after 5 consecutive failures, for 30 s. The agent hands off on `lab_outage`.

**Agent.** The three tools have strict JSON schemas, and the server re-validates every call:
- the slot must have been offered in this conversation;
- the phone number never comes from tool arguments.

Turns are capped at 4 LLM calls. Code forces a handoff after 3 failed bookings, on a lab outage, on hitting the loop limit, on the budget limit, or on a dead-lettered message. The model decides on medical questions, angry users and requests for a person. See PLAN.md §6 and `app/agent/`.

**Observability.**
- `/metrics` is Prometheus text: webhook latency histogram plus exact recent p50/p95/p99, dedupe hits, rate-limit hits, queue depth and oldest age, DLQ, lab calls by outcome, retries, bookings, `double_bookings`, handoffs, tokens and ₹/$ cost by model, and **per-conversation tokens and cost**.
- DB-derived series are correct across all worker replicas and survive restarts.
- Per-conversation series use only `conversation_id` labels, capped at 200. The full list is at `/metrics/conversations`.
- Logs are JSON with masked phone numbers (`+91 99999 0****`).

## Design decisions

| Decision | Why |
|---|---|
| Postgres as the queue (no Redis) | Dedupe and enqueue become one atomic transaction, with no dual-write window. 1M messages a month is about 0.4 msg/s. COST.md §3 says when to change this. |
| Conversation as the unit of work | Per-user ordering comes for free, bursts coalesce into one LLM turn (cheaper), and there is one lease to reason about. |
| Constraints over code | Double booking is prevented by unique indexes. Code bugs can cause failed bookings, but not duplicates. |
| Naive mock lab by default | `LAB_ENFORCE_IDEMPOTENCY=false` and `LAB_ENFORCE_CAPACITY=false`, so the gateway alone has to prevent duplicates. The lab's detector then checks that it did. |
| Transactional outbox | A reply is stored atomically with the state change and sent once by `dedupe_key`. |
| Append-only spend ledger with a DB trigger | Real LLM spend can't be truncated by test resets. The budget guard reads from it. |
| Unknown model price → `NULL` cost, not 0, and real mode refuses to start | A missing price can't silently bypass the budget guard. |

## Known limitations

- **Live-tested provider.** Only the Gemini path (OpenAI-compatible adapter) has been run live. The Anthropic adapter is unit-tested with fakes only.
- **Rate limits not exercised live.** The provider retry path (429 with `Retry-After`) is verified by unit tests only. No 429 was observed in the demo, and the worker logs from the demo run were not retained, so their absence can't be proven.
- **Load numbers use the mock LLM.** Its `chars/4` token estimate (926 input tokens per message) is about 1.8× lower than the real tokenizer's 1,697 (COST.md §5).
- **Per-process circuit breakers.** Each worker keeps its own breaker in memory. After an outage, each one cools down independently (30 s), and a failed half-open probe re-opens it. A shared breaker in Postgres or Redis would be steadier.
- **Ambiguous-timeout gap (residual risk).** We wait for the client timeout plus 0.5 s before looking a booking up. A lab that commits even later than that, *and* has no idempotency, could still get a second POST for the same patient and slot. The gateway's own constraints still block a second gateway booking. See PLAN.md §8.
- **Graph sender and deploy configs are untested.** `WHATSAPP_MODE=graph` is a stub that was never run against Meta. `render.yaml` and `DEPLOY.md` were written but not deployed.
- **Single API process.** The API runs as one process so its in-process latency percentiles stay exact. Scale out by adding containers.
- **Mixed date formats in replies.** The reconciler's "booking confirmed" follow-up message refers to the slot by id, not by its friendly time.

## LLM spend

From `make spend` (append-only `llm_spend_ledger`), 2026-11-21:

| | |
|---|---|
| Model | `gemini-3.1-flash-lite` |
| Real calls | 37 (20 messages: full demo plus a 2-scenario rerun after prompt tuning) |
| Tokens | 33,941 input + 1,708 output → **1,697 input + 85 output per message** |
| **Billed** | **₹0** (Gemini free tier) |
| **Paid-tier equivalent** | **$0.011047 ≈ ₹0.97** |
| Not in the ledger | the live smoke check: 3 calls, paid-equivalent $0.000514, plus one HTTP 404 call |

These numbers were captured before the final `docker compose down -v`. That command deletes the Postgres volume, and the ledger with it: the ledger is append-only for the life of a database, not across volume deletion. The budget cap is `LLM_BUDGET_USD=2.00`. The project limit was ₹500. The 1M-messages-a-month cost recomputed from these measured tokens is in COST.md §5: base case $633 a month, or $0.63 per 1,000 messages.

## What I learned from the real model

Full transcripts are in [docs/REAL_LLM_DEMO.md](docs/REAL_LLM_DEMO.md); the rerun after tuning is in [docs/REAL_LLM_DEMO_RERUN.md](docs/REAL_LLM_DEMO_RERUN.md).

- **It mostly worked on the first try.** All 7 scenarios passed their database checks with zero double bookings:
  - happy path
  - conflict, then rebook
  - medical handoff
  - angry-user handoff
  - duplicate webhook
  - lab outage
  - out of scope

  The tool arguments were well-formed. It never invented a slot id; server-side validation would have rejected one anyway.
- **Relative slot references were resolved wrongly.** After 08:00 was taken, "the next available slot after that one" was booked as **09:00 instead of 08:30**. Fix: a prompt rule to resolve relative picks against the time order of the most recently listed slots, and to ask when it's ambiguous. The rerun booked 08:30.
- **It over-promised.** After an already-confirmed booking it said "You'll receive a confirmation shortly!". Fix: a prompt rule that a confirmation states test, date, time and name and promises nothing further. The rerun complied.
- **Out-of-scope requests** (booking a restaurant table) were declined politely without tool calls. An explicit decline rule was added as well.
- **Regression guard:** `tests/unit/test_agent_units.py::test_prompt_keeps_rules_added_after_real_model_demo`. Model behaviour itself can't be unit-tested deterministically, so the transcripts are the evidence.
- **Live behaviour beat the docs:** a model the docs list as served returned 404 for a new key, and the mock's token estimate was 2× low. Both now feed into the docs and the cost model.

## Repo layout

```
app/api        webhook, metrics, admin, health, latency middleware
app/ingest     dedupe + rate limit + enqueue (one SQL call per message)
app/worker     claim loops, lease heartbeat, commit/fail, leader (outbox, reconciler)
app/agent      agent loop, tools + validation, prompts; llm/ = mock, anthropic, openai, pricing, budget
app/lab        lab client, retry, circuit breaker, idempotent booking service
app/whatsapp   signature, payload models, sender (mock / Graph stub)
mocklab/       flaky in-memory lab with chaos controls and the double-booking detector
migrations/    schema + SQL functions (constraints carry the invariants)
scripts/       wa.py (signed payloads), load_test.py, demo_real_llm.py, spend_report.py, video_demo.sh
tests/         unit/, db/ (real Postgres), integration/ (live stack, failure injection)
```
