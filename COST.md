# COST: 1 million WhatsApp messages a month

All prices are USD, taken from the providers' published pages on **2026-11-20**. INR uses **₹88 per $1**, which is an assumption (`USD_INR` in `.env`). The token figures start from what was **measured** in the load test (`LOAD_REPORT.md`) and are then scaled explicitly in the scenarios below.

> **Update 2026-11-21:** the runtime LLM is now `gemini-3.1-flash-lite`, and real tokens per message have been measured. **§5 has the measured numbers and the recomputed 1M-messages-a-month cost** (base case $633 a month, $0.63 per 1,000 messages). Sections 1–4 are the original mock-token estimate on Claude Haiku 5.5, kept for comparison.

## 1. Inputs

| Input | Value | Source |
|---|---|---|
| Volume | 1,000,000 inbound messages a month (about 0.39 msg/s on average; bursts handled by the queue) | brief |
| Messages reaching the LLM | Assumed **100%**, which is conservative. In the load test only 710 of 1,000 did (71%): duplicates and rate-limited spam never call the LLM. | LOAD_REPORT |
| Measured LLM calls per processed message | **1.40** (993 calls for 710 messages; coalescing merges bursts into one turn) | LOAD_REPORT |
| Measured tokens per processed message | **800 input + 38 output**, from the mock's `ceil(chars/4)` estimate. This is the run these sections were built on. After the prompt tuning on 2026-11-21 the same load test measures 926 + 38 (see §5). | LOAD_REPORT (earlier run) |
| Default model | Claude Haiku 5.5 (`claude-haiku-5-5`): **$0.10 / MTok input, $0.50 / MTok output**, $0.01 cache read, $0.125 cache write (5 min); prompts ≤100K tokens | https://platform.claude.com/docs/en/about-claude/pricing |

The mock's character-based estimate is not the real tokenizer. It also doesn't count the real model's thinking tokens, which are billed as output; the adapter runs at `effort: low` to keep those small. The scenarios therefore scale the measured numbers up rather than trusting them as-is:

| Scenario | Input tok/msg | Output tok/msg | Rationale |
|---|---|---|---|
| Low | 800 | 38 | the measured figures as they are |
| **Base** | **1,600** | **115** | 2× input for tokenizer and tool-schema overhead; 3× output for longer real replies plus some thinking |
| High | 3,200 | 300 | long histories (8 turns kept) and heavier thinking |

## 2. LLM cost (Claude Haiku 5.5)

Cost per message = input × $0.10/1e6 + output × $0.50/1e6

| Scenario | Per message | × 1M messages | INR / month |
|---|---|---|---|
| Low | 800×0.10e-6 + 38×0.50e-6 = **$0.000099** | **$99** | ₹8,712 |
| **Base** | 1,600×0.10e-6 + 115×0.50e-6 = **$0.0002175** | **$217.50** | **₹19,140** |
| High | 3,200×0.10e-6 + 300×0.50e-6 = **$0.00047** | **$470** | ₹41,360 |

### Same base tokens (1,600 in / 115 out) on other models

| Model | $/MTok in / out | Per message | 1M messages | Source |
|---|---|---|---|---|
| gpt-5-nano | 0.05 / 0.40 | $0.000126 | **$126** | developers.openai.com/api/docs/pricing |
| **claude-haiku-5-5 (default)** | 0.10 / 0.50 | $0.0002175 | **$217.50** | platform.claude.com pricing |
| gpt-4o-mini | 0.15 / 0.60 | $0.000309 | $309 | developers.openai.com/api/docs/pricing |
| gpt-5-mini | 0.25 / 2.00 | $0.00063 | $630 | developers.openai.com/api/docs/pricing |
| claude-haiku-4-5 | 1 / 5 | $0.002175 | $2,175 | platform.claude.com pricing |
| claude-sonnet-5-5 | 2 / 10 | $0.00435 | $4,350 | platform.claude.com pricing |

All of these are interchangeable via `LLM_PROVIDER`, `LLM_MODEL` and `LLM_BASE_URL`, and their prices are already in `pricing.py`. A cheaper model only pays off if it still calls the tools correctly. Before switching, run the `scripts/demo_real_llm.py` conversations (or a larger eval) against it. The Batch API's 50% discount doesn't apply here, because replies are interactive.

### Option: prompt caching

The static prefix (tool schemas plus fixed rules) measures about **530 tokens per call** (2,131 characters ÷ 4). In the measured numbers that is about 93% of input, because conversations are short. For the base scenario, assume a conservative **60%** of input tokens are cacheable. Suppose they are served from cache at Haiku 5.5's cache-read price of $0.01 (base scenario: 960 tokens cached, 640 uncached):

960 × 0.01e-6 + 640 × 0.10e-6 + 115 × 0.50e-6 = $0.0001313 per message, or **$131 a month instead of $217.50 (−40%)**. Cache writes add about $0.125/MTok each time the 5-minute cache expires. At 0.4 msg/s the cache stays warm.

Two caveats (not enabled by default):
1. **Minimum cacheable length.** Anthropic only caches prefixes above a model-dependent minimum (512–4,096 tokens). By estimate our static prefix is about 530 tokens, which may be below it. Check `cache_read_input_tokens` in `llm_usage`; the adapter already records it.
2. **Volatile fields last.** `TODAY`, `STATE` and `CONTEXT_JSON` would have to move to the end, after the cache breakpoint, so the prefix stays byte-identical across requests.

### Other levers already built in

- **Dedupe and rate limiting.** In the load test these kept 29% of traffic away from the LLM.
- **Coalescing.** A burst of messages from one user becomes one agent turn.
- **History trimmed to 8 turns.**
- **Loop guard.** At most 4 LLM calls per turn.
- **Budget guard.** `LLM_BUDGET_USD` sets a hard cap.

## 3. Infrastructure (Fly.io prices, iad/ewr region)

| Component | Choice | $/month | Source |
|---|---|---|---|
| API | 2 × shared-cpu-1x 1 GB (redundancy; one is enough for 0.4 msg/s) | 2 × $6.70 = **$13.40** | https://docs.fly.io/about/pricing |
| Workers | 2 × shared-cpu-2x 2 GB, each with 8 concurrent claim loops | 2 × $13.39 = **$26.78** | same |
| Postgres | Fly Managed Postgres Basic | **$38.00** | same |
| DB storage | about 3 KB per message across inbound, outbound, usage and lab rows → about 3 GB a month, 90 days kept ≈ 10 GB × $0.28 | **$2.80** | same |
| **Infra total** | | **≈ $81 / month (₹7,128)** | |

The cheaper Postgres alternative is Neon Launch: $0.106 per CU-hour and $0.35 per GB-month (https://neon.com/pricing). 0.5 CU always on comes to 0.5 × 730 h × $0.106 = $38.69, plus 10 GB × $0.35 = $3.50. That is about the same as Fly MPG, and less if it scales to zero.

Not included: Meta's WhatsApp Business messaging charges (billed by Meta per conversation or template, outside this system), the lab's own API, and log or metrics hosting (Prometheus and Grafana can be self-hosted on the same machines).

### Capacity check

- The load test pushed 1,000 messages in 64 s, including bursts of 20 in under a second. The workers finished the backlog about 1 s after the last send.
- Webhook ack latency was p99 12.9–14.4 ms (client, across the last two runs) and 9.6 ms (server).
- 1M messages a month is about 0.4 msg/s on average. Even a 50× peak (20 msg/s) is well within what two workers handled in the test.
- Turns are I/O-bound: lab latency is 0.3–1.5 s and LLM latency is in seconds. Throughput therefore scales with `WORKER_CONCURRENCY`, not with CPU.

### When to move beyond the Postgres queue

Postgres stays the right choice while claims are cheap. Add a dedicated queue or Redis when any of these happens:
- sustained traffic exceeds roughly **200–500 msg/s**;
- `queue_oldest_age_seconds` grows even with more workers;
- vacuum can't keep up with churn on `inbound_messages`;
- multi-region ingest becomes necessary.

At that point the move would be SQS (or Redis Streams) for the work queue and Redis for the token buckets. The dedupe and booking invariants stay in Postgres.

## 4. Totals

| Scenario | LLM | Infra | **Total / month** | **Per 1,000 messages** |
|---|---|---|---|---|
| Low | $99 | $81 | **$180** (₹15,840) | **$0.18** (₹15.8) |
| **Base** | $217.50 | $81 | **$298.50** (₹26,268) | **$0.30** (₹26.3) |
| Base + prompt caching | $131 | $81 | $212 (₹18,656) | $0.21 (₹18.7) |
| High | $470 | $81 | **$551** (₹48,488) | **$0.55** (₹48.5) |

At this volume the LLM is 55–85% of the cost, so the levers that matter are tokens per message and model choice, not servers.

## 5. Measured with the real model (2026-11-21), and recomputed

### What was actually spent

| Item | Value | Source |
|---|---|---|
| Model | `gemini-3.1-flash-lite`, through Gemini's OpenAI-compatible endpoint | `docs/REAL_LLM_DEMO.md` |
| Real demo traffic | 20 messages (13 in the full demo, 7 in the post-tuning rerun), **37 LLM calls** | `make spend` |
| Tokens | **33,941 input + 1,708 output** (0 cache reads) | `make spend` |
| **Billed** | **$0.00 (₹0)**, on the free tier | `make spend` with `LLM_BILLING_TIER=free` |
| Paid-tier equivalent | **$0.011047 (₹0.97)** = 33,941 × $0.25/1e6 + 1,708 × $1.50/1e6 | `make spend` |
| Live smoke check (not in the ledger) | 3 successful calls, paid-equivalent $0.000514, plus 1 call that returned HTTP 404 (`gemini-2.5-flash-lite` "no longer available to new users") | `make smoke-real` output |

**Price source:** https://aistudio.google.com/docs/pricing (redirected from ai.google.dev/gemini-api/docs/pricing), fetched 2026-11-21. `gemini-3.1-flash-lite` paid tier: $0.25/1M input (text), $1.50/1M output (thinking included), $0.025 context caching. The free tier is "Free of charge", and on the free tier "content [is] used to improve our products".

### Real vs. mock tokens per message

| | Input tok / msg | Output tok / msg | LLM calls / msg |
|---|---|---|---|
| Mock estimate (load test, `chars/4`, current prompt) | 926 | 38 | 1.40 |
| **Real, measured** (Gemini tokenizer, 20 messages) | **1,697** | **85** | **1.85** |
| Ratio | 1.8× | 2.2× | 1.3× |

The real numbers are about 2× the mock estimate. They land close to the earlier "Base" assumption (1,600 / 115), so the measured values replace the guesses below. The demo mix leans towards booking flows, which take 2 calls each; handoffs take 1. Real traffic with more one-shot questions would come in slightly lower.

### 1M messages a month, recomputed from the measured tokens (1,697 in / 85 out)

| Scenario | Assumption | Per message | LLM / month | + infra $81 | **Per 1,000 msgs** |
|---|---|---|---|---|---|
| **Low** | measured tokens; only 71% of messages reach the LLM (duplicate and spam filtering as in the load test) | 0.71 × $0.00055175 | **$391.74** | $472.74 (₹41,601) | **$0.47** |
| **Base** | measured tokens; every message reaches the LLM | 1,697×0.25e-6 + 85×1.50e-6 = **$0.00055175** | **$551.75** | $632.75 (₹55,682) | **$0.63** |
| **High** | 2× input (8-turn histories), 3× output: 3,394 / 255 | 3,394×0.25e-6 + 255×1.50e-6 = $0.0012310 | **$1,231.00** | $1,312.00 (₹115,456) | **$1.31** |

**Same measured tokens on other models.** These are a rough comparison: each provider's tokenizer counts differently.

| Model | Per message | LLM / month | Note |
|---|---|---|---|
| gemini-3.1-flash-lite (used) | $0.00055175 | $551.75 | cheapest Gemini Flash-class model a new key could call |
| gemini-2.5-flash-lite | 1,697×0.10e-6 + 85×0.40e-6 = $0.0002037 | $203.70 | listed at $0.10/$0.40 but returned 404 for this new key |
| claude-haiku-5-5 | 1,697×0.10e-6 + 85×0.50e-6 = $0.0002122 | $212.20 | supported by the existing Anthropic adapter |
| gemini free tier | $0 | $0 | **not usable for production patient traffic:** free-tier content is used to improve Google's products, and rate limits apply |

**Prompt caching** was not observed (0 cached tokens). The static prefix (tools plus rules) was 664 input tokens on the first call of a turn. That's about 70% of per-message input (1.85 calls × ~640). If Gemini context caching applied to it at $0.025/1M:

1,184 × 0.025e-6 + 513 × 0.25e-6 + 85 × 1.50e-6 = $0.0002853 per message, or **$285 a month (−48%)**.

This is not verified: it depends on Gemini's minimum cacheable size, and caching is not available on the free tier.

**Bottom line:** at 1M messages a month with the measured tokens, expect about **$470–$1,310 a month all-in on `gemini-3.1-flash-lite`**, with **$633 as the base case** ($0.63 per 1,000 messages). Switching to Haiku 5.5 or caching the static prefix roughly halves the LLM part. Sections 2–4 above are the earlier mock-token estimate for Haiku 5.5, kept for comparison.

### Budget guard

`make spend` reads the append-only `llm_spend_ledger`, which survives load-test resets. The worker refuses real calls once the recorded paid-equivalent spend reaches `LLM_BUDGET_USD` ($2.00). The ₹0.97 spent is 0.6% of that cap and 0.2% of the ₹500 limit.
